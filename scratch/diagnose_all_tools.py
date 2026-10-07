import os
import re
import asyncio
import httpx
import json
import base64
from pathlib import Path

domain = os.environ.get("IMAGE_MCP_DOMAIN", "https://oc.khc6.cn")
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
    for line in text.split("\n"):
        line = line.strip()
        if line.startswith("data:"):
            json_str = line[5:].strip()
            return json.loads(json_str)
    try:
        return json.loads(text)
    except Exception:
        return {"raw": text}

async def run_diagnostics():
    async with httpx.AsyncClient(verify=False, timeout=60.0) as client:
        print("=== 1. Web & API 基础可达性 ===")
        r_admin = await client.get(f"{domain}/admin")
        print(f"GET /admin -> HTTP {r_admin.status_code}")
        
        headers = {"Authorization": f"Bearer {token}"}
        r_cfg = await client.get(f"{domain}/api/config", headers=headers)
        print(f"GET /api/config -> HTTP {r_cfg.status_code}")
        cfg = r_cfg.json()
        print("生图模型池:", [f"{m['provider']}/{m['model']} (key={m.get('key')})" for m in cfg.get("generation", {}).get("models", [])])
        print("识图模型池:", [f"{m['provider']}/{m['model']} (key={m.get('key')})" for m in cfg.get("vision", {}).get("models", [])])

        print("\n=== 2. MCP 2.0.0 握手与工具枚举 ===")
        init_payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2026-07-28",
                "capabilities": {},
                "clientInfo": {"name": "diagnose-client", "version": "1.0"}
            }
        }
        r_init = await client.post(f"{domain}/mcp", json=init_payload, headers=headers)
        sess_id = r_init.headers.get("mcp-session-id")
        print(f"POST /mcp initialize -> HTTP {r_init.status_code}, Session: {sess_id}")
        mcp_headers = {**headers, "mcp-session-id": sess_id}

        await client.post(f"{domain}/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"}, headers=mcp_headers)
        
        r_tools = await client.post(f"{domain}/mcp", json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}, headers=mcp_headers)
        tools = parse_sse_response(r_tools.text).get("result", {}).get("tools", [])
        print("已注册工具:", [t["name"] for t in tools])

        print("\n=== 3. 逐项测试 MCP 核心工具 ===")
        
        # 3.1 list_models
        r = await client.post(f"{domain}/mcp", json={"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "list_models", "arguments": {}}}, headers=mcp_headers)
        res = parse_sse_response(r.text)
        is_err = res.get("result", {}).get("isError", False) or "error" in res
        print(f"[*] 工具 list_models -> {'❌ 失败' if is_err else '✅ 正常'}")
        
        # 3.2 search_images
        r = await client.post(f"{domain}/mcp", json={"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "search_images", "arguments": {"query": "mountain lake", "limit": 2}}}, headers=mcp_headers)
        res = parse_sse_response(r.text)
        is_err = res.get("result", {}).get("isError", False) or "error" in res
        print(f"[*] 工具 search_images -> {'❌ 失败' if is_err else '✅ 正常'}")

        # 3.3 enhance_prompt
        r = await client.post(f"{domain}/mcp", json={"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": "enhance_prompt", "arguments": {"prompt": "可爱的小猫", "style": "anime"}}}, headers=mcp_headers)
        res = parse_sse_response(r.text)
        is_err = res.get("result", {}).get("isError", False) or "error" in res
        print(f"[*] 工具 enhance_prompt -> {'❌ 失败' if is_err else '✅ 正常'}")

        # 3.4 describe_image (使用小图片测试)
        tiny_png = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
        r = await client.post(f"{domain}/mcp", json={"jsonrpc": "2.0", "id": 6, "method": "tools/call", "params": {"name": "describe_image", "arguments": {"image": f"data:image/png;base64,{tiny_png}", "prompt": "描述这张图片"}}}, headers=mcp_headers)
        res = parse_sse_response(r.text)
        is_err = res.get("result", {}).get("isError", False) or "error" in res
        print(f"[*] 工具 describe_image -> {'❌ 失败' if is_err else '✅ 正常'}")
        if is_err:
            print(f"    错误详情: {res}")
        else:
            txt = res.get("result", {}).get("content", [{}])[0].get("text", "")
            print(f"    识别输出: {txt[:100]}...")

        # 3.5 generate_image
        r = await client.post(f"{domain}/mcp", json={"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": {"name": "generate_image", "arguments": {"prompt": "A red apple on a clean wooden table, 8k resolution"}}}, headers=mcp_headers)
        res = parse_sse_response(r.text)
        is_err = res.get("result", {}).get("isError", False) or "error" in res
        print(f"[*] 工具 generate_image -> {'❌ 失败' if is_err else '✅ 正常'}")
        if is_err:
            print(f"    错误详情: {res}")
        else:
            cnt = len(res.get("result", {}).get("content", []))
            print(f"    出图结果: 成功返回 {cnt} 张图像")

if __name__ == "__main__":
    asyncio.run(run_diagnostics())
