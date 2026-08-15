<template>
  <div class="placeholder">
    <h2>{{ title }}</h2>
    <p>页面骨架已就位（M0）——M2 将在此实现完整功能。</p>
    <el-tag type="info">路由: {{ route.path }}</el-tag>
  </div>
</template>

<script setup>
// 学习点：useRoute 拿到当前路由；computed 派生值（meta.title 变了这里自动变）。
import { computed } from 'vue'
import { useRoute } from 'vue-router'

const route = useRoute()
const title = computed(() => route.meta.title || route.path)
</script>
