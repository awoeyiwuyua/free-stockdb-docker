// router/index.js — 路由表 = 左侧导航结构（Phase 5 IA 四组九页）。
// 学习点：路由 meta 携带页面标题；afterEach 统一改浏览器标签标题；
// history 模式让每个页面有独立 URL（刷新/收藏/前进后退都正常）。
import { createRouter, createWebHistory } from 'vue-router'
import PlaceholderView from '../views/PlaceholderView.vue'

const routes = [
  { path: '/', redirect: '/overview' },

  // 总览
  { path: '/overview', component: PlaceholderView, meta: { title: '总览' } },

  // 数据
  { path: '/data/sync', component: PlaceholderView, meta: { title: '数据同步' } },
  { path: '/data/mydb', component: PlaceholderView, meta: { title: '私有存储' } },

  // 模拟盘
  { path: '/paper', component: PlaceholderView, meta: { title: '模拟盘' } },
  { path: '/paper/audit', component: PlaceholderView, meta: { title: '审计报告' } },
  { path: '/paper/signal', component: PlaceholderView, meta: { title: '信号体检' } },

  // 运维
  { path: '/ops/system', component: PlaceholderView, meta: { title: '系统' } },
  { path: '/ops/alerts', component: PlaceholderView, meta: { title: '通知中心' } },
  { path: '/ops/mcp', component: PlaceholderView, meta: { title: 'MCP 观测' } },
  { path: '/ops/version', component: PlaceholderView, meta: { title: '版本' } },
]

const router = createRouter({
  history: createWebHistory(),
  routes,
})

router.afterEach((to) => {
  document.title = `${to.meta.title || ''} · stockdb 控制台`
})

export default router
