import { NavLink, Route, Routes, useParams, Navigate } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { api } from './api'
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

export default function App() {
  return (
    <Routes>
      <Route path="/" element={<Projects />} />
      <Route path="/new" element={<NewProject />} />
      <Route path="/p/:id/*" element={<ProjectShell />} />
    </Routes>
  )
}
