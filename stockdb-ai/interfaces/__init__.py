"""interfaces — 接口层（0.9.8 严格分层：接口层统一收拢）。

对外的一切入口都在本层：
- interfaces/web/ — HTTP 接口（路由 + Handler，程序/脚本取数）
- interfaces/mcp/ — MCP 接口（服务器 + 契约信封 + sdk_bridge + pybao_tools 封装）

依赖铁律：接口层 → 服务层 → 领域层/数据层；本层不承载业务逻辑。
"""
