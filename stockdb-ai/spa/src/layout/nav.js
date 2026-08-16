// nav.js — 左侧导航唯一配置源（纯数据，可单测）。
// Phase 5.1（LuCI 经验版）→ 0.8.0 收敛：菜单树 = 总览单页 + 一个分组（系统运维），
// 每项一个职责、一条 URL；badge 字段挂全局 store 红点徽标。模拟盘分组已于 0.8.0 移除。
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
]

// 展平：全部页面（含分组信息，测试保证 path 唯一）
export const NAV_ITEMS = [
  ...TOP_ITEMS,
  ...NAV_GROUPS.flatMap((g) => g.items.map((it) => ({ ...it, group: g.title }))),
]

// 旧路径 → 新地址（路由 redirect 兜底，老书签不 404）
// 0.8.0：模拟盘三个旧路径（/paper、/paper/audit、/paper/signal）全部收敛到总览
export const LEGACY_REDIRECTS = {
  '/data/sync': '/ops/sync',
  '/data/mydb': '/ops/mydb',
  '/ops/system': '/ops/health',
  '/ops/version': '/overview',
  '/data': '/ops/sync',
  '/alerts': '/ops/alerts',
  '/paper': '/overview',
  '/paper/audit': '/overview',
  '/paper/signal': '/overview',
}
