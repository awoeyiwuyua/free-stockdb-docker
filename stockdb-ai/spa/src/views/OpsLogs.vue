<template>
  <div class="page">
    <div class="page-head">
      <div>
        <h2 class="page-title">日志中心</h2>
        <p class="page-sub">同步日志 + stockdb 容器日志，聚合查看 · 15s 自动刷新</p>
      </div>
      <div class="page-actions">
        <el-input
          v-model="keyword"
          placeholder="关键字过滤（实时，不分大小写）"
          clearable
          :prefix-icon="Search"
          class="search-input"
        />
        <el-button :icon="Refresh" :loading="refreshing" @click="refreshAll">刷新</el-button>
      </div>
    </div>

    <!-- ============ ① 同步日志（/api/log） ============ -->
    <section class="card">
      <div class="sec-head">
        <h3 class="sec-title">同步日志</h3>
        <div class="sec-right">
          <span class="sec-meta">
            共 {{ syncLineCount }} 行<template v-if="lastUpdated.sync"> · {{ lastUpdated.sync }} 更新</template>
          </span>
          <el-button size="small" :icon="Refresh" :loading="syncLoading" @click="loadSyncLog(true)">刷新</el-button>
        </div>
      </div>

      <el-skeleton v-if="syncLoading && !syncLog" :rows="6" animated />
      <el-alert v-else-if="syncError && !syncLog" type="error" :closable="false" show-icon title="同步日志读取失败">
        <template #default>
          {{ syncError }}
          <el-button size="small" class="retry-btn" @click="loadSyncLog(true)">重试</el-button>
        </template>
      </el-alert>
      <template v-else>
        <el-alert
          v-if="syncError"
          type="warning"
          :closable="false"
          show-icon
          class="stale-alert"
          :title="`刷新失败：${syncError}（将自动重试）`"
        />
        <div v-if="!syncLog" class="log-empty">（暂无同步日志）</div>
        <div v-else-if="!displaySyncLog" class="log-empty">没有匹配「{{ keyword }}」的日志行</div>
        <pre v-else class="log-pre">{{ displaySyncLog }}</pre>
      </template>
    </section>

    <!-- ============ ② 容器日志（/api/container/logs） ============ -->
    <section class="card">
      <div class="sec-head">
        <h3 class="sec-title">容器日志</h3>
        <div class="sec-right">
          <span class="sec-meta">
            共 {{ contLineCount }} 行<template v-if="lastUpdated.cont"> · {{ lastUpdated.cont }} 更新</template>
          </span>
          <el-button size="small" :icon="Refresh" :loading="contLoading" @click="loadContLog(true)">刷新</el-button>
        </div>
      </div>

      <el-skeleton v-if="contLoading && !contLog" :rows="6" animated />
      <el-alert v-else-if="contError && !contLog" type="error" :closable="false" show-icon title="容器日志读取失败">
        <template #default>
          {{ contError }}
          <el-button size="small" class="retry-btn" @click="loadContLog(true)">重试</el-button>
        </template>
      </el-alert>
      <template v-else>
        <el-alert
          v-if="contDegraded"
          type="warning"
          :closable="false"
          show-icon
          class="stale-alert"
          :title="contDegraded"
        />
        <el-alert
          v-if="contError"
          type="warning"
          :closable="false"
          show-icon
          class="stale-alert"
          :title="`刷新失败：${contError}（将自动重试）`"
        />
        <div v-if="!contLog" class="log-empty">（暂无容器日志）</div>
        <div v-else-if="!displayContLog" class="log-empty">没有匹配「{{ keyword }}」的日志行</div>
        <pre v-else class="log-pre">{{ displayContLog }}</pre>
      </template>
    </section>
  </div>
</template>

<script setup>
// 日志中心（0.8.0 起两源：同步日志 + 容器日志；模拟盘事件源已随模拟盘移除）
// 只有展示/过滤，没有任何写操作；前端关键字过滤，不请求后端。
import { ref, computed, onMounted, onUnmounted } from 'vue'
import { Refresh, Search } from '@element-plus/icons-vue'
import { getLog, getContainerLogs } from '../api/status.js'

