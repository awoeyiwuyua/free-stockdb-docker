# syntax=docker/dockerfile:1
#
# free-stockdb webui 容器 — 自建 UI（行情查询 + 同步管理）
#
# 功能（纯 Python 标准库，零第三方依赖）：
#   - 行情查询：调 stockdb 的 7899 HTTP API（日K/分钟K/复权）
#   - 同步管理：挂载 docker socket + /data 卷，网页按钮一键完成
#     「停 stockdb → 容器内跑数据更新（增量同步）→ 重启 stockdb」
#   - 日志查看：同步过程实时输出 /data/sync.log
#
# 与上游零冲突：独立于 free-stockdb 源码，只通过 7899 HTTP 与 docker 交互。

ARG VERSION=0.3.1

# ================= 构建阶段：下载 stockdb 发布包取「数据更新」同步器 =================
FROM alpine:3.20 AS build

ARG VERSION
ARG TARGETARCH
ARG GH_TAG_ENCODED=%E6%B5%8B%E8%AF%95%E7%89%88%E6%9C%AC0.3.1

RUN apk add --no-cache curl tar

RUN set -eu; \
    case "${TARGETARCH}" in \
      amd64) \
        PLATFORM=manylinux-x64; \
        SHA256=16eef82d88dceb3724a81701872277a115df7c3cac171fe041eb5de64db8e525 ;; \
      arm64) \
        PLATFORM=alpine-arm64; \
        SHA256=eba14a57d6debf4db57070816b30f7e18374b58f08ce134628e2bbcb98f467b3 ;; \
      *) \
        echo "unsupported TARGETARCH: ${TARGETARCH}" >&2; exit 1 ;; \
    esac; \
    PKG="free-stockdb-${PLATFORM}-v${VERSION}-more-power.tar"; \
    echo "downloading ${PKG} (${TARGETARCH})"; \
    curl -fsSL -o /tmp/pkg.tar \
      "https://github.com/hello245m/free-stockdb/releases/download/${GH_TAG_ENCODED}/${PKG}"; \
    echo "${SHA256}  /tmp/pkg.tar" | sha256sum -c -; \
    mkdir -p /out; \
    tar -xf /tmp/pkg.tar -C /out; \
    test -x "/out/stockdb/数据更新" || { echo "sync binary missing" >&2; exit 1; }

# ================= 运行时阶段 =================
FROM python:3.12-alpine

# bash：极空间等容器管理 APP 进容器默认执行 /bin/bash，alpine 默认没有会导致
# "OCI runtime exec failed: /bin/bash not found"。装上后用 APP 终端可正常进入。
RUN apk add --no-cache tzdata curl bash

# 同步器二进制（webui 容器内直接跑同步；/data 卷挂载后即可读写行情库）
COPY --from=build /out/stockdb/数据更新 /opt/stockdb/数据更新
# 同步源模板（entrypoint 拷到 /data 供编辑）
COPY --from=build /out/stockdb/sync_url.txt /opt/stockdb/sync_url.txt

COPY app.py entrypoint.sh /opt/webui/
COPY static/ /opt/webui/static/

WORKDIR /opt/webui
EXPOSE 8080

ENTRYPOINT ["/opt/webui/entrypoint.sh"]
