"""本地确定性修图工具（零额度）。"""
import base64
import asyncio
from typing import Annotated, Literal, Optional

import httpx
from pydantic import BaseModel, Field
from mcp import types as mcp_types


from core import (
    mcp, GeneratedImage, resolve_image_to_base64, _tool_result, _annotations, _build_generation_content,
)
import imaging


# ---------------------------------------------------------------------------
# MCP 核心工具 4: 本地确定性修图（零额度）
# ---------------------------------------------------------------------------
class TransformResult(BaseModel):
    image: GeneratedImage
    applied: list[str] = Field(description="实际执行的操作序列")


@mcp.tool(annotations=_annotations(read_only=False, open_world=False, idempotent=True, title="本地修图"))
async def transform_image(
    image: str,
    crop: Optional[list[int]] = None,
    rotate: int = 0,
    flip: Literal["none", "horizontal", "vertical"] = "none",
    width: Annotated[int, Field(ge=0, le=8192)] = 0,
    height: Annotated[int, Field(ge=0, le=8192)] = 0,
    max_side: Annotated[int, Field(ge=0, le=8192)] = 0,
    upscale: Literal[1, 2, 4] = 1,
    remove_background: bool = False,
    format: Literal["png", "jpeg", "webp"] = "png",
    quality: Annotated[int, Field(ge=1, le=100)] = 90,
) -> Annotated[mcp_types.CallToolResult, TransformResult]:
    """本地图像处理，不调用大模型、不消耗额度、结果可复现。按 裁剪→旋转→翻转→缩放→去背景→编码 顺序执行。

    参数说明:
        image: 公网 URL 或 Base64 / data URI。
        crop: [ymin, xmin, ymax, xmax]，0~1000 归一化坐标（可直接用 describe_image gui 预设返回的 box）。
        rotate: 顺时针旋转角度。
        flip: 镜像翻转。
        width / height: 目标尺寸，只给一个则按比例；max_side: 长边上限（仅缩小）。
        upscale: 2 或 4 倍 Lanczos 放大（插值放大，非 AI 超分）。
        remove_background: 抠图去背景（需服务端安装 rembg），输出透明 PNG。
        format / quality: 输出格式与质量。
    """
    client: httpx.AsyncClient = getattr(mcp, "_http_client", None)
    if not client:
        raise RuntimeError("HTTP 客户端未启动")
    raw = base64.b64decode(await resolve_image_to_base64(client, image))
    out, mime, applied, _size = await asyncio.get_running_loop().run_in_executor(
        None,
        lambda: imaging.transform(
            raw, crop=crop, rotate=rotate, flip=flip, width=width, height=height, max_side=max_side,
            upscale=upscale, remove_background=remove_background, fmt=format, quality=quality,
        ),
    )
    images, blocks = _build_generation_content([{"b64": base64.b64encode(out).decode(), "mime": mime}])
    result = TransformResult(image=images[0], applied=applied)
    im = images[0]
    text = f"处理完成（{' → '.join(applied)}），输出 {im.width}x{im.height} {mime}。\n{im.url or im.filename}"
    return _tool_result(text, result, blocks)
