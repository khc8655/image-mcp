# KHC Image MCP Suite 核心开发与接手维护全景指南 (v3.0)

> **面向所有接手开发此项目的 AI Agent 及开发者的终极开发手册**  
> 本文档详尽记载了 `caddy/mcp-server/` 的架构设计、核心逻辑分布、数据流转、测试套件、云端部署与常见运维步骤。任何接手本项目的 Agent 遵照本文档即可无障碍进行后续开发、扩展适配器与调试部署。

---

## 1. 系统定位与全局架构

本项目是一个专为现代 AI Agent（如 WorkBuddy、Cursor、OpenClaw、Hermes、Claude Code 等）量身打造的**多模态图片全生命周期 MCP (Model Context Protocol 2.0.0) 服务端**，同时集成了基于 **KHC Web Design System** 的去程序员化轻量运维管理控制台（`/admin`）。

### 1.1 核心设计理念
1. **彻底去 Key 化与静默直读 (Zero-Key Experience)**：
   - 界面与模型配置中彻底摒弃 Key 选择与切换交互，用户与 Agent 无需维护 Key；
   - 后端 `resolve_local_provider_and_key` 自动选取对应厂商有效真实 Key（自动过滤测试 mock 键），本地无凭据时自动静默从中枢拉取，即开即用。
2. **双支柱全对称**：生图（Text-to-Image / Image-to-Image）与识图（Vision / OCR）具备完全对称的物理顺序优先级调度、主备故障转移与模型全生命周期可编辑能力。
3. **凭据与规则双向解耦**：
   - **Key 资产**：由外部腾讯云 EdgeOne 凭据中枢（`https://api.khc6.cn`）托管，本服务端按需拉取纯凭据并持久化存储在本地 `config.json`；
   - **适配器规则**：每个厂商拥有独立的规则文件 `presets/{provider}.json`，规则更新免重启一键热加载，厂商 API 升级绝不触动已有密钥。
4. **极简物理顺序调度**：默认将排在第 1 位的设为主力模型，超时或异常自动链式顺延回退至后序备用模型；Agent 传入 `model` 或 `provider` 时优先定向调度。

### 1.2 全链路拓扑图

```text
┌──────────────────────────────────────────────┐
│  Agent 客户端 (WorkBuddy, Cursor, Claude Code) │
└──────────────────────┬───────────────────────┘
                       │ (MCP 2.0.0 StreamableHTTP: POST /mcp)
                       ▼
┌──────────────────────────────────────────────┐
│  腾讯云 EdgeOne 边缘加速 (https://oc.khc6.cn)  │
│  • SSL 证书托管与全局低延迟加速                │
│  • 回源地址: [<ORIGIN_SERVER_IPV6>]:3000      │
└──────────────────────┬───────────────────────┘
                       │ (IPv6 直连回源)
                       ▼
┌──────────────────────────────────────────────┐
│  Azure 日本西 VM (Ubuntu 24.04 / khc-live-cn)  │
│  ┌────────────────────────────────────────┐  │
│  │ Caddy 反向代理 (:3000)                  │  │
│  │ • /admin, /static/*, /api/* -> :3002   │  │
│  │ • /mcp (CORS 预检 + Bearer 鉴权) -> :3002│  │
│  └───────────────────┬────────────────────┘  │
│                      ▼                       │
│  ┌────────────────────────────────────────┐  │
│  │ Python ASGI 服务 (:3002 / server.py)   │  │
│  │ • 🎨 生图引擎 (物理顺序 + 链式故障转移) │  │
│  │ • 👁️ 识图引擎 (分级 OCR + 故障转移)    │  │
│  │ • 🔍 搜图引擎 (文字搜图 + 语义以图搜图) │  │
│  │ • 🛠️ 轻量 Web 控制台 (/admin)          │  │
│  └───────┬────────────────────────┬───────┘  │
└──────────┼────────────────────────┼──────────┘
           │ (按需同步凭据)           │ (免重启热加载)
           ▼                        ▼
┌─────────────────────┐  ┌───────────────────────┐
│ EdgeOne 凭据中枢     │  │ 本地独立 Presets 规则库 │
│ (https://api.khc6.cn│  │ (presets/*.json)      │
│  /api/vault/*)      │  │ Google, MiniMax,      │
└─────────────────────┘  │ Agnes, SenseNova...   │
                         └───────────────────────┘
```

