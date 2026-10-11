"""
本地确定性图像处理（不调用大模型、零额度）：裁剪、旋转、翻转、缩放、放大、去背景、格式转换。

- 坐标与 describe_image(gui) 一致：[ymin, xmin, ymax, xmax]，0~1000 归一化；
- 去背景依赖可选包 rembg（见 requirements-extra.txt），未安装时给出明确提示；
- upscale 为 Lanczos 插值放大，适合尺寸适配，不是 AI 超分。
"""
from __future__ import annotations

import io
from typing import Optional

MAX_OUTPUT_PIXELS = 64_000_000  # 约 8000x8000，防止内存炸裂


def _rembg_remove(im):
    try:
        from rembg import remove
    except ImportError as exc:
        raise RuntimeError("去背景需要安装可选依赖 rembg：pip install -r requirements-extra.txt") from exc
    return remove(im)


def transform(
    raw: bytes,
    crop: Optional[list[int]] = None,
    rotate: int = 0,
    flip: str = "none",
    width: int = 0,
    height: int = 0,
    max_side: int = 0,
    upscale: int = 1,
    remove_background: bool = False,
    fmt: str = "png",
    quality: int = 90,
) -> tuple[bytes, str, list[str], tuple[int, int]]:
    from PIL import Image, ImageOps

    applied: list[str] = []
    with Image.open(io.BytesIO(raw)) as src:
        im = ImageOps.exif_transpose(src)
        im.load()

    if crop:
        if len(crop) != 4:
            raise ValueError("crop 需要 4 个数: [ymin, xmin, ymax, xmax]，0~1000 归一化")
        y1, x1, y2, x2 = [max(0, min(1000, int(v))) for v in crop]
        y1, y2 = sorted((y1, y2))
        x1, x2 = sorted((x1, x2))
        if y2 - y1 < 1 or x2 - x1 < 1:
            raise ValueError("crop 区域为空")
        w, h = im.size
        box = (round(x1 * w / 1000), round(y1 * h / 1000), round(x2 * w / 1000), round(y2 * h / 1000))
        im = im.crop(box)
        applied.append(f"crop{list(box)}")

    if rotate % 360:
        im = im.rotate(-rotate, expand=True)  # 正数为顺时针，更符合直觉
        applied.append(f"rotate({rotate})")

    if flip == "horizontal":
        im = ImageOps.mirror(im)
        applied.append("flip_horizontal")
    elif flip == "vertical":
        im = ImageOps.flip(im)
        applied.append("flip_vertical")

    w, h = im.size
    target = None
    if width or height:
        tw = width or round(w * height / h)
        th = height or round(h * width / w)
        target = (tw, th)
    elif max_side and max(w, h) > max_side:
        scale = max_side / max(w, h)
        target = (round(w * scale), round(h * scale))
    elif upscale and upscale > 1:
        target = (w * upscale, h * upscale)
    if target:
        if target[0] < 1 or target[1] < 1:
            raise ValueError("目标尺寸无效")
        if target[0] * target[1] > MAX_OUTPUT_PIXELS:
            raise ValueError("输出尺寸过大（上限约 6400 万像素）")
        im = im.resize(target, Image.LANCZOS)
        applied.append(f"resize{list(target)}")

    if remove_background:
        im = _rembg_remove(im)
        applied.append("remove_background")
        if fmt == "jpeg":
            fmt = "png"  # 透明背景无法存 JPEG

    fmt = fmt.lower()
    buf = io.BytesIO()
    if fmt == "jpeg":
        im.convert("RGB").save(buf, format="JPEG", quality=quality, optimize=True)
        mime = "image/jpeg"
    elif fmt == "webp":
        im.save(buf, format="WEBP", quality=quality)
        mime = "image/webp"
    else:
        im.save(buf, format="PNG", optimize=True)
        mime = "image/png"
    applied.append(f"encode({fmt})")
    return buf.getvalue(), mime, applied, im.size
