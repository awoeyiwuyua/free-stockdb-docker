<template>
  <div class="audit-page">
    <!-- ========== 页头：标题 + 上次刷新时间 + 手动刷新按钮 ========== -->
    <div class="page-head">
      <div>
        <h2 class="page-title">审计报告</h2>
        <p class="page-desc">
          模拟盘只读审计（mode=ro 绝不写库）：重放一致性 / 防重 / 状态机转移 / 滑点 / 净值 vs 基准
        </p>
      </div>
      <div class="head-right">
        <span v-if="lastUpdated" class="updated">上次刷新 {{ fmtTime(lastUpdated) }}</span>
        <el-button size="small" :loading="refreshing" @click="load">
          <el-icon><Refresh /></el-icon>&nbsp;刷新
        </el-button>
      </div>
    </div>

    <!-- ========== 加载态：首次进入显示骨架屏 ========== -->
    <el-skeleton v-if="loading" :rows="8" animated />

    <template v-else>
      <!-- ========== 错误态：接口失败降级提示（页面不崩，旧数据仍保留） ========== -->
      <el-alert
        v-if="error"
        type="error"
        :closable="false"
        show-icon
        :title="`审计接口不可用：${error}`"
        class="err-alert"
      />

      <template v-if="report">
        <!-- ========== 统计卡：4 个关键指标，数值 >0 标红（err），正常标绿（ok） ========== -->
        <div class="stat-grid">
          <StatCard
            label="重放不一致"
            :value="mismatchCount"
            :tone="mismatchCount > 0 ? 'err' : 'ok'"
            :sub="`共 ${totalDecisions} 条决策`"
          />
          <StatCard
            label="重复意图"
            :value="duplicateIntents"
            :tone="duplicateIntents > 0 ? 'err' : 'ok'"
            sub="重复 intent_key 组数（正常 0）"
          />
          <StatCard
            label="非法状态转移"
            :value="illegalCount"
            :tone="illegalCount > 0 ? 'err' : 'ok'"
            sub="order_intents 状态机违规次数"
          />
          <StatCard
            label="平均滑点"
            :value="slippageText"
            :tone="slippageTone"
            :sub="slippageSub"
          />
        </div>

        <!-- ========== 空库 / 缺库降级态：核心字段全 0 且无曲线 → EmptyState ========== -->
        <EmptyState
          v-if="isEmpty"
          icon="Document"
          title="暂无审计数据"
          description="审计数据库为空或不可读（只读模式返回全 0）。模拟盘跑完首个交易日后，报告会自动填充。"
        />

        <template v-else>
          <!-- ========== 明细区：重放不一致 / 非法转移 / 订单状态 三块表格 ========== -->
          <div class="detail-grid">
            <div class="panel">
              <h3 class="panel-title">
                重放不一致明细
                <el-tag size="small" :type="mismatchCount > 0 ? 'danger' : 'success'">
                  {{ mismatchCount }}
                </el-tag>
              </h3>
              <el-table v-if="mismatchExamples.length" :data="mismatchExamples" size="small" class="tbl">
                <el-table-column prop="decision_id" label="决策 ID" min-width="110" show-overflow-tooltip />
                <el-table-column label="交易日" width="110">
                  <template #default="{ row }">{{ fmtYMD(row.trade_date) }}</template>
                </el-table-column>
                <el-table-column prop="desired_target" label="期望目标" width="90" align="right" />
                <el-table-column prop="replayed_desired" label="重放目标" width="90" align="right" />
                <el-table-column prop="reason" label="原因" min-width="200" show-overflow-tooltip />
              </el-table>
              <p v-else class="muted-note">✓ 全部决策重放一致，无明细</p>
            </div>

            <div class="panel">
              <h3 class="panel-title">
                非法状态转移明细
                <el-tag size="small" :type="illegalCount > 0 ? 'danger' : 'success'">
                  {{ illegalCount }}
                </el-tag>
              </h3>
              <el-table v-if="illegalExamples.length" :data="illegalExamples" size="small" class="tbl">
                <el-table-column prop="intent_key" label="意图键" min-width="120" show-overflow-tooltip />
                <el-table-column label="交易日" width="110">
                  <template #default="{ row }">{{ fmtYMD(row.trade_date) }}</template>
                </el-table-column>
                <el-table-column prop="previous" label="前一目标" width="90" align="right" />
                <el-table-column prop="desired" label="期望目标" width="90" align="right" />
              </el-table>
              <p v-else class="muted-note">✓ 无非法状态机转移</p>
            </div>

            <div class="panel">
              <h3 class="panel-title">订单生命周期状态</h3>
              <div class="status-hint">
                <el-tag size="small" :type="unfilledCount > 0 ? 'warning' : 'info'">
                  未成交 {{ unfilledCount }}
                </el-tag>
                <el-tag size="small" :type="partialCount > 0 ? 'warning' : 'info'">
                  部分成交 {{ partialCount }}
                </el-tag>
              </div>
              <el-table v-if="statusRows.length" :data="statusRows" size="small" class="tbl">
                <el-table-column prop="status" label="状态" min-width="160" show-overflow-tooltip />
                <el-table-column prop="count" label="笔数" width="100" align="right" />
              </el-table>
              <p v-else class="muted-note">暂无订单状态数据</p>
            </div>
          </div>

          <!-- ========== 净值 vs 基准：ECharts 双折线 ========== -->
          <div class="panel">
            <h3 class="panel-title">净值 vs 基准（159915）</h3>
            <!-- 净值有、基准没有（网络/数据源失败）→ 黄色提示，只画净值线 -->
            <el-alert
              v-if="!navEmpty && benchmarkEmpty"
              type="warning"
              :closable="false"
              show-icon
              title="基准曲线不可用（159915 日K 获取失败），本次仅展示策略净值"
              class="chart-alert"
            />
            <EChart v-if="!navEmpty" :option="chartOption" height="320px" />
            <EmptyState
              v-else
              icon="TrendCharts"
              title="暂无净值曲线"
              description="portfolio_snapshots 还没有快照数据，跑完首个交易日后自动生成。"
            />
            <p class="chart-note">
              说明：后端基准已按首日归一化为 1.0；策略净值按同一方法归一化（首日 = 1.0），
              两条线同基点才可对比。本页只读，不会写入任何数据。
            </p>
          </div>
        </template>

        <!-- 报告元信息：生成时间 + 审计的数据库路径（排障用） -->
        <p v-if="report" class="meta-line">
          生成时间：{{ fmtDT(report.generated_at) }} · 数据库：{{ report.db_path }}
        </p>
      </template>
    </template>
  </div>
