import { useEffect, useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Link, useParams } from 'react-router-dom'
import { API_BASE, abs, api, fmtShort, getToken, isRemote, uploadFiles } from '../api'
import { activeJob, useEvents } from '../hooks/useEvents'
import FileBrowser from '../components/FileBrowser'

type Shot = { index: number; start: number; end: number; duration: number; category: string; species: string; caption: string; score: number; max_motion: number; rejected: string; duplicate_of: number | null; thumb_url: string | null; starred: boolean; banned: boolean; used: boolean; classified_by: string }

const hdr = (): Record<string, string> => (getToken() ? { 'X-Wildcut-Token': getToken() } : {})
async function docGet(id: string) { const r = await fetch(`${API_BASE}/api/projects/${id}/documentary`, { headers: hdr() }); if (!r.ok) throw new Error(await r.text()); return r.json() }
async function post(url: string, body?: unknown) { const r = await fetch(API_BASE + url, { method: 'POST', headers: { 'Content-Type': 'application/json', ...hdr() }, body: body ? JSON.stringify(body) : undefined }); if (!r.ok) { const j = await r.json().catch(() => ({})); throw new Error(j.detail ?? r.statusText) } return r.json() }
async function patch(url: string, body: unknown) { const r = await fetch(API_BASE + url, { method: 'PATCH', headers: { 'Content-Type': 'application/json', ...hdr() }, body: JSON.stringify(body) }); if (!r.ok) throw new Error(await r.text()); return r.json() }

