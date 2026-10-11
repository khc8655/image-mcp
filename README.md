# KHC Image MCP Suite (多模态图片全生命周期 MCP 工具服务与 Web 控制台)

本项目是一个专为现代 AI Agent（如 WorkBuddy、Cursor、OpenClaw、Hermes、Claude Code 等）量身打造的**多模态图片全生命周期 MCP (Model Context Protocol 2.0.0) 服务**。

> **核心架构设计亮点 (v2.8)**：
> 1. **物理顺序优先级调度与链式故障转移**：生图与识图彻底废弃复杂的猜词黑盒路由，采用透明物理顺序模式（排在第 1 位的为主力模型，超时/故障自动顺延后序备用节点，同时支持 Agent 传参定向调度目标模型）。
> 2. **独立适配器体系与 Key 彻底解耦**：每个模型提供商在 `presets/{provider}.json` 拥有独立的适配规则文件（覆盖 Google Gemini/Imagen、MiniMax、SenseNova、SiliconFlow FLUX、Agnes AI、OpenAI DALL-E 3、智谱 CogView 等）。Key 密钥由 EdgeOne 凭据中枢托管并按需拉取至本地；**Key 随意轮换不改配置，厂商 API 升级只需单文件热更，不触动任何已有密钥**。
> 3. **模型全生命周期可编辑**：挂载支持自主挑选绑定 Key，识图直连 EdgeOne 统一中枢模型库；表格行内即改 Key、行内即改角色偏好；操作列支持编辑模态框修改 Key、角色及**厂商专属高级参数**（提示词优化器、水印、画质、负向词、人像等级）。
> 4. **业务驱动的清晰四大 Web 控制台 Tab**：
>    - **🎨 生图管理**：顺序调度策略、出图超时配置、生图活跃模型池（行内切 Key/角色/数字排序/编辑模态框）；
>    - **👁️ 识图与 OCR**：分级 OCR 等级（Fast/Standard/Document/GUI）、本地 RapidOCR ONNX 极速引擎与多模态模型池主备切换；
>    - **🔍 搜图管理**：全网搜图引擎调度参数与搜图在线测试控制台（Playground 实时图片预览）；
>    - **⚙️ 系统配置**：EdgeOne 凭据中枢（一键全量同步）、本地 Provider 资产仓库、独立 Presets 规则库（**免重启一键热重载**）与安全设置。
> 5. **标准 UI 规范**：内嵌 Web 控制台（`/admin`）遵循 KHC Web Design System，零第三方重量级前端框架依赖。

---

## 🧭 Agent 与开发者接手速览 (Quick Navigation)

任何后续接手本项目的 Agent 或开发者，请按以下索引快速切入开发：

| 关注维度 | 指南文档与入口 | 说明 |
| :--- | :--- | :--- |
| **发版与质量门禁规范** | [👉 `RELEASE_SPEC.md`](./RELEASE_SPEC.md) | **强制发版守则**：四层质量门禁矩阵 (L1~L4)、前后端冒烟测试与发版/回滚 SOP |
| **核心源码与开发** | [👉 `caddy/mcp-server/README.md`](./caddy/mcp-server/README.md) | **最核心必读手册**：包含目录职责表、调度算法、Presets 规范、开发避坑与测试套件 |
| **网关与运维部署** | [👉 `caddy/README.md`](./caddy/README.md) | Caddy 反代配置、端口说明、Azure VM 运维脚本 (`manage.sh`) 与增量同步流程 |
| **单元与集成测试** | [`caddy/mcp-server/tests/`](./caddy/mcp-server/tests/) | pytest，mock 上游，CI 自动运行 |
| **生产集成验证** | [`scripts/live/verify_image_mcp.py`](./caddy/mcp-server/scripts/live/verify_image_mcp.py) | 针对生产环境的全链路回归脚本（需真实 Key，不进 CI） |

---

## 核心架构与数据流图

