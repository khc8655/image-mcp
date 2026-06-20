# Vertex AI Unified Proxy (AWS Lambda Version)

这是一个专为 **AWS Lambda** 优化的超轻量级 AI 代理服务，移除了 Express 等传统 Web 框架依赖，实现了零外部依赖包。这使得冷启动时间（Cold Start）缩短到最快（通常在 100ms - 300ms 左右）。

同时支持 **AWS Lambda 响应流（Response Streaming）**，从而实现 LLM 流式输出（SSE）的无延迟首字响应。

---

## 🛠 部署指引

### 第一步：打包代码
在本地 `lambda/` 目录运行以下命令打包（生成 `function.zip`）：
```bash
# 在 lambda 目录下运行
zip -r function.zip index.js package.json
```

### 第二步：创建 AWS Lambda 函数
1. 打开 [AWS Lambda 控制台](https://console.aws.amazon.com/lambda/)。
2. 点击 **Create function (创建函数)**：
   * 选择 **Author from scratch (从头开始创作)**。
   * **Function name (函数名称)**：填写如 `vertex-unified-proxy`。
   * **Runtime (运行时)**：选择 **Node.js 22.x**。
   * **Architecture (架构)**：推荐选择 **arm64**（更便宜且性能更佳），或者默认 `x86_64`。
   * 其他保持默认，点击 **Create function**。

### 第三步：上传代码
1. 进入刚创建的函数页面，选择 **Code (代码)** 选项卡。
2. 点击右侧的 **Upload from (上传来源)** -> 选择 **.zip file**。
3. 上传刚刚生成的 `function.zip` 并保存。

### 第四步：启用函数 URL 与“响应流”支持 (重要 🌟)
因为我们要支持 GPT/Gemini 流式打字机效果，所以必须启用 **Function URL** 并设置为 **RESPONSE_STREAM** 模式：
1. 在函数页面选择 **Configuration (配置)** 选项卡 -> 选择左侧的 **Function URL (函数 URL)**。
2. 点击 **Create function URL (创建函数 URL)**：
   * **Auth type (身份验证类型)**：选择 **NONE**（允许公网直接访问。安全性由代码中的 `CLIENT_AUTH_TOKEN` 接管）。
   * 点击展开 **Additional settings (其他设置)**：
     * ⚠️ **Invoke mode (调用模式)**：必须选择 **`RESPONSE_STREAM`** (而不是默认的 BUFFERED)。
     * 勾选 **Configure CORS (配置跨源资源共享 CORS)**。
3. 点击 **Save**。保存后，页面顶部会显示一个专属的公网链接（如 `https://xxxx.lambda-url.ap-southeast-1.on.aws/`），这即是您的 API 基址（API Base URL）。

### 第五步：配置环境变量
在 **Configuration (配置)** 选项卡 -> 选择左侧的 **Environment variables (环境变量)**，添加以下变量（切记去掉所有双引号 `"`）：

| 键 (Key) | 值 (Value) 样例 | 说明 |
| :--- | :--- | :--- |
| `VERTEX_PROJECT` | `jjb111` 或 `heroic-diode-497507-s8` | GCP 项目 ID |
| `VERTEX_API_KEY` | `AQ.Ab8RN6L7...` | Google Vertex AI 的 API 密钥 |
| `AGNES_API_KEY` | `sk-L0UhAOV...` | Agnes AI 的 API 密钥 |
| `CLIENT_AUTH_TOKEN` | `6ammYiLu4...` | （可选）客户端访问密码（如不设置则免密） |
| `TEXT_MODELS` | `gemini-3.1-flash-lite,gemini-3.1-flash,agnes-2.0-flash` | 可选，自定义支持的文本模型列表 |
| `MINIMAX_API_KEY` | `sk-cp-JUj...` | （可选）MiniMax API 密钥 |
| `SENSENOVA_API_KEY` | `sk-7x5Uuk...` | （可选）商汤 API 密钥 |

---

## 🔗 客户端连接配置
配置完成后，您可在 One-API / Next Chat / LobeChat 等客户端中按如下规则配置：
* **接口类型 (Type)**: OpenAI / 兼容通道
* **API Base URL (代理地址)**: `https://<您的函数URL>.lambda-url.<区域>.on.aws/v1` *(注意末尾带 `/v1`)*
* **API Key (秘钥)**: 填写您设置的 `CLIENT_AUTH_TOKEN`（如果没设则随便填）。
* **测试模型**: `agnes-2.0-flash` 或 `gemini-3.1-flash`。
