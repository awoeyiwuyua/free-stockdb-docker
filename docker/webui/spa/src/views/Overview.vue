<template>
  <!-- ═══════════════ 总览页（Phase 5.1 LuCI 驾驶舱版）：5 指标卡 + 4 紧凑区块卡 ═══════════════
       学习点：
       1) 全局数据（健康/告警/模拟盘/MCP/版本）全部读 Pinia store —— App 层已做 30s 轮询，
          本页只为两个独立数据源自管轮询：情绪投递 getSignalStatus()、版本 getVersion()。
       2) 三态齐备：总览 null 未出错 → 骨架屏；出错 → ElAlert + 错误空态；有数据 → 正常渲染。
       3) 密度哲学：StatCard 网格 minmax(180px,1fr)、卡片内边距 12-14px、标题行紧凑。 -->
  <div class="overview-page">
    <!-- ── 页头：标题 + 最近刷新时间 + 手动刷新按钮（.page-head 紧凑风格） ── -->
    <header class="page-head">
      <div class="head-left">
        <h2 class="page-title">总览</h2>
        <span class="head-sub">
          最近刷新 {{ store.lastRefresh ? hhmmss(store.lastRefresh) : '等待首次刷新' }}
        </span>
      </div>
      <!-- 手动刷新：总览 store + 情绪信号 + 版本，三个数据源一起刷 -->
      <el-button type="primary" :icon="Refresh" :loading="refreshing" size="small" @click="onRefresh">
        刷新
      </el-button>
    </header>

    <!-- 刷新失败 → 顶部红色 ElAlert（不遮内容，只提示"数据可能过期"） -->
    <el-alert
      v-if="store.error"
      type="error"
      :closable="false"
      show-icon
      :title="store.error"
      class="top-alert"
    />

    <!-- ── 加载态：首次数据还没回来（overview 为 null 且无报错）→ 骨架屏 ── -->
    <template v-if="store.overview === null && !store.error">
      <div class="stat-grid">
        <div v-for="i in 5" :key="i" class="sk-card">
          <el-skeleton animated :rows="2" />
        </div>
      </div>
      <div class="cards-grid">
        <div v-for="i in 4" :key="'c' + i" class="sk-card">
          <el-skeleton animated :rows="5" />
        </div>
      </div>
    </template>

    <!-- 错误态：首次加载就失败 → 错误空态 + 重试按钮（页面不崩） -->
    <EmptyState
      v-else-if="store.overview === null"
      icon="CircleClose"
      title="总览数据加载失败"
      description="接口暂不可用，请检查后端服务后重试"
    >
      <el-button type="primary" @click="onRefresh">重试</el-button>
    </EmptyState>

    <!-- ── 正常内容（数据到位后渲染） ── -->
    <template v-else>
      <!-- ① 第一行：5 张指标卡（值行更紧凑，样式用 :deep 压缩 StatCard） -->
      <div class="stat-grid">
        <StatCard label="数据最新" :value="dataLatestValue" :sub="dataLatestSub" :tone="dataLatestTone" />
        <StatCard label="告警" :value="alertCountValue" :sub="alertCountSub" :tone="alertCountTone" />
        <StatCard label="模拟盘" :value="paperStateText" :sub="paperStateSub" :tone="paperStateTone" />
        <StatCard label="MCP 成功率" :value="mcpRateValue" :sub="mcpRateSub" tone="ok" />
        <StatCard label="面板版本" :value="versionValue" :sub="versionSub" :tone="versionTone" />
      </div>

      <!-- ② 区块卡：4 张紧凑卡（模拟盘进度 / 告警 / 情绪投递 / 版本） -->
      <div class="cards-grid">
        <!-- 模拟盘今日进度摘要：下一触发 + 完成计数 + 最后状态（完整时间轴已移至 /paper） -->
        <section class="card">
          <div class="card-head">
            <h3 class="card-title">模拟盘今日进度</h3>
          </div>

          <!-- 总览接口未返回模拟盘块 → 降级空态 -->
          <EmptyState
            v-if="store.paper === null"
            icon="Warning"
            title="模拟盘数据不可用"
            description="总览接口未返回模拟盘状态"
          />
          <template v-else-if="timeline.length">
            <!-- 下一触发：next_runs[0]，形如 '20260815 09:27'，日期套 fmtYMD -->
            <div class="kv-row">
              <span class="kv-label">下一触发</span>
              <el-tag size="small" type="info" effect="plain">{{ nextRunText }}</el-tag>
            </div>

            <!-- 完成/未完成计数：细进度条 + "已完成 x/y" 文案 -->
            <div class="progress-row">
              <el-progress
                :percentage="progressPct"
                :stroke-width="6"
                :show-text="false"
                class="progress-bar"
              />
              <span class="progress-text">已完成 {{ firedCount }}/{{ totalCount }}</span>
            </div>

            <!-- 最后状态：最后一个已触发时点的 label + 状态文案 -->
            <p class="muted last-fired">{{ lastFiredText }}</p>
          </template>
          <!-- 时间轴为空（今日还没触发过）→ 空态 -->
          <EmptyState
            v-else
            icon="Clock"
            title="今日暂无进度"
            description="调度尚未触发或模拟盘数据未生成"
          />

          <div class="card-foot">
            <RouterLink to="/paper" class="foot-link">查看模拟盘 →</RouterLink>
          </div>
        </section>

        <!-- 告警摘要：只保留 count + 最近 3 条极简（完整列表移至 /ops/alerts） -->
        <section class="card">
          <div class="card-head">
            <h3 class="card-title">告警</h3>
            <el-badge :value="store.alertCount" :hidden="store.alertCount === 0" type="danger">
              <el-tag size="small" type="info">共 {{ store.alertCount }} 条</el-tag>
            </el-badge>
          </div>

          <div v-if="recent3.length" class="alert-list">
            <div v-for="a in recent3" :key="a.ts" class="alert-row">
              <!-- 级别色点：error 红 / warning 黄 / info 品牌蓝 -->
              <span class="dot" :style="{ background: alertColor(a.level) }" />
              <span class="alert-time" :title="a.ts">{{ fmtHm(a.ts) }}</span>
              <span class="alert-src">{{ a.source }}</span>
              <span class="alert-msg" :title="a.message">{{ a.message }}</span>
            </div>
          </div>
          <EmptyState v-else icon="Bell" title="暂无告警" description="当前没有待处理的告警" />

          <div class="card-foot">
            <RouterLink to="/ops/alerts" class="foot-link">查看全部告警 →</RouterLink>
          </div>
        </section>

        <!-- 情绪投递：只保留徽标（已投递/缺失/不合格 三元判断）+ 详情链接 -->
        <section class="card">
          <div class="card-head">
            <h3 class="card-title">情绪投递</h3>
            <!-- 三元判断：exists/parsed/all_ok → 已投递(绿) / 缺失(黄) / 不合格(红) / 接口异常(红) -->
            <el-tag :type="signalOverall.tag" size="small" effect="dark">
              {{ signalOverall.text }}
            </el-tag>
          </div>

          <!-- 加载态：骨架 -->
          <el-skeleton v-if="signalLoading && !signal" animated :rows="2" />
          <!-- 接口异常：错误文案（不崩页面） -->
          <p v-else-if="signalError && !signal" class="muted">信号体检接口不可用</p>
          <!-- 正常/缺失/不合格：一行弱化说明（错误原因或交易日） -->
          <p v-else class="muted signal-note">{{ signalNote }}</p>

          <div class="card-foot">
            <RouterLink to="/paper/signal" class="foot-link">信号体检详情 →</RouterLink>
          </div>
        </section>

        <!-- 版本卡（并入原 OpsVersion 页）：独立 getVersion() 30s 轮询 -->
        <section class="card">
          <div class="card-head">
            <h3 class="card-title">版本</h3>
            <div class="badge-row">
              <!-- stale 高亮：有新版本 → 警告色；上游可达且最新 → 绿色（纯数据驱动，
                   加载中/接口异常时 ver 为 null，三个徽标自然都不渲染） -->
              <el-tag v-if="ver?.stale" size="small" type="warning" effect="dark">有新版本</el-tag>
              <el-tag v-else-if="ver?.upstream?.tag_name" size="small" type="success" effect="plain">已是最新</el-tag>
              <el-tag v-else-if="ver" size="small" type="info" effect="plain">上游不可达</el-tag>
              <!-- ui_mode 徽标：新版 SPA 壳 / 旧版 legacy 壳 -->
              <el-tag
                v-if="ver"
                size="small"
                :type="ver.ui_mode === 'legacy' ? 'warning' : 'success'"
                effect="plain"
              >{{ ver.ui_mode === 'legacy' ? '旧版 legacy' : '新版 SPA' }}</el-tag>
            </div>
          </div>

          <!-- 加载态：骨架 -->
          <el-skeleton v-if="verLoading && !ver" animated :rows="3" />
          <!-- 接口异常：一行弱化文案 -->
          <p v-else-if="verError && !ver" class="muted">版本信息不可用</p>
          <template v-else-if="ver">
            <div class="kv-row">
              <span class="kv-label">webui 版本</span>
              <span class="kv-value">v{{ ver.webui?.version ?? '—' }}</span>
            </div>
            <div class="kv-row">
              <span class="kv-label">镜像 tag</span>
              <span class="kv-value">{{ ver.image?.tag || '—' }}</span>
            </div>
            <div class="kv-row">
              <span class="kv-label">上游 release</span>
              <!-- upstream.tag_name：可点击跳 GitHub release（target=_blank） -->
              <template v-if="ver.upstream?.tag_name">
                <a
                  v-if="ver.upstream.html_url"
                  :href="ver.upstream.html_url"
                  target="_blank"
                  rel="noopener"
                  class="foot-link"
                >v{{ ver.upstream.tag_name }} ↗</a>
                <span v-else class="kv-value">v{{ ver.upstream.tag_name }}</span>
              </template>
              <span v-else class="kv-value muted">暂不可用</span>
            </div>
            <!-- stale 时的升级提示文案（后端 version_payload 的 msg） -->
            <div v-if="ver.msg" class="kv-row">
              <span class="kv-label">提示</span>
              <span class="kv-value warn-text">{{ ver.msg }}</span>
            </div>
          </template>

          <!-- 旧面板逃生通道：/legacy 直达旧页面 -->
          <div class="card-foot">
            <a href="/legacy" target="_blank" rel="noopener" class="foot-link">旧面板 /legacy ↗</a>
          </div>
        </section>
      </div>
    </template>
  </div>