```text
[EdgeOne 凭据中枢 (api.khc6.cn)]           [本地独立 Presets 适配库 (presets/*.json)]
  ├── GET  /api/vault/manifest               ├── google.json, minimax.json, agnes.json
  └── POST /api/vault/fetch                  └── sensenova.json, siliconflow.json, openai.json...
              │                                      │
              │ (拉取 Key 资产保存至 config.json)      │ (免重启热加载适配规则)
              └──────────────┬───────────────────────┘
                             ▼
            [腾讯云 EdgeOne 边缘加速 (https://oc.khc6.cn)]
                             │ (IPv6 :3000 源站直连)
                             ▼
            [Azure 日本西 VM :3000 Caddy -> :3002 server.py]
      ┌──────────────────────┼──────────────────────┐
      ▼                      ▼                      ▼
【🎨 生图引擎 (顺序+容灾)】   【👁️ 识图与分级 OCR】    【🔍 搜图引擎 (免 Key 弹性)】
• 物理顺序优先级调度      • 极速纯文本 (fast)     • 文字搜图 (Text Search)
• 故障/超时自动链式顺延   • 标准通用 (standard)   • 语义以图搜图 (Reverse Search)
• 支持 Agent 定向指定     • 文档表格 (document)   • DuckDuckGo + Bing + Pexels
• 动态注入厂商高级参数    • 界面坐标定位 (gui)    • 在线测试控制台 (Playground)
```

---

## Web 控制台四大业务 Tab 概览

访问地址：`https://oc.khc6.cn/admin`

| Tab 标签 | 核心功能 | 包含卡片与交互 |
| :--- | :--- | :--- |
| **🎨 生图管理** | 文生图、图生图一站式收敛 | 1. **生图调度策略**（顺序优先级模式说明、出图超时配置，超时主动熔断守护）<br>2. **生图活跃模型池**（增删改、数字直改优先级排序、行内即选切换 Key、行内即改角色）<br>3. **挂载生图模型**（自主挑选绑定 Key、从官方 Presets 纯点选模型）<br>4. **模型编辑模态框**（修改绑定 Key、角色标签、厂商专属高级控制参数） |
| **👁️ 识图与 OCR** | 图像视觉理解一站式收敛 | 1. **识别策略与本地引擎**（顺序优先级模式、Fast/Standard/Document/GUI 预设切换、RapidOCR 开关、响应超时）<br>2. **多模态识图模型池**（增删改、数字直改排序、行内即选切换 Key、行内即改角色）<br>3. **挂载识图模型**（直接对接 EdgeOne 统一中枢模型库，纯点选挂载）<br>4. **模型编辑模态框**（支持全生命周期修改与高级参数配置） |
| **🔍 搜图管理** | 全网高清搜图一站式收敛 | 1. **搜图引擎参数**（DuckDuckGo 免 Key / Bing / Pexels 商用 Key、最大条数、OCR 联动）<br>2. **搜图在线测试控制台 (Playground)**（输入关键词或传图，实时渲染图片卡片、来源与规格） |
| **⚙️ 系统配置** | 共享底层基础设施统一归集 | 1. **EdgeOne 凭据中枢**（中枢 URL/Token 配置、连通测试、**一键全量同步全部凭据**）<br>2. **独立适配器预设库**（**免重启一键热加载**本地 7 大独立 Presets 规则库）<br>3. **系统安全与服务状态**（Admin Token 修改、服务健康状态） |

---

## MCP 核心工具清单 (Model Context Protocol 2.0.0)

向 AI Agent 暴露 7 个工具（均带 annotations 与 outputSchema，返回 structuredContent）：

| 工具名称 | 类别 | 功能说明 | 典型入参 |
| :--- | :--- | :--- | :--- |
| **`generate_image`** | 生图 | 文生图，10 种比例、4 级画质、5 个办公场景模板；返回签名链接 + 缩略图 | `prompt`, `aspect_ratio`, `resolution`, `scene`, `negative_prompt`, `seed`, `count`, `model` |
| **`edit_image`** | 生图 | AI 改图、风格重绘、mask 局部重绘 | `image`, `prompt`, `mask`, `aspect_ratio`, `count`, `model` |
| **`transform_image`** | 修图 | 本地裁剪/旋转/翻转/缩放/放大/去背景/格式转换，零额度 | `image`, `crop`, `rotate`, `flip`, `width`, `height`, `upscale`, `remove_background`, `format` |
| **`describe_image`** | 识图 | 分级 OCR：fast 本地 / standard / document（本地 OCR 校对）/ gui（结构化坐标） | `image`, `prompt`, `preset`, `model` |
| **`search_images`** | 搜图 | 文字搜图；以图搜图（配置 SerpAPI 时走 Google Lens） | `query`, `image`, `size`, `aspect_ratio`, `limit` |
| **`enhance_prompt`** | 辅助 | 将简短中文构思自动扩写为专业级英文绘图或办公架构拓扑提示词 | `prompt`, `style`, `purpose` |
| **`list_models`** | 辅助 | 查询当前服务端已持久化的模型池、办公场景模板与当前调度模式 | 无 |

