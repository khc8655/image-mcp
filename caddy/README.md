# KHC Image MCP Suite - Caddy 网关与云端生产运维指南 (Azure VM)

> **生产环境定位**：本项目部署在 **Azure 日本西 (Japan West) VM**（私有运维参数详见本地 `DEPLOYMENT_VM.local.md`），通过 **Caddy** 提供高性能无损反向代理与 CORS 预检处理，上游依托 **腾讯云 EdgeOne 边缘加速** (`https://oc.khc6.cn`)，后端承载多模态图片全生命周期 **MCP 2.0.0** 服务与 **Web 管理控制台**。

---

## 1. 核心网络架构与端口转发

```text
[Agent 客户端 (Cursor, WorkBuddy, Claude Code)]        [管理员 (Web 浏览器)]
                 │                                                │
                 │ (MCP 2.0.0 StreamableHTTP)                     │ (Web 控制台 Token 鉴权)
                 ▼                                                ▼
     [腾讯云 EdgeOne 边缘加速节点 (https://oc.khc6.cn)]  <-- 国内低延迟加速 (TTFB < 90ms)
                                 │
                                 │ (IPv6:3000 源站直连回源)
                                 ▼
         [Azure 日本西 VM :3000 Caddy 反向代理]
             ├── GET  /admin, /static/*, /api/*  --> 转发至 127.0.0.1:3002
             ├── OPTIONS /mcp                   --> 本地 204 放行 CORS 预检
             └── POST /mcp (Bearer 鉴权拦截)     --> 转发至 127.0.0.1:3002 (MCP 2.0.0)
                                 │
                                 ▼
             [Python ASGI 进程 (127.0.0.1:3002 / server.py)]
                 ├── 🎨 生图支柱引擎 (物理顺序调度 + 链式故障转移)
                 ├── 👁️ 识图支柱引擎 (分级 OCR + 故障转移)
                 ├── 🔍 独立搜图引擎 (文字搜图 + 语义以图搜图)
                 ├── 📂 本地独立 Presets 规则库 (免重启热重载)
                 └── 🔑 EdgeOne Vault 凭据中枢同步 (全量对齐 Key 资产)
```

---

## 2. 核心端口与安全边界

| 端口 | 监听协议 | 绑定地址 | 职责说明与安全边界 |
| :--- | :--- | :--- | :--- |
| **`3000`** | TCP (HTTP) | `0.0.0.0` / `[::]` | **对外唯一入口**。由 Caddy 监听，仅供腾讯云 EdgeOne 节点回源。云安全组仅开放此端口。 |
| **`3002`** | TCP (HTTP) | `127.0.0.1` | **内部核心服务**。由 Python ASGI (Uvicorn / `server.py`) 监听，禁止公网直连，只能由本地 Caddy 反向代理。 |

---

## 3. Caddyfile 核心规则解析 (`caddy/Caddyfile`)

```caddy
{
    auto_https off
    admin off
}

:3000 {
    # 1. Web 控制台与静态资源路由 (强制全局禁用边缘与浏览器缓存，保证前端即改即见)
    @admin_routes {
        path /admin /admin/* /static/* /api/* /files/*
    }
    handle @admin_routes {
        header Cache-Control "no-cache, no-store, must-revalidate"
        header Pragma "no-cache"
        header Expires "0"
        reverse_proxy 127.0.0.1:{env.MCP_SERVER_PORT}
    }

    # 2. MCP 2.0.0 跨域预检快速放行
    @mcp_options {
        path /mcp /mcp/*
        method OPTIONS
    }
    handle @mcp_options {
        header Access-Control-Allow-Origin "*"
        header Access-Control-Allow-Methods "GET, POST, OPTIONS, DELETE"
        header Access-Control-Allow-Headers "Content-Type, Authorization, Mcp-Protocol-Version, Mcp-Session-Id"
        header Access-Control-Expose-Headers "Mcp-Session-Id, Mcp-Protocol-Version"
        header Access-Control-Max-Age "86400"
        respond "" 204
    }

    # 3. MCP 业务请求路由 (校验 Bearer Token 并流式透传)
    @mcp_path {
        path /mcp /mcp/*
        not method OPTIONS
    }
    # Bearer 鉴权与按客户端限流在应用层 (security.py) 完成
    handle @mcp_path {
        header Access-Control-Allow-Origin "*"
        header Access-Control-Expose-Headers "Mcp-Session-Id, Mcp-Protocol-Version"
        reverse_proxy 127.0.0.1:{env.MCP_SERVER_PORT} {
            flush_interval -1
            header_down Cache-Control "no-cache, no-transform"
            header_down X-Accel-Buffering "no"
        }
    }

    handle {
        respond "Not Found" 404
    }
}
```

---

## 4. 生产运维命令速查 (`manage.sh`)

生产服务器已预置管理脚本 `/home/ubuntu/caddy/manage.sh`，支持服务生命周期一键管理：

```bash
# 1. 查看 Caddy 与 Python MCP 服务的运行状态与 PID
/home/ubuntu/caddy/manage.sh status

# 2. 重启全部服务 (平滑重启 Caddy 并拉起最新 server.py 进程)
/home/ubuntu/caddy/manage.sh restart

# 3. 仅热重载 Caddy 配置文件 (零断流)
/home/ubuntu/caddy/manage.sh reload

# 4. 实时查看 MCP 服务业务日志
/home/ubuntu/caddy/manage.sh logs

# 5. 停止服务
/home/ubuntu/caddy/manage.sh stop
```

---

## 5. 跨机部署与同步命令 (从本地 Mac 同步至 Azure VM)

### 5.1 部署凭据
> 详见本地私有运维文档 `DEPLOYMENT_VM.local.md`（公共模板参考 `DEPLOYMENT_VM.example.md`）。
* 目标主机：`<USERNAME>@<YOUR_SERVER_IPV6>`
* SSH 密钥：`<YOUR_SSH_KEY_PATH>`

### 5.2 推荐标准增量同步流程
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

# 线上集成回归（需真实 Key）
python caddy/mcp-server/scripts/live/verify_image_mcp.py
```

---

## 6. 核心开发与业务逻辑参考

如需深入开发生图/识图调度逻辑、修改 Web 控制台微模块或新增独立适配器，请直接查阅：
👉 [**KHC Image MCP Suite 核心开发与接手维护全景指南 (caddy/mcp-server/README.md)**](./mcp-server/README.md)
