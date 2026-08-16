"""storage — 基础设施层（数据读写，0.9.1 四层架构框架）。

职责（0.9.2 搬迁目标）：
  - mydb_store：mydb 读写封装（现 app.py 的 mydb_write/read/tables、_auction_series_*）
  - records：日检/运行记录存储（0.9.2 可观测性三件套的落点）
  - 外部访问：stockdb_fetch（引擎 HTTP）、fetch_quotes（腾讯/东财）、pybao 加载

依赖纪律：本层可依赖 ops/、config；禁止依赖 services/、core/、web/、mcp/（业务规则
不进本层；本层不知道"调用者是谁"）。

当前状态（0.9.1）：框架占位——mydb 读写与引擎 HTTP 仍住在 app.py，随 0.9.2 批次 3 迁入。
"""
