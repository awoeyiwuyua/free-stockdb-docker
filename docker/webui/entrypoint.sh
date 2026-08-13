#!/bin/sh
# webui 容器入口：初始化数据卷内的配置模板后启动 Python HTTP 服务
set -eu

mkdir -p /data

# sync_url.txt 模板落卷（首次启动），用户可后续编辑 /data/sync_url.txt
if [ ! -f /data/sync_url.txt ]; then
  cp /opt/stockdb/sync_url.txt /data/sync_url.txt
fi

exec python /opt/webui/app.py
