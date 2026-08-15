// nav.js — 左侧导航唯一配置源（纯数据，可单测）。
// 路由表与侧边栏都从这里生成：改导航 = 改这一个文件。
// 学习点：单一数据源（single source of truth）——避免两处配置不同步。

export const NAV_GROUPS = [
  {
    title: '总览',
    items: [
      { path: '/overview', title: '总览', icon: 'Odometer' },
    ],
  },
  {
    title: '数据',
    items: [
      { path: '/data/sync', title: '数据同步', icon: 'Refresh' },
      { path: '/data/mydb', title: '私有存储', icon: 'Coin' },
    ],
  },
  {
    title: '模拟盘',
    items: [
      { path: '/paper', title: '模拟盘', icon: 'TrendCharts' },
      { path: '/paper/audit', title: '审计报告', icon: 'DocumentChecked' },
      { path: '/paper/signal', title: '信号体检', icon: 'FirstAidKit' },
    ],
  },
  {
    title: '运维',
    items: [
      { path: '/ops/system', title: '系统', icon: 'Setting' },
      { path: '/ops/alerts', title: '通知中心', icon: 'Bell' },
      { path: '/ops/mcp', title: 'MCP 观测', icon: 'Monitor' },
      { path: '/ops/version', title: '版本', icon: 'Promotion' },
    ],
  },
]

// 展平：所有页面（path 唯一性由测试保证）
export const NAV_ITEMS = NAV_GROUPS.flatMap((g) =>
  g.items.map((it) => ({ ...it, group: g.title }))
)
