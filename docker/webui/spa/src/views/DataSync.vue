<template>
  <!-- ============================================================
       数据同步页（/data/sync）——旧面板「数据同步」页签 source 子页搬迁。
       学习点：本页几乎全是"状态展示 + 触发动作"，没有复杂业务逻辑——
       页面自管 30s 轮询（onMounted 首拉 + setInterval + onUnmounted 清理），
       危险操作（重启容器）必须 ElMessageBox.confirm 二次确认。
       ============================================================ -->
  <div class="page">

    <!-- 页头：标题 + 手动刷新按钮（轮询之外随时补拉一次） -->
    <div class="page-head">
      <h2 class="page-title">数据同步</h2>
      <el-button :icon="Refresh" :loading="loading" @click="loadAll(true)">手动刷新</el-button>
    </div>

    <!-- 非阻塞错误条：轮询期间某次拉取失败时展示文案，页面其余部分保持可用 -->
    <el-alert
      v-if="error && status"
      class="page-alert"
      type="error"
      :title="error"
      show-icon
      :closable="true"
      @close="error = null"
    />

    <!-- ① 加载态：整页骨架屏（el-skeleton），首次数据没回来前显示 -->
    <el-skeleton v-if="loading && !status" :rows="8" animated />

    <!-- ② 错误态：首拉就失败且没有任何数据 → EmptyState + 重试按钮，页面不崩 -->
    <EmptyState
      v-else-if="error && !status"
      icon="WarningFilled"
      title="状态加载失败"
      :description="error"
    >
      <el-button type="primary" @click="loadAll(true)">重试</el-button>
    </EmptyState>

    <!-- ③ 正常态：数据齐了，逐块渲染 -->
    <template v-else>

      <!-- ================= 状态总览 ================= -->
      <section class="card">
        <h3 class="card-title">状态总览</h3>

        <!-- 同步管道横幅：正在同步 → 进度条 + 阶段；空闲 → 最近一次结果 -->
        <div v-if="status.sync_running" class="sync-banner">
          <el-progress
            :percentage="phasePct"
            :stroke-width="14"
            :format="() => `正在${phaseLabel} · ${phasePct}%`"
          />
          <div class="sync-meta">
            <el-tag type="primary" effect="dark">同步中</el-tag>
            <span class="hint">已运行 {{ syncElapsedText }}（每 30s 刷新一次阶段）</span>
          </div>
        </div>
        <div v-else class="sync-banner idle">
          <el-tag :type="lastExitOk ? 'success' : lastExitCode == null ? 'info' : 'danger'">
            {{ lastExitOk ? '空闲 · 上次成功' : lastExitCode == null ? '空闲' : '空闲 · 上次失败' }}
          </el-tag>
          <span v-if="lastSync" class="hint">
            上次同步 {{ lastSync.ts }} · 下载 {{ lastSync.downloads ?? '—' }} 个文件
          </span>
          <span v-else class="hint">尚无同步记录，点击下方按钮启动首次同步</span>
        </div>

        <!-- 容器 / 数据源 / 同步能力 描述块（el-descriptions 一键出表格排版） -->
        <el-descriptions :column="2" border size="small" class="desc">
          <el-descriptions-item label="stockdb 进程">
            <el-tag :type="status.container?.ok ? 'success' : 'danger'" size="small">
              {{ status.container?.ok ? '运行中' : '已停止' }}
            </el-tag>
            <span class="hint" style="margin-left: 6px">{{ status.container?.note || '' }}</span>
          </el-descriptions-item>
          <el-descriptions-item label="运行时长">
            {{ fmtUptime(status.container?.started) }}
          </el-descriptions-item>
          <el-descriptions-item label="镜像" :span="1">
            {{ status.container?.image || '—' }}
          </el-descriptions-item>
          <el-descriptions-item label="数据源" :span="1">
            {{ status.source || '—' }}
          </el-descriptions-item>
        </el-descriptions>

        <!-- 指标卡栅格：StatCard 组件统一样式，tone 按语义着色 -->
        <div class="stat-grid">
          <!-- 数据新鲜度：滞后天数从全局 store 读（health.lag_days，App 已 30s 轮询 /api/overview，
               本页不必重复请求；≤1 正常 / 2 警告 / >2 错误） -->
          <StatCard
            label="数据最新"
            :value="status.data_latest ? fmtYMD(status.data_latest) : '—'"
            :sub="`镜像 ${status.mirror || '—'}`"
            :tone="freshTone"
          />
          <!-- 全市场标的数量：code_stats{stock,etf,other} 由后端 15s 缓存兜底 -->
          <StatCard label="股票数" :value="status.code_stats?.stock ?? '—'" sub="code_stats.stock" />
          <StatCard label="ETF 数" :value="status.code_stats?.etf ?? '—'" sub="code_stats.etf" />
          <StatCard label="其他标的" :value="status.code_stats?.other ?? '—'" sub="code_stats.other" />
          <!-- 行情服务延迟：null 说明查询失败 → 标红（与旧页 hcSvc 同判据） -->
          <StatCard
            label="行情响应"
            :value="status.code_stats?.latency_ms != null ? `${status.code_stats.latency_ms} ms` : '不可用'"
            :tone="status.code_stats?.latency_ms != null ? 'ok' : 'err'"
            sub="code_stats.latency_ms"
          />
          <!-- 覆盖范围：coverage{earliest,latest} 是 8 位数字，只取年份展示 -->
          <StatCard
            label="数据覆盖"
            :value="coverageText"
            sub="coverage.earliest ~ latest"
          />
          <StatCard
            label="定时调度器"
            :value="status.scheduler_alive ? '运行中' : '已停止'"
            :tone="status.scheduler_alive ? 'ok' : 'err'"
            sub="scheduler_alive（后台线程心跳）"
          />
          <!-- 今日是否交易日：仅提示用，不影响操作 -->
          <StatCard
            label="今日交易日"
            :value="status.trading_today ? '是' : '否'"
            :tone="status.trading_today ? 'ok' : 'warn'"
            sub="trading_today（定时按此跳过休市）"
          />
        </div>

        <!-- 磁盘用量：el-progress 容量条，>80% 变红提醒扩容 -->
        <div class="disk-block">
          <div class="disk-label">
            <span>数据卷磁盘</span>
            <span class="hint">
              {{ diskText }}
            </span>
          </div>
          <!-- 颜色走语义 CSS 变量（--err/--warn/--ok），随主题自动切换，不写死十六进制 -->
          <el-progress
            v-if="diskPct != null"
            :percentage="diskPct"
            :stroke-width="12"
            :color="diskPct > 80 ? 'var(--err)' : diskPct > 60 ? 'var(--warn)' : 'var(--ok)'"
            :format="() => `${diskPct}%`"
          />
        </div>

        <!-- 同步能力检查：sync_cap{ok, checks{updater,source,writable,retry_pending}} -->
        <div class="cap-block">
          <div class="cap-title">
            同步能力检查
            <el-tag :type="status.sync_cap?.ok ? 'success' : 'danger'" size="small">
              {{ status.sync_cap?.ok ? '可用' : '不可用' }}
            </el-tag>
          </div>
          <ul class="cap-list">
            <li v-for="(check, name) in status.sync_cap?.checks || {}" :key="name">
              <el-icon :color="check.ok === false ? 'var(--err)' : check.warn ? 'var(--warn)' : 'var(--ok)'">
                <component :is="check.ok === false ? 'CircleCloseFilled' : check.warn ? 'WarningFilled' : 'CircleCheckFilled'" />
              </el-icon>
              <span class="cap-name">{{ CAP_LABELS[name] || name }}</span>
              <span class="hint">{{ check.detail }}</span>
            </li>
          </ul>
        </div>
      </section>

      <!-- ================= 操作区 ================= -->
      <section class="card">
        <h3 class="card-title">同步操作</h3>
        <p class="card-hint">
          热更新：stockdb 保持运行、增量同步 + 自动 reload，零中断（推荐）；停服严格模式：按官方要求先停服务再同步，故障兜底用。
        </p>
        <div class="actions">
          <el-button
            type="primary"
            :icon="VideoPlay"
            :loading="syncBusy"
            :disabled="status.sync_running"
            @click="doSync(true)"
          >
            立即热更新
          </el-button>
          <el-button
            type="warning"
            :icon="SwitchButton"
            :loading="syncBusy"
            :disabled="status.sync_running"
            @click="doSync(false)"
          >
            停服严格同步
          </el-button>
          <span v-if="status.sync_running" class="hint">同步进行中，按钮已禁用（后端锁保证串行）</span>
        </div>
      </section>

      <!-- ================= 定时计划 ================= -->
      <section class="card">
        <h3 class="card-title">定时自动同步</h3>
        <div class="sch-row">
          <el-switch v-model="schEnabled" active-text="启用定时" @change="markSchDirty" />
          <el-switch v-model="schTrading" active-text="仅交易日触发" @change="markSchDirty" />
          <span v-if="schTodayNote" class="hint">{{ schTodayNote }}</span>
        </div>
        <div class="sch-row">
          <span class="sch-label">执行时间点（可多选，也可直接输入 HH:MM 回车添加）：</span>
          <el-select
            v-model="schTimes"
            multiple
            filterable
            allow-create
            default-first-option
            collapse-tags
            placeholder="选择或输入时间点"
            style="width: 380px"
            @change="markSchDirty"
          >
            <el-option v-for="t in TIME_OPTIONS" :key="t" :label="t" :value="t" />
          </el-select>
          <!-- 未保存提示：有草稿时 30s 轮询不再覆盖表单，避免吞掉用户输入 -->
          <span v-if="schDirty" class="hint" style="color: var(--warn)">有未保存的修改，保存前轮询不覆盖表单</span>
        </div>
        <div class="sch-info hint">
          下次触发：{{ schedule?.next_trigger || '—' }}
          <template v-if="schedule?.last_trigger?.ts">
            ｜ 上次触发：{{ schedule.last_trigger.ts }}
            {{ schedule.last_trigger.exit === 0 ? '✅' : schedule.last_trigger.exit == null ? '⏳' : '❌' }}
          </template>
        </div>
        <el-button type="primary" :loading="schSaving" @click="saveSch">保存定时计划</el-button>
      </section>

      <!-- ================= 同步历史 ================= -->
      <section class="card">
        <h3 class="card-title">同步历史</h3>
        <!-- 空态：还没有任何同步记录 -->
        <EmptyState
          v-if="!history.length"
          icon="Clock"
          title="暂无同步历史"
          description="启动一次同步后，这里会记录每次任务的触发来源 / 模式 / 结果 / 耗时。"
        />
        <el-table v-else :data="history" size="small" border>
          <!-- 展开列：点行首箭头看失败原因 / 警告等详情（旧页 historyRow 的点击展开） -->
          <el-table-column type="expand">
            <template #default="{ row }">
              <div class="hist-detail">
                <div v-if="row.reason">失败原因：{{ row.reason }}</div>
                <div v-if="row.warn">⚠ 警告：{{ row.warn }}</div>
                <div v-if="row.deletes != null">删除文件：{{ row.deletes }} 个</div>
                <div v-if="!row.reason && !row.warn && row.deletes == null" class="hint">（无额外详情）</div>
              </div>
            </template>
          </el-table-column>
          <el-table-column prop="ts" label="时间" width="170" />
          <el-table-column label="触发" width="110">
            <template #default="{ row }">{{ TRIGGER_LABEL[row.trigger] || row.trigger }}</template>
          </el-table-column>
          <el-table-column label="模式" width="110">
            <template #default="{ row }">{{ row.mode === 'strict' ? '严格(停服)' : '热更新' }}</template>
          </el-table-column>
          <el-table-column label="结果" width="90">
            <template #default="{ row }">
              <el-tag :type="resultTagType(row)" size="small">{{ resultText(row) }}</el-tag>
            </template>
          </el-table-column>
          <el-table-column prop="downloads" label="下载" width="70">
            <template #default="{ row }">{{ row.downloads ?? '—' }}</template>
          </el-table-column>
          <el-table-column label="校验" width="80">
            <template #default="{ row }">{{ VERIFIED_LABEL[row.verified] || '—' }}</template>
          </el-table-column>
          <el-table-column label="耗时" width="90">
            <template #default="{ row }">{{ row.duration_sec != null ? `${row.duration_sec}s` : '—' }}</template>
          </el-table-column>
          <el-table-column label="数据最新" min-width="100">
            <template #default="{ row }">{{ fmtYMD(row.data_latest) }}</template>
          </el-table-column>
        </el-table>
      </section>

      <!-- ================= 同步日志 ================= -->
      <section class="card">
        <h3 class="card-title">
          同步日志
          <el-button text size="small" :icon="Bottom" @click="scrollLogBottom">回到底部</el-button>
        </h3>
        <!-- 日志为空时的占位；正常时 pre 等宽字体展示，30s 轮询自动追加 -->
        <pre v-if="!syncLog" class="log-pre hint">（暂无同步日志）</pre>
        <pre v-else ref="logEl" class="log-pre">{{ syncLog }}</pre>
      </section>

      <!-- ================= 容器区 ================= -->
      <section class="card">
        <h3 class="card-title">容器（stockdb 进程）</h3>
        <div class="actions">
          <el-button :icon="Document" @click="toggleContainerLog">
            {{ containerLogOpen ? '收起容器日志' : '展开容器日志' }}
          </el-button>
          <el-button type="danger" :icon="RefreshRight" :loading="restarting" :disabled="status.sync_running" @click="doRestart">
            重启 stockdb
          </el-button>
          <span class="hint">重启期间行情服务短暂中断；同步进行中禁止重启（后端同样拒绝）</span>
        </div>
        <!-- 容器日志：展开时才拉取（懒加载，省请求） -->
        <pre v-if="containerLogOpen" class="log-pre">{{ containerLog || '（stockdb 日志为空）' }}</pre>
      </section>

      <!-- ================= 港股同步 ================= -->
      <section class="card">
        <h3 class="card-title">港股日K 同步</h3>
        <p class="card-hint">
          拉取港股日K 写入私有表 <code>hk日k:</code>（东财优先、腾讯降级），按代码隔离存储。代码逗号/空格分隔，如
          <code>00700, 00941</code>。
        </p>
        <div class="actions">
          <el-input
            v-model="hkCodes"
            placeholder="港股代码，逗号分隔，如 00700,00941"
            style="width: 320px"
            @keyup.enter="doHkSync"
          />
          <el-input-number v-model="hkYears" :min="1" :max="10" :step="1" />
          <span class="hint">年数（保留最近 N 年日K）</span>
          <el-button type="primary" :loading="hkBusy" :icon="Download" @click="doHkSync">开始同步</el-button>
        </div>
        <!-- 结果卡：每只代码一行，成功显示写入根数，失败显示后端 error 文案 -->
        <el-table v-if="hkResult.length" :data="hkResult" size="small" border class="hk-result">
          <el-table-column prop="code" label="代码" width="110" />
          <el-table-column label="状态" width="90">
            <template #default="{ row }">
              <el-tag :type="row.ok ? 'success' : 'danger'" size="small">{{ row.ok ? '成功' : '失败' }}</el-tag>
            </template>
          </el-table-column>
          <el-table-column label="结果">
            <template #default="{ row }">{{ row.detail }}</template>
          </el-table-column>
        </el-table>
      </section>

    </template>
  </div>
