#!/usr/bin/env python3
import sys
import base64
import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, patch, MagicMock

# Ensure server.py can be imported
sys.path.append(str(Path(__file__).parent))

import server
import httpx

# Mock image classes since mcp is disabled locally

# Helper to verify base64 decoding of mock images
MOCK_B64_IMAGE = base64.b64encode(b"fake_image_bytes").decode("utf-8")
MOCK_B64_MASK = base64.b64encode(b"fake_mask_bytes").decode("utf-8")
MOCK_B64_IMAGE_LONG = base64.b64encode(b"fake_image_bytes" * 10).decode("utf-8")
MOCK_B64_MASK_LONG = base64.b64encode(b"fake_mask_bytes" * 10).decode("utf-8")

async def test_get_google_headers():
    print("--- Testing get_google_headers ---")
    mock_client = AsyncMock(spec=httpx.AsyncClient)
    
    # 1. API key auth
    with patch("server.API_KEY", "AIzaSyTestApiKey"):
        headers = await server.get_google_headers(mock_client)
        assert headers["x-goog-api-key"] == "AIzaSyTestApiKey"
        print("✔ API Key auth works")
        
    # 2. Metadata Server ADC auth
    with patch("server.API_KEY", ""):
        # Simulate successful token fetch
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"access_token": "ya29.GlcMockToken"}
        mock_client.get.return_value = mock_response
        
        headers = await server.get_google_headers(mock_client)
        assert headers["Authorization"] == "Bearer ya29.GlcMockToken"
        print("✔ Metadata Server ADC auth works")
        
        # Simulate metadata server failure (reset token cache to force hitting the metadata request path)
        server._gcp_token_cache = {"token": None, "expires_at": 0.0}
        mock_client.get.side_effect = Exception("Connection refused")
        try:
            await server.get_google_headers(mock_client)
            assert False, "Should raise RuntimeError"
        except RuntimeError as e:
            assert "Google Vertex AI authentication failed" in str(e)
            print("✔ Correctly raises error on auth failure")
            
    # Reset side effect
    mock_client.get.side_effect = None

async def test_generate_image_explicit():
    print("\n--- Testing generate_image (Explicit Model) ---")
    mock_client = AsyncMock(spec=httpx.AsyncClient)
    server.mcp._http_client = mock_client
    
    with patch("server.API_KEY", "AIzaSyTestApiKey"):
        # 1. Google Model Explicit Call
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "candidates": [{
                "content": {
                    "parts": [{
                        "inlineData": {
                            "mimeType": "image/png",
                            "data": MOCK_B64_IMAGE
                        }
                    }]
                }
            }]
        }
        mock_client.post.return_value = mock_resp
        
        result = await server.generate_image(prompt="a red apple", model="google/gemini-3.1-flash-lite-image", size="1024x1024")
        assert len(result) == 1
        assert result[0].data == b"fake_image_bytes"
        fmt = getattr(result[0], "format", getattr(result[0], "_format", None))
        assert fmt == "png"
        print("✔ Google explicit generate_image works")
        
        # Verify correct URL & payload structure
        post_args = mock_client.post.call_args
        assert "/publishers/google/models/gemini-3.1-flash-lite-image:generateContent" in post_args[0][0]
        payload = post_args[1]["json"]
        assert payload["contents"]["parts"][0]["text"] == "a red apple"
        assert payload["generationConfig"]["imageConfig"]["aspectRatio"] == "1:1"
        
        # 2. Agnes AI Explicit Call
        mock_resp_agnes = MagicMock()
        mock_resp_agnes.status_code = 200
        mock_resp_agnes.json.return_value = {
            "data": [{"b64_json": MOCK_B64_IMAGE}]
        }
        mock_client.post.return_value = mock_resp_agnes
        
        with patch("server.AGNES_API_KEY", "agnes_key"):
            result = await server.generate_image(prompt="a blue banana", model="agnes/agnes-image-21-flash", size="1024x1024")
            assert len(result) == 1
            assert result[0].data == b"fake_image_bytes"
            print("✔ Agnes AI explicit generate_image works")
            
            post_args = mock_client.post.call_args
            assert post_args[0][0] == "https://apihub.agnes-ai.com/v1/images/generations"
            assert post_args[1]["json"]["model"] == "agnes-image-21-flash"
            assert post_args[1]["headers"]["Authorization"] == "Bearer agnes_key"

        # 3. MiniMax Explicit Call
        mock_resp_minimax = MagicMock()
        mock_resp_minimax.status_code = 200
        mock_resp_minimax.json.return_value = {
            "data": {"image_base64": [MOCK_B64_IMAGE]}
        }
        mock_client.post.return_value = mock_resp_minimax
        
        with patch("server.MINIMAX_API_KEY", "minimax_key"):
            result = await server.generate_image(prompt="a yellow lemon", model="minimax/image-01", size="1024x1024")
            assert len(result) == 1
            assert result[0].data == b"fake_image_bytes"
            print("✔ MiniMax explicit generate_image works")
            
            post_args = mock_client.post.call_args
            assert post_args[0][0] == "https://api.minimaxi.com/v1/image_generation"
            assert post_args[1]["json"]["model"] == "image-01"
            assert post_args[1]["json"]["aspect_ratio"] == "1:1"
            assert post_args[1]["headers"]["Authorization"] == "Bearer minimax_key"

