// api/paper.test.js — 模拟盘接口（paper.js）契约测试。
// 学习点：
//   1) 危险操作（pause/apikey）与普通查询一样走假 fetch，只断言"请求长什么样"，
//      不碰任何真实副作用；二次确认逻辑属于页面层，不在这里测；
//   2) apikey 是敏感字段：本测试只验证它进了请求 body，绝不涉及回显/缓存逻辑。

import { describe, it, expect, afterEach, vi } from 'vitest'
import {
  getPaperStatus,
  getPaperOverview,
  getSnapshots,
  getDecisions,
  getOrders,
  getEvents,
  getAudit,
  getSignalStatus,
  setPause,
  runNow,
  saveApikey,
  checkConnectivity,
} from './paper.js'

// 造一个"长得像 Response"的对象：http.js 只读 ok / status / text 三个属性
const mockFetchOk = () =>
  vi.fn().mockResolvedValue({
    ok: true,
    status: 200,
    text: async () => '{}',
  })

const callAt = (fetchMock, idx = 0) => {
  const [url, opts] = fetchMock.mock.calls[idx]
  return { url, opts }
}

afterEach(() => {
  // 撤掉全局 fetch 假实现，避免污染其他测试文件（vi.stubGlobal 的配套清理）
  vi.unstubAllGlobals()
})

describe('GET 类接口：URL 与方法', () => {
  // it.each 表格化：一行 = 一个用例（含 limit 参数的也一并锁定 query）
  it.each([
    ['getPaperStatus', getPaperStatus, '/api/paper/status'],
    ['getPaperOverview', getPaperOverview, '/api/paper/overview'],
    ['getSnapshots(60)', () => getSnapshots(60), '/api/paper/snapshot?limit=60'],
    ['getDecisions(30)', () => getDecisions(30), '/api/paper/decisions?limit=30'],
    ['getOrders(500)', () => getOrders(500), '/api/paper/orders?limit=500'],
    ['getEvents(200)', () => getEvents(200), '/api/paper/events?limit=200'],
    ['getAudit', getAudit, '/api/paper/audit'],
    ['getSignalStatus', getSignalStatus, '/api/paper/signal-status'],
  ])('%s 请求 %s', async (_label, fn, expectUrl) => {
    const fetchMock = mockFetchOk()
    vi.stubGlobal('fetch', fetchMock)

    await fn()

    const { url, opts } = callAt(fetchMock)
    expect(url).toBe(expectUrl)
    expect(opts.method).toBe('GET')
  })

  it('getSnapshots 缺省 limit=60（净值曲线默认取最近 60 点）', async () => {
    const fetchMock = mockFetchOk()
    vi.stubGlobal('fetch', fetchMock)

    await getSnapshots()

    const { url } = callAt(fetchMock)
    expect(url).toBe('/api/paper/snapshot?limit=60')
  })
})

describe('POST 类接口：方法 + JSON body', () => {
  it('setPause(true) → POST /api/paper/pause，body {enabled:true}（暂停）', async () => {
    const fetchMock = mockFetchOk()
    vi.stubGlobal('fetch', fetchMock)

    await setPause(true)

    const { url, opts } = callAt(fetchMock)
    expect(url).toBe('/api/paper/pause')
    expect(opts.method).toBe('POST')
    expect(JSON.parse(opts.body)).toEqual({ enabled: true })
  })

  it('setPause(false) → body {enabled:false}（恢复交易）', async () => {
    const fetchMock = mockFetchOk()
    vi.stubGlobal('fetch', fetchMock)

    await setPause(false)

    const { opts } = callAt(fetchMock)
    expect(JSON.parse(opts.body)).toEqual({ enabled: false })
  })

  it('runNow(14:50) → POST /api/paper/run-now，body {timepoint:"14:50"}', async () => {
    const fetchMock = mockFetchOk()
    vi.stubGlobal('fetch', fetchMock)

    await runNow('14:50')

    const { url, opts } = callAt(fetchMock)
    expect(url).toBe('/api/paper/run-now')
    expect(opts.method).toBe('POST')
    expect(JSON.parse(opts.body)).toEqual({ timepoint: '14:50' })
  })

  it('saveApikey(k) → POST /api/paper/apikey，body {apikey:k}', async () => {
    const fetchMock = mockFetchOk()
    vi.stubGlobal('fetch', fetchMock)

    await saveApikey('sk-test-123')

    const { url, opts } = callAt(fetchMock)
    expect(url).toBe('/api/paper/apikey')
    expect(opts.method).toBe('POST')
    expect(JSON.parse(opts.body)).toEqual({ apikey: 'sk-test-123' })
  })

  it('checkConnectivity → POST /api/paper/connectivity（空 body）', async () => {
    const fetchMock = mockFetchOk()
    vi.stubGlobal('fetch', fetchMock)

    await checkConnectivity()

    const { url, opts } = callAt(fetchMock)
    expect(url).toBe('/api/paper/connectivity')
    expect(opts.method).toBe('POST')
    expect(JSON.parse(opts.body)).toEqual({})
  })
})
