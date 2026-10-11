#!/usr/bin/env python3
"""
KHC Image MCP Suite Server (StreamableHTTP Mode)
专注多模态图片全生命周期处理的 MCP 服务：
1. 凭据与适配同步：按需从 EdgeOne Vault 中枢拉取提供商与 Key、从 GitHub Presets 拉取适配规则，并持久化存储在本服务端 (config.json)
2. 生图：文生图、图生图（智能自动路由 / 广播全发多图 / 手动指定三大模式，基于本地存储的 adapter_rules 驱动）
3. 识图：Agent 专用 OCR 分级体系（fast 本地极速 / standard / document / gui，支持多节点按序故障切换）
4. 搜图：文字搜图 + 联动内部 OCR 语义以图搜图
5. 管理：严格遵循 /Users/xk/Documents/design-system (KHC Web Design System) 的内嵌 Web 控制台
"""

import os
import sys
import re
import copy
import json
import base64
import time
import socket
import logging
import asyncio
import ipaddress
import urllib.parse
import uuid
from datetime import datetime
from pathlib import Path
from contextlib import asynccontextmanager
from typing import Annotated, Literal, Optional

import httpx
import uvicorn
from starlette.applications import Starlette
from starlette.routing import Route, Mount
from starlette.responses import JSONResponse, Response, HTMLResponse
from starlette.staticfiles import StaticFiles

SERVER_START_TIME = time.time()


try:
    from mcp.server.mcpserver import MCPServer as FastMCP, Image, Context
except ImportError:
    from mcp.server.fastmcp import FastMCP, Image, Context
from mcp import types as mcp_types
from pydantic import BaseModel, Field

from config_manager import config_mgr, DEFAULT_IMAGE_ADAPTER_RULES
import security
import storage
import imaging
from cache import vision_cache, search_cache, make_key
from security import sanitize_error

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

_CDN_CATALOG_URLS = [
    "https://cdn.jsdelivr.net/gh/khc8655/ocrproxy@main/shared/presets/catalog.json",
    "https://raw.githubusercontent.com/khc8655/ocrproxy/main/shared/presets/catalog.json",
]

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

_gcp_token_cache: dict = {"token": None, "expires_at": 0.0}


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


# ---------------------------------------------------------------------------
# 本地凭据解析辅助函数
# ---------------------------------------------------------------------------


def resolve_local_provider_and_key(provider_id: str, key_label: str = "") -> tuple[dict, str, str]:
    """从本服务端已存储的 providers 中查找供应商配置与对应明文 Key（全自动智能选取有效凭据）"""
    providers = config_mgr.get("providers", default={})
    prov = providers.get(provider_id)
    if not prov:
        for k, v in providers.items():
            if k.lower() == (provider_id or "").lower():
                prov = v
                provider_id = k
                break
    if not prov or not isinstance(prov, dict):
        raise ValueError(f"本地未找到供应商 [{provider_id}] 的配置，请先在 Web 管理端从 EdgeOne 中枢拉取")

    keys_dict = prov.get("keys") or {}

    def is_valid_secret(secret: str) -> bool:
        if not secret or not isinstance(secret, str):
            return False
        s = secret.strip()
        if s.endswith("-12345") or s == "mock" or s == "fake":
            return False
        return len(s) > 5

    # 1. 尝试从指定的 key_label 获取有效真实 key
    if key_label and key_label in keys_dict and is_valid_secret(keys_dict[key_label]):
        return prov, key_label, keys_dict[key_label].strip()

    # 2. 自动从 keys_dict 中筛选第一个真实有效的 Key
    for k_lbl, k_sec in keys_dict.items():
        if is_valid_secret(k_sec):
            return prov, k_lbl, k_sec.strip()

    # 3. 若有任何非空 Key 字段则返回首个，否则提示异常
    if keys_dict:
        first_label, first_secret = next(iter(keys_dict.items()))
        return prov, first_label, first_secret.strip()

    raise ValueError(f"供应商 [{provider_id}] 本地尚未配置任何有效 API Key")


