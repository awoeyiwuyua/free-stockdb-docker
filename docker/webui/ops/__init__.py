"""ops — 横切关注点（各层共用，0.9.1 四层架构框架）。

职责（0.9.2 搬迁目标）：
  - alerts：告警中心（现 app.py 的 Alerts/notify_alert）
  - logging：结构化日志（现 app.py 的 log/tail_log）
  - scheduler：调度线程（scheduler_loop / auction_scheduler_loop / ops_watchdog_loop）
  - health：健康/体检（health_status / diag）

依赖纪律：本层可依赖 config、storage；**不承载业务用例**（用例在 services/）；
其他层可依赖本层。

当前状态（0.9.1）：框架占位——各横切件仍住在 app.py，随 0.9.2 批次 2 迁入。
"""
