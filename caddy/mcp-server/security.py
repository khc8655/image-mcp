"""
安全基础设施：Token 校验、客户端鉴权与限流、URL 签名、本地路径策略、错误脱敏、SSRF 校验。

设计原则：
- 默认拒绝 (fail closed)：未配置任何客户端 Token 时 /mcp 一律 401；未开启本地路径时拒绝路径入参。
- 常量时间比较：所有 Token / 签名比较使用 hmac.compare_digest。
- 管理面与调用面分离：Admin Token 不得与任何客户端 Token 相同。
"""
from __future__ import annotations

import os
import re
import hmac
import time
import socket
import asyncio
import hashlib
import ipaddress
import logging
import urllib.parse
from collections import deque
from pathlib import Path
from typing import Optional

logger = logging.getLogger("image-mcp-suite.security")


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------
def constant_eq(a: str, b: str) -> bool:
    """常量时间字符串比较，防范时序侧信道。"""
    if not a or not b:
        return False
    return hmac.compare_digest(a.strip().encode("utf-8"), b.strip().encode("utf-8"))


def _env_flag(name: str, default: bool = False) -> bool:
    val = os.environ.get(name)
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")


def extract_bearer(headers) -> str:
    auth = headers.get("authorization") or headers.get("Authorization") or ""
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return ""


# ---------------------------------------------------------------------------
# 客户端 Token（MCP 调用面）与限流
# ---------------------------------------------------------------------------
def load_client_tokens(config_mgr) -> list[dict]:
    """
    汇总所有可调用 /mcp 的客户端 Token：
    - 环境变量 CLIENT_AUTH_TOKEN（兼容旧部署，逗号分隔可配多个，统一命名为 env-N）
    - config.json 中 auth.client_tokens: [{"name": "cursor", "token": "...", "enabled": true, "rpm": 60}]
    """
    tokens: list[dict] = []
    env_raw = os.environ.get("CLIENT_AUTH_TOKEN", "")
    for idx, tk in enumerate([t.strip() for t in env_raw.split(",") if t.strip()], 1):
        tokens.append({"name": f"env-{idx}", "token": tk, "enabled": True})
    for item in config_mgr.get("auth", "client_tokens", default=[]) or []:
        if isinstance(item, dict) and item.get("token") and item.get("enabled", True):
            tokens.append(item)
    return tokens


def authenticate_client(config_mgr, token: str) -> Optional[dict]:
    if not token:
        return None
    matched = None
    for item in load_client_tokens(config_mgr):
        # 遍历全部条目，避免提前返回带来的时序差异
        if constant_eq(token, item["token"]) and matched is None:
            matched = item
    return matched


class SlidingWindowLimiter:
    """进程内滑动窗口限流（单实例部署足够；多实例需换 Redis）。"""

    def __init__(self) -> None:
        self._hits: dict[str, deque] = {}

    def allow(self, key: str, rpm: int) -> tuple[bool, int]:
        if rpm <= 0:
            return True, 0
        now = time.monotonic()
        q = self._hits.setdefault(key, deque())
        while q and now - q[0] > 60:
            q.popleft()
        if len(q) >= rpm:
            retry_after = int(60 - (now - q[0])) + 1
            return False, retry_after
        q.append(now)
        return True, 0


client_limiter = SlidingWindowLimiter()


def make_mcp_auth_middleware(app, config_mgr):
    """包裹 /mcp 子应用的 ASGI 鉴权 + 限流中间件。"""
    from starlette.responses import JSONResponse

    async def middleware(scope, receive, send):
        if scope["type"] != "http" or scope.get("method") == "OPTIONS":
            await app(scope, receive, send)
            return
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
        client = authenticate_client(config_mgr, extract_bearer(headers))
        if not client:
            resp = JSONResponse(
                {"error": "Unauthorized"}, status_code=401,
                headers={"WWW-Authenticate": 'Bearer realm="image-mcp"'},
            )
            await resp(scope, receive, send)
            return
        rpm = int(client.get("rpm") or config_mgr.get("auth", "default_rpm", default=120) or 0)
        if scope.get("method") == "POST":
            ok, retry_after = client_limiter.allow(client["name"], rpm)
            if not ok:
                resp = JSONResponse(
                    {"error": "Too Many Requests", "retry_after": retry_after}, status_code=429,
                    headers={"Retry-After": str(retry_after)},
                )
                await resp(scope, receive, send)
                return
        scope = dict(scope)
        scope.setdefault("state", {})
        scope["state"] = dict(scope["state"], mcp_client=client["name"])
        await app(scope, receive, send)

    return middleware