export default function Documentary() {
  const { id } = useParams()
  const pid = id!
  const qc = useQueryClient()
  const snap = useEvents(pid)
  const project = useQuery({ queryKey: ['project', pid], queryFn: () => api.project(pid) })
  const doc = useQuery({ queryKey: ['documentary', pid], queryFn: () => docGet(pid), refetchInterval: (q) => (q.state.data?.bank?.ready ? false : 4000) })
  const opts = project.data?.options?.documentary ?? {}
  const [path, setPath] = useState<string>(opts.path ?? '')
  const [animal, setAnimal] = useState<string>(opts.animal ?? 'auto')
  const [edits, setEdits] = useState<string>(opts.edits ?? 'as_many')
  const [filter, setFilter] = useState<string>('hero')
  const [browse, setBrowse] = useState(false)
  const [err, setErr] = useState('')
  const [pct, setPct] = useState<number | null>(null)
  const [songPct, setSongPct] = useState<number | null>(null)
  const uploadSong = useMutation({ mutationFn: (f: File) => api.uploadSong(pid, f, setSongPct), onSuccess: () => { setSongPct(null); qc.invalidateQueries({ queryKey: ['project', pid] }) }, onError: (e: Error) => { setSongPct(null); setErr(e.message) } })
  const toVisual = useMutation({ mutationFn: () => api.clearSong(pid), onSuccess: () => qc.invalidateQueries({ queryKey: ['project', pid] }) })
  const uploadFilm = useMutation({ mutationFn: (f: File) => uploadFiles(`/api/projects/${pid}/documentary/upload?animal=${encodeURIComponent(animal || 'auto')}&edits=${edits}`, [f], 'file', setPct), onSuccess: (d: any) => { setPct(null); setPath(d.path); inv() }, onError: (e: Error) => { setPct(null); setErr(e.message) } })
  useEffect(() => { if (opts.path && !path) setPath(opts.path); if (opts.animal && animal === 'auto') setAnimal(opts.animal); if (opts.edits) setEdits(opts.edits) }, [opts.path, opts.animal, opts.edits])  // eslint-disable-line react-hooks/exhaustive-deps
  const inv = () => { qc.invalidateQueries({ queryKey: ['documentary', pid] }); qc.invalidateQueries({ queryKey: ['project', pid] }) }
  const register = useMutation({ mutationFn: () => post(`/api/projects/${pid}/documentary/register`, { path: path || opts.path, animal: animal || 'auto', edits }), onSuccess: inv, onError: (e: Error) => setErr(e.message) })
  const analyze = useMutation({ mutationFn: async (force: boolean) => { if (!doc.data?.registered) await register.mutateAsync(); return post(`/api/projects/${pid}/documentary/analyze?force=${force}`) }, onSuccess: inv, onError: (e: Error) => setErr(e.message) })
  const flag = useMutation({ mutationFn: ({ index, body }: { index: number; body: { starred?: boolean; banned?: boolean } }) => patch(`/api/projects/${pid}/documentary/shots/${index}`, body), onSuccess: inv })
  const generate = useMutation({ mutationFn: () => post(`/api/projects/${pid}/documentary/generate`, { count: edits }), onSuccess: inv, onError: (e: Error) => setErr(e.message) })
  const [picked, setPicked] = useState<Record<string, boolean>>({})
  const [chapterText, setChapterText] = useState('')
  const [chaptersLoaded, setChaptersLoaded] = useState(false)
  const saveChapters = useMutation({ mutationFn: async () => { const r = await fetch(`${API_BASE}/api/projects/${pid}/documentary/chapters`, { method: 'PUT', headers: { 'Content-Type': 'application/json', ...hdr() }, body: JSON.stringify({ text: chapterText }) }); if (!r.ok) throw new Error(await r.text()); return r.json() }, onSuccess: inv, onError: (e: Error) => setErr(e.message) })
  const generateAnimals = useMutation({ mutationFn: () => post(`/api/projects/${pid}/documentary/generate`, { count: 'as_many', animals: Object.keys(picked).filter(k => picked[k]) }), onSuccess: () => { setPicked({}); inv() }, onError: (e: Error) => setErr(e.message) })
  const job = activeJob(snap, ['documentary_analyze', 'documentary_generate'])
  const bank = doc.data?.bank
  const shots: Shot[] = useMemo(() => bank?.shots ?? [], [bank])
  const visible = useMemo(() => shots.filter(s => filter === 'rejected' ? !!s.rejected : (!s.rejected && s.category === filter)).sort((a, b) => b.score - a.score), [shots, filter])
  const est = bank?.estimate
  const d = doc.data?.documentary
  return (
    <div className="h-full overflow-auto scroll p-5 flex flex-col gap-4">
      <div className="card p-4 flex flex-col gap-3">
        <div className="flex items-center gap-3"><h2 className="font-medium">Documentary</h2>{d && <span className="muted text-xs truncate max-w-[560px]" title={d.path}>{d.path}</span>}{err && <span className="text-[#ff8a73] text-xs">{err}</span>}</div>
        <div className="grid grid-cols-[1fr_200px_240px_auto] gap-3 items-end">
          <div><label className="label">Film</label><div className="flex gap-2 items-center">
            <label className="btn btn-primary cursor-pointer whitespace-nowrap">{uploadFilm.isPending ? `Uploading… ${pct ?? 0}%` : 'Upload film'}<input type="file" accept="video/*,.mkv,.mp4,.mov" className="hidden" onChange={e => { const f = e.target.files?.[0]; if (f) uploadFilm.mutate(f); e.target.value = '' }} /></label>
            {!isRemote() ? <><input className="input" value={path} onChange={e => setPath(e.target.value)} placeholder="or a local path / a file in inbox/" /><button className="btn" onClick={() => setBrowse(true)}>Browse…</button></> : <span className="muted text-xs truncate">{path ? path.split(/[\\/]/).pop() : 'no film yet'}</span>}
          </div>{uploadFilm.isPending && <div className="h-1.5 rounded bg-[#23232b] overflow-hidden mt-2"><div className="h-full" style={{ width: `${pct ?? 0}%`, background: 'var(--accent)' }} /></div>}</div>
          <div><label className="label">Animal</label><input className="input" value={animal} onChange={e => setAnimal(e.target.value)} placeholder="auto, any, or a name" title="auto = detect the main animal; any = multi-animal compilation; or type a species" /></div>
          <div><label className="label">Edits</label><select className="input" value={edits} onChange={e => setEdits(e.target.value)}><option value="1">1</option><option value="2">2</option><option value="3">3</option><option value="as_many">as many as the footage supports</option></select></div>
          <div className="flex gap-2">
            <button className="btn" disabled={register.isPending || !path} onClick={() => register.mutate()}>Save</button>
            <button className="btn btn-primary" disabled={!!job || analyze.isPending || !path} onClick={() => analyze.mutate(false)}>{job?.kind === 'documentary_analyze' ? 'Analyzing…' : bank?.ready ? 'Re-analyze' : 'Analyze film'}</button>
          </div>
        </div>
        {job && <div><div className="h-2 rounded bg-[#23232b] overflow-hidden"><div className="h-full" style={{ width: `${Math.round(job.progress * 100)}%`, background: 'var(--accent)' }} /></div><div className="muted text-xs mt-1">{job.kind}: {Math.round(job.progress * 100)}% {job.message}</div></div>}
        <div className="flex items-center gap-3 flex-wrap">
          <span className="label mb-0">Song</span>
          <label className="btn cursor-pointer">{uploadSong.isPending ? `Uploading… ${songPct ?? 0}%` : (project.data?.song_path ? 'Replace song' : 'Upload song (MP3 / WAV / M4A)')}<input type="file" accept="audio/*" className="hidden" onChange={e => { const f = e.target.files?.[0]; if (f) uploadSong.mutate(f); e.target.value = '' }} /></label>
          {project.data?.song_path ? <span className="text-sm">{project.data.song_path.split(/[\\/]/).pop()} <span className="muted">· music-synced: cuts land on the beat and the hero on the drop</span></span>
            : project.data?.mode === 'music' ? <span className="text-xs" style={{ color: 'var(--accent)' }}>Music-synced project without a song yet: upload your phonk track, or <button className="underline" onClick={() => toVisual.mutate()}>switch to visual peaks</button> (no music).</span>
            : <span className="muted text-xs">visual-peaks mode (no music); uploading a song switches to music-synced</span>}
        </div>
        <div className="text-xs muted">Multi-GB files are read in place by the worker; a 540p proxy and all analysis are cached per film, so generating more edits later never reprocesses it. Never uses the documentary's audio.</div>
      </div>
      {bank?.ready && (
        <div className="card p-4 flex flex-wrap gap-x-6 gap-y-2 text-sm items-center">
          <span><b>{bank.n_shots}</b> shots, <b>{bank.n_kept}</b> kept</span>
          <span className="muted">rejected: {Object.entries(bank.rejected).filter(([, v]) => (v as number) > 0).map(([k, v]) => `${v} ${k}`).join(', ') || 'none'}</span>
          <span>HERO {bank.categories.hero} · AURA {bank.categories.aura} · BROLL {bank.categories.broll} · OTHER {bank.categories.other}</span>
          <span className="muted">animal: <b style={{ color: 'var(--text)' }}>{bank.animal === 'any' ? 'multiple (compilation)' : (bank.animal || 'unknown')}</b>{bank.species?.length > 1 && <span> · {bank.species.slice(0, 6).map((s: [string, number]) => `${s[0]} ${Math.round(s[1])}s`).join(', ')}</span>}{bank.classified_by_claude ? ` · ${bank.classified_by_claude} shots classified by Claude` : ' · heuristic classification (Claude not configured)'}</span>
          <span className="muted">analysis {d?.analysis_seconds ? `${Math.round(d.analysis_seconds)}s` : ''} ({Object.entries(bank.timings).map(([k, v]) => `${k} ${v}s`).join(', ')})</span>
          <span className="ml-auto pill pill-warn">supports {est?.supported ?? 0} more edit{est?.supported === 1 ? '' : 's'} · {est?.hero_seconds}s of HERO in {est?.hero_shots} shots</span>
          <button className="btn btn-primary" disabled={!!job || !(est?.hero_shots > 0)} onClick={() => generate.mutate()}>{est?.supported ? `Generate ${edits === 'as_many' ? est.supported : edits} edit${(edits === '1' || (edits === 'as_many' && est.supported === 1)) ? '' : 's'}` : 'Generate 1 shorter edit'}</button>
        </div>
      )}
      {bank?.notes?.length > 0 && <div className="text-xs" style={{ color: 'var(--accent)' }}>{bank.notes.join(' ')}</div>}
      {bank?.ready && (
        <div className="card p-4 flex flex-col gap-3">
          <div className="flex items-center gap-3"><div className="font-medium">One edit per animal</div><span className="muted text-xs">Tick the animals you want; each gets its own 60 to 70 s edit titled after it, built only from that animal's shots.</span></div>
          <div className="flex flex-wrap gap-2">
            {(bank.animals ?? []).map((a: any) => (
              <label key={a.animal} className={`btn btn-sm cursor-pointer ${picked[a.animal] ? 'border-[var(--accent)]' : ''}`}>
                <input type="checkbox" className="mr-1" checked={!!picked[a.animal]} onChange={e => setPicked(p => ({ ...p, [a.animal]: e.target.checked }))} />
                <span title={(a.members ?? []).length > 1 ? `includes: ${a.members.join(', ')}` : undefined}>{a.animal}{(a.members ?? []).length > 1 ? ` (+${a.members.length - 1})` : ''}</span> <span className="muted ml-1">{a.hero_shots} hero · {Math.round(a.hero_seconds)}s{a.aura_shots ? ` · ${a.aura_shots} aura` : ''}</span>
              </label>
            ))}
            {(bank.animals ?? []).length === 0 && <span className="muted text-sm">No animals with HERO shots yet.</span>}
          </div>
          <div className="flex gap-2 items-center">
            <button className="btn btn-primary" disabled={!!job || !Object.values(picked).some(Boolean)} onClick={() => generateAnimals.mutate()}>Generate {Object.values(picked).filter(Boolean).length || ''} animal edit{Object.values(picked).filter(Boolean).length === 1 ? '' : 's'}</button>
            <button className="btn btn-sm" onClick={() => setPicked(Object.fromEntries((bank.animals ?? []).filter((a: any) => a.hero_seconds >= 20).map((a: any) => [a.animal, true])))}>select all with 20 s+</button>
          </div>
          <details open={!!(bank.chapters ?? []).length} onToggle={() => { if (!chaptersLoaded && bank.chapters?.length) { setChapterText(bank.chapters.map((c: any) => `${Math.floor(c.start / 60)}:${String(Math.floor(c.start % 60)).padStart(2, '0')} ${c.animal}`).join('\n')); setChaptersLoaded(true) } }}>
            <summary className="cursor-pointer text-sm muted">Know where each animal's segment starts? Paste timestamps (optional, more reliable than species tags){bank.chapters?.length ? ` · ${bank.chapters.length} chapters saved` : ''}</summary>
            <div className="mt-2 flex gap-2 items-start">
              <textarea className="input font-mono" rows={6} placeholder={'0:00 lion\n3:12 emperor penguin\n6:40 marine iguana\n...'} value={chapterText} onChange={e => setChapterText(e.target.value)} />
              <button className="btn" disabled={saveChapters.isPending} onClick={() => saveChapters.mutate()}>Save chapters</button>
            </div>
          </details>
        </div>
      )}
      {doc.data?.edits?.length > 0 && (
        <div className="card p-4">
          <div className="font-medium mb-2">Generated edits</div>
          <div className="grid grid-cols-2 md:grid-cols-3 gap-3">
            {doc.data.edits.map((e: any) => (
              <Link key={e.id} to={`/p/${e.id}/editor`} className="card p-3 hover:border-[var(--accent)]">
                <div className="font-medium truncate">{e.name}</div>
                <div className="text-xs muted">{e.clip_count} clips · {e.status}{e.last_export ? ' · exported' : ''}</div>
                <div className="text-xs mt-1" style={{ color: 'var(--accent)' }}>open in the editor / Director →</div>
              </Link>
            ))}
          </div>
        </div>
      )}
      {bank?.ready && (
        <div className="flex flex-col gap-3">
          <div className="flex items-center gap-2">
            {(['hero', 'aura', 'broll', 'other', 'rejected'] as const).map(c => <button key={c} className={`btn btn-sm ${filter === c ? 'border-[var(--accent)]' : ''}`} onClick={() => setFilter(c)}>{c.toUpperCase()} <span className="muted">{c === 'rejected' ? shots.filter(s => s.rejected).length : bank.categories[c]}</span></button>)}
            <span className="muted text-xs ml-2">hover to preview · ★ star to prefer · ban to exclude · greyed = already used in an edit</span>
          </div>
          <div className="grid grid-cols-3 md:grid-cols-4 xl:grid-cols-6 gap-3">
            {visible.map(s => <ShotCard key={s.index} s={s} proxy={bank.proxy_url} onStar={() => flag.mutate({ index: s.index, body: { starred: !s.starred } })} onBan={() => flag.mutate({ index: s.index, body: { banned: !s.banned } })} />)}
          </div>
        </div>
      )}
      {browse && <FileBrowser kinds={['video']} onClose={() => setBrowse(false)} onPick={(p) => { setPath(p); setBrowse(false) }} />}
    </div>
  )
}

