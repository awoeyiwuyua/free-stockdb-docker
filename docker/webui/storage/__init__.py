"""storage — 基础设施层（数据读写，0.9.1 四层架构框架）。

多源抽象（2026-08-16 用户拍板：上游只是数据层的一部分）：
  - providers/free_stockdb.py：上游引擎（引擎 HTTP + pybao 扩展）——当前唯一行情源
  - providers/mydb_store.py：mydb 读写（研究成果自持：打板指标/序列/清单/快照）
  - records.py：日检/运行记录存储（0.9.2 可观测性三件套的落点）

依赖纪律：本层可依赖 ops/、config；禁止依赖 services/、core/、web/、mcp/（业务规则
不进本层；本层不知道"调用者是谁"）。0.9.2 搬迁只做目录归位与模块拆分（文件边界按
provider 划分），统一数据访问接口随 M5 引入。

当前状态（0.9.1）：框架占位——mydb 读写与引擎 HTTP 仍住在 app.py，随 0.9.2 批次 3 迁入。
"""
