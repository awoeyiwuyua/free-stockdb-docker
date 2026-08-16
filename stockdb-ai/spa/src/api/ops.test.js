// api/ops.test.js — 运维域接口（ops.js）契约测试。
// 学习点：
//   1) clearAlerts 必须是 POST：清空告警不可逆，若用 GET 会被浏览器缓存/预取，
//      可能造成"打开页面就误清空"；契约测试专门锁死方法与 URL；
//   2) getAlerts 的 limit 必须进 query 参数，与后端 handler 读取方式一一对应。

import { describe, it, expect, afterEach, vi } from 'vitest'
import { getAlerts, clearAlerts, getMcpStats, getMcpCalls, getVersion } from './ops.js'

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
  // it.each 表格化：一行 = 一个用例
  it.each([
    ['getAlerts(200)', () => getAlerts(200), '/api/alerts?limit=200'],
    ['getMcpStats', getMcpStats, '/api/mcp/stats'],
    ['getMcpCalls(50)', () => getMcpCalls(50), '/api/mcp/calls?limit=50'],
    ['getVersion', getVersion, '/api/version'],
  ])('%s 请求 %s', async (_label, fn, expectUrl) => {
    const fetchMock = mockFetchOk()
    vi.stubGlobal('fetch', fetchMock)

    await fn()

    const { url, opts } = callAt(fetchMock)
    expect(url).toBe(expectUrl)
    expect(opts.method).toBe('GET')
  })

  it('getAlerts 缺省 limit=200', async () => {
    const fetchMock = mockFetchOk()
    vi.stubGlobal('fetch', fetchMock)

    await getAlerts()

    const { url } = callAt(fetchMock)
    expect(url).toBe('/api/alerts?limit=200')
  })
})

describe('clearAlerts：清空告警必须 POST', () => {
  it('方法 + URL + 空 body 三元组', async () => {
    const fetchMock = mockFetchOk()
    vi.stubGlobal('fetch', fetchMock)

    await clearAlerts()

    const { url, opts } = callAt(fetchMock)
    expect(url).toBe('/api/alerts/clear')
    expect(opts.method).toBe('POST')
    expect(JSON.parse(opts.body)).toEqual({})
  })
})
