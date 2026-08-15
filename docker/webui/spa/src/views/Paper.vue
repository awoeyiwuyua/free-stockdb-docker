<template>
  <div class="paper-page">
    <!-- 页头：标题 + 说明 + 手动刷新按钮（右对齐） -->
    <div class="page-head">
      <div>
        <h2 class="page-title">模拟盘</h2>
        <p class="page-sub">本地账本模拟交易：状态 / 账户 / 净值曲线 / 手动单步 / 明细 / apikey</p>
      </div>
      <div class="page-actions">
        <!-- lastRefresh 展示最近一次成功拉取的时间，让用户感知数据新鲜度 -->
        <span class="refresh-hint">上次刷新 {{ fmtClock(lastRefresh) }}</span>
        <el-button :icon="Refresh" :loading="refreshing" @click="handleRefresh">刷新</el-button>
      </div>
    </div>

    <!-- ① 加载态：首次进入还没拿到任何数据时，用骨架屏占位（el-skeleton） -->
    <el-skeleton v-if="loading" :rows="6" animated class="page-skeleton" />

    <!-- ② 错误态（页面级）：连 /api/paper/status 都失败且没有缓存 → 整页错误 + 重试按钮 -->
    <section v-else-if="!state.status" class="panel-card">
      <EmptyState
        icon="WarningFilled"
        title="模拟盘状态加载失败"
        :description="state.pageError || '状态接口不可用'"
      >
        <el-button type="primary" @click="handleRefresh">重试</el-button>
      </EmptyState>
    </section>

    <template v-else>
      <!-- 轮询中途失败：页面不崩，保留旧数据 + 顶部提示（不刷屏：只在由好变坏瞬间弹一次 ElMessage） -->
      <el-alert
        v-if="state.pageError"
        :title="'状态刷新失败（当前展示旧数据）：' + state.pageError"
        type="warning"
        :closable="false"
        show-icon
      />

      <!-- ============ 1) 状态卡 ============ -->
      <section class="panel-card">
        <div class="card-head">
          <div class="card-title">
            <!-- 状态圆点：绿=就绪 / 黄=降级 / 红=引擎不可用，一眼看出整体健康度 -->
            <span class="status-dot" :style="{ background: statusToneColor }" />
            状态
          </div>
          <!-- 暂停/恢复：危险操作，点击后 ElMessageBox 二次确认 -->
          <el-button
            :type="state.status.paused ? 'success' : 'warning'"
            :icon="state.status.paused ? VideoPlay : VideoPause"
            :disabled="!state.status.engine_available"
            @click="handleTogglePause"
          >
            {{ state.status.paused ? '恢复运行' : '暂停' }}
          </el-button>
        </div>

        <el-descriptions :column="4" border size="small" class="status-desc">
          <el-descriptions-item label="apikey">
            <!-- 掩码直接展示（后端返回的就是掩码），页面任何位置绝无原文 -->
            <el-tag :type="state.status.configured ? 'success' : 'warning'" size="small">
              {{ state.status.configured ? state.status.masked_key : '未配置' }}
            </el-tag>
          </el-descriptions-item>
          <el-descriptions-item label="交易开关">
            <el-tag :type="state.status.trading_enabled ? 'success' : 'info'" size="small">
              {{ state.status.trading_enabled ? '已开启' : '未开启' }}
            </el-tag>
          </el-descriptions-item>
          <el-descriptions-item label="暂停">
            <el-tag :type="state.status.paused ? 'warning' : 'success'" size="small">
              {{ state.status.paused ? '是' : '否' }}
            </el-tag>
          </el-descriptions-item>
          <el-descriptions-item label="引擎">
            <el-tag :type="state.status.engine_available ? 'success' : 'danger'" size="small">
              {{ state.status.engine_available ? '可用' : '不可用' }}
            </el-tag>
          </el-descriptions-item>
          <el-descriptions-item label="调度线程">
            <el-tag :type="state.status.scheduler_alive ? 'success' : 'info'" size="small">
              {{ state.status.scheduler_alive ? '存活' : '停止' }}
            </el-tag>
          </el-descriptions-item>
          <el-descriptions-item label="模块">
            <el-tag :type="state.status.modules_ok ? 'success' : 'danger'" size="small">
              {{ state.status.modules_ok ? '正常' : '缺失' }}
            </el-tag>
          </el-descriptions-item>
          <el-descriptions-item label="下次触发">
            {{ nextRunsText }}
          </el-descriptions-item>
          <el-descriptions-item label="时点">
            {{ (state.status.times || []).join(' / ') }}
          </el-descriptions-item>
        </el-descriptions>

        <!-- 降级文案：reason 非空 = 引擎不可用/未配 key/已暂停/交易开关未开 之一 -->
        <el-alert
          v-if="state.status.reason"
          :title="'模拟盘降级：' + state.status.reason"
          :type="state.status.engine_available ? 'warning' : 'error'"
          :closable="false"
          show-icon
          class="reason-alert"
        />
      </section>

      <!-- 引擎不可用：账户/曲线/单步/明细接口全部 501，统一降级空态，避免满屏报错 -->
      <section v-if="!state.status.engine_available" class="panel-card">
        <EmptyState
          icon="Warning"
          title="模拟盘引擎不可用"
          :description="state.status.reason || '引擎不可用，无法读取账户与明细数据'"
        />
      </section>

      <template v-else>
        <!-- ============ 2) 账户卡 ============ -->
        <section class="panel-card">
          <div class="card-head">
            <div class="card-title">账户总览</div>
            <!-- 最新快照交易日（fmtYMD：YYYYMMDD → YYYY-MM-DD）+ 名义本金 -->
            <span class="hint">{{ snapshotMeta }}</span>
          </div>
          <!-- 分区块错误态：该接口失败不影响其他卡片 -->
          <el-alert
            v-if="state.errs.overview"
            :title="'账户数据加载失败：' + state.errs.overview"
            type="error"
            :closable="false"
            show-icon
            class="sec-alert"
          />
          <!-- 空态：引擎可用但没有快照（还没跑过收盘对账） -->
          <EmptyState
            v-else-if="!state.overview?.latest_snapshot"
            icon="Wallet"
            title="暂无组合快照"
            description="收盘对账后生成账户数据；模拟盘首次运行后可见"
          />
          <!-- 正常态：StatCard 组合展示余额/持仓/盈亏 -->
          <div v-else class="stat-grid">
            <StatCard
              v-for="c in accountCards"
              :key="c.label"
              :label="c.label"
              :value="c.value"
              :sub="c.sub"
              :tone="c.tone"
            />
          </div>
        </section>

        <!-- ============ 3) 净值曲线 ============ -->
        <section class="panel-card">
          <div class="card-head">
            <div class="card-title">净值曲线</div>
            <span class="hint">组合净值 · 最近 60 个快照</span>
          </div>
          <el-alert
            v-if="state.errs.snapshots"
            :title="'净值曲线加载失败：' + state.errs.snapshots"
            type="error"
            :closable="false"
            show-icon
            class="sec-alert"
          />
          <EmptyState
            v-else-if="!state.snapshots.length"
            icon="TrendCharts"
            title="暂无净值快照"
            description="收盘对账后生成收益曲线"
          />
          <EChart v-else :option="chartOption" height="320px" />
        </section>

        <!-- ============ 4) 手动单步卡 ============ -->
        <section class="panel-card">
          <div class="card-head">
            <div class="card-title">手动单步（run-now）</div>
          </div>
          <div class="run-row">
            <!-- 7 时点选择器：按「数据/交易」分组，标签用后端 timepoint_labels 中文文案 -->
            <el-select v-model="runTp" placeholder="选择时点手动触发" style="min-width: 300px">
              <el-option-group label="数据时点（离线演练，不触碰券商）">
                <el-option
                  v-for="tp in dataTimepoints"
                  :key="tp"
                  :label="timepointLabel(tp)"
                  :value="tp"
                />
              </el-option-group>
              <el-option-group label="交易时点（需开启交易开关）">
                <el-option
                  v-for="tp in tradingTimepoints"
                  :key="tp"
                  :label="timepointLabel(tp)"
                  :value="tp"
                />
              </el-option-group>
            </el-select>
            <el-button type="primary" :loading="running" @click="handleRunNow">手动执行</el-button>
          </div>
          <p class="hint">
            交易时点（14:45 / 14:50 / 14:57 / 15:05）需先开启交易开关
            （PAPER_TRADING_ENABLED=true）；未开启时后端返回 501 文案，这里直接展示。
            数据时点（08:45 / 09:27 / 09:28）可离线演练决策流水线。
          </p>
          <!-- 执行结果：成功绿色 / 失败红色（含后端 501 中文降级文案） -->
          <el-alert
            v-if="runResult"
            :title="runResult"
            :type="runOk ? 'success' : 'error'"
            :closable="false"
            show-icon
            class="sec-alert"
          />
        </section>

        <!-- ============ 5) 明细：长页堆叠三段（决策 → 订单 → 事件） ============ -->
        <!--
          LuCI 长页风：明细不再用 el-tabs 分页切换，三块内容直接纵向铺开，
          每段 = 紧凑标题行（小标题 + 条数 badge）+ 内容，一眼看全、少一次点击。
          顺序固定：决策 → 订单 → 事件。
        -->
        <div class="detail-stack">
          <!-- 5a) 决策段：交易日 / 信号 prev→cur / 目标 prev→desired / 理由 / 状态 -->
          <section class="panel-card">
            <div class="sec-head">
              <h3 class="sec-title">决策</h3>
              <!-- 条数 badge：替代旧 tab 标签「决策（N）」，行高更紧凑 -->
              <el-tag size="small" :type="state.decisions.length ? 'primary' : 'info'">
                {{ state.decisions.length }}
              </el-tag>
            </div>
            <el-alert
              v-if="state.errs.decisions"
              :title="'决策加载失败：' + state.errs.decisions"
              type="error"
              :closable="false"
              show-icon
              class="sec-alert"
            />
            <EmptyState
              v-else-if="!state.decisions.length"
              icon="Document"
              title="暂无决策"
              description="运行 09:27 信号冻结+决策 时点后生成"
            />
            <el-table v-else :data="state.decisions" size="small" stripe>
              <el-table-column label="交易日" width="110">
                <template #default="{ row }">{{ fmtYMD(row.trade_date) }}</template>
              </el-table-column>
              <el-table-column label="信号 prev→cur" min-width="150">
                <template #default="{ row }">
                  {{ orDash(row.previous_rank) }} → {{ orDash(row.current_rank) }}
                </template>
              </el-table-column>
              <el-table-column label="目标 prev→desired" min-width="150">
                <template #default="{ row }">
                  {{ orDash(row.previous_target) }} → {{ orDash(row.desired_target) }}
                </template>
              </el-table-column>
              <el-table-column label="理由" min-width="170">
                <template #default="{ row }">{{ orDash(row.reason_code) }}</template>
              </el-table-column>
              <el-table-column label="状态" min-width="150">
                <template #default="{ row }">
                  <el-tag size="small" :type="statusTagType(row.status)">{{ row.status || '—' }}</el-tag>
                </template>
              </el-table-column>
            </el-table>
          </section>

          <!-- 5b) 订单段：交易日 / 动作 / 数量 / 价格类型 / 状态 / 时间 -->
          <section class="panel-card">
            <div class="sec-head">
              <h3 class="sec-title">订单</h3>
              <el-tag size="small" :type="state.orders.length ? 'primary' : 'info'">
                {{ state.orders.length }}
              </el-tag>
            </div>
            <el-alert
              v-if="state.errs.orders"
              :title="'订单加载失败：' + state.errs.orders"
              type="error"
              :closable="false"
              show-icon
              class="sec-alert"
            />
            <EmptyState
              v-else-if="!state.orders.length"
              icon="List"
              title="暂无订单"
              description="运行 14:50 窗口下单 时点后生成订单意图"
            />
            <el-table v-else :data="state.orders" size="small" stripe>
              <el-table-column label="交易日" width="110">
                <template #default="{ row }">{{ fmtYMD(row.trade_date) }}</template>
              </el-table-column>
              <el-table-column label="动作" width="110">
                <template #default="{ row }">
                  <el-tag size="small" type="info">{{ row.action }}</el-tag>
                </template>
              </el-table-column>
              <el-table-column label="数量" min-width="140">
                <template #default="{ row }">
                  {{ orDash(row.target_qty) }}（差额 {{ orDash(row.delta_qty) }}）
                </template>
              </el-table-column>
              <el-table-column label="价格类型" width="110">
                <template #default="{ row }">{{ orDash(row.price_type) }}</template>
              </el-table-column>
              <el-table-column label="状态" min-width="150">
                <template #default="{ row }">
                  <el-tag size="small" :type="statusTagType(row.status)">{{ row.status || '—' }}</el-tag>
                </template>
              </el-table-column>
              <el-table-column label="时间" min-width="170">
                <template #default="{ row }">{{ orDash(row.created_at) }}</template>
              </el-table-column>
            </el-table>
          </section>

          <!-- 5c) 事件段：级别着色 + 时点过滤（对齐旧页「过滤/明细」行为） -->
          <section class="panel-card">
            <div class="sec-head">
              <h3 class="sec-title">事件</h3>
              <el-tag size="small" :type="state.events.length ? 'primary' : 'info'">
                {{ state.events.length }}
              </el-tag>
            </div>
            <el-alert
              v-if="state.errs.events"
              :title="'事件加载失败：' + state.errs.events"
              type="error"
              :closable="false"
              show-icon
              class="sec-alert"
            />
            <template v-else>
              <div class="ev-toolbar">
                <el-select v-model="evTpFilter" size="small" style="width: 160px">
                  <el-option label="全部时点" value="" />
                  <el-option v-for="tp in evTimepoints" :key="tp" :label="tp" :value="tp" />
                </el-select>
                <span class="hint">共 {{ state.events.length }} 条 · 显示 {{ filteredEvents.length }} 条</span>
              </div>
              <el-timeline v-if="filteredEvents.length" class="ev-timeline">
                <el-timeline-item
                  v-for="e in filteredEvents"
                  :key="e.id"
                  :type="levelType(e.level)"
                  :timestamp="e.ts"
                  placement="top"
                >
                  <div class="ev-row">
                    <el-tag size="small" :type="levelType(e.level)">{{ e.level }}</el-tag>
                    <el-tag v-if="e.timepoint" size="small" type="info">{{ e.timepoint }}</el-tag>
                    <b class="ev-name">{{ e.event }}</b>
                    <span class="ev-detail">{{ e.detail }}</span>
                  </div>
                </el-timeline-item>
              </el-timeline>
              <EmptyState
                v-else
                icon="AlarmClock"
                :title="evTpFilter ? '该时点暂无事件' : '暂无事件'"
                description="模拟盘运行后生成系统事件记录"
              />
            </template>
          </section>
        </div>
      </template>

      <!-- ============ 6) apikey 卡（引擎不可用时也展示：配好 key 是恢复的第一步） ============ -->
      <section class="panel-card">
        <div class="card-head">
          <div class="card-title">MX apikey</div>
          <span class="hint">仅存本机 DATA_DIR，页面任何位置只展示掩码，绝无原文</span>
        </div>
        <div class="key-row">
          <el-input
            v-model="apiKeyInput"
            type="password"
            placeholder="粘贴 MX_APIKEY，仅存本机 /data/mx_apikey.txt"
            style="max-width: 420px"
            autocomplete="off"
            @keyup.enter="handleSaveKey"
          />
          <el-button type="primary" :loading="saving" @click="handleSaveKey">保存</el-button>
          <!-- 清除 = 危险操作 → 二次确认 -->
          <el-button type="danger" plain @click="handleClearKey">清除</el-button>
          <el-button :loading="connChecking" @click="handleConnectivity">连通自检</el-button>
        </div>
        <!-- 连通自检结果：JSON / 后端文案（501 时展示 error 文案） -->
        <pre class="conn-pre">{{ connResult }}</pre>
      </section>
    </template>
  </div>