# ---------------------------------------------------------------------------
# 生成文件的签名 URL
# ---------------------------------------------------------------------------
def get_signing_secret(config_mgr) -> str:
    env_secret = os.environ.get("IMAGE_MCP_SIGNING_SECRET", "").strip()
    if env_secret:
        return env_secret
    secret = config_mgr.get("auth", "signing_secret", default="")
    if not secret:
        secret = os.urandom(32).hex()
        config_mgr.set_value(("auth", "signing_secret"), secret)
        logger.info("已生成文件签名密钥并写入 config.json (auth.signing_secret)")
    return secret


def sign_path(config_mgr, rel_path: str, ttl_seconds: int) -> tuple[int, str]:
    exp = int(time.time()) + max(60, int(ttl_seconds))
    msg = f"{rel_path}:{exp}".encode("utf-8")
    sig = hmac.new(get_signing_secret(config_mgr).encode("utf-8"), msg, hashlib.sha256).hexdigest()[:32]
    return exp, sig


def verify_signed_path(config_mgr, rel_path: str, exp: str, sig: str) -> bool:
    try:
        exp_i = int(exp)
    except (TypeError, ValueError):
        return False
    if exp_i < time.time():
        return False
    msg = f"{rel_path}:{exp_i}".encode("utf-8")
    expected = hmac.new(get_signing_secret(config_mgr).encode("utf-8"), msg, hashlib.sha256).hexdigest()[:32]
    return constant_eq(expected, sig or "")


# ---------------------------------------------------------------------------
# 本地路径策略（替代旧的关键词黑名单）
# ---------------------------------------------------------------------------
VALID_IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp")
MAX_IMAGE_BYTES = 20 * 1024 * 1024


def local_paths_allowed() -> bool:
    """远程部署默认关闭；stdio 本地模式下由入口自动开启。"""
    return _env_flag("IMAGE_MCP_ALLOW_LOCAL_PATHS", default=False)


def allowed_local_roots() -> list[Path]:
    raw = os.environ.get("IMAGE_MCP_LOCAL_ROOTS", "")
    roots = [Path(p).expanduser().resolve() for p in raw.split(os.pathsep) if p.strip()]
    if not roots and _env_flag("IMAGE_MCP_STDIO", default=False):
        roots = [Path.home().resolve()]
    return roots


def read_local_image(path_str: str) -> bytes:
    """仅在显式开启时读取白名单根目录下的图片文件。"""
    if not local_paths_allowed():
        raise ValueError(
            "本服务为远程部署，不接受本地文件路径（路径指向的是调用方机器而非服务器）。"
            "请改传公网 URL 或 Base64 / data URI。"
        )
    roots = allowed_local_roots()
    if not roots:
        raise ValueError("已开启本地路径但未配置 IMAGE_MCP_LOCAL_ROOTS 白名单目录")
    real = Path(path_str).expanduser().resolve()
    if not any(real == r or r in real.parents for r in roots):
        raise ValueError("安全策略拦截: 路径不在允许的目录白名单内")
    if real.suffix.lower() not in VALID_IMAGE_EXTS:
        raise ValueError(f"仅支持图片文件: {', '.join(VALID_IMAGE_EXTS)}")
    if not real.is_file():
        raise ValueError(f"文件不存在: {path_str}")
    if real.stat().st_size > MAX_IMAGE_BYTES:
        raise ValueError("图片文件超过 20MB 上限")
    return real.read_bytes()


