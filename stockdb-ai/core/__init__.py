"""core — 领域层（核心规则/指标，0.9.1 四层架构框架）。

职责（0.9.2 搬迁目标）：
  - board_metrics：涨停判定/一字板/溢价口径（0.8.x 异源验收签字核心）
  - auction_metrics：分位/强弱标签；auction_list：清单口径
  - calendar_xshg：A 股交易日历

依赖纪律（最严格）：本层为纯规则——**不 import 任何其他层**（web/services/storage/ops/mcp
一律禁止）；不碰网络/文件/数据库；输入输出为纯数据结构，可独立测试。

当前状态（0.9.1）：框架占位——领域模块仍住在 mcp/（board_metrics.py、auction_metrics.py、
auction_list.py、calendar_xshg.py），随 0.9.2 批次 5 归位。
"""