# ---------------------------------------------------------------------------
# 基于本地 adapter_rules 驱动的统一生图与图生图执行器
# ---------------------------------------------------------------------------
async def execute_image_generation(
    client: httpx.AsyncClient,
    provider_id: str,
    model: str,
    key_label: str,
    prompt: str,
    aspect_ratio: str = "1:1",
    action: str = "generate",
    image_b64: str = "",
    mask_b64: str = "",
    resolution: str = "auto",
    seed: int = -1,
    negative_prompt: str = "",
    count: int = 1,
    advanced: dict = None
) -> list[dict]:
    """根据本服务端存储的 provider 配置与 adapter_rules.image_generation 规则调用上游生图接口"""
    prov, used_label, api_key = resolve_local_provider_and_key(provider_id, key_label)
    rules = config_mgr.get_image_adapter_rules(provider_id)
    protocol = rules.get("protocol", "openai_images")
    timeout = float(config_mgr.get("generation", "timeout_seconds", default=45))
    clean_model = model.replace("google/", "").replace("models/", "")
    adv = advanced or {}

    logger.info(f"执行生图: provider={provider_id}, key={used_label}, model={clean_model}, protocol={protocol}, action={action}, aspect={aspect_ratio}, res={resolution}")

    # 驱动 1: Google Gemini / Vertex 原生生图协议 (:generateContent)
    if protocol in ("gemini_image", "gemini_generate_content"):
        headers = {"Content-Type": "application/json"}
        # 自动识别 Google AI Studio / Gemini Developer Key (AIza... / AQ...) 与 Vertex AI Key
        if api_key.startswith("AIza") or api_key.startswith("AQ."):
            native_base = rules.get("native_base_url", "https://generativelanguage.googleapis.com/v1beta/models").rstrip("/")
            url = f"{native_base}/{clean_model}:generateContent"
            headers["x-goog-api-key"] = api_key
        else:
            project = (prov.get("adapter_rules") or {}).get("vertex_project") or rules.get("vertex_project", "jjb111")
            location = (prov.get("adapter_rules") or {}).get("vertex_location") or rules.get("vertex_location", "global")
            host = "aiplatform.googleapis.com" if location == "global" else f"{location}-aiplatform.googleapis.com"
            url = f"https://{host}/v1/projects/{project}/locations/{location}/publishers/google/models/{clean_model}:generateContent"
            headers["x-goog-api-key"] = api_key

        gen_cfg = {
            "responseModalities": rules.get("response_modalities", ["TEXT", "IMAGE"]),
            "imageConfig": {"aspectRatio": aspect_ratio}
        }
        res_upper = (resolution or "").strip().upper()
        if res_upper in ("1K", "2K", "4K"):
            gen_cfg["imageConfig"]["imageSize"] = res_upper
        elif adv.get("image_size"):
            gen_cfg["imageConfig"]["imageSize"] = adv["image_size"]

        if seed is not None and seed >= 0:
            gen_cfg["seed"] = seed

        if adv.get("person_generation"):
            gen_cfg["personGeneration"] = adv["person_generation"]

        full_prompt = prompt
        if negative_prompt:
            full_prompt = f"{prompt}\n[Avoid/Negative Prompt: {negative_prompt}]"

        if action == "edit" and image_b64:
            parts = [
                {"inlineData": {"mimeType": detect_image_mime(image_b64), "data": image_b64}},
                {"text": full_prompt}
            ]
            if mask_b64:
                parts.insert(1, {"inlineData": {"mimeType": detect_image_mime(mask_b64), "data": mask_b64}})
            payload = {
                "contents": {"role": "USER", "parts": parts},
                "generationConfig": gen_cfg
            }
        else:
            payload = {
                "contents": {"role": "USER", "parts": [{"text": full_prompt}]},
                "generationConfig": gen_cfg
            }

        resp = await client.post(url, json=payload, headers=headers, timeout=timeout)
        if resp.status_code != 200:
            raise RuntimeError(f"Google ({used_label}) 出图异常 ({resp.status_code}): {resp.text[:300]}")
        resp_json = resp.json()
        images = []
        for candidate in resp_json.get("candidates", []):
            for part in candidate.get("content", {}).get("parts", []):
                inline_data = part.get("inlineData") or part.get("inline_data")
                if inline_data and "data" in inline_data:
                    images.append({
                        "b64": inline_data["data"],
                        "mime": inline_data.get("mimeType", inline_data.get("mime_type", "image/png"))
                    })
        if not images:
            raise RuntimeError(f"Google ({used_label}) 未返回有效图像数据")
        return images

    # 驱动 2: MiniMax 海螺生图协议 (/v1/image_generation)
    if protocol == "minimax_image":
        base_url = (prov.get("base_url") or rules.get("base_url") or "https://api.minimaxi.com/v1").rstrip("/")
        if "api.minimax.com" in base_url:
            base_url = base_url.replace("api.minimax.com", "api.minimaxi.com")
        if not base_url.endswith("/v1"):
            base_url = f"{base_url}/v1"
        endpoint = rules.get("endpoint", "/image_generation")
        url = f"{base_url}{endpoint}"
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}

        payload = {
            "model": clean_model or "image-01",
            "prompt": prompt,
            "response_format": "base64",
            "prompt_optimizer": adv.get("prompt_optimizer", True)
        }
        if "aigc_watermark" in adv:
            payload["aigc_watermark"] = bool(adv["aigc_watermark"])
        if isinstance(rules.get("default_params"), dict):
            payload.update(rules["default_params"])

        payload["aspect_ratio"] = aspect_ratio
        if seed is not None and seed >= 0:
            payload["seed"] = seed
        if negative_prompt:
            payload["negative_prompt"] = negative_prompt

        if action == "edit" and image_b64:
            ref_field = rules.get("edit_reference_field", "subject_reference")
            ref_type = rules.get("edit_reference_type", "character")
            payload[ref_field] = [{"type": ref_type, "image_file": f"data:{detect_image_mime(image_b64)};base64,{image_b64}"}]

        resp = await client.post(url, json=payload, headers=headers, timeout=timeout)
        if resp.status_code != 200:
            raise RuntimeError(f"MiniMax ({used_label}) 出图异常 ({resp.status_code}): {resp.text[:300]}")
        resp_json = resp.json()
        base_resp = resp_json.get("base_resp", {})
        if base_resp.get("status_code", 0) != 0:
            raise RuntimeError(f"MiniMax ({used_label}) 业务错误: {base_resp.get('status_msg')}")
        images = []
        raw_data = resp_json.get("data")
        if isinstance(raw_data, dict):
            for img_data in raw_data.get("image_base64", []):
                images.append({"b64": img_data, "mime": "image/jpeg"})
        elif isinstance(raw_data, list):
            for item in raw_data:
                b64_val = item.get("b64_json") or item.get("image_base64") or item.get("base64")
                if b64_val:
                    images.append({"b64": b64_val, "mime": "image/jpeg"})
        if not images:
            raise RuntimeError(f"MiniMax ({used_label}) 未返回有效图像数据")
        return images

    # 驱动 3: Agnes AI 官方生图协议 (/v1/images/generations)
    if protocol == "agnes_image":
        base_url = (prov.get("base_url") or rules.get("base_url") or "https://apihub.agnes-ai.com/v1").rstrip("/")
        if not base_url.endswith("/v1"):
            base_url = f"{base_url}/v1"
        endpoint = rules.get("endpoint", "/images/generations")
        url = f"{base_url}{endpoint}"
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}

        size = adv.get("size") or rules.get("default_size", "2K")
        res_upper = (resolution or "").strip().upper()
        if res_upper in ("1K", "2K", "4K"):
            size = res_upper
        allowed_ratios = rules.get("allowed_aspect_ratios", ["1:1", "16:9", "9:16", "4:3", "3:4", "3:2", "2:3", "21:9"])
        ratio = aspect_ratio if aspect_ratio in allowed_ratios else "1:1"

        extra_body = {"response_format": "b64_json"}
        neg = negative_prompt or adv.get("negative_prompt")
        if neg:
            extra_body["negative_prompt"] = neg
        if seed is not None and seed >= 0:
            extra_body["seed"] = seed
        if action == "edit" and image_b64:
            extra_body["image"] = [f"data:{detect_image_mime(image_b64)};base64,{image_b64}"]

        payload = {
            "model": clean_model or "agnes-image-2.5-flash",
            "prompt": prompt,
            "size": size,
            "ratio": ratio,
            "extra_body": extra_body
        }
        if action != "edit":
            payload["return_base64"] = True

        resp = await client.post(url, json=payload, headers=headers, timeout=timeout)
        if resp.status_code != 200:
            raise RuntimeError(f"Agnes ({used_label}) 出图异常 ({resp.status_code}): {resp.text[:300]}")
        resp_json = resp.json()
        images = []
        for item in resp_json.get("data", []):
            b64_val = item.get("b64_json")
            if not b64_val and item.get("url"):
                b64_val = await resolve_image_to_base64(client, item["url"])
            if b64_val:
                images.append({"b64": b64_val, "mime": "image/png"})
        if not images:
            raise RuntimeError(f"Agnes ({used_label}) 未返回有效图像数据")
        return images

    # 驱动 4: 通用 OpenAI 兼容生图协议 (SenseNova / SiliconFlow / OpenAI / Zhipu)
    base_url = (rules.get("base_url") or prov.get("base_url") or "").rstrip("/")
    if "api.sensenova.cn" in base_url:
        # 商汤真实生图接口固定在 token.sensenova.cn/v1
        base_url = base_url.replace("api.sensenova.cn", "token.sensenova.cn")
    if not base_url.endswith("/v1"):
        base_url = f"{base_url}/v1"

    endpoint = rules.get("endpoint", "/images/generations")
    url = f"{base_url}{endpoint}"
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}

    size_map = rules.get("size_map") or {}
    use_size = size_map.get(aspect_ratio) or ASPECT_TO_SIZE.get(aspect_ratio, "1024x1024")

    payload = {
        "model": clean_model,
        "prompt": prompt,
        "n": max(1, min(count, 4)),
        "size": use_size,
    }
    neg = negative_prompt or adv.get("negative_prompt")
    if neg:
        payload["negative_prompt"] = neg
    if seed is not None and seed >= 0:
        payload["seed"] = seed
    if rules.get("response_format"):
        payload["response_format"] = rules["response_format"]

    resp = await client.post(url, json=payload, headers=headers, timeout=timeout)
    if resp.status_code != 200:
        raise RuntimeError(f"{provider_id} ({used_label}) 出图异常 ({resp.status_code}): {resp.text[:300]}")
    resp_json = resp.json()
    images = []
    b64_fields = rules.get("response_b64_fields", ["b64_json", "b64_image", "base64"])
    for item in resp_json.get("data", []):
        b64_val = None
        for f_name in b64_fields:
            if item.get(f_name):
                b64_val = item[f_name]
                break
        if not b64_val and item.get("url"):
            b64_val = await resolve_image_to_base64(client, item["url"])
        if b64_val:
            images.append({"b64": b64_val, "mime": "image/png"})
    if not images:
        raise RuntimeError(f"{provider_id} ({used_label}) 未返回有效图像数据")
    return images


