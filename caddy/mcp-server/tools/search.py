"""搜图工具：文字搜图、Google Lens 以图搜图、识图关键词回退。"""
import os
import re
import base64
import asyncio
import urllib.parse
from typing import Annotated, Literal, Optional

import httpx
from pydantic import Field
from mcp import types as mcp_types

from config_manager import config_mgr
import security
import storage
from security import sanitize_error

from core import (
    mcp, logger, Context, ImageHit, SearchResult,
    detect_image_mime, resolve_image_to_base64, _tool_result, _annotations,
)
from cache import search_cache, make_key
from tools.vision import _describe


# ---------------------------------------------------------------------------
# MCP 核心工具 3: 搜图
# ---------------------------------------------------------------------------
async def _search_by_text(client: httpx.AsyncClient, actual_query: str, size: str, aspect_ratio: str, limit: int) -> list[dict]:
    """依次尝试 DuckDuckGo → Bing → Pexels，返回统一结构的结果列表。"""
    results = []

    # 引擎 1: DuckDuckGo Images
    try:
        try:
            from ddgs import DDGS  # duckduckgo_search 已更名为 ddgs
        except ImportError:
            from duckduckgo_search import DDGS
        size_filter = size if size in ["large", "wallpaper"] else None
        layout_filter = aspect_ratio if aspect_ratio in ["square", "wide", "tall"] else None

        loop = asyncio.get_running_loop()
        def _ddg_search():
            with DDGS() as ddgs:
                return list(ddgs.images(
                    actual_query,
                    max_results=limit,
                    size=size_filter,
                    layout=layout_filter
                ))
        raw_results = await loop.run_in_executor(None, _ddg_search)
        for r in raw_results:
            results.append({
                "title": r.get("title", "相关图片"),
                "image_url": r.get("image"),
                "thumbnail_url": r.get("thumbnail"),
                "width": r.get("width"),
                "height": r.get("height"),
                "page_url": r.get("url"),
                "source": "duckduckgo.com"
            })
    except Exception as e:
        logger.warning(f"DuckDuckGo 搜图暂不可用或频控: {e}，自动启用 Bing 搜图...")

    # 引擎 2: Bing 免 Key 高可用图库
    if not results:
        try:
            bing_headers = {
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            }
            bing_url = f"https://www.bing.com/images/search?q={urllib.parse.quote(actual_query)}&form=HDRSC2"
            b_resp = await client.get(bing_url, headers=bing_headers, timeout=12.0)
            if b_resp.status_code == 200:
                raw_matches = re.findall(r'murl&quot;:&quot;(https?://[^&]+)&quot;.*?t1&quot;:&quot;([^&]*)&quot;', b_resp.text)
                for img_url, title in raw_matches[:limit]:
                    clean_title = title.strip() or f"{actual_query} 相关高清图片"
                    results.append({
                        "title": clean_title,
                        "image_url": img_url,
                        "thumbnail_url": img_url,
                        "source": "bing.com"
                    })
                if not results:
                    all_murls = re.findall(r'murl&quot;:&quot;(https?://[^&]+)&quot;', b_resp.text)
                    for img_url in all_murls[:limit]:
                        results.append({
                            "title": f"{actual_query} 相关图片",
                            "image_url": img_url,
                            "thumbnail_url": img_url,
                            "source": "bing.com"
                        })
        except Exception as e:
            logger.warning(f"Bing 备选搜图异常: {e}")

    # 引擎 3: Pexels 摄影图库
    pexels_key = config_mgr.get("search", "pexels_api_key", default="")
    if not results and pexels_key:
        try:
            p_url = f"https://api.pexels.com/v1/search?query={urllib.parse.quote(actual_query)}&per_page={limit}"
            p_resp = await client.get(p_url, headers={"Authorization": pexels_key}, timeout=15.0)
            if p_resp.status_code == 200:
                p_data = p_resp.json()
                for photo in p_data.get("photos", []):
                    src = photo.get("src", {})
                    results.append({
                        "title": photo.get("alt", "Pexels 摄影图"),
                        "image_url": src.get("original") or src.get("large2x"),
                        "thumbnail_url": src.get("medium"),
                        "width": photo.get("width"),
                        "height": photo.get("height"),
                        "page_url": photo.get("url"),
                        "source": "pexels.com"
                    })
        except Exception as e:
            logger.warning(f"Pexels 搜图失败: {e}")

    return results[:limit]


async def _public_image_url(client: httpx.AsyncClient, image: str) -> Optional[str]:
    """Lens 需要公网可访问的图片 URL：传入 URL 直接用，否则暂存为签名链接（需配置 PUBLIC_BASE_URL）。"""
    image = image.strip()
    if image.startswith(("http://", "https://")):
        await security.validate_url_safety(image)
        return image
    if not storage.public_base_url(config_mgr):
        return None
    b64 = await resolve_image_to_base64(client, image)
    meta = storage.save_image(base64.b64decode(b64), detect_image_mime(b64))
    return storage.signed_url(config_mgr, meta["rel_path"])


