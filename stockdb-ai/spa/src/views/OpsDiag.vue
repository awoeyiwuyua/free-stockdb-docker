<template>
  <div class="ops-diag">
    <!-- ============ 页头：标题 + 汇总徽标 + 立即体检 ============ -->
    <div class="page-head">
      <div class="head-left">
        <h2 class="page-title">诊断中心</h2>
        <!-- 汇总徽标：后端 all_ok 全绿 → 绿色「全部通过」；任一失败 → 红色「N 项异常」 -->
        <el-tag
          v-if="data"
          :type="data.all_ok ? 'success' : 'danger'"
          size="small"
          effect="plain"
          class="summary-badge"
        >
          {{ data.all_ok ? '全部通过' : `${failCount} 项异常` }}
        </el-tag>
        <!-- 上次体检时间：generated_at 是后端生成时刻，数据新鲜度一眼可见 -->
        <span v-if="data" class="refresh-hint">上次体检 {{ fmtClock(data.generated_at) }}</span>
      </div>
      <div class="page-actions">
        <!-- 「立即体检」是手动动作 → 按钮 loading；60s 轮询是静默动作，不闪 loading -->
        <el-button type="primary" :icon="Refresh" :loading="checking" @click="load(true)">立即体检</el-button>
      </div>
    </div>

    <!-- 三态一：加载态（首次进入骨架屏，模拟卡片外形） -->
    <div v-if="loading && !data" class="panel">
      <el-skeleton :rows="8" animated />
    </div>

    <!-- 三态二：错误态（首次拉取就失败且没有旧数据：文案 + 重试，页面不崩） -->
    <div v-else-if="error && !data" class="panel">
      <el-alert type="error" :closable="false" show-icon title="诊断接口读取失败">
        <template #default>
          {{ error }}
          <el-button size="small" class="retry-btn" @click="load(true)">重试</el-button>
        </template>
      </el-alert>
    </div>

    <!-- 三态三：空态（接口正常但没有任何检查项——防御性兜底） -->
    <EmptyState
      v-else-if="!checks.length"
      icon="Aim"
      title="暂无检查项"
      description="后端未返回任何诊断检查项；可点右上角「立即体检」重试"
    />

    <!-- ============ 正常态 ============ -->
    <template v-else>
      <!-- 轮询失败但手里还有旧数据：顶部给一行弱提示，页面照常展示（降级不崩） -->
      <el-alert
        v-if="error"
        type="warning"
        :closable="false"
        show-icon
        :title="`最近体检失败：${error}（将自动重试）`"
      />

      <!-- ① 六检查卡片网格：label + 状态圆点 + note；失败卡红边框一眼定位 -->
      <section class="check-grid">
        <div
          v-for="c in checks"
          :key="c.name"
          class="check-card"
          :class="{ 'card-fail': !c.ok }"
        >
          <div class="check-head">
            <span class="check-dot" :style="{ background: c.ok ? 'var(--ok)' : 'var(--err)' }" />
            <span class="check-label">{{ c.label }}</span>
            <span class="check-state" :class="c.ok ? 'st-ok' : 'st-err'">
              {{ c.ok ? '通过' : '异常' }}
            </span>
          </div>
          <div class="check-note">{{ c.note || '—' }}</div>
        </div>
      </section>

      <!-- ② 环境信息：el-descriptions 两列展示（border + size=small，LuCI 密度） -->
      <section class="panel">
        <h3 class="panel-title">环境信息</h3>
        <el-descriptions :column="2" border size="small">
          <el-descriptions-item label="Python">{{ env?.python || '—' }}</el-descriptions-item>
          <el-descriptions-item label="架构">{{ env?.arch || '—' }}</el-descriptions-item>
          <el-descriptions-item label="前端版本">{{ env?.webui_version || '—' }}</el-descriptions-item>
          <el-descriptions-item label="界面模式">{{ uiModeLabel(env?.ui_mode) }}</el-descriptions-item>
          <el-descriptions-item label="镜像 tag">{{ env?.image_tag || '—' }}</el-descriptions-item>
          <el-descriptions-item label="启动时间">{{ env?.started || '—' }}</el-descriptions-item>
          <el-descriptions-item label="已运行">{{ uptimeText() }}</el-descriptions-item>
          <el-descriptions-item label="数据目录">{{ env?.data_dir || '—' }}</el-descriptions-item>
          <el-descriptions-item label="数据最新日">{{ env?.data_latest ? fmtYMD(env.data_latest) : '—' }}</el-descriptions-item>
        </el-descriptions>
      </section>

      <!-- ③ 底部说明：体检定位 + 降级语义，帮用户理解哪些异常需要真正处理 -->
      <section class="panel note-panel">
        <p class="note-text">
          <el-icon class="note-icon"><InfoFilled /></el-icon>
          诊断是「人点一下」的体检：网络类检查（上游 GitHub / 妙想 API）失败只降级提示、不影响本机数据；本机类检查（服务 / 模块 / 磁盘 / 日历）异常才需要关注。
        </p>
      </section>
    </template>
  </div>
</template>