# ---------------------------------------------------------------------------
# 基于本地 adapter_rules 驱动的统一多模态识图 / OCR 执行器
# ---------------------------------------------------------------------------
async def execute_vision_completion(
    client: httpx.AsyncClient,
    provider_id: str,
    model: str,
    key_label: str,
    prompt: str,
    image_b64: str = "",
    preset: str = "standard"
) -> str:
    """根据本服务端存储的 provider 配置与 adapter_rules.vision 规则调用上游多模态视觉接口"""
    prov, used_label, api_key = resolve_local_provider_and_key(provider_id, key_label)
    rules = config_mgr.get_vision_adapter_rules(provider_id)
    protocol = rules.get("protocol", "openai_vision")
    timeout = float(config_mgr.get("vision", "timeout_seconds", default=45))
    clean_model = model.replace("google/", "").replace("models/", "")
    base_url = (prov.get("base_url") or rules.get("base_url") or "").rstrip("/")

    logger.info(f"执行识图: provider={provider_id}, key={used_label}, model={clean_model}, protocol={protocol}, preset={preset}")

    # 驱动 1: Google Gemini / Vertex 原生多模态视觉协议 (:generateContent)
    if protocol == "gemini_vision":
        headers = {"Content-Type": "application/json"}
        # 自动识别 Google AI Studio / Gemini Developer Key (AIza... / AQ...) 与 Vertex AI Key
        if api_key.startswith("AIza") or api_key.startswith("AQ."):
            native_base = rules.get("native_base_url", "https://generativelanguage.googleapis.com/v1beta/models").rstrip("/")
            url = f"{native_base}/{clean_model}:generateContent"
            headers["x-goog-api-key"] = api_key
        else:
            project = (prov.get("adapter_rules") or {}).get("vertex_project") or rules.get("vertex_project", "jjb111")
            location = (prov.get("adapter_rules") or {}).get("vertex_location") or rules.get("vertex_location", "global")
            host = "aiplatform.googleapis.com" if location == "global" else f"{location}-aiplatform.googleapis.com"
            url = f"https://{host}/v1/projects/{project}/locations/{location}/publishers/google/models/{clean_model}:generateContent"
            headers["x-goog-api-key"] = api_key

        parts = [{"text": prompt}]
        if image_b64:
            parts.insert(0, {"inlineData": {"mimeType": detect_image_mime(image_b64), "data": image_b64}})
        payload = {"contents": {"role": "USER", "parts": parts}}

        resp = await client.post(url, json=payload, headers=headers, timeout=timeout)
        if resp.status_code == 200:
            candidates = resp.json().get("candidates", [])
            if candidates:
                text_parts = [p.get("text", "") for p in candidates[0].get("content", {}).get("parts", []) if "text" in p]
                return "\n".join(text_parts).strip()
        raise RuntimeError(f"Google Vertex ({used_label}) 识图异常 ({resp.status_code}): {resp.text[:200]}")

    # 驱动 2: MiniMax / Agnes / OpenAI 兼容多模态 Chat Completions 协议
    endpoint = rules.get("endpoint", "/chat/completions")
    if "api.minimax.com" in base_url:
        base_url = base_url.replace("api.minimax.com", "api.minimaxi.com")
    if "api.sensenova.cn" in base_url:
        base_url = base_url.replace("api.sensenova.cn", "token.sensenova.cn")
    if not base_url.endswith("/v1") and not base_url.endswith("/openai"):
        base_url = f"{base_url}/v1"
    chat_url = f"{base_url}{endpoint}" if not base_url.endswith(endpoint) else base_url
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}

    case_map = rules.get("case_sensitive_models") or (prov.get("adapter_rules") or {}).get("case_sensitive_models") or {}
    if clean_model.lower() in case_map:
        clean_model = case_map[clean_model.lower()]

    detail = rules.get("detail", "high")
    if image_b64:
        content = [
            {"type": "text", "text": prompt},
            {"type": "image_url", "image_url": {"url": f"data:{detect_image_mime(image_b64)};base64,{image_b64}", "detail": detail}}
        ]
    else:
        content = prompt

    payload = {
        "model": clean_model,
        "messages": [{"role": "user", "content": content}],
        "stream": False,
        "max_tokens": rules.get("max_tokens", 4096)
    }
    resp = await client.post(chat_url, json=payload, headers=headers, timeout=timeout)
    if resp.status_code != 200:
        raise RuntimeError(f"{provider_id}/{clean_model} ({used_label}) 识图失败 (HTTP {resp.status_code}): {resp.text[:200]}")

    data = resp.json()
    choices = data.get("choices", [])
    if choices:
        return (choices[0].get("message", {}).get("content") or "").strip()
    raise RuntimeError(f"{provider_id}/{clean_model} 未返回有效分析结果")


