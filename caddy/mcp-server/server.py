#!/usr/bin/env python3
"""
KHC Image MCP Suite Server (StreamableHTTP Mode)
专注多模态图片全生命周期处理的 MCP 服务：
1. 凭据与适配同步：按需从 EdgeOne Vault 中枢拉取提供商与 Key、从 GitHub Presets 拉取适配规则，并持久化存储在本服务端 (config.json)
2. 生图：文生图、图生图（智能自动路由 / 广播全发多图 / 手动指定三大模式，基于本地存储的 adapter_rules 驱动）
3. 识图：Agent 专用 OCR 分级体系（fast 本地极速 / standard / document / gui，支持多节点按序故障切换）
4. 搜图：文字搜图 + 联动内部 OCR 语义以图搜图
5. 管理：内嵌 Web 控制台 (/admin)

模块划分：core(共享) / providers(上游适配) / tools/*(MCP 工具) / admin(控制台 API) /
security(鉴权与防护) / storage(生图存储) / imaging(本地修图) / cache(缓存)。

运行方式：
  python server.py            # Streamable HTTP（远程部署，默认端口 3002）
  python server.py --stdio    # stdio（本地 Agent 直接拉起，允许读取本地图片路径）
"""

import os
import sys
import asyncio
from pathlib import Path
from contextlib import asynccontextmanager

import httpx
import uvicorn
from starlette.applications import Starlette
from starlette.routing import Route, Mount
from starlette.responses import JSONResponse, Response
from starlette.staticfiles import StaticFiles

from config_manager import config_mgr
import security
import storage
from core import mcp, logger
import tools  # noqa: F401  注册全部 MCP 工具
from admin import (
    admin_page, get_config_api, post_config_api, get_presets_api, reload_presets_api, search_test_api,
    vault_test_api, vault_manifest_api, vault_fetch_key_api, vault_sync_api, vault_probe_models_api,
)

# 兼容旧的 `import server; server.generate_image(...)` 用法
from tools.generation import generate_image, edit_image  # noqa: E402,F401
from tools.vision import describe_image  # noqa: E402,F401
from tools.search import search_images  # noqa: E402,F401
from tools.transform import transform_image  # noqa: E402,F401
from tools.misc import enhance_prompt, list_models  # noqa: E402,F401


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


def run_stdio() -> None:
    """stdio 模式：由本地 Agent 直接拉起，调用方与服务同机，允许读取 HOME 下的本地图片。"""
    os.environ.setdefault("IMAGE_MCP_STDIO", "1")
    os.environ.setdefault("IMAGE_MCP_ALLOW_LOCAL_PATHS", "1")

    async def _main():
        limits = httpx.Limits(max_keepalive_connections=20, max_connections=50)
        async with httpx.AsyncClient(limits=limits, timeout=httpx.Timeout(120.0, connect=10.0)) as client:
            mcp._http_client = client
            await mcp.run_stdio_async()

    asyncio.run(_main())


def main() -> None:
    if "--stdio" in sys.argv:
        run_stdio()
        return
    port = int(os.environ.get("MCP_SERVER_PORT", "3002"))
    logger.info(f"Image MCP Suite 启动在端口 {port}...")
    uvicorn.run(create_app(), host="127.0.0.1", port=port, proxy_headers=True, forwarded_allow_ips="127.0.0.1")


if __name__ == "__main__":
    main()
