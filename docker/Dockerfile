# syntax=docker/dockerfile:1
#
# free-stockdb 容器镜像 — 多架构（amd64 / arm64）
#
# 构建策略：
#   - 官方发布包是静态链接的独立二进制（服务端 stockdb + 更新器 数据更新），
#     无需源码编译，直接从 GitHub Releases 下载并校验 SHA256。
#   - 用 buildx 的 TARGETARCH 在构建期自动匹配平台：
#         amd64 -> free-stockdb-manylinux-x64-*.tar（glibc 静态）
#         arm64 -> free-stockdb-alpine-arm64-*.tar（musl 静态）
#   - 运行时用 alpine 提供 shell/tzdata（entrypoint、时区），二进制零依赖。
#
# 构建示例（在 docker/ 目录，由 .github/workflows/build-image.yml 自动执行）：
#   docker buildx build --platform linux/amd64,linux/arm64 \
#     -t ghcr.io/awoeyiwuyua/free-stockdb:0.3.1 --push .

ARG VERSION=0.3.1

# ================= 构建阶段：下载 + SHA256 校验 + 解包 =================
FROM alpine:3.20 AS build

ARG VERSION
# buildx 自动注入：amd64 / arm64（Docker 平台架构，非 uname）
ARG TARGETARCH
# 上游 release tag（中文 tag 的 URL 编码形式）
ARG GH_TAG_ENCODED=%E6%B5%8B%E8%AF%95%E7%89%88%E6%9C%AC0.3.1

RUN apk add --no-cache curl tar

# 平台映射 + 对应发布包 SHA256（官方 .SHA256.txt，构建期强制校验）
RUN set -eu; \
    case "${TARGETARCH}" in \
      amd64) \
        PLATFORM=manylinux-x64; \
        SHA256=953e2b1efcd06cee8968f14dcb699cd9bd7142897761cc9f6656d7d9364ea09d ;; \
      arm64) \
        PLATFORM=alpine-arm64; \
        SHA256=2799f4a61830868bf6f856942f3dbda67bba4aa51dab5e4fd0b848952269f66b ;; \
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
    test -x "/out/stockdb/stockdb" && test -x "/out/stockdb/数据更新" || { \
      echo "release package layout unexpected" >&2; ls -la /out/stockdb; exit 1; }

# ================= 运行时阶段 =================
FROM alpine:3.20

RUN apk add --no-cache tzdata ca-certificates

COPY --from=build /out/stockdb /opt/stockdb
# 发行版自带 stockdb.conf 是 127.0.0.1 原版且 conf 必须命令行传入；
# 容器内统一用 /etc/stockdb/stockdb.conf 模板（0.0.0.0 + 卷内 pidfile/log），
# 删掉自带的避免混淆。
RUN rm -f /opt/stockdb/stockdb.conf

# 容器内监听必须 0.0.0.0 才能对外映射；conf 模板由 entrypoint 拷到 /data 卷
COPY stockdb.conf /etc/stockdb/stockdb.conf
COPY entrypoint.sh sync.sh /opt/stockdb/

WORKDIR /opt/stockdb

EXPOSE 7899

ENTRYPOINT ["/opt/stockdb/entrypoint.sh"]
