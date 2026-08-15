<template>
  <div class="signal-page">
    <!-- ========== 页头：标题 + 上次刷新时间 + 手动刷新按钮 ========== -->
    <div class="page-head">
      <div>
        <h2 class="page-title">信号体检</h2>
        <p class="page-desc">
          最近交易日情绪信号文件（emotion/&lt;date&gt;.json）7 项校验体检，规则与引擎
          DATA_NOT_QUALIFIED 一致
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
      <!-- ========== 错误态：接口失败降级提示（页面不崩） ========== -->
      <el-alert
        v-if="error"
        type="error"
        :closable="false"
        show-icon
        :title="`信号体检接口不可用：${error}`"
        class="err-alert"
      />

      <template v-if="signal">
        <!-- ========== 概况卡：交易日 / 存在 / 解析 / 校验通过数 ========== -->
        <div class="panel">
          <h3 class="panel-title">概况</h3>
          <div class="stat-grid">
            <StatCard
              label="交易日"
              :value="tradeDate"
              tone="brand"
              sub="最近交易日（周末/节假日自动回退）"
            />
            <StatCard
              label="文件存在"
              :value="signal.exists ? '是' : '否'"
              :tone="signal.exists ? 'ok' : 'err'"
              sub="emotion 信号目录"
            />
            <StatCard
              label="解析成功"
              :value="signal.parsed ? '是' : '否'"
              :tone="signal.parsed ? 'ok' : 'err'"
              sub="JSON 根节点为对象"
            />
            <StatCard
              label="校验通过"
              :value="checksPassText"
              :tone="checksPassed === checksTotal && checksTotal > 0 ? 'ok' : 'err'"
              sub="7 项校验点"
            />
          </div>
          <!-- 后端 error 非空 → ElAlert warning（文件缺失 / 解析失败等中文原因） -->
          <el-alert
            v-if="signal.error"
            type="warning"
            :closable="false"
            show-icon
            :title="signal.error"
            class="signal-error"
          />
          <p class="path-line">信号文件：<code>{{ signal.path }}</code></p>
        </div>

        <!-- ========== 7 项 checks 逐项卡片：通过绿点 / 失败红点 + 失败说明 ========== -->
        <div v-if="checkItems.length" class="panel">
          <h3 class="panel-title">7 项校验点</h3>
          <div class="checks-grid">
            <div
              v-for="item in checkItems"
              :key="item.key"
              class="check-card"
              :class="item.ok ? 'is-ok' : 'is-err'"
            >
              <!-- 状态点：通过绿 / 失败红，色弱用户也能靠位置+文字区分 -->
              <span class="dot" :class="item.ok ? 'dot-ok' : 'dot-err'" />
              <div class="check-body">
                <div class="check-name">
                  {{ item.label }}
                  <el-tag size="small" :type="item.ok ? 'success' : 'danger'">
                    {{ item.ok ? '通过' : '失败' }}
                  </el-tag>
                </div>
                <!-- 后端返回的 reason 就是中文说明：失败时高亮，通过时弱化 -->
                <div class="check-reason" :class="item.ok ? '' : 'reason-err'">
                  {{ item.reason }}
                </div>
              </div>
            </div>
          </div>
        </div>

        <!-- ========== fields 关键字段摘要（仅展示，无敏感内容） ========== -->
        <div v-if="fieldRows.length" class="panel">
          <h3 class="panel-title">关键字段摘要</h3>
          <el-descriptions :column="2" border size="small" class="fields-desc">
            <el-descriptions-item v-for="f in fieldRows" :key="f.key" :label="f.label">
              <!-- formal_usable 是布尔，用标签展示更直观 -->
              <el-tag
                v-if="f.key === 'formal_usable'"
                size="small"
                :type="f.value === true ? 'success' : 'danger'"
              >
                {{ f.value === true ? 'true' : fmtField(f.value) }}
              </el-tag>
              <span v-else>{{ fmtField(f.value) }}</span>
            </el-descriptions-item>
          </el-descriptions>
        </div>

        <!-- ========== 文件缺失降级态：exists=false → EmptyState + 后端 error 文案 ========== -->
        <EmptyState
          v-if="!signal?.exists"
          icon="WarningFilled"
          title="信号文件缺失"
          :description="signal?.error || '最近交易日没有可用的信号文件'"
        />
      </template>
    </template>
  </div>
</template>

<script setup>
// ============================================================
// PaperSignal.vue — 信号文件体检（M2 补位页：旧面板从未渲染过 /api/paper/signal-status）。
// 数据源 GET /api/paper/signal-status，对应 docker/webui/app.py 的 signal_status：
// 对最近交易日的情绪信号文件做 7 项校验（与引擎 DATA_NOT_QUALIFIED 逐条一致）。
// 三态齐备：骨架屏加载 → 错误提示（不崩） → 数据 / 文件缺失空态。
// ============================================================
import { ref, computed, onMounted, onUnmounted } from 'vue'
import { getSignalStatus } from '../api/paper.js'
import { fmtYMD } from '../utils/format.js'
import StatCard from '../components/StatCard.vue'
import EmptyState from '../components/EmptyState.vue'

