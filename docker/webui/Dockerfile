# syntax=docker/dockerfile:1
#
# free-stockdb webui 容器 — 自建 UI（行情查询 + 同步管理 + mydb 私有写入 + 港股）
#
# 功能（纯 Python 标准库 + pybao 写库，零 pip 第三方依赖）：
#   - 行情查询：调 stockdb 的 7899 HTTP API（日K/分钟K/复权）
#   - 同步管理：挂载 docker socket + /data 卷，网页按钮一键完成
#     「停 stockdb → 容器内跑数据更新（增量同步）→ 重启 stockdb」
#   - mydb 私有写入：pybao 客户端连 7899 执行 rd.set/mset（自定义表/港股落盘）
#   - 港股研究：东财/腾讯实时拉取 + 写入 hk日k: 表
#
# 架构说明：
#   - 基础镜像 python:3.14-alpine（musl）：arm64 发布包的 pybao 是 musl 构建
#     （依赖 libc.so），NAS(arm64) 可直接加载；amd64 发布包 pybao 是 glibc 构建，
#     在 alpine 下加载失败时 webui 优雅降级（mydb 写入不可用，A 股功能不受影响）。
#   - Python 3.14 GIL：上游 3.14t(free-threaded) 的 abi3t 变体实测 ABI 不匹配
#     （_PyType_FromSlots 符号缺失），待上游修复后评估升级。

ARG VERSION=0.3.1

# ================= 构建阶段：下载 stockdb 发布包取同步器 + pybao =================
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
FROM python:3.14-alpine

# bash：极空间等容器管理 APP 进容器默认执行 /bin/bash，alpine 默认没有会导致
# "OCI runtime exec failed: /bin/bash not found"。装上后用 APP 终端可正常进入。
RUN apk add --no-cache tzdata curl bash

# 同步器二进制（webui 容器内直接跑同步；/data 卷挂载后即可读写行情库）
COPY --from=build /out/stockdb/数据更新 /opt/stockdb/数据更新
# 同步源模板（entrypoint 拷到 /data 供编辑）
COPY --from=build /out/stockdb/sync_url.txt /opt/stockdb/sync_url.txt
# pybao 写库（含 stock_sdk.py + 对应架构 stockdb.abi3.so；PYTHONPATH 注入）
COPY --from=build /out/stockdb/pybao /opt/stockdb/pybao

COPY app.py entrypoint.sh /opt/webui/
COPY static/ /opt/webui/static/

# uv 运行时：统一 Python 版本管理；容器内 uv 仅作启动器（--no-project 用系统 python）
RUN pip install --no-cache-dir uv

ENV PYTHONPATH=/opt/stockdb/pybao

WORKDIR /opt/webui
EXPOSE 8080

ENTRYPOINT ["/opt/webui/entrypoint.sh"]
