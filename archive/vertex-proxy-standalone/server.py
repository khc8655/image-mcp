#!/usr/bin/env python3
"""
Standalone Vertex AI OpenAI-Compatible Proxy Server

完全独立的 Google Vertex AI OpenAI 兼容中转代理服务。
提供标准 POST /chat/completions 与 POST /v1/chat/completions 接口。

核心特性：
1. 双轨鉴权：支持静态 VERTEX_API_KEY 与 GCP 元数据服务 ADC (动态 Access Token 缓存刷新)。
2. 推理思考模式转换：自动将 OpenAI 标准参数 reasoning_effort 转换为 Vertex AI 的 extra_body.google.thinking_config。
3. thought_signature 无状态双向编解码：通过 tool_call_id 嵌入 `:::` 签名，完美支持 Gemini 2.5 / 3.x 多轮工具调用。
4. LRU 内存防护：自动防内存泄露缓存 (上限 2000)。
5. 纯流式 SSE 转发与连接池调优：TCP 分包签名自动回查，零缓冲长连接。
6. 可选 MiniMax 分流支持。
"""

import os
import sys
import re
import json
import time
import logging
import asyncio
from pathlib import Path
from collections import OrderedDict
from contextlib import asynccontextmanager

import httpx
import uvicorn
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.responses import JSONResponse, Response, StreamingResponse

