"""storage — 数据层（基础设施：一切落盘与外部数据源的读写，0.9.1 四层架构框架）。

**代码与数据的关系**（0.10.0 治理批明确）：本目录是数据层的**代码**；
数据实际落盘在 DATA_DIR（本机开发 = 仓库根 data/，生产 = /data 卷）——
点开本目录只有 .py，要看 Parquet/SQLite/jsonl 请去 DATA_DIR。

模块清单（按归属分三类）：
  - providers/：外部数据源适配器（文件边界按 provider 划分，D3/D11）
      free_stockdb.py（引擎 HTTP 闸口）、quote_sources.py（腾讯/东财采集）、
      mydb_store.py（引擎私有 KV：hk日k + 自定义表 + 回滚写回）
  - warehouse/：列式仓库子系统（0.10.0 D12）——layout/sink/catalog/engine/
      queries/reconcile；数据落 DATA_DIR/warehouse/（Parquet + DuckDB）
  - research_store.py + research_factory.py：研究成果自持（SQLite WAL，
      落 DATA_DIR/research/research.db，旧根路径粘性兼容）；
      records.py：日检/运行记录（jsonl，落 DATA_DIR/records/）

依赖纪律：本层可依赖 ops/、config + 第三方库（duckdb）；禁止依赖 services/、
core/、interfaces/（业务规则不进本层；本层不知道"调用者是谁"——服务层经注入访问，
见 test_layer_boundaries）。

接口现状（如实描述）：抽象接口仅 ResearchStore（0.9.5 M5 交付，仓储模式）；
行情 provider 尚无统一接口——当前仅一个行情源（free_stockdb），待出现第二个
行情源（如自建源/镜像）再抽象，避免过度设计。
"""
