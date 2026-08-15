<template>
  <div class="ops-version">
    <!-- ============ 页头：刷新 + 旧面板逃生通道入口 ============ -->
    <div class="page-head">
      <h2 class="page-title">版本</h2>
      <div class="page-actions">
        <el-button :icon="Refresh" @click="load()">刷新</el-button>
        <!-- 旧面板逃生通道：新面板出问题时随时能跳回 legacy（新窗口打开） -->
        <el-button
          :icon="Link"
          tag="a"
          href="/legacy"
          target="_blank"
          rel="noopener"
          plain
        >旧面板入口 ↗</el-button>
      </div>
    </div>

    <!-- 三态一：加载态 -->
    <div v-if="loading" class="panel">
      <el-skeleton :rows="6" animated />
    </div>

    <!-- 三态二：错误态 -->
    <div v-else-if="error && !version" class="panel">
      <el-alert type="error" :closable="false" show-icon title="版本信息读取失败">
        <template #default>
          {{ error }}
          <el-button size="small" class="retry-btn" @click="load()">重试</el-button>
        </template>
      </el-alert>
    </div>

    <!-- 三态三：空态（理论上 webui 版本恒有值，仅兜底） -->
    <EmptyState
      v-else-if="!version"
      icon="Promotion"
      title="暂无版本信息"
      description="接口已就绪但未返回版本数据，请稍后重试"
    />

    <!-- ============ 正常态 ============ -->
    <template v-else>
      <!-- 轮询失败但手里有旧数据：弱提示 -->
      <el-alert
        v-if="error"
        type="warning"
        :closable="false"
        show-icon
        class="stale-alert"
        :title="`最近刷新失败：${error}（将自动重试）`"
      />

      <!-- stale 高亮条：上游已发布新版本时醒目提示升级 -->
      <el-alert
        v-if="version.stale"
        type="warning"
        :closable="false"
        show-icon
        class="stale-banner"
        :title="version.msg || '上游已发布新版本，建议升级'"
      />

      <!-- 版本信息卡片：webui.version / image.tag / upstream / ui_mode -->
      <section class="panel">
        <h3 class="panel-title">版本信息</h3>
        <div class="ver-row">
          <span class="ver-label">面板版本</span>
          <span class="ver-value">v{{ version.webui?.version || '—' }}</span>
        </div>
        <div class="ver-row">
          <span class="ver-label">镜像引擎</span>
          <!-- image.tag 可能为 null（本地开发没注入环境变量）→ 显示 '—' -->
          <span class="ver-value">{{ version.image?.tag || '—' }}</span>
        </div>
        <div class="ver-row">
          <span class="ver-label">上游最新</span>
          <span class="ver-value">
            <!-- 上游 release：版本号 + 跳转 GitHub 发布页的链接（新窗口，防钓鱼用 noopener） -->
            <template v-if="upstreamTag">
              {{ upstreamTag }}
              <a
                v-if="version.upstream?.html_url"
                class="ver-link"
                :href="version.upstream.html_url"
                target="_blank"
                rel="noopener"
              >打开发布页 ↗</a>
            </template>
            <template v-else>未获取到</template>
          </span>
        </div>
        <div class="ver-row">
          <span class="ver-label">界面模式</span>
          <span class="ver-value">
            <!-- ui_mode：当前前端壳是 spa 还是 legacy，徽标一眼可辨 -->
            <el-tag :type="version.ui_mode === 'legacy' ? 'warning' : 'success'" size="small" effect="dark">
              {{ version.ui_mode === 'legacy' ? '旧面板 legacy' : '新版 SPA' }}
            </el-tag>
            <el-tag v-if="!version.stale" type="info" size="small" class="fresh-tag" effect="plain">
              已是最新或无法对比
            </el-tag>
          </span>
        </div>
      </section>

      <!-- 逃生通道说明卡 -->
      <section class="panel legacy-hint">
        <h3 class="panel-title">旧面板入口</h3>
        <p class="legacy-text">
          新面板（SPA）仍在完善中，遇到功能缺失时可用旧面板应急：
          <a class="ver-link" href="/legacy" target="_blank" rel="noopener">打开 /legacy 旧面板 ↗</a>
        </p>
      </section>
    </template>
  </div>
</template>

<script setup>
// OpsVersion — 版本页：webui.version / image.tag / upstream release / stale 高亮 / ui_mode 徽标。
// 学习点：
// 1) 后端约定 version 载荷：{webui:{version}, image:{tag}, upstream:{tag_name,html_url,published_at}, stale, msg, ui_mode}，
//    upstream 网络探针失败时为 null，前端要容错（显示"未获取到"而不是崩）；
// 2) 外链一律 target=_blank + rel=noopener：新窗口打开且不泄露窗口引用（安全习惯）；
// 3) 版本探针后端缓存 1h，轮询 30s 不会真打 GitHub，放心轮询。
import { ref, computed, onMounted, onUnmounted } from 'vue'
import { Refresh, Link } from '@element-plus/icons-vue'
import { getVersion } from '../api/ops.js'
import EmptyState from '../components/EmptyState.vue'

const loading = ref(true)
const error = ref('')
const version = ref(null) // /api/version 全量载荷

let busy = false
let timer = null

async function load() {
  if (busy) return
  busy = true
  try {
    version.value = (await getVersion()) || null
    error.value = ''
  } catch (e) {
    error.value = e?.message || '版本检查接口未就绪'
  } finally {
    loading.value = false
    busy = false
  }
}

// upstream 可能为 null（GitHub 探针失败/离线），逐层取 tag_name，容错给 '—'
const upstreamTag = computed(() => {
  const up = version.value?.upstream
  return (up && (up.tag_name || up.tag)) || null
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
.ops-version {
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
.stale-alert {
  margin-bottom: 0;
}
/* stale 高亮：黄底强调 + 左边框，用户一进来就看到升级提示 */
.stale-banner {
  border-left: 4px solid var(--warn);
}
.ver-row {
  display: flex;
  align-items: center;
  gap: 16px;
  padding: 10px 0;
  border-bottom: 1px solid var(--line);
  font-size: 14px;
}
.ver-row:last-of-type {
  border-bottom: none;
}
.ver-label {
  width: 90px;
  flex-shrink: 0;
  color: var(--muted);
}
.ver-value {
  color: var(--text);
  font-weight: 600;
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}
.ver-link {
  color: var(--brand);
  text-decoration: none;
  font-weight: 500;
}
.fresh-tag {
  margin-left: 4px;
}
.legacy-hint .legacy-text {
  margin: 0;
  font-size: 13px;
  color: var(--muted);
  line-height: 1.8;
}
.retry-btn {
  margin-left: 12px;
}
</style>
