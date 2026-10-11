"""识图与分级 OCR 工具。"""
import re
import json
import base64
import asyncio
from typing import Annotated, Optional

import httpx
from mcp import types as mcp_types

from config_manager import config_mgr
from security import sanitize_error

import providers
from core import (
    mcp, logger, Context, VisionPreset,
    Attempt, UIElement, VisionResult, resolve_image_to_base64, get_rapidocr_engine, _tool_result, _progress,
    _annotations, _pick_candidates,
)
from cache import vision_cache, make_key


# ---------------------------------------------------------------------------
# MCP 核心工具 2: 识图与分级 OCR (顺序优先级调度 + 链式故障转移)
# ---------------------------------------------------------------------------
GUI_INSTRUCTION = (
    "你是一个界面自动化定位助手。识别画面中所有可交互元素（按钮、输入框、链接、开关、菜单项等）。"
    "只输出一个 JSON 数组，不要任何解释或 Markdown 代码块，每项格式为："
    '{"label": "元素文字或用途", "type": "button|input|link|checkbox|menu|other", "box": [ymin, xmin, ymax, xmax]}，'
    "坐标为 0~1000 的整数归一化坐标。"
)
DOCUMENT_INSTRUCTION = (
    "你是一个高精度文档与表格解析专家。请将图片中的所有文字、数据、财务报表提取为标准 GitHub Flavored Markdown (GFM)，"
    "表格用 GFM 表格，严格保留列对齐、数字精度和层次标题结构。"
)


