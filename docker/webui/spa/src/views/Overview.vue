<template>
  <!-- ═══════════════ 总览页：5 张指标卡 + 5 张信息卡，全部读全局 store（App 层 30s 轮询） ═══════════════ -->
  <div class="overview-page">
    <!-- ── 页头：标题 + 最近刷新时间 + 手动刷新按钮 ── -->
    <header class="page-head">
      <div class="head-left">
        <h2 class="page-title">总览</h2>
        <!-- lastRefresh 是 store 里"最近一次成功刷新"的 Date，展示成 HH:MM:SS -->
        <span class="head-sub">
          最近刷新 {{ store.lastRefresh ? hhmmss(store.lastRefresh) : '等待首次刷新' }}
        </span>
      </div>
      <!-- 手动刷新：同时刷新全局 store 与情绪信号卡（两个数据源） -->
      <el-button type="primary" :icon="Refresh" :loading="refreshing" @click="onRefresh">
        刷新
      </el-button>
    </header>

    <!-- 刷新失败 → 顶部红色 ElAlert（不遮内容，只提示"数据可能过期"） -->
    <el-alert
      v-if="store.error"
      class="top-alert"
      type="error"
      :closable="false"
      show-icon
      :title="store.error"
    />

    <!-- ── 加载态：首次数据还没回来（overview 为 null 且无报错）→ 骨架屏 ── -->
    <template v-if="store.overview === null && !store.error">
      <div class="stat-grid">
        <div v-for="i in 5" :key="i" class="sk-card">
          <el-skeleton animated :rows="2" />
        </div>
      </div>
      <div class="cards-grid">
        <div v-for="i in 3" :key="'c' + i" class="sk-card">
          <el-skeleton animated :rows="6" />
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
      <!-- ① 第一行：5 张指标卡 -->
      <div class="stat-grid">
        <StatCard
          label="数据最新"
          :value="dataLatestValue"
          :sub="dataLatestSub"
          :tone="dataLatestTone"
        />
        <StatCard
          label="告警"
          :value="alertCountValue"
          :sub="alertCountSub"
          :tone="alertCountTone"
        />
        <StatCard
          label="模拟盘"
          :value="paperStateText"
          :sub="paperStateSub"
          :tone="paperStateTone"
        />
        <!-- MCP 成功率：ok_rate 是 0~1 小数（如 0.9），乘 100 转百分比展示 -->
        <StatCard
          label="MCP 成功率"
          :value="mcpRateValue"
          :sub="mcpRateSub"
          tone="ok"
        />
        <StatCard
          label="面板版本"
          :value="versionValue"
          :sub="versionSub"
          :tone="versionTone"
        />
      </div>

      <!-- ② 卡片区：自适应栅格，宽屏并排、窄屏自动换行 -->
      <div class="cards-grid">
        <!-- 模拟盘今日时间轴（paper.timeline：09:25 信号发布 + 7 个触发时点） -->
        <section class="card">
          <div class="card-head">
            <h3 class="card-title">模拟盘今日时间轴</h3>
            <!-- 引擎 / 交易开关 / 暂停 三枚状态徽标 -->
            <div class="badge-row">
              <el-tag
                v-for="b in paperBadges"
                :key="b.text"
                :type="b.type"
                size="small"
                effect="dark"
              >{{ b.text }}</el-tag>
            </div>
          </div>

          <!-- 下次触发时间点：小标签形式（next_runs 形如 '20260815 09:27'） -->
          <div class="next-runs">
            <span class="kv-label">下次触发</span>
            <template v-if="nextRuns.length">
              <el-tag v-for="r in nextRuns" :key="r" size="small" type="info">{{ r }}</el-tag>
            </template>
            <span v-else class="muted">今日无未来时点</span>
          </div>

          <!-- 时间轴：用 el-timeline，节点颜色跟随状态（ok/warn/err/run/wait） -->
          <el-timeline v-if="timeline.length" class="ov-timeline">
            <el-timeline-item
              v-for="x in timeline"
              :key="x.tp"
              :timestamp="x.tp"
              :color="timelineColor(x.state)"
              placement="top"
            >
              <div class="tl-body">
                <span class="tl-label">{{ x.label }}</span>
                <el-tag :type="timelineTag(x.state)" size="small" effect="plain">
                  {{ timelineText(x.state) }}
                </el-tag>
                <!-- detail 为空时按 fired 给兜底文案（与旧面板口径一致） -->
                <div class="muted tl-detail">{{ x.detail || (x.fired ? '已触发' : '待触发') }}</div>
              </div>
            </el-timeline-item>
          </el-timeline>
          <EmptyState
            v-else
            icon="Clock"
            title="今日时间轴为空"
            description="调度尚未触发或模拟盘数据未生成"
          />
        </section>

        <!-- 告警摘要（overview.alerts.recent，字段以 app.py 实际为准：ts/level/source/message） -->
        <section class="card">
          <div class="card-head">
            <h3 class="card-title">告警摘要</h3>
            <!-- 徽标红点：有告警时显示数量 -->
            <el-badge :value="store.alertCount" :hidden="store.alertCount === 0" type="danger">
              <el-tag size="small" type="info">共 {{ store.alertCount }} 条</el-tag>
            </el-badge>
          </div>

          <div v-if="recentAlerts.length" class="alert-list">
            <div v-for="a in recentAlerts" :key="a.ts" class="alert-row">
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

        <!-- 数据与 MCP：health.note 文案 + mcp 三项小字 -->
        <section class="card">
          <h3 class="card-title">数据与 MCP</h3>
          <div class="kv-row">
            <span class="kv-label">数据健康</span>
            <el-tag :type="healthStatusTag.type" size="small" effect="plain">
              {{ healthStatusTag.text }}
            </el-tag>
          </div>
          <!-- health.note：后端计算好的中文健康说明（如"数据最新 …"） -->
          <p class="health-note">{{ healthNote || '暂无健康说明' }}</p>
          <!-- MCP 统计小字：调用次数 / 平均耗时 / P95 耗时 -->
          <div class="mcp-grid">
            <div v-for="m in mcpLines" :key="m.label" class="mcp-cell">
              <div class="muted mcp-label">{{ m.label }}</div>
              <div class="mcp-value">{{ m.value }}</div>
            </div>
          </div>
        </section>

        <!-- 情绪投递状态（补位卡）：独立 30s 轮询 getSignalStatus() -->
        <section class="card">
          <div class="card-head">
            <h3 class="card-title">情绪投递状态</h3>
            <!-- 总览徽标：可投递 / N 项未通过 / 文件缺失 / 接口异常 -->
            <el-tag :type="signalOverall.tag" size="small" effect="dark">
              {{ signalOverall.text }}
            </el-tag>
          </div>

          <div class="signal-body">
            <!-- 首次加载：骨架 -->
            <el-skeleton v-if="signalLoading && !signal" animated :rows="4" />
            <!-- 接口异常：错误态（不崩页面） -->
            <el-alert
              v-else-if="signalError"
              type="error"
              :closable="false"
              show-icon
              :title="signalError"
            />
            <!-- 信号文件缺失：降级提示（exists=false + 后端 error 文案） -->
            <EmptyState
              v-else-if="signal && !signal.exists"
              icon="Warning"
              title="信号文件缺失"
              :description="signal.error || '最近交易日情绪文件尚未生成'"
            />
            <template v-else-if="signal">
              <!-- 摘要行：文件存在 / 解析成功 两枚状态徽标 -->
              <div class="signal-summary">
                <el-tag :type="signal.exists ? 'success' : 'danger'" size="small" effect="plain">
                  文件{{ signal.exists ? '存在' : '缺失' }}
                </el-tag>
                <el-tag :type="signal.parsed ? 'success' : 'danger'" size="small" effect="plain">
                  解析{{ signal.parsed ? '成功' : '失败' }}
                </el-tag>
                <span v-if="signal.error" class="muted">{{ signal.error }}</span>
              </div>

              <!-- 关键字段摘要（fields 里挑最有用的几项展示） -->
              <div v-if="signalFields.length" class="signal-fields">
                <span v-for="f in signalFields" :key="f.key" class="field-pill">
                  {{ f.label }}：<b>{{ f.value }}</b>
                </span>
              </div>

              <!-- 7 项检查：小圆点 + 名称 + 原因；未通过项整行红色 -->
              <div class="check-list">
                <div
                  v-for="c in signalChecks"
                  :key="c.key"
                  class="check-row"
                  :class="{ fail: c.ok === false }"
                >
                  <span
                    class="check-dot"
                    :class="{ ok: c.ok === true, fail: c.ok === false }"
                  />
                  <span class="check-name">{{ c.name }}</span>
                  <span class="check-reason" :title="c.reason">{{ c.reason }}</span>
                </div>
              </div>
            </template>
          </div>
        </section>

        <!-- 版本卡：stale 高亮 + 上游 release 链接 + ui_mode 前端壳 -->
        <section class="card">
          <div class="card-head">
            <h3 class="card-title">版本</h3>
            <!-- 有新版本（stale）→ 警告色高亮；否则若上游可达 → 绿色"已是最新" -->
            <el-tag v-if="store.version?.stale" type="warning" size="small" effect="dark">
              有新版本
            </el-tag>
            <el-tag v-else-if="store.version?.upstream" type="success" size="small" effect="plain">
              已是最新
            </el-tag>
          </div>

          <div class="ver-big" :class="{ stale: store.version?.stale }">
            {{ versionValue }}
          </div>

          <div class="kv-row">
            <span class="kv-label">镜像 tag</span>
            <span class="kv-value">{{ store.version?.image?.tag || '—' }}</span>
          </div>
          <div class="kv-row">
            <span class="kv-label">前端壳</span>
            <span class="kv-value">{{ uiModeText }}</span>
            <!-- SPA 模式下提供旧面板逃生通道（/legacy 直达旧页面） -->
            <a
              v-if="store.version?.ui_mode !== 'legacy'"
              class="foot-link"
              href="/legacy"
              target="_blank"
              rel="noopener"
            >旧面板 /legacy</a>
          </div>
          <div v-if="store.version?.msg" class="kv-row">
            <span class="kv-label">提示</span>
            <span class="kv-value warn-text">{{ store.version.msg }}</span>
          </div>
          <div class="kv-row">
            <span class="kv-label">上游 release</span>
            <template v-if="store.version?.upstream?.tag_name">
              <a
                v-if="store.version.upstream.html_url"
                :href="store.version.upstream.html_url"
                target="_blank"
                rel="noopener"
                class="foot-link"
              >v{{ store.version.upstream.tag_name }} ↗</a>
              <span v-else class="kv-value">v{{ store.version.upstream.tag_name }}</span>
            </template>
            <span v-else class="kv-value muted">暂不可用</span>
          </div>
        </section>
      </div>
    </template>
  </div>
