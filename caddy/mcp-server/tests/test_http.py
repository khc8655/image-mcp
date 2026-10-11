import json

import pytest
from starlette.testclient import TestClient

import server
from conftest import png_b64

HDR = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
AUTH = {**HDR, "Authorization": "Bearer client-token-123456"}


def rpc(client, method, params=None, id_=1, headers=AUTH):
    body = {"jsonrpc": "2.0", "id": id_, "method": method, "params": params or {}}
    r = client.post("/mcp/", json=body, headers=headers)
    return r


def parse(r):
    text = r.text
    if "data:" in text:
        text = [l[5:].strip() for l in text.splitlines() if l.startswith("data:")][-1]
    return json.loads(text)


@pytest.fixture
def client():
    with TestClient(server.create_app()) as c:
        yield c


def init(client):
    r = rpc(client, "initialize", {
        "protocolVersion": "2025-06-18", "capabilities": {},
        "clientInfo": {"name": "t", "version": "1"},
    })
    assert r.status_code == 200, r.text
    sid = r.headers.get("mcp-session-id")
    h = dict(AUTH)
    if sid:
        h["Mcp-Session-Id"] = sid
    client.post("/mcp/", json={"jsonrpc": "2.0", "method": "notifications/initialized"}, headers=h)
    return h


def test_mcp_requires_token(client):
    assert rpc(client, "initialize", headers=HDR).status_code == 401
    assert rpc(client, "initialize", headers={**HDR, "Authorization": "Bearer nope"}).status_code == 401


def test_tools_list_has_annotations_and_schema(client):
    h = init(client)
    tools = {t["name"]: t for t in parse(rpc(client, "tools/list", id_=2, headers=h))["result"]["tools"]}
    gen = tools["generate_image"]
    assert gen["annotations"]["readOnlyHint"] is False
    assert "images" in json.dumps(gen["outputSchema"])
    assert tools["describe_image"]["annotations"]["readOnlyHint"] is True
    assert "style" not in gen["inputSchema"]["properties"]


def test_generate_then_download_signed(client, upstream):
    h = init(client)
    out = parse(rpc(client, "tools/call", {"name": "generate_image", "arguments": {"prompt": "x"}}, id_=3, headers=h))
    url = out["result"]["structuredContent"]["images"][0]["url"]
    path = url.replace("http://testserver", "")
    assert client.get(path).status_code == 200
    assert client.get(path.replace("sig=", "sig=0")).status_code == 403
    assert client.get("/static/generated/x.png").status_code == 404


def test_admin_api_rejects_client_token(client):
    r = client.get("/api/config", headers={"Authorization": "Bearer client-token-123456"})
    assert r.status_code == 401
    r = client.get("/api/config", headers={"Authorization": "Bearer admin-token-abcdef"})
    assert r.status_code == 200
