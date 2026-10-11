"""
生成图片的存储、签名访问、缩略图与过期清理。

- 文件存放在 static 目录之外（默认 ./data/generated/YYYYMMDD/），不再被 /static 公开挂载；
- 对外只通过 /files/<YYYYMMDD>/<filename>?exp=..&sig=.. 的签名 URL 访问，过期即失效；
- 后台任务按 generation.retention_days（默认 7 天）删除旧目录。
"""
from __future__ import annotations

import io
import os
import time
import uuid
import base64
import shutil
import asyncio
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from security import sign_path, verify_signed_path

logger = logging.getLogger("image-mcp-suite.storage")

DATA_DIR = Path(os.environ.get("IMAGE_MCP_DATA_DIR", Path(__file__).resolve().parent / "data")).resolve()
GENERATED_DIR = DATA_DIR / "generated"
_EXT_BY_MIME = {"image/jpeg": "jpg", "image/jpg": "jpg", "image/webp": "webp", "image/gif": "gif", "image/png": "png"}


def save_image(raw: bytes, mime: str = "image/png") -> dict:
    ext = _EXT_BY_MIME.get(mime.lower(), "png")
    date_str = datetime.now().strftime("%Y%m%d")
    target_dir = GENERATED_DIR / date_str
    target_dir.mkdir(parents=True, exist_ok=True)
    filename = f"gen_{int(time.time())}_{uuid.uuid4().hex}.{ext}"
    path = target_dir / filename
    path.write_bytes(raw)
    width = height = None
    try:
        from PIL import Image as PILImage
        with PILImage.open(io.BytesIO(raw)) as im:
            width, height = im.size
    except Exception:
        pass
    return {
        "rel_path": f"{date_str}/{filename}",
        "filename": filename,
        "mime": mime,
        "size_bytes": len(raw),
        "width": width,
        "height": height,
    }


def make_thumbnail(raw: bytes, max_side: int = 384, quality: int = 70) -> Optional[bytes]:
    """生成 JPEG 缩略图（几十 KB），供 Agent 预览；Pillow 不可用时返回 None。"""
    try:
        from PIL import Image as PILImage
        with PILImage.open(io.BytesIO(raw)) as im:
            im = im.convert("RGB")
            im.thumbnail((max_side, max_side))
            buf = io.BytesIO()
            im.save(buf, format="JPEG", quality=quality, optimize=True)
            return buf.getvalue()
    except Exception as e:
        logger.debug(f"缩略图生成失败: {e}")
        return None


def public_base_url(config_mgr) -> str:
    return (
        os.environ.get("PUBLIC_BASE_URL", "")
        or config_mgr.get("server", "public_base_url", default="")
        or ""
    ).strip().rstrip("/")


def signed_url(config_mgr, rel_path: str) -> str:
    ttl = int(config_mgr.get("generation", "url_ttl_hours", default=24) or 24) * 3600
    exp, sig = sign_path(config_mgr, rel_path, ttl)
    path = f"/files/{rel_path}?exp={exp}&sig={sig}"
    base = public_base_url(config_mgr)
    return f"{base}{path}" if base else path


def resolve_rel_path(rel_path: str) -> Optional[Path]:
    """将 YYYYMMDD/filename 解析为真实路径，并确保不越出 GENERATED_DIR。"""
    candidate = (GENERATED_DIR / rel_path).resolve()
    if GENERATED_DIR not in candidate.parents or not candidate.is_file():
        return None
    return candidate


async def serve_file(request, config_mgr):
    from starlette.responses import FileResponse, JSONResponse

    rel_path = request.path_params.get("rel_path", "")
    exp = request.query_params.get("exp", "")
    sig = request.query_params.get("sig", "")
    if not verify_signed_path(config_mgr, rel_path, exp, sig):
        return JSONResponse({"error": "链接无效或已过期"}, status_code=403)
    path = resolve_rel_path(rel_path)
    if not path:
        return JSONResponse({"error": "Not Found"}, status_code=404)
    return FileResponse(path, headers={"Cache-Control": "private, max-age=3600"})


def cleanup_expired(retention_days: int) -> int:
    if retention_days <= 0 or not GENERATED_DIR.exists():
        return 0
    cutoff = (datetime.now() - timedelta(days=retention_days)).strftime("%Y%m%d")
    removed = 0
    for d in GENERATED_DIR.iterdir():
        if d.is_dir() and d.name.isdigit() and len(d.name) == 8 and d.name < cutoff:
            shutil.rmtree(d, ignore_errors=True)
            removed += 1
    if removed:
        logger.info(f"已清理 {removed} 个过期生图目录 (保留 {retention_days} 天)")
    return removed


async def cleanup_loop(config_mgr, interval_seconds: int = 3600) -> None:
    while True:
        try:
            cleanup_expired(int(config_mgr.get("generation", "retention_days", default=7) or 0))
        except Exception as e:
            logger.warning(f"清理过期生图失败: {e}")
        await asyncio.sleep(interval_seconds)


def b64_to_bytes(b64_str: str) -> bytes:
    return base64.b64decode(b64_str)
