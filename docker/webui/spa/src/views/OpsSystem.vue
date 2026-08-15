<template>
  <div class="ops-system">
    <!-- ============ 页头：标题 + 操作按钮区 ============ -->
    <div class="page-head">
      <h2 class="page-title">系统</h2>
      <div class="page-actions">
        <!-- 手动刷新：load() 幂等，轮询与手动共用同一个函数，不会叠加请求（busy 互斥） -->
        <el-button :icon="Refresh" @click="load()">刷新</el-button>
        <!-- 重启是危险操作：按钮本身用 danger 色提示风险，真正的确认交给 ElMessageBox -->
        <el-button
          type="danger"
          :icon="RefreshRight"
          :disabled="!status?.container?.ok"
          @click="onRestart"
        >重启 stockdb</el-button>
      </div>
    </div>

    <!-- 进程不可控时提示：container.ok=false（本地开发/无 pidfile），禁用重启与日志 -->
    <el-alert
      v-if="status && status.container && !status.container.ok"
      type="warning"
      :closable="false"
      show-icon
      class="block-alert"
      title="stockdb 进程未运行"
      :description="status.container.note || '进程不可控，重启与日志按钮已禁用'"
    />

    <!-- ============ 三态一：加载态（el-skeleton 骨架屏） ============ -->
    <template v-if="loading">
      <div class="panel">
        <el-skeleton :rows="4" animated />
      </div>
      <div class="panel">
        <el-skeleton :rows="6" animated />
      </div>
    </template>

    <!-- ============ 三态二：错误态（接口挂了：展示文案 + 重试，页面不崩） ============ -->
    <div v-else-if="error && !health && !status" class="error-state">
      <el-alert type="error" :closable="false" show-icon title="系统状态读取失败">
        <template #default>
          {{ error }}
          <el-button size="small" class="retry-btn" @click="load()">重试</el-button>
        </template>
      </el-alert>
    </div>

    <!-- ============ 三态三：空态（请求成功但没有任何数据） ============ -->
    <EmptyState
      v-else-if="!health && !status"
      icon="Setting"
      title="暂无系统状态"
      description="接口已就绪但未返回数据，请稍后重试"
    />

    <!-- ============ 正常态：健康卡 + 状态分块 + 容器日志 ============ -->
    <template v-else>
      <!-- 健康卡：/api/health → latest / lag_days / mirror / status / note -->
      <section class="panel">
        <h3 class="panel-title">数据健康</h3>
        <div class="stat-grid">
          <!-- 数据最新日期：fmtYMD 把 20260814 转成 2026-08-14，方便人读 -->
          <StatCard label="数据最新" :value="health?.latest ? fmtYMD(health.latest) : '—'"
            :tone="lagTone(health?.lag_days)" />
          <!-- 滞后天数：≤1 天正常(ok)、2 天警告(warn)、>2 天错误(err)，颜色随数值变 -->
          <StatCard label="滞后天数" :value="health?.lag_days ?? '—'" :tone="lagTone(health?.lag_days)"
            sub="按工作日计算" />
          <!-- 镜像日期：镜像源标注的最新数据日期，可据此判断"本地落后是不是镜像没发新数据" -->
          <StatCard label="镜像日期" :value="health?.mirror || '—'" sub="镜像源最新" />
          <!-- 状态：ok=正常 / stale=待更新 / unknown=未知，用语义色区分 -->
          <StatCard label="状态" :value="statusText(health?.status)" :tone="statusTone(health?.status)"
            sub="数据健康度" />
        </div>
        <!-- note 是后端给的中文解释（如"可同步"/"镜像尚未发布新数据"），直接展示 -->
        <p v-if="health?.note" class="health-note">{{ health.note }}</p>
      </section>

      <!-- 状态总览：/api/status 的 container / disk / sync_cap 三块 -->
      <section class="block-grid">
        <!-- 进程卡：stockdb 是否在跑 -->
        <div class="panel block">
          <h3 class="panel-title">进程</h3>
          <div class="block-line">
            <span class="dot" :style="{ background: status?.container?.ok ? 'var(--ok)' : 'var(--err)' }" />
            <span class="block-label">{{ status?.container?.ok ? '运行中' : '已停止' }}</span>
            <span class="block-sub">{{ status?.container?.note || '' }}</span>
          </div>
          <!-- started 是进程启动的 epoch 秒，转成"已运行 x 天 x 小时" -->
          <div class="block-line">
            <span class="block-label">运行时长</span>
            <span class="block-sub">{{ fmtUptime(status?.container?.started) }}</span>
          </div>
        </div>

        <!-- 磁盘卡：used/total + 进度条；>80% 转警告色（旧面板同款阈值） -->
        <div class="panel block">
          <h3 class="panel-title">磁盘</h3>
          <template v-if="status?.disk?.total_gb != null">
            <div class="block-line">
              <span class="block-label">已用</span>
              <span class="block-sub">
                {{ status.disk.used_gb }} GB / {{ status.disk.total_gb }} GB
                （{{ status.disk.free_gb ?? '—' }} GB 可用）
              </span>
            </div>
            <el-progress
              :percentage="diskPct"
              :color="diskPct > 80 ? 'var(--warn)' : 'var(--brand)'"
              :stroke-width="10"
            />
          </template>
          <span v-else class="block-sub">磁盘信息不可用</span>
        </div>

        <!-- 同步能力卡：ok=更新程序/数据源/数据卷 全部就绪；checks 逐项列明细 -->
        <div class="panel block">
          <h3 class="panel-title">同步能力</h3>
          <div class="block-line">
            <span class="dot" :style="{ background: status?.sync_cap?.ok ? 'var(--ok)' : 'var(--err)' }" />
            <span class="block-label">{{ status?.sync_cap?.ok ? '可用' : '不可用' }}</span>
            <span v-if="status?.sync_cap?.warn" class="block-sub">含待重试任务（不判不可用）</span>
          </div>
          <!-- checks：updater / source / writable / retry_pending，每项一个点 + 说明 -->
          <div v-for="(c, key) in status?.sync_cap?.checks || {}" :key="key" class="cap-row">
            <span class="dot"
              :style="{ background: c.ok ? 'var(--ok)' : c.warn ? 'var(--warn)' : 'var(--err)' }" />
            <span class="cap-name">{{ capName(key) }}</span>
            <span class="cap-detail">{{ c.detail }}</span>
          </div>
        </div>
      </section>

      <!-- 容器日志：展开/收起；首次展开时拉取 tail=150，之后每次展开重新拉取保持新鲜 -->
      <section class="panel">
        <div class="panel-title-row">
          <h3 class="panel-title">容器日志</h3>
          <div>
            <el-button size="small" text :icon="containerLogsOpen ? 'CaretTop' : 'CaretBottom'"
              @click="toggleLogs">
              {{ containerLogsOpen ? '收起日志' : '展开日志' }}
            </el-button>
            <el-button v-if="containerLogsOpen" size="small" text :icon="Refresh" @click="loadLogs">
              刷新
            </el-button>
          </div>
        </div>

        <template v-if="containerLogsOpen">
          <!-- 日志读取中：占位提示（小范围 loading，不打扰整页） -->
          <div v-if="logsLoading" class="logs-loading">日志加载中…</div>
          <!-- 日志内容：pre 保留换行与缩进；白字配色在深浅主题都可读 -->
          <pre v-else-if="containerLog" class="log-pre">{{ containerLog }}</pre>
          <!-- 空态：日志文件不存在或为空（stockdb 还没写过日志） -->
          <EmptyState
            v-else-if="!logsError"
            icon="Document"
            title="暂无容器日志"
            description="stockdb 日志文件为空或尚未产生"
          />
          <div v-else class="error-state">
            <el-alert type="error" :closable="false" show-icon :title="logsError" />
          </div>
        </template>
        <div v-else class="logs-hint">点击「展开日志」查看 stockdb 最近 150 行日志</div>
      </section>
    </template>
  </div>
