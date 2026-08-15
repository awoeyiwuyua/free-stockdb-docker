<template>
  <div class="ops-mcp">
    <!-- ============ 页头 ============ -->
    <div class="page-head">
      <h2 class="page-title">MCP 观测</h2>
      <div class="page-actions">
        <el-button :icon="Refresh" @click="load()">刷新</el-button>
      </div>
    </div>

    <!-- 三态一：加载态 -->
    <div v-if="loading" class="panel">
      <el-skeleton :rows="8" animated />
    </div>

    <!-- 三态二：错误态（首次拉取就失败） -->
    <div v-else-if="error && !hasData" class="panel">
      <el-alert type="error" :closable="false" show-icon title="MCP 观测接口读取失败">
        <template #default>
          {{ error }}
          <el-button size="small" class="retry-btn" @click="load()">重试</el-button>
        </template>
      </el-alert>
    </div>

    <!-- 三态三：空态（接口正常但没有调用记录：MCP 工具还没被调过） -->
    <EmptyState
      v-else-if="!hasData"
      icon="Monitor"
      title="暂无 MCP 调用记录"
      description="MCP 工具被调用后自动采集；右上角「刷新」可手动触发一次"
    />

    <!-- ============ 正常态 ============ -->
    <template v-else>
      <!-- 轮询失败但手里有旧数据：弱提示，不打断展示 -->
      <el-alert
        v-if="error"
        type="warning"
        :closable="false"
        show-icon
        class="stale-alert"
        :title="`最近刷新失败：${error}（将自动重试）`"
      />

      <!-- 统计总览：total / ok_rate / avg_ms / p95_ms（/api/mcp/stats） -->
      <section class="panel">
        <h3 class="panel-title">统计总览</h3>
        <div class="stat-grid">
          <StatCard label="总调用" :value="stats?.total ?? 0" tone="brand" sub="窗口内累计次数" />
          <!-- 成功率着色：≥90% 绿 / ≥70% 黄 / 更低红，与旧面板同口径 -->
          <StatCard label="成功率" :value="fmtRate(stats?.ok_rate)" :tone="rateTone(stats?.ok_rate)"
            sub="最近 500 条窗口" />
          <StatCard label="平均耗时" :value="fmtElapsed(stats?.avg_ms)" sub="单次调用耗时" />
          <StatCard label="P95 耗时" :value="fmtElapsed(stats?.p95_ms)" sub="95% 调用低于该值" />
        </div>
      </section>

      <!-- 按工具分布：柱状图 + 表格（by_tool） -->
      <section class="panel">
        <h3 class="panel-title">按工具分布</h3>
        <div v-if="tools.length" class="tool-layout">
          <!-- EChart：只传 option，组件内部负责初始化/重绘/销毁 -->
          <EChart :option="chartOption" height="320px" class="tool-chart" />
          <!-- 表格补充成功率与耗时：柱状图只看次数，表格看质量 -->
          <el-table :data="tools" size="small" class="tool-table">
            <el-table-column prop="tool" label="工具" min-width="140" show-overflow-tooltip />
            <el-table-column prop="n" label="调用次数" width="90" align="right" />
            <el-table-column label="成功" width="80" align="right">
              <template #default="{ row }">
                <span :style="{ color: row.ok >= row.n ? 'var(--ok)' : 'var(--warn)' }">
                  {{ row.ok ?? 0 }}
                </span>
              </template>
            </el-table-column>
            <el-table-column label="平均耗时" width="100" align="right">
              <template #default="{ row }">{{ fmtElapsed(row.avg_ms) }}</template>
            </el-table-column>
          </el-table>
        </div>
        <EmptyState
          v-else
          icon="DataAnalysis"
          title="暂无工具分布数据"
          description="MCP 工具被调用后自动采集"
        />
      </section>

      <!-- 调用明细：最近 50 条（/api/mcp/calls），失败行红色 -->
      <section class="panel">
        <div class="panel-title-row">
          <h3 class="panel-title">调用明细</h3>
          <span class="list-meta" v-if="calls.length">最近 {{ calls.length }} 条</span>
        </div>
        <el-table
          :data="calls"
          size="small"
          :row-class-name="rowClass"
          class="calls-table"
        >
          <el-table-column label="时间" width="170">
            <template #default="{ row }">
              <span class="muted-cell">{{ fmtTs(row.ts) }}</span>
            </template>
          </el-table-column>
          <el-table-column prop="tool" label="工具" min-width="140" show-overflow-tooltip />
          <el-table-column label="状态" width="80">
            <template #default="{ row }">
              <!-- 成功绿点 / 失败红点：ok 与 is_error 双字段兼容旧记录 -->
              <span class="st-dot" :style="{ background: isOk(row) ? 'var(--ok)' : 'var(--err)' }" />
              <span class="st-text" :style="{ color: isOk(row) ? 'var(--ok)' : 'var(--err)' }">
                {{ isOk(row) ? '成功' : '失败' }}
              </span>
            </template>
          </el-table-column>
          <el-table-column label="耗时" width="100" align="right">
            <template #default="{ row }">{{ fmtElapsed(row.elapsed_ms) }}</template>
          </el-table-column>
          <el-table-column label="字节" width="90" align="right">
            <template #default="{ row }">{{ row.bytes ?? '—' }}</template>
          </el-table-column>
        </el-table>
      </section>
    </template>
  </div>
</template>