# ---------------------------------------------------------------------------
# MCP 核心工具 1: 生图与图生图 (顺序优先级调度 + 链式故障转移)
# ---------------------------------------------------------------------------
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
                execute_image_generation(
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
                execute_vision_completion(
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


# ---------------------------------------------------------------------------
# 辅助工具: 提示词增强与能力列表
# ---------------------------------------------------------------------------
@mcp.tool(annotations=_annotations(read_only=True, title="提示词增强"))
async def enhance_prompt(prompt: str, style: str = "realistic", purpose: Literal["general", "work"] = "general") -> str:
    """根据目标风格与用途自动优化扩展绘图提示词。

    参数说明:
        prompt: 用户的简短原始构思。
        style: 画面风格偏好，例如 "realistic", "isometric", "vector", "cyberpunk", "minimalist" 等。
        purpose: 用途场景，"general"(通用艺术创作) 或 "work"(信息化建设/办公拓扑/软件架构专业场景)。
    """
    client: httpx.AsyncClient = getattr(mcp, "_http_client", None)
    if not client:
        raise RuntimeError("HTTP 客户端未启动")

    if purpose == "work" or style in ("work", "tech", "topology", "architecture", "infographic"):
        sys_prompt = (
            "你是一个顶尖的企业级信息化与技术视觉架构设计专家。请将用户的简短构思扩写为适用于专业生图引擎的高精度英文 Prompt。"
            "重点突出：清晰的技术分层结构、扁平或 2.5D 等距视角、工整的高对比度色彩、干净的纯白或科技感背景、整洁的连线与模块规范。"
            f"风格偏好: {style}。\n"
            "直接输出优化后的英文 Prompt，不要包含任何前导词或额外解释。\n\n"
            f"用户构思: {prompt}"
        )
    else:
        sys_prompt = f"你是一个顶级 AI 绘图提示词大师。请将用户的简短构思扩写为适用于 Midjourney/Imagen 的极具美感、细节丰富的绘图 Prompt。风格偏好: {style}。直接输出优化后的英文 Prompt，不要任何解释。\n\n用户构思: {prompt}"

    vision_models = [
        m for m in config_mgr.get("vision", "models", default=[])
        if isinstance(m, dict) and m.get("enabled", True)
    ]
    for vm in vision_models:
        try:
            res = await execute_vision_completion(
                client=client,
                provider_id=vm["provider"],
                model=vm["model"],
                key_label=vm.get("key", ""),
                prompt=sys_prompt
            )
            if res:
                return res
        except Exception as e:
            logger.warning(f"提示词优化节点 {vm.get('provider')}/{vm.get('model')} 失败: {e}")
    return prompt


@mcp.tool(annotations=_annotations(read_only=True, open_world=False, idempotent=True, title="模型与配置查询"))
async def list_models() -> dict:
    """查询当前系统在本地已持久化的供应商、生图模型池、识图模型池、办公场景模板及运行配置。"""
    cfg = config_mgr.get_all()
    provs = cfg.get("providers", {})
    return {
        "service": "KHC Image MCP Suite",
        "version": "3.0",
        "local_providers": [
            {
                "id": pid,
                "name": p.get("name", pid),
                "preset_version": p.get("preset_version", "1.1.0"),
                "keys_count": len(p.get("keys") or {})
            }
            for pid, p in provs.items()
        ],
        "current_generation_mode": cfg.get("generation", {}).get("default_mode", "sequential"),
        "generation_defaults": {
            "aspect_ratio": cfg.get("generation", {}).get("default_aspect_ratio") or cfg.get("generation", {}).get("default_aspect", "1:1"),
            "resolution": cfg.get("generation", {}).get("default_resolution", "auto"),
            "scene": cfg.get("generation", {}).get("default_scene", "none"),
        },
        "available_scenes": [
            {"id": sid, "name": sinfo["name"], "description": sinfo["description"]}
            for sid, sinfo in SCENE_PRESETS.items()
        ],
        "generation_models": [
            {k: m.get(k) for k in ("provider", "model", "role", "enabled")}
            for m in cfg.get("generation", {}).get("models", []) if isinstance(m, dict)
        ],
        "current_ocr_preset": cfg.get("vision", {}).get("default_preset", "standard"),
        "vision_models": [
            {k: m.get(k) for k in ("provider", "model", "role", "enabled")}
            for m in cfg.get("vision", {}).get("models", []) if isinstance(m, dict)
        ],
        "current_search_engine": cfg.get("search", {}).get("default_text_engine", "duckduckgo")
    }


# ---------------------------------------------------------------------------
# Web 管理控制台与 EdgeOne Vault / GitHub Presets 同步 API
# ---------------------------------------------------------------------------
def require_admin_token(request) -> bool:
    auth_header = request.headers.get("Authorization", "")
    token = ""
    if auth_header.startswith("Bearer "):
        token = auth_header[7:].strip()
    if not token or not config_mgr.verify_token(token):
        return False
    return True


def _get_vault_creds(body: dict = None) -> tuple[str, str]:
    body = body or {}
    url = (body.get("edgeone_url") or body.get("url") or config_mgr.get("edgeone_vault", "url", default="")).strip().rstrip("/")
    raw_token = (body.get("token") or "").strip()
    if not raw_token or "*" in raw_token:
        raw_token = config_mgr.get("edgeone_vault", "token", default="").strip()
    return url, raw_token


async def admin_page(request) -> Response:
    template_dir = Path(__file__).resolve().parent / "templates"
    admin_html_path = template_dir / "admin.html"
    if not admin_html_path.exists():
        return HTMLResponse("<h3>KHC Image MCP 控制台模板文件未找到</h3>", status_code=404)

    html_content = admin_html_path.read_text(encoding="utf-8")
    partials_dir = template_dir / "partials"
    if partials_dir.exists():
        for p_file in partials_dir.glob("*.html"):
            placeholder = f"<!-- include:{p_file.stem} -->"
            if placeholder in html_content:
                html_content = html_content.replace(placeholder, p_file.read_text(encoding="utf-8"))

    # 动态插入版本号破坏 EdgeOne CDN 与浏览器旧缓存
    v_tag = f"?v=2.9.0.{int(SERVER_START_TIME)}"
    import re
    html_content = re.sub(r'/static/admin\.css(\?v=[^\s"\'<>]+)?', f'/static/admin.css{v_tag}', html_content)
    html_content = re.sub(r'/static/js/app\.js(\?v=[^\s"\'<>]+)?', f'/static/js/app.js{v_tag}', html_content)

    return HTMLResponse(content=html_content, headers={
        "Cache-Control": "no-cache, no-store, must-revalidate",
        "Pragma": "no-cache",
        "Expires": "0"
    })



async def get_config_api(request) -> Response:
    if not require_admin_token(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    return JSONResponse(config_mgr.get_safe_dict())


async def post_config_api(request) -> Response:
    if not require_admin_token(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    try:
        new_data = await request.json()
        success = config_mgr.update(new_data)
        if success:
            return JSONResponse({
                "status": "success",
                "message": "配置已持久化保存至服务端并立即生效",
                "config": config_mgr.get_safe_dict()
            })
        return JSONResponse({"error": "保存失败"}, status_code=500)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=400)


async def vault_test_api(request) -> Response:
    """测试 EdgeOne 凭据中枢连通性并自动保存中枢 URL/Token"""
    if not require_admin_token(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    try:
        body = await request.json()
    except Exception:
        body = {}

    edgeone_url, token = _get_vault_creds(body)
    if not edgeone_url:
        return JSONResponse({"ok": False, "error": "请填写 EdgeOne 凭据中枢地址 (如 https://api.khc6.cn)"}, status_code=400)

    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    t0 = time.monotonic()
    client: httpx.AsyncClient = getattr(mcp, "_http_client", None)
    try:
        res = await client.get(f"{edgeone_url}/api/vault/manifest", headers=headers, timeout=10.0)
        latency_ms = max(1, int((time.monotonic() - t0) * 1000))
        if res.status_code == 401:
            return JSONResponse({"ok": False, "error": "EdgeOne 中枢鉴权失败 (HTTP 401)，请核对中枢 Token"}, status_code=401)
        if not res.is_success:
            return JSONResponse({"ok": False, "error": f"EdgeOne 中枢返回 HTTP {res.status_code}"}, status_code=res.status_code)

        data = res.json()
        providers = data.get("providers") or {}
        # 验证通过后自动持久化 edgeone_vault 配置
        config_mgr.update({"edgeone_vault": {"url": edgeone_url, "token": token}})
        return JSONResponse({
            "ok": True,
            "latency_ms": latency_ms,
            "edgeone_url": edgeone_url,
            "providers_count": len(providers),
            "provider_ids": sorted(list(providers.keys())),
        })
    except Exception as e:
        return JSONResponse({"ok": False, "error": f"连接 EdgeOne 中枢异常: {str(e)}"}, status_code=502)


async def vault_manifest_api(request) -> Response:
    """从 EdgeOne 中枢获取供应商与 Key 别名清单，并融合 GitHub Presets 适配信息"""
    if not require_admin_token(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    try:
        body = await request.json()
    except Exception:
        body = {}

    edgeone_url, token = _get_vault_creds(body)
    if not edgeone_url:
        return JSONResponse({"ok": False, "error": "尚未配置 EdgeOne 凭据中枢地址"}, status_code=400)

    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    client: httpx.AsyncClient = getattr(mcp, "_http_client", None)
    try:
        res = await client.get(f"{edgeone_url}/api/vault/manifest", headers=headers, timeout=10.0)
        if not res.is_success:
            return JSONResponse({"ok": False, "error": f"拉取 EdgeOne 清单失败 (HTTP {res.status_code})"}, status_code=res.status_code)
        manifest = res.json()
        providers = manifest.get("providers") or {}

        # 为每个供应商补充已知生图模型列表（优先取本地独立 Presets 中的 image_models）
        for pid, pinfo in providers.items():
            if not isinstance(pinfo, dict):
                continue
            p_low = pid.lower()
            preset = config_mgr.get_preset(p_low)
            img_models = []
            if preset and preset.get("image_models"):
                img_models = [
                    (im["name"] if isinstance(im, dict) else im) for im in preset["image_models"]
                ]
            if not img_models and p_low in KNOWN_PROVIDER_IMAGE_MODELS:
                img_models = KNOWN_PROVIDER_IMAGE_MODELS[p_low]
            pinfo["image_models"] = img_models

        return JSONResponse({
            "ok": True,
            "version": manifest.get("version", "1.0.0"),
            "providers": providers,
            "count": len(providers)
        })
    except Exception as e:
        return JSONResponse({"ok": False, "error": f"请求 EdgeOne 清单异常: {str(e)}"}, status_code=502)


async def vault_fetch_key_api(request) -> Response:
    """按需从 EdgeOne 拉取指定供应商与 Key 密钥，自动绑定本地独立 Presets 规则并存储在本服务端"""
    if not require_admin_token(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "无效的 JSON 请求体"}, status_code=400)

    provider = (body.get("provider") or "").strip()
    key_label = (body.get("key_label") or "").strip()
    keys_list = body.get("keys") or ([key_label] if key_label else [])
    if not provider:
        return JSONResponse({"ok": False, "error": "provider 不能为空"}, status_code=400)

    edgeone_url, token = _get_vault_creds(body)
    if not edgeone_url:
        return JSONResponse({"ok": False, "error": "未配置 EdgeOne 凭据中枢地址"}, status_code=400)

    headers = {"Accept": "application/json", "Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    client: httpx.AsyncClient = getattr(mcp, "_http_client", None)
    try:
        payload = {"provider": provider, "credentials_only": True}
        if len(keys_list) == 1:
            payload["key_label"] = keys_list[0]
        elif len(keys_list) > 1:
            payload["keys"] = keys_list

        # 优先请求 EdgeOne 专为外部微服务/MCP 提供的纯凭据接口 /api/vault/credentials，降级回退到 /api/vault/fetch
        res = await client.post(f"{edgeone_url}/api/vault/credentials", headers=headers, json=payload, timeout=10.0)
        if res.status_code == 404:
            res = await client.post(f"{edgeone_url}/api/vault/fetch", headers=headers, json=payload, timeout=10.0)

        if not res.is_success:
            return JSONResponse({"ok": False, "error": f"从 EdgeOne 拉取凭据失败 (HTTP {res.status_code})"}, status_code=res.status_code)

        data = res.json()
        if not data.get("ok"):
            return JSONResponse({"ok": False, "error": data.get("error", "拉取失败")}, status_code=400)

        # 提取纯凭据：base_url 与 keys 字典
        prov_cfg = data.get("provider_config") or {}
        if not prov_cfg:
            prov_cfg = {
                "base_url": data.get("base_url", ""),
                "name": data.get("name") or provider,
                "protocol": data.get("protocol", "openai"),
                "keys": {}
            }
            if isinstance(data.get("keys"), dict):
                for kl, kv in data["keys"].items():
                    prov_cfg["keys"][kl] = kv.get("key") if isinstance(kv, dict) else kv
            elif data.get("key_label") and data.get("key"):
                prov_cfg["keys"][data["key_label"]] = data["key"]

        # 本地存储并自动关联本地 Presets 适配器规则
        saved_prov = config_mgr.upsert_provider_from_vault(provider, prov_cfg)
        return JSONResponse({
            "ok": True,
            "provider": provider,
            "imported_keys": list((prov_cfg.get("keys") or {}).keys()),
            "total_local_keys": len(saved_prov.get("keys") or {}),
            "config": config_mgr.get_safe_dict()
        })
    except Exception as e:
        return JSONResponse({"ok": False, "error": f"拉取并存储凭据失败: {str(e)}"}, status_code=502)


async def vault_sync_api(request) -> Response:
    """一键从 EdgeOne 中枢全量同步所有授权供应商与 Key 凭据，自动对齐本地 config.json 并重载本地独立 Presets 规则"""
    if not require_admin_token(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    try:
        body = await request.json()
    except Exception:
        body = {}

    edgeone_url, token = _get_vault_creds(body)
    if not edgeone_url:
        return JSONResponse({"ok": False, "error": "未配置 EdgeOne 凭据中枢地址"}, status_code=400)

    headers = {"Accept": "application/json", "Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    client: httpx.AsyncClient = getattr(mcp, "_http_client", None)
    try:
        # 1. 重载本地独立 Presets 规则
        reloaded_presets_count = config_mgr.reload_presets()

        # 2. 获取中枢 Manifest
        res = await client.get(f"{edgeone_url}/api/vault/manifest", headers=headers, timeout=10.0)
        if not res.is_success:
            return JSONResponse({"ok": False, "error": f"EdgeOne 返回 HTTP {res.status_code}"}, status_code=res.status_code)
        manifest = res.json()
        remote_providers = manifest.get("providers") or {}

        updated_count = 0
        imported_keys_count = 0

        # 3. 对中枢里拥有的每个提供商，拉取纯凭据并对齐入库
        for pid, r_info in remote_providers.items():
            if not isinstance(r_info, dict):
                continue
            r_keys = r_info.get("keys") or []
            # 发起纯凭据拉取
            cred_payload = {"provider": pid, "credentials_only": True}
            try:
                c_res = await client.post(f"{edgeone_url}/api/vault/credentials", headers=headers, json=cred_payload, timeout=8.0)
                if c_res.status_code == 404:
                    c_res = await client.post(f"{edgeone_url}/api/vault/fetch", headers=headers, json=cred_payload, timeout=8.0)
                
                if c_res.is_success and c_res.json().get("ok"):
                    c_data = c_res.json()
                    prov_cfg = c_data.get("provider_config") or {}
                    if not prov_cfg:
                        prov_cfg = {
                            "base_url": c_data.get("base_url") or r_info.get("base_url", ""),
                            "name": c_data.get("name") or r_info.get("name") or pid,
                            "protocol": c_data.get("protocol") or r_info.get("protocol", "openai"),
                            "keys": {}
                        }
                        if isinstance(c_data.get("keys"), dict):
                            for kl, kv in c_data["keys"].items():
                                prov_cfg["keys"][kl] = kv.get("key") if isinstance(kv, dict) else kv
                        elif c_data.get("key_label") and c_data.get("key"):
                            prov_cfg["keys"][c_data["key_label"]] = c_data["key"]

                    saved_p = config_mgr.upsert_provider_from_vault(pid, prov_cfg)
                    updated_count += 1
                    imported_keys_count += len(prov_cfg.get("keys") or {})
                else:
                    # 至少更新 base_url 与基础字段
                    sync_payload = {
                        "base_url": r_info.get("base_url", ""),
                        "name": r_info.get("name") or pid,
                    }
                    config_mgr.upsert_provider_from_vault(pid, sync_payload)
                    updated_count += 1
            except Exception as pe:
                logger.warning(f"同步供应商 {pid} 凭据异常: {pe}")

        return JSONResponse({
            "ok": True,
            "updated_count": updated_count,
            "imported_keys_count": imported_keys_count,
            "reloaded_presets_count": reloaded_presets_count,
            "message": f"成功从中枢同步 {updated_count} 家厂商凭据 ({imported_keys_count} 个有效 Key)，并热重载 {reloaded_presets_count} 个本地独立适配规则",
            "config": config_mgr.get_safe_dict()
        })
    except Exception as e:
        return JSONResponse({"ok": False, "error": f"同步凭据异常: {str(e)}"}, status_code=502)


async def vault_probe_models_api(request) -> Response:
    """使用指定供应商与 Key 实时探测上游可用模型列表（若 Key 尚未拉取到本地，自动从 EdgeOne 拉取）"""
    if not require_admin_token(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "Invalid JSON"}, status_code=400)

    provider = (body.get("provider") or "").strip()
    key_label = (body.get("key_label") or "").strip()
    if not provider:
        return JSONResponse({"ok": False, "error": "请先选择供应商"}, status_code=400)

    client: httpx.AsyncClient = getattr(mcp, "_http_client", None)

    # 若本地尚无该供应商或该 Key，先自动从 EdgeOne 拉取并落盘
    local_provs = config_mgr.get("providers", default={})
    has_local_key = (
        provider in local_provs and
        isinstance(local_provs[provider].get("keys"), dict) and
        (key_label in local_provs[provider]["keys"] if key_label else bool(local_provs[provider]["keys"]))
    )
    if not has_local_key:
        edgeone_url, token = _get_vault_creds(body)
        if edgeone_url:
            headers = {"Accept": "application/json", "Content-Type": "application/json"}
            if token:
                headers["Authorization"] = f"Bearer {token}"
            fetch_body = {"provider": provider}
            if key_label:
                fetch_body["key_label"] = key_label
            try:
                f_res = await client.post(f"{edgeone_url}/api/vault/fetch", headers=headers, json=fetch_body, timeout=8.0)
                if f_res.is_success:
                    f_data = f_res.json()
                    if f_data.get("ok") and f_data.get("provider_config"):
                        config_mgr.upsert_provider_from_vault(provider, f_data["provider_config"])
            except Exception as e:
                logger.warning(f"探测前自动从 EdgeOne 拉取 Key 失败: {e}")

    try:
        prov, used_label, api_key = resolve_local_provider_and_key(provider, key_label)
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)

    base_url = (prov.get("base_url") or "").rstrip("/")
    is_google = provider.lower() == "google" or "generativelanguage.googleapis.com" in base_url.lower()

    probe_urls = []
    if is_google:
        probe_urls.append(("https://generativelanguage.googleapis.com/v1beta/openai/models", {"Authorization": f"Bearer {api_key}"}))
        probe_urls.append((f"https://generativelanguage.googleapis.com/v1beta/models?key={api_key}", {}))
    else:
        urls = [f"{base_url}/models"] if base_url.endswith("/v1") else [f"{base_url}/v1/models", f"{base_url}/models"]
        for u in urls:
            probe_urls.append((u, {"Authorization": f"Bearer {api_key}"}))

    models = []
    last_err = ""
    for u, hdrs in probe_urls:
        try:
            r = await client.get(u, headers={"Accept": "application/json", **hdrs}, timeout=8.0)
            if not r.is_success:
                last_err = f"HTTP {r.status_code}"
                continue
            d = r.json()
            raw_list = d.get("data") or d.get("models") or (d if isinstance(d, list) else [])
            for item in raw_list:
                m_id = item if isinstance(item, str) else (item.get("id") or item.get("name") or "")
                if m_id:
                    if m_id.startswith("models/"):
                        m_id = m_id[7:]
                    models.append(m_id)
            if models:
                break
        except Exception as e:
            last_err = str(e)

    # 合并独立 Presets 模型与本地生图模型
    preset = config_mgr.get_preset(provider)
    preset_models = [
        (m["name"] if isinstance(m, dict) else m)
        for m in (preset.get("image_models") or []) + (preset.get("vision_models") or [])
    ]
    for m in (prov.get("image_models") or []) + (prov.get("models") or []) + preset_models + KNOWN_PROVIDER_IMAGE_MODELS.get(provider.lower(), []):
        if m and m not in models:
            models.append(m)

    models = sorted(list(dict.fromkeys(models)))
    if not models:
        return JSONResponse({"ok": False, "error": f"未能探测到模型列表 ({last_err})"})

    return JSONResponse({
        "ok": True,
        "provider": provider,
        "used_key_label": used_label,
        "models": models,
        "count": len(models)
    })


async def get_presets_api(request) -> Response:
    """获取本地所有独立适配器预设文件内容与 catalog 清单"""
    if not require_admin_token(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    return JSONResponse(config_mgr.get_all_presets())


async def reload_presets_api(request) -> Response:
    """免重启热重载本地 presets/ 目录下的所有独立适配器预设规则"""
    if not require_admin_token(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    count = config_mgr.reload_presets()
    return JSONResponse({
        "ok": True,
        "message": f"成功热重载 {count} 个独立适配器预设文件",
        "presets_count": count,
        "all_presets": config_mgr.get_all_presets(),
        "config": config_mgr.get_safe_dict()
    })


async def search_test_api(request) -> Response:
    """供控制台在线测试搜图引擎"""
    if not require_admin_token(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    try:
        body = await request.json()
    except Exception:
        body = {}
    query = (body.get("query") or "").strip()
    image = (body.get("image") or "").strip()
    limit = max(1, min(int(body.get("limit") or 6), 20))
    try:
        res = await search_images(query=query, image=image, limit=limit)
        data = res.structuredContent if hasattr(res, "structuredContent") else res.structured_content
        hits = data.get("results", [])
        if not hits:
            md = f"未检索到与 '{data.get('query')}' 相关的图片，请尝试更换关键词。"
        else:
            cards = []
            for idx, h in enumerate(hits, 1):
                thumb = h.get("thumbnail_url") or h["image_url"]
                dim = f"{h['width']}x{h['height']}" if h.get("width") and h.get("height") else "高分辨率"
                cards.append(
                    f"{idx}. **[{h['title']}]({h['image_url']})**\n"
                    f"   [![{h['title']}]({thumb})]({h['image_url']})\n"
                    f"   - 规格: {dim} | 来源: {h['source']}"
                )
            md = f"### 搜图结果 (检索词: '{data.get('query')}'，共找到 {len(hits)} 张图片)：\n\n" + "\n\n".join(cards)
        return JSONResponse({"ok": True, "markdown": md, "data": data})
    except Exception as e:
        return JSONResponse({"ok": False, "error": sanitize_error(e)}, status_code=400)


# ---------------------------------------------------------------------------
# 应用生命周期与单轨 ASGI 架构
# ---------------------------------------------------------------------------
@asynccontextmanager
async def app_lifespan(app):
    limits = httpx.Limits(max_keepalive_connections=50, max_connections=200)
    timeout = httpx.Timeout(connect=10.0, read=120.0, write=30.0, pool=10.0)
    async with httpx.AsyncClient(limits=limits, timeout=timeout, follow_redirects=True) as client:
        app.state.http_client = client
        mcp._http_client = client
        logger.info("HTTP 连接池已启动 (KHC Image MCP Suite)")
        security.get_signing_secret(config_mgr)
        if config_mgr.admin_token_conflicts():
            logger.error("Admin Token 与 MCP 客户端 Token 相同，管理面已禁用。请设置独立的 ADMIN_TOKEN。")
        if not security.load_client_tokens(config_mgr):
            logger.warning("未配置任何 MCP 客户端 Token (CLIENT_AUTH_TOKEN / auth.client_tokens)，/mcp 将拒绝所有请求")
        if not storage.public_base_url(config_mgr):
            logger.warning("未配置 PUBLIC_BASE_URL / server.public_base_url，生图结果将以内联 Base64 返回")
        cleanup_task = asyncio.create_task(storage.cleanup_loop(config_mgr))
        try:
            if hasattr(mcp, "session_manager"):
                logger.info("启动 MCP Session Manager")
                async with mcp.session_manager.run():
                    yield
            else:
                yield
        finally:
            cleanup_task.cancel()
    logger.info("HTTP 连接池已释放 (KHC Image MCP Suite)")


async def files_endpoint(request) -> Response:
    return await storage.serve_file(request, config_mgr)


static_dir = Path(__file__).resolve().parent / "static"

def create_app():
    """构建完整 ASGI 应用（便于测试与多入口复用）。"""

    ts_settings = None
    try:
        from mcp.server.transport_security import TransportSecuritySettings
        ts_settings = TransportSecuritySettings(enable_dns_rebinding_protection=False)
    except Exception:
        pass

    http_app = None
    if hasattr(mcp, "streamable_http_app"):
        try:
            http_app = mcp.streamable_http_app(streamable_http_path="/", transport_security=ts_settings)
        except TypeError:
            http_app = mcp.streamable_http_app()
    elif hasattr(mcp, "http_app"):
        try:
            http_app = mcp.http_app(streamable_http_path="/", transport_security=ts_settings)
        except TypeError:
            http_app = mcp.http_app()

    async def unified_mcp_app(scope, receive, send):
        if http_app:
            http_scope = dict(scope, path="/", raw_path=b"/", root_path="")
            await http_app(http_scope, receive, send)
        else:
            resp = JSONResponse({"error": "No StreamableHTTP MCP transport app available"}, status_code=404)
            await resp(scope, receive, send)

    routes = [
        Route("/admin", endpoint=admin_page, methods=["GET"]),
        Route("/api/config", endpoint=get_config_api, methods=["GET"]),
        Route("/api/config", endpoint=post_config_api, methods=["POST"]),
        Route("/api/presets", endpoint=get_presets_api, methods=["GET"]),
        Route("/api/presets/reload", endpoint=reload_presets_api, methods=["POST"]),
        Route("/api/search/test", endpoint=search_test_api, methods=["POST"]),
        Route("/api/vault/test", endpoint=vault_test_api, methods=["POST"]),
        Route("/api/vault/manifest", endpoint=vault_manifest_api, methods=["POST"]),
        Route("/api/vault/fetch-key", endpoint=vault_fetch_key_api, methods=["POST"]),
        Route("/api/vault/sync", endpoint=vault_sync_api, methods=["POST"]),
        Route("/api/vault/probe-models", endpoint=vault_probe_models_api, methods=["POST"]),
        Route("/files/{rel_path:path}", endpoint=files_endpoint, methods=["GET", "HEAD"]),
        Mount("/mcp", app=security.make_mcp_auth_middleware(unified_mcp_app, config_mgr)),
    ]

    if static_dir.exists():
        routes.append(Mount("/static", app=StaticFiles(directory=str(static_dir)), name="static"))

    app = Starlette(lifespan=app_lifespan, routes=routes)
    app.router.redirect_slashes = False

    async def mcp_normalize_middleware(scope, receive, send):
        if scope["type"] == "http":
            if scope["path"] == "/mcp":
                scope = dict(scope, path="/mcp/", raw_path=b"/mcp/")

            path = scope.get("path", "")
            if path.startswith("/static/generated"):
                # 旧版生图目录不再公开，统一走 /files 签名链接
                await JSONResponse({"error": "Not Found"}, status_code=404)(scope, receive, send)
                return
            if path.startswith("/static/") or path in ("/admin", "/api/config", "/api/presets"):
                async def send_wrapper(message):
                    if message["type"] == "http.response.start":
                        headers = list(message.get("headers", []))
                        headers = [h for h in headers if h[0].lower() not in (b"cache-control", b"pragma", b"expires")]
                        headers.append((b"cache-control", b"no-cache, no-store, must-revalidate"))
                        headers.append((b"pragma", b"no-cache"))
                        headers.append((b"expires", b"0"))
                        message["headers"] = headers
                    await send(message)
                await app(scope, receive, send_wrapper)
                return

        await app(scope, receive, send)


    return mcp_normalize_middleware


if __name__ == "__main__":
    port = int(os.environ.get("MCP_SERVER_PORT", "3002"))
    logger.info(f"Image MCP Suite 启动在端口 {port}...")
    uvicorn.run(create_app(), host="127.0.0.1", port=port, proxy_headers=True, forwarded_allow_ips="127.0.0.1")
