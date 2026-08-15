// api/data.js — 私有存储（mydb）/ 查询台接口封装。
import { getJson, postJson } from './http.js'

export const getTables = () => getJson('/api/data/tables') // mydb 表清单
export const readData = (table, key) => getJson(`/api/data/read?table=${encodeURIComponent(table)}&key=${encodeURIComponent(key ?? '')}`)
// 写入：单条 {table, key, value} 或批量 {table, items:[[k,v],...]}
export const writeData = (payload) => postJson('/api/data/write', payload)
// 查询台：直查 stockdb 任意表（t 为查询语句）
export const queryStockdb = (t) => getJson(`/api/query?t=${encodeURIComponent(t)}`)
