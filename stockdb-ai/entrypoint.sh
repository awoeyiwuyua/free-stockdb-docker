#!/bin/sh
# free-stockdb 单镜像（0.5.0）容器入口：
#   1. 数据卷准备 + conf/sync_url 模板落卷
#   2. 可选首次同步（STOCKDB_SYNC_FIRST=1）
#   3. 后台监督 stockdb 进程存活（/data/.stockdb-paused 存在时不拉起——
#      webui 停服同步/进程重启通过 pidfile SIGTERM + 写/删暂停标记协调）
#   4. 前台循环拉起 webui（webui 崩溃 3s 后自动重启，不连累 stockdb）
set -eu

# ---- 1. 数据卷准备（发行版硬编码 /data 行情、/mydb 私有库）----
mkdir -p /data /mydb

# ---- 2. conf / sync_url 模板落卷（可写 + 用户可改）----
if [ ! -f /data/stockdb.conf ]; then
  cp /etc/stockdb/stockdb.conf /data/stockdb.conf
fi
if [ ! -f /data/sync_url.txt ]; then
  cp /opt/stockdb/sync_url.txt /data/sync_url.txt
fi

# ---- 3. 可选：首次同步（服务未启动，天然满足「同步须停服务」）----
# 同步失败不阻塞启动（数据保持上一次状态），避免容器重启循环。
if [ "${STOCKDB_SYNC_FIRST:-0}" = "1" ]; then
  echo "[entrypoint] STOCKDB_SYNC_FIRST=1: running incremental sync (数据更新)..."
  if (cd /data && /opt/stockdb/数据更新); then
    echo "[entrypoint] sync finished"
  else
    echo "[entrypoint] WARNING: sync failed; starting anyway (data may be stale)" >&2
  fi
fi

# ---- 4. stockdb 监督子进程：存活保持（暂停标记存在时停着）----
(
  while :; do
    if [ ! -f /data/.stockdb-paused ]; then
      PID="$(cat /data/stockdb.pid 2>/dev/null || true)"
      if [ -n "$PID" ] && kill -0 "$PID" 2>/dev/null; then
        # 0.9.11：kill -0 只能证明"有进程"——容器 PID 空间小，stockdb 崩溃后其
        # PID 可能被 5s 窗口内重启的 webui python/sleep 子进程复用 → 误报存活
        # 永不重启。校验 /proc/<pid>/exe 是否指向 stockdb 二进制本体（Linux）。
        EXE="$(readlink "/proc/$PID/exe" 2>/dev/null || true)"
        case "$EXE" in
          */stockdb) : ;;  # 身份匹配：确为 stockdb
          *) echo "[entrypoint] stale pid $PID (exe=$EXE)，重启 stockdb ..." >&2
             kill "$PID" 2>/dev/null || true
             PID="";;
        esac
      else
        PID=""
      fi
      if [ -z "$PID" ]; then
        echo "[entrypoint] starting stockdb ..."
        (cd /data && exec /opt/stockdb/stockdb /data/stockdb.conf) &
        echo "$!" > /data/stockdb.pid
      fi
    fi
    sleep 5
  done
) &
STOCKDB_WATCHER=$!

# ---- 5. 前台循环拉起 webui（webui 退出自动重启，容器存活由它保持）----
# 注意：set -e 下循环体内的 python 失败会直接退出 entrypoint（容器随 webui 一起停），
# 必须用 if 条件位置豁免——webui 崩溃（含被信号杀死）时循环继续重启，不影响 stockdb。
echo "[entrypoint] starting webui (8080) ..."
while :; do
  if python /opt/webui/app.py; then
    echo "[entrypoint] webui exited (code 0); restarting in 3s ..." >&2
  else
    echo "[entrypoint] webui exited (code $?); restarting in 3s ..." >&2
  fi
  sleep 3
done