# ---------------------------------------------------------------------------
# SSRF 校验
# ---------------------------------------------------------------------------
def _ip_is_public(ip: ipaddress._BaseAddress) -> bool:
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return ip.is_global and not ip.is_multicast


async def validate_url_safety(url_str: str) -> None:
    """解析域名并要求全部解析结果均为公网地址；仅允许 http/https。"""
    parsed = urllib.parse.urlparse(url_str)
    if parsed.scheme not in ("http", "https"):
        raise ValueError("SSRF 防御拦截: 仅允许 http/https 协议")
    hostname = (parsed.hostname or "").lower()
    if not hostname:
        raise ValueError("无效的图片 URL: 缺少 hostname")
    try:
        ip = ipaddress.ip_address(hostname)
        if not _ip_is_public(ip):
            raise ValueError(f"SSRF 防御拦截: 非公网 IP 地址 {ip} 禁止访问")
        return
    except ValueError as exc:
        if "SSRF" in str(exc):
            raise
    loop = asyncio.get_running_loop()
    try:
        addr_info = await loop.getaddrinfo(hostname, parsed.port or (443 if parsed.scheme == "https" else 80))
    except socket.gaierror:
        raise ValueError(f"SSRF 防御拦截: 无法解析域名 {hostname}")
    for _family, _, _, _, sockaddr in addr_info:
        ip = ipaddress.ip_address(sockaddr[0])
        if not _ip_is_public(ip):
            raise ValueError(f"SSRF 防御拦截: 域名 {hostname} 解析到非公网地址 {ip}")


async def safe_download(client, url: str, max_bytes: int = MAX_IMAGE_BYTES, max_redirects: int = 5) -> bytes:
    """逐跳校验重定向目标的安全下载，防止 302 跳转到内网绕过 SSRF 校验。"""
    current = url
    for _ in range(max_redirects + 1):
        await validate_url_safety(current)
        async with client.stream("GET", current, timeout=30.0, follow_redirects=False) as resp:
            if resp.status_code in (301, 302, 303, 307, 308):
                loc = resp.headers.get("location")
                if not loc:
                    raise RuntimeError("下载失败: 重定向缺少 Location")
                current = urllib.parse.urljoin(current, loc)
                continue
            if resp.status_code != 200:
                raise RuntimeError(f"下载网络图片失败: status={resp.status_code}")
            declared = resp.headers.get("content-length")
            if declared and declared.isdigit() and int(declared) > max_bytes:
                raise ValueError("图片大小超过 20MB 上限")
            chunks, total = [], 0
            async for chunk in resp.aiter_bytes():
                total += len(chunk)
                if total > max_bytes:
                    raise ValueError("图片大小超过 20MB 上限")
                chunks.append(chunk)
            return b"".join(chunks)
    raise RuntimeError("下载失败: 重定向次数过多")


# ---------------------------------------------------------------------------
# 错误信息脱敏
# ---------------------------------------------------------------------------
_REDACT_PATTERNS = [
    (re.compile(r"sk-[A-Za-z0-9_\-]{8,}"), "sk-***"),
    (re.compile(r"AIza[0-9A-Za-z_\-]{20,}"), "AIza***"),
    (re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._\-]{8,}"), r"\1***"),
    (re.compile(r"(?i)((?:api[_-]?key|key|token|secret|signature|sig|access_token)=)[^&\s\"']+"), r"\1***"),
    (re.compile(r"(?i)(\"(?:api[_-]?key|key|token|secret|authorization)\"\s*:\s*\")[^\"]+"), r"\1***"),
    (re.compile(r"eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{5,}"), "jwt***"),
]


def sanitize_error(msg, limit: int = 300) -> str:
    """去除上游错误中的 Key / Token / 签名等敏感片段，并截断长度。"""
    text = str(msg or "")
    for pat, repl in _REDACT_PATTERNS:
        text = pat.sub(repl, text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit] + ("…" if len(text) > limit else "")