async def test_generate_image_fallback():
    print("\n--- Testing generate_image (Fallback sequence) ---")
    mock_client = AsyncMock(spec=httpx.AsyncClient)
    server.mcp._http_client = mock_client
    
    with patch("server.API_KEY", "AIzaSyTestApiKey"), \
         patch("server.AGNES_API_KEY", "agnes_key"), \
         patch("server.MINIMAX_API_KEY", "minimax_key"), \
         patch("server.SENSENOVA_API_KEY", "sensenova_key"):
        
        # Scenario: Google returns 429, Agnes fails, MiniMax succeeds
        google_resp = MagicMock()
        google_resp.status_code = 429
        google_resp.text = "Quota exceeded"
        
        agnes_resp = MagicMock()
        agnes_resp.status_code = 500
        agnes_resp.text = "Internal Server Error"
        
        minimax_resp = MagicMock()
        minimax_resp.status_code = 200
        minimax_resp.json.return_value = {
            "data": {"image_base64": [MOCK_B64_IMAGE]}
        }
        
        # Set mock return sequence for POST calls
        mock_client.post.side_effect = [google_resp, agnes_resp, minimax_resp]
        
        # Leave model parameter empty to trigger auto-fallback
        result = await server.generate_image(prompt="a yellow lemon")
        assert len(result) == 1
        assert result[0].data == b"fake_image_bytes"
        print("✔ Auto-fallback from Google -> Agnes -> MiniMax works successfully")
        
        # Reset side effect
        mock_client.post.side_effect = None

        # Scenario: All fail, verify aggregate error report
        google_resp = MagicMock()
        google_resp.status_code = 403
        google_resp.text = "Forbidden"
        
        agnes_resp = MagicMock()
        agnes_resp.status_code = 503
        agnes_resp.text = "Service Unavailable"
        
        minimax_resp = MagicMock()
        minimax_resp.status_code = 401
        minimax_resp.text = "Unauthorized"
        
        sensenova_resp = MagicMock()
        sensenova_resp.status_code = 400
        sensenova_resp.text = "Bad Request"
        
        mock_client.post.side_effect = [google_resp, agnes_resp, minimax_resp, sensenova_resp]
        
        try:
            await server.generate_image(prompt="a purple dragon")
            assert False, "Should raise aggregate error"
        except RuntimeError as e:
            err_msg = str(e)
            assert "All image generation attempts failed" in err_msg
            assert "Forbidden" in err_msg
            assert "Service Unavailable" in err_msg
            assert "Unauthorized" in err_msg
            assert "Bad Request" in err_msg
            print("✔ Aggregate error reports all failures accurately")
            
        mock_client.post.side_effect = None

