<template>
  <!-- ============================================================
       系统健康页（/ops/health）——任务 G2。
       LuCI 一页一职责：容器 / 磁盘 / 数据健康 / 同步能力 / 日志 / 重启
       全部收在这一个页面，用户不用翻菜单就能判断"系统行不行、要不要动它"。
       学习点：
       1) 三个数据源（getHealth / getStatus / getDiag）并行拉取、各自降级，
          任一个失败只记 error 文案，不拖垮其它卡片；
       2) 三态齐备：首拉骨架屏 → 首拉失败且无数据给 EmptyState+重试 →
          正常态（轮询失败但有旧数据时顶部弱提示，不打断浏览）；
       3) 危险操作（重启）ElMessageBox.confirm 二次确认；日志懒加载（展开才拉）。
       ============================================================ -->
  <div class="page">

    <!-- ============ 页头：小标题 + 右侧操作（LuCI 紧凑风格） ============ -->
    <div class="page-head">
      <h2 class="page-title">系统健康</h2>
      <div class="page-actions">
        <el-button :icon="Refresh" :loading="loading" @click="loadAll(true)">手动刷新</el-button>
      </div>
    </div>

    <!-- 三态一：加载中（骨架屏）——首拉数据没回来前显示 -->
    <el-skeleton v-if="loading && !hasData" :rows="10" animated />

    <!-- 三态二：错误态——首拉就失败且手里没有任何数据 → EmptyState + 重试 -->
    <EmptyState
      v-else-if="error && !hasData"
      icon="WarningFilled"
      title="健康数据加载失败"
      :description="error"
    >
      <el-button type="primary" @click="loadAll(true)">重试</el-button>
    </EmptyState>

    <!-- 三态三：正常态——数据在手，逐块渲染 -->
    <template v-else>
      <!-- 轮询失败但手里有旧数据：顶部一行弱提示，表格照常展示（降级不崩） -->
      <el-alert
        v-if="error"
        class="page-alert"
        type="warning"
        :closable="true"
        :title="`最近刷新失败：${error}（将自动重试）`"
        show-icon
        @close="error = ''"
      />

      <!-- ================= 1. 健康卡：getHealth() ================= -->
      <section class="card">
        <h3 class="card-title">数据健康</h3>
        <!-- StatCard 组合：latest / lag_days / mirror / status，滞后着色 -->
        <div class="stat-grid">
          <StatCard
            label="数据最新"
            :value="health ? fmtYMD(health.latest) : '—'"
            :tone="healthTone"
            sub="latest（000001 日K 最大日期）"
          />
          <StatCard
            label="滞后天数"
            :value="health?.lag_days != null ? `${health.lag_days} 天` : '—'"
            :tone="healthTone"
            sub="lag_days（工作日口径）"
          />
          <StatCard label="镜像日期" :value="health?.mirror || '—'" sub="mirror（镜像源标注）" />
          <StatCard label="健康状态" :value="statusLabel" :tone="statusTone" sub="status（ok/stale/unknown）" />
        </div>
        <!-- note：后端给出的一句话判断（如"可同步" / "镜像尚未发布"），放卡片底部 -->
        <div v-if="health?.note" class="note-line">{{ health.note }}</div>
      </section>

      <!-- ================= 2. 容器卡：getStatus().container + 日志 + 重启 ================= -->
      <section class="card">
        <!-- 卡片标题行：左标题、右操作（危险重启按钮就近放置，一眼可见） -->
        <div class="card-head">
          <h3 class="card-title">容器（stockdb 进程）</h3>
          <div class="card-actions">
            <el-button text size="small" :icon="Document" @click="toggleContainerLog">
              {{ containerLogOpen ? '收起容器日志' : '展开容器日志' }}
            </el-button>
            <el-button
              type="danger"
              size="small"
              :icon="RefreshRight"
              :loading="restarting"
              :disabled="status?.sync_running"
              @click="doRestart"
            >重启 stockdb</el-button>
          </div>
        </div>

        <!-- el-descriptions 一行表格排版进程状态/时长/镜像/备注 -->
        <el-descriptions :column="2" border size="small">
          <el-descriptions-item label="进程状态">
            <el-tag :type="container?.ok ? 'success' : 'danger'" size="small">
              {{ container?.ok ? '运行中' : '已停止' }}
            </el-tag>
            <span class="hint" style="margin-left: 6px">{{ container?.note || '' }}</span>
          </el-descriptions-item>
          <el-descriptions-item label="运行时长">
            {{ fmtUptime(container?.started) }}
          </el-descriptions-item>
          <el-descriptions-item label="状态原文">
            {{ container?.status || '—' }}
          </el-descriptions-item>
          <el-descriptions-item label="镜像">
            {{ container?.image || '—' }}
          </el-descriptions-item>
        </el-descriptions>

        <!-- 容器日志：展开才拉取（懒加载，省请求）；pre 等宽 + 深色底 -->
        <pre v-if="containerLogOpen" class="log-pre">{{ containerLog || '（stockdb 日志为空）' }}</pre>
      </section>

      <!-- ================= 3. 磁盘卡：getStatus().disk ================= -->
      <section class="card">
        <h3 class="card-title">磁盘用量</h3>
        <!-- disk_usage 返回 {total_gb, used_gb, free_gb}（异常时全为 null），
             只有拿到 total 才画进度条，否则给"不可用"降级文案 -->
        <template v-if="diskPct != null">
          <el-progress
            :percentage="diskPct"
            :stroke-width="12"
            :color="diskColor"
            :format="() => `${diskPct}%`"
          />
          <div class="note-line">{{ diskText }}</div>
        </template>
        <div v-else class="hint">磁盘信息不可用（shutil.disk_usage 失败时后端返回 null 字段）</div>
      </section>

      <!-- ================= 4. 同步能力卡：getStatus().sync_cap ================= -->
      <section class="card">
        <h3 class="card-title">同步能力</h3>
        <div v-if="status?.sync_cap" class="cap-block">
          <div class="cap-title">
            能力总览
            <el-tag :type="status.sync_cap.ok ? 'success' : 'danger'" size="small">
              {{ status.sync_cap.ok ? '可用' : '不可用' }}
            </el-tag>
            <el-tag v-if="status.sync_cap.warn" type="warning" size="small">有待重试任务</el-tag>
          </div>
          <!-- checks{updater,source,writable,retry_pending}：逐项小圆点着色 -->
          <ul class="cap-list">
            <li v-for="(check, name) in status.sync_cap.checks || {}" :key="name">
              <span class="cap-dot" :style="{ background: dotColor(check) }" />
              <span class="cap-name">{{ CAP_LABELS[name] || name }}</span>
              <span class="hint">{{ check.detail }}</span>
            </li>
          </ul>
        </div>
        <div v-else class="hint">同步能力信息不可用</div>
      </section>

      <!-- ================= 5. 环境信息卡：getDiag().env ================= -->
      <section class="card">
        <h3 class="card-title">环境信息</h3>
        <el-descriptions v-if="diag?.env" :column="2" border size="small">
          <el-descriptions-item label="Python">{{ diag.env.python || '—' }}</el-descriptions-item>
          <el-descriptions-item label="架构">{{ diag.env.arch || '—' }}</el-descriptions-item>
          <el-descriptions-item label="WebUI 版本">{{ diag.env.webui_version || '—' }}</el-descriptions-item>
          <el-descriptions-item label="界面模式">{{ diag.env.ui_mode || '—' }}</el-descriptions-item>
          <el-descriptions-item label="镜像 tag">{{ diag.env.image_tag || '—' }}</el-descriptions-item>
          <el-descriptions-item label="启动时间">{{ diag.env.started || '—' }}</el-descriptions-item>
          <!-- uptime_seconds 是秒数 → 转成 'X天X时X分'（见 fmtUptimeSec） -->
          <el-descriptions-item label="运行时长">{{ fmtUptimeSec(diag.env.uptime_seconds) }}</el-descriptions-item>
          <el-descriptions-item label="数据目录">{{ diag.env.data_dir || '—' }}</el-descriptions-item>
          <el-descriptions-item label="数据最新">{{ fmtYMD(diag.env.data_latest) }}</el-descriptions-item>
        </el-descriptions>
        <div v-else class="hint">环境信息不可用</div>
        <!-- 底部一行：诊断生成时间 + 跳转完整诊断页（/ops/diag） -->
        <div class="env-foot">
          <span class="hint">
            诊断生成于 {{ diag?.generated_at ? String(diag.generated_at).slice(0, 19).replace('T', ' ') : '—' }}
          </span>
          <router-link class="diag-link" to="/ops/diag">完整诊断 →</router-link>
        </div>
      </section>

    </template>
  </div>
