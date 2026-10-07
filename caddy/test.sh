#!/bin/bash
# =============================================
#   Vertex Proxy - 部署验证脚本
#   用法: ./test.sh
#   在 VM 上运行，验证所有组件是否就绪
# =============================================

set -e

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

PASS=0
WARN=0
FAIL=0

pass() { echo -e "  ${GREEN}[PASS]${NC} $1"; ((PASS++)) || true; }
warn() { echo -e "  ${YELLOW}[WARN]${NC} $1"; ((WARN++)) || true; }
fail() { echo -e "  ${RED}[FAIL]${NC} $1"; ((FAIL++)) || true; }

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ENV_FILE="$SCRIPT_DIR/.env"

echo ""
echo "=== Vertex Proxy - Deployment Test ==="
echo ""

# ------------------------------------------------------------------
# 1. System checks
# ------------------------------------------------------------------
echo "[1/6] System checks..."

if command -v python3 &>/dev/null; then
    PY_VER=$(python3 --version 2>&1)
    pass "Python: $PY_VER"
else
    fail "Python3 not found"
fi

# Caddy 检测: systemd 服务 或 自定义路径
CADDY_BIN="${CADDY_BIN:-/home/jjb/bin/caddy}"
if [ -f "$CADDY_BIN" ]; then
    CADDY_VER=$("$CADDY_BIN" version 2>&1)
    pass "Caddy: $CADDY_VER ($CADDY_BIN)"
elif command -v caddy &>/dev/null; then
    CADDY_VER=$(caddy version 2>&1)
    pass "Caddy: $CADDY_VER"
else
    fail "Caddy not found"
fi

if [ -d /etc/systemd/system ] && systemctl is-active --quiet caddy 2>/dev/null; then
    pass "Caddy service is running (systemd)"
elif [ -f "$SCRIPT_DIR/caddy.pid" ] && kill -0 "$(cat "$SCRIPT_DIR/caddy.pid")" 2>/dev/null; then
    pass "Caddy is running (manual, PID $(cat "$SCRIPT_DIR/caddy.pid"))"
elif ss -tlnp 2>/dev/null | grep -q ':3000 '; then
    pass "Caddy is running (port 3000 active)"
else
    warn "Caddy is not running (will be started by setup)"
fi

echo ""

# ------------------------------------------------------------------
# 2. File checks
# ------------------------------------------------------------------
echo "[2/6] File checks..."

for f in .env Caddyfile manage.sh reload-caddy.sh mcp-server/server.py mcp-server/requirements.txt; do
    if [ -f "$SCRIPT_DIR/$f" ]; then
        pass "$f exists"
    else
        fail "$f MISSING"
    fi
done

# Check .env permissions (should be 600 for security)
if [ -f "$ENV_FILE" ]; then
    PERMS=$(stat -c %a "$ENV_FILE" 2>/dev/null || stat -f %Lp "$ENV_FILE" 2>/dev/null)
    if [ "$PERMS" = "600" ]; then
        pass ".env permissions: $PERMS (secure)"
    else
        warn ".env permissions: $PERMS (recommend chmod 600 .env)"
    fi
fi

echo ""

# ------------------------------------------------------------------
# 3. Configuration checks
# ------------------------------------------------------------------
echo "[3/6] Configuration checks..."

# Source .env
if [ -f "$ENV_FILE" ]; then
    set -a
    source "$ENV_FILE"
    set +a
fi

# Required vars
for var in VERTEX_PROJECT VERTEX_API_KEY CLIENT_AUTH_TOKEN; do
    val="${!var}"
    if [ -n "$val" ] && [ "$val" != "你的Google_API_KEY填这里" ] && [ "$val" != "你的客户端认证Token填这里" ]; then
        pass "$var is set (${val:0:8}...)"
    elif [ -n "$val" ]; then
        fail "$var has placeholder value - please fill in real value"
    else
        fail "$var is empty"
    fi
done

# Optional vars
for var in VERTEX_LOCATION IMAGE_MODEL MCP_SERVER_PORT IMAGE_SAVE_DIR; do
    if [ -n "${!var}" ]; then
        pass "$var = ${!var}"
    else
        warn "$var not set (will use default)"
    fi
done

# MODELS_JSON validation
if [ -n "$MODELS_JSON" ]; then
    if echo "$MODELS_JSON" | python3 -c "import sys,json; json.loads(sys.stdin.read().strip())" 2>/dev/null; then
        pass "MODELS_JSON is valid JSON"
    else
        fail "MODELS_JSON is NOT valid JSON"
    fi
else
    fail "MODELS_JSON is empty"
fi

echo ""