---

## 2. 源码目录结构与职责速查表

```text
caddy/mcp-server/
├── server.py                 # [核心主程序] MCP 2.0.0 工具定义、Starlette HTTP 路由、生图/识图/搜图执行器
├── config_manager.py         # [配置中枢] ConfigManager 单例，负责 config.json 读写、脱敏、独立 Presets 热加载
├── config.json               # [运行时配置] 持久化保存的本地 Provider 凭据、模型池、调度策略、搜图设置
├── requirements.txt          # [依赖清单] Python 依赖声明 (mcp, starlette, uvicorn, httpx, rapidocr 等)
├── presets/                  # [独立适配器规则库] 零代码耦合的独立厂商规范
│   ├── catalog.json          # 规则库目录清单与能力索引
│   ├── google.json           # Google Gemini / Vertex AI 原生生图与多模态规则
│   ├── minimax.json          # MiniMax (海螺 AI) 官方生图 (image-01/live) 与多模态规则
│   ├── agnes.json            # Agnes AI (agnes-image-2.5-flash / 3.0-flash) 规则
│   ├── sensenova.json        # 商汤日日新 (SenseNova-u1) 高清画质规则
│   ├── siliconflow.json      # 硅基流动 (FLUX.1 schnell/dev) 规则
│   ├── openai.json           # OpenAI (DALL-E 3 / GPT-4o) 官方规则
│   └── zhipu.json            # 智谱 (CogView-3/4 / GLM-4V) 中文语义规则
├── templates/                # [服务端渲染模板] 基于标准 HTML 声明式拼装，零第三方依赖
│   ├── admin.html            # 控制台外壳骨架 (~90 行)
│   └── partials/             # 业务微模块片段 (各 30~70 行)
│       ├── tab_generation.html  # 生图管理 Tab (超时配置、顺序调度说明、活跃模型池)
│       ├── tab_vision.html      # 识图与 OCR Tab (超时配置、Preset 等级、模型池)
│       ├── tab_search.html      # 搜图管理 Tab (引擎配置、Playground 测试控制台)
│       ├── tab_system.html      # 系统配置 Tab (EdgeOne 中枢一键同步、规则热重载)
│       ├── modals.html          # 挂载模型弹窗 (选厂商/Key/模型) + 编辑模型弹窗 (Key/角色/高级参数)
│       └── icons.html           # SVG 矢量图标精灵库
├── static/                   # [静态前端资产] 严格遵循 KHC Web Design System 规范
│   ├── admin.css             # 控制台专用布局微调与组件样式
│   ├── core.css              # 基础原子类、间距、排版样式
│   ├── colors_and_type.css   # 色彩系统变量与字体定义
│   ├── components.css        # 卡片、按钮、表格、表单、Badge、Modal 样式
│   └── js/                   # 前端 ES Modules 微模块 (带 ?v=2.8.0 统一防缓存标签)
│       ├── app.js            # 主入口协调器，初始化、登录鉴权、全局函数挂载至 window
│       ├── api.js            # 封装 Fetch 请求、全局 Token 自动注入与统一错误处理
│       ├── common.js         # Toast 通知系统、Tab 切换逻辑、Modal 弹窗显隐
│       ├── tab_generation.js # 生图业务交互、模型池渲染、行内切 Key/角色、数字重排
│       ├── tab_vision.js     # 识图业务交互、模型池渲染、行内切 Key/角色、数字重排
│       ├── tab_search.js     # 搜图参数配置与在线检索 Playground
│       ├── tab_system.js     # 凭据中枢连通测试、一键全量凭据同步、热重载适配规则
│       └── modals.js         # 纯点选挂载模型、自主选 Key、编辑模型弹窗与高级参数表单
└── scratch/                  # [开发与测试测试套件] 严禁污染根目录，所有脚本集中于此
    ├── test_e2e_flow.py      # [主力 E2E 测试] 全流程闭环回归（凭据拉取/挂载/切Key/编辑/顺序调度故障转移）
    ├── test_dual_pillars_mcp.py # 独立 Presets 规则解析与纯凭据合并单元测试
    └── test_payload_dispatch.py # 针对各大厂商请求 Payload 协议组装准确性拦截测试
```

---

## 3. 核心业务逻辑与调度引擎详解

