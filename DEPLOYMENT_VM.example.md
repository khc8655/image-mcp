# KHC Image MCP Suite - 云端生产运维与部署参考模板

> 提示：本文件为通用部署示例模板。实际生产环境私有配置（如特定 VM IP、私钥路径、管理令牌）应配置在本地忽略文件 `DEPLOYMENT_VM.local.md` 或环境变量中。

---

## 1. 生产服务器部署示例

* **云平台参考**：Azure VM / AWS EC2 / 腾讯云 CVM / 阿里云 ECS
* **推荐系统**：Ubuntu 22.04 / 24.04 LTS
* **网络与访问**：
  * 对外入口端口：`3000`（Caddy 反向代理，配置 SSL 证书或 CDN 回源）
  * 内部后端端口：`3002`（Python ASGI 监听，仅绑定 `127.0.0.1`）
* **SSH 登录**：
  ```bash
  ssh -i <YOUR_SSH_KEY_PATH> <USERNAME>@<YOUR_SERVER_IP>
  ```

---

## 2. 远端服务目录布局

```text
/home/ubuntu/caddy/
├── Caddyfile              # Caddy 反向代理网关配置
├── manage.sh              # 统一进程守护与启停脚本
├── .env                   # 运行时环境变量
└── mcp-server/            # Python ASGI 核心服务目录
    ├── server.py          # 主服务
    ├── config.json        # 运行时持久化配置 (基于 config.example.json 初始化)
    ├── presets/           # 独立适配器规则库
    ├── templates/         # HTML 控制台模板
    └── static/            # 静态资源与图片持久化目录
```

---

## 3. 服务启停与同步示例

```bash
# 重启服务
ssh -i <YOUR_SSH_KEY_PATH> <USERNAME>@<YOUR_SERVER_IP> "/home/ubuntu/caddy/manage.sh restart"

# 查看状态
ssh -i <YOUR_SSH_KEY_PATH> <USERNAME>@<YOUR_SERVER_IP> "/home/ubuntu/caddy/manage.sh status"

# 查看日志
ssh -i <YOUR_SSH_KEY_PATH> <USERNAME>@<YOUR_SERVER_IP> "tail -f /home/ubuntu/caddy/mcp-server/mcp.log"
```
