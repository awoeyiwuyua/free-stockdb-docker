"""web — 接口层（HTTP 侧门面，0.9.1 四层架构框架）。

职责（0.9.2 搬迁目标）：
  - routes.py：URL → handler 映射表
  - handlers.py：各端点处理——只做收参数/校验/调服务层/组装响应（信封/错误码）

依赖纪律：本层可依赖 services/、core/、storage/、ops/；禁止反向（其他层不得 import 本层）。

当前状态（0.9.1）：框架占位——HTTP 路由仍住在 app.py（Handler），随 0.9.2 批次 6 迁入。
"""
