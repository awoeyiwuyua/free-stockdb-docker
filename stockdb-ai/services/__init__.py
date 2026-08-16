"""services — 应用服务层（用例编排，0.9.1 四层架构框架）。

职责（0.9.2 搬迁目标）：
  - auction_collect / auction_close / auction_backfill：打板三用例（拉数据→算→存→降级）
  - sync：数据同步用例

依赖纪律：本层可依赖 core/、storage/、ops/、config；禁止依赖 web/ 与 mcp/（接口层）。
领域规则一律下沉 core/，本层只做编排与降级。

当前状态（0.9.1）：框架占位——用例函数仍住在 app.py（auction_run_collect/close/backfill、
run_sync），随 0.9.2 批次 4 迁入。
"""