</template>

<script setup>
// ================= 引入 =================
// vue 组合式 API：ref 造响应式变量；computed 派生；生命周期钩子管轮询
import { ref, computed, nextTick, onMounted, onUnmounted } from 'vue'
// ElMessage / ElMessageBox 是"命令式"弹窗，不走模板组件，必须显式 import
import { ElMessage, ElMessageBox } from 'element-plus'
// 按钮上的图标：:icon 属性需要"真实的组件对象"（全局注册只对模板标签生效），
// 所以和 EmptyState.vue 一样从图标包里显式 import（包已在 package.json 依赖里）
import {
  Refresh, VideoPlay, SwitchButton, Bottom, Document, RefreshRight, Download,
} from '@element-plus/icons-vue'
// 状态域 API：本页所有后端调用都从这一个模块来（接口变化只改 api/status.js）
import {
  getStatus, getHistory, getSchedule, saveSchedule,
  getLog, getContainerLogs, restartContainer, runSync, hkSync,
} from '../api/status.js'
// 现成资产：指标卡 / 空态 / 格式化工具
import StatCard from '../components/StatCard.vue'
import EmptyState from '../components/EmptyState.vue'
import { fmtYMD } from '../utils/format.js'
// 全局 store：数据滞后天数等"总览相关"字段直接读 store，不重复轮询
import { useGlobalStore } from '../stores/global.js'

