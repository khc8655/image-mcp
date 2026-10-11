"""
共享核心：MCP 实例、常量与场景模板、图像输入解析、结构化输出模型与通用工具函数。
"""
import re
import base64
import time
import logging
from typing import Literal, Optional

import httpx
from pydantic import BaseModel, Field
from mcp import types as mcp_types

from config_manager import config_mgr
import security
import storage

try:
    from mcp.server.mcpserver import MCPServer as FastMCP, Image, Context  # noqa: F401
except ImportError:  # mcp 1.x
    from mcp.server.fastmcp import FastMCP, Image, Context  # noqa: F401

SERVER_START_TIME = time.time()

# ---------------------------------------------------------------------------
# 日志配置
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("image-mcp-suite")

# ---------------------------------------------------------------------------
# 常量、尺寸映射与 GitHub Presets 镜像源
# ---------------------------------------------------------------------------
ASPECT_TO_SIZE = {
    "1:1": "1024x1024",
    "16:9": "1792x1024",
    "9:16": "1024x1792",
    "4:3": "1280x896",
    "3:4": "896x1280",
    "3:2": "1536x1024",
    "2:3": "1024x1536",
    "4:5": "1024x1280",
    "5:4": "1280x1024",
    "21:9": "2048x896",
}

# ---------------------------------------------------------------------------
# 办公/工作专业生图场景模板引擎 (Scene Presets)
# ---------------------------------------------------------------------------
SCENE_PRESETS = {
    "infra_scene": {
        "name": "信息化全景场景图",
        "description": "智慧园区、数据中心机房、政务云平台、工业互联网全景示意图",
        "default_aspect": "16:9",
        "default_resolution": "2K",
        "prompt_prefix": "Professional isometric 2.5D modern technology infographic illustration showing an advanced IT enterprise infrastructure, smart campus digital twin datacenter, sleek blue and cyan glowing network pathways, clean modular server racks, glass-walled command operations center, highly organized, ",
        "prompt_suffix": ", 8k resolution, isometric perspective, clean corporate tech style, subtle cinematic lighting, unreal engine 5 render aesthetics, sharp focus",
        "negative_prompt": "blurry, messy wires, cartoon, low resolution, distorted hardware, cluttered text artifacts, noise, abstract chaos"
    },
    "network_topology": {
        "name": "网络拓扑示意图",
        "description": "企业内网、混合云骨干网、SD-WAN、微隔离防火墙架构拓扑",
        "default_aspect": "16:9",
        "default_resolution": "2K",
        "prompt_prefix": "Clean and modern enterprise computer network topology diagram, flat vector graphic style with isometric technical elements, organized network nodes including core routers, firewalls, cloud gateways, edge servers, neatly connected by structured glowing data fiber lines, clean white background, high contrast, ",
        "prompt_suffix": ", professional network engineering schematic, symmetrical layout, crisp technical design, vector tech icons, minimalist UI aesthetics",
        "negative_prompt": "photo, messy handwriting, illegible scribble, blurry, tangled lines, complex noise, dark background"
    },
    "architecture": {
        "name": "系统软件架构图",
        "description": "分层微服务、分布式架构、中台及数据流转分层示意",
        "default_aspect": "16:9",
        "default_resolution": "2K",
        "prompt_prefix": "Modern software system architecture visual diagram, clean layered modular design, stratified color-coded components representing frontend client, API gateway, microservices cluster, distributed message queue, and database tier, clean directional flow arrows, elegant tech aesthetic, ",
        "prompt_suffix": ", clean white presentation background, structured rectangular layout, enterprise architecture diagram style, ultra crisp details",
        "negative_prompt": "messy, sketch, photorealistic face, random doodles, chaotic connections, dark dingy background"
    },
    "infographic": {
        "name": "科技信息图/汇报海报",
        "description": "技术白皮书插图、数字化转型成果汇报、工整中文与科技视觉排版",
        "default_aspect": "3:4",
        "default_resolution": "2K",
        "prompt_prefix": "High-end corporate IT technology whitepaper infographic poster, balanced visual hierarchy, corporate cyan and deep navy color palette, sleek circular and modular metric charts, digital transformation motifs, neat layout, ",
        "prompt_suffix": ", graphic design award winner, clean typography zones, corporate presentation poster, minimalist flat design with subtle 3D glassmorphism",
        "negative_prompt": "cluttered, ugly, unaligned text, distorted shapes, low contrast, washed out colors"
    },
    "icon_set": {
        "name": "科技图标与组件组",
        "description": "PPT 与交互设计专用科技产品、云计算、安全、硬件扁平图标组",
        "default_aspect": "1:1",
        "default_resolution": "1K",
        "prompt_prefix": "A unified set of 9 modern minimalist IT tech vector icons, grid layout on isolated pure white background, cohesive line art with subtle gradient accents representing cloud computing, cyber security shield, database server, fiber optic network, API integration, microchip, ",
        "prompt_suffix": ", Dribbble award-winning icon set, consistent stroke weight, Figma design system style, perfectly centered icons",
        "negative_prompt": "3D realistic render, photographic noise, shadows, uneven sizing, messy lines"
    }
}

