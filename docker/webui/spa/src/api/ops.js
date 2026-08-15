// api/ops.js — 运维域接口封装（告警/MCP 观测/版本）。
import { getJson, postJson } from './http.js'

export const getAlerts = (limit = 200) => getJson(`/api/alerts?limit=${limit}`)
export const clearAlerts = () => postJson('/api/alerts/clear', {})
export const getMcpStats = () => getJson('/api/mcp/stats') // 总调用/成功率/avg/p95/by_tool
export const getMcpCalls = (limit = 50) => getJson(`/api/mcp/calls?limit=${limit}`)
export const getVersion = () => getJson('/api/version') // webui/image/upstream/stale/ui_mode
