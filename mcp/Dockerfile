# syntax=docker/dockerfile:1
#
# free-stockdb 只读 HTTP MCP 容器 — 纯 Python 标准库，无 pybao/.so，无需 apk 额外依赖
#
# - 只读 MCP：把 stockdb 的 7899 HTTP API 封装成 5 个 MCP 工具
# - 双传输：默认 HTTP（POST /mcp，GET / 健康检查），也支持 stdio（无 --http 参数）
# - 零第三方依赖：仅 python:3.14-alpine 基础镜像，不装任何额外 apk 包
# - 并发：ThreadingHTTPServer（每请求一线程）+ 重型工具内部 RLock 串行化

FROM python:3.14-alpine

# 只读 MCP server + 打板溢价算法子集（vendor 自命理档案，纯标准库）
COPY stockdb_mcp_server.py board_metrics.py /opt/mcp/

WORKDIR /opt/mcp

EXPOSE 8080

ENTRYPOINT ["python", "/opt/mcp/stockdb_mcp_server.py"]
CMD ["--http", "--host", "0.0.0.0", "--port", "8080"]