</template>

<script setup>
// ==================== 模拟盘主页（G3） ====================
// 数据来源（docker/webui/app.py 对应 handler）：
//   /api/paper/status      状态（恒 200）→ 配置/开关/暂停/引擎/掩码/下次触发/调度/模块
//   /api/paper/overview    账户总览（balance / positions / pnl / model_nav）
//   /api/paper/snapshot    净值曲线（最近 60 个快照）
//   /api/paper/decisions   策略决策列表
//   /api/paper/orders      订单意图列表
//   /api/paper/events      系统事件时间轴
// 轮询节奏：onMounted 拉一次 + setInterval 30s，onUnmounted 清理（页面自管）。
// 引擎不可用时，读接口全部 501（{error: 中文文案}），本页用状态卡降级 + 空态兜底。

import { ref, reactive, computed, onMounted, onUnmounted } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Refresh, VideoPlay, VideoPause } from '@element-plus/icons-vue'
import StatCard from '../components/StatCard.vue'
import EmptyState from '../components/EmptyState.vue'
import EChart from '../components/EChart.vue'
import {
  getPaperStatus,
  getPaperOverview,
  getSnapshots,
  getDecisions,
  getOrders,
  getEvents,
  setPause,
  runNow,
  saveApikey,
  checkConnectivity,
} from '../api/paper.js'
import { fmtYMD, fmtMoney, fmtPct } from '../utils/format.js'