function ShotCard({ s, proxy, onStar, onBan }: { s: Shot; proxy: string; onStar: () => void; onBan: () => void }) {
  const [hover, setHover] = useState(false)
  return (
    <div className={`card overflow-hidden ${s.banned ? 'opacity-40' : s.used ? 'opacity-60' : ''}`} onMouseEnter={() => setHover(true)} onMouseLeave={() => setHover(false)}>
      <div className="aspect-video bg-black relative">
        {hover && !s.rejected ? <video src={`${abs(proxy)}#t=${s.start.toFixed(2)},${s.end.toFixed(2)}`} autoPlay muted loop playsInline className="w-full h-full object-cover" /> : s.thumb_url ? <img src={abs(s.thumb_url)} className="w-full h-full object-cover" alt="" loading="lazy" /> : <div className="w-full h-full flex items-center justify-center muted text-xs">{s.rejected}</div>}
        <span className="absolute top-1 left-1 pill" style={{ background: 'rgba(0,0,0,.65)' }}>{fmtShort(s.start)}–{fmtShort(s.end)}</span>
        {!s.rejected && <span className="absolute top-1 right-1 pill" style={{ background: 'rgba(0,0,0,.65)' }}>{s.score.toFixed(2)}</span>}
        {s.used && <span className="absolute bottom-1 left-1 pill pill-ok">used</span>}
        {s.starred && <span className="absolute bottom-1 right-1 pill pill-warn">★</span>}
      </div>
      <div className="p-1.5 text-[11px] flex items-center gap-1">
        <span className="truncate flex-1" title={s.caption}>{s.rejected ? `rejected: ${s.rejected}${s.duplicate_of != null ? ` of #${s.duplicate_of}` : ''}` : (s.caption || `${s.category} · motion ${s.max_motion.toFixed(2)}`)}</span>
        {!s.rejected && <><button className="btn btn-sm" onClick={onStar} title="prefer this shot">★</button><button className="btn btn-sm btn-danger" onClick={onBan} title={s.banned ? 'unban' : 'exclude from edits'}>{s.banned ? 'unban' : 'ban'}</button></>}
      </div>
    </div>
  )
}
