// nav.test.js — 导航配置纯数据单测（Vitest）。运行：npm run test
// 学习点：nav.js 是"单一数据源"，路由表与侧边栏都从它生成；
//         测试它 = 守护菜单结构合法、path 唯一、分组顺序不变。

import { describe, it, expect } from 'vitest'
import { NAV_GROUPS, NAV_ITEMS } from './nav.js'

describe('NAV_GROUPS 结构合法', () => {
  it('每个分组都有 title 与 items 数组', () => {
    // forEach 逐个分组检查：缺 title 或 items 不是数组，说明有人改坏了配置
    NAV_GROUPS.forEach((g) => {
      expect(g.title, `分组缺少 title: ${JSON.stringify(g)}`).toBeTruthy()
      expect(Array.isArray(g.items), `分组「${g.title}」的 items 不是数组`).toBe(true)
    })
  })

  it('每个导航项都有 path/title/icon 三件套', () => {
    // 双重循环：外层分组、内层条目；SideNav 渲染依赖这三件套，缺一个就报错
    NAV_GROUPS.forEach((g) => {
      g.items.forEach((it) => {
        expect(it.path, `条目缺 path: ${JSON.stringify(it)}`).toBeTruthy()
        expect(it.title, `条目缺 title: ${JSON.stringify(it)}`).toBeTruthy()
        expect(it.icon, `条目缺 icon: ${JSON.stringify(it)}`).toBeTruthy()
      })
    })
  })
})

describe('NAV_ITEMS 展平', () => {
  it('展平数量 = 各组 items 之和，且钉死当前真实数量', () => {
    // 自洽性：展平总数必须等于各组条目数相加（flatMap 的唯一作用就是展平）
    const sum = NAV_GROUPS.reduce((n, g) => n + g.items.length, 0)
    expect(NAV_ITEMS.length).toBe(sum)
    // 钉死当前真实数量（1+2+3+4=10），防止误删菜单项；改菜单时记得同步这里
    expect(NAV_ITEMS.length).toBe(10)
  })

  it('path 全唯一', () => {
    // 路由 path 必须唯一：重复时 vue-router 只认第一个，后面的页面永远打不开
    const paths = NAV_ITEMS.map((it) => it.path)
    expect(new Set(paths).size).toBe(paths.length)
  })

  it('四个分组标题依次为 总览/数据/模拟盘/运维', () => {
    // toEqual 同时校验顺序：顺序变了说明侧边栏分组被挪动过
    expect(NAV_GROUPS.map((g) => g.title)).toEqual(['总览', '数据', '模拟盘', '运维'])
  })

  it('模拟盘组包含审计报告与信号体检', () => {
    const paper = NAV_GROUPS.find((g) => g.title === '模拟盘')
    // find 找不到会返回 undefined，先断言存在再取 items，避免 TypeError
    expect(paper).toBeTruthy()
    const paths = paper.items.map((it) => it.path)
    expect(paths).toContain('/paper/audit')
    expect(paths).toContain('/paper/signal')
  })
})
