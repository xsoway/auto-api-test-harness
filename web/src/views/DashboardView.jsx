import React, { useEffect, useState } from 'react'
import { fetchMetrics, fmtCost, fmtTokens } from '../api.js'

// 成本看板：总成本/token/成功率 + 模型分布 + 类型分布。
export default function DashboardView() {
  const [m, setM] = useState(null)
  const [error, setError] = useState(null)

  useEffect(() => {
    let alive = true
    fetchMetrics()
      .then((d) => alive && setM(d))
      .catch((e) => alive && setError(String(e)))
    return () => {
      alive = false
    }
  }, [])

  if (error) return <div className="view"><div className="error">{error}</div></div>
  if (!m) return <div className="view"><div className="empty">加载中…</div></div>

  const byModel = m.by_model || {}
  const byType = m.by_type || {}

  return (
    <div className="view">
      <h1>成本看板</h1>

      <div className="stat-grid">
        <Stat label="总成本" value={fmtCost(m.total_cost)} />
        <Stat label="总 Token" value={fmtTokens(m.total_tokens)} />
        <Stat label="成功率" value={m.success_rate != null ? `${m.success_rate}%` : '—'} />
        <Stat label="Runs" value={m.runs} />
        <Stat label="用例总数" value={m.total_tests} />
      </div>
      <div className="muted" style={{ marginTop: 4 }}>
        成本为 0：未配置模型价格表（本地 sglang 只记账 latency/token，不计费）。
      </div>

      <section className="card">
        <h2>按模型</h2>
        <ModelTable rows={byModel} />
      </section>

      <section className="card">
        <h2>按类型</h2>
        <table className="tbl">
          <thead>
            <tr>
              <th>类型</th>
              <th>请求数</th>
              <th>Prompt Tokens</th>
              <th>Completion Tokens</th>
              <th>成本</th>
            </tr>
          </thead>
          <tbody>
            {Object.entries(byType).map(([t, v]) => (
              <tr key={t}>
                <td><span className={`pill pill-${t.toLowerCase()}`}>{t}</span></td>
                <td>{v.requests}</td>
                <td>{fmtTokens(v.prompt_tokens)}</td>
                <td>{fmtTokens(v.completion_tokens)}</td>
                <td>{fmtCost(v.cost)}</td>
              </tr>
            ))}
            {Object.keys(byType).length === 0 && (
              <tr><td colSpan="5" className="empty">无数据</td></tr>
            )}
          </tbody>
        </table>
      </section>
    </div>
  )
}

function Stat({ label, value }) {
  return (
    <div className="stat">
      <div className="stat-label">{label}</div>
      <div className="stat-value">{value}</div>
    </div>
  )
}

function ModelTable({ rows }) {
  const list = Object.entries(rows)
  if (list.length === 0) return <div className="empty">无数据</div>
  const total = list.reduce((s, [, v]) => s + v.cost, 0)
  return (
    <table className="tbl">
      <thead>
        <tr>
          <th>模型</th>
          <th>请求数</th>
          <th>Prompt Tokens</th>
          <th>Completion Tokens</th>
          <th>成本</th>
          <th>占比</th>
        </tr>
      </thead>
      <tbody>
        {list.map(([model, v]) => (
          <tr key={model}>
            <td>{model}</td>
            <td>{v.requests}</td>
            <td>{fmtTokens(v.prompt_tokens)}</td>
            <td>{fmtTokens(v.completion_tokens)}</td>
            <td>{fmtCost(v.cost)}</td>
            <td>
              <div className="bar">
                <div
                  className="bar-fill"
                  style={{ width: `${total ? (v.cost / total) * 100 : 0}%` }}
                />
              </div>
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  )
}