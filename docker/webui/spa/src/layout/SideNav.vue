<template>
  <div class="side-nav">
    <div class="side-logo">
      <span class="logo-mark">📈</span>
      <span v-show="!collapsed" class="logo-text">stockdb 控制台</span>
    </div>
    <el-menu
      :default-active="route.path"
      :collapse="collapsed"
      :collapse-transition="false"
      router
      class="side-menu"
    >
      <el-menu-item-group v-for="g in NAV_GROUPS" :key="g.title" :title="collapsed ? '' : g.title">
        <el-menu-item v-for="it in g.items" :key="it.path" :index="it.path">
          <el-icon><component :is="it.icon" /></el-icon>
          <template #title>{{ it.title }}</template>
        </el-menu-item>
      </el-menu-item-group>
    </el-menu>
  </div>
</template>

<script setup>
// 学习点：props 接收父组件状态（折叠开关）；el-menu 的 router 模式 =
// 点菜单即跳路由，default-active 用当前路由路径高亮。
import { useRoute } from 'vue-router'
import { NAV_GROUPS } from './nav.js'

defineProps({
  collapsed: { type: Boolean, default: false },
})

const route = useRoute()
</script>

<style scoped>
.side-nav {
  height: 100%;
  display: flex;
  flex-direction: column;
  border-right: 1px solid var(--line);
  background: var(--panel);
}
.side-logo {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 14px 18px;
  font-weight: 700;
  font-size: 15px;
  border-bottom: 1px solid var(--line);
  min-height: 52px;
}
.logo-mark {
  font-size: 20px;
}
.side-menu {
  flex: 1;
  border-right: none;
  padding: 8px 0;
}
.side-menu :deep(.el-menu-item-group__title) {
  padding: 12px 18px 4px;
  font-size: 11px;
  color: var(--muted);
  letter-spacing: 1px;
}
</style>
