import { useEffect, useRef, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useParams } from 'react-router-dom'
import WaveSurfer from 'wavesurfer.js'
import { abs, api, fmtShort, isRemote, type BeatGrid } from '../api'
import FileBrowser from '../components/FileBrowser'

export default function Music() {
  const { id } = useParams()
  const pid = id!
  const qc = useQueryClient()
  const project = useQuery({ queryKey: ['project', pid], queryFn: () => api.project(pid) })
  const grid = useQuery({ queryKey: ['beatgrid', pid], queryFn: () => api.beatgrid(pid), retry: false, refetchInterval: (q) => (q.state.data ? false : 2000) })
  const [path, setPath] = useState('')
  const [browse, setBrowse] = useState(false)
  const [err, setErr] = useState('')
  const [pct, setPct] = useState<number | null>(null)
  const inv = () => { qc.invalidateQueries({ queryKey: ['project', pid] }); qc.invalidateQueries({ queryKey: ['beatgrid', pid] }) }
  const setSong = useMutation({ mutationFn: (p: string) => api.setSong(pid, p), onSuccess: inv, onError: (e: Error) => setErr(e.message) })
  const upload = useMutation({ mutationFn: (f: File) => api.uploadSong(pid, f, setPct), onSuccess: () => { setPct(null); inv() }, onError: (e: Error) => { setPct(null); setErr(e.message) } })
  const clear = useMutation({ mutationFn: () => api.clearSong(pid), onSuccess: inv })
  const patch = useMutation({ mutationFn: (b: any) => api.patchProject(pid, b), onSuccess: inv })
  const setWindow = useMutation({ mutationFn: (w: { start: number; end: number; locked: boolean }) => api.setWindow(pid, w.start, w.end, w.locked), onSuccess: () => { inv(); qc.invalidateQueries({ queryKey: ['edl', pid] }) } })
  const setDrop = useMutation({ mutationFn: (t: number) => api.setDrop(pid, t), onSuccess: inv })
  const p = project.data
  const targetLen = p ? ({ '15': 15, '30': 30, '45': 45, '60': 60 } as Record<string, number>)[p.target_length] ?? null : null
  return (
    <div className="p-5 flex flex-col gap-4 h-full overflow-auto scroll">
      <div className="card p-4 flex flex-col gap-3">
        <div className="flex items-center gap-3">
          <h2 className="font-medium">Song</h2>
          {p?.song_path ? <span className="text-xs muted truncate max-w-[520px]" title={p.song_path}>{p.song_path.split(/[\\/]/).pop()}</span> : <span className="muted text-xs">none yet</span>}
          {p?.song_path && <button className="btn btn-sm" onClick={() => clear.mutate()}>remove (switch to visual peaks)</button>}
          {err && <span className="text-[#ff8a73] text-xs">{err}</span>}
        </div>
        <div className="flex gap-2 items-end">
          <label className="btn btn-primary cursor-pointer">{upload.isPending ? `Uploading… ${pct ?? 0}%` : 'Upload song (MP3 / WAV / M4A)'}<input type="file" accept="audio/*" className="hidden" onChange={e => { const f = e.target.files?.[0]; if (f) upload.mutate(f); e.target.value = '' }} /></label>
          {!isRemote() && <>
            <div className="flex-1"><label className="label">or a local path</label><input className="input" placeholder="/Users/tim/Music/phonk_track.mp3" value={path} onChange={e => setPath(e.target.value)} onKeyDown={e => e.key === 'Enter' && path && setSong.mutate(path)} /></div>
            <button className="btn" onClick={() => setBrowse(true)}>Browse…</button>
            <button className="btn" disabled={!path} onClick={() => setSong.mutate(path)}>Use path</button>
          </>}
        </div>
        <div className="flex gap-4 items-center text-sm">
          <label className="flex items-center gap-2"><span className="muted">Export audio</span>
            <select className="input w-56" value={p?.audio_export ?? 'silent'} onChange={e => patch.mutate({ audio_export: e.target.value })}><option value="silent">Silent (attach sound on TikTok)</option><option value="mixed">Song mixed in</option></select>
          </label>
          {p?.song_window && <span className="muted">Window {fmtShort(p.song_window.start)} – {fmtShort(p.song_window.end)} · sound offset <b style={{ color: 'var(--accent)' }}>{fmtShort(p.song_window.start)}</b></span>}
        </div>
      </div>
      {p?.song_path && !grid.data && <div className="card p-4 muted text-sm">Analyzing beats, bass hits and the drop…</div>}
      {p?.song_url && grid.data && (
        <Waveform url={abs(p.song_url)} grid={grid.data} window={p.song_window} targetLen={targetLen}
          onWindow={(s, e) => setWindow.mutate({ start: s, end: e, locked: true })} onDrop={(t) => setDrop.mutate(t)} />
      )}
      {grid.data && (
        <div className="text-xs muted flex gap-4">
          <span>{grid.data.tempo.toFixed(1)} BPM</span><span>{grid.data.beats.length} beats</span><span>{grid.data.bass_hits.length} bass hits</span>
          <span>drop candidates: {grid.data.drop_candidates.map(c => `${fmtShort(c.t)} (${Math.round(c.score * 100)}%)`).join(', ')}</span>
          <span>Click a drop candidate to override; drag the window; the edit replans with the drop at ~60% of the window.</span>
        </div>
      )}
      {browse && <FileBrowser kinds={['audio']} onClose={() => setBrowse(false)} onPick={(p) => { setSong.mutate(p); setBrowse(false) }} />}
    </div>
  )
}

