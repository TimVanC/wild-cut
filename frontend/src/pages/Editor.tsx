import { useCallback, useEffect, useRef, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Link, useParams } from 'react-router-dom'
import { abs, api, frameUrl, type Edl } from '../api'
import { activeJob, useEvents } from '../hooks/useEvents'
import Timeline, { type Selection } from '../components/Timeline'
import Inspector from '../components/Inspector'
import DirectorChat from '../components/DirectorChat'
import ExportDialog from '../components/ExportDialog'

export default function Editor() {
  const { id } = useParams()
  const pid = id!
  const qc = useQueryClient()
  const snap = useEvents(pid)
  const project = useQuery({ queryKey: ['project', pid], queryFn: () => api.project(pid) })
  const edlQ = useQuery({ queryKey: ['edl', pid], queryFn: () => api.edl(pid), retry: false })
  const grid = useQuery({ queryKey: ['beatgrid', pid], queryFn: () => api.beatgrid(pid), retry: false, enabled: project.data?.mode === 'music' })
  const clips = useQuery({ queryKey: ['clips', pid], queryFn: () => api.clips(pid) })
  const preview = useQuery({ queryKey: ['preview', pid], queryFn: () => api.previewStatus(pid), refetchInterval: (q) => (q.state.data?.ready ? false : 2000) })
  const [selection, setSelection] = useState<Selection>(null)
  const [time, setTime] = useState(0)
  const [dirty, setDirty] = useState<[number, number][]>([])
  const [showExport, setShowExport] = useState(false)
  const [err, setErr] = useState('')
  const video = useRef<HTMLVideoElement>(null)
  const edl: Edl | undefined = edlQ.data?.edl
  const invalidateEdl = () => { qc.invalidateQueries({ queryKey: ['edl', pid] }); qc.invalidateQueries({ queryKey: ['preview', pid] }); qc.invalidateQueries({ queryKey: ['project', pid] }) }
  const op = useMutation({
    mutationFn: ({ op, args }: { op: string; args: Record<string, unknown> }) => api.edlOp(pid, op, args),
    onSuccess: (r) => { setDirty(r.changed); qc.setQueryData(['edl', pid], (old: any) => old ? { ...old, edl: r.edl, version: r.version, can_undo: true, can_redo: false } : old); invalidateEdl() },
    onError: (e: Error) => setErr(e.message),
  })
  const plan = useMutation({ mutationFn: (b: { text_only?: boolean; keep_locks?: boolean }) => api.plan(pid, b), onSuccess: () => { setSelection(null); invalidateEdl() }, onError: (e: Error) => setErr(e.message) })
  const undo = useMutation({ mutationFn: () => api.undo(pid), onSuccess: invalidateEdl })
  const redo = useMutation({ mutationFn: () => api.redo(pid), onSuccess: invalidateEdl })
  const renderPreview = useMutation({ mutationFn: () => api.preview(pid), onSuccess: () => qc.invalidateQueries({ queryKey: ['preview', pid] }) })
  const doOp = useCallback((o: string, args: Record<string, unknown>) => { setErr(''); op.mutate({ op: o, args }) }, [op])
  const previewJob = activeJob(snap, ['preview'])
  const planning = activeJob(snap, ['analyze', 'plan', 'chat'])
  const previewReady = preview.data?.ready && preview.data.version === edlQ.data?.version
  const seek = (t: number) => { setTime(t); if (video.current && previewReady) video.current.currentTime = t }
  useEffect(() => { if (previewReady) setDirty([]) }, [previewReady])
  // keyboard: Space play/pause, J/K previous/next clip, Delete removes the selection
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const tag = (e.target as HTMLElement)?.tagName
      if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT') return
      if (!edl) return
      if (e.code === 'Space') { e.preventDefault(); const v = video.current; if (v && previewReady) { v.paused ? v.play() : v.pause() } }
      if (e.key === 'j' || e.key === 'k') {
        const starts = edl.clips.map(c => c.start)
        const cur = time
        const idx = e.key === 'k' ? starts.findIndex(s => s > cur + 0.05) : [...starts].reverse().findIndex(s => s < cur - 0.05)
        const t = e.key === 'k' ? (idx >= 0 ? starts[idx] : cur) : (idx >= 0 ? [...starts].reverse()[idx] : 0)
        seek(t + 0.001)
        const c = edl.clips.find(c => Math.abs(c.start - t) < 1e-6); if (c) setSelection({ kind: 'clip', id: c.id })
      }
      if ((e.key === 'Delete' || e.key === 'Backspace') && selection) { doOp(selection.kind === 'clip' ? 'remove_clip' : 'delete_item', { item_id: selection.id }); setSelection(null) }
      if ((e.metaKey || e.ctrlKey) && e.key === 'z') { e.preventDefault(); e.shiftKey ? redo.mutate() : undo.mutate() }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [edl, time, selection, previewReady, doOp, undo, redo])
  const p = project.data
  if (edlQ.isLoading || project.isLoading) return <div className="p-6 muted">Loading…</div>
  if (!edl) {
    return (
      <div className="p-6 flex flex-col gap-3 max-w-xl">
        <div className="card p-5">
          <div className="font-medium mb-1">No edit yet</div>
          <div className="muted text-sm mb-3">{planning ? `${planning.kind}: ${Math.round(planning.progress * 100)}% ${planning.message}` : p?.style === 'showdown' ? 'Fill in the Showdown stats sheet and build the edit.' : 'Add footage and run Analyze to build the first edit.'}</div>
          <div className="flex gap-2">
            {p?.style !== 'showdown' && <Link className="btn" to={`/p/${pid}/footage`}>Footage</Link>}
            {p?.style === 'showdown' && <Link className="btn" to={`/p/${pid}/showdown`}>Showdown</Link>}
            {snap?.project.status === 'error' && <span className="text-[#ff8a73] text-sm self-center">{snap.project.message}</span>}
          </div>
        </div>
      </div>
    )
  }
  const aspect = edl.aspect.replace(':', ' / ')
  return (
    <div className="h-full grid grid-rows-[1fr_auto] min-h-0">
      <div className="grid grid-cols-[minmax(280px,38%)_1fr_360px] min-h-0">
        <div className="p-3 flex flex-col gap-2 min-h-0 border-r" style={{ borderColor: 'var(--line)' }}>
          <div className="flex-1 min-h-0 flex items-center justify-center bg-black rounded-lg overflow-hidden relative">
            {previewReady && preview.data?.url ? (
              <video ref={video} src={abs(preview.data.url)} className="max-h-full max-w-full" style={{ aspectRatio: aspect }} controls onTimeUpdate={e => setTime((e.target as HTMLVideoElement).currentTime)} />
            ) : (
              <img src={frameUrl(pid, time, 540)} className="max-h-full max-w-full" style={{ aspectRatio: aspect }} alt="" />
            )}
            {!previewReady && <div className="absolute bottom-2 left-2 pill pill-warn">{previewJob ? `rendering preview ${Math.round(previewJob.progress * 100)}% · ${previewJob.message}` : 'preview not rendered: showing frames'}</div>}
          </div>
          <div className="flex flex-wrap gap-1.5">
            <button className="btn btn-sm" disabled={!edlQ.data?.can_undo} onClick={() => undo.mutate()}>↶ undo</button>
            <button className="btn btn-sm" disabled={!edlQ.data?.can_redo} onClick={() => redo.mutate()}>↷ redo</button>
            <button className="btn btn-sm" disabled={!!planning} onClick={() => plan.mutate({ keep_locks: true })} title="New random seed; pinned items stay">⟳ Regenerate</button>
            {edl.style !== 'showdown' && <button className="btn btn-sm" onClick={() => plan.mutate({ text_only: true })}>Regenerate text</button>}
            <button className="btn btn-sm" disabled={!!previewJob} onClick={() => renderPreview.mutate()}>Preview render</button>
            <button className="btn btn-sm btn-primary ml-auto" onClick={() => setShowExport(true)}>Export…</button>
          </div>
          <div className="text-xs muted flex gap-3 flex-wrap">
            <span>v{edlQ.data?.version}</span><span>{edl.duration.toFixed(1)}s</span><span>{edl.clips.length} clips</span><span>{edl.effects.length} effects</span>
            {edl.markers.drop != null && <span>drop @ {edl.markers.drop.toFixed(2)}s</span>}
            {edl.audio.sound_offset != null && <span>sound offset {Math.floor(edl.audio.sound_offset / 60)}:{String(Math.round(edl.audio.sound_offset % 60)).padStart(2, '0')}</span>}
            {err && <span className="text-[#ff8a73]">{err}</span>}
          </div>
          {edl.notes.length > 0 && <div className="text-xs" style={{ color: 'var(--accent)' }}>{edl.notes.join(' ')}</div>}
        </div>
        <div className="min-h-0 overflow-auto scroll border-r" style={{ borderColor: 'var(--line)', background: 'var(--panel)' }}>
          <Inspector pid={pid} edl={edl} selection={selection} onOp={doOp} onClose={() => setSelection(null)} />
        </div>
        <div className="min-h-0" style={{ background: 'var(--panel)' }}>
          <DirectorChat pid={pid} snap={snap} clips={clips.data ?? []} />
        </div>
      </div>
      <div className="border-t overflow-x-auto" style={{ borderColor: 'var(--line)', background: 'var(--panel)' }}>
        <Timeline edl={edl} grid={grid.data ?? null} time={time} selection={selection} onSelect={setSelection} onSeek={seek} dirty={dirty}
          onTrim={(itemId, inT, outT) => doOp('set_clip_range', { item_id: itemId, in: inT, out: outT })} />
      </div>
      {showExport && p && <ExportDialog pid={pid} project={p} snap={snap} onClose={() => setShowExport(false)} />}
    </div>
  )
}
