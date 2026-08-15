// views-null-safety.test.js — 空载荷安全性回归测试（事故类防护）。
// 背景：0.6.5/0.6.6 连续出现「Cannot read properties of null」——后端瞬时失败时
// 页面载荷为 null，模板裸读字段 → 渲染异常。本测试把各页在"所有接口都失败"
// （载荷保持 null/[]）状态下挂载一遍，断言不抛异常——以后谁再裸读，这里先红。
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount } from '@vue/test-utils'
import ElementPlus from 'element-plus'
import { createPinia } from 'pinia'
import { createRouter, createWebHistory } from 'vue-router'

// EChart 在 happy-dom 无 canvas：打桩成空组件（图渲染非本测试目标）
vi.mock('./components/EChart.vue', () => ({
  default: { template: '<div class="echart-stub" />', props: ['option', 'height'] },
}))

// 各域 API 全部拒绝：模拟后端抖动，页面载荷保持初始 null/[]
// vi.hoisted：mock 工厂会被提升到文件顶部，helper 必须放这里避免 TDZ
const { rejectAll } = vi.hoisted(() => ({
  rejectAll: (mod) => {
    const stub = {}
    for (const [k, v] of Object.entries(mod)) {
      stub[k] = typeof v === 'function' ? vi.fn().mockRejectedValue(new Error('后端暂不可用')) : v
    }
    return stub
  },
}))
vi.mock('./api/status.js', async (io) => rejectAll(await io()))
vi.mock('./api/paper.js', async (io) => rejectAll(await io()))
vi.mock('./api/ops.js', async (io) => rejectAll(await io()))
vi.mock('./api/diag.js', async (io) => rejectAll(await io()))
vi.mock('./api/data.js', async (io) => rejectAll(await io()))

import OpsSync from './views/OpsSync.vue'
import OpsHealth from './views/OpsHealth.vue'
import OpsDiag from './views/OpsDiag.vue'
import OpsLogs from './views/OpsLogs.vue'
import Paper from './views/Paper.vue'
import PaperAudit from './views/PaperAudit.vue'
import PaperSignal from './views/PaperSignal.vue'
import OpsAlerts from './views/OpsAlerts.vue'
import OpsMcp from './views/OpsMcp.vue'
import OpsMydb from './views/OpsMydb.vue'

const CASES = [
  ['/ops/sync', '数据同步', OpsSync],
  ['/ops/health', '系统健康', OpsHealth],
  ['/ops/diag', '诊断中心', OpsDiag],
  ['/ops/logs', '日志中心', OpsLogs],
  ['/paper', '模拟盘', Paper],
  ['/paper/audit', '审计报告', PaperAudit],
  ['/paper/signal', '信号体检', PaperSignal],
  ['/ops/alerts', '通知中心', OpsAlerts],
  ['/ops/mcp', 'MCP 观测', OpsMcp],
  ['/ops/mydb', '私有存储', OpsMydb],
]

describe('空载荷挂载安全（后端失败不崩）', () => {
  let router
  beforeEach(() => {
    router = createRouter({
      history: createWebHistory(),
      routes: CASES.map(([path, title, comp]) => ({
        path,
        component: comp,
        meta: { title },
      })),
    })
  })

  it.each(CASES)('%s 挂载不抛异常', async (path, _title, comp) => {
    await router.push(path)
    await router.isReady()
    // 挂载即触发 onMounted 拉数据（全部 reject）；不应有任何渲染/生命周期异常
    const wrapper = mount(comp, {
      global: { plugins: [ElementPlus, createPinia(), router] },
    })
    expect(wrapper.html()).toBeTruthy()
    wrapper.unmount()
  })
})