// 交易时点（与 app.py _PAPER_TRADING_TIMEPOINTS 保持一致）：
// 涉及券商 MX 接口，手动单步受交易开关约束；其余为数据时点，可离线演练。
const TRADING_TIMEPOINTS = ['14:45', '14:50', '14:57', '15:05']
const POLL_MS = 30000 // 轮询节拍：统一 30 秒

// —— 响应式状态：reactive 集中管理，模板里写 state.xxx ——
const state = reactive({
  loading: true, // 首次加载（骨架屏）
  pageError: '', // 页面级错误（状态接口失败）
  status: null, // /api/paper/status 载荷
  overview: null, // /api/paper/overview 载荷
  snapshots: [], // 净值快照数组
  decisions: [],
  orders: [],
  events: [],
  // 各明细接口独立的错误文案：单个失败不影响其他卡片（页面不崩）
  errs: { overview: '', snapshots: '', decisions: '', orders: '', events: '' },
  lastRefresh: null, // 最近成功刷新时间（Date）
})

// —— 交互态 ref ——
const refreshing = ref(false) // 手动刷新按钮 loading
const running = ref(false) // 手动单步执行中
const saving = ref(false) // apikey 保存中
const connChecking = ref(false) // 连通自检中
const runTp = ref('') // 选中的时点
const runResult = ref('') // run-now 结果文案
const runOk = ref(false) // run-now 是否成功（决定结果 alert 颜色）
const apiKeyInput = ref('') // apikey 输入框（password，仅提交不回显）
const connResult = ref('（点击「连通自检」查看结果）')
const evTpFilter = ref('') // 事件时点过滤