</template>

<script setup>
// ================= 引入 =================
// 组合式 API：ref 造响应式变量；computed 派生展示值；生命周期钩子管轮询
import { ref, computed, onMounted, onUnmounted } from 'vue'
// ElMessage / ElMessageBox 是"命令式"弹窗，不走模板组件，必须显式 import
import { ElMessage, ElMessageBox } from 'element-plus'
// 按钮上的图标：:icon 属性需要"真实的组件对象"，从图标包显式 import
import { Refresh, Document, RefreshRight } from '@element-plus/icons-vue'
// 状态域 API（容器/健康/日志/重启都在这一个模块里）
import { getHealth, getStatus, getContainerLogs, restartContainer } from '../api/status.js'
import { getDiag } from '../api/diag.js' // 诊断聚合（env 环境信息用）
// 现成资产：指标卡 / 空态 / 格式化
import StatCard from '../components/StatCard.vue'
import EmptyState from '../components/EmptyState.vue'
import { fmtYMD } from '../utils/format.js'

// ================= 常量 =================
// 轮询节拍：全站约定统一 30s（环境信息与健康一起 30s 轮询——
// getDiag 是本地只读聚合，无网络开销，不必单独放慢）
const POLL_MS = 30_000
// sync_cap.checks 的键名 → 中文（供能力检查列表展示）
const CAP_LABELS = { updater: '更新程序', source: '数据源', writable: '数据卷', retry_pending: '待重试' }

