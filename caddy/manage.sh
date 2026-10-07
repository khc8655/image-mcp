#!/bin/bash
# =============================================
#   Vertex Proxy - 管理脚本
#   部署路径: /home/jjb/caddy/
#   用法: ./manage.sh [setup|start|stop|restart|reload|status|logs]
#   自动检测 Caddy 运行模式: systemd (优先) / 手动 (兼容当前服务器)
# =============================================

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ENV_FILE="$SCRIPT_DIR/.env"
PID_FILE="$SCRIPT_DIR/mcp-server/mcp.pid"
LOG_FILE="$SCRIPT_DIR/mcp-server/mcp.log"
CADDY_PID_FILE="$SCRIPT_DIR/caddy.pid"
# Detect Virtual Environment directory
if [ -d "$SCRIPT_DIR/mcp-server/.venv_local" ]; then
    VENV="$SCRIPT_DIR/mcp-server/.venv_local"
else
    VENV="$SCRIPT_DIR/mcp-server/.venv"
fi
SERVER="$SCRIPT_DIR/mcp-server/server.py"
MODELS_JSON_FILE="$SCRIPT_DIR/models.json"



# Load .env
if [ -f "$ENV_FILE" ]; then
    set -a
    source "$ENV_FILE"
    set +a
fi

# Detect Python executable inside venv
if [ -f "$VENV/bin/python3" ]; then
    PYTHON="$VENV/bin/python3"
else
    PYTHON="$VENV/bin/python"
fi
if which caddy >/dev/null 2>&1; then
    CADDY_BIN="$(which caddy)"
else
    CADDY_BIN="${CADDY_BIN:-/usr/bin/caddy}"
fi

# ------------------------------------------------------------------
#   Caddy 模式检测: systemd / 手动
# ------------------------------------------------------------------

caddy_use_systemd() {
    # 同时满足: systemd 可用 + caddy service 已注册
    [ -d /etc/systemd/system ] && \
    systemctl list-unit-files caddy.service &>/dev/null 2>&1
}

caddy_restart() {
    if caddy_use_systemd; then
        echo "[*] Caddy: systemd 模式"
        # 写入 env override
        local override_dir="/etc/systemd/system/caddy.service.d"
        sudo mkdir -p "$override_dir"
        sudo tee "$override_dir/vertex-env.conf" > /dev/null <<CONF
[Service]
EnvironmentFile=$ENV_FILE
CONF
        sudo systemctl daemon-reload
        sudo systemctl restart caddy && echo "[+] Caddy restarted (systemd)" || echo "[-] Caddy restart failed"
    else
        echo "[*] Caddy: 手动模式 ($CADDY_BIN)"
        # 杀掉旧进程
        if [ -f "$CADDY_PID_FILE" ]; then
            local old_pid
            old_pid=$(cat "$CADDY_PID_FILE")
            if kill -0 "$old_pid" 2>/dev/null; then
                kill "$old_pid" 2>/dev/null
                sleep 1
            fi
            rm -f "$CADDY_PID_FILE"
        fi
        # 兜底: 按端口杀
        if fuser 3000/tcp >/dev/null 2>&1; then
            fuser -k 3000/tcp >/dev/null 2>&1 || true
            sleep 1
        fi
        # 启动
        nohup "$CADDY_BIN" run --config "$SCRIPT_DIR/Caddyfile" > "$SCRIPT_DIR/caddy.log" 2>&1 &
        local pid=$!
        echo "$pid" > "$CADDY_PID_FILE"
        sleep 1
        if kill -0 "$pid" 2>/dev/null; then
            echo "[+] Caddy started (PID $pid)"
        else
            echo "[-] Caddy failed to start, check $SCRIPT_DIR/caddy.log"
        fi
    fi
}