<script setup>
// OpsMcp — MCP 观测页：统计卡（total/ok_rate/avg_ms/p95_ms）+ 按工具分布
// （ECharts 柱状图 + 表格）+ 调用明细表格（失败行红），30s 轮询。
// 学习点：
// 1) getMcpStats 与 getMcpCalls 无依赖，Promise.all 并行拉，两个都拿到才渲染；
// 2) ECharts option 用 computed 派生：数据一变自动重绘（EChart 组件 watch option）；
// 3) Canvas 画不了 CSS 变量：图表颜色要在 JS 里读 getComputedStyle 拿当前主题色。
import { ref, computed, onMounted, onUnmounted } from 'vue'
import { Refresh } from '@element-plus/icons-vue'
import { getMcpStats, getMcpCalls } from '../api/ops.js'
import StatCard from '../components/StatCard.vue'
import EmptyState from '../components/EmptyState.vue'
import EChart from '../components/EChart.vue'
import { fmtElapsed } from '../utils/format.js'

const loading = ref(true)
const error = ref('')
const stats = ref(null) // {total, ok_rate, avg_ms, p95_ms, by_tool:[{tool,n,ok,avg_ms}]}
const calls = ref([])   // [{ts, tool, ok, is_error, elapsed_ms, bytes}]

let busy = false
let timer = null

// 有数据 = 统计块有内容（total>0）或明细有行；空态判定用
const hasData = computed(() => (stats.value?.total ?? 0) > 0 || calls.value.length > 0)

const tools = computed(() => stats.value?.by_tool || [])

async function load() {
  if (busy) return
  busy = true
  try {
    const [st, cl] = await Promise.all([getMcpStats(), getMcpCalls(50)])
    stats.value = st || null
    calls.value = Array.isArray(cl?.calls) ? cl.calls : []
    error.value = ''
  } catch (e) {
    error.value = e?.message || 'MCP 观测接口未就绪'
  } finally {
    loading.value = false
    busy = false
  }
}

// —— 展示派生 ——
// 耗时统一走 utils/format.js 的 fmtElapsed（ms → '45ms' / '1.23s' / '2.1min'），
// 不再在页面里手写 toFixed（全站数字展示统一入口）。
// 成功率 0~1 → 百分比（如 0.9123 → '91.2%'）。
// 注意不能用 fmtPct 直接套：fmtPct 期望的是"已经是百分数"的值（会加 +/- 号），
// 而 ok_rate 是 0~1 的比值，这里 ×100 后展示即可，不带正负号。
function fmtRate(v) {
  return v === null || v === undefined ? '—' : (Number(v) * 100).toFixed(1) + '%'
}
function rateTone(v) {
  if (v === null || v === undefined) return ''
  if (v >= 0.9) return 'ok'
  if (v >= 0.7) return 'warn'
  return 'err'
}
function fmtTs(ts) {
  return ts ? String(ts).slice(0, 19).replace('T', ' ') : '—'
}
// 成功判定与旧面板一致：ok 为真 且 不是错误记录
function isOk(row) {
  return !!row.ok && !row.is_error
}
// 失败行整体变红：给 tr 挂 row-fail 类，CSS 用 :deep 命中表格内部 td
function rowClass({ row }) {
  return isOk(row) ? '' : 'row-fail'
}

// —— ECharts 柱状图 option（computed：数据变自动重绘） ——
function themeColor(name) {
  // Canvas 不认 CSS 变量，这里读当前主题的实际色值（主题在挂载前已定好）
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim() || '#38bdf8'
}
const chartOption = computed(() => {
  const muted = themeColor('--muted')
  const line = themeColor('--line')
  return {
    tooltip: { trigger: 'axis', axisPointer: { type: 'shadow' } },
    legend: { show: false },
    grid: { left: 8, right: 16, top: 20, bottom: 8, containLabel: true },
    xAxis: {
      type: 'category',
      data: tools.value.map((t) => t.tool),
      // 工具名可能较长：旋转 30° + 全部显示，避免标签重叠
      axisLabel: { interval: 0, rotate: 30, color: muted },
      axisLine: { lineStyle: { color: line } },
    },
    yAxis: {
      type: 'value',
      name: '调用次数',
      nameTextStyle: { color: muted },
      axisLabel: { color: muted },
      splitLine: { lineStyle: { color: line } },
    },
    series: [
      {
        type: 'bar',
        data: tools.value.map((t) => t.n),
        barMaxWidth: 36,
        itemStyle: { color: themeColor('--brand'), borderRadius: [4, 4, 0, 0] },
      },
    ],
  }
})

// —— 生命周期：挂载拉一次 + 30s 轮询；卸载清理 ——
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
.ops-mcp {
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
.tool-layout {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 16px;
  align-items: start;
}
/* 窄屏时图与表上下堆叠，避免图表被压成一条缝 */
@media (max-width: 900px) {
  .tool-layout {
    grid-template-columns: 1fr;
  }
}
.tool-chart {
  min-width: 0;
}
.list-meta {
  font-size: 12px;
  color: var(--muted);
}
.stale-alert {
  margin-bottom: 0;
}
.muted-cell {
  color: var(--muted);
}
.st-dot {
  display: inline-block;
  width: 8px;
  height: 8px;
  border-radius: 50%;
  margin-right: 6px;
  vertical-align: middle;
}
.st-text {
  font-size: 13px;
  vertical-align: middle;
}
/* 失败行整体红色：el-table 内部 td 不在本组件作用域，需要 :deep() 穿透 */
:deep(.calls-table .row-fail td) {
  color: var(--err);
}
.retry-btn {
  margin-left: 12px;
}
</style>