</template>

<script setup>
// OpsSystem — 运维/系统页：健康卡 + 进程/磁盘/同步能力分块 + 容器日志 + 重启。
// 学习点：
// 1) Promise.all 并行拉 getHealth/getStatus，两个接口一个不挂等另一个，比串行快一倍；
// 2) 30s 轮询用 busy 标志互斥：上一次还没回来就不重复发请求，避免堆积；
// 3) 危险操作（重启）走 ElMessageBox.confirm，用户明确点确认才真正调接口。
import { ref, computed, onMounted, onUnmounted } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Refresh, RefreshRight } from '@element-plus/icons-vue'
import { getHealth, getStatus, getContainerLogs, restartContainer } from '../api/status.js'
import StatCard from '../components/StatCard.vue'
import EmptyState from '../components/EmptyState.vue'
import { fmtYMD } from '../utils/format.js'

// —— 数据状态 ——
const loading = ref(true) // 首次加载中（骨架屏）；之后轮询/手动刷新不再闪骨架
const error = ref('')     // 最近一次失败文案；为空表示最近一次成功
const health = ref(null)  // /api/health 全量载荷
const status = ref(null)  // /api/status 全量载荷

// —— 容器日志局部状态 ——
const containerLogsOpen = ref(false) // 展开/收起开关
const containerLog = ref('')         // 日志文本
const logsLoading = ref(false)       // 日志拉取中
const logsError = ref('')            // 日志读取失败文案