async def test_edit_image():
    print("\n--- Testing edit_image (JSON & Multipart) ---")
    mock_client = AsyncMock(spec=httpx.AsyncClient)
    server.mcp._http_client = mock_client
    
    with patch("server.API_KEY", "AIzaSyTestApiKey"), \
         patch("server.AGNES_API_KEY", "agnes_key"):
        
        # 1. Google Edit (Prompt-based, JSON payload)
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "candidates": [{
                "content": {
                    "parts": [{
                        "inlineData": {
                            "mimeType": "image/png",
                            "data": MOCK_B64_IMAGE
                        }
                    }]
                }
            }]
        }
        mock_client.post.return_value = mock_resp
        
        result = await server.edit_image(image=MOCK_B64_IMAGE_LONG, prompt="add a party hat", model="google/gemini-3.1-flash-lite-image")
        assert len(result) == 1
        assert result[0].data == b"fake_image_bytes"
        print("✔ Google prompt-based edit_image works")
        
        # Verify Google edit payload parts contains base image and prompt
        post_args = mock_client.post.call_args
        parts = post_args[1]["json"]["contents"]["parts"]
        assert parts[0]["inlineData"]["data"] == MOCK_B64_IMAGE_LONG
        assert parts[1]["text"] == "add a party hat"
        
        # 2. Agnes Edit (Image-to-image, JSON payload)
        mock_resp_agnes = MagicMock()
        mock_resp_agnes.status_code = 200
        mock_resp_agnes.json.return_value = {
            "data": [{"b64_json": MOCK_B64_IMAGE}]
        }
        mock_client.post.return_value = mock_resp_agnes
        
        result = await server.edit_image(
            image=MOCK_B64_IMAGE_LONG,
            prompt="make it a watercolor painting",
            mask=MOCK_B64_MASK_LONG,
            model="agnes/agnes-image-2.1-flash"
        )
        assert len(result) == 1
        assert result[0].data == b"fake_image_bytes"
        print("✔ Agnes JSON edit_image works")
        
        # Verify JSON payload
        post_args = mock_client.post.call_args
        assert post_args[0][0] == "https://apihub.agnes-ai.com/v1/images/generations"
        payload = post_args[1]["json"]
        assert payload["model"] == "agnes-image-2.1-flash"
        assert payload["prompt"] == "make it a watercolor painting"
        assert payload["image"] == f"data:image/png;base64,{MOCK_B64_IMAGE_LONG}"

        # 3. MiniMax Edit (I2I subject_reference, JSON payload)
        mock_resp_minimax = MagicMock()
        mock_resp_minimax.status_code = 200
        mock_resp_minimax.json.return_value = {
            "data": {"image_base64": [MOCK_B64_IMAGE]}
        }
        mock_client.post.return_value = mock_resp_minimax
        
        with patch("server.MINIMAX_API_KEY", "minimax_key"):
            result = await server.edit_image(
                image=MOCK_B64_IMAGE_LONG,
                prompt="turn it into a digital artwork",
                model="minimax/image-01"
            )
            assert len(result) == 1
            assert result[0].data == b"fake_image_bytes"
            print("✔ MiniMax subject_reference edit_image works")
            
            post_args = mock_client.post.call_args
            assert post_args[0][0] == "https://api.minimaxi.com/v1/image_generation"
            payload = post_args[1]["json"]
            assert payload["model"] == "image-01"
            assert payload["prompt"] == "turn it into a digital artwork"
            assert payload["subject_reference"][0]["type"] == "character"
            assert payload["subject_reference"][0]["image_file"] == f"data:image/png;base64,{MOCK_B64_IMAGE_LONG}"

# upscale_image is deprecated and removed from server.py

async def test_generate_video():
    print("\n--- Testing generate_video (Agnes & SenseNova) ---")
    mock_client = AsyncMock(spec=httpx.AsyncClient)
    server.mcp._http_client = mock_client
    
    with patch("server.AGNES_API_KEY", "agnes_key"), \
         patch("server.SENSENOVA_API_KEY", "sensenova_key"):
        
        # 1. Agnes Video Gen with status polling
        create_resp = MagicMock()
        create_resp.status_code = 200
        create_resp.json.return_value = {"id": "agnes_task_123"}
        
        poll_resp_running = MagicMock()
        poll_resp_running.status_code = 200
        poll_resp_running.json.return_value = {"status": "running"}
        
        poll_resp_completed = MagicMock()
        poll_resp_completed.status_code = 200
        poll_resp_completed.json.return_value = {
            "status": "completed",
            "video_url": "https://cdn.agnes.ai/video123.mp4"
        }
        
        mock_client.post.return_value = create_resp
        mock_client.get.side_effect = [poll_resp_running, poll_resp_completed]
        
        # Speed up sleeps in generate_video for testing
        with patch("asyncio.sleep", AsyncMock()):
            result = await server.generate_video(prompt="a drone flight over forest", model="agnes/agnes-video-v20")
            assert "https://cdn.agnes.ai/video123.mp4" in result
            print("✔ Agnes generate_video polling works")
            
        # Reset side effects
        mock_client.get.side_effect = None
        


async def test_list_models():
    print("\n--- Testing list_models capabilities matrix ---")
    result = await server.list_models()
    assert "google/gemini-3.1-flash-lite-image" in result
    assert "agnes/agnes-video" in result
    assert "sensenova/sensenova-u" in result
    assert "Vendor-Specific Capability Matrix" in result
    print("✔ list_models description is accurate and informative")

async def run_all_tests():
    try:
        await test_get_google_headers()
        await test_generate_image_explicit()
        await test_generate_image_fallback()
        await test_edit_image()
        await test_generate_video()
        await test_list_models()
        print("\n🎉 ALL TESTS COMPLETED SUCCESSFULLY!")
    except AssertionError:
        print("\n❌ TEST FAILURE: AssertionError", file=sys.stderr)
        import traceback
        traceback.print_exc()
        sys.exit(1)
    except Exception as e:
        print(f"\n❌ TEST FAILURE: Unexpected Exception: {e}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        sys.exit(1)

if __name__ == "__main__":
    asyncio.run(run_all_tests())
