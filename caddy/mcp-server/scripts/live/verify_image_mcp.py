#!/usr/bin/env python3
import os
import re
import asyncio
import httpx
import json
import sys
from pathlib import Path

domain = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("IMAGE_MCP_DOMAIN", "https://oc.khc6.cn")
token = os.environ.get("IMAGE_MCP_TOKEN", "")

if not token:
    local_md = Path(__file__).resolve().parent.parent / "DEPLOYMENT_VM.local.md"
    if local_md.exists():
        match = re.search(r"生产鉴权 Token[：:]\s*`?([a-zA-Z0-9_-]+)`?", local_md.read_text())
        if match:
            token = match.group(1).strip()

if not token:
    raise ValueError("请在环境变量 IMAGE_MCP_TOKEN 中设置鉴权 Token 或在 DEPLOYMENT_VM.local.md 中配置")

def parse_sse_response(text: str) -> dict:
    """解析 StreamableHTTP MCP 的 SSE 响应"""
    for line in text.split("\n"):
        line = line.strip()
        if line.startswith("data:"):
            json_str = line[5:].strip()
            return json.loads(json_str)
    return json.loads(text)

async def verify_all():
    async with httpx.AsyncClient(verify=False, timeout=30.0) as client:
        print("==================================================")
        print("1. 验证 Web 管理端页面与 KHC Design System 静态资源")
        print("==================================================")
        r_admin = await client.get(f"{domain}/admin")
        print(f"[+] GET /admin -> {r_admin.status_code} (长度: {len(r_admin.text)} bytes)")
        assert r_admin.status_code == 200, "Admin 页面加载失败"
        assert "KHC Image MCP 管理控制台" in r_admin.text, "页面内容不符"

        for css_file in ["colors_and_type.css", "core.css", "components.css"]:
            r_css = await client.get(f"{domain}/static/{css_file}")
            print(f"[+] GET /static/{css_file} -> {r_css.status_code}")
            assert r_css.status_code == 200, f"静态 CSS {css_file} 访问失败"

        print("\n==================================================")
        print("2. 验证 Web API 鉴权、EdgeOne Vault 清单与本地存储")
        print("==================================================")
        r_bad = await client.get(f"{domain}/api/config")
        print(f"[+] GET /api/config (无 Token) -> {r_bad.status_code} (期望 401)")
        assert r_bad.status_code == 401

        headers = {"Authorization": f"Bearer {token}"}
        r_good = await client.get(f"{domain}/api/config", headers=headers)
        print(f"[+] GET /api/config (有效 Token) -> {r_good.status_code}")
        cfg = r_good.json()
        print(f"[+] EdgeOne 中枢地址: {cfg.get('edgeone_vault', {}).get('url')}")
        print(f"[+] 本服务端已存储供应商: {list(cfg.get('providers', {}).keys())}")
        print(f"[+] 当前生图模式: {cfg.get('generation', {}).get('default_mode')} (已挂载 {len(cfg.get('generation', {}).get('models', []))} 个模型)")
        print(f"[+] 当前 OCR 等级: {cfg.get('vision', {}).get('default_preset')} (已挂载 {len(cfg.get('vision', {}).get('models', []))} 个模型)")

        r_mf = await client.post(f"{domain}/api/vault/manifest", json={}, headers=headers)
        mf_data = r_mf.json()
        print(f"[+] POST /api/vault/manifest -> {r_mf.status_code}, 发现中枢供应商: {mf_data.get('count')} 家")
        assert mf_data.get("ok") is True

        print("\n==================================================")
        print("2.1. 验证独立 Presets 规则库与免重启热重载 API")
        print("==================================================")
        r_presets = await client.get(f"{domain}/api/presets", headers=headers)
        assert r_presets.status_code == 200, "GET /api/presets 失败"
        p_data = r_presets.json()
        print(f"[+] GET /api/presets -> 成功获取 {len(p_data.get('presets', {}))} 个独立预设 ({list(p_data.get('presets', {}).keys())})")
        assert len(p_data.get('presets', {})) >= 6, "预设数量不足 6 个"

        r_reload = await client.post(f"{domain}/api/presets/reload", headers=headers)
        assert r_reload.status_code == 200, "POST /api/presets/reload 失败"
        reload_data = r_reload.json()
        print(f"[+] POST /api/presets/reload -> {reload_data.get('message')}")
        assert reload_data.get("ok") is True

        print("\n==================================================")
        print("3. 验证 MCP 2.0.0 协议初始化与工具注册列表")
        print("==================================================")
        init_payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2026-07-28",
                "capabilities": {},
                "clientInfo": {"name": "test-agent", "version": "1.0"}
            }
        }
        r_init = await client.post(f"{domain}/mcp", json=init_payload, headers=headers)
        sess_id = r_init.headers.get("mcp-session-id")
        print(f"[+] POST /mcp initialize -> {r_init.status_code}, Session: {sess_id}")

        mcp_headers = {**headers, "mcp-session-id": sess_id}
        await client.post(f"{domain}/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"}, headers=mcp_headers)

        list_payload = {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}
        r_tools = await client.post(f"{domain}/mcp", json=list_payload, headers=mcp_headers)
        res_data = parse_sse_response(r_tools.text)
        tools = res_data.get("result", {}).get("tools", [])
        tool_names = [t["name"] for t in tools]
        print(f"[+] 已注册 MCP 工具列表 ({len(tool_names)} 个): {tool_names}")
        for expected in ["generate_image", "edit_image", "describe_image", "search_images", "enhance_prompt", "list_models"]:
            assert expected in tool_names

        print("\n==================================================")
        print("4. 验证 MCP 工具 list_models 与 search_images 调用")
        print("==================================================")
        call_models = {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "list_models", "arguments": {}}
        }
        r_lm = await client.post(f"{domain}/mcp", json=call_models, headers=mcp_headers)
        lm_data = parse_sse_response(r_lm.text)
        lm_text = lm_data.get("result", {}).get("content", [{}])[0].get("text", "")
        print(f"[+] list_models 返回:\n{lm_text}\n")

        call_search = {
            "jsonrpc": "2.0",
            "id": 4,
            "method": "tools/call",
            "params": {
                "name": "search_images",
                "arguments": {"query": "cyberpunk neon city wallpaper", "limit": 3}
            }
        }
        r_search = await client.post(f"{domain}/mcp", json=call_search, headers=mcp_headers)
        search_data = parse_sse_response(r_search.text)
        search_res = search_data.get("result", {})
        res_text = search_res.get("content", [{}])[0].get("text", "")
        print(f"[+] 搜图结果预览:\n{res_text[:350]}...\n")
        assert "搜图结果" in res_text, "未成功获取搜图结果"

        print("==================================================")
        print("全部端到端功能验证通过！")
        print("==================================================")

if __name__ == "__main__":
    asyncio.run(verify_all())