// ================= 常量（直接暴露给模板） =================
// 同步阶段 → 中文标签 与 进度百分比（对应后端 _sync_state.phase 取值）
const PHASE_LABEL = {
  idle: '空闲', stopping: '停止服务', syncing: '同步数据中',
  restarting: '重启服务', verifying: '数据校验', done: '已完成',
}
const PHASE_PCT = {
  idle: 0, stopping: 10, syncing: 45, restarting: 70, verifying: 85, done: 100,
}
// 触发来源 → 中文（后端 trigger 字段）
const TRIGGER_LABEL = { scheduled: '⏰ 定时', 'scheduled-retry': '↻ 定时·重试', manual: '手动' }
// 校验结果 → 中文（后端 verified 字段）
const VERIFIED_LABEL = { pass: '通过', fail: '失败', skipped: '跳过' }
// sync_cap.checks 的键名 → 中文（供能力检查列表展示）
const CAP_LABELS = { updater: '更新程序', source: '数据源', writable: '数据卷', retry_pending: '待重试' }
// 定时时间点选项：每 15 分钟一个，00:00 ~ 23:45（配合 el-select allow-create 可输入任意 HH:MM）
const TIME_OPTIONS = Array.from({ length: 96 }, (_, i) => {
  const h = String(Math.floor(i / 4)).padStart(2, '0')
  const m = String((i % 4) * 15).padStart(2, '0')
  return `${h}:${m}`
})
// 轮询节拍：与全局约定一致，30 秒一次
const POLL_MS = 30_000

