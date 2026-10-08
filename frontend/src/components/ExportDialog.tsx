import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { abs, api, fmtShort, type Project } from '../api'
import { activeJob, type Snapshot } from '../hooks/useEvents'

export default function ExportDialog({ pid, project, snap, onClose }: { pid: string; project: Project; snap: Snapshot | null; onClose: () => void }) {
  const qc = useQueryClient()
  const [quality, setQuality] = useState('full')
  const [audio, setAudio] = useState<string>(project.audio_export)
  const [err, setErr] = useState('')
  const exports = useQuery({ queryKey: ['exports', pid], queryFn: () => api.exports(pid) })
  const start = useMutation({ mutationFn: () => api.exportProject(pid, { quality, audio }), onError: (e: Error) => setErr(e.message), onSuccess: () => setErr('') })
  const job = activeJob(snap, ['export'])
  const lastErr = snap?.jobs.find(j => j.kind === 'export' && j.status === 'error')
  const latest = exports.data?.[0]
  const copy = (t: string) => navigator.clipboard.writeText(t)
  return (
    <div className="fixed inset-0 bg-black/60 flex items-center justify-center z-50" onClick={onClose}>
      <div className="card w-[720px] max-h-[88vh] overflow-auto scroll p-5 flex flex-col gap-4" onClick={e => e.stopPropagation()}>
        <div className="flex items-center"><h2 className="font-medium text-base">Export</h2><button className="ml-auto btn btn-sm" onClick={onClose}>close</button></div>
        <div className="grid grid-cols-2 gap-3">
          <label><span className="label">Resolution</span><select className="input" value={quality} onChange={e => setQuality(e.target.value)}><option value="full">1080 wide (final)</option><option value="preview">540 wide (fast check)</option></select></label>
          <label><span className="label">Audio</span><select className="input" value={audio} onChange={e => setAudio(e.target.value)}>
            <option value="silent">Silent (attach the sound on TikTok)</option>
            {project.mode === 'music' && <option value="mixed">Song mixed in</option>}
            {project.mode === 'visual' && <option value="original">Original clip audio</option>}
          </select></label>
        </div>
        <div className="flex items-center gap-3">
          <button className="btn btn-primary" disabled={!!job || start.isPending} onClick={() => start.mutate()}>{job ? 'Rendering…' : 'Render MP4'}</button>
          {job && <div className="flex-1"><div className="h-2 rounded bg-[#23232b] overflow-hidden"><div className="h-full" style={{ width: `${Math.round(job.progress * 100)}%`, background: 'var(--accent)' }} /></div><div className="muted text-xs mt-1">{Math.round(job.progress * 100)}% {job.message}</div></div>}
          {err && <span className="text-[#ff8a73] text-xs">{err}</span>}
          {!job && lastErr && !err && <span className="text-[#ff8a73] text-xs">{lastErr.error}</span>}
        </div>
        {latest && !job && (
          <div className="flex flex-col gap-3">
            <video src={abs(latest.url)} controls className="w-full max-h-[42vh] bg-black rounded" />
            <div className="text-xs muted truncate" title={latest.path}>{latest.path}</div>
            {latest.sound_offset != null && <div className="card p-3 text-sm" style={{ borderColor: '#5a4518' }}>Silent export: on TikTok, add the same sound and <b style={{ color: 'var(--accent)' }}>start it at {fmtShort(latest.sound_offset)}</b> ({latest.sound_offset.toFixed(2)} s into the track). <button className="btn btn-sm ml-2" onClick={() => copy(fmtShort(latest.sound_offset!))}>copy offset</button></div>}
            <div className="card p-3 text-xs whitespace-pre-wrap">{latest.caption}</div>
            <div className="flex gap-2">
              <button className="btn btn-sm" onClick={() => copy(latest.caption)}>copy caption</button>
              <button className="btn btn-sm" onClick={() => api.reveal(latest.path)}>reveal in folder</button>
              <span className="muted text-xs self-center">credits.txt, caption.txt{project.style === 'showdown' ? ', stats.csv' : ''} saved next to the MP4</span>
              <button className="ml-auto btn btn-sm" onClick={() => qc.invalidateQueries({ queryKey: ['exports', pid] })}>refresh</button>
            </div>
          </div>
        )}
        {exports.data && exports.data.length > 1 && <div className="text-xs muted">{exports.data.length} exports so far.</div>}
      </div>
    </div>
  )
}
