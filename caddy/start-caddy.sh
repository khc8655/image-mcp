#!/bin/sh
# 开机自启: source .env → 启动 Caddy
. /home/jjb/caddy/.env
exec /home/jjb/bin/caddy run --config /home/jjb/caddy/Caddyfile