---

## 接入 Agent

**远程（Streamable HTTP）** — Claude Code：
```bash
claude mcp add --transport http image-mcp https://oc.khc6.cn/mcp \
  --header "Authorization: Bearer <CLIENT_TOKEN>"
```
Cursor / 其他支持 `mcp.json` 的客户端：
```json
{
  "mcpServers": {
    "image-mcp": {
      "url": "https://oc.khc6.cn/mcp",
      "headers": { "Authorization": "Bearer <CLIENT_TOKEN>" }
    }
  }
}
```

**本地（stdio）** — 服务与 Agent 同机，可直接传本地图片路径（默认限 HOME 目录）：
```json
{
  "mcpServers": {
    "image-mcp": {
      "command": "python",
      "args": ["/path/to/image-mcp/caddy/mcp-server/server.py", "--stdio"]
    }
  }
}
```

## 关键环境变量

| 变量 | 说明 |
| :--- | :--- |
| `CLIENT_AUTH_TOKEN` | MCP 调用 Token，逗号分隔可配多个；也可在 config.json `auth.client_tokens` 中配置命名 Token 与 `rpm` |
| `ADMIN_TOKEN` | 控制台 Token，**必须与客户端 Token 不同**；未设置时首次启动随机生成并写入 config.json |
| `PUBLIC_BASE_URL` | 公网地址（如 `https://oc.khc6.cn`），用于生成生图签名链接；不设置则内联返回原图 |
| `IMAGE_MCP_SIGNING_SECRET` | 签名链接密钥，不设置则自动生成 |
| `IMAGE_MCP_DATA_DIR` | 生图存储目录，默认 `caddy/mcp-server/data` |
| `IMAGE_MCP_ALLOW_LOCAL_PATHS` / `IMAGE_MCP_LOCAL_ROOTS` | 是否接受本地路径入参及白名单目录（stdio 模式自动开启） |
| `SERPAPI_KEY` | 启用 Google Lens 以图搜图（也可在控制台 `search.serpapi_key` 配置） |

## 本地开发与测试

```bash
cd caddy/mcp-server
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt

ruff check .          # 静态检查
pytest -q             # 单元 + 集成测试（mock 上游，无需真实 Key）
python server.py      # 本地启动 → http://127.0.0.1:3002/admin
```

或使用 Docker：
```bash
docker build -t image-mcp caddy/mcp-server
docker run -p 3002:3002 -e CLIENT_AUTH_TOKEN=xxx -e ADMIN_TOKEN=yyy -v $PWD/data:/app/data image-mcp
```

---

## 生产部署与运维

> 💡 **私有配置说明**：生产服务器专有运维参数已统一保存在本地私有文档 `DEPLOYMENT_VM.local.md`（该文件已由 `.gitignore` 排除，防止公开泄露）。通用模板参见 `DEPLOYMENT_VM.example.md`。

* **云平台参考**：Azure VM / AWS EC2 / 腾讯云 CVM / 阿里云 ECS
* **边缘加速**：支持腾讯云 EdgeOne / Cloudflare CDN IPv6 回源至 TCP 3000
* **增量同步与重启示例**：
  ```bash
  # 同步整个服务目录（排除运行时数据、密钥配置、测试与脚本）
  rsync -avz -e "ssh -i <YOUR_SSH_KEY_PATH>" \
    --exclude '.venv*' --exclude '__pycache__' --exclude 'config.json' \
    --exclude 'data/' --exclude 'tests/' --exclude 'scripts/' \
    caddy/mcp-server/ "<USERNAME>@[<YOUR_SERVER_IPV6>]:/home/ubuntu/caddy/mcp-server/"
  rsync -avz -e "ssh -i <YOUR_SSH_KEY_PATH>" caddy/Caddyfile "<USERNAME>@[<YOUR_SERVER_IPV6>]:/home/ubuntu/caddy/"
  
  # 远程安装依赖并重启
  ssh -i <YOUR_SSH_KEY_PATH> <USERNAME>@<YOUR_SERVER_IPV6> \
    "cd /home/ubuntu/caddy && .venv/bin/pip install -r mcp-server/requirements.txt && ./manage.sh restart"
  ```