// busy 互斥：轮询是 30s 一次，但接口可能很慢（如健康度要扫库），
// 上次没回来时直接跳过本次，绝不让两个请求叠在一起。
let busy = false
let timer = null // setInterval 返回的 id，onUnmounted 里必须清掉，否则离开页面还偷偷轮询

// 主加载函数：首次进入与 30s 轮询共用。失败不抛错，只记录 error 文案，
// 页面靠 error 状态降级展示，而不是整个崩掉。
async function load() {
  if (busy) return
  busy = true
  try {
    // 并行拉两个接口：health 只读最新日期，status 带进程/磁盘/能力，互不依赖
    const [h, s] = await Promise.all([getHealth(), getStatus()])
    health.value = h || null
    status.value = s || null
    error.value = '' // 成功就把旧错误清掉
  } catch (e) {
    // ApiError.message 已是后端中文文案（如"接口不可用"），直接拿来展示
    error.value = e?.message || '系统状态接口未就绪'
  } finally {
    loading.value = false
    busy = false
  }
}

// —— 容器日志：展开时拉取，收起时不动数据（下次展开再刷新） ——
async function loadLogs() {
  logsLoading.value = true
  logsError.value = ''
  try {
    const r = await getContainerLogs(150)
    // 后端约定：{log: 文本, error?: 读取失败文案}；log 可能为空字符串
    containerLog.value = r?.log || ''
    logsError.value = r?.error || ''
  } catch (e) {
    logsError.value = e?.message || '容器日志读取失败'
  } finally {
    logsLoading.value = false
  }
}

function toggleLogs() {
  containerLogsOpen.value = !containerLogsOpen.value
  // 展开即拉取，但有个例外：手里已有日志内容就不重复拉（点「刷新」手动更新）。
  // 上次拉回来是空日志或读取失败都算"没有内容"→ 重新展开会再试一次
  if (containerLogsOpen.value && !containerLog.value && !logsLoading.value) {
    loadLogs()
  }
}

// —— 重启（危险操作）：二次确认后调 POST /api/container/restart ——
async function onRestart() {
  try {
    // confirm 返回 Promise：用户点「确定」才继续，点「取消」会 reject 被下面 catch 吞掉
    await ElMessageBox.confirm(
      '确定重启 stockdb 进程？重启期间行情服务会短暂中断。',
      '重启确认',
      { type: 'warning', confirmButtonText: '确认重启', cancelButtonText: '取消' }
    )
  } catch {
    return // 用户取消了，什么都不做
  }
  try {
    const r = await restartContainer()
    // 后端失败时 msg 形如"重启失败: ..."，200 也带失败文案 → 按内容区分提示等级
    if (r?.msg && r.msg.includes('失败')) ElMessage.error(r.msg)
    else ElMessage.success(r?.msg || '已发送重启，进程状态将自动刷新')
    load() // 重启后立刻刷新一次进程状态，别等 30s
  } catch (e) {
    ElMessage.error('重启失败：' + (e?.message || e))
  }
}