// ================= 状态 =================
const store = useGlobalStore()
const status = ref(null)      // /api/status 全量载荷（含 container/sync/disk/calendar...）
const history = ref([])       // /api/history 同步历史（后端按时间正序追加，末尾最新）
const schedule = ref(null)    // /api/schedule 定时配置
const syncLog = ref('')       // /api/log 同步日志尾部
const containerLog = ref('')  // /api/container/logs 容器日志
const containerLogOpen = ref(false) // 容器日志展开开关（懒加载）
const loading = ref(false)    // 首拉/手动刷新中
const error = ref(null)       // 最近一次失败文案（页面顶部 alert，不打断使用）
const syncBusy = ref(false)   // 同步请求进行中（按钮 loading）
const restarting = ref(false) // 重启请求进行中
const logEl = ref(null)       // 日志 pre 的 DOM 引用（自动滚动用）

// 定时计划表单（与后端 schedule 对象双向同步；schDirty 防轮询吞掉未保存草稿）
const schEnabled = ref(false)
const schTimes = ref([])
const schTrading = ref(true)
const schSaving = ref(false)
const schDirty = ref(false)
// 用户手动改过表单（@change 事件只在用户交互时触发，程序赋值不会误标脏）
function markSchDirty() {
  schDirty.value = true
}

