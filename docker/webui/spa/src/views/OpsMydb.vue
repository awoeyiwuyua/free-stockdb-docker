<template>
  <!-- ============================================================
       私有存储页（/ops/mydb）——旧面板「私有存储」子页搬迁。
       学习点：本页是"手动操作型"页面——表清单 30s 轮询保新鲜，
       读取/写入/查询都是用户点按触发；写入属数据改动，必须二次确认。
       ============================================================ -->
  <div class="page">

    <!-- 页头：标题 + 手动刷新（只刷新表清单） -->
    <div class="page-head">
      <h2 class="page-title">私有存储</h2>
      <el-button :icon="Refresh" :loading="tablesLoading" @click="loadTables(true)">刷新表清单</el-button>
    </div>

    <!-- 非阻塞错误条：轮询失败时不打断使用 -->
    <el-alert
      v-if="tablesError && tables.length"
      class="page-alert"
      type="error"
      :title="tablesError"
      show-icon
      :closable="true"
      @close="tablesError = null"
    />

    <!-- ① 加载态：骨架屏 -->
    <el-skeleton v-if="tablesLoading && !tables.length" :rows="5" animated />

    <!-- ② 错误态：表清单都拿不到 → EmptyState + 重试 -->
    <EmptyState
      v-else-if="tablesError && !tables.length"
      icon="WarningFilled"
      title="私有存储不可用"
      :description="tablesError"
    >
      <el-button type="primary" @click="loadTables(true)">重试</el-button>
    </EmptyState>

    <!-- ③ 正常态 -->
    <template v-else>
      <!-- 顶部指标：自定义表数量（getTables() 已过滤上游保留表） -->
      <div class="stat-grid">
        <StatCard label="自定义表" :value="tables.length" tone="brand" sub="getTables() 实时清单" />
      </div>

      <div class="two-col">
        <!-- ========== 表清单 + 读取 ========== -->
        <section class="card">
          <h3 class="card-title">表清单与读取</h3>
          <div class="form-row">
            <span class="label">选择表</span>
            <el-select v-model="selTable" filterable placeholder="选择自定义表" style="width: 220px">
              <el-option v-for="t in tables" :key="t" :label="t" :value="t" />
            </el-select>
          </div>
          <div class="form-row">
            <span class="label">key</span>
            <el-input
              v-model="readKey"
              placeholder="留空 = 列出表内全部键值"
              style="width: 220px"
              @keyup.enter="doRead"
            />
            <el-button type="primary" :icon="Search" :loading="reading" @click="doRead">读取</el-button>
          </div>

          <!-- 读取结果区：单 key 结果 / 全键列表 两种形态 -->
          <div class="read-result">
            <!-- 读取失败：错误文案优先展示（失败时 readResult 会清空，必须先判 readError） -->
            <el-alert v-if="readError" type="error" :title="readError" show-icon :closable="false" />
            <!-- 未读取过：引导提示 -->
            <EmptyState
              v-else-if="!readResult"
              icon="Reading"
              title="尚未读取"
              description="选择表后点击「读取」：key 留空会列出表内全部键值。"
            />
            <!-- 单条读取：value 可能是任意 JSON，用格式化文本展示 -->
            <template v-else-if="readSingle">
              <div class="sub-title">读取结果：{{ readSingle.table }}:{{ readSingle.key }}</div>
              <pre class="val-pre">{{ pretty(readSingle.value) }}</pre>
            </template>
            <!-- 全键列表：键 → 值 表格 -->
            <template v-else-if="readList">
              <EmptyState
                v-if="!readListRows.length"
                icon="Box"
                title="该表暂无数据"
                description="可到右侧「数据写入」区写入第一条记录。"
              />
              <template v-else>
                <div class="sub-title">
                  共 {{ readListRows.length }} 个键（{{ readList.table }}）
                </div>
                <el-table :data="readListRows" size="small" border max-height="300">
                  <el-table-column prop="key" label="键" width="140" show-overflow-tooltip />
                  <el-table-column prop="val" label="值" show-overflow-tooltip />
                </el-table>
              </template>
            </template>
          </div>
        </section>

        <!-- ========== 数据写入 ========== -->
        <section class="card">
          <h3 class="card-title">数据写入</h3>
          <p class="card-hint">
            写入私有表（表名不存在会自动创建，但不得覆盖上游保留表如 日k: / 股票代码）。写入前需二次确认。
          </p>
          <div class="form-row">
            <span class="label">表名</span>
            <!-- allow-create：既可从现有表选，也可直接输入新表名 -->
            <el-select
              v-model="writeTable"
              filterable
              allow-create
              default-first-option
              placeholder="选择或输入表名"
              style="width: 220px"
            >
              <el-option v-for="t in tables" :key="t" :label="t" :value="t" />
            </el-select>
          </div>
          <div class="form-row">
            <el-radio-group v-model="writeMode">
              <el-radio-button value="single">单条 key/value</el-radio-button>
              <el-radio-button value="batch">批量 items</el-radio-button>
            </el-radio-group>
          </div>

          <!-- 单条模式 -->
          <template v-if="writeMode === 'single'">
            <div class="form-row">
              <span class="label">key</span>
              <el-input v-model="writeKey" placeholder="键名（如 20260814）" style="width: 220px" />
            </div>
            <div class="form-row">
              <span class="label">value</span>
              <!-- 注意：placeholder 里含双引号，属性定界符改用单引号（HTML 属性里不能裸写 "） -->
              <el-input
                v-model="writeValue"
                type="textarea"
                :rows="4"
                placeholder='写入的值：任意 JSON（数字/字符串/对象），例如 {"date": 20260814, "close": 5.2}'
              />
            </div>
          </template>

          <!-- 批量模式：直接贴 JSON -->
          <template v-else>
            <div class="form-row">
              <el-input
                v-model="batchPayload"
                type="textarea"
                :rows="5"
                placeholder='批量格式：[["k1", 值1], ["k2", 值2]]  或  {"items": [["k1", 值1], ...]}'
              />
            </div>
          </template>

          <div class="form-row">
            <el-button type="primary" :icon="EditPen" :loading="writing" @click="doWrite">写入数据</el-button>
          </div>
        </section>
      </div>

      <!-- ========== 查询台 ========== -->
      <section class="card">
        <h3 class="card-title">查询台</h3>
        <p class="card-hint">
          直查 stockdb 任意表（等价于 <code>/?cmd=get&t=表名</code>）。表名可带前缀通配，如
          <code>股票代码</code>、<code>日k:000001:2024*</code>、<code>hk日k:00700*</code>。返回 JSON 自动表格化。
        </p>
        <div class="form-row">
          <el-input
            v-model="queryInput"
            placeholder="输入表名或查询语句，如 股票代码"
            style="width: 360px"
            @keyup.enter="doQuery"
          />
          <el-button type="primary" :icon="Search" :loading="querying" @click="doQuery">查询</el-button>
        </div>

        <!-- 查询结果 -->
        <div class="query-result">
          <!-- 查询失败：错误文案（同时已 ElMessage.error 提示） -->
          <el-alert v-if="queryError" type="error" :title="queryError" show-icon :closable="false" />
          <!-- 还没查过 -->
          <EmptyState
            v-else-if="!queryDone"
            icon="Monitor"
            title="等待查询"
            description="输入表名后回车或点「查询」，返回的 JSON 会按字段展开成表格。"
          />
          <!-- 查过但空返回 -->
          <EmptyState
            v-else-if="!queryColumns.length"
            icon="Box"
            title="无返回内容"
            description="该查询没有返回任何数据（可能是空表或返回了非 JSON）。"
          />
          <!-- 正常结果：动态列表格（最多展示前 500 行，防大表卡死） -->
          <template v-else>
            <div class="sub-title">
              返回 {{ queryRows.length }} 行
              <span v-if="queryTruncated" class="hint">（内容过多，仅展示前 {{ MAX_ROWS }} 行）</span>
            </div>
            <el-table :data="queryTableData" size="small" border max-height="420">
              <el-table-column
                v-for="col in queryColumns"
                :key="col"
                :prop="col"
                :label="col"
                min-width="120"
                show-overflow-tooltip
              />
            </el-table>
          </template>
        </div>
      </section>

    </template>
  </div>
