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

import httpx
import uvicorn
from starlette.applications import Starlette
from starlette.routing import Route, Mount
from starlette.responses import JSONResponse, Response, HTMLResponse
from starlette.staticfiles import StaticFiles

SERVER_START_TIME = time.time()


try:
    from mcp.server.mcpserver import MCPServer as FastMCP, Image
except ImportError:
    from mcp.server.fastmcp import FastMCP, Image

from config_manager import config_mgr, DEFAULT_IMAGE_ADAPTER_RULES

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
# SSRF 安全校验与图像输入解析
# ---------------------------------------------------------------------------
async def validate_url_safety(url_str: str):
    """异步 DNS 解析并校验 IP 安全性，防范 DNS Rebinding 与内网探测攻击。"""
    parsed = urllib.parse.urlparse(url_str)
    hostname = parsed.hostname
    if not hostname:
        raise ValueError("无效的图片 URL: 缺少 hostname")

    hostname = hostname.lower()
    if hostname in ("localhost", "::1", "0.0.0.0"):
        raise ValueError("SSRF 防御拦截: 不允许访问本地回环地址")

    try:
        ip = ipaddress.ip_address(hostname)
        if ip.is_private or ip.is_link_local or ip.is_loopback or ip.is_reserved:
            raise ValueError(f"SSRF 防御拦截: 私有/保留 IP 地址 {ip} 禁止访问")
        return
    except ValueError as exc:
        if "SSRF 防御拦截" in str(exc):
            raise

    loop = asyncio.get_running_loop()
    try:
        addr_info = await loop.getaddrinfo(hostname, parsed.port or (443 if parsed.scheme == "https" else 80))
    except socket.gaierror:
        raise ValueError(f"SSRF 防御拦截: 无法解析域名 {hostname}")

    for family, _, _, _, sockaddr in addr_info:
        ip = ipaddress.ip_address(sockaddr[0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
            raise ValueError(f"SSRF 防御拦截: 域名 {hostname} 解析到的 IP {ip} 属于内网保留地址")


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


GENERATED_IMAGES_DIR = Path(__file__).resolve().parent / "static" / "generated"


def save_generated_image_to_disk(b64_str: str, mime: str = "image/png") -> dict:
    """将生图成功的 Base64 数据持久化至 static/generated/YYYYMMDD 目录并返回元数据"""
    try:
        ext = "png"
        if "jpeg" in mime or "jpg" in mime:
            ext = "jpg"
        elif "webp" in mime:
            ext = "webp"

        date_str = datetime.now().strftime("%Y%m%d")
        target_dir = GENERATED_IMAGES_DIR / date_str
        target_dir.mkdir(parents=True, exist_ok=True)

        img_id = uuid.uuid4().hex[:12]
        filename = f"gen_{int(time.time())}_{img_id}.{ext}"
        filepath = target_dir / filename

        img_bytes = base64.b64decode(b64_str)
        filepath.write_bytes(img_bytes)

        rel_url = f"/static/generated/{date_str}/{filename}"
        return {
            "saved_path": str(filepath),
            "relative_url": rel_url,
            "filename": filename,
            "size_bytes": len(img_bytes)
        }
    except Exception as e:
        logger.warning(f"持久化保存生图文件失败: {e}")
        return {}


async def resolve_image_to_base64(client: httpx.AsyncClient, image_str: str) -> str:
    """将输入的图片字符串（Base64 / 本地安全路径 / HTTP URL）统一转换为纯 Base64。"""
    image_str = image_str.strip()
    if not image_str:
        raise ValueError("图片入参为空")

    if image_str.startswith("data:image/"):
        parts = image_str.split(",")
        return parts[1] if len(parts) > 1 else parts[0]

    # 安全检查：防御本地敏感文件外带 (LFI / Path Traversal)
    is_path = "/" in image_str or "\\" in image_str
    if is_path and not image_str.startswith("http://") and not image_str.startswith("https://"):
        norm_path = os.path.normpath(image_str)
        lower_path = norm_path.lower()
        forbidden_keywords = ["config.json", ".env", "/etc/", "/proc/", "/sys/", "/var/", "id_rsa", "pem", "passwd", "shadow"]
        if any(fk in lower_path for fk in forbidden_keywords) or ".." in norm_path:
            raise ValueError(f"安全策略拦截: 禁止读取敏感文件或跨目录访问 ({image_str})")

        valid_img_exts = (".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp")
        if lower_path.endswith(valid_img_exts) and os.path.exists(norm_path) and os.path.isfile(norm_path):
            if os.path.getsize(norm_path) > 20 * 1024 * 1024:
                raise ValueError("图片文件超过 20MB 上限")
            with open(norm_path, "rb") as f:
                return base64.b64encode(f.read()).decode("utf-8")

    if (
        (not image_str.startswith("http://") and not image_str.startswith("https://") and "/" not in image_str and "\\" not in image_str and len(image_str) > 100) or
        (len(image_str) > 1000 and not image_str.startswith("http://") and not image_str.startswith("https://"))
    ):
        return image_str

    if image_str.startswith("http://") or image_str.startswith("https://"):
        await validate_url_safety(image_str)
        async with client.stream("GET", image_str, timeout=30.0) as resp:
            if resp.status_code != 200:
                raise RuntimeError(f"下载网络图片失败: {image_str}, status={resp.status_code}")
            content_chunks = []
            total_size = 0
            async for chunk in resp.aiter_bytes():
                total_size += len(chunk)
                if total_size > 20 * 1024 * 1024:
                    raise ValueError("图片大小超过 20MB 上限")
                content_chunks.append(chunk)
            raw_bytes = b"".join(content_chunks)
            return base64.b64encode(raw_bytes).decode("utf-8")

    return image_str


_rapidocr_engine = None


def get_rapidocr_engine():
    """懒加载并复用 RapidOCR 实例（模型加载耗时数百毫秒，避免每次调用重复初始化）。"""
    global _rapidocr_engine
    if _rapidocr_engine is None:
        from rapidocr_onnxruntime import RapidOCR
        _rapidocr_engine = RapidOCR()
        logger.info("本地 RapidOCR 引擎已加载")
    return _rapidocr_engine


def to_mcp_images(raw_images: list[dict]) -> list[Image]:
    return [
        Image(data=base64.b64decode(img["b64"]), format=img.get("mime", "image/png").split("/")[-1])
        for img in raw_images if "b64" in img
    ]


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
@mcp.tool()
async def generate_image(
    prompt: str,
    aspect_ratio: str = "auto",
    action: str = "generate",
    image: str = "",
    mask: str = "",
    scene: str = "auto",
    resolution: str = "auto",
    negative_prompt: str = "",
    seed: int = -1,
    count: int = 1,
    model: str = "",
    provider: str = "",
    style: str = "auto",
    mode: str = "sequential"
) -> list:
    """文生图与图生图综合工具。默认采用后台配置的物理顺序优先级调度与自动故障转移。

    参数说明:
        prompt: 画面意图描述词。
        aspect_ratio: 比例，支持 "auto"(默认，跟随场景模板/后台配置，均未配置时为 1:1) 及 10 种比例："1:1", "16:9", "9:16", "4:3", "3:4", "3:2", "2:3", "4:5", "5:4", "21:9"。显式传入的比例不会被覆盖。
        action: 操作类型，"generate"(文生图) 或 "edit"(图生图)。默认 "generate"。
        image: 参考图（仅在 action="edit" 时需要），支持公网 URL、本地文件绝对路径或 Base64。
        mask: 可选，局部重绘蒙版（仅 action="edit" 时生效），格式同 image。
        scene: 办公/工作专业场景预设模板。可选值：
               - "auto": 默认跟随后台配置，若未配置则保持原始输入；
               - "infra_scene": 信息化全景场景图（数据中心/智慧园区/政务云/工业互联网 2.5D 高清示意图）；
               - "network_topology": 网络拓扑示意图（核心路由/防火墙/混合云整洁连线效果图）；
               - "architecture": 系统软件架构图（分层微服务/API网关/数据流清晰色块图）；
               - "infographic": 科技信息图/汇报海报（白皮书与技术汇报高质感排版图）；
               - "icon_set": 科技图标与组件组（极简纯白底扁平科技图标套件）；
               - "none": 不套用任何模板，使用纯净提示词。
        resolution: 画质分辨率，可选 "auto"(默认), "1K", "2K", "4K"。
        negative_prompt: 负向提示词（不想在画面中出现的元素或瑕疵）。
        seed: 随机种子 (整数，>= 0 时锁定随机性以便复现风格；-1 为完全随机)。
        count: 生成张数 (默认为 1)。
        model: 可选，显式指定具体模型 ID（留空则默认按后台配置顺序调度第 1 个主力模型）。
        provider: 可选，显式指定模型提供商（留空则默认按后台配置顺序调度）。
        style: 兼容参数，风格偏好。
        mode: 兼容参数，默认 "sequential"(顺序优先级模式)。
    """
    client: httpx.AsyncClient = getattr(mcp, "_http_client", None)
    if not client:
        raise RuntimeError("HTTP 客户端未启动")

    bound_models = [
        m for m in config_mgr.get("generation", "models", default=[])
        if isinstance(m, dict) and m.get("enabled", True)
    ]
    if not bound_models:
        raise RuntimeError("当前服务端尚未绑定任何可用生图模型，请在 Web 管理端 (/admin) 挂载生图模型")

    # 1. 办公场景预设模板解析与提示词拼装
    active_scene = scene.strip()
    if active_scene == "auto":
        active_scene = config_mgr.get("generation", "default_scene", default="none")

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

    # 全局默认参数回退
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
    if action == "edit" and image:
        image_b64 = await resolve_image_to_base64(client, image)
        if mask:
            mask_b64 = await resolve_image_to_base64(client, mask)

    # 2. 候选排序：若调用方显式指定了 model 或 provider，则将匹配模型提升至第 1 位
    target = (model or provider).strip().lower()
    if target:
        chosen = None
        remaining = []
        for m in bound_models:
            m_id = m.get("model", "").lower()
            p_id = m.get("provider", "").lower()
            if not chosen and (target == m_id or target in m_id or target == p_id):
                chosen = m
            else:
                remaining.append(m)
        candidates = [chosen] + remaining if chosen else bound_models
    else:
        candidates = list(bound_models)

    timeout_sec = float(config_mgr.get("generation", "timeout_seconds", default=45))
    failures = {}

    # 3. 纯粹的顺序优先级调度 + 链式故障转移
    for cand in candidates:
        rules = config_mgr.get_image_adapter_rules(cand["provider"])
        if action == "edit" and not rules.get("supports_edit", True):
            continue
        if mask_b64 and not rules.get("supports_mask", cand["provider"] == "google"):
            failures[f"{cand['provider']}/{cand['model']}"] = "不支持 mask 局部重绘，已跳过"
            continue
        tag = f"{cand['provider']}/{cand['model']}"
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
                # 自动将生成的图片持久化存储至 static/generated/ 目录
                for img_item in raw:
                    if "b64" in img_item:
                        saved_meta = save_generated_image_to_disk(img_item["b64"], img_item.get("mime", "image/png"))
                        if saved_meta:
                            img_item["saved_path"] = saved_meta.get("saved_path")
                            img_item["relative_url"] = saved_meta.get("relative_url")
                            logger.info(f"生图已持久化: {saved_meta.get('relative_url')} ({saved_meta.get('size_bytes')} 字节)")
                return to_mcp_images(raw)
        except asyncio.TimeoutError:
            failures[tag] = f"请求超时 ({timeout_sec}s)"
            logger.warning(f"生图模型 {tag} 响应超时，自动切换下一优先级备用模型...")
        except Exception as e:
            failures[tag] = str(e)
            logger.warning(f"生图模型 {tag} 执行失败: {e}，自动切换下一优先级备用模型...")

    raise RuntimeError(f"所有可用生图模型均执行失败: {failures}")


@mcp.tool()
async def edit_image(
    image: str,
    prompt: str,
    mask: str = "",
    scene: str = "auto",
    aspect_ratio: str = "auto",
    resolution: str = "auto",
    negative_prompt: str = "",
    seed: int = -1,
    count: int = 1,
    model: str = "",
    provider: str = ""
) -> list:
    """对已有图像进行编辑、重绘或主体参考图生图（快速入口）。

    参数说明:
        image: 待编辑原图，支持公网 URL、本地文件绝对路径或 Base64。
        prompt: 编辑指令。
        mask: 可选，局部重绘蒙版。仅路由到声明支持 mask 的模型（当前默认 Google）。
        count: 生成张数 (默认为 1)。
        其余参数同 generate_image。
    """
    return await generate_image(
        prompt=prompt,
        action="edit",
        image=image,
        mask=mask,
        count=count,
        scene=scene,
        aspect_ratio=aspect_ratio,
        resolution=resolution,
        negative_prompt=negative_prompt,
        seed=seed,
        model=model,
        provider=provider
    )


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# MCP 核心工具 2: 识图与分级 OCR (顺序优先级调度 + 链式故障转移)
# ---------------------------------------------------------------------------
@mcp.tool()
async def describe_image(
    image: str,
    prompt: str = "提取图中的全部文字内容并进行详细结构化分析",
    preset: str = "",
    model: str = "",
    provider: str = ""
) -> str:
    """视觉识别与多级 OCR 分析工具。默认采用后台配置的顺序优先级调度与自动故障转移。

    参数说明:
        image: 目标图片，支持公网 URL、本地文件绝对路径或 Base64。
        prompt: 分析要求或引导词。
        preset: 识别等级，可选 "fast"(本地极速纯文本) | "standard"(默认通用多模态) | "document"(高精文档与表格) | "gui"(界面定位)。
        model: 可选，显式指定识图模型 ID（留空则默认按后台配置顺序调度第 1 个主力模型）。
        provider: 可选，显式指定模型提供商。
    """
    client: httpx.AsyncClient = getattr(mcp, "_http_client", None)
    if not client:
        raise RuntimeError("HTTP 客户端未启动")

    image_b64 = await resolve_image_to_base64(client, image)
    active_preset = preset.strip() or config_mgr.get("vision", "default_preset", default="standard")
    logger.info(f"describe_image: preset={active_preset}, prompt={prompt[:40]}...")

    # 等级 1: fast - 本地极速纯文本 (RapidOCR)
    if active_preset == "fast" and config_mgr.get("vision", "rapidocr_enabled", default=True):
        try:
            ocr_engine = get_rapidocr_engine()
            raw_bytes = base64.b64decode(image_b64)
            # ONNX 推理为同步 CPU 计算，放入线程池避免阻塞事件循环
            result, _ = await asyncio.get_running_loop().run_in_executor(None, ocr_engine, raw_bytes)
            if result:
                lines = [f"- {item[1]} (置信度: {item[2]:.2f})" for item in result]
                return "### 极速 OCR 提取结果 (本地 RapidOCR 引擎):\n\n" + "\n".join(lines)
        except Exception as e:
            logger.warning(f"本地 RapidOCR 执行失败或未安装，平滑降级至云端多模态模型池: {e}")

    system_instruction = ""
    if active_preset == "document":
        system_instruction = "你是一个高精度文档与表格解析专家。请将图片中的所有文字、数据、财务报表提取为标准 GitHub Flavored Markdown (GFM) 表格，严格保留列对齐、数字精度和层次标题结构。"
    elif active_preset == "gui":
        system_instruction = "你是一个界面自动化定位助手。请识别画面中的按钮、输入框、可点击元素，并以 [ymin, xmin, ymax, xmax] (0~1000 归一化坐标) 格式标注出所有元素的位置和中心点。"

    full_prompt = f"{system_instruction}\n\n用户要求: {prompt}".strip()

    vision_models = [
        m for m in config_mgr.get("vision", "models", default=[])
        if isinstance(m, dict) and m.get("enabled", True)
    ]
    if not vision_models:
        raise RuntimeError("当前未绑定任何多模态识图模型，请在 Web 管理端 (/admin) 挂载识图模型")

    # 1. 候选排序：若显式指定了 model 或 provider，优先调度
    target = (model or provider).strip().lower()
    if target:
        chosen = None
        remaining = []
        for m in vision_models:
            m_id = m.get("model", "").lower()
            p_id = m.get("provider", "").lower()
            if not chosen and (target == m_id or target in m_id or target == p_id):
                chosen = m
            else:
                remaining.append(m)
        candidates = [chosen] + remaining if chosen else vision_models
    else:
        # 默认按后台物理顺序（第 1 个为主节点）
        candidates = list(vision_models)

    timeout_sec = float(config_mgr.get("vision", "timeout_seconds", default=45))
    errors = []
    for vm in candidates:
        tag = f"{vm.get('provider')}/{vm.get('model')}"
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
                return res_text
        except asyncio.TimeoutError:
            err_msg = f"{tag}: 请求超时 ({timeout_sec}s)"
            errors.append(err_msg)
            logger.warning(f"识图节点超时故障转移: {err_msg}")
        except Exception as e:
            err_msg = f"{tag}: {e}"
            errors.append(err_msg)
            logger.warning(f"识图节点异常故障转移: {err_msg}")

    raise RuntimeError(f"所有识图与 OCR 模型节点均执行失败: {'; '.join(errors)}")


# ---------------------------------------------------------------------------
# MCP 核心工具 3: 搜图 (文字搜图 + 联动已有 OCR 语义以图搜图)
# ---------------------------------------------------------------------------
@mcp.tool()
async def search_images(
    query: str = "",
    image: str = "",
    size: str = "all",
    aspect_ratio: str = "all",
    limit: int = 6
) -> str:
    """全网高质量图片搜索工具（支持文字搜图与以图搜图）。"""
    client: httpx.AsyncClient = getattr(mcp, "_http_client", None)
    if not client:
        raise RuntimeError("HTTP 客户端未启动")

    actual_query = query.strip()

    if image and not actual_query:
        logger.info("以图搜图模式: 联动内部识图/OCR 引擎抽取特征词...")
        extract_prompt = "请用 3 到 6 个最精准的核心实体与视觉关键词概括这张图片的主体（如品牌型号、物体名称、艺术风格），直接输出关键词，用空格分隔，不要任何额外解释。"
        visual_keywords = await describe_image(image=image, prompt=extract_prompt, preset="standard")
        actual_query = visual_keywords.replace("\n", " ").replace("#", "").strip()
        logger.info(f"以图搜图自动生成检索词: {actual_query}")

    if not actual_query:
        raise ValueError("请提供搜索关键词 (query) 或待搜寻的参考图片 (image)")

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
                        "source": "pexels.com"
                    })
        except Exception as e:
            logger.warning(f"Pexels 搜图失败: {e}")

    if not results:
        return f"未检索到与 '{actual_query}' 相关的图片，请尝试更换关键词。"

    card_items = []
    for idx, item in enumerate(results, 1):
        thumb = item.get("thumbnail_url") or item.get("image_url")
        orig = item.get("image_url")
        title = item.get("title")
        w, h = item.get("width"), item.get("height")
        src = item.get("source")
        dim_str = f"{w}x{h}" if w and h else "高分辨率"
        card_items.append(
            f"{idx}. **[{title}]({orig})**\n"
            f"   [![{title}]({thumb})]({orig})\n"
            f"   - 规格: {dim_str} | 来源: {src}"
        )

    header = f"### 搜图结果 (检索词: '{actual_query}'，共找到 {len(results)} 张高清图片)：\n\n"
    return header + "\n\n".join(card_items)


