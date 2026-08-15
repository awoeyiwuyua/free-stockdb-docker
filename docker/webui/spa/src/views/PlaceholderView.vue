<template>
  <div class="placeholder-page">
    <!-- EmptyState：居中空态壳。title 用当前路由 meta 里的标题（由 router 注入）；
         description 是通用提示语，所有占位页共用 -->
    <EmptyState
      :title="title"
      description="该页面将在 M2 实现完整功能"
    >
      <!-- el-tag 显示当前路由 path，开发时一眼确认"我在哪个页面" -->
      <el-tag type="info">路由：{{ route.path }}</el-tag>
    </EmptyState>

    <!-- 组件用法演示区：4 个 StatCard 示例，证明组件开箱即用 -->
    <section class="demo-section">
      <h3 class="demo-title">指标卡示例（StatCard）</h3>
      <p class="demo-hint">
        以下数值均为占位符 —，M2 接入 /api/overview 后将替换为真实数据。
      </p>
      <div class="stat-grid">
        <!-- 数据滞后：黄色警告系（warn），对应 health.lag_days -->
        <StatCard label="数据滞后" value="—" tone="warn" sub="来源：health.lag_days" />
        <!-- 告警：红色错误系（err），对应 alerts.count -->
        <StatCard label="告警" value="—" tone="err" sub="来源：alerts.count" />
        <!-- 模拟盘：品牌蓝色系（brand），对应 paper 状态字段 -->
        <StatCard label="模拟盘" value="—" tone="brand" sub="来源：paper 状态" />
        <!-- MCP 成功率：绿色正常系（ok），对应 mcp.ok_rate -->
        <StatCard label="MCP 成功率" value="—" tone="ok" sub="来源：mcp.ok_rate" />
      </div>
    </section>
  </div>
</template>

<script setup>
// 学习点：useRoute 读取当前路由；computed 派生标题（meta.title 变了这里自动更新）。
// 本页只是"占位壳"：顺便演示 EmptyState / StatCard 两个新组件的用法，M2 换成真实页面。
import { computed } from 'vue'
import { useRoute } from 'vue-router'
import StatCard from '../components/StatCard.vue'
import EmptyState from '../components/EmptyState.vue'

const route = useRoute()
const title = computed(() => route.meta.title || route.path)
</script>

<style scoped>
.placeholder-page {
  display: flex;
  flex-direction: column;
  gap: 20px;
}
.demo-section {
  background: var(--panel);
  border: 1px solid var(--line);
  border-radius: 12px;
  padding: 16px;
}
.demo-title {
  margin: 0 0 4px;
  font-size: 14px;
  font-weight: 600;
  color: var(--text);
}
.demo-hint {
  margin: 0 0 12px;
  font-size: 12px;
  color: var(--muted);
}
/* 响应式栅格：容器宽就一排 4 个，窄了自动换行，不用手写媒体查询 */
.stat-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
  gap: 12px;
}
</style>
