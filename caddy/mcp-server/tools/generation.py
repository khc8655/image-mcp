"""生图与 AI 改图工具。"""
import time
import asyncio
from typing import Annotated, Literal, Optional

import httpx
from pydantic import Field
from mcp import types as mcp_types

from config_manager import config_mgr
from security import sanitize_error

import providers
from core import (
    mcp, logger, Context, ASPECT_TO_SIZE, SCENE_PRESETS, AspectRatio, Resolution, Scene, Attempt, GenerationResult, resolve_image_to_base64, _tool_result, _progress,
    _annotations, _pick_candidates, _build_generation_content,
)


@mcp.tool(annotations=_annotations(read_only=False, title="生成图片"))
async def generate_image(
    prompt: str,
    aspect_ratio: AspectRatio = "auto",
    resolution: Resolution = "auto",
    scene: Scene = "auto",
    negative_prompt: str = "",
    seed: int = -1,
    count: Annotated[int, Field(ge=1, le=4)] = 1,
    model: str = "",
    action: Literal["generate", "edit"] = "generate",
    image: str = "",
    mask: str = "",
    ctx: Optional[Context] = None,
) -> Annotated[mcp_types.CallToolResult, GenerationResult]:
    """文生图（需要改图请优先用 edit_image）。按后台配置的模型顺序调度，失败或超时自动切换下一个。

    参数说明:
        prompt: 画面描述，中英文均可。
        aspect_ratio: "auto"(默认，跟随场景模板/后台配置，均无则 1:1) 或指定比例；显式指定不会被覆盖。
        resolution: "auto" | "1K" | "2K" | "4K"。
        scene: 办公场景模板。"auto" 跟随后台；"none" 不套模板；
               infra_scene 信息化全景 / network_topology 网络拓扑 / architecture 软件架构 /
               infographic 汇报海报 / icon_set 图标组。
        negative_prompt: 不希望出现的元素。
        seed: >=0 固定随机性以便复现；-1 随机。
        count: 张数 1~4。
        model: 可选，指定模型，格式 "provider/model"、模型 ID 或 provider 名。
    返回: 签名链接 + 缩略图（或原图），structuredContent 中含模型、尺寸、seed、失败记录。
    """
    client: httpx.AsyncClient = getattr(mcp, "_http_client", None)
    if not client:
        raise RuntimeError("HTTP 客户端未启动")
    started = time.monotonic()

    bound_models = [
        m for m in config_mgr.get("generation", "models", default=[])
        if isinstance(m, dict) and m.get("enabled", True)
    ]
    if not bound_models:
        raise RuntimeError("当前服务端尚未绑定任何可用生图模型，请在 Web 管理端 (/admin) 挂载生图模型")

    # 1. 办公场景预设模板解析与提示词拼装
    active_scene = (scene or "auto").strip()
    if active_scene == "auto":
        active_scene = config_mgr.get("generation", "default_scene", default="none") or "none"

    final_prompt = prompt.strip()
    final_negative = negative_prompt.strip()
    target_aspect = (aspect_ratio or "auto").strip()
    target_res = resolution

    if active_scene in SCENE_PRESETS:
        sc = SCENE_PRESETS[active_scene]
        logger.info(f"启用办公生图场景模板: [{sc['name']}] ({active_scene})")
        final_prompt = f"{sc['prompt_prefix']}{final_prompt}{sc['prompt_suffix']}"
        if sc.get("negative_prompt"):
            final_negative = f"{sc['negative_prompt']}, {final_negative}" if final_negative else sc["negative_prompt"]
        if target_aspect == "auto" and sc.get("default_aspect"):
            target_aspect = sc["default_aspect"]
        if (target_res == "auto" or not target_res) and sc.get("default_resolution"):
            target_res = sc["default_resolution"]

    if target_aspect == "auto":
        # 兼容 Web 控制台写入的 default_aspect_ratio 与旧字段 default_aspect
        cfg_aspect = (
            config_mgr.get("generation", "default_aspect_ratio", default="")
            or config_mgr.get("generation", "default_aspect", default="")
        )
        if cfg_aspect and cfg_aspect in ASPECT_TO_SIZE:
            target_aspect = cfg_aspect
    if target_res == "auto" or not target_res:
        cfg_res = config_mgr.get("generation", "default_resolution", default="")
        if cfg_res:
            target_res = cfg_res

    target_aspect = target_aspect if target_aspect in ASPECT_TO_SIZE else "1:1"

    image_b64 = ""
    mask_b64 = ""
    if action == "edit":
        if not image:
            raise ValueError("action=edit 时必须提供 image")
        image_b64 = await resolve_image_to_base64(client, image)
        if mask:
            mask_b64 = await resolve_image_to_base64(client, mask)

    candidates = _pick_candidates(bound_models, model)
    timeout_sec = float(config_mgr.get("generation", "timeout_seconds", default=45))
    failures: list[Attempt] = []
    total = len(candidates)

    # 2. 顺序优先级调度 + 链式故障转移
    for idx, cand in enumerate(candidates):
        rules = config_mgr.get_image_adapter_rules(cand["provider"])
        tag = f"{cand['provider']}/{cand['model']}"
        if action == "edit" and not rules.get("supports_edit", True):
            continue
        if mask_b64 and not rules.get("supports_mask", cand["provider"] == "google"):
            failures.append(Attempt(target=tag, error="不支持 mask 局部重绘，已跳过"))
            continue
        await _progress(ctx, idx, total, f"正在调用 {tag}")
        try:
            raw = await asyncio.wait_for(
                providers.execute_image_generation(
                    client=client,
                    provider_id=cand["provider"],
                    model=cand["model"],
                    key_label=cand.get("key", ""),
                    prompt=final_prompt,
                    aspect_ratio=target_aspect,
                    action=action,
                    image_b64=image_b64,
                    mask_b64=mask_b64,
                    resolution=target_res,
                    seed=seed,
                    negative_prompt=final_negative,
                    count=count,
                    advanced=cand.get("advanced") or {}
                ),
                timeout=timeout_sec
            )
            if raw:
                images, blocks = _build_generation_content(raw)
                if not images:
                    raise RuntimeError("上游未返回可解析的图像数据")
                await _progress(ctx, total, total, "完成")
                result = GenerationResult(
                    provider=cand["provider"], model=cand["model"], images=images,
                    aspect_ratio=target_aspect, resolution=str(target_res), scene=active_scene,
                    seed=seed, elapsed_ms=int((time.monotonic() - started) * 1000),
                    failed_attempts=failures,
                )
                lines = [f"已由 {tag} 生成 {len(images)} 张图片（{target_aspect}, {target_res}）。"]
                for n, im in enumerate(images, 1):
                    lines.append(f"{n}. {im.url or im.filename}" + (f" ({im.width}x{im.height})" if im.width else ""))
                if failures:
                    lines.append("已跳过: " + "; ".join(f"{a.target}: {a.error}" for a in failures))
                return _tool_result("\n".join(lines), result, blocks)
        except asyncio.TimeoutError:
            failures.append(Attempt(target=tag, error=f"请求超时 ({timeout_sec:.0f}s)"))
            logger.warning(f"生图模型 {tag} 响应超时，切换下一优先级模型...")
        except Exception as e:
            failures.append(Attempt(target=tag, error=sanitize_error(e)))
            logger.warning(f"生图模型 {tag} 执行失败: {sanitize_error(e)}，切换下一优先级模型...")

    detail = "; ".join(f"{a.target}: {a.error}" for a in failures) or "没有支持该操作的模型"
    raise RuntimeError(f"所有可用生图模型均执行失败: {detail}")


@mcp.tool(annotations=_annotations(read_only=False, title="编辑图片"))
async def edit_image(
    image: str,
    prompt: str,
    mask: str = "",
    aspect_ratio: AspectRatio = "auto",
    resolution: Resolution = "auto",
    negative_prompt: str = "",
    seed: int = -1,
    count: Annotated[int, Field(ge=1, le=4)] = 1,
    model: str = "",
    ctx: Optional[Context] = None,
) -> Annotated[mcp_types.CallToolResult, GenerationResult]:
    """用 AI 编辑已有图片：改风格、增删元素、以原图为参考重绘。

    参数说明:
        image: 原图，公网 URL 或 Base64 / data URI。
        prompt: 编辑指令，例如“把背景换成海边日落”。
        mask: 可选，局部重绘蒙版（白色区域为重绘区），只会路由到支持 mask 的模型。
        其余参数同 generate_image。
    裁剪、缩放、旋转、格式转换、去背景等确定性操作请用 transform_image，不消耗模型额度。
    """
    return await generate_image(
        prompt=prompt, action="edit", image=image, mask=mask, scene="none",
        aspect_ratio=aspect_ratio, resolution=resolution, negative_prompt=negative_prompt,
        seed=seed, count=count, model=model, ctx=ctx,
    )
