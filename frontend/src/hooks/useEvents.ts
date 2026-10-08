import { useEffect, useState } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { abs } from '../api'

export type Snapshot = {
  project: { id: string; status: string; progress: number; message: string; updated_at: string; claude_spend_usd: number; song_path: string | null }
  jobs: { id: string; kind: string; status: string; progress: number; message: string; error: string; result: any; stages?: Record<string, { progress: number; message: string; state: string }>; payload?: any }[]
  edl_version: number | null
  chat_count: number
}

/** Subscribe to the project's SSE stream and invalidate queries when state changes. */
export function useEvents(projectId: string | undefined) {
  const qc = useQueryClient()
  const [snap, setSnap] = useState<Snapshot | null>(null)
  useEffect(() => {
    if (!projectId) return
    const es = new EventSource(abs(`/api/projects/${projectId}/events`))
    let prev: Snapshot | null = null
    es.onmessage = (ev) => {
      const next = JSON.parse(ev.data) as Snapshot
      setSnap(next)
      if (prev) {
        if (prev.project.status !== next.project.status) qc.invalidateQueries({ queryKey: ['project', projectId] })
        if (prev.edl_version !== next.edl_version) {
          qc.invalidateQueries({ queryKey: ['edl', projectId] })
          qc.invalidateQueries({ queryKey: ['preview', projectId] })
          qc.invalidateQueries({ queryKey: ['project', projectId] })
        }
        if (prev.chat_count !== next.chat_count) qc.invalidateQueries({ queryKey: ['chat', projectId] })
        const finished = next.jobs.filter(j => (j.status === 'done' || j.status === 'error') && prev!.jobs.find(p => p.id === j.id)?.status !== j.status)
        for (const j of finished) {
          qc.invalidateQueries({ queryKey: ['project', projectId] })
          if (j.kind === 'preview') qc.invalidateQueries({ queryKey: ['preview', projectId] })
          if (j.kind === 'export') qc.invalidateQueries({ queryKey: ['exports', projectId] })
          if (j.kind === 'analyze' || j.kind === 'analyze_clips' || j.kind === 'import_stock') {
            qc.invalidateQueries({ queryKey: ['clips', projectId] }); qc.invalidateQueries({ queryKey: ['moments', projectId] })
            qc.invalidateQueries({ queryKey: ['beatgrid', projectId] }); qc.invalidateQueries({ queryKey: ['edl', projectId] })
          }
          if (j.kind === 'showdown_stats') qc.invalidateQueries({ queryKey: ['showdown', projectId] })
          if (j.kind === 'chat') { qc.invalidateQueries({ queryKey: ['chat', projectId] }); qc.invalidateQueries({ queryKey: ['edl', projectId] }) }
          if (j.kind.startsWith('documentary')) { qc.invalidateQueries({ queryKey: ['documentary', projectId] }); qc.invalidateQueries({ queryKey: ['projects'] }) }
        }
      }
      prev = next
    }
    return () => es.close()
  }, [projectId, qc])
  return snap
}

export function activeJob(snap: Snapshot | null, kinds?: string[]) {
  if (!snap) return null
  return snap.jobs.find(j => (j.status === 'queued' || j.status === 'running') && (!kinds || kinds.includes(j.kind))) ?? null
}