// 轮询防串扰：每次请求自增序号，只采纳最后一次请求的结果，
// 避免 30s 轮询与手动刷新并发时，慢的旧请求把新数据覆盖掉。
let seq = 0
let timer = null

// ==================== 数据拉取 ====================

// 拉状态 + 明细；status 恒 200，明细在引擎可用时才拉（否则 501）
async function loadAll() {
  const mySeq = ++seq
  try {
    const s = await getPaperStatus()
    if (mySeq !== seq) return // 已有更新的请求，本次结果作废
    state.status = s
    state.lastRefresh = new Date()
    if (state.pageError) state.pageError = '' // 恢复成功，清掉旧错误
    if (s.engine_available) await loadDetail(mySeq)
    else clearDetail() // 引擎不可用：明细接口必然 501，直接清空降级
  } catch (e) {
    if (mySeq !== seq) return
    const msg = e?.message || '状态接口不可用'
    // 只在「由好变坏」的瞬间弹一次 ElMessage，避免每 30s 轮询失败时刷屏
    if (!state.pageError) ElMessage.error('模拟盘状态刷新失败：' + msg)
    state.pageError = msg
  } finally {
    if (mySeq === seq) state.loading = false
  }
}

// 并行拉 5 个明细接口；每个独立 try/catch（settle），单个失败不拖垮整体
async function loadDetail(mySeq) {
  const settle = async (p) => {
    try {
      return { ok: true, data: await p }
    } catch (e) {
      return { ok: false, err: e?.message || '接口不可用' }
    }
  }
  const [ov, snap, dec, ord, ev] = await Promise.all([
    settle(getPaperOverview()),
    settle(getSnapshots(60)),
    settle(getDecisions(30)),
    settle(getOrders(500)),
    settle(getEvents(200)),
  ])
  if (mySeq !== seq) return
  // 成功：写数据并清错误；失败：保留旧数据，只记录错误文案
  applySection('overview', ov, (d) => { state.overview = d })
  applySection('snapshots', snap, (d) => { state.snapshots = d.snapshots || [] })
  applySection('decisions', dec, (d) => { state.decisions = d.decisions || [] })
  applySection('orders', ord, (d) => { state.orders = d.orders || [] })
  applySection('events', ev, (d) => { state.events = d.events || [] })
}

