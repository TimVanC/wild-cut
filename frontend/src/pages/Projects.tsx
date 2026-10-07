import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import { api, type Project } from '../api'

export default function Projects() {
  const qc = useQueryClient()
  const projects = useQuery({ queryKey: ['projects'], queryFn: api.projects, refetchInterval: 4000 })
  const cfg = useQuery({ queryKey: ['config'], queryFn: api.config })
  const del = useMutation({ mutationFn: (id: string) => api.deleteProject(id), onSuccess: () => qc.invalidateQueries({ queryKey: ['projects'] }) })
  const dest = (p: Project) => p.options?.documentary ? 'documentary' : p.style === 'showdown' ? 'showdown' : p.status === 'new' ? 'footage' : 'editor'
  return (
    <div className="max-w-6xl mx-auto p-6">
      <div className="flex items-center gap-3 mb-6">
        <h1 className="text-xl font-semibold tracking-wide">WILD<span style={{ color: 'var(--accent)' }}>CUT</span></h1>
        <span className="muted">projects</span>
        <div className="ml-auto flex gap-2">
          <Link to="/new?kind=documentary" className="btn">New from documentary</Link>
          <Link to="/new" className="btn btn-primary">New project</Link>
        </div>
      </div>
      {cfg.data && (!cfg.data.claude.enabled || cfg.data.claude.needs_workspace) && (
        <div className="card p-3 mb-4 text-sm" style={{ borderColor: '#5a4518' }}>
          <b style={{ color: 'var(--accent)' }}>Claude is not fully configured.</b> {cfg.data.claude.enabled ? 'Your key is user-scoped: set ANTHROPIC_WORKSPACE_ID in .env.' : 'Set ANTHROPIC_API_KEY in .env.'} Analysis still runs with motion-only tags; vision tags, titles from species, and the Director chat need Claude.
        </div>
      )}
      {cfg.data && !cfg.data.stock.enabled && <div className="muted text-xs mb-4">{cfg.data.stock.message}</div>}
      {projects.data?.length === 0 && <div className="card p-8 text-center muted">No projects yet. Create one, add footage, and hit Analyze.</div>}
      <div className="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-4 gap-4">
        {projects.data?.map(p => (
          <div key={p.id} className="card overflow-hidden flex flex-col">
            <Link to={`/p/${p.id}/${dest(p)}`} className="block aspect-[4/3] bg-black/40 relative">
              {p.thumb_url ? <img src={p.thumb_url} className="w-full h-full object-cover" alt="" /> : <div className="w-full h-full flex items-center justify-center muted text-xs">no footage yet</div>}
              <span className="absolute top-2 left-2 pill" style={{ background: 'rgba(0,0,0,.6)' }}>{p.style}</span>
              {p.options?.documentary && <span className="absolute top-2 right-2 pill" style={{ background: 'rgba(0,0,0,.6)' }}>doc</span>}
            </Link>
            <div className="p-3 flex-1 flex flex-col gap-1">
              <div className="font-medium truncate">{p.name}</div>
              <div className="text-xs muted">{p.aspect} · {p.mode === 'music' ? 'music' : 'visual'} · {p.clip_count} clips</div>
              <div className="flex items-center gap-2 mt-1">
                <span className={`pill ${p.status === 'error' ? 'pill-err' : p.status === 'exported' ? 'pill-ok' : ''}`}>{p.status}</span>
                {p.last_export && <span className="text-xs muted truncate" title={p.last_export.path}>exported {new Date(p.last_export.created_at).toLocaleDateString()}</span>}
                <button className="ml-auto btn btn-sm btn-danger" onClick={() => { if (confirm(`Delete "${p.name}"?`)) del.mutate(p.id) }}>delete</button>
              </div>
            </div>
          </div>
        ))}
      </div>
    </div>
  )
}