// ================= 状态 =================
const health = ref(null)          // /api/health：数据健康卡
const status = ref(null)          // /api/status：容器/磁盘/同步能力（取用其中子块）
const diag = ref(null)            // /api/diag：env 环境信息
const containerLog = ref('')      // /api/container/logs 容器日志文本
const containerLogOpen = ref(false) // 容器日志展开开关（懒加载）
const loading = ref(true)         // 首拉/手动刷新中（骨架屏依据）
const error = ref('')             // 最近一次失败文案（顶部弱提示）
const restarting = ref(false)     // 重启请求进行中（按钮 loading）

// 互斥：上一轮还没回来就跳过本轮，避免轮询请求堆积
let busy = false
let pollTimer = null

// ================= 派生值 =================
// 是否已有任何数据：决定骨架屏 / 错误空态 / 正常态的分支走向
const hasData = computed(() => health.value != null || status.value != null || diag.value != null)
// 容器子块（status.container），取不到给 null，模板里 ?. 兜底
const container = computed(() => status.value?.container ?? null)

// 滞后着色：≤1 天正常（ok）/ 2 天警告 / >2 天错误；未知（null）不给色
const healthTone = computed(() => {
  const lag = health.value?.lag_days
  if (lag == null) return ''
  if (lag <= 1) return 'ok'
  if (lag === 2) return 'warn'
  return 'err'
})
// 健康状态文案与颜色：ok→正常 / stale→落后 / unknown→未知
const statusLabel = computed(
  () => ({ ok: '正常', stale: '落后', unknown: '未知' })[health.value?.status] ?? '—'
)
const statusTone = computed(() => {
  const st = health.value?.status
  if (st === 'ok') return 'ok'
  if (st === 'stale') return 'warn'
  return 'err' // unknown：拿不到日期属于异常，标红提醒
})