### 3.1 极简顺序优先级调度引擎 (`generate_image` / `describe_image`)
在 v2.8 中，生图与识图彻底废弃了黑盒的猜词路由与广播多图模式，统一为**透明且确定性的物理顺序优先级模式**：

1. **候选列表获取**：
   从 `config_mgr.get("generation", "models")` 提取所有 `enabled: True` 的模型列表，物理列表索引 `0` 即为默认首选主力模型。
2. **定向优先拦截**：
   若调用端或 Agent 传入了参数 `model` 或 `provider`：
   - 在已启用的模型池中匹配该目标模型；
   - 命中后将该模型置于候选列表首位，其余可用模型作为后序兜底备选。
3. **链式故障转移 (Fallback) 与熔断守护**：
   - 按序遍历候选模型；
   - 针对每个模型，使用 `asyncio.wait_for(..., timeout=timeout_sec)`（从配置读取 `timeout_seconds`，默认 45 秒）进行严格守护；
   - 一旦遇到 HTTP 报错、限流（429）或超时（`TimeoutError`），在日志打印 Warning 警告，并自动顺延调用下一顺位备用模型；
   - 只要有任意一个模型成功生成/分析，立即返回结果；若全池遍历完毕均失败，抛出汇总诊断异常。

### 3.2 厂商高级参数动态注入机制
在模型池中，每个模型条目可包含可选的 `advanced` 字典，调用 `execute_image_generation` 时会自动解构并注入上游官方 Payload：
- **MiniMax**：
  - `prompt_optimizer`: 布尔值，控制是否开启官方提示词美化器；
  - `aigc_watermark`: 布尔值，控制是否在生成图打上 AIGC 水印。
- **FLUX / Agnes**：
  - `size`: 分辨率，可选 `"1K"` 或 `"2K"`；
  - `negative_prompt`: 默认负向提示词，如 `blurry, low quality`。
- **Google Imagen / Gemini**：
  - `person_generation`: 人像安全等级，可选 `"ALLOW_ADULT"`, `"DONT_ALLOW"`, `"ALLOW_ALL"`。

### 3.3 识图分级体系与本地 RapidOCR
`describe_image` 支持 `preset` 等级：
- `fast`: 若配置中 `rapidocr_enabled: True`，优先尝试直接加载本地 `rapidocr_onnxruntime` 引擎离线抽取文字（零 Token 消耗、100ms 极速响应）；若未安装或抽取为空，平滑降级至云端视觉模型池。
- `standard`: 注入标准视觉引导词。
- `document`: 注入表格专业引导词，要求抽取并输出规范的 GitHub Flavored Markdown (GFM) 表格。
- `gui`: 注入界面坐标引导词，要求输出 `[ymin, xmin, ymax, xmax]` 归一化坐标。

### 3.4 彻底去 Key 化与上游三大真实驱动修复 (v2.9)
在 v2.9 中，系统实现了「Zero-Key 极简体验」并彻底修复了三大厂商的真实底层调用：
1. **界面与模型彻底去 Key 化**：
   - 挂载弹窗与编辑弹窗彻底移除了 Key 选择；
   - 模型池表格中不再显示 Key 列，模型配置条目不再绑定 `key` 属性；
2. **后端全自动智能匹配与静默直读**：
   - `resolve_local_provider_and_key` 自动识别并过滤历史单测残留的 mock 键（如 `*-12345`），优先选取厂商的第一条真实有效 Key；
   - 若本地缓存中无可用 Key，自动静默触发 EdgeOne 凭据中枢（`https://api.khc6.cn`）同步拉取，无需人工干预；
3. **上游真实驱动关键修复**：
   - **Google Gemini**：支持新版 Developer Key（以 `AQ.` 开头），判定 `api_key.startswith("AIza") or api_key.startswith("AQ.")` 直连 `https://generativelanguage.googleapis.com/v1beta/models/{clean_model}:generateContent`；识图模型升级为当前稳定官方多模态模型 `gemini-3.8-flash`；
   - **MiniMax**：代码与配置统一纠错为官方真实域名 `https://api.minimaxi.com/v1`（末尾带 `i`）；
   - **SenseNova (商汤日日新)**：彻底移除将 `token.sensenova.cn` 改为 `api.sensenova.cn` 的代码，统一保持官方有效生图端点 `https://token.sensenova.cn/v1/images/generations`；