</template>

<script setup>
// ============================================================
// Overview.vue — 总览驾驶舱（Phase 5.1 瘦身版）。
// 学习点：
// 1) 页面的"总览数据"全部读全局 store（App 层已做 30s 轮询），页面自身不为它重复轮询；
//    只有"情绪投递"与"版本"是两个独立数据源（/api/paper/signal-status、/api/version），
//    由本页用一个 30s 定时器共同轮询，onUnmounted 必须清理。
// 2) 所有展示字段都做防御（?. 与 || 兜底），后端某块降级为 null 时页面不崩、显示 '—'。
// ============================================================
import { ref, computed, onMounted, onUnmounted } from 'vue'
import { ElMessage } from 'element-plus'
// 图标显式 import（el-button :icon 需要组件对象；模板里 <el-icon> 才走全局注册）
import { Refresh } from '@element-plus/icons-vue'
import StatCard from '../components/StatCard.vue'
import EmptyState from '../components/EmptyState.vue'
import { fmtYMD } from '../utils/format.js'
import { getSignalStatus } from '../api/paper.js'
import { getVersion } from '../api/ops.js'
import { useGlobalStore } from '../stores/global.js'

const store = useGlobalStore()

/* ═══════════════ 手动刷新（总览 + 信号 + 版本一起刷） ═══════════════ */
const refreshing = ref(false)
const onRefresh = async () => {
  refreshing.value = true
  // store.refresh() 内部已把异常写进 store.error，不会 throw；Promise.allSettled 保证
  // 信号/版本刷新失败也不影响总览刷新。最后用 ElMessage 给"点按钮没反应"一个明确反馈。
  await Promise.allSettled([store.refresh(), loadSignal(), loadVersion()])
  refreshing.value = false
  if (store.error) ElMessage.warning(store.error)
}

