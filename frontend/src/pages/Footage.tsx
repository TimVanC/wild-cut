import { useRef, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useNavigate, useParams } from 'react-router-dom'
import { abs, api, fileUrl, isRemote, type Clip, type StockResult } from '../api'
import FileBrowser from '../components/FileBrowser'
import StageProgress from '../components/StageProgress'
import { useEvents } from '../hooks/useEvents'

export default function Footage() {
  const { id } = useParams()
  const pid = id!
  const qc = useQueryClient()
  const snap = useEvents(pid)
  const [brief, setBrief] = useState<string | null>(null)
  const [subject, setSubject] = useState<string | null>(null)
  const [action, setAction] = useState<string | null>(null)
  const nav = useNavigate()
  const cfg = useQuery({ queryKey: ['config'], queryFn: api.config })
  const project = useQuery({ queryKey: ['project', pid], queryFn: () => api.project(pid) })
  const clips = useQuery({ queryKey: ['clips', pid], queryFn: () => api.clips(pid), refetchInterval: 3000 })
  const library = useQuery({ queryKey: ['library'], queryFn: () => api.library() })
  const [tab, setTab] = useState<'local' | 'stock' | 'library'>('local')
  const [browse, setBrowse] = useState(false)
  const [q, setQ] = useState('')
  const [orientation, setOrientation] = useState('')
  const [picked, setPicked] = useState<Record<string, StockResult>>({})
  const [err, setErr] = useState('')
  const [pct, setPct] = useState<number | null>(null)
  const fileInput = useRef<HTMLInputElement>(null)
  const invalidate = () => { qc.invalidateQueries({ queryKey: ['clips', pid] }); qc.invalidateQueries({ queryKey: ['project', pid] }) }
  const addPath = useMutation({ mutationFn: (p: string) => api.addClipPath(pid, p), onSuccess: invalidate, onError: (e: Error) => setErr(e.message) })
  const upload = useMutation({ mutationFn: (files: File[]) => api.uploadClips(pid, files, setPct), onSuccess: () => { setPct(null); invalidate() }, onError: (e: Error) => { setPct(null); setErr(e.message) } })
  const remove = useMutation({ mutationFn: (cid: string) => api.deleteClip(pid, cid), onSuccess: invalidate })
  const addLib = useMutation({ mutationFn: (lid: string) => api.addFromLibrary(pid, lid), onSuccess: invalidate })
  const search = useMutation({ mutationFn: () => api.stockSearch(q, orientation || undefined), onError: (e: Error) => setErr(e.message) })
  const importStock = useMutation({ mutationFn: () => api.stockImport(pid, Object.values(picked)), onSuccess: () => { setPicked({}); invalidate() } })
  const saveBrief = useMutation({ mutationFn: (text: string) => api.patchProject(pid, { options: { brief: text } } as any), onSuccess: invalidate })
  const saveFocus = useMutation({ mutationFn: (f: { subject: string; action: string }) => api.patchProject(pid, { options: { focus: f } } as any), onSuccess: invalidate })
  const focusNow = () => ({ subject: (subject ?? project.data?.options?.focus?.subject ?? '').trim(), action: (action ?? project.data?.options?.focus?.action ?? '').trim() })
  const analyze = useMutation({
    mutationFn: async () => {
      const f = focusNow()
      if (f.subject !== (project.data?.options?.focus?.subject ?? '') || f.action !== (project.data?.options?.focus?.action ?? '')) await saveFocus.mutateAsync(f)
      if (brief !== null && brief !== (project.data?.options?.brief ?? '')) await saveBrief.mutateAsync(brief)
      return api.analyze(pid, true)
    },
    onSuccess: () => { invalidate(); nav(`/p/${pid}/${project.data?.mode === 'music' && !project.data.song_path ? 'music' : 'editor'}`) }, onError: (e: Error) => setErr(e.message),
  })
  const onDrop = (e: React.DragEvent) => { e.preventDefault(); const files = Array.from(e.dataTransfer.files); if (files.length) upload.mutate(files) }
  const stockOn = cfg.data?.stock.enabled

  return (
    <div className="h-full grid grid-cols-[1fr_380px] min-h-0">
      <div className="p-5 overflow-auto scroll">
        <div className="flex items-center gap-2 mb-4">
          {(['local', 'stock', 'library'] as const).map(t => <button key={t} onClick={() => setTab(t)} className={`btn ${tab === t ? 'border-[var(--accent)]' : ''}`}>{t === 'local' ? 'Local files' : t === 'stock' ? 'Stock search' : 'Library'}</button>)}
          {err && <span className="text-[#ff8a73] text-xs ml-3 truncate max-w-[480px]">{err}</span>}
        </div>
        {tab === 'local' && (
          <div className="flex flex-col gap-4">
            <div onDragOver={e => e.preventDefault()} onDrop={onDrop} className="card p-8 text-center border-dashed cursor-pointer" onClick={() => fileInput.current?.click()}>
              <button className="btn btn-primary text-base px-5 py-2" disabled={upload.isPending} onClick={e => { e.stopPropagation(); fileInput.current?.click() }}>{upload.isPending ? `Uploading… ${pct ?? 0}%` : 'Upload video files'}</button>
              <div className="text-xs muted mt-3">Opens your file explorer. You can also drop MP4 / MOV / MKV files (or photos) anywhere on this box.</div>
              {upload.isPending && <div className="h-2 rounded bg-[#23232b] overflow-hidden mt-3 max-w-md mx-auto"><div className="h-full" style={{ width: `${pct ?? 0}%`, background: 'var(--accent)' }} /></div>}
              <input ref={fileInput} type="file" multiple accept="video/*,image/*" className="hidden" onChange={e => { const f = Array.from(e.target.files ?? []); if (f.length) upload.mutate(f); e.target.value = '' }} />
            </div>
            {!isRemote() && <>
              <div className="flex gap-2 items-end">
                <PathAdder onAdd={p => addPath.mutate(p)} onBrowse={() => setBrowse(true)} />
              </div>
              <div className="text-xs muted">Or drop files into <code>{cfg.data?.inbox_dir}</code>: they appear in the Library. Files dropped into <code>{cfg.data?.inbox_dir}/{pid}/</code> import straight into this project.</div>
            </>}
          </div>
        )}
        {tab === 'stock' && (
          <div className="flex flex-col gap-3">
            {!stockOn && <div className="card p-3 text-sm" style={{ borderColor: '#5a4518' }}>{cfg.data?.stock.message}</div>}
            <div className="flex gap-2">
              <input className="input" placeholder='Theme or query, e.g. "cheetah hunting" or "predators hunting"' value={q} onChange={e => setQ(e.target.value)} onKeyDown={e => e.key === 'Enter' && q && search.mutate()} />
              <select className="input w-40" value={orientation} onChange={e => setOrientation(e.target.value)}><option value="">any orientation</option><option value="portrait">portrait</option><option value="landscape">landscape</option><option value="square">square</option></select>
              <button className="btn btn-primary" disabled={!stockOn || !q || search.isPending} onClick={() => search.mutate()}>{search.isPending ? 'Searching…' : 'Search'}</button>
            </div>
            {search.data && <div className="text-xs muted">Queries: {search.data.queries.join(' · ')} — {search.data.results.length} results, best action clips first.</div>}
            <div className="grid grid-cols-3 xl:grid-cols-4 gap-3">
              {search.data?.results.map(r => <StockCard key={r.key} r={r} picked={!!picked[r.key]} onToggle={() => setPicked(p => { const n = { ...p }; if (n[r.key]) delete n[r.key]; else n[r.key] = r; return n })} />)}
            </div>
            {Object.keys(picked).length > 0 && <div className="sticky bottom-2 card p-3 flex items-center gap-3"><span>{Object.keys(picked).length} selected</span><button className="btn btn-primary" disabled={importStock.isPending} onClick={() => importStock.mutate()}>Import into project</button><span className="text-xs muted">Downloads through the official APIs; credits and licenses are stored with each clip.</span></div>}
          </div>
        )}
        {tab === 'library' && (
          <div className="grid grid-cols-3 xl:grid-cols-4 gap-3">
            {library.data?.length === 0 && <div className="muted text-sm col-span-full">Library is empty. Imported stock clips and inbox drops land here for reuse.</div>}
            {library.data?.map(l => (
              <div key={l.id} className="card overflow-hidden">
                <div className="aspect-video bg-black/40">{l.thumb_url && <img src={abs(l.thumb_url)} className="w-full h-full object-cover" alt="" />}</div>
                <div className="p-2 text-xs">
                  <div className="truncate" title={l.path}>{l.query || l.source_id}</div>
                  <div className="muted truncate">{l.source} · {l.duration.toFixed(1)}s · {l.width}x{l.height}</div>
                  <button className="btn btn-sm mt-2" onClick={() => addLib.mutate(l.id)}>Add to project</button>
                </div>
              </div>
            ))}
          </div>
        )}
      </div>
      <aside className="border-l p-4 overflow-auto scroll flex flex-col gap-3" style={{ borderColor: 'var(--line)', background: 'var(--panel)' }}>
        <div className="flex items-center gap-2"><h2 className="font-medium">Project footage</h2><span className="muted text-xs">{clips.data?.length ?? 0} clips</span></div>
        {clips.data?.map(c => <ClipRow key={c.id} c={c} pid={pid} onRemove={() => remove.mutate(c.id)} />)}
        {clips.data?.length === 0 && <div className="muted text-sm">No clips yet.</div>}
        <div className="mt-auto flex flex-col gap-2">
          <label className="label">This edit is about</label>
          <div className="grid grid-cols-2 gap-2">
            <input className="input" placeholder="the animal, e.g. iguana" value={subject ?? (project.data?.options?.focus?.subject ?? '')} onChange={e => setSubject(e.target.value)} onBlur={() => saveFocus.mutate(focusNow())} />
            <input className="input" placeholder="what it does, e.g. escapes the snakes" value={action ?? (project.data?.options?.focus?.action ?? '')} onChange={e => setAction(e.target.value)} onBlur={() => saveFocus.mutate(focusNow())} />
          </div>
          <div className="text-xs muted">The subject's moments lead the edit, its key action lands on the drop, other animals only build tension. Works without the Director.</div>
          <label className="label">More direction (optional)</label>
          <textarea className="input text-sm" rows={5} value={brief ?? (project.data?.options?.brief ?? '')} onChange={e => setBrief(e.target.value)} onBlur={() => { if (brief !== null) saveBrief.mutate(brief) }}
            placeholder={'Your vision and the moments that matter, with timestamps in the clip:\n- the drop should hit when the iguana breaks free at 2:41\n- open on the snakes creeping at 0:35\n- title: THE IGUANA\n- dark, fast, no flashes'} />
          <div className="text-xs muted">The auto edit is built first, then the Director applies this brief and you keep directing it in the editor chat.</div>
          <StageProgress snap={snap} />
          {project.data?.mode === 'music' && !project.data.song_path && <div className="text-xs muted">Music-synced project: add the song on the Music tab before or after analysis.</div>}
          <button className="btn btn-primary" disabled={!clips.data?.length || analyze.isPending} onClick={() => analyze.mutate()}>Analyze footage and build the edit</button>
          <div className="text-xs muted">Shot detection, motion scoring, subject tracking, Claude tags, then the auto edit.</div>
        </div>
      </aside>
      {browse && <FileBrowser kinds={['video', 'image']} onClose={() => setBrowse(false)} onPick={(p) => { addPath.mutate(p); setBrowse(false) }} />}
    </div>
  )
}

