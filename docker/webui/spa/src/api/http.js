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

export async function request(path, { method = 'GET', body, timeoutMs = 20000, signal } = {}) {
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
