import React, { useEffect, useState } from 'react'
import { Routes, Route, NavLink, Link, useLocation } from 'react-router-dom'
import { HashRouter } from 'react-router-dom'
import TracesView from './views/TracesView.jsx'
import TraceDetailView from './views/TraceDetailView.jsx'
import AnalysisView from './views/AnalysisView.jsx'
import DashboardView from './views/DashboardView.jsx'

function Layout() {
  const loc = useLocation()
  return (
    <div className="app">
      <aside className="sidebar">
        <div className="brand">
          <span className="brand-dot" />
          Local Obs
        </div>
        <nav>
          <NavLink to="/dashboard" className="nav-link">
            📊 成本看板
          </NavLink>
          <NavLink to="/traces" className="nav-link">
            🕸️ Traces
          </NavLink>
          <NavLink to="/analysis" className="nav-link">
            📈 结果分析
          </NavLink>
        </nav>
        <div className="sidebar-foot">Langfuse-style · local</div>
      </aside>
      <main className="content" key={loc.key}>
        <Routes location={loc}>
          <Route path="/" element={<TracesView />} />
          <Route path="/traces" element={<TracesView />} />
          <Route path="/traces/:id" element={<TraceDetailView />} />
          <Route path="/analysis" element={<AnalysisView />} />
          <Route path="/dashboard" element={<DashboardView />} />
          <Route path="*" element={<NotFound />} />
        </Routes>
      </main>
    </div>
  )
}

function NotFound() {
  return <div className="empty">Page not found.</div>
}

export default function App() {
  return (
    <HashRouter>
      <Layout />
    </HashRouter>
  )
}