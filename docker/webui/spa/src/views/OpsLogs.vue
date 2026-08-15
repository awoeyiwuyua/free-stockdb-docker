<template>
  <!-- ============================================================
       日志中心页（/ops/logs）——任务 G5：聚合「同步日志 + 容器日志 + 模拟盘事件」三源。
       学习点：
       1) 前端聚合：三个接口并行拉取、各自独立管理 loading/error/数据，
          一个源挂了不影响另外两个（降级不崩）；
       2) 关键字过滤是"前端实时 contains 过滤"（computed 派生），不请求后端，
          文本源按行过滤、事件表按 detail/event/level 等字段过滤；
       3) 本页轮询 15s（日志要新，比全局 30s 更密），卸载必须清理定时器；
       4) 三态齐备：骨架屏 / 错误降级文案（含后端 error 字段）/ EmptyState。
       ============================================================ -->
  <div class="page">

    <!-- 页头：小标题 + 右侧（自动刷新提示 + 全局关键字搜索框） -->
    <div class="page-head">
      <h2 class="page-title">日志中心</h2>
      <div class="page-actions">
        <span class="page-hint">每 15 秒自动刷新</span>
        <!-- clearable：点 ✕ 一键清空关键字；v-model 实时驱动下方三个区的过滤 -->
        <el-input
          v-model="keyword"
          class="kw-input"
          :prefix-icon="Search"
          placeholder="关键字过滤：日志行 / 事件名 / 级别 / 详情"
          clearable
        />
      </div>
    </div>

    <!-- ================= ① 同步日志（/api/log） ================= -->
    <section class="card">
      <!-- 紧凑标题行：标题在左，行数与刷新按钮在右（LuCI 信息优先） -->
      <div class="sec-head">
        <h3 class="sec-title">同步日志</h3>
        <div class="sec-right">
          <span class="sec-meta">
            共 {{ syncLineCount }} 行<template v-if="lastUpdated.sync"> · {{ lastUpdated.sync }} 更新</template>
          </span>
          <el-button size="small" :icon="Refresh" :loading="syncLoading" @click="loadSyncLog(true)">刷新</el-button>
        </div>
      </div>

      <!-- 三态一：首次加载骨架屏 -->
      <el-skeleton v-if="syncLoading && !syncLog" :rows="6" animated />
      <!-- 三态二：首次拉取就失败且没有旧数据 → 错误降级文案 + 重试按钮 -->
      <el-alert v-else-if="syncError && !syncLog" type="error" :closable="false" show-icon title="同步日志读取失败">
        <template #default>
          {{ syncError }}
          <el-button size="small" class="retry-btn" @click="loadSyncLog(true)">重试</el-button>
        </template>
      </el-alert>
      <!-- 三态三：正常态（含轮询失败但有旧数据的弱提示） -->
      <template v-else>
        <!-- 轮询失败但手里还有旧数据：顶部一行弱提示，日志照常展示不打断 -->
        <el-alert
          v-if="syncError"
          type="warning"
          :closable="false"
          show-icon
          class="stale-alert"
          :title="`刷新失败：${syncError}（将自动重试）`"
        />
        <!-- 日志本体：等宽字体 + 深色底 + 内部滚动；关键字命中则只显示匹配行 -->
        <div v-if="!syncLog" class="log-empty">（暂无同步日志，启动同步后自动生成）</div>
        <div v-else-if="!displaySyncLog" class="log-empty">没有匹配「{{ keyword }}」的日志行</div>
        <pre v-else ref="syncPre" class="log-pre">{{ displaySyncLog }}</pre>
      </template>
    </section>

    <!-- ================= ② 容器日志（/api/container/logs） ================= -->
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
        <!-- 后端 200 但返回 error 字段（如 /data/log.txt 读失败）：降级文案，保留已有内容 -->
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
        <pre v-else ref="contPre" class="log-pre">{{ displayContLog }}</pre>
      </template>
    </section>

    <!-- ================= ③ 模拟盘事件（/api/paper/events） ================= -->
    <section class="card">
      <div class="sec-head">
        <h3 class="sec-title">模拟盘事件</h3>
        <div class="sec-right">
          <span class="sec-meta">
            共 {{ filteredEvents.length }} 条<template v-if="lastUpdated.evt"> · {{ lastUpdated.evt }} 更新</template>
          </span>
          <el-button size="small" :icon="Refresh" :loading="evtLoading" @click="loadEvents(true)">刷新</el-button>
        </div>
      </div>

      <el-skeleton v-if="evtLoading && !events.length" :rows="6" animated />
      <!-- 引擎不可用等场景：后端 501 会抛 ApiError（message 就是后端中文降级文案） -->
      <el-alert v-else-if="evtError && !events.length" type="error" :closable="false" show-icon title="模拟盘事件读取失败">
        <template #default>
          {{ evtError }}
          <el-button size="small" class="retry-btn" @click="loadEvents(true)">重试</el-button>
        </template>
      </el-alert>
      <template v-else>
        <el-alert
          v-if="evtError"
          type="warning"
          :closable="false"
          show-icon
          class="stale-alert"
          :title="`刷新失败：${evtError}（将自动重试）`"
        />
        <EmptyState
          v-if="!events.length"
          icon="List"
          title="暂无模拟盘事件"
          description="模拟盘产生决策 / 下单 / 对账等系统事件后，会在这里按时间倒序展示"
        />
        <!-- 有事件但被关键字全过滤 → 明确提示，别误读成"没有事件" -->
        <div v-else-if="!filteredEvents.length" class="log-empty">
          没有匹配「{{ keyword }}」的事件
        </div>
        <!-- 紧凑表格：size=small + border，字段与 app.py system_events 表一一对应 -->
        <el-table v-else :data="filteredEvents" size="small" border class="evt-table">
          <el-table-column label="时间" width="150">
            <template #default="{ row }">
              <span class="muted-cell">{{ fmtTs(row.ts) }}</span>
            </template>
          </el-table-column>
          <el-table-column label="级别" width="76">
            <template #default="{ row }">
              <el-tag :type="levelTagType(row.level)" size="small">{{ levelLabel(row.level) }}</el-tag>
            </template>
          </el-table-column>
          <el-table-column label="时点" width="64">
            <template #default="{ row }">{{ row.timepoint || '—' }}</template>
          </el-table-column>
          <el-table-column prop="event" label="事件" width="150" show-overflow-tooltip />
          <el-table-column label="详情" min-width="220" show-overflow-tooltip>
            <template #default="{ row }">{{ row.detail || '—' }}</template>
          </el-table-column>
        </el-table>
      </template>
    </section>
  </div>
