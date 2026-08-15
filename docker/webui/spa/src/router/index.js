// router/index.js — 路由表从 nav.js 单一配置源生成。
// 学习点：路由 meta 携带标题/分组/图标；createWebHistory 让每页有独立 URL。
import { createRouter, createWebHistory } from 'vue-router'
import { NAV_ITEMS } from '../layout/nav.js'
import PlaceholderView from '../views/PlaceholderView.vue'

const routes = [
  { path: '/', redirect: '/overview' },
  ...NAV_ITEMS.map((it) => ({
    path: it.path,
    component: PlaceholderView,
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