4. **单测数据隔离**：
   - `scratch/test_e2e_flow.py` 使用独立临时测试文件 `test_config.json` 进行全流程验证并在结束后自动销毁，彻底杜绝污染生产 `config.json`。

### 3.5 v3.0 办公生图参数扩展与场景模板引擎 (方案 A+B)
针对工作信息化、网络拓扑图与架构图等专业级办公使用场景，v3.0 进行了深度扩展，保持纯粹生图定位（复杂代码图表由 Agent 本身处理）：
1. **生图参数全量扩展 (方案 A)**：
   - **10 大主流宽高比**：涵盖 `1:1`, `16:9`, `9:16`, `4:3`, `3:4`, `3:2`, `2:3`, `4:5`, `5:4`, `21:9`（带鱼屏/全景）；
   - **画质分辨率等级**：支持 `auto`, `1K`, `2K`, `4K`，自动适配 Google Gemini 3 Pro `imageConfig.imageSize` 与 Agnes 等上游尺寸控制；
   - **种子与并发张数**：入参支持 `seed` (锁定随机性以复现风格) 与 `count` (并发生成张数)；
   - **负向提示词**：支持 `negative_prompt` 参数，在上游各引擎（Google、MiniMax、Agnes、OpenAI 等）中精准注入排除负向要素。
2. **5 大专业办公场景模板引擎 (方案 B)**：
   通过 `scene` 参数一键调用内置专业企业级提示词、工程后缀、负向词与默认画幅比例：
   - `infra_scene`: **信息化全景场景图**（智慧园区/IDC机房/政务云/工业互联网 2.5D 高清示意图）；
   - `network_topology`: **网络拓扑示意图**（核心路由/防火墙/混合云骨干网白底整洁连线效果图）；
   - `architecture`: **系统软件架构图**（分层微服务/API网关/数据流清晰色块图）；
   - `infographic`: **科技信息图/汇报海报**（技术白皮书与数字化汇报高质感排版图）；
   - `icon_set`: **科技图标与组件组**（极简纯白底扁平科技图标套件）；
   - `none`: 不套用任何模板，使用纯净提示词。
3. **出图自动持久化与回源支持**：
   出图自动写入 `static/generated/YYYYMMDD/*.png`，并通过 EdgeOne 边缘回源 `https://oc.khc6.cn/static/generated/...` 供浏览器即时预览与下载。
4. **安全加固与底层修复**：
   - 防御 LFI 本地敏感文件泄露：`resolve_image_to_base64` 严禁读取系统路径 (`/etc/passwd`)、凭据文件 (`config.json`) 及跨目录穿越 (`..`)；
   - 多模态 MIME 动态嗅探：自动检测 JPEG/PNG/WebP/GIF Magic Bytes，根除 MIME 格式混淆异常。
5. **专业级提示词扩写**：
   `enhance_prompt` 增加 `purpose="work"` 支持，面向企业架构设计与信息化技术场景进行专业级提示词自动扩写。

---

## 4. 独立适配器规范 (`presets/{provider}.json`)

任何接手本项目的 Agent 如需接入新的模型提供商，**无需修改 `server.py` 的核心调度逻辑**，只需在 `presets/` 下新建一个 JSON 文件，并遵循以下 Schema：

```json
{
  "id": "new_provider",
  "name": "提供商显示名称",
  "version": "1.0.0",
  "capabilities": ["image_generation", "vision"],
  "image_models": [
    {
      "name": "model-id-v1",
      "display_name": "旗舰超清模型",
      "description": "模型特点描述",
      "supported_ratios": ["1:1", "16:9", "9:16", "4:3", "3:4"]
    }
  ],
  "vision_models": [
    {
      "name": "vision-model-v1",
      "display_name": "多模态旗舰",
      "description": "多模态能力描述"
    }
  ],
  "adapter_rules": {
    "image_generation": {
      "protocol": "openai_images", // 可选: openai_images | minimax_image | gemini_image | agnes_image
      "base_url": "https://api.example.com/v1",
      "endpoint": "/images/generations",
      "default_params": {},
      "size_map": {
        "1:1": "1024x1024",
        "16:9": "1792x1024",
        "9:16": "1024x1792"
      },
      "response_b64_fields": ["b64_json", "base64", "image_base64"],
      "supports_edit": false
    },
    "vision": {
      "protocol": "openai_vision", // 可选: openai_vision | gemini_vision | agnes_vision
      "base_url": "https://api.example.com/v1",
      "endpoint": "/chat/completions"
    }
  }
}
```

