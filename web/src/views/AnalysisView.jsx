import React, { useEffect, useState } from 'react'
import { fetchAnalysis } from '../api.js'

// 测试结果分析：状态分布 / flaky / 用例通过率排名 / 耗时与通过率趋势。
// 数据来自 store.analysis()（跨全部 run 聚合，focus 测试结果而非 LLM 成本）。
export default function AnalysisView() {
  const [a, setA] = useState(null)
  const [error, setError] = useState(null)

  useEffect(() => {
    let alive = true
    fetchAnalysis()
      .then((d) => alive && setA(d))
      .catch((e) => alive && setError(String(e)))
    return () => {
      alive = false
    }
  }, [])

  if (error) return <div className="view"><div className="error">{error}</div></div>
  if (!a) return <div className="view"><div className="empty">加载中…</div></div>

  const stateDist = a.state_dist || {}
  const flaky = a.flaky || {}
  const ranking = a.case_ranking || []
  const trend = a.duration_trend || []

  const flakyPct = flaky.runs_total
    ? ((flaky.runs_affected || 0) / flaky.runs_total) * 100
    : 0
  const maxStatus = Math.max(1, ...Object.values(stateDist))

  return (
    <div className="view">
      <h1>结果分析</h1>

      <div className="stat-grid">
        <Stat label="Run 总数" value={flaky.runs_total ?? '—'} />
        <Stat label="Flaky 用例" value={flaky.total ?? 0} />
        <Stat
          label="Flaky 影响 Run"
          value={`${flaky.runs_affected ?? 0} / ${flaky.runs_total ?? 0} (${flakyPct.toFixed(0)}%)`}
        />
        <Stat label="状态码种类" value={Object.keys(stateDist).length} />
      </div>

      <section className="card">
        <h2>状态码分布</h2>
        {Object.keys(stateDist).length === 0 ? (
          <div className="empty">无状态码数据</div>
        ) : (
          <table className="tbl">
            <thead>
              <tr><th>状态码</th><th>次数</th><th>占比</th></tr>
            </thead>
            <tbody>
              {Object.entries(stateDist)
                .sort((a, b) => b[1] - a[1])
                .map(([code, n]) => (
                  <tr key={code}>
                    <td><span className={`pill pill-${code === '200' ? 'pass' : 'fail'}`}>{code}</span></td>
                    <td>{n}</td>
                    <td>
                      <div className="bar">
                        <div
                          className="bar-fill bar-fill-status"
                          style={{ width: `${(n / maxStatus) * 100}%` }}
                        />
                      </div>
                    </td>
                  </tr>
                ))}
            </tbody>
          </table>
        )}
      </section>

      <section className="card">
        <h2>用例通过率排名（跨 run，最不稳定在前）</h2>
        {ranking.length === 0 ? (
          <div className="empty">无跨 run 用例数据</div>
        ) : (
          <table className="tbl">
            <thead>
              <tr><th>用例</th><th>总次</th><th>通过</th><th>失败</th><th>通过率</th><th>平均耗时</th></tr>
            </thead>
            <tbody>
              {ranking.map((c) => (
                <tr key={c.name}>
                  <td>{c.name}</td>
                  <td>{c.total}</td>
                  <td>{c.passed}</td>
                  <td>{c.failed}</td>
                  <td>
                    <span className={`pill ${c.pass_rate < 100 ? 'pill-fail' : 'pill-pass'}`}>
                      {c.pass_rate}%
                    </span>
                  </td>
                  <td>{fmtMs(c.avg_duration_ms)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>

      <section className="card">
        <h2>通过率趋势（按 Run 时间）</h2>
        {trend.length === 0 ? (
          <div className="empty">无趋势数据</div>
        ) : (
          <TrendChart trend={trend} />
        )}
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

function fmtMs(ms) {
  if (ms == null || isNaN(ms)) return '—'
  if (ms >= 1000) return `${(ms / 1000).toFixed(2)}s`
  return `${Math.round(ms)}ms`
}

// 通过率/flaky 折线：纯 CSS 柱状（复用 .bar），时间升序。
function TrendChart({ trend }) {
  const maxRate = 100
  return (
    <div className="trend">
      <div className="trend-row trend-label">
        {trend.map((t, i) => (
          <div key={i} className="trend-cell">
            <span className="trend-time">{t.time ? t.time.slice(5, 16) : i}</span>
            <div className="trend-bars">
              <div className="trend-bar-wrap">
                <div
                  className="trend-bar trend-bar-rate"
                  style={{ height: `${(t.pass_rate / maxRate) * 100}%` }}
                  title={`${t.time}\n通过率 ${t.pass_rate}%`}
                />
              </div>
            </div>
          </div>
        ))}
      </div>
      <div className="muted">
        蓝色柱高 = 通过率（%），底部为 Run 时间（月-日 时:分）
      </div>
    </div>
  )
}