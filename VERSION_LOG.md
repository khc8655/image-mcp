# 📌 Vertex AI Unified Proxy - Git & 版本维护说明 (VERSION_LOG.md)

为了确保后续维护中 **Oracle Cloud (OCI) VM、Cloudflare Worker** 两端代码不再发生逻辑混乱，本项目核心文件以虚拟机（OCI VM）上的运行版本为准则，**本地仓库是唯一修改源头**（详见下方"关键维护规范"）。

本文档详细梳理了当前项目的架构设计、各端功能定位、版本更新历史以及未来维护规则。

---

## 🗺️ 架构设计与各端功能定位

本项目围绕 Vertex AI 提供 OpenAI 兼容代理，当前部署了两个版本：

```mermaid
graph TD
    subgraph VM [Oracle Cloud (OCI) 虚拟机环境]
        Caddy[Caddy 443/HTTPS] -->|反代 /mcp 与 /v1/*| PyUnified[server.py 3002/Starlette]
    end

    subgraph Serverless [无服务器环境]
        CFWorker[Cloudflare Worker / src/index.js]
    end

    PyUnified -->|/openapi| GoogleVertex[Google Vertex AI]
    CFWorker -->|/openapi| GoogleVertex
```

### 1. Oracle Cloud (OCI) VM 版本 (以其为绝对准则 🏆)
VM 上运行着一套统一的中转服务，由 Caddy 对外暴露（公网 `443` 端口，源站 `oc.866155.xyz`，前端由腾讯云 EdgeOne 加速）：
*   **统一 MCP + 文本服务 (`caddy/mcp-server/server.py`)**：
    *   **运行端口**：`3002` (通过 Starlette ASGI 框架运行)
    *   **职责**：处理 `/mcp` 接口的多模态图片生成工具调用，以及 `/v1/chat/completions` 等文本模型请求（负责推理模型 `reasoning_effort` 参数清洗、`max_completion_tokens` 重映射及 `thought_signature` 透传）。
*   **统一 Caddy 服务 (`caddy/Caddyfile`)**：
    *   **运行端口**：公网 `443`
    *   **职责**：负责公网客户端身份认证（Bearer Token）、OPTIONS 跨域处理，并将 `/mcp` 与 `/v1/*` 反向代理至 `3002`。

### 2. Cloudflare Worker 版本 (`src/index.js`)
*   **职责**：将统一 MCP + 文本服务移植到 Cloudflare 边缘端运行，供特定的边缘调用客户端使用。其工具调用逻辑与 VM 的 `server.py` 保持一致，但采用 Javascript ES 模块编写，自托管了鉴权与 CORS。

### 3. AWS Lambda 版本 (`lambda/`) — ⚠️ 已移除
*   历史上曾存在一个**极简的透传代理**（`lambda/index.js`），目前已从仓库中删除，仅保留在 Git 历史中。

---

## 🔄 文件一致性维护说明

> **说明**：历史版本中的 MD5 一致性校验表已移除（其中部分文件已删除或废弃，校验码无法再验证）。
> 当前维护规则：**以本地 `caddy/` 为唯一修改源头**，修改后同步上传至 VM 的 `/home/jjb/caddy/`（见下方"关键维护规范"）。

---

## 📜 历史更新说明 (Git Commit History)

以下是本项目各阶段更新的里程碑历史记录：

*   **`2026-06-25` feat(vm+cf): 支持 Agnes 文本模型智能中转与自动重试**
    *   **拦截与纯透传中转**：在 GCP VM (`server.py`) 和 Cloudflare Worker (`src/index.js`) 的 `/chat/completions` 分发入口增加对 Agnes 文本模型（如 `agnes-2.0-flash`）的提早拦截，支持智能剥离 `google/` 或 `agnes/` 等厂商前缀，实现纯透传中转。
    *   **上游 3 次自动重试**：Agnes AI 上游接口在并发或密集请求时极易返回 500/503 (OpenAIException / do_request_failed)。在此版本中，我们在两端中转层中均加入了 3 次自动重试（每次间隔 1 秒）机制，以极大地提升客户端调用的稳定性。
    *   **验证完全通过**：对 VM 和 Cloudflare Worker 的流式及非流式调用均已通过真实接口压测，稳定可用。