</template>

<script setup>
// ================= 引入 =================
import { ref, computed, onMounted, onUnmounted } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
// 私有存储域 API：表清单 / 读取 / 写入 / 查询台
import { getTables, readData, writeData, queryStockdb } from '../api/data.js'
// 现成资产：指标卡 / 空态 / 图标（图标与 DataSync 一样显式 import，包已在依赖里）
import StatCard from '../components/StatCard.vue'
import EmptyState from '../components/EmptyState.vue'
import { Refresh, Search, EditPen } from '@element-plus/icons-vue'

// ================= 常量 =================
const POLL_MS = 30_000 // 表清单轮询节拍（本页只有清单需要保鲜，读取结果不动）
const MAX_ROWS = 500 // 查询结果最大展示行数

// ================= 表清单 =================
const tables = ref([]) // 自定义表名数组（后端已过滤上游保留表）
const tablesLoading = ref(false)
const tablesError = ref(null)

// 读取表单
const selTable = ref('') // 当前选中的表（读取区）
const readKey = ref('') // 读取的 key，留空 = 列出全部
const reading = ref(false)
const readResult = ref(null) // {table,key,value} 或 {table,keys,values}
const readError = ref(null)

// 写入表单
const writeMode = ref('single') // single | batch
const writeTable = ref('')
const writeKey = ref('')
const writeValue = ref('')
const batchPayload = ref('')
const writing = ref(false)

