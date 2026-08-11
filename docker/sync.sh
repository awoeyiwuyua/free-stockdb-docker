#!/bin/sh
# free-stockdb 数据同步（增量，可反复运行直至无新文件）
# 同步器读当前目录下的 sync_url.txt（首行数据源）与 stockdb.conf。
# 注意：发行版要求"同步期间应停止服务"（官方 DATA_SOURCE.md），
# 请通过 compose 的 sync profile 运行，不要在 stockdb 服务运行时执行。
set -eu

# ---- 工作目录切到 /data（可写卷）----
cd /data

# ---- 同步源配置落盘（首次从镜像拷贝，之后可编辑 /data/sync_url.txt）----
if [ ! -f /data/sync_url.txt ]; then
  cp /opt/stockdb/sync_url.txt /data/sync_url.txt
fi

echo "sync source: $(grep -vE '^#|^$' /data/sync_url.txt | head -n 2 | tr '\n' ' ')"

# ---- 运行更新器（发行版二进制名：数据更新）----
# 额外参数（-run HH:MM:SS 定时等）通过 STOCKDB_SYNC_ARGS 传入
exec /opt/stockdb/数据更新 ${STOCKDB_SYNC_ARGS:-}