<script setup>
// OpsDiag — 诊断中心：一键体检（六检查卡片 + 环境信息 + 底部说明），60s 静默轮询。
// 学习点：
// 1) 「立即体检」是手动动作 → 按钮 loading（checking）；60s 轮询是静默动作 → 不闪 loading，
//    两者共用 busy 互斥，防止上一轮没回来时请求堆积；
// 2) 汇总徽标直接从后端 all_ok 派生：全绿 → 绿 tag「全部通过」，否则红 tag「N 项异常」；
//    失败卡片用 :class 条件拼 card-fail 类 → 红边框（var(--err)），一眼定位问题项；
// 3) uptime_seconds 单位是「秒」，而 utils/format.js 的 fmtElapsed 期望「毫秒」→ ×1000 再格式化。
import { ref, computed, onMounted, onUnmounted } from 'vue'
import { ElMessage } from 'element-plus'
import { Refresh } from '@element-plus/icons-vue'
import { getDiag } from '../api/diag.js'
import EmptyState from '../components/EmptyState.vue'
import { fmtYMD, fmtElapsed } from '../utils/format.js'

const loading = ref(true)  // 首次加载中
const checking = ref(false) // 「立即体检」按钮 loading（只随手动动作出现）
const error = ref('')       // 最近一次失败文案
const data = ref(null)      // /api/diag 全量载荷 {generated_at, env, checks, all_ok}

let busy = false // 互斥：上一轮请求未回就跳过本轮（轮询 + 手动共用一把锁）
let timer = null

const checks = computed(() => data.value?.checks || [])
const env = computed(() => data.value?.env || null)
// 异常项数：徽标红色时显示「N 项异常」
const failCount = computed(() => checks.value.filter((c) => !c.ok).length)

async function load(manual = false) {
  if (busy) return
  busy = true
  if (manual) checking.value = true
  try {
    const r = await getDiag()
    data.value = r || null
    error.value = ''
    // 手动体检成功给一句摘要反馈；轮询成功保持静默（页面数据本身就在变）
    if (manual) {
      ElMessage.success(r?.all_ok ? '体检完成：全部通过' : `体检完成：${failCount.value} 项异常`)
    }
  } catch (e) {
    error.value = e?.message || '诊断接口未就绪'
    // 手动体检失败要立刻告诉用户；轮询失败只写 error，由顶部弱提示兜底
    if (manual) ElMessage.error('体检失败：' + error.value)
  } finally {
    loading.value = false
    checking.value = false
    busy = false
  }
}

// —— 展示派生 ——
// generated_at 形如 '2026-08-14T23:38:06'：截前 19 位 + T 换空格，变成人读的时间
function fmtClock(ts) {
  return ts ? String(ts).slice(0, 19).replace('T', ' ') : '—'
}
// ui_mode 是机器名：spa / legacy，翻译成人话再展示，未知值原样兜底
function uiModeLabel(mode) {
  if (mode === 'spa') return 'SPA 新版'
  if (mode === 'legacy') return '旧版界面'
  return mode || '—'
}
// uptime_seconds 单位是秒，fmtElapsed 期望毫秒 → ×1000；缺失显示 '—'
function uptimeText() {
  const s = env.value?.uptime_seconds
  return s == null ? '—' : fmtElapsed(Number(s) * 1000)
}

// —— 生命周期：挂载拉一次 + 60s 静默轮询；卸载清理定时器 ——
onMounted(() => {
  load()
  timer = setInterval(() => load(), 60000)
})
onUnmounted(() => {
  if (timer) clearInterval(timer)
  timer = null
})
</script>

<style scoped>
.ops-diag {
  display: flex;
  flex-direction: column;
  gap: 16px;
}
/* LuCI 紧凑页头：小标题 + 右侧操作 */
.page-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  flex-wrap: wrap;
  gap: 10px;
}
.head-left {
  display: flex;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
}
.page-title {
  margin: 0;
  font-size: 18px;
  font-weight: 700;
  color: var(--text);
}
.summary-badge {
  font-weight: 600;
}
.refresh-hint {
  font-size: 12px;
  color: var(--muted);
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
/* —— 六检查卡片网格：auto-fit + minmax(220px,1fr)，窄屏自动换行 —— */
.check-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
  gap: 12px;
}
.check-card {
  background: var(--panel);
  border: 1px solid var(--line);
  border-radius: 12px;
  padding: 14px; /* LuCI 密度：卡片内边距 12-14px */
  display: flex;
  flex-direction: column;
  gap: 8px;
}
/* 失败卡：红边框 + 极浅红底（--err 6% 透明叠加，深浅主题都协调） */
.check-card.card-fail {
  border-color: var(--err);
  background: rgba(239, 68, 68, 0.06);
}
.check-head {
  display: flex;
  align-items: center;
  gap: 6px;
}
.check-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  flex-shrink: 0; /* 圆点不随内容压缩变形 */
}
.check-label {
  font-size: 13px;
  font-weight: 600;
  color: var(--text);
}
/* 状态文字右对齐：label 靠左、通过/异常靠右，一眼扫出问题项 */
.check-state {
  margin-left: auto;
  font-size: 12px;
  font-weight: 600;
}
.st-ok  { color: var(--ok); }
.st-err { color: var(--err); }
.check-note {
  font-size: 12px;
  line-height: 1.5;
  color: var(--muted);
  word-break: break-all; /* note 可能是很长的路径/报错文本，允许折行 */
}
.note-panel {
  padding: 14px 16px; /* 说明块收紧一点，弱化存在感 */
}
.note-text {
  margin: 0;
  display: flex;
  align-items: flex-start;
  gap: 6px;
  font-size: 12px;
  line-height: 1.7;
  color: var(--muted);
}
.note-icon {
  margin-top: 3px;
  flex-shrink: 0;
}
.retry-btn {
  margin-left: 12px;
}
</style>