// —— 展示派生（纯函数，方便一眼看懂） ——
// 滞后着色与全局状态条同一套口径：≤1 天 ok、2 天 warn、>2 天 err；未知不给色
function lagTone(lag) {
  if (lag === null || lag === undefined) return ''
  if (lag <= 1) return 'ok'
  if (lag <= 2) return 'warn'
  return 'err'
}

function statusText(s) {
  return s === 'ok' ? '正常' : s === 'stale' ? '待更新' : '未知'
}
function statusTone(s) {
  return s === 'ok' ? 'ok' : s === 'stale' ? 'warn' : 'err'
}

// 磁盘使用率：防止除零；total 缺失时显示 0
const diskPct = computed(() => {
  const d = status.value?.disk
  if (!d || !d.total_gb) return 0
  return Math.min(100, Math.round((d.used_gb / d.total_gb) * 100))
})

// started 为 epoch 秒 → "x 天 x 小时 x 分钟"（与旧面板 fmtUptime 同款）
function fmtUptime(started) {
  if (!started) return '—'
  const sec = Math.max(0, Math.floor(Date.now() / 1000 - Number(started)))
  const d = Math.floor(sec / 86400)
  const h = Math.floor((sec % 86400) / 3600)
  const m = Math.floor((sec % 3600) / 60)
  return (d ? d + ' 天 ' : '') + (h ? h + ' 小时 ' : '') + m + ' 分钟'
}

// checks 的键是英文（updater/source/writable/retry_pending），转成中文小标签
function capName(key) {
  return { updater: '更新程序', source: '数据源', writable: '数据卷', retry_pending: '待重试' }[key] || key
}

// —— 生命周期：挂载拉一次 + 开 30s 轮询；卸载清定时器（防泄漏） ——
onMounted(() => {
  load()
  timer = setInterval(() => load(), 30000)
})
onUnmounted(() => {
  if (timer) clearInterval(timer)
  timer = null
})
</script>

<style scoped>
.ops-system {
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
  gap: 8px;
}
.block-alert {
  margin-bottom: 4px;
}
.panel {
  background: var(--panel);
  border: 1px solid var(--line);
  border-radius: 12px;
  padding: 16px;
}
.panel-title {
  margin: 0 0 12px;
  font-size: 14px;
  font-weight: 600;
  color: var(--text);
}
.panel-title-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
}
.stat-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
  gap: 12px;
}
.health-note {
  margin: 12px 0 0;
  font-size: 13px;
  color: var(--muted);
}
.block-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(240px, 1fr));
  gap: 16px;
}
.block {
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.block-line {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 13px;
}
.block-label {
  color: var(--text);
  font-weight: 600;
  white-space: nowrap;
}
.block-sub {
  color: var(--muted);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  flex-shrink: 0;
}
.cap-row {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 13px;
}
.cap-name {
  color: var(--text);
  white-space: nowrap;
}
.cap-detail {
  color: var(--muted);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.logs-hint {
  font-size: 13px;
  color: var(--muted);
}
.logs-loading {
  font-size: 13px;
  color: var(--muted);
  padding: 8px 0;
}
.log-pre {
  margin: 0;
  max-height: 420px;
  overflow: auto;
  padding: 12px;
  border-radius: 8px;
  background: var(--panel2);
  border: 1px solid var(--line);
  font: 12px/1.6 ui-monospace, 'SF Mono', Menlo, Consolas, monospace;
  color: var(--text);
  white-space: pre-wrap; /* 长行自动折行，避免横向拖滚动条 */
  word-break: break-all;
}
.error-state {
  padding: 8px 0;
}
.retry-btn {
  margin-left: 12px;
}
</style>
