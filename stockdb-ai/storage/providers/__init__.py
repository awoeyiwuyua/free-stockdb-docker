"""storage.providers — 外部数据源适配器（基础设施层，D3 多源抽象）。

应用层不感知数据从哪来；文件边界按 provider 划分——
  - free_stockdb.py：上游引擎（HTTP 闸口：熔断 + 信号量；0.10.0 C1 起 MCP 查询
    也经此闸口）
  - quote_sources.py：公网行情源（腾讯/东财竞价快照采集，D11 采集执行归数据层）
  - mydb_store.py：引擎私有 KV 读写（0.9.5 后职责 = hk日k + 用户自定义表 +
    RESEARCH_STORE=mydb 回滚写回；研究成果主线已迁自持 SQLite）
"""
