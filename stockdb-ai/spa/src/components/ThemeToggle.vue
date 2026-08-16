<template>
  <!-- 主题切换按钮：深色时显示 Sunny（点击切浅色），浅色时显示 Moon（点击切深色） -->
  <el-button text class="theme-toggle" :title="isDark ? '切换到浅色' : '切换到深色'" @click="toggle">
    <el-icon><component :is="isDark ? 'Sunny' : 'Moon'" /></el-icon>
  </el-button>
</template>

<script setup>
// 学习点：
// 1) 深/浅主题的本质 = html 根元素带不带 .dark class（配合 base.css 双主题变量）。
// 2) localStorage 持久化用户选择，刷新页面后由 main.js 恢复主题。
// 3) 初始状态不写死，而是读 document 的现状，保证与 main.js 的初始化结果一致。
import { ref } from 'vue'

const THEME_KEY = 'webui-theme' // localStorage 键名（值为 'dark' 或 'light'）

// 初始值 = html 根元素现在是否带着 dark class（main.js 挂载前已按 localStorage 设好）
const isDark = ref(document.documentElement.classList.contains('dark'))

// 把目标主题应用到根元素 + 存进 localStorage（唯一写主题的地方）
const applyTheme = (dark) => {
  document.documentElement.classList.toggle('dark', dark)
  localStorage.setItem(THEME_KEY, dark ? 'dark' : 'light')
  isDark.value = dark // 同步 ref，让图标跟着切换
}

const toggle = () => applyTheme(!isDark.value)
</script>

<style scoped>
.theme-toggle {
  padding: 6px;
  font-size: 16px;
}
</style>
