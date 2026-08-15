// format.test.js — 纯函数单测（Vitest）。运行：npm run test
import { describe, it, expect } from 'vitest'
import { fmtYMD, fmtMoney, fmtPct, fmtElapsed } from './format.js'
import { buildQuery } from '../api/http.js'

describe('fmtYMD', () => {
  it('8 位日期加横线', () => {
    expect(fmtYMD('20260814')).toBe('2026-08-14')
    expect(fmtYMD(20260814)).toBe('2026-08-14')
  })
  it('非 8 位数字原样返回', () => {
    expect(fmtYMD('2026-08-14')).toBe('2026-08-14')
    expect(fmtYMD(null)).toBe('')
    expect(fmtYMD(undefined)).toBe('')
  })
})

describe('fmtMoney', () => {
  it('千分位与小数位', () => {
    expect(fmtMoney(1234567.8)).toBe('1,234,567.80')
    expect(fmtMoney(100000)).toBe('100,000.00')
  })
  it('非数字返回占位', () => {
    expect(fmtMoney(NaN)).toBe('—')
    expect(fmtMoney(null)).toBe('—')
  })
})

describe('fmtPct', () => {
  it('正数带加号', () => {
    expect(fmtPct(1.2345)).toBe('+1.23%')
    expect(fmtPct(-0.5)).toBe('-0.50%')
  })
  it('非数字返回占位', () => {
    expect(fmtPct('x')).toBe('—')
  })
})

describe('fmtElapsed', () => {
  it('毫秒/秒/分三段', () => {
    expect(fmtElapsed(500)).toBe('500ms')
    expect(fmtElapsed(1234)).toBe('1.23s')
    expect(fmtElapsed(123456)).toBe('2.1min')
  })
  it('负数返回占位', () => {
    expect(fmtElapsed(-1)).toBe('—')
  })
})

describe('buildQuery', () => {
  it('剔除空值并按序拼接', () => {
    expect(buildQuery({ limit: 20, t: '', x: null })).toBe('?limit=20')
    expect(buildQuery({})).toBe('')
  })
})
