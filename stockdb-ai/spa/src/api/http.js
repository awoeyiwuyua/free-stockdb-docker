// http.js — 统一 fetch 封装：超时、JSON 解析、非 2xx 抛 ApiError。
// 学习点：AbortController 超时；后端 8 错误码约定（error 字段）直接透传给调用方。

export class ApiError extends Error {
  constructor(status, data, url) {
    super(typeof data?.error === 'string' ? data.error : `HTTP ${status}`)
    this.name = 'ApiError'
    this.status = status
    this.data = data
    this.url = url
  }
}

// 把 {a:1,b:undefined,c:'x'} 编成 '?a=1&c=x'（空值自动剔除）
export function buildQuery(params = {}) {
  const parts = []
  for (const [k, v] of Object.entries(params)) {
    if (v === undefined || v === null || v === '') continue
    parts.push(`${encodeURIComponent(k)}=${encodeURIComponent(v)}`)
  }
  return parts.length ? `?${parts.join('&')}` : ''
}

// 在途请求表（key → Promise）：同请求并发共享，防切页风暴
const _inflight = new Map()

export async function request(path, { method = 'GET', body, timeoutMs = 20000, signal, dedup = true } = {}) {
  // 在途去重：相同 method+URL+body 的并发请求共享同一个 Promise。
  // 多标签/快速切页会短时间重复发起同一批接口，去重后后端只收到一路，
  // 其余等待同一结果——避免请求风暴把 ThreadingHTTPServer 拖死。
  const dedupKey = dedup ? `${method} ${path} ${body !== undefined ? JSON.stringify(body) : ''}` : null
  if (dedupKey && _inflight.has(dedupKey)) {
    return _inflight.get(dedupKey)
  }
  const p = _doRequest(path, { method, body, timeoutMs, signal }).finally(() => {
    if (dedupKey) _inflight.delete(dedupKey)
  })
  if (dedupKey) _inflight.set(dedupKey, p)
  return p
}

async function _doRequest(path, { method, body, timeoutMs, signal }) {
  const ctrl = new AbortController()
  const timer = setTimeout(() => ctrl.abort(new Error('timeout')), timeoutMs)
  try {
    const resp = await fetch(path, {
      method,
      headers: body !== undefined ? { 'Content-Type': 'application/json' } : undefined,
      body: body !== undefined ? JSON.stringify(body) : undefined,
      signal: signal || ctrl.signal,
    })
    const text = await resp.text()
    let data = null
    try {
      data = text ? JSON.parse(text) : null
    } catch {
      data = null
    }
    if (!resp.ok) throw new ApiError(resp.status, data ?? {}, path)
    return data
  } finally {
    clearTimeout(timer)
  }
}

export const getJson = (path, opts = {}) => request(path, { ...opts, method: 'GET' })
export const postJson = (path, body, opts = {}) => request(path, { ...opts, method: 'POST', body })
