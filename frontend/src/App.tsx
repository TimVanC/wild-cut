import React, { useEffect, useState } from 'react'
import { NavLink, Route, Routes, useParams, Navigate } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { API_BASE, api, getToken, setToken } from './api'
import Projects from './pages/Projects'
import NewProject from './pages/NewProject'
import Footage from './pages/Footage'
import Music from './pages/Music'
import Editor from './pages/Editor'
import Showdown from './pages/Showdown'
import Documentary from './pages/Documentary'
import { useEvents } from './hooks/useEvents'

function ProjectShell() {
  const { id } = useParams()
  const snap = useEvents(id)
  const project = useQuery({ queryKey: ['project', id], queryFn: () => api.project(id!), enabled: !!id })
  const p = project.data
  const isShowdown = p?.style === 'showdown'
  const isDoc = !!p?.options?.documentary
  const tab = (to: string, label: string) => (
    <NavLink to={`/p/${id}/${to}`} className={({ isActive }) => `px-3 py-1.5 rounded-md text-sm ${isActive ? 'bg-[#23232b] text-white' : 'muted hover:text-white'}`}>{label}</NavLink>
  )
  const status = snap?.project.status ?? p?.status
  const running = snap?.jobs.find(j => j.status === 'running' || j.status === 'queued')
  return (
    <div className="h-full flex flex-col">
      <header className="flex items-center gap-3 px-4 py-2 border-b" style={{ borderColor: 'var(--line)', background: 'var(--panel)' }}>
        <NavLink to="/" className="font-semibold tracking-wide text-[15px]">WILD<span style={{ color: 'var(--accent)' }}>CUT</span></NavLink>
        <span className="muted">/</span>
        <span className="font-medium truncate max-w-[260px]">{p?.name ?? '…'}</span>
        {p && <span className="pill">{p.style} · {p.aspect} · {p.mode === 'music' ? 'music-synced' : 'visual peaks'}</span>}
        <nav className="flex items-center gap-1 ml-4">
          {isDoc && tab('documentary', 'Documentary')}
          {!isShowdown && tab('footage', 'Footage')}
          {isShowdown && tab('showdown', 'Showdown')}
          {p?.mode === 'music' && tab('music', 'Music')}
          {tab('editor', 'Editor')}
        </nav>
        <div className="ml-auto flex items-center gap-3 text-xs">
          {running && <span className="pill pill-warn">{running.kind} {Math.round((running.progress ?? 0) * 100)}% {running.message ? `· ${running.message}` : ''}</span>}
          {!running && status && <span className={`pill ${status === 'error' ? 'pill-err' : status === 'exported' ? 'pill-ok' : ''}`}>{status}</span>}
          {snap?.project.message && status === 'error' && <span className="text-[#ff8a73] max-w-[320px] truncate" title={snap.project.message}>{snap.project.message}</span>}
          {p && <span className="muted">Claude ${p.budget.spent.toFixed(3)} / ${p.budget.limit.toFixed(2)}</span>}
        </div>
      </header>
      <div className="flex-1 min-h-0">
        <Routes>
          <Route path="footage" element={<Footage />} />
          <Route path="music" element={<Music />} />
          <Route path="editor" element={<Editor />} />
          <Route path="showdown" element={<Showdown />} />
          <Route path="documentary" element={<Documentary />} />
          <Route path="*" element={<Navigate to={isDoc ? 'documentary' : isShowdown ? 'showdown' : 'footage'} replace />} />
        </Routes>
      </div>
    </div>
  )
}

function TokenGate({ children }: { children: React.ReactNode }) {
  const [need, setNeed] = useState(false)
  const [val, setVal] = useState('')
  useEffect(() => {
    const on = () => setNeed(true)
    window.addEventListener('wc-auth-required', on)
    fetch(API_BASE + '/api/auth', { headers: getToken() ? { 'X-Wildcut-Token': getToken() } : {} }).then(r => { if (r.status === 401) setNeed(true) }).catch(() => {})
    return () => window.removeEventListener('wc-auth-required', on)
  }, [])
  if (!need) return <>{children}</>
  return (
    <div className="h-full flex items-center justify-center p-6">
      <div className="card p-6 w-[380px] flex flex-col gap-3">
        <div className="font-semibold tracking-wide">WILD<span style={{ color: 'var(--accent)' }}>CUT</span></div>
        <div className="text-sm muted">This server requires an access token (WILDCUT_ACCESS_TOKEN on the server).</div>
        <input className="input" type="password" placeholder="access token" value={val} onChange={e => setVal(e.target.value)} onKeyDown={e => { if (e.key === 'Enter' && val) { setToken(val); location.reload() } }} autoFocus />
        <button className="btn btn-primary" disabled={!val} onClick={() => { setToken(val); location.reload() }}>Enter</button>
      </div>
    </div>
  )
}

export default function App() {
  return (
    <TokenGate>
    <Routes>
      <Route path="/" element={<Projects />} />
      <Route path="/new" element={<NewProject />} />
      <Route path="/p/:id/*" element={<ProjectShell />} />
    </Routes>
    </TokenGate>
  )
}