// 港股表单
const hkCodes = ref('')
const hkYears = ref(2)
const hkBusy = ref(false)
const hkResult = ref([]) // 结果行：{code, ok, detail}

// ================= 派生值 =================
// 当前同步阶段百分比（同步中才有意义，空闲给 0）
const phasePct = computed(() => PHASE_PCT[status.value?.sync_phase] ?? 0)
const phaseLabel = computed(() => PHASE_LABEL[status.value?.sync_phase] ?? '处理中')
// 同步已运行时长文本：sync_started 是 epoch 秒，和当前时间相减
const syncElapsedText = computed(() => {
  const s = status.value?.sync_started
  if (!s) return ''
  const sec = Math.max(0, Math.floor(Date.now() / 1000 - s))
  const h = Math.floor(sec / 3600)
  const m = String(Math.floor((sec % 3600) / 60)).padStart(2, '0')
  const ss = String(sec % 60).padStart(2, '0')
  return h ? `${h}:${m}:${ss}` : `${m}:${ss}`
})
// 最近一次同步摘要（/api/status 里的 last_sync = 历史数组最后一条）
const lastSync = computed(() => status.value?.last_sync ?? null)
const lastExitCode = computed(() => status.value?.exit_code ?? null)
const lastExitOk = computed(() => lastExitCode.value === 0)
// 数据新鲜度着色：滞后天数读全局 store（App.vue 已轮询 /api/overview）
const freshTone = computed(() => {
  const lag = store.health?.lag_days
  if (lag == null) return ''
  if (lag <= 1) return 'ok'
  if (lag === 2) return 'warn'
  return 'err'
})
// 覆盖范围文本：coverage{earliest,latest} 是 8 位数字 → 只取年份，如 '1990 ~ 2026'
const coverageText = computed(() => {
  const c = status.value?.coverage
  if (!c || !c.earliest) return '—'
  return `${String(c.earliest).slice(0, 4)} ~ ${String(c.latest).slice(0, 4)}`
})
// 磁盘：百分比 + 文字（el-progress 需要 0~100 整数）
const diskPct = computed(() => {
  const d = status.value?.disk
  if (!d || d.total_gb == null || !d.total_gb) return null
  return Math.round((d.used_gb / d.total_gb) * 100)
})
const diskText = computed(() => {
  const d = status.value?.disk
  if (!d || d.total_gb == null) return '—'
  return `${d.used_gb} GB / ${d.total_gb} GB · ${d.free_gb ?? '?'} GB 可用`
})
// 定时提示：启用 + 仅交易日 且 今天不是交易日 → 提示会跳过
const schTodayNote = computed(() => {
  if (!schEnabled.value || !schTrading.value || !status.value) return ''
  return status.value.trading_today ? '' : '今日非交易日，定时将跳过'
})