// 把 Date 格式化成 HH:MM:SS（最近刷新时间展示用）
const hhmmss = (d) => {
  const p = (n) => String(n).padStart(2, '0')
  return `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`
}

/* ═══════════════ ① 第一行 5 张指标卡（数据均来自 store getters） ═══════════════ */
// 数据最新：值 = 最新数据日期；tone 按滞后天数：≤1 ok / =2 warn / >2 err / 未知默认
const dataLatestValue = computed(() => (store.health?.latest ? fmtYMD(store.health.latest) : '—'))
const dataLatestTone = computed(() => {
  const lag = store.lagDays
  if (lag === null) return '' // 未知：不给语义色，保持默认
  if (lag <= 1) return 'ok'
  if (lag === 2) return 'warn'
  return 'err'
})
const dataLatestSub = computed(() => {
  const lag = store.lagDays
  if (lag === null) return store.health?.status === 'unknown' ? '数据日期未知' : '状态未知'
  return lag === 0 ? '已是最新' : `滞后 ${lag} 天`
})

// 告警：数量 >0 用红色强调
const alertCountValue = computed(() => String(store.alertCount))
const alertCountTone = computed(() => (store.alertCount > 0 ? 'err' : 'ok'))
const alertCountSub = computed(() => (store.alertCount > 0 ? '有待处理告警' : '一切正常'))

