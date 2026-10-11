import base64
import io
import os
import sys
import tempfile
from pathlib import Path

import pytest

_TMP = tempfile.mkdtemp(prefix="image-mcp-test-")
os.environ["IMAGE_MCP_CONFIG"] = str(Path(_TMP) / "config.json")
os.environ["IMAGE_MCP_DATA_DIR"] = str(Path(_TMP) / "data")
os.environ["CLIENT_AUTH_TOKEN"] = "client-token-123456"
os.environ["ADMIN_TOKEN"] = "admin-token-abcdef"
os.environ["PUBLIC_BASE_URL"] = "http://testserver"
os.environ.pop("IMAGE_MCP_ALLOW_LOCAL_PATHS", None)

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import server  # noqa: E402
from config_manager import config_mgr  # noqa: E402


def png_bytes(size=(64, 48), color=(200, 30, 30)) -> bytes:
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, format="PNG")
    return buf.getvalue()


def png_b64(**kw) -> str:
    return base64.b64encode(png_bytes(**kw)).decode()


@pytest.fixture(autouse=True)
def base_config():
    cfg = config_mgr._config
    cfg["generation"]["models"] = [
        {"provider": "openai", "model": "gpt-image-1", "enabled": True},
        {"provider": "google", "model": "gemini-3.1-flash-image", "enabled": True},
    ]
    cfg["generation"]["default_scene"] = "none"
    cfg["generation"]["default_aspect_ratio"] = "16:9"
    cfg["generation"]["return_mode"] = "url"
    cfg["vision"]["models"] = [{"provider": "google", "model": "gemini-2.5-flash", "enabled": True}]
    cfg["auth"]["client_tokens"] = []
    yield


class FakeUpstream:
    def __init__(self):
        self.calls = []
        self.fail = set()

    async def generate(self, **kw):
        self.calls.append(kw)
        if kw["provider_id"] in self.fail:
            raise RuntimeError("upstream 500 key=sk-SECRETSECRETSECRET123")
        return [{"b64": png_b64(), "mime": "image/png"}]


@pytest.fixture
def upstream(monkeypatch):
    fake = FakeUpstream()
    monkeypatch.setattr(server, "execute_image_generation", fake.generate)
    server.mcp._http_client = object()
    return fake
