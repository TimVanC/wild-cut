import { useEffect, useRef, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { abs, api, type Clip } from '../api'
import { activeJob, type Snapshot } from '../hooks/useEvents'

export default function DirectorChat({ pid, snap, clips }: { pid: string; snap: Snapshot | null; clips: Clip[] }) {
  const qc = useQueryClient()
  const history = useQuery({ queryKey: ['chat', pid], queryFn: () => api.chat(pid) })
  const [text, setText] = useState('')
  const [pending, setPending] = useState<string | null>(null)
  const send = useMutation({ mutationFn: (m: string) => api.sendChat(pid, m), onMutate: (m) => setPending(m), onError: () => setPending(null) })
  const upload = useMutation({ mutationFn: (files: File[]) => api.uploadClips(pid, files), onSuccess: () => { qc.invalidateQueries({ queryKey: ['clips', pid] }) } })
  const job = activeJob(snap, ['chat'])
  const analyzing = activeJob(snap, ['analyze_clips', 'analyze'])
  const bottom = useRef<HTMLDivElement>(null)
  useEffect(() => { bottom.current?.scrollIntoView({ behavior: 'smooth' }) }, [history.data?.length, pending, job?.message])
  useEffect(() => { if (!job && pending && history.data?.some(m => m.role === 'user' && m.content === pending)) setPending(null) }, [job, pending, history.data])
  const submit = () => { const m = text.trim(); if (!m) return; send.mutate(m); setText('') }
  const onDrop = (e: React.DragEvent) => { e.preventDefault(); const f = Array.from(e.dataTransfer.files).filter(f => f.type.startsWith('video/') || f.type.startsWith('image/')); if (f.length) upload.mutate(f) }
  return (
    <div className="h-full flex flex-col" onDragOver={e => e.preventDefault()} onDrop={onDrop}>
      <div className="px-3 py-2 border-b flex items-center gap-2" style={{ borderColor: 'var(--line)' }}>
        <span className="font-medium">Director</span>
        <span className="muted text-xs">drop clips here · "clip 2 first, title when it lets go of the branch, then clip 3"</span>
      </div>
      <div className="flex-1 overflow-auto scroll p-3 flex flex-col gap-2 text-sm">
        {clips.length > 0 && (
          <div className="flex gap-2 overflow-x-auto pb-1">
            {clips.map(c => (
              <div key={c.id} className="shrink-0 w-28 text-[11px]">
                <div className="aspect-video rounded bg-black/50 overflow-hidden">{c.thumb_url && <img src={abs(c.thumb_url)} className="w-full h-full object-cover" alt="" />}</div>
                <div className="truncate mt-0.5"><b>{c.label}</b> {c.analyzed ? '' : <span className="muted">(analyzing)</span>}</div>
                <div className="muted truncate" title={c.description}>{c.description || '…'}</div>
              </div>
            ))}
          </div>
        )}
        {history.data?.map(m => (
          <div key={m.id} className={`max-w-[92%] rounded-lg px-3 py-2 ${m.role === 'user' ? 'self-end' : 'self-start'}`} style={{ background: m.role === 'user' ? '#2a2518' : 'var(--panel-2)' }}>
            {m.content && <div className="whitespace-pre-wrap">{m.content}</div>}
            {m.tools.length > 0 && <div className="flex flex-wrap gap-1 mt-1">{m.tools.map((t, i) => <span key={i} className="pill">{t}</span>)}</div>}
          </div>
        ))}
        {pending && !history.data?.some(m => m.role === 'user' && m.content === pending) && <div className="self-end max-w-[92%] rounded-lg px-3 py-2" style={{ background: '#2a2518' }}>{pending}</div>}
        {job && <div className="self-start muted text-xs">Director is working… {job.message}</div>}
        {analyzing && <div className="self-start muted text-xs">Analyzing new clips… {Math.round(analyzing.progress * 100)}% {analyzing.message}</div>}
        {upload.isPending && <div className="muted text-xs">Uploading…</div>}
        <div ref={bottom} />
      </div>
      <div className="p-2 border-t flex gap-2" style={{ borderColor: 'var(--line)' }}>
        <textarea className="input resize-none" rows={2} placeholder="Direct the edit…" value={text} onChange={e => setText(e.target.value)} onKeyDown={e => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); submit() } }} />
        <button className="btn btn-primary self-end" disabled={!text.trim() || !!job} onClick={submit}>Send</button>
      </div>
    </div>
  )
}