async def _reverse_search_lens(client: httpx.AsyncClient, image_url: str, q: str, limit: int, api_key: str) -> list[ImageHit]:
    params = {"engine": "google_lens", "url": image_url, "api_key": api_key, "type": "visual_matches"}
    if q:
        params["q"] = q
    resp = await client.get("https://serpapi.com/search.json", params=params, timeout=40.0)
    if resp.status_code != 200:
        raise RuntimeError(f"SerpAPI {resp.status_code}: {resp.text[:200]}")
    data = resp.json()
    if data.get("error"):
        raise RuntimeError(f"SerpAPI: {data['error']}")
    hits = []
    for m in data.get("visual_matches", [])[:limit]:
        img = m.get("image") or m.get("thumbnail")
        if not img:
            continue
        hits.append(ImageHit(
            title=str(m.get("title", ""))[:200], image_url=img, thumbnail_url=m.get("thumbnail"),
            page_url=m.get("link"), width=m.get("image_width"), height=m.get("image_height"),
            source=m.get("source", "google_lens"),
        ))
    return hits


@mcp.tool(annotations=_annotations(read_only=True, title="搜图"))
async def search_images(
    query: str = "",
    image: str = "",
    size: Literal["all", "large", "wallpaper"] = "all",
    aspect_ratio: Literal["all", "square", "wide", "tall"] = "all",
    limit: Annotated[int, Field(ge=1, le=20)] = 6,
    ctx: Optional[Context] = None,
) -> Annotated[mcp_types.CallToolResult, SearchResult]:
    """全网搜图。传 query 按文字搜；传 image 时做以图搜图：
    配置了 SerpAPI Key 则用 Google Lens 找视觉相似/同款/出处（query 可作为补充限定词），
    否则先识别图片关键词再按关键词搜相似主题图片。

    参数说明:
        query: 搜索关键词。
        image: 参考图（公网 URL 或 Base64）。
        size / aspect_ratio: 尺寸与版式过滤（DuckDuckGo 引擎生效）。
        limit: 返回条数 1~20。
    """
    client: httpx.AsyncClient = getattr(mcp, "_http_client", None)
    if not client:
        raise RuntimeError("HTTP 客户端未启动")

    actual_query = query.strip()
    mode = "text"
    serpapi_key = config_mgr.get("search", "serpapi_key", default="") or os.environ.get("SERPAPI_KEY", "")
    if image and serpapi_key:
        try:
            image_url = await _public_image_url(client, image)
            if image_url:
                hits = await _reverse_search_lens(client, image_url, actual_query, limit, serpapi_key)
                result = SearchResult(query=actual_query, mode="reverse_lens", results=hits)
                if hits:
                    lines = [f"以图搜图结果（Google Lens，{len(hits)} 条）:"]
                    for n, h in enumerate(hits, 1):
                        lines.append(f"{n}. {h.title} | {h.source}\n   {h.page_url or h.image_url}")
                    return _tool_result("\n".join(lines), result)
        except Exception as e:
            logger.warning(f"Google Lens 以图搜图失败，回退关键词模式: {sanitize_error(e)}")
    if image and not actual_query:
        mode = "reverse_keywords"
        image_b64 = await resolve_image_to_base64(client, image)
        extract_prompt = "请用 3 到 6 个最精准的核心实体与视觉关键词概括这张图片的主体（如品牌型号、物体名称、艺术风格），直接输出关键词，用空格分隔，不要任何额外解释。"
        vr = await _describe(client, image_b64, extract_prompt, "standard", "", ctx)
        actual_query = vr.text.replace("\n", " ").replace("#", "").strip()[:120]
        logger.info(f"以图搜图自动生成检索词: {actual_query}")

    if not actual_query:
        raise ValueError("请提供搜索关键词 (query) 或参考图片 (image)")

    s_key = make_key("search", actual_query, size, aspect_ratio, limit)
    raw = search_cache.get(s_key)
    if raw is None:
        raw = await _search_by_text(client, actual_query, size, aspect_ratio, limit)
        if raw:
            search_cache.set(s_key, raw, float(config_mgr.get("search", "cache_ttl_seconds", default=600) or 0))
    hits = [
        ImageHit(
            title=str(r.get("title") or actual_query)[:200],
            image_url=r["image_url"],
            thumbnail_url=r.get("thumbnail_url"),
            page_url=r.get("page_url"),
            width=r.get("width") if isinstance(r.get("width"), int) else None,
            height=r.get("height") if isinstance(r.get("height"), int) else None,
            source=r.get("source", ""),
        )
        for r in raw if r.get("image_url")
    ]
    result = SearchResult(query=actual_query, mode=mode, results=hits)
    if not hits:
        return _tool_result(f"未检索到与 '{actual_query}' 相关的图片，请尝试更换关键词。", result)

    lines = [f"搜图结果（检索词: '{actual_query}'，{len(hits)} 张）:"]
    for n, h in enumerate(hits, 1):
        dim = f"{h.width}x{h.height}" if h.width and h.height else "尺寸未知"
        lines.append(f"{n}. {h.title} | {dim} | {h.source}\n   {h.image_url}")
    return _tool_result("\n".join(lines), result)
