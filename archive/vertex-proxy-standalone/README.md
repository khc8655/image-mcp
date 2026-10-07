# 🚀 Vertex AI OpenAI-Compatible Standalone Proxy

这是一个**完全独立、自包含、开箱即用**的 Google Vertex AI 转 OpenAI 兼容接口代理服务。

您可以直接将本目录复制到任何独立项目、云服务器、容器或微服务中直接运行。

---

## 🌟 核心特性与解决的痛点

1. **解决 Google `thought_signature` 阻断痛点（无状态双向透传）**：
   * Gemini 2.5 / 3.x 在开启思考模式时，函数调用会生成加密签名 `thought_signature`。在多轮对话中，Google 强制要求客户端带回该签名。
   * 本代理通过在 `tool_call_id` 中嵌入 `:::` 签名，实现了标准 OpenAI 客户端无需任何修改即可完美通过 Google 签名校验。
2. **推理思考档位自动转换 (`reasoning_effort` -> `thinking_config`)**：
   * 自动剥离顶层非标的 `reasoning_effort` 参数，并智能映射为 Google 官方标准的 `extra_body.google.thinking_config`，兼容 Gemini 2.5 开关模式与 Gemini 3.6 档位模式。
3. **参数兼容与清洗**：
   * 自动将新版 SDK 的 `max_completion_tokens` 重映射为 `max_tokens`。
   * 自动补齐模型名称的 `google/` 前缀。
   * 自动对齐 Assistant 消息内容（有工具调用时重置为 `None`）与 Arguments JSON 格式化。
4. **内存与连接池调优**：
   * 采用固定容量 `LRUCache(2000)`，杜绝长期运行导致的内存泄漏 (OOM)。
   * 配置 `httpx` 连接池 `Limits(max_keepalive=50, max_connections=200)`，并针对流式 SSE 数据包拆包进行了自动签名恢复。
5. **双轨鉴权**：
   * 支持使用私密 `VERTEX_API_KEY`，也支持在 GCP Compute Engine / GKE 环境下自动从元数据服务器获取并刷新 ADC Access Token。
6. **可选 MiniMax 分流**：
   * 支持通过配置 `MINIMAX_API_KEY`，将 `minimax/*` 请求自动分流至 MiniMax 官方中转。

---

## 📦 快速部署与运行

### 1. 安装依赖

需要 Python 3.10+ 环境：

```bash
cd vertex-proxy-standalone
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 2. 配置环境变量

复制配置文件模板：

```bash
cp .env.example .env
```

编辑 `.env`：

```ini
VERTEX_PROJECT=your-gcp-project-id
VERTEX_LOCATION=global
VERTEX_API_KEY=AQ.Ab8RN6L7_...

# 监听端口
PORT=3002
HOST=0.0.0.0
```

### 3. 启动服务

```bash
python server.py
```

或者使用 uvicorn 生产模式多线程启动：

```bash
uvicorn server:app --host 0.0.0.0 --port 3002 --workers 2
```

---

## 🧪 客户端调用与测试

### 1. 流式文本与思考测试 (curl)

```bash
curl -N http://127.0.0.1:3002/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "google/gemini-3.6-flash",
    "messages": [{"role": "user", "content": "你好，用一句话介绍你自己"}],
    "stream": true
  }'
```

### 2. 深度思考模式 (Thinking Mode)

```bash
curl -N http://127.0.0.1:3002/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "google/gemini-3.6-flash",
    "messages": [{"role": "user", "content": "9.11 和 9.9 谁大？请详细推理"}],
    "reasoning_effort": "high",
    "stream": true
  }'
```

---

## 🛡️ 反向代理配置建议 (Caddy / Nginx)

为了保障万字超长思考流不会被中间代理掐断，请务必在反向代理中**禁用缓冲**并**调大超时**：

### Caddyfile 配置范例：
```caddyfile
your-domain.com {
    reverse_proxy 127.0.0.1:3002 {
        flush_interval -1
        header_down Cache-Control "no-cache, no-transform"
        header_down X-Accel-Buffering "no"
    }
}
```

### Nginx 配置范例：
```nginx
location / {
    proxy_pass http://127.0.0.1:3002;
    proxy_http_version 1.1;
    proxy_set_header Connection "";
    proxy_buffering off;
    proxy_cache off;
    proxy_read_timeout 300s;
}
```