// 查询台
const queryInput = ref('')
const querying = ref(false)
const queryDone = ref(false) // 是否执行过查询（区分"没查"和"查了但空"）
const queryError = ref(null)
const queryColumns = ref([]) // 动态列名
const queryRows = ref([]) // 原始行（数组数组），表格数据在 computed 里转换

// ================= 派生 =================
// 读取结果两种形态：single = 有 value 字段；list = 有 keys 字段
const readSingle = computed(() => (readResult.value && 'value' in readResult.value ? readResult.value : null))
const readList = computed(() => (readResult.value && 'keys' in readResult.value ? readResult.value : null))
// 全键列表 → 表格行（值统一转成展示文本，避免表格渲染 [object Object]）
const readListRows = computed(() => {
  const v = readResult.value
  if (!v || !Array.isArray(v.keys)) return []
  return v.keys.map((k) => ({ key: k, val: displayValue(v.values?.[k]) }))
})
// 查询行（数组数组）→ el-table 要的对象数组（列名作 key）
const queryTableData = computed(() =>
  queryRows.value.slice(0, MAX_ROWS).map((r) =>
    Object.fromEntries(queryColumns.value.map((col, i) => [col, r[i]]))
  )
)
const queryTruncated = computed(() => queryRows.value.length > MAX_ROWS)

// ================= 表清单 =================
// 拉表清单；手动刷新时转圈，轮询静默。保留当前选中表（还在清单里就不换）
async function loadTables(manual = false) {
  if (manual) tablesLoading.value = true
  try {
    const r = await getTables()
    tables.value = r?.tables || []
    tablesError.value = null
    if (!tables.value.includes(selTable.value)) selTable.value = tables.value[0] || ''
  } catch (e) {
    tablesError.value = e?.message || '表清单接口不可用'
  } finally {
    tablesLoading.value = false
  }
}

// ================= 读取 =================
// key 留空 = 读整表（后端返回 keys+values），填 key = 读单条
async function doRead() {
  if (!selTable.value) {
    ElMessage.warning('请先选择表')
    return
  }
  reading.value = true
  readError.value = null
  try {
    readResult.value = await readData(selTable.value, readKey.value.trim())
  } catch (e) {
    readResult.value = null
    readError.value = e?.message || '读取失败'
    ElMessage.error(readError.value) // 后端 error 文案统一走 ElMessage
  } finally {
    reading.value = false
  }
}