// ================= 数据加载 =================
// 拉 /api/status；失败把文案写进 error（页面级，不抛崩溃）
async function loadStatus() {
  try {
    const data = await getStatus()
    if (data) {
      status.value = data
      error.value = null
    }
  } catch (e) {
    error.value = e?.message || '状态接口不可用'
  }
}

// 拉 /api/history：数组直接给表格（新→旧排列由模板 reverse 处理）
async function loadHistory() {
  try {
    const data = await getHistory()
    history.value = (data?.history || []).slice().reverse()
  } catch (e) {
    error.value = e?.message || '同步历史接口不可用'
  }
}

// 拉 /api/schedule：配置同步到表单——但用户有未保存草稿（schDirty）时跳过，
// 避免 30s 轮询把正在编辑的时间点/开关重置掉（旧页也有同样的防吞机制）
async function loadSchedule() {
  try {
    const data = await getSchedule()
    if (data?.schedule) {
      schedule.value = data.schedule
      if (!schDirty.value) {
        schEnabled.value = !!data.schedule.enabled
        schTimes.value = data.schedule.times || []
        schTrading.value = data.schedule.trading_only !== false
      }
    }
  } catch (e) {
    error.value = e?.message || '定时配置接口不可用'
  }
}

// 拉 /api/log?n=80：同步日志尾部
async function loadSyncLog() {
  try {
    const data = await getLog(80)
    syncLog.value = data?.log ?? '（暂无同步日志）'
    // 数据更新后把滚动条拉到底部（日志是往下长的，用户通常要看最新）
    await nextTick()
    if (logEl.value) logEl.value.scrollTop = logEl.value.scrollHeight
  } catch (e) {
    error.value = e?.message || '同步日志接口不可用'
  }
}

// 整页刷新入口：manual=true 时显示 loading（按钮转圈），轮询静默
async function loadAll(manual = false) {
  if (manual) loading.value = true
  try {
    await Promise.all([loadStatus(), loadHistory(), loadSchedule(), loadSyncLog()])
  } finally {
    loading.value = false
  }
}

// ================= 操作 =================
// 启动同步：hot=true 热更新 / hot=false 停服严格。同步会向数据卷写入数据，
// 属于"写入操作"，启动前必须先 ElMessageBox.confirm 二次确认（用户取消直接 return）。
async function doSync(hot) {
  const mode = hot ? '热更新' : '停服严格同步'
  try {
    await ElMessageBox.confirm(
      `确认启动${mode}？${hot
        ? '热更新：stockdb 保持运行、增量同步后自动 reload（零中断）。'
        : '停服严格：按官方要求先停止服务再同步，期间行情服务会中断。'}`,
      '启动同步',
      { type: 'warning', confirmButtonText: '启动', cancelButtonText: '取消' },
    )
  } catch {
    return // 用户点了取消
  }
  syncBusy.value = true
  try {
    const r = await runSync(hot)
    const msg = r?.msg || '已启动同步'
    if (msg.includes('运行中')) {
      ElMessage.warning(msg) // 被定时任务占用：只提示，不做多余动作（与旧页一致）
    } else {
      ElMessage.success(msg)
      // 刚启动：立刻补拉一次状态 + 日志，不用等 30s 轮询
      await Promise.all([loadStatus(), loadSyncLog()])
    }
  } catch (e) {
    ElMessage.error(e?.message || '启动同步失败')
  } finally {
    syncBusy.value = false
  }
}

// 重启 stockdb：危险操作，ElMessageBox.confirm 二次确认（用户取消直接 return）
async function doRestart() {
  try {
    await ElMessageBox.confirm(
      '确定重启 stockdb 进程？重启期间行情服务会短暂中断。',
      '危险操作',
      { type: 'warning', confirmButtonText: '重启', cancelButtonText: '取消' },
    )
  } catch {
    return // 用户点了取消
  }
  restarting.value = true
  try {
    const r = await restartContainer()
    ElMessage.success(r?.msg || '已发送重启')
    await loadStatus() // 立即刷新进程状态（后端有 5s 缓存，随后轮询会继续更新）
  } catch (e) {
    ElMessage.error(e?.message || '重启失败')
  } finally {
    restarting.value = false
  }
}

