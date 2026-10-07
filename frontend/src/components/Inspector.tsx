import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { api, clipFrameUrl, type Edl, type Moment } from '../api'
import type { Selection } from './Timeline'

export default function Inspector({ pid, edl, selection, onOp, onClose }: { pid: string; edl: Edl; selection: Selection; onOp: (op: string, args: Record<string, unknown>) => void; onClose: () => void }) {
  if (!selection) return <div className="p-3 muted text-xs">Click a clip, effect marker, or the title to edit it. Space play/pause · J/K previous/next clip · Delete removes the selection.</div>
  if (selection.kind === 'clip') return <ClipInspector pid={pid} edl={edl} id={selection.id} onOp={onOp} onClose={onClose} />
  if (selection.kind === 'effect') return <EffectInspector edl={edl} id={selection.id} onOp={onOp} onClose={onClose} />
  return <TextInspector edl={edl} id={selection.id} onOp={onOp} onClose={onClose} />
}

function Header({ title, onClose }: { title: string; onClose: () => void }) {
  return <div className="flex items-center mb-2"><div className="font-medium">{title}</div><button className="ml-auto btn btn-sm" onClick={onClose}>close</button></div>
}

function ClipInspector({ pid, edl, id, onOp, onClose }: { pid: string; edl: Edl; id: string; onOp: (op: string, args: Record<string, unknown>) => void; onClose: () => void }) {
  const c = edl.clips.find(x => x.id === id)
  const moments = useQuery({ queryKey: ['moments', pid], queryFn: () => api.moments(pid) })
  const [inT, setIn] = useState<string | null>(null)
  const [outT, setOut] = useState<string | null>(null)
  if (!c) return null
  if (c.kind === 'card') return <div className="p-3 text-xs"><Header title={`${c.card?.type} card`} onClose={onClose} /><div className="muted">Showdown cards are generated from the stats sheet and layout. Edit values on the Showdown tab.</div></div>
  const locked = c.locked_order || c.locked_range
  const alternatives = (moments.data ?? []).filter(m => m.id !== c.moment_id && !m.banned).slice(0, 24)
  return (
    <div className="p-3 text-xs flex flex-col gap-3">
      <Header title={`${c.label} · ${c.role}`} onClose={onClose} />
      <div className="muted">{c.caption_hint || c.action || ''} {c.species ? `· ${c.species}` : ''}</div>
      <div className="grid grid-cols-2 gap-2">
        <label>in (s)<input className="input" value={inT ?? c.in.toFixed(2)} onChange={e => setIn(e.target.value)} onBlur={() => { if (inT != null) { onOp('set_clip_range', { item_id: c.id, in: Number(inT) }); setIn(null) } }} /></label>
        <label>out (s)<input className="input" value={outT ?? c.out.toFixed(2)} onChange={e => setOut(e.target.value)} onBlur={() => { if (outT != null) { onOp('set_clip_range', { item_id: c.id, out: Number(outT) }); setOut(null) } }} /></label>
      </div>
      <div className="flex flex-wrap gap-1.5">
        <button className="btn btn-sm" onClick={() => onOp('set_lock', { item_id: c.id, locked: !locked })}>{locked ? '📌 unpin' : 'pin'}</button>
        <button className="btn btn-sm" onClick={() => onOp('set_speed_ramp', { item_id: c.id, slow_rate: c.speed ? null : 0.4 })}>{c.speed ? 'remove ramp' : 'add slow-mo ramp'}</button>
        {c.speed && <select className="input w-24" value={Math.min(...c.speed.map(k => k.rate))} onChange={e => onOp('set_speed_ramp', { item_id: c.id, slow_rate: Number(e.target.value) })}>{[0.3, 0.4, 0.5, 0.7].map(r => <option key={r} value={r}>{r}x</option>)}</select>}
        <button className="btn btn-sm" onClick={() => onOp('set_enabled', { item_id: c.id, enabled: !c.enabled })}>{c.enabled ? 'disable' : 'enable'}</button>
        <button className="btn btn-sm btn-danger" onClick={() => onOp('remove_clip', { item_id: c.id })}>remove</button>
      </div>
      <div className="font-medium mt-1">Swap for another moment</div>
      <div className="grid grid-cols-2 gap-2 max-h-[46vh] overflow-auto scroll pr-1">
        {alternatives.map((m: Moment) => (
          <button key={m.id} className="card p-1 text-left hover:border-[var(--accent)]" onClick={() => onOp('swap_moment', { item_id: c.id, moment_id: m.id })} title={`${m.caption_hint} score ${m.score}`}>
            <div className="aspect-video bg-black/50 rounded overflow-hidden"><img src={m.thumb_url ?? clipFrameUrl(pid, m.clip_id, m.peak_t)} className="w-full h-full object-cover" loading="lazy" alt="" /></div>
            <div className="truncate mt-1">{edl.clips.find(x => x.clip_id === m.clip_id)?.label ?? ''} {m.action} @ {m.peak_t.toFixed(1)}s</div>
            <div className="muted truncate">{m.caption_hint || `score ${m.score.toFixed(2)}`}{m.starred ? ' ★' : ''}</div>
          </button>
        ))}
      </div>
    </div>
  )
}

