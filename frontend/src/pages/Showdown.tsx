import { useEffect, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useNavigate, useParams } from 'react-router-dom'
import { api, type Clip } from '../api'
import { activeJob, useEvents } from '../hooks/useEvents'
import FileBrowser from '../components/FileBrowser'

type Row = { animal: string; value: number | null; unit: string; source_url: string; fact: string; fact_source_url: string; confirmed: boolean; clip_id: string | null }

export default function Showdown() {
  const { id } = useParams()
  const pid = id!
  const qc = useQueryClient()
  const nav = useNavigate()
  const snap = useEvents(pid)
  const cfg = useQuery({ queryKey: ['config'], queryFn: api.config })
  const project = useQuery({ queryKey: ['project', pid], queryFn: () => api.project(pid) })
  const sd = useQuery({ queryKey: ['showdown', pid], queryFn: () => api.showdown(pid) })
  const clips = useQuery({ queryKey: ['clips', pid], queryFn: () => api.clips(pid), refetchInterval: 3000 })
  const [stat, setStat] = useState('top_speed')
  const [unit, setUnit] = useState('')
  const [animals, setAnimals] = useState('')
  const [suggest, setSuggest] = useState(false)
  const [custom, setCustom] = useState('')
  const [layout, setLayout] = useState('default')
  const [winner, setWinner] = useState('')
  const [rows, setRows] = useState<Row[]>([])
  const [browse, setBrowse] = useState(false)
  const [err, setErr] = useState('')
  useEffect(() => {
    const d = sd.data
    if (!d) return
    if (d.stat) setStat(d.stat)
    if (d.unit) setUnit(d.unit)
    if (d.animals?.length) setAnimals(d.animals.join(', '))
    if (d.custom_label) setCustom(d.custom_label)
    if (d.layout) setLayout(d.layout)
    setWinner(d.winner_clip_id ?? '')
    setRows((d.stats ?? []).map((r: any) => ({ ...r, value: r.value ?? null, source_url: r.source_url ?? '', fact: r.fact ?? '', fact_source_url: r.fact_source_url ?? '', confirmed: !!r.confirmed, clip_id: r.clip_id ?? null })))
  }, [sd.data])
  const statCfg = cfg.data?.showdown_stats[stat]
  const inv = () => { qc.invalidateQueries({ queryKey: ['showdown', pid] }); qc.invalidateQueries({ queryKey: ['project', pid] }) }
  const draft = useMutation({ mutationFn: () => api.putShowdown(pid, { stat, unit: unit || statCfg?.default_unit, animals: suggest ? [] : animals.split(',').map(s => s.trim()).filter(Boolean), custom_label: custom, layout, winner_clip_id: winner || null }, true), onSuccess: inv, onError: (e: Error) => setErr(e.message) })
  const saveRows = useMutation({ mutationFn: (r: Row[]) => api.putStats(pid, r), onSuccess: inv, onError: (e: Error) => setErr(e.message) })
  const saveMeta = useMutation({ mutationFn: () => api.putShowdown(pid, { stat, unit: unit || statCfg?.default_unit, animals: rows.map(r => r.animal), custom_label: custom, layout, winner_clip_id: winner || null }, false), onSuccess: inv })
  const addClip = useMutation({ mutationFn: (p: string) => api.addClipPath(pid, p), onSuccess: () => qc.invalidateQueries({ queryKey: ['clips', pid] }), onError: (e: Error) => setErr(e.message) })
  const upload = useMutation({ mutationFn: (files: File[]) => api.uploadClips(pid, files, ), onSuccess: () => qc.invalidateQueries({ queryKey: ['clips', pid] }) })
  const build = useMutation({ mutationFn: async () => { await saveMeta.mutateAsync(); await api.putStats(pid, rows); return api.analyze(pid, true) }, onSuccess: () => { inv(); nav(`/p/${pid}/editor`) }, onError: (e: Error) => setErr(e.message) })
  const job = activeJob(snap, ['showdown_stats'])
  const blockers: string[] = sd.data?.blockers ?? []
  const update = (i: number, patch: Partial<Row>) => setRows(rs => rs.map((r, j) => (j === i ? { ...r, ...patch } : r)))
  const flagged = (r: Row) => !r.confirmed && (r.value == null || !r.source_url.trim())
  const claude = cfg.data?.claude.enabled && !cfg.data.claude.needs_workspace
  return (
    <div className="p-5 flex flex-col gap-4 h-full overflow-auto scroll max-w-6xl">
      <div className="card p-4 grid grid-cols-[1fr_1fr] gap-4">
        <div className="flex flex-col gap-3">
          <div className="font-medium">Stat</div>
          <div className="grid grid-cols-2 gap-2">
            <label><span className="label">Compare</span><select className="input" value={stat} onChange={e => { setStat(e.target.value); setUnit('') }}>{Object.entries(cfg.data?.showdown_stats ?? {}).map(([k, v]) => <option key={k} value={k}>{v.label.toLowerCase()}</option>)}</select></label>
            <label><span className="label">Unit</span><select className="input" value={unit || statCfg?.default_unit || ''} onChange={e => setUnit(e.target.value)}>{(statCfg?.units ?? ['']).map(u => <option key={u} value={u}>{u || '(none)'}</option>)}</select></label>
          </div>
          {stat === 'custom' && <label><span className="label">Custom stat label</span><input className="input" value={custom} onChange={e => setCustom(e.target.value)} placeholder="DIVE DEPTH" /></label>}
          <label className="flex items-center gap-2 text-sm"><input type="checkbox" checked={suggest} onChange={e => setSuggest(e.target.checked)} /> Let Claude suggest 4 to 8 animals (escalating, record holder last)</label>
          {!suggest && <label><span className="label">Animals (comma separated)</span><input className="input" value={animals} onChange={e => setAnimals(e.target.value)} placeholder="cheetah, pronghorn, sailfish, peregrine falcon" /></label>}
          <div className="flex gap-2 items-center">
            <button className="btn btn-primary" disabled={!!job || draft.isPending || (!suggest && !animals.trim())} onClick={() => draft.mutate()}>{job ? 'Drafting…' : 'Draft stats sheet with Claude'}</button>
            {!claude && <span className="text-xs muted">Claude not configured: rows are created empty for you to fill.</span>}
            {err && <span className="text-[#ff8a73] text-xs">{err}</span>}
          </div>
        </div>
        <div className="flex flex-col gap-3">
          <div className="font-medium">Layout and media</div>
          <label><span className="label">Card layout</span><select className="input" value={layout} onChange={e => setLayout(e.target.value)}>{cfg.data?.showdown_layouts.map(l => <option key={l.id} value={l.id}>{l.name}</option>)}</select></label>
          <label><span className="label">Winner action clip (optional, 3 to 6 s after the reveal)</span><select className="input" value={winner} onChange={e => setWinner(e.target.value)}><option value="">none</option>{clips.data?.map(c => <option key={c.id} value={c.id}>{c.label} · {c.description || c.path.split(/[\\/]/).pop()}</option>)}</select></label>
          <div className="text-xs muted">Media per animal: add photos or short clips below (local files, uploads, or stock search on the Footage tab), then pick one per row.</div>
          <div className="flex gap-2">
            <button className="btn btn-sm" onClick={() => setBrowse(true)}>Add local file…</button>
            <label className="btn btn-sm cursor-pointer">Upload<input type="file" multiple accept="video/*,image/*" className="hidden" onChange={e => { const f = Array.from(e.target.files ?? []); if (f.length) upload.mutate(f); e.target.value = '' }} /></label>
            <button className="btn btn-sm" onClick={() => nav(`/p/${pid}/footage`)}>Stock search</button>
          </div>
        </div>
      </div>
      <div className="card p-4">
        <div className="flex items-center gap-3 mb-3">
          <div className="font-medium">Stats sheet</div>
          <span className="muted text-xs">Review every value and source before export. Rows in red have no source or no value and block export until confirmed or fixed.</span>
          {blockers.length > 0 && <span className="pill pill-err ml-auto">export blocked: {blockers.join(', ')}</span>}
          {blockers.length === 0 && rows.length > 0 && <span className="pill pill-ok ml-auto">sheet ok</span>}
        </div>
        <table className="w-full text-xs">
          <thead><tr className="muted text-left"><th className="p-1">animal</th><th className="p-1 w-24">value</th><th className="p-1">source URL</th><th className="p-1">fact (one line)</th><th className="p-1 w-44">media</th><th className="p-1 w-20">confirmed</th><th className="p-1 w-8"></th></tr></thead>
          <tbody>
            {rows.map((r, i) => (
              <tr key={i} style={{ background: flagged(r) ? 'rgba(229,57,53,.12)' : undefined }}>
                <td className="p-1"><input className="input" value={r.animal} onChange={e => update(i, { animal: e.target.value })} /></td>
                <td className="p-1"><input className="input" value={r.value ?? ''} onChange={e => update(i, { value: e.target.value === '' ? null : Number(e.target.value) })} /></td>
                <td className="p-1"><input className="input" value={r.source_url} onChange={e => update(i, { source_url: e.target.value })} placeholder="https://…" style={{ borderColor: !r.source_url && !r.confirmed ? '#e53935' : undefined }} /></td>
                <td className="p-1"><input className="input" value={r.fact} onChange={e => update(i, { fact: e.target.value })} /></td>
                <td className="p-1"><select className="input" value={r.clip_id ?? ''} onChange={e => update(i, { clip_id: e.target.value || null })}><option value="">(placeholder)</option>{clips.data?.map((c: Clip) => <option key={c.id} value={c.id}>{c.label} · {(c.description || c.path.split(/[\\/]/).pop() || '').slice(0, 28)}</option>)}</select></td>
                <td className="p-1 text-center"><input type="checkbox" checked={r.confirmed} onChange={e => update(i, { confirmed: e.target.checked })} /></td>
                <td className="p-1"><button className="btn btn-sm btn-danger" onClick={() => setRows(rs => rs.filter((_, j) => j !== i))}>×</button></td>
              </tr>
            ))}
          </tbody>
        </table>
        <div className="flex gap-2 mt-3">
          <button className="btn btn-sm" onClick={() => setRows(rs => [...rs, { animal: '', value: null, unit: unit || statCfg?.default_unit || '', source_url: '', fact: '', fact_source_url: '', confirmed: false, clip_id: null }])}>+ row</button>
          <button className="btn btn-sm" disabled={saveRows.isPending} onClick={() => { saveMeta.mutate(); saveRows.mutate(rows) }}>Save sheet</button>
          <button className="btn btn-primary ml-auto" disabled={rows.length < 2 || build.isPending} onClick={() => build.mutate()}>Save and build the edit</button>
        </div>
        <div className="text-xs muted mt-2">Music-synced: each challenger enters on a downbeat and the winner lands on the drop. Visual mode: 1.6 s per challenger. Export writes stats.csv with every value and source.</div>
      </div>
      {project.data?.mode === 'music' && !project.data.song_path && <div className="text-xs muted">Music-synced project without a song: add one on the Music tab or the edit uses the fixed rhythm.</div>}
      {browse && <FileBrowser kinds={['video', 'image']} onClose={() => setBrowse(false)} onPick={(p) => { addClip.mutate(p); setBrowse(false) }} />}
    </div>
  )
}
