// api/status.test.js — 系统域接口（status.js）契约测试。
// 学习点：
//   1) 测试不发真实网络请求：vi.stubGlobal('fetch', vi.fn(...)) 接管全局 fetch，
//      每次调用都会记进 mock.calls，事后逐条断言"打给了谁、用什么方法、带什么 body"；
//   2) mock 只需实现 http.js 用到的 ok / status / text 三个字段，text 返回 '{}' 让 JSON.parse 不报错；
//   3) 契约测试的意义：谁改了接口路径/参数名/body 字段，这里立刻红，
//      页面不会静默请求错误地址（旧面板行为基线就是这些路径）。

import { describe, it, expect, afterEach, vi } from 'vitest'
import {
  getStatus,
  getHealth,
  getHistory,
  getSchedule,
  saveSchedule,
  getLog,
  getContainerLogs,
  restartContainer,
  runSync,
  hkSync,
} from './status.js'

// 造一个"长得很像 Response"的对象：http.js 只读 ok / status / text 三个属性
const mockFetchOk = () =>
  vi.fn().mockResolvedValue({
    ok: true,
    status: 200,
    text: async () => '{}', // 空对象 JSON，http.js 解析后返回 null；测试不关心返回值
  })

// 从第 idx 次 fetch 调用取出 [url, options] 并解构成对象，方便断言
const callAt = (fetchMock, idx = 0) => {
  const [url, opts] = fetchMock.mock.calls[idx]
  return { url, opts }
}

afterEach(() => {
  // 撤掉全局 fetch 假实现，避免污染其他测试文件（vi.stubGlobal 的配套清理）
  vi.unstubAllGlobals()
})

describe('GET 类接口：URL 与方法', () => {
  // it.each 表格化：一行 = 一个用例，重复样板只写一遍
  it.each([
    ['getStatus', getStatus, '/api/status'],
    ['getHealth', getHealth, '/api/health'],
    ['getHistory', getHistory, '/api/history'],
    ['getSchedule', getSchedule, '/api/schedule'],
    ['getLog(80)', () => getLog(80), '/api/log?n=80'],
    ['getContainerLogs(150)', () => getContainerLogs(150), '/api/container/logs?tail=150'],
  ])('%s 请求 %s', async (_label, fn, expectUrl) => {
    const fetchMock = mockFetchOk()
    vi.stubGlobal('fetch', fetchMock)

    await fn()

    const { url, opts } = callAt(fetchMock)
    expect(url).toBe(expectUrl) // URL 逐字符一致（toContain 会放过多出来的尾巴）
    expect(opts.method).toBe('GET')
  })
})

describe('saveSchedule：query 参数拼接', () => {
  it('tradingOnly=false 时 action/enabled/times/trading_only 四项齐全', async () => {
    const fetchMock = mockFetchOk()
    vi.stubGlobal('fetch', fetchMock)

    // 任务要求：URL 必须同时含这四个片段（toContain 逐一锁定）
    await saveSchedule(true, ['08:30', '15:30'], false)

    const { url, opts } = callAt(fetchMock)
    expect(url).toContain('action=save')
    expect(url).toContain('enabled=true')
    expect(url).toContain('times=08:30,15:30') // times 是数组 join(',') 后的字符串
    expect(url).toContain('trading_only=false')
    // 再锁定完整 URL：拼接顺序与格式一点都不能错
    expect(url).toBe('/api/schedule?action=save&enabled=true&times=08:30,15:30&trading_only=false')
    expect(opts.method).toBe('GET')
  })

  it('tradingOnly 缺省为 true', async () => {
    const fetchMock = mockFetchOk()
    vi.stubGlobal('fetch', fetchMock)

    await saveSchedule(false, ['09:00'])

    const { url } = callAt(fetchMock)
    expect(url).toContain('enabled=false')
    expect(url).toContain('trading_only=true')
  })
})

describe('POST 类接口：方法 + JSON body', () => {
  it('runSync(true) → POST /api/sync，body {hot:true}（热更新）', async () => {
    const fetchMock = mockFetchOk()
    vi.stubGlobal('fetch', fetchMock)

    await runSync(true)

    const { url, opts } = callAt(fetchMock)
    expect(url).toBe('/api/sync')
    expect(opts.method).toBe('POST')
    // body 在 http.js 里是 JSON.stringify 后的字符串，解析回来逐字段深度比对
    expect(JSON.parse(opts.body)).toEqual({ hot: true })
  })

  it('runSync(false) → body {hot:false}（停服严格模式）', async () => {
    const fetchMock = mockFetchOk()
    vi.stubGlobal('fetch', fetchMock)

    await runSync(false)

    const { url, opts } = callAt(fetchMock)
    expect(url).toBe('/api/sync')
    expect(opts.method).toBe('POST')
    expect(JSON.parse(opts.body)).toEqual({ hot: false })
  })

  it('restartContainer → POST /api/container/restart（空 body，危险操作）', async () => {
    const fetchMock = mockFetchOk()
    vi.stubGlobal('fetch', fetchMock)

    await restartContainer()

    const { url, opts } = callAt(fetchMock)
    expect(url).toBe('/api/container/restart')
    expect(opts.method).toBe('POST')
    expect(JSON.parse(opts.body)).toEqual({})
  })

  it('hkSync 原样传 codes 数组与 years 数字', async () => {
    const fetchMock = mockFetchOk()
    vi.stubGlobal('fetch', fetchMock)

    await hkSync(['00700', '09988'], 3)

    const { url, opts } = callAt(fetchMock)
    expect(url).toBe('/api/hk/sync')
    expect(opts.method).toBe('POST')
    // toEqual 深度比较：数组必须原样两个元素、years 必须是数字 3
    expect(JSON.parse(opts.body)).toEqual({ codes: ['00700', '09988'], years: 3 })
  })

  it('hkSync years 缺省为 2', async () => {
    const fetchMock = mockFetchOk()
    vi.stubGlobal('fetch', fetchMock)

    await hkSync(['00700'])

    const { opts } = callAt(fetchMock)
    expect(JSON.parse(opts.body)).toEqual({ codes: ['00700'], years: 2 })
  })
})
