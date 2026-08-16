<template>
  <!-- EChart：ECharts 按需封装（Phase 5 图表统一入口）。
       学习点：按需引入 = 只打包用到的图表类型，镜像里的 JS 更小、首屏更快。
       用法：<EChart :option="chartOption" height="320px" /> -->
  <div ref="el" class="echart" :style="{ height }" />
</template>

<script setup>
// 按需注册：折线/柱状/饼图 + 常用组件 + Canvas 渲染器
import * as echarts from 'echarts/core'
import { LineChart, BarChart, PieChart } from 'echarts/charts'
import {
  GridComponent,
  TooltipComponent,
  LegendComponent,
  TitleComponent,
  DataZoomComponent,
} from 'echarts/components'
import { CanvasRenderer } from 'echarts/renderers'
import { ref, onMounted, onUnmounted, watch } from 'vue'

echarts.use([
  LineChart, BarChart, PieChart,
  GridComponent, TooltipComponent, LegendComponent, TitleComponent, DataZoomComponent,
  CanvasRenderer,
])

const props = defineProps({
  option: { type: Object, required: true }, // ECharts option（数据变了自动重绘）
  height: { type: String, default: '300px' },
})

const el = ref(null)
let chart = null
let ro = null

onMounted(() => {
  chart = echarts.init(el.value)
  chart.setOption(props.option)
  // 容器尺寸变化（侧边栏折叠/窗口缩放）时自动 resize，图表不塌
  ro = new ResizeObserver(() => chart && chart.resize())
  ro.observe(el.value)
})

watch(() => props.option, (opt) => {
  if (chart && opt) chart.setOption(opt, { notMerge: true })
}, { deep: true })

onUnmounted(() => {
  if (ro) { ro.disconnect(); ro = null }
  if (chart) { chart.dispose(); chart = null }
})
</script>

<style scoped>
.echart {
  width: 100%;
}
</style>