# 内置已知供应商的默认推荐生图模型（本地独立预设加载失败时的开箱即用兜底）
KNOWN_PROVIDER_IMAGE_MODELS = {
    "google": ["gemini-3.1-flash-lite-image", "gemini-3.1-flash-image", "gemini-3-pro-image", "gemini-2.5-flash-image"],
    "minimax": ["image-01", "image-01-live"],
    "sensenova": ["sensenova-u1-fast"],
    "siliconflow": ["Kwai-Kolors/Kolors", "black-forest-labs/FLUX.1-schnell"],
    "agnes": ["agnes-image-2.5-flash"],
    "openai": ["dall-e-3", "gpt-image-1"],
}

# 内置已知供应商的默认推荐识图/多模态视觉模型
KNOWN_PROVIDER_VISION_MODELS = {
    "google": ["gemini-3.5-flash", "gemini-2.5-flash", "gemini-2.5-pro"],
    "minimax": ["MiniMax-M3"],
    "agnes": ["agnes-3.0-flash", "agnes-2.5-flash"],
    "sensenova": ["SenseChat-Vision"],
    "openai": ["gpt-4o", "gpt-4o-mini"],
}

# ---------------------------------------------------------------------------
# 初始化 MCP Server
# ---------------------------------------------------------------------------
mcp = FastMCP("image-mcp-suite")



# ---------------------------------------------------------------------------
# 图像输入解析（SSRF / 本地路径策略见 security.py）
# ---------------------------------------------------------------------------
validate_url_safety = security.validate_url_safety  # 兼容旧引用


def detect_image_mime(b64_str: str) -> str:
    """快速嗅探 Base64 头部 magic bytes 获取准确的 MIME 类型"""
    s = (b64_str or "").strip()[:16]
    if s.startswith("/9j/"):
        return "image/jpeg"
    elif s.startswith("iVBOR"):
        return "image/png"
    elif s.startswith("R0lGOD"):
        return "image/gif"
    elif s.startswith("UklGR"):
        return "image/webp"
    return "image/png"


_B64_RE = re.compile(r"^[A-Za-z0-9+/=\s]+$")


async def resolve_image_to_base64(client: httpx.AsyncClient, image_str: str) -> str:
    """将输入的图片（data URI / Base64 / http(s) URL / 白名单本地路径）统一转换为纯 Base64。"""
    image_str = (image_str or "").strip()
    if not image_str:
        raise ValueError("图片入参为空")

    if image_str.startswith("data:image/"):
        parts = image_str.split(",", 1)
        return parts[1] if len(parts) > 1 else ""

    if image_str.startswith(("http://", "https://")):
        raw = await security.safe_download(client, image_str)
        return base64.b64encode(raw).decode("utf-8")

    # 纯 Base64（足够长且只含 Base64 字符）
    if len(image_str) > 100 and _B64_RE.match(image_str[:4096]):
        return image_str

    # 其余视为本地路径：远程模式默认拒绝，stdio/白名单模式才读取
    raw = security.read_local_image(image_str)
    return base64.b64encode(raw).decode("utf-8")


_rapidocr_engine = None


def get_rapidocr_engine():
    """懒加载并复用 RapidOCR 实例（模型加载耗时数百毫秒，避免每次调用重复初始化）。"""
    global _rapidocr_engine
    if _rapidocr_engine is None:
        from rapidocr_onnxruntime import RapidOCR
        _rapidocr_engine = RapidOCR()
        logger.info("本地 RapidOCR 引擎已加载")
    return _rapidocr_engine


# ---------------------------------------------------------------------------
# 结构化输出模型 (outputSchema / structuredContent)
# ---------------------------------------------------------------------------
class Attempt(BaseModel):
    target: str = Field(description="provider/model")
    error: str = Field(description="脱敏后的失败原因")


class GeneratedImage(BaseModel):
    url: Optional[str] = Field(None, description="签名下载链接（带过期时间）；未配置公网地址时为空，原图以内联图片返回")
    filename: str
    mime: str
    size_bytes: int
    width: Optional[int] = None
    height: Optional[int] = None


class GenerationResult(BaseModel):
    provider: str
    model: str
    images: list[GeneratedImage]
    aspect_ratio: str
    resolution: str
    scene: str
    seed: int
    elapsed_ms: int
    failed_attempts: list[Attempt] = []


