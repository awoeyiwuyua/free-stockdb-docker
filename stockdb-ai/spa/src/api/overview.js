// api/overview.js — 总览聚合接口封装（/api/overview）。
import { getJson } from './http.js'

// 一次请求拿全：健康/告警/模拟盘/MCP/版本（后端聚合，替代 5 次轮询）
export const getOverview = () => getJson('/api/overview')