</template>

<script setup>
// ============================================================
// PaperAudit.vue — 审计报告（M2 补位页：旧面板从未渲染过 /api/paper/audit）。
// 数据源 GET /api/paper/audit，对应 docker/webui/app.py 的 paper_audit_report：
// 后端逐表容错，任何表缺失都返回全 0 / []（不抛），所以前端只需处理网络级错误。
// 三态齐备：骨架屏加载 → 错误提示（不崩） → 数据 / 空态。
// ============================================================
import { ref, computed, onMounted, onUnmounted } from 'vue'
import { getAudit } from '../api/paper.js' // 审计接口（只读，mode=ro）
import { fmtYMD } from '../utils/format.js' // 20260814 → 2026-08-14
import StatCard from '../components/StatCard.vue'
import EmptyState from '../components/EmptyState.vue'
import EChart from '../components/EChart.vue'

// 图表两条线的颜色：读取主题 CSS 变量（策略=品牌蓝 --brand，基准=琥珀橙 --warn），
// 深浅主题下都跟随 --brand/--warn 取值，不写死十六进制。
// Canvas 不认 CSS 变量，所以和 OpsMcp/Paper 页一样在 JS 里 getComputedStyle 解析。
function cssVar(name, fallback) {
  try {
    return getComputedStyle(document.documentElement).getPropertyValue(name).trim() || fallback
  } catch {
    return fallback
  }
}