const POLL_MS = 15_000  // 日志讲究新鲜，15s 一刷

const keyword = ref('')
const refreshing = ref(false)

// —— 同步日志源 ——
const syncLog = ref(null)
const syncError = ref('')
const syncLoading = ref(false)
const lastUpdated = ref({ sync: '', cont: '' })

async function loadSyncLog(manual = false) {
  if (syncLoading.value) return  // 在途互斥，防请求堆积
  syncLoading.value = true
  try {
    const r = await getLog(200) // 约定返回 {log: 文本}
    syncLog.value = typeof r?.log === 'string' ? r.log : ''
    syncError.value = ''
    lastUpdated.value.sync = new Date().toLocaleTimeString('zh-CN', { hour12: false })
  } catch (e) {
    syncError.value = e?.message || '接口不可用'
  } finally {
    syncLoading.value = false
  }
}

// —— 容器日志源 ——
const contLog = ref(null)
const contError = ref('')
const contLoading = ref(false)
const contDegraded = ref('')

async function loadContLog(manual = false) {
  if (contLoading.value) return
  contLoading.value = true
  try {
    const r = await getContainerLogs(150) // 约定返回 {log: 文本, error?: 读失败原因}
    contLog.value = typeof r?.log === 'string' ? r.log : ''
    contDegraded.value = r?.error && !contLog.value ? r.error : ''
    contError.value = ''
    lastUpdated.value.cont = new Date().toLocaleTimeString('zh-CN', { hour12: false })
  } catch (e) {
    contError.value = e?.message || '接口不可用'
  } finally {
    contLoading.value = false
  }
}

// —— 关键字过滤（统一小写 contains，纯前端） ——
const kw = computed(() => keyword.value.trim().toLowerCase())

const syncLines = computed(() => (syncLog.value || '').split('\n'))
const contLines = computed(() => (contLog.value || '').split('\n'))
const syncLineCount = computed(() => syncLines.value.filter((l) => l.trim()).length)
const contLineCount = computed(() => contLines.value.filter((l) => l.trim()).length)

const displaySyncLog = computed(() =>
  kw.value ? syncLines.value.filter((l) => l.toLowerCase().includes(kw.value)).join('\n') : syncLog.value || ''
)
const displayContLog = computed(() =>
  kw.value ? contLines.value.filter((l) => l.toLowerCase().includes(kw.value)).join('\n') : contLog.value || ''
)

async function refreshAll() {
  refreshing.value = true
  try {
    await Promise.all([loadSyncLog(), loadContLog()])
  } finally {
    refreshing.value = false
  }
}

let timer = null
onMounted(() => {
  refreshAll()
  timer = setInterval(refreshAll, POLL_MS) // 后台静默轮询
})
onUnmounted(() => {
  if (timer) clearInterval(timer)
})
</script>

<style scoped>
.page { display: flex; flex-direction: column; gap: 16px; }
.page-head { display: flex; justify-content: space-between; align-items: flex-end; flex-wrap: wrap; gap: 12px; }
.page-title { margin: 0; font-size: 18px; }
.page-sub { margin: 4px 0 0; font-size: 13px; color: var(--muted); }
.page-actions { display: flex; gap: 10px; align-items: center; }
.search-input { width: 260px; }
.card { background: var(--panel); border: 1px solid var(--line); border-radius: 12px; padding: 14px 16px; }
.sec-head { display: flex; justify-content: space-between; align-items: center; margin-bottom: 10px; }
.sec-title { margin: 0; font-size: 14px; font-weight: 600; }
.sec-right { display: flex; align-items: center; gap: 10px; }
.sec-meta { font-size: 12px; color: var(--muted); }
.log-pre { background: var(--panel2); border: 1px solid var(--line); border-radius: 8px; padding: 10px 12px; margin: 0; max-height: 420px; overflow: auto; font: 12px/1.5 ui-monospace, SFMono-Regular, Menlo, monospace; white-space: pre-wrap; word-break: break-all; }
.log-empty { color: var(--muted); font-size: 13px; padding: 12px 0; }
.stale-alert { margin-bottom: 8px; }
.retry-btn { margin-left: 8px; }
</style>
