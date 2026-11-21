import React, { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { fetchTraces, fmtCost, fmtDur, fmtTime } from '../api.js'

// Traces 列表：以表格展示运行聚合，点击进入详情。
export default function TracesView() {
  const [data, setData] = useState(null)
  const [error, setError] = useState(null)
  const [page, setPage] = useState(1)
  const [name, setName] = useState('')
  const LIMIT = 20

  useEffect(() => {
    let alive = true
    setData(null)
    setError(null)
    fetchTraces(page, LIMIT, name)
      .then((d) => alive && setData(d))
      .catch((e) => alive && setError(String(e)))
    return () => {
      alive = false
    }
  }, [page, name])

  const handleSearch = (e) => {
    e.preventDefault()
    setPage(1)
  }

  return (
    <div className="view">
      <div className="view-head">
        <h1>Traces</h1>
        <form className="search" onSubmit={handleSearch}>
          <input
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="按名称过滤…"
          />
          <button type="submit">搜索</button>
        </form>
      </div>

      {error && <div className="error">{error}</div>}
      {!data && !error && <div className="empty">加载中…</div>}

      {data && (
        <>
          <table className="tbl">
            <thead>
              <tr>
                <th>名称</th>
                <th>类型</th>
                <th>开始时间</th>
                <th>时长</th>
                <th>成本</th>
                <th>通过率</th>
                <th>结果分布</th>
              </tr>
            </thead>
            <tbody>
              {data.data.map((r) => (
                <tr key={r.id}>
                  <td>
                    <Link className="trace-link" to={`/traces/${encodeURIComponent(r.id)}`}>
                      {r.name}
                    </Link>
                  </td>
                  <td className="muted">{r.source_type || '—'}</td>
                  <td>{fmtTime(r.start_time)}</td>
                  <td>{fmtDur(r.total_duration_ms)}</td>
                  <td>{fmtCost(r.total_cost)}</td>
                  <td>{r.pass_rate != null ? `${r.pass_rate}%` : '—'}</td>
                  <td>
                    <ResultDist dist={r.result_dist} />
                  </td>
                </tr>
              ))}
              {data.data.length === 0 && (
                <tr>
                  <td colSpan="7" className="empty">
                    无数据（先运行 ingest/seed）
                  </td>
                </tr>
              )}
            </tbody>
          </table>
          <div className="pager">
            <button disabled={page <= 1} onClick={() => setPage((p) => p - 1)}>
              ‹
            </button>
            <span>
              第 {page} 页 · 共 {data.total} 条
            </span>
            <button
              disabled={page * LIMIT >= data.total}
              onClick={() => setPage((p) => p + 1)}
            >
              ›
            </button>
          </div>
        </>
      )}
    </div>
  )
}

function ResultDist({ dist }) {
  if (!dist) return <span className="muted">—</span>
  const parts = Object.entries(dist).filter(([, n]) => n > 0)
  if (parts.length === 0) return <span className="muted">—</span>
  return (
    <span className="dist">
      {parts.map(([k, n]) => (
        <span key={k} className={`pill pill-${String(k).toLowerCase()}`}>
          {k} {n}
        </span>
      ))}
    </span>
  )
}