// —— 页面状态：loading 首次骨架 / refreshing 按钮转圈 / error 错误文案 / report 报告 ——
const loading = ref(true)
const refreshing = ref(false)
const error = ref('')
const report = ref(null)
const lastUpdated = ref(null)
let timer = null // 30s 轮询句柄（onUnmounted 必须清理，避免离开页面后继续发请求）

// 拉取审计报告：成功写数据并清错误；失败只降级提示，页面不崩
async function load() {
  refreshing.value = true
  try {
    const data = await getAudit()
    report.value = data
    error.value = ''
    lastUpdated.value = new Date()
  } catch (e) {
    // ApiError.message 已是后端中文错误文案（见 api/http.js），直接展示
    error.value = e?.message || '接口不可用'
  } finally {
    loading.value = false
    refreshing.value = false
  }
}

// 生命周期：进入页面立即拉一次 + 每 30s 自动刷新；离开页面清定时器
onMounted(() => {
  load()
  timer = setInterval(load, 30000)
})
onUnmounted(() => clearInterval(timer))

// —— 派生计算：从报告里安全取值（字段缺失一律兜底 0 / []，页面不崩） ——
const totalDecisions = computed(() => report.value?.total_decisions ?? 0)
const mismatchCount = computed(() => report.value?.replay_mismatches?.count ?? 0)
const mismatchExamples = computed(() => report.value?.replay_mismatches?.examples ?? [])
const duplicateIntents = computed(() => report.value?.duplicate_intents ?? 0)
const illegalCount = computed(() => report.value?.illegal_transitions?.count ?? 0)
const illegalExamples = computed(() => report.value?.illegal_transitions?.examples ?? [])
const unfilledCount = computed(() => report.value?.unfilled_count ?? 0)
const partialCount = computed(() => report.value?.partial_count ?? 0)

// 滑点：avg_slippage 是"比值"（如 0.015 = 1.5%，后端注明非百分比），
// 转成百分比展示更直观；无样本时后端给 None → 显示 —。
const avgSlippage = computed(() => report.value?.slippage?.avg_slippage ?? null)
const slipN = computed(() => report.value?.slippage?.n ?? 0)
const slippageText = computed(() =>
  avgSlippage.value == null ? '—' : `${(avgSlippage.value * 100).toFixed(3)}%`)
const slippageTone = computed(() =>
  avgSlippage.value == null || avgSlippage.value <= 0 ? 'ok' : 'err')
const slippageSub = computed(() =>
  avgSlippage.value == null ? '无限价成交样本' : `${slipN.value} 笔限价单`)

// 订单状态计数 {状态: 数量} → 表格行数组（el-table 只吃数组）
const statusRows = computed(() =>
  Object.entries(report.value?.order_status_counts ?? {}).map(([status, count]) => ({
    status,
    count,
  })))

// 空库 / 缺库判定：核心字段全 0 且无净值曲线 → 显示 EmptyState
const isEmpty = computed(() => {
  if (!report.value) return false
  return (
    totalDecisions.value === 0 &&
    mismatchCount.value === 0 &&
    illegalCount.value === 0 &&
    duplicateIntents.value === 0 &&
    slipN.value === 0 &&
    (report.value.nav_series?.length ?? 0) === 0
  )
})

const navs = computed(() => report.value?.nav_series ?? [])
const navEmpty = computed(() => navs.value.length === 0)
const bench = computed(() => report.value?.benchmark_series ?? [])
const benchmarkEmpty = computed(() => bench.value.length === 0)