*   **`2026-06-24` feat(lambda): 重构为纯非流式版 + 补齐思考等级与 Token 自动映射**
    *   **彻底禁用流式响应**：将 Lambda 处理器重构为标准 HTTP 异步处理器，不再使用 `streamifyResponse`，并将所有 completions 请求强制指定为 `stream: false`，返回标准 HTTP 响应，解决其它 Agent 无法解析流式分块的兼容性痛点。
    *   **CORS 预检修复**：修正 `OPTIONS` 处理器的响应状态码为 `200` 并写入空体，解决了 204 被 Lambda 转换导致丢失跨域许可头（CORS Headers）的致命缺陷。
    *   **思考等级自适应与对齐**：当检测到 `reasoning_effort` 时进行标准档位归一化（如 `minimal` -> `low` 等），不进行额外的模型支持拦截与过滤，完全作为轻量中转，保证本版本的极致性能与稳定。
    *   **Token 自动映射**：自动转换新版 SDK 的 `max_completion_tokens` 为 `max_tokens`。

*   **`2026-08-14` feat(server+caddy): EdgeOne 加速、443 端口、MCP 2.0.0 与全量 server.py 重构调优 🏆**
    *   **腾讯云 EdgeOne 节点加速 (`oc.khc6.cn`)**：配置 Caddy 443 端口与 15 年 Cloudflare Origin CA 证书，回源首包延迟由 334ms 降至 88ms（最快 9ms）。
    *   **新增 `describe_image` (视能分析/OCR 识别) MCP 工具**：主选 `google/gemini-3.6-flash`，遇 429 自动无缝降级平滑切至 `minimax/minimax-m3`。
    *   **P0 - 内存泄漏根治**：将无上限全局字典 `COMPLETION_TO_*` 升级为 `LRUCache(2000)`，实现固定容量自动淘汰。
    *   **P0 - 非流式 5xx 重试修复**：循环结束增加状态码 Guard，当重试耗尽且 >= 500 时直接透传原始错误响应，避免盲目 JSON 解析崩溃。
    *   **P1 - 流式 Chunk 拆包签名恢复**：修复 TCP 分包时 `id` 与 `thought_signature` 分落不同 SSE Chunk 导致签名丢失的问题，实现缓存自动回查补全。
    *   **P1 - 超时压缩与连接池调优**：`httpx` 连接池配置 `Limits(max_keepalive=50, max_connections=200)`；图片工具自动回退超时压缩至 45s。
    *   **P2 - SSRF DNS Rebinding 防御**：引入 `asyncio` 异步 DNS 查询全量校验真实 IP 列表，防御 TTL=0 域名重绑定绕过。
    *   **P2 - 重构与代码瘦身**：提取 `to_mcp_images()` 与 `flatten_content()` 消除 9 处重复代码；将高频流式签名日志降级为 `debug`。

*   **`2026-06-24` feat(minimax): 启用 prompt_optimizer + 审查 MiniMax/SenseNova 文档**
    *   阅读 MiniMax 官方文档（文生图/图生图）及 SenseNova-U1 README，对比当前实现。
    *   **[VM+Worker] MiniMax 文生图加 `prompt_optimizer: true`**：MiniMax 官方 API 支持此参数，开启后平台自动对提示词进行优化，免费提升出图质量，VM 和 Worker 均已同步开启（`server.py` L601、`src/index.js` L947）。
    *   **[VM+Worker] MiniMax 图生图加 `prompt_optimizer: true`**：图生图端点同样支持，同步开启（`server.py` L635、`src/index.js` L984）。
    *   **[VM+Worker] 注释 `subject_reference.type` 可选值**：标注 `character`（人物一致性）和 `style`（风格迁移）两种模式，方便后续扩展。
    *   **[结论] SenseNova 无需改动**：README 中的 `--negative-prompt`、`--num-steps` 等是本地 CLI 推理参数，非商业 API 参数；商业 API 仅接受标准 `model/prompt/n/size`，当前实现已正确。

