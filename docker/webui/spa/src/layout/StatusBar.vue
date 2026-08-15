<template>
  <header class="status-bar">
    <el-button text class="collapse-btn" @click="$emit('toggle-collapse')">
      <el-icon><component :is="collapsed ? 'Expand' : 'Fold'" /></el-icon>
    </el-button>

    <!-- 数据新鲜度 -->
    <div class="sb-item" :class="lagClass">
      <span class="sb-label">数据</span>
      <span class="sb-value">
        {{ store.health?.latest ? fmtYMD(store.health.latest) : '—' }}
        <span v-if="store.lagDays !== null" class="sb-sub">滞后 {{ store.lagDays }} 天</span>
      </span>
    </div>

    <!-- 模拟盘状态 -->
    <RouterLink class="sb-item link" to="/paper">
      <span class="sb-label">模拟盘</span>
      <el-tag
        :type="paperTag.type"
        size="small"
        effect="dark"
      >{{ paperTag.text }}</el-tag>
    </RouterLink>

    <!-- 告警红点 -->
    <RouterLink class="sb-item link" to="/ops/alerts">
      <span class="sb-label">告警</span>
      <el-badge :value="store.alertCount" :hidden="store.alertCount === 0" type="danger">
        <span class="sb-value">{{ store.alertCount === 0 ? '无' : store.alertCount }}</span>
      </el-badge>
    </RouterLink>

    <div class="sb-spacer" />

    <!-- 刷新状态 + 错误 -->
    <span v-if="store.error" class="sb-error" :title="store.error">接口异常</span>
    <span class="sb-refresh" title="最近刷新时间">
      {{ store.lastRefresh ? `刷新 ${hhmm(store.lastRefresh)}` : '等待首次刷新' }}
    </span>

    <ThemeToggle />

    <!-- 时钟 -->
    <span class="sb-clock">{{ clock }}</span>
  </header>
</template>

<script setup>
// 学习点：computed 从 store 派生展示数据；setInterval 在 onUnmounted 清理（防泄漏）。
import { ref, computed, onMounted, onUnmounted } from 'vue'
import { useGlobalStore } from '../stores/global.js'
import { fmtYMD } from '../utils/format.js'
import ThemeToggle from '../components/ThemeToggle.vue'

defineProps({
  collapsed: { type: Boolean, default: false },
})
defineEmits(['toggle-collapse'])

const store = useGlobalStore()

const clock = ref('--:--:--')
let clockTimer = null
onMounted(() => {
  const tick = () => {
    clock.value = new Date().toLocaleTimeString('zh-CN', { hour12: false })
  }
  tick()
  clockTimer = setInterval(tick, 1000)
})
onUnmounted(() => clearInterval(clockTimer))

const hhmm = (d) =>
  `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`

// 数据滞后着色：1 天以内正常；2 天警告；更多/未知显示错误色
const lagClass = computed(() => {
  const lag = store.lagDays
  if (lag === null) return ''
  if (lag <= 1) return 'ok'
  if (lag <= 2) return 'warn'
  return 'err'
})

// 模拟盘徽标：交易开启=成功色；暂停=警告；观察期=品牌色；引擎缺失=错误
const paperTag = computed(() => {
  const p = store.paper
  if (!p || !p.modules_ok) return { type: 'danger', text: '不可用' }
  if (p.paused) return { type: 'warning', text: '已暂停' }
  if (p.trading_enabled) return { type: 'success', text: '交易开启' }
  if (p.engine_available) return { type: 'primary', text: '观察期' }
  return { type: 'danger', text: '引擎缺失' }
})
</script>

<style scoped>
.status-bar {
  display: flex;
  align-items: center;
  gap: 18px;
  padding: 8px 16px;
  border-bottom: 1px solid var(--line);
  background: var(--panel);
  flex-wrap: wrap;
}
.collapse-btn {
  padding: 6px;
}
.sb-item {
  display: flex;
  flex-direction: column;
  line-height: 1.3;
}
.sb-item.link {
  color: inherit;
  text-decoration: none;
}
.sb-label {
  font-size: 11px;
  color: var(--muted);
}
.sb-value {
  font-size: 13px;
  font-weight: 600;
}
.sb-value.stale {
  color: var(--warn);
}
.sb-sub {
  font-size: 11px;
  color: var(--muted);
  margin-left: 6px;
  font-weight: 400;
}
.sb-item.ok .sb-value { color: var(--ok); }
.sb-item.warn .sb-value { color: var(--warn); }
.sb-item.err .sb-value { color: var(--err); }
.sb-spacer { flex: 1; }
.sb-error {
  color: var(--err);
  font-size: 12px;
}
.sb-refresh {
  color: var(--muted);
  font-size: 11px;
}
.sb-clock {
  font-variant-numeric: tabular-nums;
  font-size: 13px;
  color: var(--text);
}
</style>
