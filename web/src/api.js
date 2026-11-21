// 后端 API 访问封装。
// 生产（由 serve() 托管）请求同源 /api/*；dev 走 VITE_API_BASE 环境变量（或 vite proxy）。
const API_BASE =
  import.meta.env.VITE_API_BASE || (import.meta.env.DEV ? '/api' : '/api')

async function get(path) {
  const resp = await fetch(`${API_BASE}${path}`)
  if (!resp.ok) {
    const body = await resp.text().catch(() => '')
    throw new Error(`GET ${path} → ${resp.status} ${body}`)
  }
  return resp.json()
}

export function fetchMetrics() {
  return get('/metrics')
}

export function fetchTraces(page = 1, limit = 20, name = '') {
  const q = new URLSearchParams({ page, limit })
  if (name) q.set('name', name)
  return get(`/traces?${q.toString()}`)
}

export function fetchTrace(id) {
  return get(`/traces/${encodeURIComponent(id)}`)
}

export function fetchObservation(id) {
  return get(`/observations/${encodeURIComponent(id)}`)
}

export function fetchAnalysis() {
  return get('/analysis')
}

// 小工具：格式化数值。
export function fmtCost(c) {
  if (c == null || isNaN(c)) return '—'
  return `$${Number(c).toFixed(4)}`
}

export function fmtTokens(n) {
  if (n == null || isNaN(n)) return '—'
  return Number(n).toLocaleString()
}

export function fmtDur(ms) {
  if (ms == null || isNaN(ms)) return '—'
  if (ms >= 1000) return `${(ms / 1000).toFixed(2)}s`
  return `${Math.round(ms)}ms`
}

export function fmtTime(iso) {
  if (!iso) return '—'
  const d = new Date(iso)
  if (isNaN(d.getTime())) return String(iso)
  return d.toLocaleString('zh-CN', { hour12: false })
}