# ------------------------------------------------------------------
# 4. Python dependency checks
# ------------------------------------------------------------------
echo "[4/6] Python dependency checks..."

VENV="$SCRIPT_DIR/mcp-server/.venv"
if [ -f "$VENV/bin/python" ]; then
    pass "Virtual environment exists"

    if "$VENV/bin/python" -c "import httpx; print(f'  httpx {httpx.__version__}')" 2>/dev/null; then
        pass "httpx installed"
    else
        fail "httpx not installed in venv"
    fi

    if "$VENV/bin/python" -c "import mcp.server.fastmcp; print(f'  mcp OK')" 2>/dev/null; then
        pass "mcp[cli] installed"
    else
        fail "mcp[cli] not installed in venv"
    fi
else
    warn "Virtual environment not found (will be created on first start)"
    warn "Run: python3 -m venv $VENV && $VENV/bin/pip install -r mcp-server/requirements.txt"
fi

echo ""

# ------------------------------------------------------------------
# 5. Python syntax & import test
# ------------------------------------------------------------------
echo "[5/6] Python syntax & import test..."

SERVER="$SCRIPT_DIR/mcp-server/server.py"
if [ -f "$SERVER" ]; then
    # Syntax check
    if python3 -m py_compile "$SERVER" 2>/dev/null; then
        pass "server.py syntax OK"
    else
        fail "server.py has syntax errors:"
        python3 -m py_compile "$SERVER" 2>&1 | head -5
    fi

    # Import check (using venv if available)
    PY_CMD="$VENV/bin/python"
    [ ! -f "$PY_CMD" ] && PY_CMD="python3"

    if "$PY_CMD" -c "
import httpx
from mcp.server.fastmcp import FastMCP, Image
print('  imports OK')
" 2>/dev/null; then
        pass "server.py imports OK"
    else
        fail "server.py import error (dependencies missing?)"
    fi
fi

echo ""

# ------------------------------------------------------------------
# 6. Caddy configuration validation
# ------------------------------------------------------------------
echo "[6/6] Caddy configuration validation..."

CADDYFILE="$SCRIPT_DIR/Caddyfile"
if [ -f "$CADDYFILE" ]; then
    pass "Caddyfile exists at $CADDYFILE"

    CADDY_CMD="$CADDY_BIN"
    command -v "$CADDY_CMD" &>/dev/null || CADDY_CMD="caddy"

    if command -v "$CADDY_CMD" &>/dev/null; then
        if "$CADDY_CMD" validate --config "$CADDYFILE" 2>/dev/null; then
            pass "Caddyfile syntax valid"
        else
            fail "Caddyfile syntax invalid:"
            "$CADDY_CMD" validate --config "$CADDYFILE" 2>&1 | head -10
        fi
    else
        warn "Caddy binary not found, skipping syntax check"
    fi
else
    fail "Caddyfile not found at $CADDYFILE"
fi

# Check models.json
if [ -f "$SCRIPT_DIR/models.json" ]; then
    if python3 -c "import json; json.load(open('$SCRIPT_DIR/models.json'))" 2>/dev/null; then
        pass "models.json exists and valid"
    else
        fail "models.json exists but invalid JSON"
    fi
else
    warn "models.json not found (will be created by ./manage.sh reload)"
fi

# Check TLS certs
if [ -f "/home/jjb/.acme.sh/g.jjb0888.cn_ecc/fullchain.cer" ]; then
    pass "TLS certificate exists"
else
    warn "TLS certificate not found (acme.sh will create on first run)"
fi

# Check old image-proxy
if fuser 3001/tcp >/dev/null 2>&1; then
    warn "Old image-proxy still running on port 3001 — should be cleaned up"
else
    pass "No old image-proxy on port 3001"
fi

echo ""

# ------------------------------------------------------------------
# Summary
# ------------------------------------------------------------------
echo "========================================="
echo -e "  Results: ${GREEN}$PASS passed${NC}, ${YELLOW}$WARN warnings${NC}, ${RED}$FAIL failed${NC}"
echo "========================================="
echo ""

if [ "$FAIL" -gt 0 ]; then
    echo "Some checks FAILED. Fix the issues above before deploying."
    exit 1
elif [ "$WARN" -gt 0 ]; then
    echo "All critical checks passed. Some warnings to review."
    echo ""
    echo "Next steps:"
    echo "  1. ./manage.sh setup    # first-time deployment"
    echo "  2. ./manage.sh status   # verify everything is running"
    exit 0
else
    echo "All checks passed! Ready to deploy."
    echo ""
    echo "Next steps:"
    echo "  1. ./manage.sh setup    # first-time deployment"
    echo "  2. ./manage.sh status   # verify everything is running"
    exit 0
fi
