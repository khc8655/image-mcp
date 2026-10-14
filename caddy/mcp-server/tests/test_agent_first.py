"""Agent 优先的生图：默认值、降级适配、Gemini 请求与说明文字。"""
import json

import httpx
import pytest

import server
import providers
import prompting
from conftest import png_b64


def structured(res):
    return getattr(res, "structuredContent", None) or res.structured_content


@pytest.mark.asyncio
async def test_omitted_params_use_admin_defaults(upstream):
    gen = server.config_mgr._config["generation"]
    gen.update({"default_resolution": "4K", "default_count": 3, "default_grounding": True, "default_quality": "fast"})
    gen["models"] = [{"provider": "google", "model": "gemini-nano-banana-2.1", "enabled": True}]
    res = await server.generate_image(prompt="x")
    call = upstream.calls[-1]
    assert (call["resolution"], call["count"], call["grounding"], call["thinking"]) == ("4K", 3, True, "")
    assert structured(res)["quality"] == "fast"


@pytest.mark.asyncio
async def test_explicit_params_beat_defaults(upstream):
    server.config_mgr._config["generation"].update({"default_grounding": True, "default_count": 3})
    await server.generate_image(prompt="x", grounding=False, count=1)
    call = upstream.calls[-1]
    assert call["count"] == 1 and call["grounding"] is False


@pytest.mark.asyncio
async def test_fallback_to_minimax_adapts_prompt_and_aspect(upstream):
    server.config_mgr._config["generation"]["models"] = [
        {"provider": "google", "model": "gemini-nano-banana-2.1", "enabled": True},
        {"provider": "minimax", "model": "image-01", "enabled": True},
    ]
    upstream.fail.add("google")
    long_prompt = ("A detailed architecture diagram for a review slide. " * 60).strip()
    res = await server.generate_image(prompt=long_prompt, aspect_ratio="4:5", grounding=True)
    call = upstream.calls[-1]
    assert call["provider_id"] == "minimax"
    assert len(call["prompt"]) <= 1500 and call["prompt"].endswith(".")
    assert call["aspect_ratio"] in ("3:4", "1:1")
    assert call["grounding"] is False
    adj = structured(res)["adjustments"]
    assert any("1500" in a for a in adj) and any("4:5" in a for a in adj)


@pytest.mark.asyncio
async def test_safety_block_returns_rewrite_hint_without_fallback(upstream, monkeypatch):
    server.config_mgr._config["generation"]["models"] = [
        {"provider": "google", "model": "gemini-nano-banana-2.1", "enabled": True},
        {"provider": "minimax", "model": "image-01", "enabled": True},
    ]

    async def blocked(**kw):
        upstream.calls.append(kw)
        raise providers.SafetyBlocked("Gemini 拒绝了该提示词（SAFETY）。请换一种说法")

    monkeypatch.setattr(providers, "execute_image_generation", blocked)
    with pytest.raises(RuntimeError, match="换一种说法"):
        await server.generate_image(prompt="x")
    assert len(upstream.calls) == 1


@pytest.mark.asyncio
async def test_edit_skips_minimax_and_passes_references(upstream):
    server.config_mgr._config["generation"]["models"] = [
        {"provider": "minimax", "model": "image-01", "enabled": True},
        {"provider": "google", "model": "gemini-nano-banana-2.1", "enabled": True},
    ]
    b64 = png_b64()
    res = await server.edit_image(image=b64, prompt="add the lamp", reference_images=[b64, b64])
    call = upstream.calls[-1]
    assert call["provider_id"] == "google" and len(call["reference_images_b64"]) == 2
    assert structured(res)["provider"] == "google"


def test_nearest_aspect_and_trim():
    assert prompting.nearest_aspect("4:5", ["1:1", "3:4", "16:9"]) == "3:4"
    assert prompting.nearest_aspect("4:1", ["16:9", "21:9", "1:1"]) == "21:9"
    assert prompting.nearest_aspect("16:9", ["16:9"]) == "16:9"
    assert prompting.trim_prompt("第一句。第二句很长很长。", 6) == "第一句。"


