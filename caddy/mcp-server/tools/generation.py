"""生图与 AI 改图工具。

提示词由调用方 Agent 按 Gemini Nano Banana 的写法撰写并原样发送；Agent 没传的参数一律走后台默认值。
后台模型列表按顺序调度，前面的失败才降级到后面的模型，降级时按目标模型的硬限制做最小适配。
"""
import time
import asyncio
from typing import Annotated, Optional

import httpx
from pydantic import Field
from mcp import types as mcp_types

from config_manager import config_mgr
from security import sanitize_error

import providers
import prompting
from core import (
    mcp, logger, Context, ASPECT_TO_SIZE, AspectRatio, Resolution, Quality, Attempt, GenerationResult,
    resolve_image_to_base64, _tool_result, _progress, _annotations, _build_generation_content,
)

ALL_ASPECTS = list(ASPECT_TO_SIZE.keys())


def _gen_cfg(key: str, default=None):
    return config_mgr.get("generation", key, default=default)


def _resolve_defaults(aspect_ratio, resolution, quality, grounding, count):
    """Agent 未传（auto/None）的参数用后台配置补齐。"""
    aspect = (aspect_ratio or "auto").strip()
    if aspect == "auto":
        aspect = _gen_cfg("default_aspect_ratio", "") or _gen_cfg("default_aspect", "") or "1:1"
    if aspect not in ASPECT_TO_SIZE:
        aspect = "1:1"

    res = (resolution or "auto").strip()
    if res == "auto":
        res = _gen_cfg("default_resolution", "") or "2K"

    q = (quality or "auto").strip()
    if q == "auto":
        q = _gen_cfg("default_quality", "auto") or "auto"

    g = grounding if grounding is not None else bool(_gen_cfg("default_grounding", False))
    n = count if count is not None else int(_gen_cfg("default_count", 1) or 1)
    return aspect, res, q, g, max(1, min(int(n), 4))


def _order_by_quality(models: list[dict], quality: str) -> list[dict]:
    """best：Pro 级模型提到最前；fast：Pro 级模型放到最后；auto：后台顺序。"""
    if quality not in ("best", "fast"):
        return list(models)
    premium = [m for m in models if prompting.is_premium_model(m.get("model", ""))]
    rest = [m for m in models if m not in premium]
    return premium + rest if quality == "best" else rest + premium


async def _run(
    *, prompt: str, action: str, aspect_ratio, resolution, quality, grounding, count,
    image: str = "", reference_images: Optional[list[str]] = None, mask: str = "",
    ctx: Optional[Context] = None,
) -> mcp_types.CallToolResult:
    client: httpx.AsyncClient = getattr(mcp, "_http_client", None)
    if not client:
        raise RuntimeError("HTTP 客户端未启动")
    if not (prompt or "").strip():
        raise ValueError("prompt 不能为空：请用完整句子描述画面（主体、动作、场景、构图、风格）")
    started = time.monotonic()

    bound = [m for m in _gen_cfg("models", []) if isinstance(m, dict) and m.get("enabled", True)]
    if not bound:
        raise RuntimeError("服务端尚未配置生图模型，请在 Web 管理端 (/admin) 添加")

    aspect, res, q, use_grounding, n = _resolve_defaults(aspect_ratio, resolution, quality, grounding, count)
    thinking = "high" if q == "best" else ""

    image_b64, mask_b64, refs_b64 = "", "", []
    if action == "edit":
        if not image:
            raise ValueError("edit_image 需要 image")
        image_b64 = await resolve_image_to_base64(client, image)
        if mask:
            mask_b64 = await resolve_image_to_base64(client, mask)
        for ref in (reference_images or [])[:13]:
            refs_b64.append(await resolve_image_to_base64(client, ref))

    candidates = _order_by_quality(bound, q)
    timeout_sec = float(_gen_cfg("timeout_seconds", 45))
    # 思考档位与搜索接地会显著拉长耗时，给足时间
    if thinking or use_grounding:
        timeout_sec = max(timeout_sec, float(_gen_cfg("timeout_seconds_extended", 120)))
    failures: list[Attempt] = []
    total = len(candidates)

    for idx, cand in enumerate(candidates):
        provider = cand["provider"]
        rules = config_mgr.get_image_adapter_rules(provider)
        tag = f"{provider}/{cand['model']}"
        if action == "edit" and not rules.get("supports_edit", True):
            continue
        if action == "edit" and provider == "minimax":
            failures.append(Attempt(target=tag, error="MiniMax 只支持人物参考图，不支持通用改图，已跳过"))
            continue
        if mask_b64 and not rules.get("supports_mask", provider == "google"):
            failures.append(Attempt(target=tag, error="不支持 mask，已跳过"))
            continue

        allowed = rules.get("allowed_aspect_ratios") or ALL_ASPECTS
        if provider == "google":
            send_prompt, send_aspect, send_res, notes = prompt, prompting.nearest_aspect(aspect, allowed), res, []
            if send_aspect != aspect:
                notes.append(f"{cand['model']} 不支持 {aspect}，改用最接近的 {send_aspect}")
        else:
            send_prompt, send_aspect, send_res, notes = prompting.adapt_request(
                provider, prompt, aspect, res, allowed, use_grounding,
            )

        await _progress(ctx, idx, total, f"正在调用 {tag}")
        try:
            raw = await asyncio.wait_for(
                providers.execute_image_generation(
                    client=client,
                    provider_id=provider,
                    model=cand["model"],
                    key_label=cand.get("key", ""),
                    prompt=send_prompt,
                    aspect_ratio=send_aspect,
                    action=action,
                    image_b64=image_b64,
                    mask_b64=mask_b64,
                    resolution=send_res,
                    count=n,
                    advanced=cand.get("advanced") or {},
                    reference_images_b64=refs_b64,
                    grounding=use_grounding and provider == "google",
                    thinking=thinking if provider == "google" else "",
                ),
                timeout=timeout_sec,
            )
            if not raw:
                raise RuntimeError("上游未返回图像")
            images, blocks = _build_generation_content(raw)
            if not images:
                raise RuntimeError("上游未返回可解析的图像数据")
            await _progress(ctx, total, total, "完成")
            note = next((r.get("note") for r in raw if r.get("note")), "")
            result = GenerationResult(
                provider=provider, model=cand["model"], images=images,
                aspect_ratio=send_aspect, resolution=str(send_res), quality=q,
                grounding=use_grounding and provider == "google",
                elapsed_ms=int((time.monotonic() - started) * 1000),
                model_note=note or "", adjustments=notes, failed_attempts=failures,
            )
            lines = [f"已由 {tag} 生成 {len(images)} 张图片（{send_aspect}, {send_res}）。"]
            for i, im in enumerate(images, 1):
                lines.append(f"{i}. {im.url or im.filename}" + (f" ({im.width}x{im.height})" if im.width else ""))
            if notes:
                lines.append("降级适配: " + "; ".join(notes))
            if failures:
                lines.append("已跳过: " + "; ".join(f"{a.target}: {a.error}" for a in failures))
            return _tool_result("\n".join(lines), result, blocks)
        except asyncio.TimeoutError:
            failures.append(Attempt(target=tag, error=f"请求超时 ({timeout_sec:.0f}s)"))
            logger.warning(f"生图模型 {tag} 超时，尝试下一个")
        except providers.SafetyBlocked as e:
            # 内容被拦截：换模型大概率同样被拦，直接把改写建议交还给 Agent
            raise RuntimeError(str(e)) from None
        except Exception as e:
            failures.append(Attempt(target=tag, error=sanitize_error(e)))
            logger.warning(f"生图模型 {tag} 失败: {sanitize_error(e)}，尝试下一个")

    detail = "; ".join(f"{a.target}: {a.error}" for a in failures) or "没有支持该操作的模型"
    raise RuntimeError(f"所有生图模型均失败: {detail}")


