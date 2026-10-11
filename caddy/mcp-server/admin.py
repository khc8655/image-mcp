"""Web 管理控制台与 EdgeOne Vault / Presets 同步 API（均需 Admin Token）。"""
import re
import time

import httpx

from config_manager import config_mgr
from security import sanitize_error
import storage


from pathlib import Path

from starlette.responses import JSONResponse, Response, HTMLResponse

from core import mcp, logger, SERVER_START_TIME, KNOWN_PROVIDER_IMAGE_MODELS, APP_VERSION
from providers import resolve_local_provider_and_key
from tools.search import search_images

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
    v_tag = f"?v={APP_VERSION}.{int(SERVER_START_TIME)}"
    html_content = re.sub(r'/static/admin\.css(\?v=[^\s"\'<>]+)?', f'/static/admin.css{v_tag}', html_content)
    html_content = re.sub(r'/static/js/app\.js(\?v=[^\s"\'<>]+)?', f'/static/js/app.js{v_tag}', html_content)
    # 动态渲染顶部导航栏版本号 Badge
    html_content = re.sub(
        r'<span class="badge b-done"[^>]*>v[0-9.]+</span>',
        f'<span class="badge b-done" style="margin-left:8px;" id="appVersionBadge">v{APP_VERSION}</span>',
        html_content
    )


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

                    config_mgr.upsert_provider_from_vault(pid, prov_cfg)
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


async def get_storage_info_api(request) -> Response:
    """获取生图持久化存储统计（图片数、大小、按日分布）"""
    if not require_admin_token(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    try:
        stats = storage.get_storage_stats()
        return JSONResponse({"ok": True, **stats})
    except Exception as e:
        return JSONResponse({"ok": False, "error": sanitize_error(e)}, status_code=500)


async def post_storage_clean_api(request) -> Response:
    """清空服务器上所有已生成的图片缓存"""
    if not require_admin_token(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    try:
        res = storage.clean_storage()
        return JSONResponse(res)
    except Exception as e:
        return JSONResponse({"ok": False, "error": sanitize_error(e)}, status_code=500)