function PathAdder({ onAdd, onBrowse }: { onAdd: (p: string) => void; onBrowse: () => void }) {
  const [p, setP] = useState('')
  return (
    <div className="flex gap-2 w-full">
      <div className="flex-1"><label className="label">Add by path</label><input className="input" placeholder="/Users/tim/Movies/gibbon_01.mp4" value={p} onChange={e => setP(e.target.value)} onKeyDown={e => { if (e.key === 'Enter' && p) { onAdd(p); setP('') } }} /></div>
      <button className="btn self-end" onClick={onBrowse}>Browse…</button>
      <button className="btn btn-primary self-end" disabled={!p} onClick={() => { onAdd(p); setP('') }}>Add</button>
    </div>
  )
}

function StockCard({ r, picked, onToggle }: { r: StockResult; picked: boolean; onToggle: () => void }) {
  const [hover, setHover] = useState(false)
  return (
    <div className={`card overflow-hidden cursor-pointer ${picked ? 'border-[var(--accent)]' : ''}`} onClick={onToggle} onMouseEnter={() => setHover(true)} onMouseLeave={() => setHover(false)}>
      <div className="aspect-video bg-black relative">
        {hover && r.preview_url && r.kind === 'video' ? <video src={r.preview_url} autoPlay muted loop playsInline className="w-full h-full object-cover" /> : <img src={r.thumbnail_url} className="w-full h-full object-cover" alt="" />}
        <input type="checkbox" checked={picked} readOnly className="absolute top-2 left-2 w-4 h-4" />
        {r.score != null && <span className="absolute top-2 right-2 pill" style={{ background: 'rgba(0,0,0,.6)' }}>{r.score}/10</span>}
        {r.in_library && <span className="absolute bottom-2 right-2 pill pill-ok">in library</span>}
      </div>
      <div className="p-2 text-xs">
        <div className="truncate">{r.credit}</div>
        <div className="muted">{r.source} · {r.duration ? `${r.duration}s · ` : ''}{r.width}x{r.height} · {r.orientation}</div>
      </div>
    </div>
  )
}

