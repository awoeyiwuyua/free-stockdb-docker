// api/diag.test.js — 诊断中心接口（diag.js）契约测试。
// 学习点（与 api/ops.test.js 同款套路）：
//   1) getDiag 必须是 GET：体检是只读操作，若被改成 POST 会在浏览器
//      预取 / 缓存场景下出岔子，契约测试专门锁死方法与 URL；
//   2) fetch 用 vi.stubGlobal 假实现，只读 ok / status / text 三个属性，
//      与 http.js 封装层的读取方式一一对应。

import { describe, it, expect, afterEach, vi } from 'vitest'
import { getDiag } from './diag.js'

// 造一个"长得像 Response"的对象：http.js 只读 ok / status / text 三个属性
const mockFetchOk = (body = '{}') =>
  vi.fn().mockResolvedValue({
    ok: true,
    status: 200,
    text: async () => body,
  })

const callAt = (fetchMock, idx = 0) => {
  const [url, opts] = fetchMock.mock.calls[idx]
  return { url, opts }
}

afterEach(() => {
  // 撤掉全局 fetch 假实现，避免污染其他测试文件（vi.stubGlobal 的配套清理）
  vi.unstubAllGlobals()
})

describe('getDiag：一键体检', () => {
  it('请求 GET /api/diag', async () => {
    const fetchMock = mockFetchOk()
    vi.stubGlobal('fetch', fetchMock)

    await getDiag()

    const { url, opts } = callAt(fetchMock)
    expect(url).toBe('/api/diag')
    expect(opts.method).toBe('GET')
  })

  it('GET 不带 body（体检是只读操作）', async () => {
    const fetchMock = mockFetchOk()
    vi.stubGlobal('fetch', fetchMock)

    await getDiag()

    const { opts } = callAt(fetchMock)
    expect(opts.body).toBeUndefined()
  })

  it('解析返回体：generated_at / env / checks / all_ok 四件套', async () => {
    // 后端 /api/diag 返回 {generated_at, env{python,arch,webui_version,ui_mode,
    // image_tag,started,uptime_seconds,data_dir,data_latest}, checks:[{name,
    // label,ok,note}], all_ok}；这里只抽关键叶子字段断言
    const payload = {
      generated_at: '2026-01-01T00:00:00Z',
      env: { ui_mode: 'luci', webui_version: '0.6.0' },
      checks: [{ name: 'disk', label: '磁盘', ok: true, note: 'ok' }],
      all_ok: true,
    }
    const fetchMock = mockFetchOk(JSON.stringify(payload))
    vi.stubGlobal('fetch', fetchMock)

    const data = await getDiag()

    expect(data.all_ok).toBe(true)
    expect(data.checks).toHaveLength(1)
    expect(data.checks[0].name).toBe('disk')
    expect(data.env.ui_mode).toBe('luci')
  })
})