def test_model_aliases():
    assert prompting.normalize_gemini_model("gemini-3.1-flash-image-preview") == "gemini-3.1-flash-image"
    assert prompting.normalize_gemini_model("nano-banana-2.1") == "gemini-nano-banana-2.1"
    assert prompting.normalize_gemini_model("models/gemini-3-pro-image") == "gemini-3-pro-image"
    assert not prompting.gemini_supports_reasoning("gemini-3.1-flash-lite-image")


def test_minimax_optimizer_only_for_short_prompts():
    assert prompting.minimax_prompt_optimizer("a cat") is True
    assert prompting.minimax_prompt_optimizer("x" * 300) is False
    assert prompting.minimax_prompt_optimizer("x" * 300, override=True) is True


def test_parse_gemini_images_note_and_block():
    ok = {"candidates": [{"content": {"parts": [
        {"text": "thinking...", "thought": True},
        {"text": "Here is your diagram."},
        {"inlineData": {"mimeType": "image/png", "data": "AAAA"}},
    ]}}]}
    imgs = providers.parse_gemini_images(ok)
    assert imgs[0]["note"] == "Here is your diagram."
    with pytest.raises(providers.SafetyBlocked):
        providers.parse_gemini_images({"promptFeedback": {"blockReason": "SAFETY"}})
    with pytest.raises(providers.SafetyBlocked):
        providers.parse_gemini_images({"candidates": [{"finishReason": "IMAGE_SAFETY", "content": {"parts": []}}]})


@pytest.mark.asyncio
async def test_gemini_payload_grounding_thinking_and_retry(monkeypatch):
    sent = []

    def handler(request: httpx.Request):
        body = json.loads(request.content)
        sent.append((str(request.url), body))
        if len(sent) == 1:
            return httpx.Response(400, json={"error": {"message": "tools not supported"}})
        return httpx.Response(200, json={"candidates": [{"content": {"parts": [
            {"inlineData": {"mimeType": "image/png", "data": png_b64()}}]}}]})

    monkeypatch.setattr(providers, "resolve_local_provider_and_key",
                        lambda p, k: ({"adapter_rules": {}}, "k1", "AIzaFAKEKEY"))
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        imgs = await providers.execute_image_generation(
            client=client, provider_id="google", model="gemini-3.1-flash-image-preview", key_label="",
            prompt="p", aspect_ratio="16:9", resolution="2K", grounding=True, thinking="high",
        )
    assert imgs
    url, first = sent[0]
    assert "gemini-3.1-flash-image:generateContent" in url
    assert first["tools"] == [{"googleSearch": {}}]
    assert first["generationConfig"]["thinkingConfig"] == {"thinkingLevel": "high"}
    assert first["generationConfig"]["imageConfig"] == {"aspectRatio": "16:9", "imageSize": "2K"}
    _, retry = sent[1]
    assert "tools" not in retry and "thinkingConfig" not in retry["generationConfig"]


@pytest.mark.asyncio
async def test_gemini_count_runs_parallel_calls(monkeypatch):
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(200, json={"candidates": [{"content": {"parts": [
            {"inlineData": {"mimeType": "image/png", "data": png_b64()}}]}}]})

    monkeypatch.setattr(providers, "resolve_local_provider_and_key",
                        lambda p, k: ({"adapter_rules": {}}, "k1", "AIzaFAKEKEY"))
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        imgs = await providers.execute_image_generation(
            client=client, provider_id="google", model="gemini-nano-banana-2.1", key_label="",
            prompt="p", count=3,
        )
    assert len(imgs) == 3 and len(calls) == 3


@pytest.mark.asyncio
async def test_server_surface_for_agents():
    assert "generate_image" in (server.mcp.instructions or "")
    tools = {t.name: t for t in await server.mcp.list_tools()}
    assert "enhance_prompt" not in tools
    gen = tools["generate_image"]
    props = set(gen.input_schema["properties"])
    assert props == {"prompt", "aspect_ratio", "resolution", "quality", "grounding", "count"}
    assert "no negative prompt" in gen.description.lower()
    prompts = {p.name for p in await server.mcp.list_prompts()}
    assert {"architecture_diagram", "infographic", "slide_background"} <= prompts