// 保存定时计划：先做客户端校验（空时间点），再二次确认（写入操作），最后调 saveSchedule
async function saveSch() {
  if (!schTimes.value.length) {
    ElMessage.warning('至少保留一个执行时间点（HH:MM）')
    return
  }
  try {
    await ElMessageBox.confirm(
      `确认保存定时计划？将按 ${schTimes.value.length} 个时间点、${schTrading.value ? '仅交易日' : '每天'} 触发。`,
      '保存定时计划',
      { type: 'warning', confirmButtonText: '保存', cancelButtonText: '取消' },
    )
  } catch {
    return // 用户点了取消
  }
  schSaving.value = true
  try {
    const r = await saveSchedule(schEnabled.value, schTimes.value, schTrading.value)
    ElMessage.success(r?.msg || '定时计划已保存')
    // 后端返回最新 schedule，直接同步回来（含 next_trigger 等派生字段）
    if (r?.schedule) {
      schedule.value = r.schedule
      schEnabled.value = !!r.schedule.enabled
      schTimes.value = r.schedule.times || []
      schTrading.value = r.schedule.trading_only !== false
    }
    schDirty.value = false // 保存成功 = 草稿已落盘，恢复轮询同步
  } catch (e) {
    ElMessage.error(e?.message || '保存失败')
  } finally {
    schSaving.value = false
  }
}

// 容器日志：展开时才拉（懒加载）；收起时保留旧内容
async function toggleContainerLog() {
  containerLogOpen.value = !containerLogOpen.value
  if (containerLogOpen.value && !containerLog.value) {
    try {
      const r = await getContainerLogs(150)
      containerLog.value = r?.log || ''
      if (r?.error) containerLog.value += `\n\n${r.error}`
    } catch (e) {
      ElMessage.error(e?.message || '读取容器日志失败')
    }
  }
}

// 港股同步：解析逗号/空格分隔的代码列表 → 二次确认（写入操作） → 调 hkSync → 拼结果行
async function doHkSync() {
  const codes = hkCodes.value.split(/[,，\s]+/).map((s) => s.trim()).filter(Boolean)
  if (!codes.length) {
    ElMessage.warning('请输入港股代码（如 00700,00941）')
    return
  }
  try {
    await ElMessageBox.confirm(
      `确认开始港股同步（${codes.length} 只代码，保留最近 ${hkYears.value || 2} 年）？将写入私有表 hk日k:。`,
      '港股同步',
      { type: 'warning', confirmButtonText: '开始同步', cancelButtonText: '取消' },
    )
  } catch {
    return // 用户点了取消
  }
  hkBusy.value = true
  try {
    const r = await hkSync(codes, hkYears.value || 2)
    // 后端返回 { code: {ok, bars, latest} | {ok:false, error} } 的对象映射
    const rows = Object.entries(r || {}).map(([code, v]) => ({
      code,
      ok: !!v.ok,
      detail: v.ok ? `写入 ${v.bars} 根日K，最新 ${fmtYMD(v.latest)}` : (v.error || '失败'),
    }))
    hkResult.value = rows
    const failed = rows.filter((x) => !x.ok)
    if (failed.length === 0) ElMessage.success(`港股同步完成（${rows.length} 只全部成功）`)
    else if (failed.length === rows.length) ElMessage.error(`港股同步失败：${failed.map((x) => x.detail).join('；')}`)
    else ElMessage.warning(`${rows.length - failed.length} 只成功，${failed.length} 只失败（见结果表）`)
  } catch (e) {
    ElMessage.error(e?.message || '港股同步请求失败')
  } finally {
    hkBusy.value = false
  }
}

// ================= 表格派生（历史） =================
// 结果标签类型：成功/未生效/运行中/失败 四态
function resultTagType(row) {
  if (row.exit_code === 0) return row.warn ? 'warning' : 'success'
  if (row.exit_code == null) return 'info'
  return 'danger'
}
function resultText(row) {
  if (row.exit_code === 0) return row.warn ? '未生效' : '成功'
  if (row.exit_code == null) return '运行中'
  return '失败'
}