// 通用分块写入：ok 则应用数据，否则记 errs[key]
function applySection(key, r, apply) {
  if (r.ok) {
    apply(r.data)
    state.errs[key] = ''
  } else {
    state.errs[key] = r.err
  }
}

// 引擎不可用：清空全部明细（降级空态）
function clearDetail() {
  state.overview = null
  state.snapshots = []
  state.decisions = []
  state.orders = []
  state.events = []
  for (const k in state.errs) state.errs[k] = ''
}

// ==================== 操作（危险操作均二次确认） ====================

async function handleRefresh() {
  refreshing.value = true
  try {
    await loadAll()
  } finally {
    refreshing.value = false
  }
}

// 暂停/恢复：ElMessageBox.confirm 二次确认 → setPause(目标状态)
async function handleTogglePause() {
  const s = state.status
  if (!s) return
  const target = !s.paused // 现在没暂停 → 目标=暂停；反之=恢复
  const label = target ? '暂停' : '恢复'
  try {
    await ElMessageBox.confirm(
      `确认${label}模拟盘？${target ? '暂停后调度与手动单步都不会执行。' : '恢复后按交易开关状态继续触发。'}`,
      '危险操作确认',
      { type: 'warning', confirmButtonText: `确认${label}`, cancelButtonText: '取消' }
    )
  } catch {
    return // 用户点取消：ElMessageBox 会 reject，静默返回即可
  }
  try {
    const r = await setPause(target)
    ElMessage.success(r?.msg || (target ? '模拟盘已暂停' : '模拟盘已恢复'))
    await loadAll() // 暂停状态影响全部接口门控，整体刷新
  } catch (e) {
    ElMessage.error('切换失败：' + (e?.message || e))
  }
}

