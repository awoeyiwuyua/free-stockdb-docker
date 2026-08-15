// router/index.js — 路由表从 nav.js 单一配置源生成，页面组件懒加载。
// Phase 5.1（LuCI 经验版）：总览单页 + 系统运维 7 子页 + 模拟盘 3 子页，旧路径 redirect 兜底。
import { createRouter, createWebHistory } from 'vue-router'
import { NAV_ITEMS, LEGACY_REDIRECTS } from '../layout/nav.js'

// 路径 → 页面组件（懒加载函数）
const VIEWS = {
  '/overview': () => import('../views/Overview.vue'),
  '/ops/sync': () => import('../views/OpsSync.vue'),
  '/ops/mydb': () => import('../views/OpsMydb.vue'),
  '/ops/health': () => import('../views/OpsHealth.vue'),
  '/ops/diag': () => import('../views/OpsDiag.vue'),
  '/ops/logs': () => import('../views/OpsLogs.vue'),
  '/ops/alerts': () => import('../views/OpsAlerts.vue'),
  '/ops/mcp': () => import('../views/OpsMcp.vue'),
  '/paper': () => import('../views/Paper.vue'),
  '/paper/audit': () => import('../views/PaperAudit.vue'),
  '/paper/signal': () => import('../views/PaperSignal.vue'),
}

const routes = [
  { path: '/', redirect: '/overview' },
  ...NAV_ITEMS.map((it) => ({
    path: it.path,
    component: VIEWS[it.path],
    meta: { title: it.title, group: it.group, icon: it.icon },
  })),
  // 旧路径兜底：老书签/旧顶栏链接跳转到新地址
  ...Object.entries(LEGACY_REDIRECTS).map(([from, to]) => ({
    path: from,
    redirect: to,
  })),
]

const router = createRouter({
  history: createWebHistory(),
  routes,
})

router.afterEach((to) => {
  document.title = `${to.meta.title || ''} · stockdb 控制台`
})

export default router