function ClipRow({ c, pid, onRemove }: { c: Clip; pid: string; onRemove: () => void }) {
  const [hover, setHover] = useState(false)
  const src = c.proxy_url ? abs(c.proxy_url) : fileUrl(c.path)
  return (
    <div className="card p-2 flex gap-2" onMouseEnter={() => setHover(true)} onMouseLeave={() => setHover(false)}>
      <div className="w-28 aspect-video bg-black/50 rounded overflow-hidden shrink-0">
        {hover ? <video src={src} muted autoPlay loop playsInline className="w-full h-full object-cover" /> : c.thumb_url ? <img src={abs(c.thumb_url)} className="w-full h-full object-cover" alt="" /> : null}
      </div>
      <div className="min-w-0 flex-1 text-xs">
        <div className="font-medium">{c.label} <span className="muted">· {c.duration.toFixed(1)}s · {c.width}x{c.height}</span></div>
        <div className="muted truncate" title={c.path}>{c.description || c.path.split(/[\\/]/).pop()}</div>
        <div className="mt-1 flex gap-1 items-center">
          <span className={`pill ${c.analyzed ? 'pill-ok' : ''}`}>{c.analyzed ? 'analyzed' : 'not analyzed'}</span>
          {c.tags?.species && <span className="pill">{c.tags.species}</span>}
          {c.source !== 'local' && <span className="pill">{c.source}</span>}
          <button className="ml-auto btn btn-sm btn-danger" onClick={onRemove}>remove</button>
        </div>
      </div>
      <span className="hidden">{pid}</span>
    </div>
  )
}
