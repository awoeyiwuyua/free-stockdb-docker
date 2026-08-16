// api/diag.js — 诊断中心接口封装（/api/diag 一键体检 + 环境信息）。
import { getJson } from './http.js'

export const getDiag = () => getJson('/api/diag')