function EffectInspector({ edl, id, onOp, onClose }: { edl: Edl; id: string; onOp: (op: string, args: Record<string, unknown>) => void; onClose: () => void }) {
  const f = edl.effects.find(x => x.id === id)
  if (!f) return null
  return (
    <div className="p-3 text-xs flex flex-col gap-3">
      <Header title={`${f.type} @ ${f.t.toFixed(2)}s`} onClose={onClose} />
      <div className="flex flex-wrap gap-1.5 items-center">
        <button className="btn btn-sm" onClick={() => onOp('toggle_effect', { item_id: f.id })}>{f.enabled ? 'turn off' : 'turn on'}</button>
        <span className="muted">intensity</span>
        {(['low', 'med', 'high'] as const).map(l => <button key={l} className={`btn btn-sm ${f.intensity === l ? 'border-[var(--accent)]' : ''}`} onClick={() => onOp('set_effect_intensity', { item_id: f.id, intensity: l })}>{l}</button>)}
        <button className="btn btn-sm" onClick={() => onOp('set_lock', { item_id: f.id, locked: !f.locked })}>{f.locked ? '📌 unpin' : 'pin'}</button>
        <button className="btn btn-sm btn-danger" onClick={() => onOp('delete_item', { item_id: f.id })}>delete</button>
      </div>
      <div className="muted">params: {JSON.stringify(f.params)}</div>
      <div className="font-medium">All effects</div>
      <div className="flex gap-1.5">{(['low', 'med', 'high'] as const).map(l => <button key={l} className={`btn btn-sm ${edl.intensity === l ? 'border-[var(--accent)]' : ''}`} onClick={() => onOp('set_all_intensity', { intensity: l })}>{l}</button>)}</div>
    </div>
  )
}

function TextInspector({ edl, id, onOp, onClose }: { edl: Edl; id: string; onOp: (op: string, args: Record<string, unknown>) => void; onClose: () => void }) {
  const t = edl.text.find(x => x.id === id)
  const [text, setText] = useState<string | null>(null)
  const [time, setTime] = useState<string | null>(null)
  const [dur, setDur] = useState<string | null>(null)
  if (!t) return null
  return (
    <div className="p-3 text-xs flex flex-col gap-3">
      <Header title="Title" onClose={onClose} />
      <label>text<input className="input" style={{ fontFamily: 'serif', letterSpacing: 2 }} value={text ?? t.text} onChange={e => setText(e.target.value.toUpperCase())} onBlur={() => { if (text != null && text !== t.text) onOp('set_text', { text }); setText(null) }} onKeyDown={e => e.key === 'Enter' && (e.target as HTMLInputElement).blur()} /></label>
      <div className="grid grid-cols-2 gap-2">
        <label>appears at (s)<input className="input" value={time ?? t.t.toFixed(2)} onChange={e => setTime(e.target.value)} onBlur={() => { if (time != null) onOp('set_text', { t: Number(time) }); setTime(null) }} /></label>
        <label>holds (s)<input className="input" value={dur ?? t.duration.toFixed(2)} onChange={e => setDur(e.target.value)} onBlur={() => { if (dur != null) onOp('set_text', { duration: Number(dur) }); setDur(null) }} /></label>
      </div>
      <div className="flex gap-1.5">
        {edl.markers.drop != null && <button className="btn btn-sm" onClick={() => onOp('set_text', { t: edl.markers.drop })}>put on the drop</button>}
        <button className="btn btn-sm" onClick={() => onOp('set_lock', { item_id: t.id, locked: !t.locked })}>{t.locked ? '📌 unpin' : 'pin'}</button>
        <button className="btn btn-sm btn-danger" onClick={() => onOp('delete_item', { item_id: t.id })}>delete</button>
      </div>
      <div className="muted">Rule: the only text in a single-animal edit is the animal's name.</div>
    </div>
  )
}