class UIElement(BaseModel):
    label: str = ""
    type: str = ""
    box: list[int] = Field(description="[ymin, xmin, ymax, xmax]，0~1000 归一化坐标")
    center: list[int] = Field(description="[y, x]，0~1000 归一化坐标")


class VisionResult(BaseModel):
    text: str
    preset: str
    engine: str = Field(description="rapidocr 或 provider/model")
    elements: Optional[list[UIElement]] = Field(None, description="仅 gui 预设：解析出的可交互元素")
    failed_attempts: list[Attempt] = []


class ImageHit(BaseModel):
    title: str
    image_url: str
    thumbnail_url: Optional[str] = None
    page_url: Optional[str] = None
    width: Optional[int] = None
    height: Optional[int] = None
    source: str


class SearchResult(BaseModel):
    query: str
    mode: str = Field(description="text | reverse_lens (Google Lens 视觉匹配) | reverse_keywords (识图关键词)")
    results: list[ImageHit]


def _tool_result(text: str, structured: BaseModel, extra: list | None = None) -> mcp_types.CallToolResult:
    content = [mcp_types.TextContent(type="text", text=text)] + list(extra or [])
    return mcp_types.CallToolResult(content=content, structuredContent=structured.model_dump(mode="json"))


async def _progress(ctx, done: float, total: float, message: str) -> None:
    if ctx is None:
        return
    try:
        await ctx.report_progress(done, total, message)
    except TypeError:
        try:
            await ctx.report_progress(done, total)
        except Exception:
            pass
    except Exception:
        pass


def _annotations(read_only: bool, open_world: bool = True, idempotent: bool = False, title: str = ""):
    return mcp_types.ToolAnnotations(
        title=title or None,
        readOnlyHint=read_only,
        destructiveHint=False,
        idempotentHint=idempotent,
        openWorldHint=open_world,
    )


def _pick_candidates(models: list[dict], target: str) -> list[dict]:
    """显式指定 model/provider 时提升至第 1 位：优先精确匹配，其次 provider，最后子串。"""
    target = (target or "").strip().lower()
    if not target:
        return list(models)
    if "/" in target:
        p, _, m = target.partition("/")
        exact = [x for x in models if x.get("provider", "").lower() == p and x.get("model", "").lower() == m]
    else:
        exact = [x for x in models if x.get("model", "").lower() == target]
    chosen = (
        exact[:1]
        or [x for x in models if x.get("provider", "").lower() == target][:1]
        or [x for x in models if target in x.get("model", "").lower()][:1]
    )
    if not chosen:
        return list(models)
    return chosen + [x for x in models if x is not chosen[0]]


AspectRatio = Literal["auto", "1:1", "16:9", "9:16", "4:3", "3:4", "3:2", "2:3", "4:5", "5:4", "21:9"]
Resolution = Literal["auto", "1K", "2K", "4K"]
Scene = Literal["auto", "none", "infra_scene", "network_topology", "architecture", "infographic", "icon_set"]
VisionPreset = Literal["", "fast", "standard", "document", "gui"]


def _build_generation_content(raw: list[dict]) -> tuple[list[GeneratedImage], list]:
    """持久化生图结果，返回结构化元数据与面向 Agent 的内容块。

    return_mode=url 且配置了公网地址：签名 URL (resource_link) + 小缩略图，避免数 MB Base64 撑爆上下文；
    否则回退为内联原图。
    """
    mode = config_mgr.get("generation", "return_mode", default="url")
    has_base = bool(storage.public_base_url(config_mgr))
    images: list[GeneratedImage] = []
    blocks: list = []
    for item in raw:
        if "b64" not in item:
            continue
        data = base64.b64decode(item["b64"])
        mime = item.get("mime") or detect_image_mime(item["b64"])
        meta = storage.save_image(data, mime)
        url = storage.signed_url(config_mgr, meta["rel_path"]) if has_base else None
        images.append(GeneratedImage(
            url=url, filename=meta["filename"], mime=mime, size_bytes=meta["size_bytes"],
            width=meta["width"], height=meta["height"],
        ))
        if mode == "url" and url:
            blocks.append(mcp_types.ResourceLink(
                type="resource_link", uri=url, name=meta["filename"], mimeType=mime, size=meta["size_bytes"],
                description="生成图片原图（签名链接，限时有效）",
            ))
            thumb = storage.make_thumbnail(data)
            if thumb:
                blocks.append(mcp_types.ImageContent(type="image", data=base64.b64encode(thumb).decode(), mimeType="image/jpeg"))
        else:
            blocks.append(mcp_types.ImageContent(type="image", data=item["b64"], mimeType=mime))
    return images, blocks