def parse_gui_elements(text: str) -> Optional[list[UIElement]]:
    """从模型输出中解析 GUI 元素 JSON，并把坐标钳制在 0~1000。解析失败返回 None。"""
    m = re.search(r"\[\s*\{.*\}\s*\]", text or "", re.S)
    if not m:
        return None
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None
    out: list[UIElement] = []
    for item in data if isinstance(data, list) else []:
        box = item.get("box") if isinstance(item, dict) else None
        if not (isinstance(box, list) and len(box) == 4):
            continue
        try:
            y1, x1, y2, x2 = [max(0, min(1000, int(round(float(v))))) for v in box]
        except (TypeError, ValueError):
            continue
        y1, y2 = sorted((y1, y2))
        x1, x2 = sorted((x1, x2))
        out.append(UIElement(
            label=str(item.get("label", ""))[:200], type=str(item.get("type", ""))[:32],
            box=[y1, x1, y2, x2], center=[(y1 + y2) // 2, (x1 + x2) // 2],
        ))
    return out


async def _local_ocr_text(image_b64: str) -> str:
    if not config_mgr.get("vision", "rapidocr_enabled", default=True):
        return ""
    try:
        engine = get_rapidocr_engine()
        result, _ = await asyncio.get_running_loop().run_in_executor(None, engine, base64.b64decode(image_b64))
        return "\n".join(item[1] for item in (result or []))
    except Exception as e:
        logger.info(f"本地 OCR 校对文本不可用: {sanitize_error(e)}")
        return ""


async def _describe(
    client: httpx.AsyncClient, image_b64: str, prompt: str, preset: str, model: str, ctx=None
) -> VisionResult:
    active_preset = preset.strip() or config_mgr.get("vision", "default_preset", default="standard")
    logger.info(f"describe_image: preset={active_preset}")

    # 等级 1: fast - 本地 RapidOCR，失败或为空时降级云端
    if active_preset == "fast" and config_mgr.get("vision", "rapidocr_enabled", default=True):
        try:
            ocr_engine = get_rapidocr_engine()
            raw_bytes = base64.b64decode(image_b64)
            # ONNX 推理为同步 CPU 计算，放入线程池避免阻塞事件循环
            result, _ = await asyncio.get_running_loop().run_in_executor(None, ocr_engine, raw_bytes)
            if result:
                text = "\n".join(item[1] for item in result)
                return VisionResult(text=text, preset="fast", engine="rapidocr")
        except Exception as e:
            logger.warning(f"本地 RapidOCR 不可用，降级至云端多模态模型池: {sanitize_error(e)}")

    cache_ttl = float(config_mgr.get("vision", "cache_ttl_seconds", default=3600) or 0)
    cache_key = make_key("vision", image_b64, prompt, active_preset, model)
    cached = vision_cache.get(cache_key)
    if cached is not None:
        return cached

    system_instruction = {"document": DOCUMENT_INSTRUCTION, "gui": GUI_INSTRUCTION}.get(active_preset, "")
    full_prompt = f"{system_instruction}\n\n用户要求: {prompt}".strip()

    # document 预设：先用本地 OCR 抽取文字，作为校对参考交给多模态模型，提升数字与字符准确率
    if active_preset == "document" and config_mgr.get("vision", "document_ocr_grounding", default=True):
        ocr_text = await _local_ocr_text(image_b64)
        if ocr_text:
            full_prompt += (
                "\n\n以下是本地 OCR 引擎识别出的原始文字（逐行，可能有错字但数字通常可靠）。"
                "请以图片版面为准组织结构，用这些文字校对数字与专有名词：\n" + ocr_text[:6000]
            )

    vision_models = [
        m for m in config_mgr.get("vision", "models", default=[])
        if isinstance(m, dict) and m.get("enabled", True)
    ]
    if not vision_models:
        raise RuntimeError("当前未绑定任何多模态识图模型，请在 Web 管理端 (/admin) 挂载识图模型")

    candidates = _pick_candidates(vision_models, model)
    timeout_sec = float(config_mgr.get("vision", "timeout_seconds", default=45))
    failures: list[Attempt] = []
    for idx, vm in enumerate(candidates):
        tag = f"{vm.get('provider')}/{vm.get('model')}"
        await _progress(ctx, idx, len(candidates), f"正在调用 {tag}")
        try:
            res_text = await asyncio.wait_for(
                providers.execute_vision_completion(
                    client=client,
                    provider_id=vm["provider"],
                    model=vm["model"],
                    key_label=vm.get("key", ""),
                    prompt=full_prompt,
                    image_b64=image_b64,
                    preset=active_preset
                ),
                timeout=timeout_sec
            )
            if res_text:
                elements = parse_gui_elements(res_text) if active_preset == "gui" else None
                result = VisionResult(
                    text=res_text, preset=active_preset, engine=tag, elements=elements, failed_attempts=failures,
                )
                vision_cache.set(cache_key, result, cache_ttl)
                return result
        except asyncio.TimeoutError:
            failures.append(Attempt(target=tag, error=f"请求超时 ({timeout_sec:.0f}s)"))
            logger.warning(f"识图节点 {tag} 超时，故障转移")
        except Exception as e:
            failures.append(Attempt(target=tag, error=sanitize_error(e)))
            logger.warning(f"识图节点 {tag} 异常，故障转移: {sanitize_error(e)}")

    raise RuntimeError("所有识图与 OCR 模型节点均执行失败: " + "; ".join(f"{a.target}: {a.error}" for a in failures))


@mcp.tool(annotations=_annotations(read_only=True, title="识图与 OCR"))
async def describe_image(
    image: str,
    prompt: str = "提取图中的全部文字内容并进行详细结构化分析",
    preset: VisionPreset = "",
    model: str = "",
    ctx: Optional[Context] = None,
) -> Annotated[mcp_types.CallToolResult, VisionResult]:
    """看图、OCR 提取文字、解析文档表格、定位界面元素。

    参数说明:
        image: 公网 URL 或 Base64 / data URI。
        prompt: 分析要求。
        preset: ""(跟随后台，默认 standard) | "fast"(本地 OCR，只要纯文字时最快最省) |
                "standard"(通用理解) | "document"(文档/表格转 Markdown) |
                "gui"(界面元素定位，structuredContent.elements 返回 0~1000 归一化坐标与中心点)。
        model: 可选，指定模型，格式 "provider/model"、模型 ID 或 provider 名。
    """
    client: httpx.AsyncClient = getattr(mcp, "_http_client", None)
    if not client:
        raise RuntimeError("HTTP 客户端未启动")
    image_b64 = await resolve_image_to_base64(client, image)
    result = await _describe(client, image_b64, prompt, preset, model, ctx)
    text = result.text
    if result.preset == "gui" and result.elements is None:
        text += "\n\n（注意：未能把输出解析为结构化元素列表，elements 为空）"
    return _tool_result(text, result)
