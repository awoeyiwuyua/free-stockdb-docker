<template>
  <div class="shell">
    <header class="shell-header">
      <span class="brand">stockdb 控制台 <em>SPA</em></span>
      <span class="ver">v{{ webuiVersion }}</span>
      <a class="legacy" href="/legacy" target="_blank" rel="noopener">旧面板（逃生通道）</a>
    </header>
    <main class="shell-main">
      <RouterView />
    </main>
  </div>
</template>

<script setup>
// M0 最小外壳：顶栏（品牌 + 版本号）+ 路由出口。
// 学习点：onMounted 里发请求拿数据；ref 包装的值在模板里自动解包。
import { ref, onMounted } from 'vue'
import { getJson } from './api/http'

const webuiVersion = ref('—')

onMounted(async () => {
  try {
    const v = await getJson('/api/version')
    webuiVersion.value = v.webui?.version || '—'
  } catch {
    /* 后端不可用时静默，保持外壳可用 */
  }
})
</script>

<style scoped>
.shell {
  min-height: 100vh;
  display: flex;
  flex-direction: column;
}
.shell-header {
  display: flex;
  align-items: center;
  gap: 14px;
  padding: 10px 20px;
  border-bottom: 1px solid var(--line);
  background: var(--panel);
}
.brand {
  font-weight: 700;
}
.brand em {
  color: var(--brand);
  font-style: normal;
  font-size: 12px;
  border: 1px solid var(--brand);
  border-radius: 4px;
  padding: 1px 5px;
  margin-left: 4px;
}
.ver {
  color: var(--muted);
  font-size: 13px;
}
.legacy {
  margin-left: auto;
  color: var(--muted);
  font-size: 13px;
}
.shell-main {
  flex: 1;
  padding: 20px;
  max-width: 1280px;
  width: 100%;
  margin: 0 auto;
}
</style>
