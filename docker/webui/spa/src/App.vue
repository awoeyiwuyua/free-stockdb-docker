<template>
  <!-- M1 应用外壳：左侧侧边栏 + 右侧（上=状态栏，下=路由内容区）上下结构 -->
  <div class="app-shell">
    <!-- 侧边栏：collapsed 由 App 统一管理，作为 prop 传给 SideNav -->
    <SideNav :collapsed="collapsed" />

    <!-- 右侧主区域 -->
    <div class="app-main">
      <!-- 状态栏自己会读 store，我们只需传折叠状态、监听它的折叠事件 -->
      <StatusBar :collapsed="collapsed" @toggle-collapse="collapsed = !collapsed" />

      <!-- 内容区：RouterView 渲染当前路由对应的页面组件 -->
      <main class="app-content">
        <RouterView />
      </main>
    </div>
  </div>
</template>

<script setup>
// 学习点：
// 1) ref 定义响应式状态，模板里直接用；事件 @xx 绑定处理函数。
// 2) onMounted 做首次数据拉取 + 开启轮询；onUnmounted 清理定时器和监听，防止泄漏。
// 3) setInterval 返回定时器 id，换间隔前必须先 clearInterval 旧的，否则会叠加出多个定时器。
import { ref, onMounted, onUnmounted } from 'vue'
import SideNav from './layout/SideNav.vue'
import StatusBar from './layout/StatusBar.vue'
import { useGlobalStore } from './stores/global.js'

// 全局数据仓库：refresh() 拉一次 /api/overview，各组件通过 getter 读取
const store = useGlobalStore()

// 侧边栏折叠状态：SideNav 用 prop 读它，StatusBar 用事件改它（单向数据流）
const collapsed = ref(false)

// 轮询间隔：标签页可见时 30 秒一次；隐藏时放宽到 5 分钟（省请求）
const POLL_FAST = 30_000
const POLL_SLOW = 5 * 60_000
let pollTimer = null // 定时器 id，方便随时换间隔 / 清除

// 当前页面是否可见（标签页是否在前台）
const pageVisible = () => document.visibilityState === 'visible'

// 按当前可见性选间隔并（重新）开启轮询：先清旧定时器再开新的
const restartPoll = () => {
  if (pollTimer) clearInterval(pollTimer)
  pollTimer = setInterval(() => store.refresh(), pageVisible() ? POLL_FAST : POLL_SLOW)
}

// 标签页可见性变化：回到前台→立即刷新一次并恢复 30s；切到后台→只放宽间隔
const onVisibilityChange = () => {
  if (pageVisible()) store.refresh() // 刚回到前台，先拿最新数据
  restartPoll() // 再按新状态切换间隔
}

onMounted(() => {
  store.refresh() // 首次进入页面先拉一次数据
  restartPoll() // 再开启周期轮询
  document.addEventListener('visibilitychange', onVisibilityChange)
})

onUnmounted(() => {
  if (pollTimer) clearInterval(pollTimer)
  pollTimer = null
  document.removeEventListener('visibilitychange', onVisibilityChange)
})
</script>

<style scoped>
/* 外层横向 flex：左=侧边栏，右=主区域 */
.app-shell {
  display: flex;
  min-height: 100vh;
}

/* 右侧主区域纵向 flex：上=状态栏，下=内容区 */
.app-main {
  flex: 1; /* 占满剩余宽度 */
  min-width: 0; /* 防止内容过宽把 flex 布局撑破 */
  display: flex;
  flex-direction: column;
}

.app-content {
  flex: 1;
  padding: 20px;
  overflow: auto; /* 内容超高时在区域内滚动，不撑破整体 */
}
</style>