// ================= 工具函数 =================
// epoch 秒 → 'x 天 x 小时 x 分钟'（进程启动时长，对应旧页 fmtUptime）
function fmtUptime(started) {
  if (!started) return '—'
  const sec = Math.max(0, Math.floor(Date.now() / 1000 - started))
  const d = Math.floor(sec / 86400)
  const h = Math.floor((sec % 86400) / 3600)
  const m = Math.floor((sec % 3600) / 60)
  return (d ? `${d} 天 ` : '') + (h ? `${h} 小时 ` : '') + `${m} 分钟`
}
// 日志滚动回底部（手动按钮）
function scrollLogBottom() {
  if (logEl.value) logEl.value.scrollTop = logEl.value.scrollHeight
}

// ================= 生命周期：首拉 + 30s 轮询 + 卸载清理 =================
let pollTimer = null
onMounted(() => {
  loadAll() // 首次进入拉全量
  // 30s 轮询：只静默刷新（失败写 error，不弹窗打扰）；容器日志不随轮询（懒加载）
  pollTimer = setInterval(() => {
    loadStatus()
    loadHistory()
    loadSchedule()
    loadSyncLog()
  }, POLL_MS)
})
// 离开页面必须清定时器，否则定时器在后台空转、卸载后还可能更新已销毁的组件
onUnmounted(() => {
  if (pollTimer) clearInterval(pollTimer)
  pollTimer = null
})
</script>

<style scoped>
/* 页面容器：纵向排列各卡片，卡片之间 16px 间距 */
.page {
  display: flex;
  flex-direction: column;
  gap: 16px;
}
.page-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
}
.page-title {
  margin: 0;
  font-size: 18px;
  color: var(--text);
}
.page-alert {
  margin-bottom: 0;
}

/* 通用卡片样式：与全局主题变量联动（--panel/--line） */
.card {
  background: var(--panel);
  border: 1px solid var(--line);
  border-radius: 12px;
  padding: 16px;
}
.card-title {
  margin: 0 0 12px;
  font-size: 15px;
  font-weight: 600;
  color: var(--text);
}
.card-hint {
  margin: 0 0 12px;
  font-size: 12px;
  color: var(--muted);
}

/* 同步横幅：正在同步/空闲两态 */
.sync-banner {
  display: flex;
  flex-direction: column;
  gap: 8px;
  padding: 12px;
  border-radius: 8px;
  background: var(--panel2);
  margin-bottom: 12px;
}
.sync-banner.idle {
  flex-direction: row;
  align-items: center;
  gap: 12px;
}

/* 指标卡栅格：auto-fit + minmax(200px,1fr) 自动换行，宽屏一排 4 张（全站统一口径） */
.stat-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
  gap: 12px;
  margin: 12px 0;
}

/* 磁盘条 */
.disk-block {
  margin: 8px 0 12px;
}
.disk-label {
  display: flex;
  justify-content: space-between;
  font-size: 13px;
  color: var(--text);
  margin-bottom: 6px;
}

/* 能力检查列表 */
.cap-block {
  border-top: 1px dashed var(--line);
  padding-top: 10px;
}
.cap-title {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 13px;
  color: var(--text);
  margin-bottom: 6px;
}
.cap-list {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-wrap: wrap;
  gap: 8px 20px;
}
.cap-list li {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 13px;
}
.cap-name {
  color: var(--text);
}

/* 描述块与操作行 */
.desc {
  margin-bottom: 4px;
}
.actions {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 10px;
}

/* 定时计划 */
.sch-row {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 12px;
  margin-bottom: 10px;
}
.sch-label {
  font-size: 13px;
  color: var(--text);
}
.sch-info {
  margin-bottom: 10px;
}

/* 历史详情（展开列内容） */
.hist-detail {
  padding: 8px 16px;
  font-size: 13px;
  color: var(--muted);
}

/* 日志 pre：等宽字体 + 固定高度内部滚动，深色底与旧面板观感一致 */
.log-pre {
  margin: 8px 0 0;
  padding: 10px;
  max-height: 260px;
  overflow: auto;
  background: var(--panel2);
  border: 1px solid var(--line);
  border-radius: 8px;
  font: 12px/1.5 ui-monospace, 'SF Mono', Menlo, Consolas, monospace;
  color: var(--text);
  white-space: pre-wrap;
  word-break: break-all;
}

.hk-result {
  margin-top: 12px;
}
.hint {
  font-size: 12px;
  color: var(--muted);
}
</style>