// 手动单步：写入操作，先二次确认；后端 501 文案（交易开关未开启等）直接展示
async function handleRunNow() {
  const tp = runTp.value
  if (!tp) {
    ElMessage.warning('请先选择要手动执行的时点')
    return
  }
  try {
    await ElMessageBox.confirm(
      `确认手动执行时点 ${tp}？\n这会对模拟盘数据库写入（决策/订单/快照），交易时点可能产生模拟下单。`,
      '写入操作确认',
      { type: 'warning', confirmButtonText: '确认执行', cancelButtonText: '取消' }
    )
  } catch {
    return
  }
  running.value = true
  runResult.value = ''
  try {
    const r = await runNow(tp)
    runOk.value = !!r.ok
    runResult.value = r.error ? `未执行：${r.error}` : (r.detail || (r.ok ? '执行完成' : '执行失败'))
    if (r.error) ElMessage.error(r.error)
    else if (r.ok) ElMessage.success(r.detail || '手动执行完成')
    else ElMessage.warning(r.detail || '手动执行未完成')
  } catch (e) {
    // 501：交易时点需开启交易开关 / apikey 未配置 / 已暂停 → 直接展示后端 error 文案
    runOk.value = false
    runResult.value = `未执行：${e?.message || e}`
    ElMessage.error(e?.message || '手动执行失败')
  } finally {
    running.value = false
    await loadAll() // 执行后刷新全部数据
  }
}

// 保存 apikey：写入本机文件，属"写入操作"——先二次确认；只提交、只展示
// 后端返回的掩码；成功后立刻清空输入框
async function handleSaveKey() {
  const key = apiKeyInput.value.trim()
  if (!key) {
    ElMessage.warning('请先粘贴 apikey 再保存')
    return
  }
  try {
    await ElMessageBox.confirm(
      '确认将 apikey 保存到本机 DATA_DIR？页面只会展示掩码，绝不回显原文。',
      '写入操作确认',
      { type: 'warning', confirmButtonText: '确认保存', cancelButtonText: '取消' }
    )
  } catch {
    return // 用户点取消
  }
  saving.value = true
  try {
    const r = await saveApikey(key)
    apiKeyInput.value = '' // 输入框不保留原文
    ElMessage.success(r.configured ? `apikey 已保存（仅存本机，掩码 ${r.masked}）` : 'apikey 未生效，请检查')
    await loadAll() // 保存后后端强制重建引擎，状态可能变化
  } catch (e) {
    ElMessage.error('保存失败：' + (e?.message || e))
  } finally {
    saving.value = false
  }
}

// 清除 apikey：危险操作，二次确认；空串提交 = 后端删除本地文件
async function handleClearKey() {
  try {
    await ElMessageBox.confirm(
      '确认清除本地保存的 apikey？清除后需重新配置才能使用交易时点。',
      '危险操作确认',
      { type: 'warning', confirmButtonText: '确认清除', cancelButtonText: '取消' }
    )
  } catch {
    return
  }
  try {
    await saveApikey('')
    apiKeyInput.value = ''
    ElMessage.success('已清除本地 apikey')
    await loadAll()
  } catch (e) {
    ElMessage.error('清除失败：' + (e?.message || e))
  }
}

// 连通自检：展示结果 JSON；HTTP 非 2xx（501）时展示后端 error 文案
async function handleConnectivity() {
  connChecking.value = true
  connResult.value = '检测中…'
  try {
    const r = await checkConnectivity()
    connResult.value = JSON.stringify(r, null, 2)
    ElMessage.success(r?.ok ? '连通自检通过' : '自检完成')
  } catch (e) {
    connResult.value = e?.message || '连通自检失败'
    ElMessage.error(e?.message || '连通自检失败')
  } finally {
    connChecking.value = false
  }
}

// ==================== 派生数据（computed） ====================

// 状态圆点颜色：绿=就绪 / 黄=降级（未配 key 或暂停）/ 红=引擎不可用
const statusToneColor = computed(() => {
  const s = state.status
  if (!s) return 'var(--muted)'
  if (!s.engine_available) return 'var(--err)'
  return s.configured && !s.paused ? 'var(--ok)' : 'var(--warn)'
})

// next_runs 是 "YYYYMMDD HH:MM" 数组，日期部分套 fmtYMD 展示
const nextRunsText = computed(() => {
  const list = state.status?.next_runs || []
  if (!list.length) return '（今日无未来时点）'
  return list.map((x) => x.replace(/^(\d{8})/, (m) => fmtYMD(m))).join(' · ')
})

// 时点分组：交易时点集合来自 app.py，其余归为数据时点
const dataTimepoints = computed(() =>
  (state.status?.times || []).filter((t) => !TRADING_TIMEPOINTS.includes(t))
)
const tradingTimepoints = computed(() =>
  (state.status?.times || []).filter((t) => TRADING_TIMEPOINTS.includes(t))
)

// 事件时点过滤：去重排序后生成下拉选项
const evTimepoints = computed(() =>
  [...new Set(state.events.map((e) => e.timepoint).filter(Boolean))].sort()
)
const filteredEvents = computed(() =>
  evTpFilter.value ? state.events.filter((e) => e.timepoint === evTpFilter.value) : state.events
)

