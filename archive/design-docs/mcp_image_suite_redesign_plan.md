# 🎨 多模态图片专属 MCP 服务（Image MCP Suite）全量重构方案 (v2.1)

> **文档版本**：v2.1 Architecture & Design System Alignment  
> **UI 规范依据**：[/Users/xk/Documents/design-system](file:///Users/xk/Documents/design-system) (KHC Web Design System)  
> **定位**：专注图片全生命周期的轻量高性能 MCP 服务（生图三模 + 分级识图 + 统一搜图 + 极简 Web 管理端）  
> **宿主环境**：Oracle Cloud 新加坡 VM + 腾讯云 EdgeOne 边缘加速 (`oc.khc6.cn`) + Caddy 443

---

## 🗺️ 一、 系统总览与网络拓扑

系统彻底剥离原有的 Vertex AI 文本中转逻辑，转型为纯粹的 **图片多模态中枢 (Image MCP Suite)**。

```mermaid
graph TD
    subgraph Clients [调用方]
        Agent[AI Agent 客户端<br>Cursor / WorkBuddy / Hermes]
        Admin[管理员浏览器<br>Web 管理端]
    end

    subgraph EdgeGateway [边缘加速与安全反代]
        EdgeOne[腾讯云 EdgeOne 加速 oc.khc6.cn]
        Caddy[Caddy :443 统一端口<br>15年 Origin CA 证书]
        EdgeOne --> Caddy
    end

    subgraph MCPCore [轻量 Python 核心进程 :3002]
        Auth[统一鉴权中间件<br>API Bearer Token]
        WebServer[Web 管理端静态托管 & API<br>基于 KHC Design System]
        ConfigManager[持久化配置管理器<br>config.json 热重载]
        
        subgraph ToolSuite [多模态图片工具集 /mcp]
            T1[🎨 生图引擎<br>文生图 / 图生图<br>自动路由 / 广播全发 / 手动指定]
            T2[👁️ 识图引擎<br>分级 OCR<br>Fast / Standard / Document / GUI]
            T3[🔍 搜图引擎<br>文字搜图 / 内部OCR联动以图搜图]
            T4[🪄 辅助工具<br>enhance_prompt / list_models]
        end
    end

    subgraph Providers [后端提供商与本地引擎]
        P_Cloud[云端大厂 API<br>Google Vertex / MiniMax / SenseNova / Pexels]
        P_Local[可选本地引擎<br>RapidOCR ONNX / ComfyUI]
    end

    Agent -->|POST /mcp (Bearer Token)| EdgeOne
    Admin -->|GET /admin (Token 登录)| EdgeOne
    Caddy --> Auth
    Auth --> WebServer
    Auth --> ToolSuite
    WebServer <--> ConfigManager
    ConfigManager --> ToolSuite
    ToolSuite --> P_Cloud
    ToolSuite --> P_Local
    T3 -.->|以图搜图联动抽取| T2
```

---

## 🖥️ 二、 Web 管理端设计（严格遵循 KHC Web Design System）

依据 `/Users/xk/Documents/design-system` 规范：
* **设计原则**：轻量优先、中文优先、少即是多（拒绝过度装饰与 Emoji 泛滥）、Brand Primary 采用 `#1664FF`、页面底色 `#F8FAFC`、卡片底色 `#FFFFFF`、边框 `#E2E8F0`。
* **零构建引入**：直接将设计系统的 `colors_and_type.css` 与 `components.css` 打包为前端静态资产，单个 `admin.html` 由 Python 后端托管，无需安装任何 Node.js/npm 依赖。
* **统一身份认证**：登录输入框直接校验 `CLIENT_AUTH_TOKEN`。登录成功后写入浏览器本地，后续调用 `/api/config` 均携带 Bearer Token。
* **热更新持久化**：所有表单修改直接更新宿主机 `config.json`，在内存中**秒级热生效**，彻底告别频繁修改 `.env` 和重启服务的繁琐流程。

### 页面布局与模块交互

```
┌────────────────────────────────────────────────────────────────────────┐
│  KHC Image MCP 控制台                           Token: •••••••• [退出] │
├────────────────────────────────────────────────────────────────────────┤
│  [基础设置]   [生图策略]   [识图与OCR]   [搜图配置]                     │
├────────────────────────────────────────────────────────────────────────┤
│                                                                        │
│  【生图模式选择】                                                      │
│  🔘 自动路由 (Auto-Route)                                              │
│     由后台根据场景自动分流最优模型（写实/中文/角色）并在故障时自动降级    │
│                                                                        │
│  ⚪ 广播全发 (Broadcast)                                               │
│     多模型并发渲染，一次性返回 2~3 张候选图供选择                       │
│     选择参与广播的模型: [✓] Google Imagen  [✓] MiniMax  [✓] SenseNova │
│                                                                        │
│  ⚪ 手动模式 (Manual)                                                  │
│     固定指定使用单一模型，不进行自动切换                                │
│     默认使用模型: [ Google Gemini 3.1 Flash Image  ▼ ]                 │
│                                                                        │
│  ────────────────────────────────────────────────────────────────────  │
│  【提供商凭证管理】                                                    │
│  • Google Vertex AI:   [ 项目 ID ] [ 区域: global ] [ API Key ]        │
│  • MiniMax API Key:    [ ••••••••••••••••••••••••••••••••••• ]         │
│  • SenseNova API Key:  [ ••••••••••••••••••••••••••••••••••• ]         │
│  • 本地 ComfyUI 端点:   [ http://127.0.0.1:8188             ] (可选)    │
│  • Pexels API Key:     [ ••••••••••••••••••••••••••••••••••• ] (可选)    │
│                                                                        │
│  ────────────────────────────────────────────────────────────────────  │
│  【识图与 OCR 配置】                                                   │
│  默认 OCR 等级: 🔘 Standard (云端通用)  ⚪ Fast (本地极速) ⚪ Document  │
│                                                                        │
│  ────────────────────────────────────────────────────────────────────  │
│  【搜图配置】                                                          │
│  默认文字搜图引擎: 🔘 DuckDuckGo (免Key)  ⚪ Pexels (高清摄影)         │
│                                                                        │
│                                                   [ 保存并应用配置 ]   │
└────────────────────────────────────────────────────────────────────────┘
```

---

## 🎨 三、 生图功能升级：三大模式（自动、广播、手动）

### 3.1 极简统一入参（面向 Agent 抽象）
Agent 只需关注视觉意图，不再接触 `cfg`、`steps` 等底层参数：
```json
{
  "prompt": "一座悬浮在晨雾峡谷之上的未来科技城市，日出金色晨光",
  "aspect_ratio": "16:9",      // 1:1, 16:9, 9:16, 4:3, 3:4
  "action": "generate",        // "generate"(文生图) 或 "edit"(图生图)
  "image": "https://...",      // 图生图时的参考图 (URL/Base64/本地路径)
  "style": "photorealistic",   // auto, photorealistic, anime, cinematic
  "mode": "auto"               // 可选覆盖: "auto" | "broadcast" | "manual"
}
```

### 3.2 三大路由模式的执行机制

1. **自动路由 (Auto-Route - 默认推荐)**：
   * 后台智能根据 Prompt 语种和特征适配：
     * 中文书法/排版/印章 $\rightarrow$ 路由至 **SenseNova-U1**；
     * 人物角色一致性 $\rightarrow$ 路由至 **MiniMax image-01**；
     * 通用写实与自然风光 $\rightarrow$ 路由至 **Google Gemini 3.1 Flash Image**。
   * **弹性级联降级**：若首选模型遇到 429 配额限制，45 秒内平滑自动降级尝试下一厂商。
2. **广播全发模式 (Broadcast - 多图并发对比)**：
   * 网关接收到请求后，同时并发下发给选中的多个模型（如 Google + MiniMax + SenseNova）；
   * 采用 `asyncio.gather(..., return_exceptions=True)` 并发收集；
   * **一次性返回多张不同模型生成的高清图片**（附带来源模型标签），供 Agent 或人类直接对比挑选！
3. **手动模式 (Manual - 确定性指定)**：
   * 用户在 Web 控制台固定选定单一模型，或 Agent 在入参中指定 `provider="minimax"`；
   * 网关绕过任何智能路由与广播逻辑，**仅调用指定的单一提供商**，适合对特定模型输出有绝对要求的固定工作流。

---

## 👁️ 四、 识图功能：Agent 专用 OCR 分级体系

将识图抽象为分级预设（Presets），兼顾极速与深度：

| 等级 (Preset) | 目标延迟 | 底层驱动 | 输出与特点 | 典型场景 |
|---|---|---|---|---|
| **`fast`** (极速纯文本) | **< 150ms** | **本地 RapidOCR (ONNX)**<br>(轻量免显卡，30MB 内存) | 纯文本提取 + 简易坐标，零云端 Token 费用 | 提取验证码、单行报错日志、截图标量 |
| **`standard`** (默认通用) | **1.0 ~ 2.0s** | **云端视觉多模态**<br>(当前为主力 Google Gemini 3.6 Flash / 备用 MiniMax-M3) | 视觉深度语义理解 + Markdown 结构化输出 | 常见图文识别、图表分析、多模态问答 |
| **`document`** (高精文档与表格) | **2.5 ~ 4.0s** | **云端 Pro 视觉 / 专项表格版面 Prompt** | 严格保留 **GFM Markdown 表格**、标题层级与阅读顺序 | 发票核对、合同抽取、财报入库 |
| **`gui`** (界面元素定位) | **0.8 ~ 1.5s** | **归一化坐标解析器** | 输出 `[ymin, xmin, ymax, xmax]` (0~1000) 与点击坐标 | Computer Use、UI 自动化定位 |

---

## 🔍 五、 搜图功能：解耦设计（文字搜图 + 联动已有 OCR 搜图）

针对用户的精准指导：**“搜图中不需要指定 Gemini，我们本来就有 OCR 的”**。我们实现彻底解耦的模块联动架构：

```
                    ┌────────────────────────┐
                    │      search_images     │
                    └───────────┬────────────┘
                                │
        ┌───────────────────────┴───────────────────────┐
        ▼ (传入 query 关键词)                            ▼ (传入 image 待搜图片)
【文字搜图 Text Search】                        【以图搜图 Reverse Search】
   ├─ DuckDuckGo Images (免Key默认)                │
   └─ Pexels API (备选高清摄影)                    ▼
                                       ┌────────────────────────────────┐
                                       │ 联动当前内部已有的 OCR/识图引擎 │
                                       │ (不管是 Fast 本地还是 Standard)│
                                       └───────────────┬────────────────┘
                                                       │ 提取核心文字与主体关键词
                                                       ▼
                                       ┌────────────────────────────────┐
                                       │ 编译为精准 Query 驱动搜图引擎   │
                                       └────────────────────────────────┘
```

1. **文字搜图**：
   * 默认采用 **DuckDuckGo Images**：在新加坡 Oracle VM 上直连响应极快，**完全免费、免 Key、开箱即用**。
   * 备选支持 **Pexels API**：在 Web 端填入 Key 即可检索 4K 商业免版权摄影图。
2. **以图搜图（完全复用系统内已有的 OCR/识图组件）**：
   * 当用户或 Agent 传入图片时，**直接调用本 MCP 内部已配置的 OCR/识图引擎**抽取核心文字、品牌型号与主体描述；
   * 即使以后后台将 OCR 切换为本地引擎或 MiniMax，以图搜图都能无缝自适应，不绑死任何单一模型；
   * 提取后的关键词自动进入搜图引擎并发检索全网高清相关图。
3. **输出标准化（严禁塞入原图 Base64）**：
   * 统一输出包含缩略图与高清原图直链的 Markdown：
     ```markdown
     [![图片标题](缩略图URL)](原图高清直链)
     * 尺寸: 1920x1080 | 来源: duckduckgo.com
     ```
   * 极度节省 Agent 上下文 Token，客户端直接渲染缩略图供人类预览。

---

## 📅 六、 分步实施计划

1. **Step 1：架构解耦与精简**
   * 从主项目中剔除 Vertex AI 文本中转代码与 `models.json`，建立基于 `config.json` 的持久化配置管理机制。
2. **Step 2：构建 KHC Design System Web 管理端**
   * 引入 `colors_and_type.css` 与 `components.css`，实现轻量、美观、专业的 `/admin` 单页面控制台，支持 Token 登录与配置热保存。
3. **Step 3：升级生图双模为三模（自动、广播、手动）**
   * 改造 `generate_image` 与 `edit_image`，接入手动模式、广播全发多图模式与自动路由模式。
4. **Step 4：落地分级 OCR 与统一搜图**
   * 完善分级 OCR（Fast/Standard/Document）；
   * 实现文字搜图与联动 OCR 的以图搜图 `search_images` 工具。
5. **Step 5：线上部署与端到端联调**
   * 部署至 Oracle VM，通过 EdgeOne 验证 Web 控制台与所有 MCP 工具。