</template>

<script setup>
// 学习点：
// 1) 本页"总览数据"全部读全局 store（App 层已做 30s 轮询），页面自身不为它重复轮询，
//    只有"情绪投递状态"卡是独立数据源（/api/paper/signal-status），由本页自管 30s 轮询。
// 2) 三态齐备：overview 为 null 未出错 → 骨架屏；出错 → ElAlert + 错误空态；有数据 → 正常渲染。
// 3) 所有展示字段都做防御（?. 与 || 兜底），后端某块降级为 null 时页面不崩、显示 '—'。
import { ref, computed, onMounted, onUnmounted } from 'vue'
import { ElMessage } from 'element-plus'
// 图标与其他页面一样显式 import（el-button :icon 需要组件对象，全局注册只保证模板标签可用）
import { Refresh } from '@element-plus/icons-vue'
import StatCard from '../components/StatCard.vue'
import EmptyState from '../components/EmptyState.vue'
import { fmtYMD, fmtElapsed } from '../utils/format.js'
import { getSignalStatus } from '../api/paper.js'
import { useGlobalStore } from '../stores/global.js'

const store = useGlobalStore()

/* ═══════════════ 手动刷新（总览 + 信号卡一起刷） ═══════════════ */
const refreshing = ref(false)
const onRefresh = async () => {
  refreshing.value = true
  // store.refresh() 内部已把异常写进 store.error，不会 throw；Promise.allSettled 保证
  // 信号卡刷新失败也不影响总览刷新。最后用 ElMessage 给"点按钮没反应"一个明确反馈。
  await Promise.allSettled([store.refresh(), loadSignal()])
  refreshing.value = false
  if (store.error) ElMessage.warning(store.error)
}