// 账户卡 StatCard 数据：字段与 /api/paper/overview 一一对应
const accountCards = computed(() => {
  const ov = state.overview
  const bal = ov?.balance || {}
  const pos = ov?.positions || {}
  const pnl = ov?.pnl || {}
  const pnlVal = pnl.pnl ?? null
  return [
    { label: '组合净值（nav）', value: fmtMoney(bal.nav), sub: '最新快照组合净值', tone: 'brand' },
    { label: '可用资金', value: fmtMoney(bal.available_cash), sub: 'available_cash', tone: '' },
    {
      label: '持仓市值',
      value: fmtMoney(pos.position_mv),
      sub: `${pos.position_qty ?? '—'} 股 · 可卖 ${pos.available_to_sell_qty ?? '—'} 股`,
      tone: '',
    },
    {
      label: '累计盈亏（对初始本金）',
      value: fmtMoney(pnlVal),
      sub: `${fmtPct(pnl.pnl_pct)} · 名义本金 ${fmtMoney(ov?.model_nav)}`,
      // 盈亏着色：盈利绿 / 亏损红 / 无数据灰
      tone: pnlVal == null ? '' : pnlVal >= 0 ? 'ok' : 'err',
    },
  ]
})

// 快照交易日 + 名义本金（账户卡头部辅助行）
const snapshotMeta = computed(() => {
  const ls = state.overview?.latest_snapshot
  if (!ls) return ''
  return `快照交易日 ${fmtYMD(ls.trade_date)} · 名义本金 ${fmtMoney(state.overview?.model_nav ?? 0)}`
})

// 净值曲线 ECharts option：快照按交易日升序，涨绿跌红（颜色取自主题 CSS 变量）
const chartOption = computed(() => {
  const snaps = [...state.snapshots].sort((a, b) =>
    String(a.trade_date).localeCompare(String(b.trade_date))
  )
  if (!snaps.length) return null
  const xData = snaps.map((s) => fmtYMD(s.trade_date))
  const yData = snaps.map((s) => Number(s.nav))
  const up = yData[yData.length - 1] >= yData[0]
  const color = up ? cssVar('--ok', '#22c55e') : cssVar('--err', '#ef4444')
  return {
    grid: { left: 8, right: 20, top: 28, bottom: 8, containLabel: true },
    tooltip: { trigger: 'axis', valueFormatter: (v) => fmtMoney(v) },
    xAxis: {
      type: 'category',
      data: xData,
      boundaryGap: false, // 折线贴边，曲线更连贯
      axisLine: { lineStyle: { color: cssVar('--line', '#1e2c45') } },
      axisLabel: { color: cssVar('--muted', '#8fa2bc'), fontSize: 11 },
    },
    yAxis: {
      type: 'value',
      scale: true, // 净值变化小的时候自动放大差异，看得更清楚
      axisLabel: { color: cssVar('--muted', '#8fa2bc'), fontSize: 11 },
      splitLine: { lineStyle: { color: cssVar('--line', '#1e2c45') } },
    },
    series: [
      {
        name: '组合净值',
        type: 'line',
        data: yData,
        smooth: true,
        showSymbol: false,
        lineStyle: { width: 2, color },
        itemStyle: { color },
        // 渐变面积：hex 颜色直接拼透明度后缀（00=全透明 → 44=半透明）
        areaStyle: {
          color: {
            type: 'linear',
            x: 0, y: 0, x2: 0, y2: 1,
            colorStops: [
              { offset: 0, color: color + '44' },
              { offset: 1, color: color + '00' },
            ],
          },
        },
      },
    ],
  }
})

// ==================== 工具函数 ====================

// 空值统一显示占位符 —（与 format.js 风格一致）
function orDash(v) {
  return v === null || v === undefined || v === '' ? '—' : String(v)
}

// Date → 'HH:MM:SS'
function fmtClock(d) {
  return d ? d.toLocaleTimeString('zh-CN', { hour12: false }) : '—'
}

// 读主题 CSS 变量（ECharts canvas 无法直接用 var()，需要解析成具体颜色）
function cssVar(name, fallback) {
  try {
    return getComputedStyle(document.documentElement).getPropertyValue(name).trim() || fallback
  } catch {
    return fallback
  }
}

// 时点下拉 label：'14:50 窗口下单'（缺标签则只显示时点本身）
function timepointLabel(tp) {
  const lbl = state.status?.timepoint_labels?.[tp]
  return lbl ? `${tp} ${lbl}` : tp
}

// 事件级别 → Element Plus 类型（el-tag / el-timeline-item 通用取值）
function levelType(level) {
  return { ERROR: 'danger', WARN: 'warning', INFO: 'primary', DEBUG: 'info' }[level] || 'info'
}

