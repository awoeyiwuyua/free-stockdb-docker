#!/bin/sh
# webui 本地开发（Mac/任意有 python3 的机器）
#
# 直连远端 stockdb（默认 Tailscale 上的极空间 100.66.1.1:7899），
# 读功能（行情/K线/健康度/自选/状态）完整可测。
# 同步/容器操控依赖 docker socket + /opt/stockdb/数据更新 二进制，
# 只能在 NAS 容器环境验证——本地启动时这些接口会优雅降级（不可用提示）。
#
# 用法：
#   ./dev.sh                      # 默认连 100.66.1.1:7899，端口 8080
#   STOCKDB_HOST=192.168.1.5 ./dev.sh   # 连其他实例
#   WEBUI_PORT=18080 ./dev.sh           # 换本地端口
set -eu

DIR="$(cd "$(dirname "$0")" && pwd)"

: "${STOCKDB_HOST:=100.66.1.1}"
: "${STOCKDB_PORT:=7899}"
: "${WEBUI_PORT:=8080}"
: "${DATA_DIR:=$DIR/.dev-data}"

mkdir -p "$DATA_DIR"
export STOCKDB_HOST STOCKDB_PORT WEBUI_PORT DATA_DIR

echo "→ webui    http://127.0.0.1:${WEBUI_PORT}"
echo "  stockdb  ${STOCKDB_HOST}:${STOCKDB_PORT}"
echo "  本地数据  ${DATA_DIR}（仅自选/历史/日志等落盘，不碰 NAS 数据卷）"
echo "  注意：docker 操控与同步仅在 NAS 容器内可用，本地对应接口返回降级提示"

exec python3 "$DIR/app.py"