</template>

<script setup>
// ================= 引入 =================
// 本页只有展示/过滤，没有任何写操作，因此不需要 ElMessageBox / ElMessage
import { ref, computed, nextTick, onMounted, onUnmounted } from 'vue'
// 按钮与输入框图标：显式 import 组件对象（:icon 需要真实组件，与全站写法一致）
import { Refresh, Search } from '@element-plus/icons-vue'
// 两个日志文本源来自 status.js；模拟盘事件来自 paper.js
import { getLog, getContainerLogs } from '../api/status.js'
import { getEvents } from '../api/paper.js'
import EmptyState from '../components/EmptyState.vue'

// ================= 常量 =================
// 本页轮询节拍：15s（任务约定——日志讲究新鲜，比全局 30s 更密）
const POLL_MS = 15_000
// 事件级别 → 中文标签（system_events.level 取值 DEBUG/INFO/WARN/ERROR）
const LEVEL_LABEL = { DEBUG: '调试', INFO: '信息', WARN: '警告', ERROR: '错误' }
// 事件级别 → el-tag 的 type（颜色语义：错误红 / 警告黄 / 其余蓝或灰）
const LEVEL_TAG = { DEBUG: 'info', INFO: 'primary', WARN: 'warning', ERROR: 'danger' }

// ================= 全局关键字 =================
// 页头搜索框：v-model 双向绑定，实时驱动下面三个 computed 过滤（纯前端 contains，不请求后端）
const keyword = ref('')
// 统一转小写，过滤时大小写不敏感
const kw = computed(() => keyword.value.trim().toLowerCase())

// ================= ① 同步日志状态 =================
const syncLog = ref('')     // /api/log 原始文本
const syncError = ref('')   // 请求失败文案
const syncLoading = ref(true) // 首次为 true 显示骨架屏；手动刷新期间驱动按钮转圈
let syncBusy = false        // 互斥锁：上一轮请求没回来就跳过本轮，避免轮询堆积
const syncPre = ref(null)   // 日志 pre 的 DOM 引用（智能滚动用）

