<template>
  <div class="ops-alerts">
    <!-- ============ 页头：标题 + 刷新 + 清空（危险操作，二次确认） ============ -->
    <div class="page-head">
      <h2 class="page-title">通知中心</h2>
      <div class="page-actions">
        <el-button :icon="Refresh" @click="load()">刷新</el-button>
        <el-button
          type="danger"
          :icon="Delete"
          :disabled="!alerts.length"
          @click="onClear"
        >清空全部</el-button>
      </div>
    </div>

    <!-- 三态一：加载态（骨架屏，模拟表格外形） -->
    <div v-if="loading" class="panel">
      <el-skeleton :rows="8" animated />
    </div>

    <!-- 三态二：错误态（首次拉取就失败且没有旧数据：展示文案 + 重试，页面不崩） -->
    <div v-else-if="error && !alerts.length" class="panel">
      <el-alert type="error" :closable="false" show-icon title="告警列表读取失败">
        <template #default>
          {{ error }}
          <el-button size="small" class="retry-btn" @click="load()">重试</el-button>
        </template>
      </el-alert>
    </div>

    <!-- 三态三：空态（请求成功但没有告警：绿色心情，说明一切正常） -->
    <EmptyState
      v-else-if="!alerts.length"
      icon="Bell"
      title="暂无告警"
      description="系统运行正常时不会有告警产生；告警由运营看门狗自动投递"
    />

    <!-- ============ 正常态：告警表格 ============ -->
    <template v-else>
      <!-- 轮询失败但手里还有旧数据：顶部给一行弱提示，表格继续展示不打断 -->
      <el-alert
        v-if="error"
        type="warning"
        :closable="false"
        show-icon
        class="stale-alert"
        :title="`最近刷新失败：${error}（将自动重试）`"
      />
      <div class="panel">
        <div class="list-meta">共 {{ alerts.length }} 条（最新在前）</div>
        <!-- 告警行点击可收起/展开「来源」明细：直接用原生 table 更轻，
             el-table 在这里没有排序/分页需求，原生表格即可 -->
        <el-table :data="alerts" size="small" class="alerts-table">
          <el-table-column label="时间" width="170">
            <template #default="{ row }">
              <span class="muted-cell">{{ fmtTs(row.ts) }}</span>
            </template>
          </el-table-column>
          <el-table-column label="级别" width="90">
            <template #default="{ row }">
              <!-- 级别色点：错误红 / 警告黄 / 提示品牌蓝，一眼扫出严重程度 -->
              <span class="level-dot" :style="{ background: levelColor(row.level) }" />
              <span class="level-text" :style="{ color: levelColor(row.level) }">
                {{ levelLabel(row.level) }}
              </span>
            </template>
          </el-table-column>
          <el-table-column label="来源" width="140" show-overflow-tooltip>
            <template #default="{ row }">
              <span class="muted-cell">{{ row.source || '—' }}</span>
            </template>
          </el-table-column>
          <el-table-column label="内容" min-width="240" show-overflow-tooltip>
            <template #default="{ row }">
              <span class="msg-cell" :style="{ color: levelColor(row.level) }">
                {{ row.message || '' }}
              </span>
            </template>
          </el-table-column>
        </el-table>
      </div>
    </template>
  </div>
</template>

<script setup>
// OpsAlerts — 通知中心（/ops/alerts）：告警列表（最新在前）+ 清空（二次确认）+ 30s 轮询。
// 学习点：
// 1) 顶栏红点数据来自全局 store（/api/overview 的 alerts.count），
//    清空成功后手动调 store.refresh() 把红点立刻归零，不用等下一轮 30s；
// 2) 清空是危险操作：ElMessageBox.confirm 二次确认，用户取消则直接 return；
// 3) 轮询失败但手里有旧数据 → 顶部弱提示 + 表格照常展示（降级不崩）。
import { ref, onMounted, onUnmounted } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Refresh, Delete } from '@element-plus/icons-vue'
import { getAlerts, clearAlerts } from '../api/ops.js'
import { useGlobalStore } from '../stores/global.js'
import EmptyState from '../components/EmptyState.vue'

const store = useGlobalStore() // 只读顶栏红点计数，清空后主动 refresh() 同步

const loading = ref(true) // 首次加载中
const error = ref('')     // 最近一次失败文案
const alerts = ref([])    // 告警数组 [{ts, level, source, message}, ...]，后端已按最新在前

// 互斥：上一轮还没回来就跳过本轮，避免轮询请求堆积
let busy = false
let timer = null

async function load() {
  if (busy) return
  busy = true
  try {
    // getAlerts(200) 与后端约定：{alerts: [...]}；容错兼容 items 字段名
    const r = await getAlerts(200)
    const list = Array.isArray(r?.alerts) ? r.alerts : Array.isArray(r?.items) ? r.items : []
    alerts.value = list
    error.value = ''
  } catch (e) {
    error.value = e?.message || '告警接口未就绪'
  } finally {
    loading.value = false
    busy = false
  }
}

// —— 清空（危险操作）：confirm → POST /api/alerts/clear → 提示 + 同步红点 ——
async function onClear() {
  if (!alerts.value.length) {
    ElMessage.warning('当前没有告警')
    return
  }
  try {
    // confirm 的 Promise 在「取消」时 reject → 下面的 catch 吞掉直接 return
    await ElMessageBox.confirm(
      `确认清空全部 ${alerts.value.length} 条告警？清空后不可恢复。`,
      '清空告警',
      { type: 'warning', confirmButtonText: '清空', cancelButtonText: '取消' }
    )
  } catch {
    return
  }
  try {
    const r = await clearAlerts()
    ElMessage.success(r?.msg || '已清空全部告警')
    alerts.value = []
    // 顶栏红点读的是 store（/api/overview），清空后主动刷新一次，红点立即消失
    store.refresh()
  } catch (e) {
    ElMessage.error('清空失败：' + (e?.message || e))
  }
}

// —— 展示派生 ——
// ts 形如 '2026-08-14T23:38:06'：截前 19 位 + T 换空格，变成人读的时间
function fmtTs(ts) {
  return ts ? String(ts).slice(0, 19).replace('T', ' ') : '—'
}
// 级别 → 颜色：错误红 / 警告黄（兼容 warn 别名）/ 其余（info）用品牌蓝
function levelColor(level) {
  if (level === 'error') return 'var(--err)'
  if (level === 'warning' || level === 'warn') return 'var(--warn)'
  return 'var(--brand)'
}
function levelLabel(level) {
  if (level === 'error') return '错误'
  if (level === 'warning' || level === 'warn') return '警告'
  return '提示'
}

// —— 生命周期：挂载拉一次 + 30s 轮询；卸载清理定时器 ——
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
.ops-alerts {
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
  padding: 14px; /* LuCI 密度：卡片内边距 12-14px */
}
.stale-alert {
  margin-bottom: 0;
}
.list-meta {
  margin-bottom: 10px;
  font-size: 12px;
  color: var(--muted);
}
.muted-cell {
  color: var(--muted);
}
.msg-cell {
  font-weight: 500;
}
.level-dot {
  display: inline-block;
  width: 8px;
  height: 8px;
  border-radius: 50%;
  margin-right: 6px;
  vertical-align: middle;
}
.level-text {
  font-size: 13px;
  vertical-align: middle;
}
.retry-btn {
  margin-left: 12px;
}
</style>