*   **`2026-06-24` fix(worker+vm): 5-point optimization & bug fix pass**
    *   **[Worker/Bug] `normalizeModel` 前缀剥离 Bug**：修复 `agnes-`/`sensenova-`/`minimax-` 短横线前缀处理错误，之前会生成 `agnes/agnes-xxx` 而非正确的 `agnes/xxx`（`src/index.js` L1151-1153）。
    *   **[Worker/Bug] `/v1/images/generations` API 路径修复**：原实现走 `chat/completions` 路径并从过时的 `audio.data` 字段解析图片，现改为与 MCP 工具一致的 native `generateContent` API，从 `candidates[].inlineData.data` 提取（`src/index.js` L261-345）。
    *   **[Worker/Quality] `SIZE_TO_ASPECT` 提到顶层**：消除 `callGoogleGenerateImage`、`callMinimaxGenerateImage`、`handleImageGenerations` 三处重复定义（`src/index.js` L44-53）。
    *   **[Worker/Quality] `_cache` 与 env 绑定**：从单一全局 `null` 改为 `Map`，以 `VERTEX_PROJECT|TEXT_MODELS` 为 key，防止多 env/多租户场景下跨请求缓存污染（`src/index.js` L73-92）。
    *   **[VM/Quality] 流式响应错误记录日志**：`chat_completions` 的 `stream_generator` 非 200 响应现在会调用 `logger.error()` 记录详情，便于排查上游异常（`server.py` L329）。
    *   **SenseNova 模型 ID 确认**：以 `sensenova-u1-fast` 为统一标准（VM `.env` 和 Worker 已一致）。

*   **`aa1edae` refactor: simplify lambda code**
    *   重构了 Lambda (`lambda/index.js`) 代理，使其成为极简透传管道。
    *   添加智能 API Key 和 OAuth 鉴权检测，解决 `API_KEY_SERVICE_BLOCKED`。
    *   位置 `global` 参数在 URL 中自动纠错转换为 `us` 以防止 Google API 报 404/400。
*   **`822220a` fix: prevent duplicate agnes entry**
    *   修复在 VM 端 `/v1/models` 输出时出现重复 `agnes-2.0-flash` 列表项的 Bug。
*   **`1bb5902` security: require auth for health check**
    *   安全加固：强制 `/health` 及根目录 `/` 请求必须携带有效的 `CLIENT_AUTH_TOKEN`，防止公网端口扫描造成敏感数据或模型 white list 信息泄露。
*   **`750f463` fix: handle prefixed/unprefixed agnes models**
    *   允许客户端输入带前缀（`agnes/`）或不带前缀的模型名，在解析过滤中进行归一化。
*   **`0349d93` fix: base64 conversion & error handling**
    *   修复 Node.js 环境下图片下载 base64 转换崩溃的问题，优化了 Vertex API 的上游异常捕获提示。
*   **`a7e3f51` feat: add support for forwarding agnes-2.0-flash**
    *   在 Node.js 服务端增加了对 `agnes-2.0-flash` 文本模型请求的动态拦截与转发功能。
*   **`cb2db18` feat: add cloudrun node/express server**
    *   新增用于容器化部署 of Node.js 镜像，对接 Starlette 应用功能。

---

## ⚠️ 关键维护规范（防乱守则）

为避免未来继续产生版本混乱，后续进行日常更新与部署时，**必须严格遵循以下规则**：

1.  **文件更改的唯一源头**：
    *   修改 **Oracle Cloud (OCI) VM** 的 Python 逻辑时，应该先在本地修改 `caddy/mcp-server/server.py`，再同步上传到虚拟机 `/home/jjb/caddy/mcp-server/server.py`。
    *   **切勿**直接在 VM 上单向修改而不更新本地，避免被后续同步覆盖。
2.  **版本隔离**：
    *   `src/index.js` 是专属于 **Cloudflare Worker** 的 Javascript 运行版本，**切勿**将 Python 版本 `server.py` 的修复与语法直接套入其中。
3.  **VM 部署重启命令**：
    *   每次修改了 VM 的 `/home/jjb/caddy/.env` 或 `/home/jjb/caddy/mcp-server/server.py` 后，必须在虚拟机目录运行重启指令生效：
        ```bash
        cd /home/jjb/caddy && ./manage.sh restart
        ```
    *   每次修改了 `/home/jjb/caddy/Caddyfile` 或 `/home/jjb/caddy/models.json` 后，必须在虚拟机运行重载命令生效：
        ```bash
        cd /home/jjb/caddy && ./manage.sh reload
        ```