caddy_stop() {
    if caddy_use_systemd; then
        sudo systemctl stop caddy 2>/dev/null || true
    fi
    if [ -f "$CADDY_PID_FILE" ]; then
        local old_pid
        old_pid=$(cat "$CADDY_PID_FILE")
        if kill -0 "$old_pid" 2>/dev/null; then
            kill "$old_pid" 2>/dev/null
            sleep 1
        fi
        rm -f "$CADDY_PID_FILE"
    fi
    if fuser 3000/tcp >/dev/null 2>&1; then
        fuser -k 3000/tcp >/dev/null 2>&1 || true
    fi
}

caddy_status() {
    if caddy_use_systemd; then
        echo -n "  Caddy:        "
        if systemctl is-active --quiet caddy 2>/dev/null; then
            echo "running (systemd)"
        else
            echo "stopped"
        fi
    else
        echo -n "  Caddy:        "
        if [ -f "$CADDY_PID_FILE" ] && kill -0 "$(cat "$CADDY_PID_FILE")" 2>/dev/null; then
            echo "running (PID $(cat "$CADDY_PID_FILE"), manual)"
        elif ss -tlnp 2>/dev/null | grep -q ':3000 '; then
            echo "running (port 3000 active, PID unknown)"
        else
            echo "stopped"
        fi
    fi
}

# ------------------------------------------------------------------
#   MCP Server (nohup 管理)
# ------------------------------------------------------------------

mcp_start() {
    if [ -f "$PID_FILE" ]; then
        local old_pid
        old_pid=$(cat "$PID_FILE")
        if kill -0 "$old_pid" 2>/dev/null; then
            echo "[!] MCP Server already running (PID $old_pid)"
            return
        fi
        echo "[!] Removing stale PID file"
        rm -f "$PID_FILE"
    fi

    echo "[*] Starting MCP Server..."

    if [ ! -f "$PYTHON" ]; then
        echo "[*] Creating virtual environment..."
        python3 -m venv "$VENV"
        "$VENV/bin/pip" install -r "$SCRIPT_DIR/mcp-server/requirements.txt"
    fi

    nohup "$PYTHON" "$SERVER" > "$LOG_FILE" 2>&1 &
    local pid=$!
    echo "$pid" > "$PID_FILE"

    sleep 1
    if kill -0 "$pid" 2>/dev/null; then
        echo "[+] MCP Server started (PID $pid)"
        echo "    Port: ${MCP_SERVER_PORT:-3002}"
        echo "    Log:  tail -f $LOG_FILE"
    else
        echo "[-] Failed to start. Last log lines:"
        tail -20 "$LOG_FILE"
        rm -f "$PID_FILE"
        return 1
    fi
}

mcp_stop() {
    if [ ! -f "$PID_FILE" ]; then
        echo "[!] MCP Server not running (no PID file)"
        return
    fi

    local pid
    pid=$(cat "$PID_FILE")

    # Safety: verify PID is actually our python server
    if [ -d "/proc/$pid" ] && ! grep -q "server.py" "/proc/$pid/cmdline" 2>/dev/null; then
        echo "[!] PID $pid is NOT our server (stale PID file), removing"
        rm -f "$PID_FILE"
        return
    fi

    if kill -0 "$pid" 2>/dev/null; then
        echo "[*] Stopping MCP Server (PID $pid)..."
        kill "$pid"
        sleep 2
        if kill -0 "$pid" 2>/dev/null; then
            echo "[*] Force killing..."
            kill -9 "$pid"
        fi
        echo "[+] Stopped"
    else
        echo "[!] Process $pid not found (stale PID file)"
    fi
    rm -f "$PID_FILE"
}



# ------------------------------------------------------------------
#   models.json (从 .env 中的 MODELS_JSON 写入)
# ------------------------------------------------------------------

write_models_json() {
    if [ -n "$MODELS_JSON" ]; then
        echo "$MODELS_JSON" > "$MODELS_JSON_FILE"
        echo "[+] models.json updated"
    else
        echo "[!] MODELS_JSON not set in .env, skipping models.json"
    fi
}