// 把 Date 格式化成 HH:MM:SS（最近刷新时间展示用）
const hhmmss = (d) => {
  const p = (n) => String(n).padStart(2, '0')
  return `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`
}

/* ═══════════════ ① 第一行 5 张指标卡 ═══════════════ */
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

// MCP 成功率：ok_rate 是 0~1 小数 → ×100 显示百分比；空窗口为 null → '—'
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

/* ═══════════════ ② 模拟盘今日时间轴 ═══════════════ */
const timeline = computed(() => store.paper?.timeline ?? [])
// next_runs 形如 '20260815 09:27'：把日期部分格式化成 2026-08-15，时间原样
const nextRuns = computed(() =>
  (store.paper?.next_runs ?? []).map((r) => {
    const [d, hm] = String(r).split(' ')
    return hm ? `${fmtYMD(d)} ${hm}` : String(r)
  })
)

// 引擎 / 交易开关 / 暂停 徽标（与顶栏 paperTag 语义一致）
const paperBadges = computed(() => {
  const p = store.paper
  if (!p) return [{ text: '状态未知', type: 'info' }]
  if (!p.modules_ok) return [{ text: '模块不可用', type: 'danger' }]
  const badges = [{
    text: p.engine_available ? '引擎可用' : '引擎缺失',
    type: p.engine_available ? 'success' : 'danger',
  }]
  if (p.paused) badges.push({ text: '已暂停', type: 'warning' })
  else if (p.trading_enabled) badges.push({ text: '交易开启', type: 'success' })
  else badges.push({ text: '观察期', type: 'primary' })
  return badges
})

