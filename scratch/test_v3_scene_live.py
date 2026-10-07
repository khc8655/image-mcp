import os
import re
import asyncio
import httpx
import json
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

async def run_v3_tests():
    headers = {"Authorization": f"Bearer {token}"}
    async with httpx.AsyncClient(verify=False, timeout=90.0) as client:
        # 1. 初始化会话
        init_payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2026-07-28",
                "capabilities": {},
                "clientInfo": {"name": "test-v3-client", "version": "1.0"}
            }
        }
        r_init = await client.post(f"{domain}/mcp", json=init_payload, headers=headers)
        sess_id = r_init.headers.get("mcp-session-id")
        mcp_headers = {**headers, "mcp-session-id": sess_id}
        await client.post(f"{domain}/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"}, headers=mcp_headers)

        # 2. 验证 list_models 中的 v3.0 信息与 5 大场景
        print("=== 1. 验证 list_models v3.0 信息 ===")
        r = await client.post(f"{domain}/mcp", json={"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "list_models", "arguments": {}}}, headers=mcp_headers)
        res = parse_sse_response(r.text)
        txt = res.get("result", {}).get("content", [{}])[0].get("text", "")
        models_data = json.loads(txt) if txt else {}
        print(f"服务版本: {models_data.get('version')}")
        print(f"生图默认配置: {models_data.get('generation_defaults')}")
        print(f"支持场景列表: {[s['id'] for s in models_data.get('available_scenes', [])]}")
        assert models_data.get("version") == "3.0"
        assert len(models_data.get("available_scenes", [])) == 5

        # 3. 验证办公场景生图 (infra_scene + 21:9 + 2K)
        print("\n=== 2. 测试办公场景模板文生图 (infra_scene, 21:9, 2K) ===")
        gen_payload = {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {
                "name": "generate_image",
                "arguments": {
                    "prompt": "省级政务大数据与算力中心全景",
                    "scene": "infra_scene",
                    "aspect_ratio": "21:9",
                    "resolution": "2K"
                }
            }
        }
        r_gen = await client.post(f"{domain}/mcp", json=gen_payload, headers=mcp_headers)
        res_gen = parse_sse_response(r_gen.text)
        is_err = res_gen.get("result", {}).get("isError", False) or "error" in res_gen
        if is_err:
            print(f"❌ 出图失败: {res_gen}")
        else:
            cnt = len(res_gen.get("result", {}).get("content", []))
            print(f"✅ 办公场景出图成功! 返回图片数量: {cnt}")

        # 4. 验证 enhance_prompt purpose='work'
        print("\n=== 3. 测试 enhance_prompt 办公技术风格扩写 (purpose='work') ===")
        enh_payload = {
            "jsonrpc": "2.0",
            "id": 4,
            "method": "tools/call",
            "params": {
                "name": "enhance_prompt",
                "arguments": {
                    "prompt": "金融核心系统多地多活架构",
                    "purpose": "work",
                    "style": "topology"
                }
            }
        }
        r_enh = await client.post(f"{domain}/mcp", json=enh_payload, headers=mcp_headers)
        res_enh = parse_sse_response(r_enh.text)
        txt_enh = res_enh.get("result", {}).get("content", [{}])[0].get("text", "")
        print(f"扩写输出: {txt_enh[:150]}...")

if __name__ == "__main__":
    asyncio.run(run_v3_tests())
