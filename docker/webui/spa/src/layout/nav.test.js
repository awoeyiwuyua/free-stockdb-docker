// nav.test.js — 导航配置纯数据单测（Vitest）。运行：npm run test
// 学习点：nav.js 是"单一数据源"，路由表与侧边栏都从它生成；
//         测试它 = 守护 Phase 5.1（LuCI 经验版）菜单结构：
//         总览单页 + 两个分组（系统运维 7 项 / 模拟盘 3 项）、path 唯一、
//         badge 只挂通知中心、旧路径重定向全部落点有效。

import { describe, it, expect } from 'vitest'
import { TOP_ITEMS, NAV_GROUPS, NAV_ITEMS, LEGACY_REDIRECTS } from './nav.js'

describe('TOP_ITEMS：总览单页', () => {
  it('恰好 1 项且 path 为 /overview', () => {
    // Phase 5.1 顶层只有总览一个入口；多一个顶层项说明菜单结构又变回多页模式
    expect(TOP_ITEMS).toHaveLength(1)
    expect(TOP_ITEMS[0].path).toBe('/overview')
  })
})

describe('NAV_GROUPS：两个分组', () => {
  it('恰好 2 组，标题顺序为 系统运维 / 模拟盘', () => {
    // toEqual 同时校验顺序：分组被挪动过（或新增分组）都会在这里暴露
    expect(NAV_GROUPS.map((g) => g.title)).toEqual(['系统运维', '模拟盘'])
  })

  // it.each 表格化：一行 = 一个分组用例（钉死每组条数，防误删菜单项）
  it.each([
    ['系统运维', 7],
    ['模拟盘', 3],
  ])('分组「%s」恰好 %i 项', (title, count) => {
    const g = NAV_GROUPS.find((it) => it.title === title)
    // find 找不到会返回 undefined，先断言存在再取 items，避免 TypeError
    expect(g, `找不到分组「${title}」`).toBeTruthy()
    expect(g.items).toHaveLength(count)
  })

  it('每个分组都有 title 与 items 数组，且每个条目都有 path/title/icon', () => {
    NAV_GROUPS.forEach((g) => {
      expect(g.title, `分组缺少 title: ${JSON.stringify(g)}`).toBeTruthy()
      expect(Array.isArray(g.items), `分组「${g.title}」的 items 不是数组`).toBe(true)
      g.items.forEach((it) => {
        expect(it.path, `条目缺 path: ${JSON.stringify(it)}`).toBeTruthy()
        expect(it.title, `条目缺 title: ${JSON.stringify(it)}`).toBeTruthy()
        expect(it.icon, `条目缺 icon: ${JSON.stringify(it)}`).toBeTruthy()
      })
    })
  })
})

describe('NAV_ITEMS：展平全量菜单', () => {
  it('共 11 项 = TOP_ITEMS 1 + 系统运维 7 + 模拟盘 3', () => {
    // 自洽性：展平总数必须等于"顶层 + 各组条目"相加（flatMap 的唯一作用就是展平）
    const sum = TOP_ITEMS.length + NAV_GROUPS.reduce((n, g) => n + g.items.length, 0)
    expect(NAV_ITEMS.length).toBe(sum)
    // 钉死当前真实数量（1+7+3=11），防止误删菜单项；改菜单时记得同步这里
    expect(NAV_ITEMS.length).toBe(11)
  })

  it('path 全唯一', () => {
    // 路由 path 必须唯一：重复时 vue-router 只认第一个，后面的页面永远打不开
    const paths = NAV_ITEMS.map((it) => it.path)
    expect(new Set(paths).size).toBe(paths.length)
  })
})

describe('badge 红点徽标', () => {
  it('只出现在通知中心项（/ops/alerts），其余条目不得携带', () => {
    // 逐个展平条目断言：有 badge 的只能是通知中心，反之必须有 badge。
    // 若 SideNav 渲染时把 badge 当字符串当 key 读取，误挂会直接触发 undefined 异常
    NAV_ITEMS.forEach((it) => {
      if (it.path === '/ops/alerts') {
        expect(it.badge, '通知中心必须挂 badge').toBe('alertCount')
        expect(it.title, '带 badge 的必须是通知中心').toBe('通知中心')
      } else {
        expect(it.badge, `${it.path} 不应有 badge`).toBeUndefined()
      }
    })
  })
})

describe('LEGACY_REDIRECTS：老书签兜底', () => {
  it('恰好 6 条，映射与预期完全一致', () => {
    // toEqual 同时校验键与值：多一条、少一条、改错目标都会失败
    expect(LEGACY_REDIRECTS).toEqual({
      '/data/sync': '/ops/sync',
      '/data/mydb': '/ops/mydb',
      '/ops/system': '/ops/health',
      '/ops/version': '/overview',
      '/data': '/ops/sync',
      '/alerts': '/ops/alerts',
    })
  })

  it('每条目标地址都在 NAV_ITEMS 的 path 集合内（重定向不 404）', () => {
    const valid = new Set(NAV_ITEMS.map((it) => it.path))
    Object.values(LEGACY_REDIRECTS).forEach((target) => {
      expect(valid.has(target), `重定向目标 ${target} 不在菜单里`).toBe(true)
    })
  })
})
