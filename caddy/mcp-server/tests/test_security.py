import pytest

import security
from config_manager import config_mgr


def test_sanitize_error():
    msg = 'bad key=AIzaSyA1234567890abcdefghijklmnop&x=1 Authorization: Bearer abcdefghijklmnop sk-abcdefghijklmnopqrst'
    out = security.sanitize_error(msg)
    assert "AIzaSy" not in out and "abcdefghijklmnop" not in out and "sk-abcdef" not in out


@pytest.mark.asyncio
@pytest.mark.parametrize("url", [
    "http://127.0.0.1/a.png", "http://169.254.169.254/latest", "http://100.64.0.1/x",
    "http://[::ffff:127.0.0.1]/x", "ftp://example.com/a.png", "http://10.0.0.1/a",
])
async def test_ssrf_blocked(url):
    with pytest.raises(ValueError):
        await security.validate_url_safety(url)


def test_signed_path_roundtrip():
    exp, sig = security.sign_path(config_mgr, "20260101/a.png", 3600)
    assert security.verify_signed_path(config_mgr, "20260101/a.png", str(exp), sig)
    assert not security.verify_signed_path(config_mgr, "20260101/b.png", str(exp), sig)
    assert not security.verify_signed_path(config_mgr, "20260101/a.png", "1", sig)


def test_admin_token_conflict_disables_admin(monkeypatch):
    assert config_mgr.verify_token("admin-token-abcdef")
    monkeypatch.setitem(config_mgr._config["auth"], "admin_token", "client-token-123456")
    assert not config_mgr.verify_token("client-token-123456")


def test_limiter():
    lim = security.SlidingWindowLimiter()
    assert all(lim.allow("a", 3)[0] for _ in range(3))
    ok, retry = lim.allow("a", 3)
    assert not ok and retry > 0


def test_safe_dict_masks_tokens():
    config_mgr._config["auth"]["client_tokens"] = [{"name": "c", "token": "abcdefghijklmnop"}]
    safe = config_mgr.get_safe_dict()
    assert "abcdefghijklmnop" not in str(safe)
    assert "signing_secret" not in safe["auth"]
    assert "admin-token-abcdef" not in str(safe)
