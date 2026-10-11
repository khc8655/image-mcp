import pytest

import server
import providers
from core import resolve_image_to_base64
from conftest import png_b64


def structured(res):
    return getattr(res, "structuredContent", None) or res.structured_content


@pytest.mark.asyncio
async def test_aspect_auto_uses_admin_default(upstream):
    await server.generate_image(prompt="x")
    assert upstream.calls[-1]["aspect_ratio"] == "16:9"


@pytest.mark.asyncio
async def test_explicit_square_not_overridden(upstream):
    await server.generate_image(prompt="x", aspect_ratio="1:1", scene="infographic")
    assert upstream.calls[-1]["aspect_ratio"] == "1:1"


@pytest.mark.asyncio
async def test_scene_default_aspect(upstream):
    await server.generate_image(prompt="x", scene="infographic")
    assert upstream.calls[-1]["aspect_ratio"] == "3:4"


@pytest.mark.asyncio
async def test_result_is_url_plus_thumbnail(upstream):
    res = await server.generate_image(prompt="x")
    data = structured(res)
    assert data["provider"] == "openai"
    img = data["images"][0]
    assert img["url"].startswith("http://testserver/files/") and "sig=" in img["url"]
    assert (img["width"], img["height"]) == (64, 48)
    kinds = [c.type for c in res.content]
    assert kinds == ["text", "resource_link", "image"]


@pytest.mark.asyncio
async def test_inline_mode(upstream):
    server.config_mgr._config["generation"]["return_mode"] = "inline"
    res = await server.generate_image(prompt="x")
    assert [c.type for c in res.content] == ["text", "image"]


@pytest.mark.asyncio
async def test_failover_and_sanitized_errors(upstream):
    upstream.fail.add("openai")
    res = await server.generate_image(prompt="x")
    data = structured(res)
    assert data["provider"] == "google"
    err = data["failed_attempts"][0]["error"]
    assert "SECRET" not in err


@pytest.mark.asyncio
async def test_all_fail_message_sanitized(upstream):
    upstream.fail.update({"openai", "google"})
    with pytest.raises(RuntimeError) as ei:
        await server.generate_image(prompt="x")
    assert "SECRET" not in str(ei.value)


@pytest.mark.asyncio
async def test_edit_with_mask_routes_to_mask_capable(upstream):
    b64 = png_b64()
    await server.edit_image(image=b64, prompt="x", mask=b64, count=2)
    call = upstream.calls[-1]
    assert call["provider_id"] == "google"
    assert call["mask_b64"] == b64 and call["count"] == 2


@pytest.mark.asyncio
async def test_model_targeting_exact(upstream):
    await server.generate_image(prompt="x", model="google/gemini-3.1-flash-image")
    assert upstream.calls[-1]["provider_id"] == "google"


@pytest.mark.asyncio
async def test_local_path_rejected_in_remote_mode():
    server.mcp._http_client = object()
    with pytest.raises(ValueError, match="远程部署"):
        await resolve_image_to_base64(None, "/etc/hosts.png")


@pytest.mark.asyncio
async def test_describe_gui_elements(monkeypatch):
    async def fake_vision(**kw):
        return '好的：[{"label":"登录","type":"button","box":[900, 100, 950, 1200]}]'
    monkeypatch.setattr(providers, "execute_vision_completion", fake_vision)
    server.mcp._http_client = object()
    res = await server.describe_image(image=png_b64(), preset="gui")
    el = structured(res)["elements"][0]
    assert el["box"] == [900, 100, 950, 1000]
    assert el["center"] == [925, 550]
