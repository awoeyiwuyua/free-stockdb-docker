<template>
  <!-- 空态占位：整体居中排版（flex 纵向 + 水平居中），适合"暂无数据/占位页"场景 -->
  <div class="empty-state">
    <!-- icon：默认 Document 图标；component :is 支持"图标名字符串"或"图标组件对象"两种写法 -->
    <el-icon class="empty-icon"><component :is="icon" /></el-icon>
    <div v-if="title" class="empty-title">{{ title }}</div>
    <div v-if="description" class="empty-desc">{{ description }}</div>
    <!-- 默认插槽：调用方放入的操作按钮区；$slots.default 不存在时不渲染，避免留空白 -->
    <div v-if="$slots.default" class="empty-actions">
      <slot />
    </div>
  </div>
</template>

<script setup>
// 学习点：默认插槽（<slot />）——父组件写在标签里的任意内容都会被塞到这里。
// 空态组件只负责"排版 + 文案"，按钮放什么由父组件决定，职责单一。
import { Document } from '@element-plus/icons-vue'

defineProps({
  // icon 属性：默认给一个现成的 Document 图标组件对象。
  // 显式 import 的原因：即使 main.js 没有做全局图标注册，这里也能正常工作，
  // 不依赖"全局注册"这个环境约定，组件自包含、更稳。
  icon: { type: [String, Object], default: Document },
  title: { type: String, default: '' },
  description: { type: String, default: '' },
})
</script>

<style scoped>
.empty-state {
  display: flex;
  flex-direction: column;
  align-items: center;   /* 水平居中 */
  justify-content: center;
  text-align: center;    /* 文字多行时也居中 */
  padding: 48px 16px;
  gap: 10px;
}
.empty-icon {
  font-size: 48px;
  color: var(--muted);   /* 图标用弱化色，突出"空"的感觉 */
}
.empty-title {
  font-size: 16px;
  font-weight: 600;
  color: var(--text);
}
.empty-desc {
  font-size: 13px;
  color: var(--muted);
  max-width: 420px;      /* 说明文字过长时限制行宽，可读性更好 */
}
.empty-actions {
  margin-top: 8px;
  display: flex;
  gap: 8px;
}
</style>
