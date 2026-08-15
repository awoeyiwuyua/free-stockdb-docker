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
      <!-- 总览：单页项（LuCI 的 Status→Overview 模式） -->
      <el-menu-item v-for="it in TOP_ITEMS" :key="it.path" :index="it.path">
        <el-icon><component :is="it.icon" /></el-icon>
        <template #title>{{ it.title }}</template>
      </el-menu-item>

      <!-- 分组：系统运维 / 模拟盘（el-sub-menu 可折叠，子项一页一职责） -->
      <el-sub-menu v-for="g in NAV_GROUPS" :key="g.title" :index="g.title">
        <template #title>
          <el-icon><component :is="g.icon" /></el-icon>
          <span>{{ g.title }}</span>
        </template>
        <el-menu-item v-for="it in g.items" :key="it.path" :index="it.path">
          <el-icon><component :is="it.icon" /></el-icon>
          <template #title>
            <el-badge
              v-if="it.badge"
              :value="store[it.badge]"
              :hidden="!store[it.badge]"
              type="danger"
              class="nav-badge"
            >
              <span>{{ it.title }}</span>
            </el-badge>
            <span v-else>{{ it.title }}</span>
          </template>
        </el-menu-item>
      </el-sub-menu>
    </el-menu>
  </div>
</template>

<script setup>
// 学习点：el-sub-menu 分组菜单树（LuCI 风格）；props 折叠开关；
// el-menu router 模式 = 点菜单即跳路由，default-active 用当前路由路径高亮。
import { useRoute } from 'vue-router'
import { TOP_ITEMS, NAV_GROUPS } from './nav.js'
import { useGlobalStore } from '../stores/global.js'

defineProps({
  collapsed: { type: Boolean, default: false },
})

const route = useRoute()
const store = useGlobalStore() // 通知中心红点徽标数据源
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
.nav-badge {
  width: 100%;
}
.nav-badge :deep(.el-badge__content) {
  transform: none;
  position: static;
  margin-left: 6px;
}
</style>