// 磁盘：百分比（el-progress 需要 0~100 整数）；total 缺失/为 0 时给 null（不画条）
const diskPct = computed(() => {
  const d = status.value?.disk
  if (!d || d.total_gb == null || !d.total_gb) return null
  return Math.round((d.used_gb / d.total_gb) * 100)
})
// 颜色走语义 CSS 变量（随主题自动切换）：>80% 红 / >60% 黄 / 否则绿
const diskColor = computed(() =>
  diskPct.value > 80 ? 'var(--err)' : diskPct.value > 60 ? 'var(--warn)' : 'var(--ok)'
)
// 磁盘明细文案（note）：已用 / 共 / 可用
const diskText = computed(() => {
  const d = status.value?.disk
  if (!d || d.total_gb == null) return ''
  return `已用 ${d.used_gb ?? '?'} GB / 共 ${d.total_gb} GB · 可用 ${d.free_gb ?? '?'} GB`
})

// ================= 数据加载 =================
// 三个接口并行拉取、各自降级：单块失败只记 error，不影响其它卡片
async function loadHealth() {
  try {
    const data = await getHealth()
    if (data) health.value = data
    return true
  } catch (e) {
    error.value = e?.message || '健康接口不可用'
    return false
  }
}
async function loadStatus() {
  try {
    const data = await getStatus()
    if (data) status.value = data
    return true
  } catch (e) {
    error.value = e?.message || '状态接口不可用'
    return false
  }
}
async function loadDiag() {
  try {
    const data = await getDiag()
    if (data) diag.value = data
    return true
  } catch (e) {
    error.value = e?.message || '诊断接口不可用'
    return false
  }
}

// 整页刷新入口：manual=true 时按钮转圈（首拉/手动）；轮询静默
async function loadAll(manual = false) {
  if (busy) return
  busy = true
  if (manual) loading.value = true
  try {
    const results = await Promise.all([loadHealth(), loadStatus(), loadDiag()])
    // 三块全部成功才清错误文案（有旧数据时页面继续展示，只留顶部弱提示）
    if (results.every(Boolean)) error.value = ''
  } finally {
    loading.value = false
    busy = false
  }
}

// ================= 操作 =================
// 容器日志：展开时才拉取（懒加载）；收起时保留旧内容；每次展开刷新一次
async function toggleContainerLog() {
  containerLogOpen.value = !containerLogOpen.value
  if (containerLogOpen.value) {
    try {
      const r = await getContainerLogs(150)
      containerLog.value = r?.log || ''
      if (r?.error) containerLog.value += `\n\n${r.error}`
    } catch (e) {
      ElMessage.error(e?.message || '读取容器日志失败')
    }
  }
}

// 重启 stockdb：危险操作，ElMessageBox.confirm 二次确认（取消直接 return）
async function doRestart() {
  try {
    await ElMessageBox.confirm(
      '确定重启 stockdb 进程？重启期间行情服务会短暂中断，建议避开交易时段执行。',
      '危险操作',
      { type: 'warning', confirmButtonText: '重启', cancelButtonText: '取消' },
    )
  } catch {
    return // 用户点了取消：什么都不做
  }
  restarting.value = true
  try {
    const r = await restartContainer()
    ElMessage.success(r?.msg || '已发送重启')
    // 立即刷新进程状态（后端容器探测有 5s 缓存，随后轮询继续跟进）
    await loadStatus()
  } catch (e) {
    ElMessage.error(e?.message || '重启失败')
  } finally {
    restarting.value = false
  }
}