// 时间轴节点配色：ok 绿 / warn 黄 / err 红 / run（已触发）品牌蓝 / wait（待触发）灰
const TIMELINE_COLORS = {
  ok: 'var(--ok)', warn: 'var(--warn)', err: 'var(--err)',
  run: 'var(--brand)', wait: 'var(--muted)',
}
const TIMELINE_TAGS = { ok: 'success', warn: 'warning', err: 'danger', run: 'primary', wait: 'info' }
const TIMELINE_TEXTS = { ok: '正常', warn: '告警', err: '异常', run: '已触发', wait: '待触发' }
const timelineColor = (s) => TIMELINE_COLORS[s] || 'var(--muted)'
const timelineTag = (s) => TIMELINE_TAGS[s] || 'info'
const timelineText = (s) => TIMELINE_TEXTS[s] || s || '未知'

/* ═══════════════ ③ 告警摘要 ═══════════════ */
// 后端告警字段是 {ts, level, source, message}（以 app.py Alerts 为准，非 level/time/text）
const recentAlerts = computed(() => store.overview?.alerts?.recent ?? [])
const alertColor = (level) => {
  const l = String(level || '').toLowerCase()
  if (l === 'error' || l === 'critical') return 'var(--err)'
  if (l === 'warning') return 'var(--warn)'
  if (l === 'info') return 'var(--brand)'
  return 'var(--muted)'
}
// ts 是 ISO 本地时间（如 2026-08-15T09:30:00），截取 HH:MM，完整值放 title 悬浮
const fmtHm = (ts) => String(ts || '').slice(11, 16) || '--:--'

/* ═══════════════ ④ 数据与 MCP ═══════════════ */
const healthNote = computed(() => store.health?.note || '')
const healthStatusTag = computed(() => {
  const st = store.health?.status
  if (st === 'ok') return { type: 'success', text: '正常' }
  if (st === 'stale') return { type: 'warning', text: '滞后' }
  return { type: 'info', text: '未知' }
})
const mcpLines = computed(() => [
  { label: '调用次数', value: store.mcp?.total ?? '—' },
  { label: '平均耗时', value: fmtElapsed(store.mcp?.avg_ms) },
  { label: 'P95 耗时', value: fmtElapsed(store.mcp?.p95_ms) },
])

/* ═══════════════ ⑤ 情绪投递状态卡（本页独立 30s 轮询） ═══════════════ */
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

let signalTimer = null
onMounted(() => {
  loadSignal() // 进页面先拉一次
  signalTimer = setInterval(loadSignal, 30_000) // 之后每 30s 刷新
})
onUnmounted(() => {
  if (signalTimer) clearInterval(signalTimer) // 离开页面清定时器，防泄漏
  signalTimer = null
})

// 卡片右上角总览徽标
const signalOverall = computed(() => {
  if (signalError.value) return { tag: 'danger', text: '接口异常' }
  const s = signal.value
  if (!s) return { tag: 'info', text: '加载中' }
  if (!s.exists) return { tag: 'warning', text: '文件缺失' }
  if (!s.parsed) return { tag: 'danger', text: '解析失败' }
  const fails = Object.values(s.checks || {}).filter((c) => c && c.ok === false).length
  return fails ? { tag: 'warning', text: `${fails} 项未通过` } : { tag: 'success', text: '可投递' }
})

// 7 项检查的展示名（顺序与后端 signal_status 引擎校验顺序一致）
const CHECK_NAMES = [
  ['current_rank_present', 'current_rank 有效'],
  ['metric_value_present', 'metric_value 必填'],
  ['history_count_ok', 'history_count == 60'],
  ['formal_usable_ok', 'formal_usable 为 true'],
  ['contract_supported', '契约版本受支持'],
  ['known_at_ok', 'known_at ≥ 09:25'],
  ['previous_rank_ok', 'previous_rank 合法'],
]
const signalChecks = computed(() => {
  const s = signal.value
  if (!s || !s.parsed || !s.checks) return []
  return CHECK_NAMES.map(([key, name]) => ({
    key,
    name,
    ...(s.checks[key] || { ok: null, reason: '该检查缺失' }), // 防御：后端漏项也不崩
  }))
})

