// nav.js — 左侧导航唯一配置源（纯数据，可单测）。
// Phase 5.1（LuCI 经验版）：菜单树 = 总览单页 + 两个分组（系统运维 / 模拟盘），
// 每项一个职责、一条 URL；badge 字段挂全局 store 红点徽标。
export const TOP_ITEMS = [
  { path: '/overview', title: '总览', icon: 'Odometer' },
]

export const NAV_GROUPS = [
  {
    title: '系统运维',
    icon: 'Setting',
    items: [
      { path: '/ops/sync', title: '数据同步', icon: 'Refresh' },
      { path: '/ops/mydb', title: '私有存储', icon: 'Coin' },
      { path: '/ops/health', title: '系统健康', icon: 'Cpu' },
      { path: '/ops/diag', title: '诊断中心', icon: 'Aim' },
      { path: '/ops/logs', title: '日志中心', icon: 'Document' },
      { path: '/ops/alerts', title: '通知中心', icon: 'Bell', badge: 'alertCount' },
      { path: '/ops/mcp', title: 'MCP 观测', icon: 'Monitor' },
    ],
  },
  {
    title: '模拟盘',
    icon: 'Wallet',
    items: [
      { path: '/paper', title: '模拟盘', icon: 'TrendCharts' },
      { path: '/paper/audit', title: '审计报告', icon: 'DocumentChecked' },
      { path: '/paper/signal', title: '信号体检', icon: 'DataAnalysis' },
    ],
  },
]

// 展平：全部页面（含分组信息，测试保证 path 唯一）
export const NAV_ITEMS = [
  ...TOP_ITEMS,
  ...NAV_GROUPS.flatMap((g) => g.items.map((it) => ({ ...it, group: g.title }))),
]

// 旧路径 → 新地址（路由 redirect 兜底，老书签不 404）
export const LEGACY_REDIRECTS = {
  '/data/sync': '/ops/sync',
  '/data/mydb': '/ops/mydb',
  '/ops/system': '/ops/health',
  '/ops/version': '/overview',
  '/data': '/ops/sync',
  '/alerts': '/ops/alerts',
}
