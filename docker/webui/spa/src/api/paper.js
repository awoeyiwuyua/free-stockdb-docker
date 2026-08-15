// api/paper.js — 模拟盘全部接口封装（状态/账户/明细/审计/信号/操作）。
// 隐私注意：apikey 只提交、只展示掩码，前端任何地方不得缓存/回显原文。
import { getJson, postJson } from './http.js'

export const getPaperStatus = () => getJson('/api/paper/status') // 配置/开关/暂停/下次触发/时间轴
export const getPaperOverview = () => getJson('/api/paper/overview') // 余额/持仓/pnl
export const getSnapshots = (limit = 60) => getJson(`/api/paper/snapshot?limit=${limit}`) // 净值曲线
export const getDecisions = (limit = 30) => getJson(`/api/paper/decisions?limit=${limit}`)
export const getOrders = (limit = 500) => getJson(`/api/paper/orders?limit=${limit}`)
export const getEvents = (limit = 200) => getJson(`/api/paper/events?limit=${limit}`)
export const getAudit = () => getJson('/api/paper/audit') // 审计报告（重放/防重/状态机/滑点/净值vs基准）
export const getSignalStatus = () => getJson('/api/paper/signal-status') // 情绪文件 7 项体检
export const setPause = (enabled) => postJson('/api/paper/pause', { enabled }) // enabled=true 暂停
export const runNow = (timepoint) => postJson('/api/paper/run-now', { timepoint }) // 手动单步（7 时点）
export const saveApikey = (apikey) => postJson('/api/paper/apikey', { apikey }) // 空串=清除
export const checkConnectivity = () => postJson('/api/paper/connectivity', {})