// ================= ② 容器日志状态 =================
const contLog = ref('')     // /api/container/logs 原始文本
const contError = ref('')   // 请求失败文案
const contDegraded = ref('') // 后端 200 但返回 error 字段（读 /data/log.txt 失败）的降级文案
const contLoading = ref(true)
let contBusy = false
const contPre = ref(null)

// ================= ③ 模拟盘事件状态 =================
const events = ref([])      // /api/paper/events → {events:[{id,ts,trade_date,timepoint,level,event,detail}]}
const evtError = ref('')    // 请求失败/引擎不可用文案（501 → ApiError.message 即后端中文文案）
const evtLoading = ref(true)
let evtBusy = false

// 每区上次成功刷新的时刻（HH:MM:SS），标题行里"信息优先"地展示新鲜度
const lastUpdated = ref({ sync: '', cont: '', evt: '' })

// ================= 关键字过滤（前端实时 contains） =================
// 文本类日志按行过滤：关键字为空 → 全部；否则只保留包含关键字的行
// 学习点：split/join 保持换行结构，pre 展示时每行仍然独立
function filterLogText(text, kw) {
  if (!kw) return text
  return text.split('\n').filter((line) => line.toLowerCase().includes(kw)).join('\n')
}
const displaySyncLog = computed(() => filterLogText(syncLog.value, kw.value))
const displayContLog = computed(() => filterLogText(contLog.value, kw.value))
// 行数统计：空文本给 0（过滤后无匹配也归 0，标题行"共 0 行"一目了然）
const syncLineCount = computed(() => (displaySyncLog.value ? displaySyncLog.value.split('\n').filter(Boolean).length : 0))
const contLineCount = computed(() => (displayContLog.value ? displayContLog.value.split('\n').filter(Boolean).length : 0))
// 事件表过滤：匹配 event/detail/timepoint/level/trade_date 任一字段（任务要求的 detail 字段匹配）
const filteredEvents = computed(() => {
  if (!kw.value) return events.value
  return events.value.filter((e) =>
    [e.event, e.detail, e.timepoint, e.level, e.trade_date]
      .some((v) => v != null && String(v).toLowerCase().includes(kw.value))
  )
})

// ================= 三个源各自加载 =================
// 通用套路：互斥锁（busy）防堆积 → 拉取 → 成功清错误/失败记文案 → finally 复位
// manual=true 时亮 loading（按钮转圈 / 骨架屏）；轮询静默刷新不打扰
async function loadSyncLog(manual = false) {
  if (syncBusy) return
  syncBusy = true
  if (manual) syncLoading.value = true
  // 智能滚动：刷新前记录"用户是否停在底部"。停在底部 → 刷新后继续贴底（日志往下长）；
  // 用户正在往上翻旧日志 → 刷新后保持原位置，不打扰阅读
  const stick = !kw.value && atBottom(syncPre.value)
  try {
    const r = await getLog(200) // 约定返回 {log: 文本}
    syncLog.value = typeof r?.log === 'string' ? r.log : ''
    syncError.value = ''
    lastUpdated.value.sync = nowText()
  } catch (e) {
    syncError.value = e?.message || '同步日志接口未就绪'
  } finally {
    syncLoading.value = false
    syncBusy = false
    if (stick) nextTick(() => { if (syncPre.value) syncPre.value.scrollTop = syncPre.value.scrollHeight })
  }
}

async function loadContLog(manual = false) {
  if (contBusy) return
  contBusy = true
  if (manual) contLoading.value = true
  const stick = !kw.value && atBottom(contPre.value)
  try {
    const r = await getContainerLogs(150) // 约定返回 {log: 文本, error?: 读失败原因}
    contLog.value = typeof r?.log === 'string' ? r.log : ''
    // 后端 200 但带 error 字段：不抛异常，单独记降级文案，保留已读到的内容
    contDegraded.value = r?.error || ''
    contError.value = ''
    lastUpdated.value.cont = nowText()
  } catch (e) {
    contError.value = e?.message || '容器日志接口未就绪'
  } finally {
    contLoading.value = false
    contBusy = false
    if (stick) nextTick(() => { if (contPre.value) contPre.value.scrollTop = contPre.value.scrollHeight })
  }
}