// ================= 工具函数 =================
// epoch 秒（进程启动时间）→ 'X天X时X分'：与当前时间相减得到已运行时长
function fmtUptime(started) {
  if (!started) return '—'
  return fmtUptimeSec(Date.now() / 1000 - started)
}
// 秒数 → 'X天X时X分'（环境信息 uptime_seconds 直接用；不足 1 小时只显示分钟）
function fmtUptimeSec(sec) {
  if (sec == null || !Number.isFinite(sec) || sec < 0) return '—'
  const s = Math.floor(sec)
  const d = Math.floor(s / 86400)
  const h = Math.floor((s % 86400) / 3600)
  const m = Math.floor((s % 3600) / 60)
  return (d ? `${d}天` : '') + (h ? `${h}时` : '') + `${m}分`
}

// 同步能力小圆点颜色：ok===false → 红；warn → 黄；其余 → 绿
function dotColor(check) {
  if (check.ok === false) return 'var(--err)'
  if (check.warn) return 'var(--warn)'
  return 'var(--ok)'
}

// ================= 生命周期：首拉 + 30s 轮询 + 卸载清理 =================
onMounted(() => {
  loadAll() // 首次进入拉全量
  // 30s 轮询：只静默刷新（失败写 error，不弹窗打扰）
  pollTimer = setInterval(() => loadAll(), POLL_MS)
})
// 离开页面必须清定时器，否则定时器后台空转、卸载后还可能更新已销毁的组件
onUnmounted(() => {
  if (pollTimer) clearInterval(pollTimer)
  pollTimer = null
})
</script>

<style scoped>
/* 页面容器：纵向排列各卡片，卡片间距 14px（LuCI 密度） */
.page {
  display: flex;
  flex-direction: column;
  gap: 14px;
}
.page-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
}
.page-title {
  margin: 0;
  font-size: 18px;
  font-weight: 700;
  color: var(--text);
}
.page-actions {
  display: flex;
  gap: 8px;
}
.page-alert {
  margin-bottom: 0;
}

/* 通用卡片：圆角 12px + 内边距 14px（任务约定 12-14px） */
.card {
  background: var(--panel);
  border: 1px solid var(--line);
  border-radius: 12px;
  padding: 14px;
}
/* 卡片标题行：左标题、右操作（LuCI 紧凑标题行风格） */
.card-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  flex-wrap: wrap;
  gap: 8px;
  margin-bottom: 12px;
}
.card-title {
  margin: 0;
  font-size: 15px;
  font-weight: 600;
  color: var(--text);
}
.card-actions {
  display: flex;
  align-items: center;
  gap: 4px;
}

/* 指标卡栅格：auto-fit + minmax(200px,1fr) 自动换行（全站统一口径） */
.stat-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
  gap: 12px;
  margin: 4px 0 8px;
}
/* 卡片底部的说明行（健康 note / 磁盘明细） */
.note-line {
  margin-top: 8px;
  font-size: 12px;
  color: var(--muted);
  border-top: 1px dashed var(--line);
  padding-top: 8px;
}

/* 日志 pre：等宽字体 + 固定高度内部滚动，深色底与旧面板观感一致 */
.log-pre {
  margin: 12px 0 0;
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

/* 同步能力列表：逐项小圆点 + 名称 + 明细 */
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
  margin-bottom: 8px;
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
.cap-dot {
  display: inline-block;
  width: 8px;
  height: 8px;
  border-radius: 50%;
  flex-shrink: 0;
}
.cap-name {
  color: var(--text);
}

/* 环境信息卡底部：生成时间 + 完整诊断链接 */
.env-foot {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  margin-top: 10px;
  border-top: 1px dashed var(--line);
  padding-top: 8px;
}
.diag-link {
  font-size: 13px;
  color: var(--brand);
}

.hint {
  font-size: 12px;
  color: var(--muted);
}
</style>