// 模拟盘：状态文案与顶栏同口径（交易开启=ok / 观察期=brand / 暂停=warn / 引擎缺失=err）
const paperStateText = computed(() => {
  const p = store.paper
  if (!p || !p.modules_ok) return '不可用'
  if (p.paused) return '已暂停'
  if (p.trading_enabled) return '交易开启'
  if (p.engine_available) return '观察期'
  return '引擎缺失'
})
const paperStateTone = computed(() => {
  const p = store.paper
  if (!p || !p.modules_ok) return 'err'
  if (p.paused) return 'warn'
  if (p.trading_enabled) return 'ok'
  if (p.engine_available) return 'brand'
  return 'err'
})
const paperStateSub = computed(() => {
  const p = store.paper
  if (!p) return '状态未知'
  if (!p.modules_ok) return p.reason || '模拟盘模块缺失'
  return p.paused ? '暂停中，调度不会触发' : p.trading_enabled ? '调度已开启' : '观察期：只决策不下单'
})

// MCP 成功率：ok_rate 是 0~1 小数（如 0.9）→ ×100 转百分比；空窗口为 null → '—'
const mcpRateValue = computed(() => {
  const ok = store.mcp?.ok_rate
  if (store.mcp?.total === undefined || ok === null || ok === undefined) return '—'
  return `${(ok * 100).toFixed(1)}%`
})
const mcpRateSub = computed(() =>
  store.mcp?.total ? `最近 ${store.mcp.total} 次调用` : '暂无调用记录')

// 面板版本：stale（上游有新版本）时警告色
const versionValue = computed(() => `v${store.version?.webui?.version ?? '—'}`)
const versionTone = computed(() => (store.version?.stale ? 'warn' : ''))
const versionSub = computed(() => {
  const v = store.version
  if (!v) return '版本信息未知'
  if (v.stale) return v.upstream?.tag_name ? `上游已有 v${v.upstream.tag_name}` : '检测到新版本'
  return v.upstream?.tag_name ? '已是最新' : '上游暂不可用'
})

/* ═══════════════ ② 模拟盘今日进度摘要 ═══════════════ */
// timeline：store.paper.timeline（8 个时点：09:25 信号发布 + 7 个触发时点）
const timeline = computed(() => store.paper?.timeline ?? [])
// 完成计数：fired=true 的时点数 / 总数
const firedCount = computed(() => timeline.value.filter((x) => x.fired).length)
const totalCount = computed(() => timeline.value.length)
const progressPct = computed(() =>
  totalCount.value ? Math.round((firedCount.value / totalCount.value) * 100) : 0)

// 下一触发：next_runs[0] 形如 '20260815 09:27'，日期部分套 fmtYMD 展示
const nextRunText = computed(() => {
  const r = store.paper?.next_runs?.[0]
  if (!r) return '今日无未来时点'
  const [d, hm] = String(r).split(' ')
  return hm ? `${fmtYMD(d)} ${hm}` : fmtYMD(String(r))
})

