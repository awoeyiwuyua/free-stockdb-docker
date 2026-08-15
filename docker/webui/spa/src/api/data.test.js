// api/data.test.js — 私有存储/查询台接口（data.js）契约测试。
// 学习点：
//   1) readData 的 table/key 必须 encodeURIComponent：表名/键里可能有中文、空格、&、= 等特殊字符，
//      不编码会让后端收到被拆坏的 query —— 所以测试专门用危险字符验证编码行为；
//   2) writeData 是整包透传：payload 原样进 body，序列化→解析回来必须逐字段一致。

import { describe, it, expect, afterEach, vi } from 'vitest'
import { getTables, readData, writeData, queryStockdb } from './data.js'

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
  it('getTables → GET /api/data/tables', async () => {
    const fetchMock = mockFetchOk()
    vi.stubGlobal('fetch', fetchMock)

    await getTables()

    const { url, opts } = callAt(fetchMock)
    expect(url).toBe('/api/data/tables')
    expect(opts.method).toBe('GET')
  })

  it('readData 的 table/key 都做了 URL 编码（含中文/空格/&/= 危险字符）', async () => {
    const fetchMock = mockFetchOk()
    vi.stubGlobal('fetch', fetchMock)

    await readData('我的 表', 'a&b=c')

    const { url, opts } = callAt(fetchMock)
    // 期望值也用 encodeURIComponent 生成：契约就是"与标准编码结果逐字符一致"
    expect(url).toBe(`/api/data/read?table=${encodeURIComponent('我的 表')}&key=${encodeURIComponent('a&b=c')}`)
    // 顺带验证编码确实是 %XX 形式："我" 的 UTF-8 编码以 %E6%88%91 开头
    expect(url).toContain('%E6%88%91')
    // 裸 & 绝不能出现在 query 里（否则会被解析成参数分隔符，把 key 拆断）
    expect(url).not.toContain('a&b=c')
    expect(opts.method).toBe('GET')
  })

  it('readData 缺省 key 时编码为空串（key= 保留占位）', async () => {
    const fetchMock = mockFetchOk()
    vi.stubGlobal('fetch', fetchMock)

    await readData('tbl')

    const { url } = callAt(fetchMock)
    expect(url).toBe('/api/data/read?table=tbl&key=')
  })

  it('queryStockdb → GET /api/query，t 已编码', async () => {
    const fetchMock = mockFetchOk()
    vi.stubGlobal('fetch', fetchMock)

    await queryStockdb('select * from bars limit 5')

    const { url, opts } = callAt(fetchMock)
    expect(url).toBe(`/api/query?t=${encodeURIComponent('select * from bars limit 5')}`)
    expect(opts.method).toBe('GET')
  })
})

describe('writeData：POST 整包透传', () => {
  it('单条 payload 原样 JSON 序列化进 body', async () => {
    const fetchMock = mockFetchOk()
    vi.stubGlobal('fetch', fetchMock)

    const payload = { table: 'bars', key: '600000.SH', value: { close: 9.87 } }
    await writeData(payload)

    const { url, opts } = callAt(fetchMock)
    expect(url).toBe('/api/data/write')
    expect(opts.method).toBe('POST')
    // toEqual 深度比较：解析回来的对象与原 payload 完全一致（value 里的嵌套对象也要在）
    expect(JSON.parse(opts.body)).toEqual(payload)
  })
})