// ================= 写入 =================
// 解析 value 文本：能解析成 JSON 就按 JSON 存（对象/数字/布尔），否则按原始字符串存
function parseSingleValue(text) {
  const t = (text || '').trim()
  if (!t) return ''
  try {
    return JSON.parse(t)
  } catch {
    return t
  }
}

// 解析批量 JSON：支持 [[k,v],...] 或 {items:[[k,v],...]} 两种写法
function parseBatchPayload(text) {
  const t = (text || '').trim()
  if (!t) throw new Error('请输入批量 JSON')
  const obj = JSON.parse(t)
  const items = Array.isArray(obj) ? obj : obj && Array.isArray(obj.items) ? obj.items : null
  if (!items) throw new Error('批量格式需为 [["k",值],...] 或 {"items":[["k",值],...]}')
  if (!items.length) throw new Error('批量 items 不能为空')
  return items.map((pair) => [String(pair[0]), pair[1]])
}

// 写入（危险操作：ElMessageBox.confirm 二次确认，取消直接 return）
async function doWrite() {
  const table = writeTable.value.trim()
  if (!table) {
    ElMessage.warning('请输入表名')
    return
  }
  let payload
  try {
    payload = writeMode.value === 'batch'
      ? { table, items: parseBatchPayload(batchPayload.value) }
      : { table, key: writeKey.value.trim(), value: parseSingleValue(writeValue.value) }
  } catch (e) {
    ElMessage.warning(e.message) // 客户端格式校验失败，还没发请求
    return
  }
  if (writeMode.value === 'single' && !payload.key) {
    ElMessage.warning('请输入 key')
    return
  }
  const confirmText = writeMode.value === 'batch'
    ? `确定向表「${table}」批量写入 ${payload.items.length} 条？写入后可用读取功能验证。`
    : `确定写入「${table}:${payload.key}」？写入后可用读取功能验证。`
  try {
    await ElMessageBox.confirm(confirmText, '确认写入', {
      type: 'warning',
      confirmButtonText: '写入',
      cancelButtonText: '取消',
    })
  } catch {
    return // 用户取消
  }
  writing.value = true
  try {
    const r = await writeData(payload)
    ElMessage.success(r?.msg || `已写入 ${r?.written ?? 0} 条`)
    await loadTables() // 表可能是新建的，刷新清单
  } catch (e) {
    ElMessage.error(e?.message || '写入失败') // 后端 400/501/500 的 error 文案
  } finally {
    writing.value = false
  }
}

// ================= 查询台 =================
async function doQuery() {
  const t = queryInput.value.trim()
  if (!t) {
    ElMessage.warning('请输入查询表名（如 股票代码）')
    return
  }
  querying.value = true
  queryError.value = null
  try {
    const data = await queryStockdb(t)
    // 把任意 JSON 结构统一转成 {columns, rows}（数组数组）
    const { columns, rows } = payloadToRows(data)
    queryColumns.value = columns
    queryRows.value = rows
    queryDone.value = true
  } catch (e) {
    queryError.value = e?.message || '查询失败'
    queryDone.value = true
    queryColumns.value = []
    queryRows.value = []
    ElMessage.error(queryError.value)
  } finally {
    querying.value = false
  }
}