// 最后状态：取最后一个已触发的时点（label + 状态文案）
const TIMELINE_TEXTS = { ok: '正常', warn: '告警', err: '异常', run: '已触发', wait: '待触发' }
const lastFired = computed(() => [...timeline.value].reverse().find((x) => x.fired))
const lastFiredText = computed(() => {
  const x = lastFired.value
  if (!x) return '今日尚未触发'
  return `最后 ${x.label} · ${TIMELINE_TEXTS[x.state] || '已触发'}`
})

/* ═══════════════ ③ 告警摘要（count + 最近 3 条极简） ═══════════════ */
// 后端告警字段是 {ts, level, source, message}；overview.alerts.recent 最多 8 条，这里只取 3
const recent3 = computed(() => (store.overview?.alerts?.recent ?? []).slice(0, 3))
const alertColor = (level) => {
  const l = String(level || '').toLowerCase()
  if (l === 'error' || l === 'critical') return 'var(--err)'
  if (l === 'warning') return 'var(--warn)'
  if (l === 'info') return 'var(--brand)'
  return 'var(--muted)'
}
// ts 是 ISO 本地时间（如 2026-08-15T09:30:00），截取 HH:MM，完整值放 title 悬浮
const fmtHm = (ts) => String(ts || '').slice(11, 16) || '--:--'

/* ═══════════════ ④ 情绪投递（只留徽标 + 一行说明，独立 30s 轮询） ═══════════════ */
const signal = ref(null) // 最近一次成功的体检载荷
const signalLoading = ref(false)
const signalError = ref('')

// 拉取一次信号体检；失败只写文案，不抛（轮询场景下异常不能让页面崩）
const loadSignal = async () => {
  signalLoading.value = true
  try {
    signal.value = await getSignalStatus()
    signalError.value = ''
  } catch (e) {
    signalError.value = e?.message || '信号体检接口不可用'
  } finally {
    signalLoading.value = false
  }
}

// 徽标三元判断（任务约定）：已投递 / 缺失 / 不合格（exists/parsed/all_ok）
const signalAllOk = computed(() => {
  const checks = Object.values(signal.value?.checks ?? {}).filter(Boolean)
  return checks.length > 0 && checks.every((c) => c.ok === true)
})
const signalOverall = computed(() => {
  if (signalError.value) return { tag: 'danger', text: '接口异常' }
  const s = signal.value
  if (!s) return { tag: 'info', text: '加载中' }
  if (!s.exists) return { tag: 'warning', text: '缺失' }
  if (!s.parsed || !signalAllOk.value) return { tag: 'danger', text: '不合格' }
  return { tag: 'success', text: '已投递' }
})

// 徽标下方的一行弱化说明：失败时给原因，成功时给最近交易日
const signalNote = computed(() => {
  const s = signal.value
  if (!s) return '等待信号体检数据…'
  if (!s.exists) return s.error || '信号文件尚未生成'
  if (!s.parsed) return s.error || '信号文件解析失败'
  if (!signalAllOk.value) {
    const fail = Object.values(s.checks || {}).find((c) => c && c.ok === false)
    return fail?.reason || '部分校验未通过'
  }
  // 从 path（…/emotion/20260804.json）提取交易日日期
  const m = (s.path || '').match(/(\d{8})\.json$/)
  return `最近交易日 ${m ? fmtYMD(m[1]) : '—'} 已就绪`
})

/* ═══════════════ ⑤ 版本卡（并入原 OpsVersion 页，独立 getVersion() 30s 轮询） ═══════════════ */
const ver = ref(null)
const verLoading = ref(false)
const verError = ref('')

const loadVersion = async () => {
  verLoading.value = true
  try {
    ver.value = await getVersion()
    verError.value = ''
  } catch (e) {
    verError.value = e?.message || '版本接口不可用'
  } finally {
    verLoading.value = false
  }
}

/* ═══════════════ 轮询：信号 + 版本共用一个 30s 定时器 ═══════════════ */
let timer = null
onMounted(() => {
  loadSignal() // 进页面先各拉一次
  loadVersion()
  timer = setInterval(() => {
    loadSignal()
    loadVersion()
  }, 30_000) // 之后每 30s 一起刷新
})
onUnmounted(() => {
  if (timer) clearInterval(timer) // 离开页面清定时器，防泄漏
  timer = null
})
</script>