新建后：
1. 在 `presets/catalog.json` 中追加该提供商条目；
2. 控制台点击「系统配置」->「一键热重载适配规则」即时生效，无需重启服务进程。

---

## 5. Web 控制台微模块化开发规范

### 5.1 模块依赖与防缓存机制
控制台采用纯原生现代浏览器 ES Modules 开发，不依赖 Webpack、Vite 等构建工具。  
**⚠️ 必须遵守的 CDN 缓存隔离铁律**：
- 在 `templates/admin.html` 以及 `static/js/*.js` 中相互 import 时，**必须携带版本查询参数**（当前统一为 `?v=2.8.0`）：
  ```javascript
  import { showToast, openModal } from './common.js?v=2.8.0';
  import { api } from './api.js?v=2.8.0';
  ```
- 每次修改前端 JS 文件，必须同步递增版本号（如 `2.8.1`），避免腾讯云 EdgeOne 或浏览器强缓存历史切片导致 `Uncaught SyntaxError` 白屏。

### 5.2 页面拼装机制 (`templates/admin.html`)
服务端在 `server.py` 的 `admin_page()` 函数中，使用 Python 原生字符串替换完成极速拼装：
```html
<!-- include:icons -->
<!-- include:tab_generation -->
<!-- include:tab_vision -->
<!-- include:tab_search -->
<!-- include:tab_system -->
<!-- include:modals -->
```
每个子功能界面独立保存在 `templates/partials/*.html` 中，职责单一，严禁单文件膨胀。

---

## 6. 本地开发、调试与运行指南

### 6.1 虚拟环境与依赖准备
根据开发机环境选择 Python 解释器（建议 Python 3.11+）：
```bash
# 1. 确保使用专门的虚拟环境 (严禁使用系统全局 Python)
cd /Users/xk/Documents/vertex/caddy/mcp-server

# 本地推荐使用的 Python 3.11 环境：
/Users/xk/Documents/mcp/.venv311/bin/python --version

# 2. 验证依赖完整性
/Users/xk/Documents/mcp/.venv311/bin/pip list | grep -E "mcp|httpx|starlette|uvicorn"
```

### 6.2 语法校验
修改 Python 源码后，务必第一时间执行编译检查：
```bash
/Users/xk/Documents/mcp/.venv311/bin/python -m py_compile server.py config_manager.py
```

### 6.3 本地启动服务进行调试
```bash
/Users/xk/Documents/mcp/.venv311/bin/python server.py
# 服务默认在 127.0.0.1:3002 启动
# 访问控制台：http://127.0.0.1:3002/admin
```

---

## 7. 自动化测试套件与执行规范

所有测试工具均保存在 `caddy/mcp-server/scratch/` 目录中，在进行任何提交或部署前，**必须保证以下测试 100% 通过**：

### 7.1 主力全生命周期闭环 E2E 测试
```bash
/Users/xk/Documents/mcp/.venv311/bin/python scratch/test_e2e_flow.py
```
**覆盖验证步骤**：
1. `[Step 1]` 模拟中枢全量同步凭据并与本地 Presets 合并落盘；
2. `[Step 2]` 模拟控制台纯点选挂载模型至活跃池；
3. `[Step 3]` 模拟表格行内秒级修改偏好角色；
4. `[Step 4]` 模拟行内启用/禁用切换；
5. `[Step 5]` 模拟模型移除；
6. `[Step 6]` 模拟挂载模型时自主选择绑定 Key（如 `vip`）；
7. `[Step 7]` 模拟表格行内即时切换绑定 Key；
8. `[Step 8]` 模拟编辑弹窗修改厂商高级参数；
9. `[Step 9]` 模拟物理顺序调度：首选模型异常时自动故障转移至第 2 顺位并成功出图；
10. `[Step 10]` 模拟 Agent 显式指定 `provider`/`model` 时定向调度目标模型。

### 7.2 适配规则与纯凭据单测
```bash
/Users/xk/Documents/mcp/.venv311/bin/python scratch/test_dual_pillars_mcp.py
```

### 7.3 上游厂商 Payload 协议分发拦截单测
```bash
/Users/xk/Documents/mcp/.venv311/bin/python scratch/test_payload_dispatch.py
```