# ---------------------------------------------------------------------------
# 简易 .env 加载器
# ---------------------------------------------------------------------------
def load_env(filepath: str) -> None:
    if not os.path.exists(filepath):
        return
    with open(filepath, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
                value = value[1:-1]
            else:
                if " #" in value:
                    value = value[:value.index(" #")].strip()
            os.environ.setdefault(key, value)

ENV_FILE = os.environ.get("ENV_FILE", str(Path(__file__).resolve().parent / ".env"))
load_env(ENV_FILE)

# ---------------------------------------------------------------------------
# 配置与日志
# ---------------------------------------------------------------------------
PROJECT = os.environ.get("VERTEX_PROJECT", "")
LOCATION = os.environ.get("VERTEX_LOCATION", "global")
API_KEY = os.environ.get("VERTEX_API_KEY", "")
MINIMAX_API_KEY = os.environ.get("MINIMAX_API_KEY", "")

PORT = int(os.environ.get("PORT", "3002"))
HOST = os.environ.get("HOST", "127.0.0.1")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("vertex-proxy")

if not PROJECT:
    logger.error("VERTEX_PROJECT 环境变量未设置！")
    sys.exit(1)

# 构建 Google Vertex AI OpenAPI 端点
if LOCATION == "global":
    HOST_PREFIX = "aiplatform.googleapis.com"
else:
    HOST_PREFIX = f"{LOCATION}-aiplatform.googleapis.com"
OPENAI_BASE = f"https://{HOST_PREFIX}/v1/projects/{PROJECT}/locations/{LOCATION}/endpoints/openapi"

# ---------------------------------------------------------------------------
# LRU 缓存与签名管理
# ---------------------------------------------------------------------------
class LRUCache(OrderedDict):
    """固定容量 LRU 缓存，超出上限自动淘汰最旧条目。"""
    def __init__(self, maxsize=2000):
        self.maxsize = maxsize
        super().__init__()

    def __setitem__(self, key, value):
        if key in self:
            self.move_to_end(key)
        super().__setitem__(key, value)
        if len(self) > self.maxsize:
            self.popitem(last=False)

THOUGHT_SIGNATURE_CACHE = LRUCache(2000)
COMPLETION_TO_TOOL_CALLS = LRUCache(2000)
COMPLETION_TO_SIGNATURE = LRUCache(2000)

TOOL_CALL_ID_PATTERN = re.compile(r'"id"\s*:\s*"([^"]+)"')
THOUGHT_SIG_PATTERN = re.compile(r'"thought_signature"\s*:\s*"([^"]+)"')

def update_signature_cache_from_chunk(chunk_str: str):
    all_ids = TOOL_CALL_ID_PATTERN.findall(chunk_str)
    completion_id = None
    tool_call_ids = []
    
    for val in all_ids:
        if val.startswith("chatcmpl-") or val.startswith("req-") or val.startswith("gen-"):
            completion_id = val
        else:
            tool_call_ids.append(val)
            
    sig_match = THOUGHT_SIG_PATTERN.search(chunk_str)
    sig = sig_match.group(1) if sig_match else None
    
    if completion_id:
        if tool_call_ids:
            if completion_id not in COMPLETION_TO_TOOL_CALLS:
                COMPLETION_TO_TOOL_CALLS[completion_id] = set()
            for tc_id in tool_call_ids:
                COMPLETION_TO_TOOL_CALLS[completion_id].add(tc_id)
                if completion_id in COMPLETION_TO_SIGNATURE:
                    signature = COMPLETION_TO_SIGNATURE[completion_id]
                    THOUGHT_SIGNATURE_CACHE[tc_id] = signature
                    logger.debug(f"[Stream-Link] tc_id={tc_id} -> sig={signature[:20]}...")
                    
        if sig:
            COMPLETION_TO_SIGNATURE[completion_id] = sig
            if completion_id in COMPLETION_TO_TOOL_CALLS:
                for tc_id in COMPLETION_TO_TOOL_CALLS[completion_id]:
                    THOUGHT_SIGNATURE_CACHE[tc_id] = sig
                    logger.debug(f"[Stream-Link] tc_id={tc_id} -> sig={sig[:20]}...")
    else:
        if tool_call_ids and sig:
            for tc_id in tool_call_ids:
                THOUGHT_SIGNATURE_CACHE[tc_id] = sig
                logger.debug(f"[Chunk-Direct] tc_id={tc_id} -> sig={sig[:20]}...")

def update_signature_cache_from_json(data: dict):
    if not isinstance(data, dict):
        return
    for choice in data.get("choices", []):
        msg = choice.get("message", {}) or {}
        for tc in msg.get("tool_calls", []):
            tc_id = tc.get("id")
            extra = tc.get("extra_content") or {}
            google = extra.get("google") or {}
            sig = google.get("thought_signature")
            if tc_id and sig:
                THOUGHT_SIGNATURE_CACHE[tc_id] = sig

# ---------------------------------------------------------------------------
# 凭据自适应与 Token 刷新
# ---------------------------------------------------------------------------
_gcp_token_cache: dict = {"token": None, "expires_at": 0.0}

async def get_google_headers(client: httpx.AsyncClient) -> dict:
    headers = {"Content-Type": "application/json"}
    if API_KEY:
        headers["x-goog-api-key"] = API_KEY
        return headers

    now = time.time()
    if _gcp_token_cache["token"] and now < _gcp_token_cache["expires_at"] - 60:
        headers["Authorization"] = f"Bearer {_gcp_token_cache['token']}"
        return headers

    try:
        resp = await client.get(
            "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token",
            headers={"Metadata-Flavor": "Google"},
            timeout=2.0
        )
        if resp.status_code == 200:
            token_data = resp.json()
            access_token = token_data.get("access_token")
            expires_in = token_data.get("expires_in", 3600)
            if access_token:
                _gcp_token_cache["token"] = access_token
                _gcp_token_cache["expires_at"] = now + expires_in
                headers["Authorization"] = f"Bearer {access_token}"
                return headers
    except Exception as e:
        logger.warning(f"GCP Metadata Server 获取 Token 失败: {e}")

    raise RuntimeError("Vertex AI 鉴权失败：既未配置 VERTEX_API_KEY，也未能从元数据服务器获取 ADC Token。")

# ---------------------------------------------------------------------------
# 消息清洗与规范化工具
# ---------------------------------------------------------------------------
def flatten_content(content) -> str:
    if isinstance(content, list):
        text_parts = []
        for part in content:
            if isinstance(part, dict):
                if part.get("type") == "text" and "text" in part:
                    text_parts.append(part["text"])
                elif "text" in part:
                    text_parts.append(part["text"])
            elif isinstance(part, str):
                text_parts.append(part)
        return "\n".join(text_parts)
    elif isinstance(content, dict):
        if content.get("type") == "text" and "text" in content:
            return content["text"]
        return json.dumps(content)
    return content if isinstance(content, str) else ""

def sanitize_messages_for_tool_calls(messages: list) -> list:
    tool_call_ids_called = set()
    tool_call_ids_responded = set()

    for msg in messages:
        if not isinstance(msg, dict):
            continue
        if msg.get("role") == "assistant" and "tool_calls" in msg and isinstance(msg["tool_calls"], list):
            for tc in msg["tool_calls"]:
                if isinstance(tc, dict) and tc.get("id"):
                    tool_call_ids_called.add(tc["id"])
        elif msg.get("role") == "tool" and msg.get("tool_call_id"):
            tool_call_ids_responded.add(msg["tool_call_id"])

    orphaned_ids = tool_call_ids_called - tool_call_ids_responded
    if not orphaned_ids:
        return messages

    sanitized = []
    for msg in messages:
        sanitized.append(msg)
        if isinstance(msg, dict) and msg.get("role") == "assistant" and "tool_calls" in msg:
            for tc in msg.get("tool_calls", []):
                tc_id = tc.get("id") if isinstance(tc, dict) else None
                if tc_id and tc_id in orphaned_ids:
                    sanitized.append({
                        "role": "tool",
                        "tool_call_id": tc_id,
                        "name": "placeholder",
                        "content": "[Result unavailable - pruned by client]"
                    })
                    logger.warning(f"[Sanitize] 补齐孤儿 Tool Call 回复: {tc_id}")
    return sanitized

def supports_reasoning(model: str) -> bool:
    m = str(model or "").replace("google/", "").lower().strip()
    return bool(re.match(r"^gemini-(2\.5|[3-9])", m))

def build_google_thinking_config(model: str, effort_str: str) -> dict | None:
    m = str(model or "").replace("google/", "").lower().strip()
    if not m.startswith("gemini"):
        return None

    if effort_str == "none":
        return {"include_thoughts": False}

    thinking_config = {"include_thoughts": True}
    if m.startswith("gemini-2.5-"):
        return thinking_config

    if effort_str not in {"minimal", "low", "medium", "high", "xhigh"}:
        effort_str = "medium"

    if m.startswith(("gemini-3", "gemini-3.1", "gemini-3.5", "gemini-3.6")):
        if "flash" in m:
            if effort_str in {"minimal", "low"}:
                thinking_config["thinking_level"] = "low"
            elif effort_str in {"high", "xhigh"}:
                thinking_config["thinking_level"] = "high"
            else:
                thinking_config["thinking_level"] = "medium"
        elif "pro" in m:
            thinking_config["thinking_level"] = "high" if effort_str in {"high", "xhigh"} else "low"

    return thinking_config

# ---------------------------------------------------------------------------
# MiniMax 分流适配 (可选)
# ---------------------------------------------------------------------------
async def handle_minimax_chat_completion(request, body: dict) -> Response:
    if not MINIMAX_API_KEY:
        return JSONResponse({"error": {"message": "MINIMAX_API_KEY 未配置", "type": "config_error"}}, status_code=500)

    model_name = body.get("model", "").replace("minimax/", "") or "minimax-m3"
    minimax_payload = {**body, "model": model_name}
    minimax_payload.pop("extra_body", None)
    minimax_payload.pop("reasoning_effort", None)

    minimax_endpoint = "https://api.minimax.chat/v1/text/chatcompletion_v2"
    headers = {
        "Authorization": f"Bearer {MINIMAX_API_KEY}",
        "Content-Type": "application/json"
    }

    client: httpx.AsyncClient = request.app.state.http_client
    is_stream = minimax_payload.get("stream", False)

    try:
        if is_stream:
            req = client.build_request("POST", minimax_endpoint, headers=headers, json=minimax_payload)
            resp = await client.send(req, stream=True)
            if resp.status_code != 200:
                err = await resp.aread()
                await resp.aclose()
                return Response(content=err, status_code=resp.status_code, headers={"Content-Type": "application/json"})

            async def generate():
                try:
                    async for chunk in resp.aiter_bytes():
                        yield chunk
                finally:
                    await resp.aclose()
            return StreamingResponse(generate(), media_type="text/event-stream")
        else:
            resp = await client.post(minimax_endpoint, headers=headers, json=minimax_payload, timeout=120.0)
            return Response(content=resp.content, status_code=resp.status_code, headers={"Content-Type": "application/json"})
    except Exception as e:
        logger.error(f"MiniMax 转发失败: {e}")
        return JSONResponse({"error": {"message": f"MiniMax 转发错误: {str(e)}", "type": "proxy_error"}}, status_code=502)

# ---------------------------------------------------------------------------
# 核心 Chat Completions 端点
# ---------------------------------------------------------------------------
async def chat_completions(request) -> Response:
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": {"message": "无效的 JSON 请求体", "type": "proxy_error"}}, status_code=400)

    # 1. MiniMax 模型直接分流
    req_model = body.get("model", "")
    if req_model.startswith("minimax/") or req_model in ("minimax-m3", "minimax-m3-pro", "abab6.5g-chat"):
        return await handle_minimax_chat_completion(request, body)

    # 2. 状态机与签名无状态解码
    for msg in body.get("messages", []):
        if not isinstance(msg, dict):
            continue

        # 2.1 Assistant 工具调用签名恢复
        if msg.get("role") == "assistant" and "tool_calls" in msg and isinstance(msg["tool_calls"], list):
            for tc in msg["tool_calls"]:
                if not isinstance(tc, dict):
                    continue
                tc_id = tc.get("id")
                if tc_id and ":::" in tc_id:
                    parts = tc_id.split(":::")
                    real_id = parts[0]
                    sig = ":::".join(parts[1:])
                    tc["id"] = real_id
                    extra = tc.setdefault("extra_content", {})
                    google = extra.setdefault("google", {})
                    google["thought_signature"] = sig
                    logger.debug(f"[Stateless-Decode] 还原签名: tool_call_id {real_id}")
                
                # 缓存回查兜底
                tc_id = tc.get("id")
                if tc_id:
                    extra = tc.setdefault("extra_content", {})
                    google = extra.setdefault("google", {})
                    if "thought_signature" not in google:
                        if tc_id in THOUGHT_SIGNATURE_CACHE:
                            google["thought_signature"] = THOUGHT_SIGNATURE_CACHE[tc_id]
                            logger.debug(f"[Cache-Fallback] 补全签名: {tc_id}")
                        else:
                            google["thought_signature"] = "skip_thought_signature_validator"

        # 2.2 Tool 消息工具 ID 恢复
        if msg.get("role") == "tool" and msg.get("tool_call_id") and ":::" in msg["tool_call_id"]:
            parts = msg["tool_call_id"].split(":::")
            msg["tool_call_id"] = parts[0]

        # 2.3 Assistant / Tool 消息内容规范化
        if msg.get("role") == "assistant":
            if "tool_calls" in msg and msg.get("tool_calls"):
                msg["content"] = None
            else:
                content = msg.get("content")
                if isinstance(content, (list, dict)):
                    msg["content"] = flatten_content(content)

            for tc in msg.get("tool_calls", []) or []:
                if isinstance(tc, dict):
                    func = tc.get("function")
                    if isinstance(func, dict) and isinstance(func.get("arguments"), dict):
                        func["arguments"] = json.dumps(func["arguments"], ensure_ascii=False)

        if msg.get("role") == "tool":
            content = msg.get("content")
            if isinstance(content, (list, dict)):
                msg["content"] = flatten_content(content)

        # 2.4 清理非标扩展字段
        ALLOWED_MSG_KEYS = {"role", "content", "tool_calls", "tool_call_id", "name", "extra_content"}
        extra_keys = [k for k in msg.keys() if k not in ALLOWED_MSG_KEYS]
        for k in extra_keys:
            msg.pop(k, None)

    if "messages" in body and isinstance(body["messages"], list):
        body["messages"] = sanitize_messages_for_tool_calls(body["messages"])

    # 3. 自动补齐 google/ 前缀
    model = body.get("model", "")
    if model and not model.startswith("google/"):
        model = "google/" + model
        body["model"] = model

    # 4. reasoning_effort 推理档位参数清洗
    if "reasoning_effort" in body:
        effort_str = str(body["reasoning_effort"]).lower().strip()
        if supports_reasoning(model):
            thinking_config = build_google_thinking_config(model, effort_str)
            if thinking_config:
                extra_body = body.setdefault("extra_body", {})
                google_extra = extra_body.setdefault("google", {})
                google_extra["thinking_config"] = thinking_config
        del body["reasoning_effort"]

    # 5. max_completion_tokens 参数别名映射
    if "max_completion_tokens" in body and "max_tokens" not in body:
        body["max_tokens"] = body["max_completion_tokens"]
        del body["max_completion_tokens"]

    client: httpx.AsyncClient = request.app.state.http_client
    url = f"{OPENAI_BASE}/chat/completions"

    try:
        auth_headers = await get_google_headers(client)

        # -------------------------------------------------------------------
        # 流式请求处理 (SSE)
        # -------------------------------------------------------------------
        if body.get("stream", False):
            req = client.build_request("POST", url, json=body, headers=auth_headers)
            resp = await client.send(req, stream=True)
            if resp.status_code != 200:
                err_content = await resp.aread()
                await resp.aclose()
                logger.error(f"Upstream 流式错误 {resp.status_code}: {err_content[:500]}")
                return Response(
                    content=err_content,
                    status_code=resp.status_code,
                    headers={"Content-Type": resp.headers.get("Content-Type", "application/json")}
                )

            async def stream_generator():
                try:
                    async for chunk in resp.aiter_bytes():
                        try:
                            chunk_str = chunk.decode("utf-8", errors="ignore")
                            update_signature_cache_from_chunk(chunk_str)

                            # 编码签名到 tool_call_id
                            id_match = TOOL_CALL_ID_PATTERN.search(chunk_str)
                            sig_match = THOUGHT_SIG_PATTERN.search(chunk_str)
                            if id_match:
                                original_id = id_match.group(1)
                                sig = sig_match.group(1) if sig_match else THOUGHT_SIGNATURE_CACHE.get(original_id)
                                if original_id and sig and ":::" not in original_id and sig != "skip_thought_signature_validator":
                                    new_id = f"{original_id}:::{sig}"
                                    chunk_str = re.sub(r'"id"\s*:\s*"' + re.escape(original_id) + r'"', f'"id":"{new_id}"', chunk_str)
                                    chunk = chunk_str.encode("utf-8")
                                    logger.debug(f"[Stateless-Encode] 编码签名: {new_id[:40]}...")
                        except Exception as e:
                            logger.error(f"检查流式数据签名异常: {e}")
                        yield chunk
                finally:
                    await resp.aclose()

            return StreamingResponse(
                stream_generator(),
                media_type="text/event-stream",
                headers={
                    "Cache-Control": "no-cache, no-transform",
                    "X-Accel-Buffering": "no"
                }
            )

        # -------------------------------------------------------------------
        # 非流式请求处理
        # -------------------------------------------------------------------
        else:
            resp = None
            for attempt in range(2):
                try:
                    resp = await client.post(url, json=body, headers=auth_headers, timeout=120.0)
                    if resp.status_code < 500:
                        break
                    if attempt == 0:
                        await asyncio.sleep(1)
                except Exception as e:
                    if attempt == 0:
                        await asyncio.sleep(1)
                        continue
                    raise e

            if resp and resp.status_code >= 500:
                logger.error(f"Upstream 非流式重试耗尽仍然报错: {resp.status_code}")
                return Response(
                    content=resp.content,
                    status_code=resp.status_code,
                    headers={"Content-Type": resp.headers.get("Content-Type", "application/json")}
                )

            try:
                if resp and resp.status_code == 200:
                    resp_data = resp.json()
                    update_signature_cache_from_json(resp_data)

                    modified = False
                    for choice in resp_data.get("choices", []):
                        msg = choice.get("message", {}) or {}
                        for tc in msg.get("tool_calls", []):
                            tc_id = tc.get("id")
                            extra = tc.get("extra_content") or {}
                            google = extra.get("google") or {}
                            sig = google.get("thought_signature")
                            if tc_id and sig:
                                tc["id"] = f"{tc_id}:::{sig}"
                                modified = True
                    if modified:
                        content_bytes = json.dumps(resp_data).encode("utf-8")
                        return Response(
                            content=content_bytes,
                            status_code=resp.status_code,
                            headers={"Content-Type": "application/json"}
                        )
            except Exception as e:
                logger.error(f"编码非流式签名异常: {e}")

            return Response(
                content=resp.content,
                status_code=resp.status_code,
                headers={"Content-Type": resp.headers.get("Content-Type", "application/json")}
            )

    except Exception as e:
        logger.error(f"Upstream 请求失败: {e}")
        return JSONResponse({"error": {"message": f"代理异常: {str(e)}", "type": "proxy_error"}}, status_code=502)

# ---------------------------------------------------------------------------
# 应用生命周期与路由注册
# ---------------------------------------------------------------------------
@asynccontextmanager
async def app_lifespan(app):
    limits = httpx.Limits(max_keepalive_connections=50, max_connections=200)
    timeout = httpx.Timeout(connect=10.0, read=120.0, write=30.0, pool=10.0)
    async with httpx.AsyncClient(limits=limits, timeout=timeout) as client:
        app.state.http_client = client
        logger.info("HTTP 连接池已启动")
        yield
    logger.info("HTTP 连接池已释放")

routes = [
    Route("/chat/completions", endpoint=chat_completions, methods=["POST"]),
    Route("/v1/chat/completions", endpoint=chat_completions, methods=["POST"]),
]

app = Starlette(lifespan=app_lifespan, routes=routes)

if __name__ == "__main__":
    logger.info(f"Vertex AI Proxy 启动中: host={HOST}, port={PORT}, project={PROJECT}, location={LOCATION}")
    uvicorn.run(app, host=HOST, port=PORT, proxy_headers=True, forwarded_allow_ips="*")