<style scoped>
/* 页面骨架：纵向卡片流，间距统一 12px（比旧版 16px 更紧凑） */
.overview-page {
  display: flex;
  flex-direction: column;
  gap: 12px;
}

/* —— 页头（.page-head 紧凑风格：小标题 + 右侧操作） —— */
.page-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  flex-wrap: wrap; /* 窄屏时按钮自动换行，不被挤扁 */
}
.head-left {
  display: flex;
  align-items: baseline;
  gap: 12px;
}
.page-title {
  margin: 0;
  font-size: 18px;
  font-weight: 700;
  color: var(--text);
}
.head-sub {
  font-size: 12px;
  color: var(--muted);
}
.top-alert {
  width: 100%;
}

/* —— 通用卡片：var(--panel) 底 + 12px 圆角 + var(--line) 边框；内边距 14px（密度约定 12-14px） —— */
.card {
  background: var(--panel);
  border: 1px solid var(--line);
  border-radius: 12px;
  padding: 14px;
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.card-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  flex-wrap: wrap;
}
.card-title {
  margin: 0;
  font-size: 14px;
  font-weight: 600;
  color: var(--text);
}
.badge-row {
  display: flex;
  gap: 6px;
  flex-wrap: wrap;
}
.muted {
  color: var(--muted);
}

/* —— 指标卡 / 骨架卡栅格：minmax(180px,1fr) 自适应换行（密度约定） —— */
.stat-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
  gap: 10px;
}
/* 压缩 StatCard：值行更紧凑（:deep 穿透进子组件作用域改内部样式） */
.stat-grid :deep(.stat-card) {
  padding: 12px;
  gap: 3px;
}
.stat-grid :deep(.stat-value) {
  font-size: 22px; /* 旧版 28px → 22px，驾驶舱更紧凑 */
}
.stat-grid :deep(.stat-sub) {
  font-size: 11px;
}
.sk-card {
  background: var(--panel);
  border: 1px solid var(--line);
  border-radius: 12px;
  padding: 14px;
}
.cards-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
  gap: 12px;
  align-items: start; /* 卡片高度各自内容自适应，不强制拉伸 */
}

/* —— 模拟盘进度卡 —— */
.kv-row {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 13px;
  flex-wrap: wrap;
}
.kv-label {
  color: var(--muted);
  font-size: 12px;
  flex-shrink: 0;
}
.kv-value {
  color: var(--text);
}
.warn-text {
  color: var(--warn);
}
.progress-row {
  display: flex;
  align-items: center;
  gap: 10px;
}
.progress-bar {
  flex: 1;
  min-width: 60px;
}
.progress-text {
  font-size: 12px;
  color: var(--text);
  font-variant-numeric: tabular-nums; /* 等宽数字：计数变化宽度不抖 */
  flex-shrink: 0;
}
.last-fired {
  margin: 0;
  font-size: 12px;
}

/* —— 告警摘要卡 —— */
.alert-list {
  display: flex;
  flex-direction: column;
}
.alert-row {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 6px 0;
  border-bottom: 1px solid var(--line);
  font-size: 13px;
}
.alert-row:last-child {
  border-bottom: none; /* 最后一行去掉分隔线，视觉不突兀 */
}
.dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  flex-shrink: 0;
}
.alert-time {
  color: var(--muted);
  font-variant-numeric: tabular-nums;
  font-size: 12px;
}
.alert-src {
  color: var(--brand);
  font-size: 12px;
  flex-shrink: 0;
}
.alert-msg {
  color: var(--text);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap; /* 超长文案截断，完整内容悬浮显示 */
}

/* —— 情绪投递卡 —— */
.signal-note {
  margin: 0;
  font-size: 12px;
  line-height: 1.5;
  word-break: break-all;
}

/* —— 卡片底部链接（顶到卡片底部，卡片高度不一也整齐） —— */
.card-foot {
  margin-top: auto;
  padding-top: 8px;
}
.foot-link {
  color: var(--brand);
  font-size: 12px;
  text-decoration: none;
}
.foot-link:hover {
  text-decoration: underline;
}
</style>