### 7.4 线上生产环境端到端验证
```bash
/Users/xk/Documents/mcp/.venv311/bin/python /Users/xk/Documents/vertex/scratch/verify_image_mcp.py
```

---

## 8. 云端生产环境部署与运维操作

> 💡 **私有配置说明**：生产服务器的真实 IP、私钥路径、管理令牌等专有运维参数已统一整理在本地私有文档 `DEPLOYMENT_VM.local.md`（该文件已由 `.gitignore` 排除，防止公开泄露）。通用模板参见 `DEPLOYMENT_VM.example.md`。

### 8.1 生产服务器配置速查 (示例)
* **云厂商与地域**：Microsoft Azure 日本西 (Japan West) VM
* **网络与访问**：
  * 主机 IPv6 地址：`<YOUR_SERVER_IPV6>` (私有运维详见 `DEPLOYMENT_VM.local.md`)
  * 操作系统用户：`ubuntu`
  * SSH 登录密钥：`<YOUR_SSH_KEY_PATH>`
  * 端口开放：仅监听 TCP `3000`（Caddy 反向代理），后端 Python 运行在内部 `127.0.0.1:3002`
  * 域名与边缘加速：`https://oc.khc6.cn`（腾讯云 EdgeOne 托管）

### 8.2 生产文件增量同步 (SCP)
在本地运行以下命令，将代码、模板、静态资产与预设同步至云端：
```bash
# 同步 Python 核心程序
scp -i <YOUR_SSH_KEY_PATH> -o StrictHostKeyChecking=no \
  caddy/mcp-server/server.py \
  caddy/mcp-server/config_manager.py \
  "ubuntu@[<YOUR_SERVER_IPV6>]:/home/ubuntu/caddy/mcp-server/"

# 递归同步模板、静态资产与适配规则
scp -i <YOUR_SSH_KEY_PATH> -o StrictHostKeyChecking=no -r \
  caddy/mcp-server/presets \
  caddy/mcp-server/templates \
  caddy/mcp-server/static \
  "ubuntu@[<YOUR_SERVER_IPV6>]:/home/ubuntu/caddy/mcp-server/"
```

### 8.3 远程服务重启与状态查看
```bash
# 重启 Caddy 与 MCP 服务进程
ssh -i <YOUR_SSH_KEY_PATH> -o StrictHostKeyChecking=no \
  ubuntu@<YOUR_SERVER_IPV6> "/home/ubuntu/caddy/manage.sh restart"

# 查看运行状态
ssh -i <YOUR_SSH_KEY_PATH> -o StrictHostKeyChecking=no \
  ubuntu@<YOUR_SERVER_IPV6> "/home/ubuntu/caddy/manage.sh status"

# 实时查看服务端日志
ssh -i <YOUR_SSH_KEY_PATH> -o StrictHostKeyChecking=no \
  ubuntu@<YOUR_SERVER_IPV6> "tail -f /home/ubuntu/caddy/mcp-server/mcp.log"
```

---

## 9. 开发者接手守则与避坑指南 (Agent Guidelines)

1. **工作区整洁原则 (Zero Clutter)**：
   严禁在工程根目录 (`vertex/` 或 `mcp-server/`) 散落临时的 debug 脚本、测试文件或 `.txt` 日志。所有测试与辅助脚本必须统一放在 `scratch/` 目录下。
2. **文档优先与自动随动原则 (Proactive Sync)**：
   每次修改代码功能、新增工具参数或调整配置字段后，**必须立即同步更新本文档与根目录 README.md**，不留待用户提醒。
3. **安全操作红线**：
   - 严禁在未经用户明确许可的情况下执行 `rm` 删除文件或全局 `pip install`/`apt-get`；
   - 本地与远端输出的密钥信息必须严格经过 `mask_key` 脱敏处理（仅透出前 4 位与后 4 位）。
4. **前端缓存防踩坑**：
   修改 `static/js/` 下的任何文件时，必须联动修改对应文件内 `import` 的 `?v=X.X.X` 后缀，以及 `templates/admin.html` 引入 `app.js` 的版本后缀。
5. **Caddy 配置文件保护**：
   远端 Caddyfile 已经针对 EdgeOne 回源做了精确配置并设置了全局禁用静态缓存头，切勿擅自将其恢复为旧版带证书域名的 OCI 配置。
