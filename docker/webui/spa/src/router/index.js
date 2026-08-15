// router/index.js — 路由表从 nav.js 单一配置源生成，页面组件懒加载。
// 学习点：() => import(...) 懒加载 = 每个页面单独打包成 chunk，
// 首屏只下载当前页，切页时才按需下载（拆包后不再有 1MB 大文件警告）。
import { createRouter, createWebHistory } from 'vue-router'
import { NAV_ITEMS } from '../layout/nav.js'

// 路径 → 页面组件（懒加载函数）。新增页面：先在 nav.js 加导航项，再在这里登记映射。
const VIEWS = {
  '/overview': () => import('../views/Overview.vue'),
  '/data/sync': () => import('../views/DataSync.vue'),
  '/data/mydb': () => import('../views/MyDb.vue'),
  '/paper': () => import('../views/Paper.vue'),
  '/paper/audit': () => import('../views/PaperAudit.vue'),
  '/paper/signal': () => import('../views/PaperSignal.vue'),
  '/ops/system': () => import('../views/OpsSystem.vue'),
  '/ops/alerts': () => import('../views/OpsAlerts.vue'),
  '/ops/mcp': () => import('../views/OpsMcp.vue'),
  '/ops/version': () => import('../views/OpsVersion.vue'),
}

const routes = [
  { path: '/', redirect: '/overview' },
  ...NAV_ITEMS.map((it) => ({
    path: it.path,
    component: VIEWS[it.path],
    meta: { title: it.title, group: it.group, icon: it.icon },
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