// 净值 vs 基准 双折线 option：
// 后端把基准归一化到首日 1.0，而净值是绝对值（如 100000）——直接画会压扁基准线，
// 所以这里把净值也按首日归一化（nav / 首日 nav），两条线同基点才可比。
const chartOption = computed(() => {
  const base = navs.value.length && navs.value[0].nav > 0 ? navs.value[0].nav : null
  const navMap = base
    ? Object.fromEntries(navs.value.map((p) => [p.date, p.nav / base]))
    : {}
  const benchMap = Object.fromEntries(bench.value.map((p) => [p.date, p.value]))
  // x 轴取两条曲线日期的并集（基准只覆盖部分日期时也不丢点）
  const dates = Array.from(
    new Set([...navs.value.map((p) => p.date), ...bench.value.map((p) => p.date)]),
  )
  // 两条线颜色取自主题变量：策略=品牌蓝、基准=警告琥珀
  const navColor = cssVar('--brand', '#38bdf8')
  const benchColor = cssVar('--warn', '#f59e0b')
  return {
    tooltip: {
      trigger: 'axis',
      valueFormatter: (v) => (v == null ? '—' : Number(v).toFixed(4)),
    },
    legend: { data: ['策略净值', '159915 基准'], top: 0 },
    grid: { left: 56, right: 20, top: 36, bottom: 28 },
    xAxis: {
      type: 'category',
      boundaryGap: false,
      data: dates,
      axisLabel: { formatter: (v) => fmtYMD(v) }, // 20260814 → 2026-08-14
    },
    yAxis: { type: 'value', scale: true, axisLabel: { formatter: (v) => v.toFixed(3) } },
    series: [
      {
        name: '策略净值',
        type: 'line',
        data: dates.map((d) => navMap[d] ?? null),
        connectNulls: true,
        smooth: true,
        showSymbol: false,
        lineStyle: { width: 2, color: navColor },
        itemStyle: { color: navColor },
      },
      {
        name: '159915 基准',
        type: 'line',
        data: dates.map((d) => benchMap[d] ?? null),
        connectNulls: true,
        smooth: true,
        showSymbol: false,
        lineStyle: { width: 2, color: benchColor },
        itemStyle: { color: benchColor },
      },
    ],
  }
})

// 时间展示辅助（本地化即可，不引新依赖）
const fmtTime = (d) => new Date(d).toLocaleTimeString('zh-CN', { hour12: false })
const fmtDT = (v) => (v ? new Date(v).toLocaleString('zh-CN', { hour12: false }) : '—')
</script>

<style scoped>
/* 页面纵向排列：页头 → 统计卡 → 明细/图表，各块之间留 16px 呼吸空间 */
.audit-page {
  display: flex;
  flex-direction: column;
  gap: 16px;
}

/* —— 页头 —— */
.page-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 12px;
  flex-wrap: wrap;
}
.page-title {
  margin: 0;
  font-size: 18px; /* 小标题，与全站 .page-title 口径一致（无装饰性大字号） */
  font-weight: 700;
  color: var(--text);
}
.page-desc {
  margin: 4px 0 0;
  font-size: 12px;
  color: var(--muted);
}
.head-right {
  display: flex;
  align-items: center;
  gap: 12px;
}
.updated {
  font-size: 12px;
  color: var(--muted);
}

/* —— 统计卡栅格：宽屏一排 4 张，窄屏自动换行 —— */
.stat-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
  gap: 12px;
}

/* —— 面板通用样式 —— */
.panel {
  background: var(--panel);
  border: 1px solid var(--line);
  border-radius: 12px;
  padding: 14px; /* LuCI 密度：卡片内边距 12-14px */
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.panel-title {
  margin: 0;
  font-size: 14px;
  font-weight: 600;
  color: var(--text);
  display: flex;
  align-items: center;
  gap: 8px;
}

/* 明细三块：窄屏纵向堆叠 */
.detail-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(320px, 1fr));
  gap: 12px;
}
.tbl {
  width: 100%;
}
.muted-note {
  margin: 0;
  font-size: 13px;
  color: var(--muted);
}
.status-hint {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
}

/* —— 图表区 —— */
.chart-alert {
  margin-bottom: 4px;
}
.chart-note {
  margin: 0;
  font-size: 12px;
  color: var(--muted);
  line-height: 1.6;
}

/* —— 其他 —— */
.err-alert {
  border-radius: 12px;
}
.meta-line {
  margin: 0;
  font-size: 12px;
  color: var(--muted);
}
</style>
