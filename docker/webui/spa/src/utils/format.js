// format.js — 展示格式化纯函数（无 DOM 依赖，可单测）。
// 学习点：纯函数 = 同样的输入永远得到同样的输出，测试最好写。

// 20260814 → 2026-08-14（非 8 位数字原样返回）
export function fmtYMD(v) {
  const s = String(v ?? '')
  return /^\d{8}$/.test(s) ? `${s.slice(0, 4)}-${s.slice(4, 6)}-${s.slice(6, 8)}` : s
}

// 1234567.8 → '1,234,567.80'（千分位，固定小数位）
export function fmtMoney(v, digits = 2) {
  if (v === null || v === undefined || v === '') return '—'
  const n = Number(v)
  if (!Number.isFinite(n)) return '—'
  return n.toLocaleString('zh-CN', {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  })
}

// 1.2345 → '+1.23%'
export function fmtPct(v, digits = 2) {
  if (v === null || v === undefined || v === '') return '—'
  const n = Number(v)
  if (!Number.isFinite(n)) return '—'
  const sign = n > 0 ? '+' : ''
  return `${sign}${n.toFixed(digits)}%`
}

// 1234 → '1.23s'；123456 → '2.06min'
export function fmtElapsed(ms) {
  if (ms === null || ms === undefined || ms === '') return '—'
  const n = Number(ms)
  if (!Number.isFinite(n) || n < 0) return '—'
  if (n < 1000) return `${Math.round(n)}ms`
  if (n < 60000) return `${(n / 1000).toFixed(2)}s`
  return `${(n / 60000).toFixed(1)}min`
}
