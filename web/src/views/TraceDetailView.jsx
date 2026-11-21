import React, { useEffect, useState } from 'react'
import { useParams, Link } from 'react-router-dom'
import { fetchTrace, fmtCost, fmtDur, fmtTime, fmtTokens } from '../api.js'

// Trace 详情：meta 卡 + observation 树时间线（span/GENERATION，LLM 明细可展开）+ 用例表。
export default function TraceDetailView() {
  const { id } = useParams()
  const [trace, setTrace] = useState(null)
  const [error, setError] = useState(null)

  useEffect(() => {
    let alive = true
    setTrace(null)
    setError(null)
    fetchTrace(id)
      .then((d) => alive && setTrace(d))
      .catch((e) => alive && setError(String(e)))
    return () => {
      alive = false
    }
  }, [id])

  if (error) return <div className="view"><div className="error">{error}</div></div>
  if (!trace) return <div className="view"><div className="empty">加载中…</div></div>

  return (
    <div className="view">
      <div className="crumb">
        <Link to="/traces">← Traces</Link>
      </div>
      <h1>{trace.name}</h1>

      <div className="meta-grid">
        <Meta label="开始时间" value={fmtTime(trace.start_time)} />
        <Meta label="总时长" value={fmtDur(trace.total_duration_ms)} />
        <Meta label="成本" value={fmtCost(trace.total_cost)} />
        <Meta
          label="通过率"
          value={trace.pass_rate != null ? `${trace.pass_rate}%` : '—'}
        />
        <Meta label="用例" value={`${trace.passed ?? '—'} 通过 / ${trace.failed ?? '—'} 失败 (共 ${trace.total ?? '—'})`} />
        <Meta label="类型" value={trace.source_type || '—'} />
      </div>

      <section className="card">
        <h2>Observations</h2>
        {trace.observations && trace.observations.length > 0 ? (
          <div className="obs-tree">
            {trace.observations.map((o) => (
              <ObsNode key={o.id} node={o} depth={0} />
            ))}
          </div>
        ) : (
          <div className="empty">无 observation</div>
        )}
      </section>

      <section className="card">
        <h2>用例结果 ({trace.tests?.length ?? 0})</h2>
        {trace.tests && trace.tests.length > 0 ? (
          <table className="tbl">
            <thead>
              <tr>
                <th></th>
                <th>名称</th>
                <th>请求</th>
                <th>结果</th>
                <th>时长</th>
                <th>失败说明</th>
              </tr>
            </thead>
            <tbody>
              {trace.tests.map((t, i) => (
                <TestCaseRow key={i} t={t} />
              ))}
            </tbody>
          </table>
        ) : (
          <div className="empty">无用例</div>
        )}
      </section>
    </div>
  )
}

function Meta({ label, value }) {
  return (
    <div className="meta">
      <div className="meta-label">{label}</div>
      <div className="meta-value">{value}</div>
    </div>
  )
}

// 单用例行：请求摘要 + 可展开的响应/断言明细（对齐旧 html 报告详情）。
function TestCaseRow({ t }) {
  const [open, setOpen] = useState(false)
  const req = t.request || {}
  const method = typeof req === 'object' && req ? req.method : null
  const path = typeof req === 'object' && req ? req.path : null
  const summar = [method, path].filter(Boolean).join(' ') || '—'
  const assertList = Array.isArray(t.assertions) ? t.assertions : []
  const nonPassAssert = assertList.filter((a) => a && a.passed === false)
  const failed = String(t.result).toLowerCase() === 'fail'
  const cls = failed ? 'pill pill-fail' : 'pill pill-pass'
  const resp = t.response || {}
  return (
    <>
      <tr>
        <td>
          <span className="chevron" onClick={() => setOpen(!open)}>
            {open ? '▾' : '▸'}
          </span>
        </td>
        <td>{t.name}</td>
        <td className="muted" title={pretty(req)}>{summar}</td>
        <td><span className={cls}>{t.result}</span></td>
        <td>{fmtDur(t.duration_ms)}</td>
        <td className="muted">{t.failure || '—'}</td>
      </tr>
      {open && (
        <tr>
          <td></td>
          <td colSpan="5">
            <div className="tcase-detail">
              <div className="gen-block">
                <div className="gen-title">请求</div>
                <pre>{pretty(req)}</pre>
              </div>
              <div className="gen-block">
                <div className="gen-title">响应（状态码 {resp.status_code ?? '—'}）</div>
                <pre>{pretty(resp)}</pre>
              </div>
              <div className="gen-block">
                <div className="gen-title">
                  断言（{assertList.length} 条{nonPassAssert.length ? `，失败 ${nonPassAssert.length}` : ''}）
                </div>
                {assertList.length === 0 ? (
                  <div className="muted">无断言</div>
                ) : (
                  <table className="tbl tbl-inline">
                    <thead>
                      <tr>
                        <th>类型</th>
                        <th>逻辑</th>
                        <th>期望</th>
                        <th>实际</th>
                        <th>结果</th>
                      </tr>
                    </thead>
                    <tbody>
                      {assertList.map((a, i) => (
                        <tr key={i}>
                          <td>{a.type}</td>
                          <td>{pretty(a.logic)}</td>
                          <td>{pretty(a.expected)}</td>
                          <td>{pretty(a.actual)}</td>
                          <td>
                            <span className={`pill ${a.passed === false ? 'pill-fail' : 'pill-pass'}`}>
                              {a.passed === false ? '失败' : '通过'}
                            </span>
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                )}
              </div>
            </div>
          </td>
        </tr>
      )}
    </>
  )
}

// 递归渲染 observation 树节点；GENERATION 展开显示 LLM 明细。
function ObsNode({ node, depth }) {
  const [open, setOpen] = useState(false)
  const isGen = node.type === 'GENERATION'
  const hasChildren = node.children && node.children.length > 0

  return (
    <div className="obs-node" style={{ marginLeft: depth * 20 }}>
      <div
        className={`obs-row ${isGen ? 'gen' : ''}`}
        onClick={() => (isGen || hasChildren) && setOpen(!open)}
      >
        <span className="chevron">{isGen || hasChildren ? (open ? '▾' : '▸') : '·'}</span>
        <span className={`obs-type obs-type-${String(node.type).toLowerCase()}`}>
          {node.type}
        </span>
        <span className="obs-name">{node.name || node.id}</span>
        {node.model && <span className="obs-model">{node.model}</span>}
        {node.status && (
          <span className={`obs-status status-${String(node.status).toLowerCase()}`}>
            {node.status}
          </span>
        )}
        <span className="obs-meta muted">
          {fmtDur(node.latency_ms)} · {fmtTokens(node.prompt_tokens)}→
          {fmtTokens(node.completion_tokens)} tok · {fmtCost(node.cost)}
        </span>
      </div>

      {open && isGen && node.has_input && (
        <div className="gen-detail">
          {node.input != null && (
            <div className="gen-block">
              <div className="gen-title">Input</div>
              <pre>{pretty(node.input)}</pre>
            </div>
          )}
          {node.output != null && (
            <div className="gen-block">
              <div className="gen-title">Output</div>
              <pre>{pretty(node.output)}</pre>
            </div>
          )}
        </div>
      )}

      {hasChildren &&
        node.children.map((c) => <ObsNode key={c.id} node={c} depth={depth + 1} />)}
    </div>
  )
}

function pretty(v) {
  if (typeof v === 'string') return v
  try {
    return JSON.stringify(v, null, 2)
  } catch {
    return String(v)
  }
}