# ---------------------------------------------------------------------------
# 辅助工具: 提示词增强与能力列表
# ---------------------------------------------------------------------------
@mcp.tool()
async def enhance_prompt(prompt: str, style: str = "realistic", purpose: str = "general") -> str:
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


@mcp.tool()
async def list_models() -> dict:
    """查询当前系统在本地已持久化的供应商、生图模型池、识图模型池、办公场景模板及运行配置。"""
    cfg = config_mgr.get_all()
    provs = cfg.get("providers", {})
    return {
        "service": "KHC Image MCP Suite",
        "version": "3.0",
        "edgeone_vault_url": (cfg.get("edgeone_vault") or {}).get("url", ""),
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
        "generation_models": cfg.get("generation", {}).get("models", []),
        "current_ocr_preset": cfg.get("vision", {}).get("default_preset", "standard"),
        "vision_models": cfg.get("vision", {}).get("models", []),
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
    limit = int(body.get("limit") or 6)
    try:
        res_md = await search_images(query=query, image=image, limit=limit)
        return JSONResponse({"ok": True, "markdown": res_md})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)


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
        if hasattr(mcp, "session_manager"):
            logger.info("启动 MCP Session Manager")
            async with mcp.session_manager.run():
                yield
        else:
            yield
    logger.info("HTTP 连接池已释放 (KHC Image MCP Suite)")


static_dir = Path(__file__).resolve().parent / "static"

if __name__ == "__main__":
    port = int(os.environ.get("MCP_SERVER_PORT", "3002"))

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
        Mount("/mcp", app=unified_mcp_app),
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


    logger.info(f"Image MCP Suite 启动在端口 {port}...")
    uvicorn.run(mcp_normalize_middleware, host="127.0.0.1", port=port, proxy_headers=True, forwarded_allow_ips="*")