// 订单/决策状态 → 标签颜色：终态绿 / 挂起黄 / 失败红
function statusTagType(s) {
  if (!s) return 'info'
  if (s.includes('FILLED')) return 'success'
  if (s.includes('PENDING') || s.includes('SUBMITTED')) return 'warning'
  if (s.includes('CANCEL') || s.includes('REJECT') || s.includes('FAIL')) return 'danger'
  return 'info'
}

// ==================== 生命周期：首次拉取 + 30s 轮询，卸载清理 ====================
onMounted(() => {
  loadAll() // 首次进入立刻拉一次
  timer = setInterval(loadAll, POLL_MS) // 之后每 30 秒静默刷新
})
onUnmounted(() => {
  if (timer) clearInterval(timer) // 离开页面必须清理定时器，防止泄漏
  timer = null
  seq += 1 // 让在途请求结果作废，避免卸载后还去改 state
})
</script>

<style scoped>
/* 页面纵向卡片流：间距 14px（LuCI 密度收紧），卡片由 .panel-card 统一样式 */
.paper-page {
  display: flex;
  flex-direction: column;
  gap: 14px;
}

/* 页头：标题左、操作右，窄屏自动换行 */
.page-head {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  flex-wrap: wrap;
  gap: 12px;
}
.page-title {
  margin: 0;
  font-size: 20px;
  font-weight: 700;
  color: var(--text);
}
.page-sub {
  margin: 4px 0 0;
  font-size: 13px;
  color: var(--muted);
}
.page-actions {
  display: flex;
  align-items: center;
  gap: 12px;
}
.refresh-hint {
  font-size: 12px;
  color: var(--muted);
}

/* 骨架屏：套一层卡片壳，视觉与真实卡片一致（内边距对齐收紧后的卡片） */
.page-skeleton {
  padding: 14px;
  background: var(--panel);
  border: 1px solid var(--line);
  border-radius: 12px;
}

/* 通用卡片：内边距 14px（密度哲学 12-14px 上限，比默认 16px 更紧凑） */
.panel-card {
  background: var(--panel);
  border: 1px solid var(--line);
  border-radius: 12px;
  padding: 14px;
}
/* 卡片头：标题 + 右侧操作/说明 */
.card-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  flex-wrap: wrap;
  margin-bottom: 10px;
}
.card-title {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 15px;
  font-weight: 600;
  color: var(--text);
}
.status-dot {
  width: 10px;
  height: 10px;
  border-radius: 50%;
  display: inline-block;
}
.status-desc {
  margin-top: 4px;
}
.reason-alert {
  margin-top: 10px;
}
.sec-alert {
  margin-bottom: 10px;
}
.hint {
  font-size: 12px;
  color: var(--muted);
}

/* 账户卡 StatCard 栅格：宽屏一排 4 个，窄屏自动换行；
   列宽下限 180px（比 200px 更窄 → 小屏也能多放一列，密度更高） */
.stat-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
  gap: 10px;
}

/* 手动单步行 */
.run-row {
  display: flex;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
}

/* apikey 行 + 连通自检结果 */
.key-row {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
  margin-bottom: 10px;
}
.conn-pre {
  margin: 0;
  padding: 12px;
  background: var(--panel2);
  border: 1px solid var(--line);
  border-radius: 8px;
  font-size: 12px;
  color: var(--text);
  overflow: auto;
  max-height: 320px;
  white-space: pre-wrap;
  word-break: break-all;
}

/* ============ 明细长页：三段卡片堆叠 + 段标题行 ============ */

/* 三段卡片间距 10px：比页面级 14px 更紧，视觉上归属同一「明细」区 */
.detail-stack {
  display: flex;
  flex-direction: column;
  gap: 10px;
}

/* 段标题行：小标题 + 条数 badge 一行排开，信息优先、无装饰性大字号 */
.sec-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  margin-bottom: 10px;
}
.sec-title {
  margin: 0;
  font-size: 14px;
  font-weight: 600;
  color: var(--text);
}

/* 事件时间轴工具栏 + 行内布局 */
.ev-toolbar {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 8px;
  flex-wrap: wrap;
}
.ev-timeline {
  padding-left: 4px;
}
/* 时间轴紧凑化：条目间距收窄、时间戳缩小，对齐 LuCI 密度哲学
   （el-timeline-item 内部模板不在本组件作用域，需要 :deep 穿透） */
.ev-timeline :deep(.el-timeline-item) {
  padding-bottom: 6px;
}
.ev-timeline :deep(.el-timeline-item__timestamp) {
  font-size: 12px;
  color: var(--muted);
}
.ev-row {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}
.ev-name {
  color: var(--text);
  font-size: 13px;
}
.ev-detail {
  color: var(--muted);
  font-size: 12px;
}
</style>
