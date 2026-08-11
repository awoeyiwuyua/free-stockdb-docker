#!/bin/sh
# free-stockdb 容器启动入口
# 发行版用法：stockdb [-d] /path/to/conf  （conf 必须作为命令行参数）
# 发行版数据路径硬编码为绝对路径：/data（行情）、/mydb（私有库）
set -eu

# ---- 1. 数据卷准备（发行版硬编码 /data /mydb 绝对路径）----
mkdir -p /data /mydb

# ---- 2. 工作目录切到可写卷 ----
# conf 里 pidfile/log 为相对 work_dir 路径（./stockdb.pid、./log.txt），
# 若在 /opt/stockdb（镜像只读层）会写失败导致启动即退出。
cd /data

# ---- 3. conf 落卷（可写 + 用户可改），首次启动从镜像模板拷贝 ----
if [ ! -f /data/stockdb.conf ]; then
  cp /etc/stockdb/stockdb.conf /data/stockdb.conf
fi
# sync_url.txt 同样落卷，便于用户编辑同步源
if [ ! -f /data/sync_url.txt ]; then
  cp /opt/stockdb/sync_url.txt /data/sync_url.txt
fi

# ---- 4. 可选：先同步再启动（极空间无终端友好模式）----
# 设置环境变量 STOCKDB_SYNC_FIRST=1 时，容器启动先执行增量同步，
# 同步完成后再启动服务。此时服务尚未启动，天然满足发行版"同步须停服务"。
# 用于：首次部署 / 日常更新数据（图形界面改 env -> 重启容器即可）。
# 同步失败不阻塞服务启动（数据保持上一次状态），避免容器重启循环。
if [ "${STOCKDB_SYNC_FIRST:-0}" = "1" ]; then
  echo "[entrypoint] STOCKDB_SYNC_FIRST=1: running incremental sync (数据更新)..."
  if /opt/stockdb/数据更新; then
    echo "[entrypoint] sync finished"
  else
    echo "[entrypoint] WARNING: sync failed; starting server anyway (data may be stale)" >&2
  fi
fi

# ---- 5. 启动服务端（前台，带 conf 绝对路径）----
exec /opt/stockdb/stockdb /data/stockdb.conf