# ------------------------------------------------------------------
#   reload (更新 Caddy 配置)
# ------------------------------------------------------------------

reload_caddy() {
    echo "[*] Updating Caddy configuration..."
    write_models_json
    caddy_restart
}

# ------------------------------------------------------------------
#   setup (首次部署)
# ------------------------------------------------------------------

setup() {
    echo "=== Vertex Proxy - First Time Setup ==="
    echo ""

    # 0. 安全检查: 旧 image-proxy 是否还在运行
    if fuser 3001/tcp >/dev/null 2>&1; then
        echo "[!] 发现旧的 image-proxy 仍在运行 (端口 3001)"
        echo "    建议停止: fuser -k 3001/tcp"
        echo "    并从 crontab 中移除 run-image-proxy.sh 那一行"
        echo ""
    fi

    # 1. Write models.json
    echo "[1/4] Writing models.json..."
    write_models_json

    # 2. 创建图片保存目录
    echo "[2/4] Creating image save directory..."
    mkdir -p "${IMAGE_SAVE_DIR:-/home/jjb/caddy/images}"

    # 3. 启动 Caddy
    echo "[3/4] Starting Caddy..."
    caddy_restart

    # 4. Start MCP Server
    echo "[4/4] Starting MCP Server..."
    mcp_start

    echo ""
    echo "=== Setup Complete ==="
    echo ""
    echo "下一步:"
    echo "  1. 编辑 .env 填入真实的 VERTEX_API_KEY"
    echo "  2. ./manage.sh reload  (重新加载配置)"
    echo "  3. curl -k https://localhost:3000/v1/models -H 'Authorization: Bearer $CLIENT_AUTH_TOKEN'"
    echo ""
    status
}

# ------------------------------------------------------------------
#   restart / status / logs
# ------------------------------------------------------------------

restart() {
    caddy_restart
    mcp_stop
    mcp_start
}

status() {
    echo "=== Vertex Proxy Status ==="
    echo ""

    caddy_status

    # MCP Server
    echo -n "  MCP Server:   "
    if [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
        echo "running (PID $(cat "$PID_FILE"))"
    else
        echo "stopped"
    fi



    echo ""
    echo "  Project:      $VERTEX_PROJECT"
    echo "  Location:     $VERTEX_LOCATION"
    echo "  Image Model:  $IMAGE_MODEL (MCP)"
    echo "  MCP Port:     ${MCP_SERVER_PORT:-3002}"
    echo "  Auth Token:   ${CLIENT_AUTH_TOKEN:0:8}..."
    echo "  API Key:      ${VERTEX_API_KEY:0:8}..."
    echo "  Save Dir:     ${IMAGE_SAVE_DIR:-/home/jjb/caddy/images}"
    echo ""
}

logs() {
    if [ -f "$LOG_FILE" ]; then
        tail -f "$LOG_FILE"
    else
        echo "[!] No log file found at $LOG_FILE"
    fi
}

start_all() {
    caddy_restart
    mcp_start
}

stop_all() {
    caddy_stop
    mcp_stop
}

case "${1:-status}" in
    setup)   setup      ;;
    start)   start_all  ;;
    stop)    stop_all   ;;
    restart) restart    ;;
    reload)  reload_caddy ;;
    status)  status     ;;
    logs)    logs       ;;
    *)
        echo "Usage: $0 {setup|start|stop|restart|reload|status|logs}"
        echo ""
        echo "  setup   - 首次部署: 启动 Caddy + MCP Server"
        echo "  start   - 启动所有后台服务 (MCP)"
        echo "  stop    - 停止所有后台服务 (MCP)"
        echo "  restart - 重启所有后台服务 (MCP)"
        echo "  reload  - 重新加载 Caddy + models.json"
        echo "  status  - 查看整体服务运行状态"
        echo "  logs    - 查看 MCP Server 日志"
        exit 1
        ;;
esac
