
import pytest

import server
import providers
from tools import vision as vision_tools, search as search_tools
from cache import vision_cache, search_cache
from conftest import png_b64


def structured(res):
    return getattr(res, "structuredContent", None) or res.structured_content


@pytest.fixture(autouse=True)
def clear_caches():
    vision_cache.clear()
    search_cache.clear()
    server.mcp._http_client = object()
    yield


@pytest.mark.asyncio
async def test_transform_crop_rotate_resize():
    res = await server.transform_image(
        image=png_b64(size=(200, 100)), crop=[0, 0, 500, 500], rotate=90, width=40, format="jpeg",
    )
    data = structured(res)
    # crop -> 100x50, rotate 90 -> 50x100, width 40 -> 40x80
    assert (data["image"]["width"], data["image"]["height"]) == (40, 80)
    assert data["image"]["mime"] == "image/jpeg"
    assert data["applied"][0].startswith("crop")


@pytest.mark.asyncio
async def test_transform_upscale_and_limits():
    res = await server.transform_image(image=png_b64(size=(10, 10)), upscale=4)
    assert structured(res)["image"]["width"] == 40
    with pytest.raises(ValueError):
        await server.transform_image(image=png_b64(), crop=[10, 10, 10, 10])


@pytest.mark.asyncio
async def test_transform_remove_bg_without_rembg_is_clear(monkeypatch):
    import builtins
    real_import = builtins.__import__

    def fake_import(name, *a, **k):
        if name == "rembg":
            raise ImportError("no rembg")
        return real_import(name, *a, **k)
    monkeypatch.setattr(builtins, "__import__", fake_import)
    with pytest.raises(RuntimeError, match="rembg"):
        await server.transform_image(image=png_b64(), remove_background=True)


@pytest.mark.asyncio
async def test_vision_cache_hits(monkeypatch):
    calls = []

    async def fake_vision(**kw):
        calls.append(kw)
        return "结果"
    monkeypatch.setattr(providers, "execute_vision_completion", fake_vision)
    img = png_b64()
    await server.describe_image(image=img, prompt="p", preset="standard")
    await server.describe_image(image=img, prompt="p", preset="standard")
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_document_grounding_adds_ocr_text(monkeypatch):
    seen = {}

    async def fake_vision(**kw):
        seen["prompt"] = kw["prompt"]
        return "| a | b |"

    async def fake_ocr(_):
        return "合计 1,234.56"
    monkeypatch.setattr(providers, "execute_vision_completion", fake_vision)
    monkeypatch.setattr(vision_tools, "_local_ocr_text", fake_ocr)
    await server.describe_image(image=png_b64(), preset="document")
    assert "合计 1,234.56" in seen["prompt"]


class FakeResp:
    def __init__(self, data, status=200):
        self._d, self.status_code, self.text = data, status, str(data)

    def json(self):
        return self._d


class FakeClient:
    def __init__(self, data):
        self.data, self.params = data, None

    async def get(self, url, params=None, **kw):
        self.params = params
        return FakeResp(self.data)


@pytest.mark.asyncio
async def test_reverse_search_lens(monkeypatch):
    server.config_mgr._config["search"]["serpapi_key"] = "serp-test-key"
    fake = FakeClient({"visual_matches": [
        {"title": "同款", "link": "https://shop.example/p", "source": "Shop", "image": "https://img.example/a.jpg",
         "thumbnail": "https://img.example/t.jpg", "image_width": 800, "image_height": 600},
    ]})
    server.mcp._http_client = fake

    async def ok(_):
        return None
    monkeypatch.setattr(server.security, "validate_url_safety", ok)
    try:
        res = await server.search_images(image="https://img.example/q.jpg")
    finally:
        server.config_mgr._config["search"]["serpapi_key"] = ""
    data = structured(res)
    assert data["mode"] == "reverse_lens"
    assert data["results"][0]["page_url"] == "https://shop.example/p"
    assert fake.params["engine"] == "google_lens" and fake.params["url"] == "https://img.example/q.jpg"


@pytest.mark.asyncio
async def test_search_cache(monkeypatch):
    calls = []

    async def fake_search(client, q, size, ar, limit):
        calls.append(q)
        return [{"title": "t", "image_url": "https://x/1.jpg", "source": "s"}]
    monkeypatch.setattr(search_tools, "_search_by_text", fake_search)
    await server.search_images(query="猫")
    await server.search_images(query="猫")
    assert calls == ["猫"]
