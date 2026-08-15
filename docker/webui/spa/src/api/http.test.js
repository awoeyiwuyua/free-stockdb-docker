// http.test.js — 在途去重测试：并发同请求共享 Promise，防切页请求风暴。
import { describe, it, expect, vi, afterEach } from 'vitest'
import { request, getJson, buildQuery } from './http.js'

afterEach(() => {
  vi.unstubAllGlobals()
})

const okResp = (payload) => ({
  ok: true,
  status: 200,
  text: async () => JSON.stringify(payload),
})

describe('request 在途去重', () => {
  it('并发同 URL 共享一次 fetch，结果一致', async () => {
    const fetchMock = vi.fn(async () => okResp({ v: 1 }))
    vi.stubGlobal('fetch', fetchMock)
    const [a, b] = await Promise.all([getJson('/api/overview'), getJson('/api/overview')])
    expect(fetchMock).toHaveBeenCalledTimes(1)
    expect(a).toEqual({ v: 1 })
    expect(b).toEqual({ v: 1 })
  })

  it('完成后再次请求会重新发起（去重表已清理）', async () => {
    const fetchMock = vi.fn(async () => okResp({ v: 2 }))
    vi.stubGlobal('fetch', fetchMock)
    await getJson('/api/health')
    await getJson('/api/health')
    expect(fetchMock).toHaveBeenCalledTimes(2)
  })

  it('dedup=false 时并发不共享', async () => {
    const fetchMock = vi.fn(async () => okResp({ v: 3 }))
    vi.stubGlobal('fetch', fetchMock)
    await Promise.all([
      request('/api/health', { method: 'GET', dedup: false }),
      request('/api/health', { method: 'GET', dedup: false }),
    ])
    expect(fetchMock).toHaveBeenCalledTimes(2)
  })

  it('buildQuery 剔除空值', () => {
    expect(buildQuery({ a: 1, b: '', c: undefined })).toBe('?a=1')
  })
})
