// api/status.js — 数据同步/系统域接口封装。
// 学习点：每个域的接口集中在一个模块，页面只 import 需要的函数，后端路径改了只动这里。
import { getJson, postJson } from './http.js'

export const getStatus = () => getJson('/api/status') // 状态总览（进程/同步/覆盖/磁盘/日历）
export const getHealth = () => getJson('/api/health') // 健康卡（最新日期/滞后天数/镜像）
export const getHistory = () => getJson('/api/history') // 同步历史
export const getSchedule = () => getJson('/api/schedule') // 定时计划
// 保存定时：enabled 布尔；times 字符串数组（如 ['08:30','15:30']）；tradingOnly 布尔
export const saveSchedule = (enabled, times, tradingOnly = true) =>
  getJson(`/api/schedule?action=save&enabled=${enabled}&times=${times.join(',')}&trading_only=${tradingOnly}`)
export const getLog = (n = 80) => getJson(`/api/log?n=${n}`) // 同步日志尾部
export const getContainerLogs = (tail = 150) => getJson(`/api/container/logs?tail=${tail}`)
export const restartContainer = () => postJson('/api/container/restart', {}) // 重启 stockdb（危险，需二次确认）
// 启动同步：hot=true 热更新（默认）；hot=false 停服严格模式
export const runSync = (hot = true) => postJson('/api/sync', { hot })
// 港股日K 落盘：codes 代码数组；years 年数
export const hkSync = (codes, years = 2) => postJson('/api/hk/sync', { codes, years })