// ================= 查询结果结构归一化 =================
// 学习点：后端返回的 JSON 形态不确定（数组 / 对象 / 对象里套对象），
// 这里统一折叠成"列名数组 + 行数组"再喂给 el-table，什么形态都能表格化。
function payloadToRows(data) {
  if (data === null || data === undefined) return { columns: [], rows: [] }
  // 1) 数组：对象数组 → 各字段当列；标量数组 → 单列「值」
  if (Array.isArray(data)) {
    if (!data.length) return { columns: [], rows: [] }
    if (isPlainObject(data[0])) {
      const cols = unionKeys(data)
      return { columns: cols, rows: data.map((item) => cols.map((c) => displayValue(item[c]))) }
    }
    return { columns: ['值'], rows: data.map((v) => [displayValue(v)]) }
  }
  // 2) 普通对象：值全是对象 → 展开成「键 + 各字段列」；否则两列「键 / 值」
  if (isPlainObject(data)) {
    const entries = Object.entries(data)
    if (!entries.length) return { columns: [], rows: [] }
    const allObj = entries.every(([, v]) => isPlainObject(v))
    if (allObj) {
      const cols = unionKeys(entries.map(([, v]) => v))
      return { columns: ['键', ...cols], rows: entries.map(([k, v]) => [k, ...cols.map((c) => displayValue(v[c]))]) }
    }
    return { columns: ['键', '值'], rows: entries.map(([k, v]) => [k, displayValue(v)]) }
  }
  // 3) 其它（数字/字符串）：单行单列
  return { columns: ['结果'], rows: [[displayValue(data)]] }
}

function isPlainObject(v) {
  return v !== null && typeof v === 'object' && !Array.isArray(v)
}

// 多个对象的所有字段名取并集（保持出现顺序、去重）
function unionKeys(items) {
  const set = new Set()
  items.forEach((item) => Object.keys(item).forEach((k) => set.add(k)))
  return [...set]
}

// 任意值 → 展示文本：对象/数组 JSON 化，避免表格里出现 [object Object]
function displayValue(v) {
  if (v === null || v === undefined) return 'null'
  if (typeof v === 'object') {
    try {
      return JSON.stringify(v)
    } catch {
      return String(v)
    }
  }
  return String(v)
}

// 单条读取的 value 美化展示（pre 块缩进排版）
function pretty(v) {
  if (v === null || v === undefined) return 'null'
  if (typeof v === 'object') {
    try {
      return JSON.stringify(v, null, 2)
    } catch {
      return String(v)
    }
  }
  return String(v)
}

// ================= 生命周期：首拉 + 30s 轮询 + 卸载清理 =================
let pollTimer = null
onMounted(() => {
  loadTables()
  pollTimer = setInterval(() => loadTables(), POLL_MS) // 只静默刷新清单
})
onUnmounted(() => {
  if (pollTimer) clearInterval(pollTimer)
  pollTimer = null
})
</script>

<style scoped>
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
  font-weight: 700;
  color: var(--text);
}
.page-alert {
  margin-bottom: 0;
}

/* 指标卡栅格：auto-fit + minmax(200px,1fr)（全站统一口径） */
.stat-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
  gap: 12px;
}

/* 两栏布局：宽屏 读取 | 写入 并排，窄屏自动堆叠 */
.two-col {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(340px, 1fr));
  gap: 16px;
  align-items: start;
}

.card {
  background: var(--panel);
  border: 1px solid var(--line);
  border-radius: 12px;
  padding: 14px; /* LuCI 密度：卡片内边距 12-14px */
}
.card-title {
  margin: 0 0 12px;
  font-size: 14px; /* 与全站 .panel-title 同口径 */
  font-weight: 600;
  color: var(--text);
}
.card-hint {
  margin: 0 0 12px;
  font-size: 12px;
  color: var(--muted);
}

/* 表单行：label 固定宽度，输入控件对齐 */
.form-row {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
  margin-bottom: 10px;
}
.label {
  width: 56px;
  font-size: 13px;
  color: var(--muted);
}

/* 子标题（结果区上方说明） */
.sub-title {
  font-size: 13px;
  color: var(--text);
  margin: 4px 0 8px;
}

/* 读取结果 / 查询结果容器 */
.read-result,
.query-result {
  margin-top: 8px;
}

/* 值展示 pre：等宽字体、可滚动 */
.val-pre {
  margin: 0;
  padding: 10px;
  max-height: 240px;
  overflow: auto;
  background: var(--panel2);
  border: 1px solid var(--line);
  border-radius: 8px;
  font: 12px/1.5 ui-monospace, 'SF Mono', Menlo, Consolas, monospace;
  color: var(--text);
  white-space: pre-wrap;
  word-break: break-all;
}
.hint {
  font-size: 12px;
  color: var(--muted);
}
</style>
