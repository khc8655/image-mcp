#!/bin/sh
# =============================================
#   证书续期钩子 - acme.sh --renew-hook 调用
#   续期后重启 Caddy 加载新证书
#   兼容 systemd / 手动两种 Caddy 运行模式
# =============================================

ENV_FILE="/home/jjb/caddy/.env"
CADDY_PID_FILE="/home/jjb/caddy/caddy.pid"

# 加载 .env (获取 CADDY_BIN 等变量)
if [ -f "$ENV_FILE" ]; then
    set -a
    . "$ENV_FILE"
    set +a
fi

CADDY_BIN="${CADDY_BIN:-/home/jjb/bin/caddy}"

LOG="/home/jjb/caddy/reload.log"
echo "$(date '+%Y-%m-%d %H:%M:%S') Cert renewed, reloading Caddy..." > "$LOG"

# 检测 Caddy 运行模式
if [ -d /etc/systemd/system ] && systemctl is-active --quiet caddy 2>/dev/null; then
    # systemd 模式
    sudo systemctl restart caddy >> "$LOG" 2>&1
    echo "$(date '+%Y-%m-%d %H:%M:%S') Caddy restarted (systemd)" >> "$LOG"
else
    # 手动模式: 杀旧进程 → 启动新进程
    if [ -f "$CADDY_PID_FILE" ]; then
        OLD_PID=$(cat "$CADDY_PID_FILE")
        kill "$OLD_PID" 2>/dev/null || true
        sleep 1
    fi
    # 兜底: 按端口杀
    if fuser 3000/tcp >/dev/null 2>&1; then
        fuser -k 3000/tcp >/dev/null 2>&1 || true
        sleep 1
    fi
    # 启动 (source .env 确保 Caddy 能读到环境变量)
    . "$ENV_FILE"
    nohup "$CADDY_BIN" run --config /home/jjb/caddy/Caddyfile >> "$LOG" 2>&1 &
    echo $! > "$CADDY_PID_FILE"
    echo "$(date '+%Y-%m-%d %H:%M:%S') Caddy restarted (manual, PID $!)" >> "$LOG"
fi