async function loadEvents(manual = false) {
  if (evtBusy) return
  evtBusy = true
  if (manual) evtLoading.value = true
  try {
    const r = await getEvents(100) // 约定返回 {events: [...]}，最新在前
    events.value = Array.isArray(r?.events) ? r.events : []
    evtError.value = ''
    lastUpdated.value.evt = nowText()
  } catch (e) {
    // 引擎不可用时后端返回 501 {error: 中文原因} → ApiError.message 已是可读降级文案
    evtError.value = e?.message || '模拟盘事件接口未就绪'
  } finally {
    evtLoading.value = false
    evtBusy = false
  }
}

// ================= 展示派生 =================
// ts 形如 '2026-08-14T23:38:06'：截前 19 位 + T 换空格 → 人读时间
function fmtTs(ts) {
  return ts ? String(ts).slice(0, 19).replace('T', ' ') : '—'
}
// 事件级别 → el-tag type（未知级别兜底 info 灰）
function levelTagType(level) {
  return LEVEL_TAG[level] || 'info'
}
function levelLabel(level) {
  return LEVEL_LABEL[level] || level || '—'
}
// 当前时刻 HH:MM:SS（展示"xx 更新"，表示上次成功拉取时间）
function nowText() {
  return new Date().toTimeString().slice(0, 8)
}
// 判断 pre 是否已滚到底部（留 24px 容差，接近底部就算）
function atBottom(el) {
  return el ? el.scrollHeight - el.scrollTop - el.clientHeight < 24 : false
}

// ================= 生命周期：首拉 + 15s 轮询 + 卸载清理 =================
let timer = null
onMounted(() => {
  loadSyncLog()
  loadContLog()
  loadEvents()
  // 15s 静默轮询：互斥锁保证上一轮没回来就跳过，失败只写 error 不弹窗
  timer = setInterval(() => {
    loadSyncLog()
    loadContLog()
    loadEvents()
  }, POLL_MS)
})
// 离开页面必须清定时器，否则后台空转、卸载后还会更新已销毁的组件
onUnmounted(() => {
  if (timer) clearInterval(timer)
  timer = null
})
</script>

<style scoped>
/* 页面容器：纵向排列三张卡片，卡片间 16px 间距（与全站 .page 口径一致） */
.page {
  display: flex;
  flex-direction: column;
  gap: 16px;
}
.page-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  flex-wrap: wrap;
  gap: 10px;
}
.page-title {
  margin: 0;
  font-size: 18px;
  font-weight: 700;
  color: var(--text);
}
.page-actions {
  display: flex;
  align-items: center;
  gap: 10px;
}
.page-hint {
  font-size: 12px;
  color: var(--muted);
}
/* 搜索框：紧凑宽度，不占满整行 */
.kw-input {
  width: 300px;
}

/* 卡片：LuCI 密度哲学 → 内边距压到 14px，不铺大留白 */
.card {
  background: var(--panel);
  border: 1px solid var(--line);
  border-radius: 12px;
  padding: 14px;
}
/* 紧凑标题行：标题在左，行数/刷新按钮在右 */
.sec-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  flex-wrap: wrap;
  gap: 8px;
  margin-bottom: 10px;
}
.sec-title {
  margin: 0;
  font-size: 15px;
  font-weight: 600;
  color: var(--text);
}
.sec-right {
  display: flex;
  align-items: center;
  gap: 10px;
}
.sec-meta {
  font-size: 12px;
  color: var(--muted);
}

/* 日志 pre：等宽字体 + 深色底 + 固定高度内部滚动（与数据同步页观感一致） */
.log-pre {
  margin: 0;
  padding: 10px;
  max-height: 320px;
  overflow: auto;
  background: var(--panel2);
  border: 1px solid var(--line);
  border-radius: 8px;
  font: 12px/1.5 ui-monospace, 'SF Mono', Menlo, Consolas, monospace;
  color: var(--text);
  white-space: pre-wrap;
  word-break: break-all;
}
/* 日志为空 / 无匹配行时的占位 */
.log-empty {
  padding: 14px 10px;
  font-size: 13px;
  color: var(--muted);
  text-align: center;
}

/* 事件表：上方留一点间隙；muted 时间弱化，让级别/事件/详情成为视觉重点 */
.evt-table {
  margin-top: 2px;
}
.muted-cell {
  color: var(--muted);
}
/* 弱提示条与重试按钮 */
.stale-alert {
  margin-bottom: 8px;
}
.retry-btn {
  margin-left: 12px;
}
</style>
