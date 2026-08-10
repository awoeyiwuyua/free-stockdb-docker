#!/bin/sh
# free-stockdb 容器启动入口
# 职责：数据卷对齐 -> conf 覆盖 -> 启动服务端（前台，随容器生命周期）
set -eu

cd /opt/stockdb

# ---- 1. 数据目录对齐到卷（发行版默认 data 为 work_dir 下 ./data，mydb 为 ./mydb）----
mkdir -p /data /mydb
# 首次启动：把发行包自带的 sync_url.txt 落到卷内一份，便于用户直接编辑 /data/sync_url.txt
if [ ! -f /data/sync_url.txt ]; then
  cp ./sync_url.txt /data/sync_url.txt
fi
ln -sfn /data ./data
ln -sfn /mydb ./mydb

# ---- 2. 应用容器内 conf（监听 0.0.0.0）----
cp /etc/stockdb/stockdb.conf ./stockdb.conf

# ---- 3. 启动服务端（前台）----
exec ./stockdb