// fields 摘要：挑对用户最有信息量的几项
const signalFields = computed(() => {
  const f = signal.value?.fields
  if (!f) return []
  const pick = [
    ['current_rank', '当前情绪'],
    ['metric_value', '情绪值'],
    ['history_count', '历史样本'],
    ['known_at', '生成时间'],
    ['source_contract_version', '契约'],
  ]
  return pick
    .filter(([k]) => f[k] !== undefined && f[k] !== null)
    .map(([k, label]) => ({ key: k, label, value: f[k] }))
})

/* ═══════════════ ⑥ 版本卡 ═══════════════ */
// ui_mode：spa = 新版前端壳；legacy = 旧版前端壳
const uiModeText = computed(() =>
  store.version?.ui_mode === 'legacy' ? '旧版面板 (legacy)' : '新版 SPA 界面')
</script>

<style scoped>
/* 页面骨架：纵向卡片流，间距统一 16px */
.overview-page {
  display: flex;
  flex-direction: column;
  gap: 16px;
}

/* —— 页头 —— */
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
  font-size: 20px;
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

/* —— 通用卡片观感：var(--panel) 底 + 12px 圆角 + var(--line) 边框（任务约定） —— */
.card {
  background: var(--panel);
  border: 1px solid var(--line);
  border-radius: 12px;
  padding: 16px;
  display: flex;
  flex-direction: column;
  gap: 12px;
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
  font-size: 15px;
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

/* —— 指标卡 / 骨架卡栅格：>=200px 宽度自适应换行 —— */
.stat-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
  gap: 12px;
}
.sk-card {
  background: var(--panel);
  border: 1px solid var(--line);
  border-radius: 12px;
  padding: 16px;
}
.cards-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(340px, 1fr));
  gap: 16px;
  align-items: start; /* 卡片高度各自内容自适应，不强制拉伸 */
}

/* —— 时间轴卡 —— */
.next-runs {
  display: flex;
  align-items: center;
  gap: 6px;
  flex-wrap: wrap;
}
.ov-timeline {
  padding-left: 4px;
}
.tl-body {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}
.tl-label {
  font-size: 13px;
  font-weight: 600;
  color: var(--text);
}
.tl-detail {
  width: 100%; /* 让 detail 独占一行，时间轴卡片更整齐 */
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
  padding: 7px 0;
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
  font-variant-numeric: tabular-nums; /* 等宽数字：时间跳动宽度不抖 */
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
.card-foot {
  margin-top: auto; /* 把底部链接顶到卡片底部，卡片高度不一也整齐 */
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

/* —— 数据与 MCP 卡 —— */
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
.health-note {
  margin: 0;
  font-size: 13px;
  color: var(--text);
  background: var(--panel2);
  border-radius: 8px;
  padding: 10px 12px;
}
.mcp-grid {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 8px;
}
.mcp-cell {
  background: var(--panel2);
  border-radius: 8px;
  padding: 8px 10px;
}
.mcp-label {
  font-size: 11px;
}
.mcp-value {
  font-size: 16px;
  font-weight: 700;
  color: var(--text);
  font-variant-numeric: tabular-nums;
}

/* —— 情绪投递状态卡 —— */
.signal-body {
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.signal-summary {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  font-size: 12px;
}
.signal-fields {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}
.field-pill {
  background: var(--panel2);
  border-radius: 999px;
  padding: 3px 10px;
  font-size: 12px;
  color: var(--muted);
}
.field-pill b {
  color: var(--text);
  font-weight: 600;
}
.check-list {
  display: flex;
  flex-direction: column;
}
.check-row {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 6px 0;
  border-bottom: 1px solid var(--line);
  font-size: 13px;
}
.check-row:last-child {
  border-bottom: none;
}
/* 小圆点：通过绿 / 失败红 / 未知灰 */
.check-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: var(--muted);
  flex-shrink: 0;
}
.check-dot.ok {
  background: var(--ok);
}
.check-dot.fail {
  background: var(--err);
}
.check-name {
  color: var(--text);
  flex-shrink: 0;
  font-weight: 600;
}
.check-reason {
  color: var(--muted);
  font-size: 12px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap; /* 原因较长时截断，完整文案悬浮 title 展示 */
}
/* 未通过项：整行红色强调 */
.check-row.fail .check-name,
.check-row.fail .check-reason {
  color: var(--err);
}

/* —— 版本卡 —— */
.ver-big {
  font-size: 32px;
  font-weight: 800;
  color: var(--text);
  font-variant-numeric: tabular-nums;
}
.ver-big.stale {
  color: var(--warn); /* 有新版本时大版本号高亮 */
}
</style>
