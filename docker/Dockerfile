# syntax=docker/dockerfile:1
#
# free-stockdb 单镜像 0.5.0 — stockdb 服务端 + 数据更新同步器 + webui 运维面板（一容器）
#
# 内容：
#   - stockdb 服务端（7899 HTTP 行情库）
#   - 数据更新 同步器（webui 网页一键同步时调用）
#   - pybao 写库客户端（webui mydb 私有存储 / 港股落盘）
#   - webui 运维面板（8080：同步管理 / 健康监控 / 私有存储 / AI 查询 /mcp 路由）
#
# 架构说明（按 TARGETARCH 选基础镜像，保证 pybao 的 stockdb.abi3.so 两架构都能加载）：
#   - amd64 → python:3.14-slim（Debian/glibc）：匹配 manylinux-x64 发布包的 pybao
#     （glibc 构建，NEEDED libgcc_s.so.1/libc6 等，musl 下根本无法加载）。
#   - arm64 → python:3.14-alpine（musl）：匹配 alpine-arm64 发布包的 pybao
#     （musl 构建，NEEDED libgcc_s.so.1 与无版本号 libc.so，需补 libgcc 包 + libc.so 软链）。
#   - stockdb 服务端与同步器为静态链接二进制，与 libc 族无关，两架构基础镜像均可运行。
#   - Python 3.14 GIL：上游 3.14t(free-threaded) 的 abi3t 变体实测 ABI 不匹配
#     （_PyType_FromSlots 符号缺失），待上游修复后评估升级。

ARG VERSION=0.3.1
ARG TARGETARCH

# ================= 构建阶段：下载 stockdb 发布包（含服务端/同步器/pybao） =================
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
    test -x "/out/stockdb/stockdb" || { echo "stockdb server missing" >&2; ls -la /out/stockdb; exit 1; }; \
    test -x "/out/stockdb/数据更新" || { echo "sync binary missing" >&2; ls -la /out/stockdb; exit 1; }; \
    test -d "/out/stockdb/pybao" || { echo "pybao missing" >&2; ls -la /out/stockdb; exit 1; }

# ================= 运行时：amd64 用 Debian(glibc)，匹配 manylinux-x64 的 pybao =================
FROM python:3.14-slim AS runtime-amd64

# bash：极空间等容器管理 APP 进容器默认执行 /bin/bash，Debian slim 默认没有需显式装；
# libgcc-s1：提供 pybao 依赖的 libgcc_s.so.1；ca-certificates 供 curl 走 HTTPS。
RUN apt-get update && apt-get install -y --no-install-recommends \
        tzdata curl bash libgcc-s1 ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# ================= 运行时：arm64 保留 alpine(musl)，补 libgcc + libc.so 软链 =================
FROM python:3.14-alpine AS runtime-arm64

# bash：极空间等容器管理 APP 进容器默认执行 /bin/bash，alpine 默认没有需显式装
# libgcc：提供 pybao 依赖的 libgcc_s.so.1；libc.so 软链：musl 动态库在
# /lib/ld-musl-aarch64.so.1（既是 ELF 解释器也充当 libc），补一个无版本号别名
# 以满足 pybao 里 `NEEDED libc.so` 的查找需求（alpine 默认无 `libc.so` 文件）。
RUN apk add --no-cache tzdata curl bash libgcc \
    && ln -s ld-musl-aarch64.so.1 /lib/libc.so

# ================= 最终阶段：按构建目标架构选中对应基础镜像 =================
FROM runtime-${TARGETARCH}

# ---- stockdb 服务端 + 同步器 + 同步源模板 + pybao（全部来自同一发行包） ----
COPY --from=build /out/stockdb/stockdb /opt/stockdb/stockdb
COPY --from=build /out/stockdb/数据更新 /opt/stockdb/数据更新
COPY --from=build /out/stockdb/sync_url.txt /opt/stockdb/sync_url.txt
COPY --from=build /out/stockdb/pybao /opt/stockdb/pybao
# 发行版自带 stockdb.conf 是 127.0.0.1 原版且 conf 必须命令行传入；
# 容器内统一用 /etc/stockdb/stockdb.conf 模板（0.0.0.0 + 卷内 pidfile/log），
# 删掉自带的避免混淆。模板由 entrypoint 拷到 /data 卷。
RUN rm -f /opt/stockdb/stockdb.conf
COPY stockdb.conf /etc/stockdb/stockdb.conf

# ---- webui 运维面板 + 只读 MCP（app.py 的 /mcp 路由复用其 dispatch，纯标准库） ----
COPY webui/app.py webui/entrypoint.sh /opt/webui/
COPY webui/mcp/ /opt/webui/mcp/
# ---- 模拟盘模块（任务E：paper_core/paper_db/mx_client/paper_engine，与 app.py 同目录分发） ----
COPY webui/paper_core.py webui/paper_db.py webui/mx_client.py webui/paper_engine.py /opt/webui/

# pybao 写库客户端路径注入（webui 的 mydb 私有存储 / 港股落盘）
ENV PYTHONPATH=/opt/stockdb/pybao

WORKDIR /opt/stockdb

EXPOSE 7899 8080

ENTRYPOINT ["/opt/webui/entrypoint.sh"]
