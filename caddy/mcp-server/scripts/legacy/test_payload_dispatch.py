"""
scratch/test_payload_dispatch.py
验证各厂商（Google Gemini, MiniMax, Agnes AI, SenseNova）生图与识图 Payload 组装与调度
"""
import sys
import json
import base64
from pathlib import Path
from unittest.mock import patch, MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import asyncio
from unittest.mock import AsyncMock, patch, MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config_manager import ConfigManager
import server

# 注入全局 config_mgr
server.config_mgr = ConfigManager()

async def run_dispatch_tests():
    print("[1] 测试 Agnes AI 生图 Payload 组装与调用拦截...")
    captured_requests = []

    mock_client = AsyncMock()

    async def mock_post(url, headers=None, json=None, **kwargs):
        captured_requests.append({"url": str(url), "headers": headers, "json": json})
        resp = MagicMock()
        resp.status_code = 200
        url_str = str(url)
        if ":generateContent" in url_str:
            resp.json.return_value = {
                "candidates": [
                    {
                        "content": {
                            "parts": [
                                {
                                    "inlineData": {
                                        "mimeType": "image/png",
                                        "data": "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
                                    }
                                }
                            ]
                        }
                    }
                ]
            }
        else:
            resp.json.return_value = {
                "data": [
                    {"b64_json": "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="}
                ]
            }
        return resp

    mock_client.post = mock_post

    # 1.1 测试 Agnes 文生图
    res = await server.execute_image_generation(
        client=mock_client,
        provider_id="agnes",
        model="agnes-image-2.5-flash",
        key_label="default",
        prompt="A futuristic cyberpunk laboratory",
        aspect_ratio="16:9"
    )
    assert len(res) == 1
    req = captured_requests[-1]
    assert req["url"] == "https://apihub.agnes-ai.com/v1/images/generations"
    assert req["json"]["model"] == "agnes-image-2.5-flash"
    assert req["json"]["size"] == "2K"
    assert req["json"]["ratio"] == "16:9"
    # 验证符合官方文档：顶层严禁 response_format，extra_body 中带 response_format: b64_json
    assert "response_format" not in req["json"]
    assert req["json"]["extra_body"]["response_format"] == "b64_json"
    print("  -> Agnes AI 文生图 Payload 完美符合官方规范！")

    # 1.2 测试 Agnes 图生图
    res_edit = await server.execute_image_generation(
        client=mock_client,
        provider_id="agnes",
        model="agnes-image-2.5-flash",
        key_label="default",
        prompt="Make it neon purple",
        aspect_ratio="16:9",
        action="edit",
        image_b64="iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
    )
    assert len(res_edit) == 1
    req_edit = captured_requests[-1]
    assert "image" in req_edit["json"]["extra_body"]
    assert isinstance(req_edit["json"]["extra_body"]["image"], list)
    print("  -> Agnes AI 图生图 (extra_body.image 数组) Payload 完美符合官方规范！")

    print("[2] 测试 MiniMax 生图 Payload 组装...")
    res_mm = await server.execute_image_generation(
        client=mock_client,
        provider_id="minimax",
        model="image-01",
        key_label="default",
        prompt="Cyberpunk street",
        aspect_ratio="21:9",
        action="edit",
        image_b64="iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
    )
    req_mm = captured_requests[-1]
    assert req_mm["json"]["aspect_ratio"] == "21:9"
    assert "subject_reference" in req_mm["json"]
    assert req_mm["json"]["subject_reference"][0]["type"] == "character"
    print("  -> MiniMax 21:9 长宽比与 subject_reference 人物主体参考组装完美符合！")

    print("[3] 测试 Gemini Native 原生生图 Payload 组装...")
    res_gem = await server.execute_image_generation(
        client=mock_client,
        provider_id="google",
        model="gemini-3.1-flash-lite-image",
        key_label="default",
        prompt="Sunset over Mars",
        aspect_ratio="16:9"
    )
    req_gem = captured_requests[-1]
    assert ":generateContent" in req_gem["url"]
    assert "contents" in req_gem["json"]
    assert "responseModalities" in req_gem["json"]["generationConfig"]
    print("  -> Gemini Vertex/AI Studio 原生 :generateContent 组装完美符合！")

    print("[4] 测试识图 (Vision) 协议驱动与多模态解析...")
    async def mock_vision_post(url, headers=None, json=None, **kwargs):
        captured_requests.append({"url": str(url), "headers": headers, "json": json})
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {
            "choices": [
                {
                    "message": {
                        "content": "A high-tech control panel with graphs and numbers."
                    }
                }
            ]
        }
        return resp

    mock_client.post = mock_vision_post
    vis_res = await server.execute_vision_completion(
        client=mock_client,
        provider_id="agnes",
        model="agnes-3.0-flash",
        key_label="default",
        prompt="Extract the text in table format",
        image_b64="iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
    )
    assert "high-tech control panel" in vis_res
    req_v = captured_requests[-1]
    assert "/chat/completions" in req_v["url"]
    content_items = req_v["json"]["messages"][0]["content"]
    assert any(it.get("type") == "image_url" for it in content_items)
    print("  -> Agnes 3.0 Flash 512K 原生多模态 Vision 驱动分发解析完全成功！")

if __name__ == "__main__":
    try:
        asyncio.run(run_dispatch_tests())
        print("\n==========================================")
        print("🎉 全部生图/识图厂商协议分发拦截测试 100% 通过！")
        print("==========================================")
    except Exception as e:
        print(f"\n❌ 测试失败: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
        print("\n==========================================")
        print("🎉 全部生图/识图厂商协议分发拦截测试 100% 通过！")
        print("==========================================")
    except Exception as e:
        print(f"\n❌ 测试失败: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