const loading = ref(true)
const refreshing = ref(false)
const error = ref('')
const signal = ref(null)
const lastUpdated = ref(null)
let timer = null // 30s 轮询句柄（onUnmounted 必须清理）

async function load() {
  refreshing.value = true
  try {
    const data = await getSignalStatus()
    signal.value = data
    error.value = ''
    lastUpdated.value = new Date()
  } catch (e) {
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

// 后端 payload 没有 trade_date 字段，但 path 形如 …/emotion/20260804.json，
// 从文件名提取 8 位数字日期再格式化（YYYYMMDD → YYYY-MM-DD）。
const tradeDate = computed(() => {
  const m = (signal.value?.path || '').match(/(\d{8})\.json$/)
  return m ? fmtYMD(m[1]) : '—'
})

// checks 中文名映射：让新手一眼看懂每项校验在查什么
const CHECK_LABELS = {
  current_rank_present: 'current_rank 存在且 0~1',
  metric_value_present: 'metric_value 必填可转 float',
  history_count_ok: 'history_count == 60',
  formal_usable_ok: 'formal_usable 必须为 true',
  contract_supported: '契约版本受支持',
  known_at_ok: 'known_at ≥ 当日 09:25',
  previous_rank_ok: 'previous_rank 合法（缺省也合法）',
}

// checks 对象 {key: {ok, reason}} → 卡片数组（顺序沿用后端返回顺序）
const checkItems = computed(() =>
  Object.entries(signal.value?.checks ?? {}).map(([key, c]) => ({
    key,
    ok: !!c?.ok,
    reason: c?.reason || '',
    label: CHECK_LABELS[key] || key,
  })))

const checksTotal = computed(() => checkItems.value.length)
const checksPassed = computed(() => checkItems.value.filter((c) => c.ok).length)
const checksPassText = computed(() =>
  checksTotal.value ? `${checksPassed.value}/${checksTotal.value}` : '—')

// fields 摘要行：只展示关键字段，全部无敏感内容（apikey 不在此列）
const FIELD_LABELS = {
  current_rank: 'current_rank',
  previous_rank: 'previous_rank',
  metric_value: 'metric_value',
  history_count: 'history_count',
  formal_usable: 'formal_usable',
  source_contract_version: 'source_contract_version',
  known_at: 'known_at',
}
const fieldRows = computed(() =>
  Object.entries(FIELD_LABELS).map(([key, label]) => ({
    key,
    label,
    value: signal.value?.fields?.[key],
  })))

// 空值统一显示 —（与 utils/format.js 风格一致）
const fmtField = (v) =>
  v === null || v === undefined || v === '' ? '—' : String(v)

const fmtTime = (d) => new Date(d).toLocaleTimeString('zh-CN', { hour12: false })
</script>

<style scoped>
/* 页面纵向排列，各块之间留 16px 呼吸空间 */
.signal-page {
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

/* —— 统计卡栅格 —— */
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
  gap: 12px;
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

/* —— 概况区 —— */
.signal-error {
  border-radius: 8px;
}
.path-line {
  margin: 0;
  font-size: 12px;
  color: var(--muted);
  word-break: break-all; /* 路径长，允许换行防溢出 */
}
.path-line code {
  background: var(--panel2);
  border: 1px solid var(--line);
  border-radius: 4px;
  padding: 1px 6px;
  color: var(--text);
}

/* —— 7 项校验卡片 —— */
.checks-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(300px, 1fr));
  gap: 10px;
}
.check-card {
  display: flex;
  align-items: flex-start;
  gap: 10px;
  background: var(--panel2);
  border: 1px solid var(--line);
  border-radius: 10px;
  padding: 12px;
}
/* 失败卡片整体描红，一眼看出问题项 */
.check-card.is-err {
  border-color: var(--err);
}
.check-card.is-ok {
  border-color: var(--line);
}
/* 状态点：12px 圆点，通过绿 / 失败红 */
.dot {
  width: 12px;
  height: 12px;
  border-radius: 50%;
  margin-top: 4px;
  flex-shrink: 0;
}
.dot-ok {
  background: var(--ok);
  box-shadow: 0 0 6px var(--ok); /* 微光晕，视觉更突出 */
}
.dot-err {
  background: var(--err);
  box-shadow: 0 0 6px var(--err);
}
.check-body {
  display: flex;
  flex-direction: column;
  gap: 4px;
  min-width: 0; /* 允许长文案折行而不是撑破卡片 */
}
.check-name {
  font-size: 13px;
  font-weight: 600;
  color: var(--text);
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}
.check-reason {
  font-size: 12px;
  color: var(--muted);
  line-height: 1.5;
}
.reason-err {
  color: var(--err); /* 失败说明用红色，重点提示 */
}

/* —— 字段摘要 —— */
.fields-desc {
  width: 100%;
}

/* —— 其他 —— */
.err-alert {
  border-radius: 12px;
}
</style>
