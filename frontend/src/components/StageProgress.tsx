import type { Snapshot } from '../hooks/useEvents'

const LABELS: Record<string, string> = { video: 'Video analysis', song: 'Song analysis' }

/** Separate progress for the video and the song while an analysis job runs (Tim: "so I can see progress is being made"). */
export default function StageProgress({ snap, compact }: { snap: Snapshot | null; compact?: boolean }) {
  const job = snap?.jobs.find(j => (j.status === 'running' || j.status === 'queued') && ['analyze', 'analyze_clips', 'plan', 'chat'].includes(j.kind)) ?? null
  if (!job) return null
  const stages = job.stages && Object.keys(job.stages).length ? job.stages : null
  const rows = stages ? Object.entries(stages) : [[job.kind, { progress: job.progress, message: job.message, state: job.status === 'queued' ? 'pending' : 'running' }] as const]
  return (
    <div className={`card ${compact ? 'p-2' : 'p-3'} flex flex-col gap-2 text-xs`}>
      {job.status === 'queued' && <div className="muted">Queued behind another job…</div>}
      {rows.map(([key, st]) => (
        <div key={key} className="flex flex-col gap-1">
          <div className="flex justify-between gap-2">
            <span className={st.state === 'pending' ? 'muted' : ''}>{LABELS[key] ?? (key === 'chat' ? (job.payload?.brief ? 'Director applying your brief' : 'Director') : key === 'plan' ? 'Building the edit' : key)}</span>
            <span className="muted truncate max-w-[60%]" title={st.message}>{st.state === 'done' ? 'done' : st.message}</span>
          </div>
          <div className="h-1.5 rounded bg-[#2a2a33] overflow-hidden"><div className="h-full rounded transition-all" style={{ width: `${Math.round(Math.max(0, Math.min(1, st.progress)) * 100)}%`, background: st.state === 'done' ? '#5fbf7f' : 'var(--accent)' }} /></div>
        </div>
      ))}
    </div>
  )
}
