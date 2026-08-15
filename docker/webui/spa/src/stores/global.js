// stores/global.js — 全局状态（Pinia）：顶栏状态条 + 总览页共用的数据仓库。
// 学习点：Pinia = 跨组件共享的响应式仓库；actions 改 state，组件只读。
import { defineStore } from 'pinia'
import { getOverview } from '../api/overview.js'

export const useGlobalStore = defineStore('global', {
  state: () => ({
    overview: null, // /api/overview 全量载荷（health/alerts/paper/mcp/version）
    error: null, // 最近一次刷新错误文案（顶栏降级展示）
    lastRefresh: null, // 最近成功刷新时间（Date）
  }),
  getters: {
    health: (s) => s.overview?.health ?? null,
    paper: (s) => s.overview?.paper ?? null,
    alertCount: (s) => s.overview?.alerts?.count ?? 0,
    mcp: (s) => s.overview?.mcp ?? null,
    version: (s) => s.overview?.version ?? null,
    // 数据滞后天数（health.lag_days，未知视为 null）
    lagDays: (s) => (s.overview?.health?.lag_days ?? null),
  },
  actions: {
    async refresh() {
      try {
        const data = await getOverview()
        this.overview = data
        this.error = null
        this.lastRefresh = new Date()
      } catch (e) {
        this.error = e?.message || '接口不可用'
      }
    },
  },
})