function Waveform({ url, grid, window: win, targetLen, onWindow, onDrop }: { url: string; grid: BeatGrid; window: { start: number; end: number } | null; targetLen: number | null; onWindow: (s: number, e: number) => void; onDrop: (t: number) => void }) {
  const ref = useRef<HTMLDivElement>(null)
  const wsRef = useRef<WaveSurfer | null>(null)
  const [ready, setReady] = useState(false)
  const [local, setLocal] = useState(win)
  const [drag, setDrag] = useState<null | { kind: 'move' | 'start' | 'end'; x0: number; w0: { start: number; end: number } }>(null)
  const dur = grid.duration
  useEffect(() => { setLocal(win) }, [win])
  useEffect(() => {
    if (!ref.current) return
    const ws = WaveSurfer.create({ container: ref.current, url, height: 120, waveColor: '#4a4a58', progressColor: '#8d8d99', cursorColor: '#f2b53a', barWidth: 2, barGap: 1, normalize: true })
    ws.on('ready', () => setReady(true))
    wsRef.current = ws
    return () => { ws.destroy(); wsRef.current = null }
  }, [url])
  const pct = (t: number) => `${(t / dur) * 100}%`
  const toTime = (clientX: number) => { const r = ref.current!.getBoundingClientRect(); return Math.max(0, Math.min(dur, ((clientX - r.left) / r.width) * dur)) }
  const onMouseMove = (e: React.MouseEvent) => {
    if (!drag || !local) return
    const dt = toTime(e.clientX) - toTime(drag.x0)
    const len = drag.w0.end - drag.w0.start
    let s = drag.w0.start, en = drag.w0.end
    if (drag.kind === 'move') { s = Math.max(0, Math.min(dur - len, drag.w0.start + dt)); en = s + len }
    if (drag.kind === 'start') s = Math.max(0, Math.min(drag.w0.end - 5, drag.w0.start + dt))
    if (drag.kind === 'end') en = Math.min(dur, Math.max(drag.w0.start + 5, drag.w0.end + dt))
    if (targetLen && drag.kind !== 'move') { /* free resize is allowed; target length is only a default */ }
    setLocal({ start: s, end: en })
  }
  const onMouseUp = () => { if (drag && local) onWindow(Number(local.start.toFixed(2)), Number(local.end.toFixed(2))); setDrag(null) }
  return (
    <div className="card p-3 select-none" onMouseMove={onMouseMove} onMouseUp={onMouseUp} onMouseLeave={() => drag && onMouseUp()}>
      <div className="relative">
        <div ref={ref} />
        {ready && (
          <div className="absolute inset-0 pointer-events-none">
            {grid.beats.map((b, i) => <div key={i} className="absolute top-0 bottom-0" style={{ left: pct(b), width: 1, background: grid.downbeats.includes(b) ? 'rgba(255,255,255,.35)' : 'rgba(255,255,255,.12)' }} />)}
            {grid.bass_hits.map((b, i) => <div key={`h${i}`} className="absolute bottom-0" style={{ left: pct(b), width: 2, height: 10, background: '#ff5a3c' }} />)}
            {grid.drop_candidates.map((c, i) => (
              <div key={`d${i}`} className="absolute top-0 bottom-0 pointer-events-auto cursor-pointer" style={{ left: pct(c.t), width: 2, background: grid.chosen_drop != null && Math.abs(c.t - grid.chosen_drop) < 0.01 ? '#f2b53a' : 'rgba(242,181,58,.4)' }} title={`drop candidate ${fmtShort(c.t)} (${Math.round(c.score * 100)}%)`} onClick={() => onDrop(c.t)}>
                <span className="absolute -top-0 left-1 text-[10px]" style={{ color: '#f2b53a' }}>{i === 0 ? 'DROP' : `alt ${i}`}</span>
              </div>
            ))}
            {local && (
              <div className="absolute top-0 bottom-0 pointer-events-auto" style={{ left: pct(local.start), width: pct(local.end - local.start), background: 'rgba(242,181,58,.12)', border: '1px solid rgba(242,181,58,.6)', cursor: 'grab' }}
                onMouseDown={e => setDrag({ kind: 'move', x0: e.clientX, w0: local })}>
                <div className="absolute left-0 top-0 bottom-0 w-2 cursor-ew-resize" style={{ background: 'rgba(242,181,58,.5)' }} onMouseDown={e => { e.stopPropagation(); setDrag({ kind: 'start', x0: e.clientX, w0: local }) }} />
                <div className="absolute right-0 top-0 bottom-0 w-2 cursor-ew-resize" style={{ background: 'rgba(242,181,58,.5)' }} onMouseDown={e => { e.stopPropagation(); setDrag({ kind: 'end', x0: e.clientX, w0: local }) }} />
                <span className="absolute bottom-1 left-3 text-[11px]" style={{ color: '#f2b53a' }}>{fmtShort(local.start)} – {fmtShort(local.end)} ({(local.end - local.start).toFixed(1)}s)</span>
              </div>
            )}
          </div>
        )}
      </div>
      <div className="flex gap-2 mt-2">
        <button className="btn btn-sm" onClick={() => wsRef.current?.playPause()}>play / pause</button>
        {local && <button className="btn btn-sm" onClick={() => wsRef.current?.setTime(local.start)}>go to window start</button>}
        {grid.chosen_drop != null && <button className="btn btn-sm" onClick={() => wsRef.current?.setTime(Math.max(0, grid.chosen_drop! - 2))}>play into the drop</button>}
      </div>
    </div>
  )
}
