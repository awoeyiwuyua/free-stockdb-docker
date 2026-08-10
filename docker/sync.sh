#!/bin/sh
# free-stockdb 数据同步（增量，可反复运行直至无新文件）
#
# 注意：发行版要求"同步期间应停止服务"（官方 DATA_SOURCE.md）。请通过
# compose 的 sync profile 运行：`docker compose --profile sync run --rm stockdb-sync`，
# 不要在 stockdb 服务运行时手动在容器里执行本脚本。
set -eu

cd /opt/stockdb

mkdir -p /data
ln -sfn /data ./data

# 同步源：默认 /data/sync_url.txt（entrypoint 已把发行版默认源落盘），
# 可通过 STOCKDB_SYNC_CONFIG 环境变量指定其他文件。
CONFIG="${STOCKDB_SYNC_CONFIG:-/data/sync_url.txt}"
if [ -f "${CONFIG}" ]; then
  cp "${CONFIG}" ./sync_url.txt
  echo "sync source: ${CONFIG} ($(grep -v '^#' "${CONFIG}" | grep -v '^$' | head -n 2 | tr '\n' ' '))"
else
  echo "warning: ${CONFIG} not found, using bundled sync_url.txt"
fi

# 额外参数（--sync / --verify 等）由 STOCKDB_SYNC_ARGS 传入；默认直接运行更新器
exec ./数据更新 ${STOCKDB_SYNC_ARGS:-}