@mcp.tool(
    annotations=_annotations(read_only=False, title="生成图片"),
    description=prompting.GENERATE_DESCRIPTION,
)
async def generate_image(
    prompt: Annotated[str, Field(description="The full scene description, written per the rules above.")],
    aspect_ratio: Annotated[AspectRatio, Field(description="Omit to use the server default.")] = "auto",
    resolution: Annotated[Resolution, Field(description="Omit to use the server default.")] = "auto",
    quality: Annotated[Quality, Field(description="best = dense text / final deliverable; fast = drafts.")] = "auto",
    grounding: Annotated[Optional[bool], Field(description="True to search the web before drawing real things.")] = None,
    count: Annotated[Optional[int], Field(ge=1, le=4, description="Number of variations.")] = None,
    ctx: Optional[Context] = None,
) -> Annotated[mcp_types.CallToolResult, GenerationResult]:
    return await _run(
        prompt=prompt, action="generate", aspect_ratio=aspect_ratio, resolution=resolution,
        quality=quality, grounding=grounding, count=count, ctx=ctx,
    )


@mcp.tool(
    annotations=_annotations(read_only=False, title="编辑图片"),
    description=prompting.EDIT_DESCRIPTION,
)
async def edit_image(
    image: Annotated[str, Field(description="Image to edit: public URL, Base64 or data URI.")],
    prompt: Annotated[str, Field(description="What changes and what stays the same.")],
    reference_images: Annotated[Optional[list[str]], Field(description="Up to 13 extra images, referenced by order in the prompt.")] = None,
    mask: Annotated[str, Field(description="Optional mask (white = area to repaint). Usually unnecessary.")] = "",
    aspect_ratio: AspectRatio = "auto",
    resolution: Resolution = "auto",
    quality: Quality = "auto",
    count: Annotated[Optional[int], Field(ge=1, le=4)] = None,
    ctx: Optional[Context] = None,
) -> Annotated[mcp_types.CallToolResult, GenerationResult]:
    return await _run(
        prompt=prompt, action="edit", image=image, reference_images=reference_images, mask=mask,
        aspect_ratio=aspect_ratio, resolution=resolution, quality=quality, grounding=False, count=count, ctx=ctx,
    )


# ---------------------------------------------------------------------------
# 工作场景范例：MCP prompts（客户端可作斜杠命令；Agent 需要时拉取，平时不占上下文）
# ---------------------------------------------------------------------------
def _register_scene_prompt(key: str):
    sc = prompting.WORK_SCENE_PROMPTS[key]

    def _prompt(subject: str = "") -> str:
        return prompting.scene_prompt_text(key, subject)

    _prompt.__name__ = key
    mcp.prompt(name=key, title=sc["title"], description=sc["description"])(_prompt)


for _key in prompting.WORK_SCENE_PROMPTS:
    _register_scene_prompt(_key)
