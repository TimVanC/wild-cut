import { useEffect, useMemo, useRef, useState } from 'react'
import type { BeatGrid, Edl, EdlClip, EdlEffect, EdlText } from '../api'

export type Selection = { kind: 'clip'; id: string } | { kind: 'effect'; id: string } | { kind: 'text'; id: string } | null

const FX_COLORS: Record<string, string> = { shake: '#ff5a3c', flash: '#ffffff', chromatic: '#7fd0ff', zoom_punch: '#f2b53a', glitch: '#c57bff', push_in: '#58c27d', fade_black: '#777', motion_blur: '#ffa54a' }

export default function Timeline({ edl, grid, time, selection, onSelect, onSeek, onTrim, dirty }: {
  edl: Edl; grid: BeatGrid | null; time: number; selection: Selection; onSelect: (s: Selection) => void; onSeek: (t: number) => void
  onTrim: (itemId: string, inT: number | undefined, outT: number | undefined) => void; dirty: [number, number][]
}) {
  const wrap = useRef<HTMLDivElement>(null)
  const [width, setWidth] = useState(800)
  useEffect(() => {
    if (!wrap.current) return
    const ro = new ResizeObserver(e => setWidth(e[0].contentRect.width))
    ro.observe(wrap.current)
    return () => ro.disconnect()
  }, [])
  const dur = Math.max(0.01, edl.duration)
  const px = (t: number) => (t / dur) * width
  const [drag, setDrag] = useState<null | { id: string; edge: 'in' | 'out'; x0: number; clip: EdlClip }>(null)
  const [dragDelta, setDragDelta] = useState(0)
  const onMove = (e: React.MouseEvent) => { if (drag) setDragDelta(((e.clientX - drag.x0) / width) * dur) }
  const onUp = () => {
    if (drag) {
      const c = drag.clip
      if (drag.edge === 'in') onTrim(c.id, Math.max(0, c.in + dragDelta), undefined)
      else onTrim(c.id, undefined, Math.max(c.in + 0.2, c.out + dragDelta))
    }
    setDrag(null); setDragDelta(0)
  }
  const seek = (e: React.MouseEvent) => { const r = wrap.current!.getBoundingClientRect(); onSeek(Math.max(0, Math.min(dur, ((e.clientX - r.left) / r.width) * dur))) }
  const wave = useMemo(() => {
    if (!grid || !edl.audio.song_window) return null
    const { start, end } = edl.audio.song_window
    const n = grid.waveform.length
    const i0 = Math.floor((start / grid.duration) * n), i1 = Math.ceil((end / grid.duration) * n)
    return grid.waveform.slice(i0, i1)
  }, [grid, edl.audio.song_window])
  const ticks = useMemo(() => { const out = []; const step = dur > 40 ? 5 : dur > 15 ? 2 : 1; for (let t = 0; t <= dur; t += step) out.push(t); return out }, [dur])
  const Row = ({ label, h, children }: { label: string; h: number; children: React.ReactNode }) => (
    <div className="flex border-b" style={{ borderColor: 'var(--line)', height: h }}>
      <div className="w-16 shrink-0 text-[11px] uppercase tracking-wider muted pl-2 pt-1 border-r" style={{ borderColor: 'var(--line)' }}>{label}</div>
      <div className="relative flex-1">{children}</div>
    </div>
  )
  return (
    <div className="select-none text-xs" onMouseMove={onMove} onMouseUp={onUp} onMouseLeave={() => drag && onUp()}>
      <div className="flex">
        <div className="w-16 shrink-0" />
        <div ref={wrap} className="relative flex-1 h-5 cursor-pointer" onClick={seek}>
          {ticks.map(t => <span key={t} className="absolute top-0 muted" style={{ left: px(t), fontSize: 10 }}>{t}s</span>)}
          {edl.sections.map(s => <span key={s.name} className="absolute bottom-0 text-[9px] uppercase" style={{ left: px(s.start), color: 'var(--accent)' }}>{s.name}</span>)}
        </div>
      </div>
      <div className="relative">
        <Row label="clips" h={58}>
          {edl.clips.map(c => {
            const isSel = selection?.kind === 'clip' && selection.id === c.id
            const left = px(c.start), w = Math.max(2, px(c.tl_duration))
            const peak = c.peak != null && c.speed ? null : null
            return (
              <div key={c.id} className={`tl-clip ${isSel ? 'selected' : ''} ${c.role === 'hero' ? 'hero' : ''} ${c.kind === 'card' ? 'card' : ''}`} style={{ left, width: w, opacity: c.enabled ? 1 : 0.35 }}
                onClick={(e) => { e.stopPropagation(); onSelect({ kind: 'clip', id: c.id }); onSeek(c.start + 0.01) }} title={`${c.label} ${c.in.toFixed(2)}-${c.out.toFixed(2)}s (${c.role})`}>
                <div className="px-1.5 pt-1 truncate font-medium">{c.kind === 'card' ? `${c.card?.type} card` : c.label}{(c.locked_order || c.locked_range) && <span className="ml-1" title="pinned">📌</span>}</div>
                <div className="px-1.5 muted truncate">{c.role}{c.speed ? ' · ramp' : ''}{c.caption_hint ? ` · ${c.caption_hint}` : ''}</div>
                {c.kind !== 'card' && <>
                  <div className="tl-handle left-0" onMouseDown={e => { e.stopPropagation(); setDrag({ id: c.id, edge: 'in', x0: e.clientX, clip: c }) }} />
                  <div className="tl-handle right-0" onMouseDown={e => { e.stopPropagation(); setDrag({ id: c.id, edge: 'out', x0: e.clientX, clip: c }) }} />
                </>}
                {peak}
              </div>
            )
          })}
          {drag && <div className="absolute top-0 bottom-0 pointer-events-none" style={{ left: px(drag.edge === 'in' ? drag.clip.start : drag.clip.start + drag.clip.tl_duration) + (dragDelta / dur) * width * (drag.edge === 'in' ? -1 : 1), width: 1, background: 'var(--accent)' }} />}
        </Row>
        <Row label="effects" h={26}>
          {edl.effects.map((f: EdlEffect) => (
            <div key={f.id} className={`tl-marker ${f.enabled ? '' : 'off'} ${selection?.kind === 'effect' && selection.id === f.id ? 'selected' : ''}`} style={{ left: px(f.t), background: FX_COLORS[f.type] ?? '#aaa' }}
              title={`${f.type} @ ${f.t.toFixed(2)}s (${f.intensity})${f.locked ? ' pinned' : ''}`} onClick={e => { e.stopPropagation(); onSelect({ kind: 'effect', id: f.id }); onSeek(f.t) }} />
          ))}
          {edl.effects.filter(f => f.type === 'push_in').map(f => <div key={`${f.id}b`} className="absolute top-2 h-1 rounded pointer-events-none" style={{ left: px(f.t), width: px(f.duration), background: 'rgba(88,194,125,.35)' }} />)}
        </Row>
        <Row label="text" h={28}>
          {edl.text.map((t: EdlText) => (
            <div key={t.id} className={`absolute top-1 bottom-1 rounded px-1.5 cursor-pointer truncate ${selection?.kind === 'text' && selection.id === t.id ? 'outline outline-2 outline-[var(--accent)]' : ''}`}
              style={{ left: px(t.t), width: Math.max(24, px(t.duration)), background: '#2b2b36', opacity: t.enabled ? 1 : .35, fontFamily: 'serif', letterSpacing: 1 }}
              onClick={e => { e.stopPropagation(); onSelect({ kind: 'text', id: t.id }); onSeek(t.t + 0.1) }} title={`${t.text} @ ${t.t.toFixed(2)}s`}>
              {t.text}{t.locked && ' 📌'}
            </div>
          ))}
        </Row>
        <Row label="audio" h={54}>
          {wave && <svg className="absolute inset-0 w-full h-full" preserveAspectRatio="none" viewBox={`0 0 ${wave.length} 100`}>
            {wave.map((v, i) => <rect key={i} x={i} y={50 - v * 45} width={1} height={v * 90} fill="#4a4a58" />)}
          </svg>}
          {!wave && <div className="absolute inset-0 flex items-center pl-2 muted">{edl.mode === 'visual' ? 'visual-peaks mode: no music' : 'no song window'}</div>}
          {edl.markers.beats.map((b, i) => <div key={i} className="absolute bottom-0" style={{ left: px(b), width: 1, height: edl.markers.downbeats.includes(b) ? 14 : 7, background: 'rgba(255,255,255,.4)' }} />)}
          {edl.markers.bass_hits.map((b, i) => <div key={`h${i}`} className="absolute top-0" style={{ left: px(b), width: 2, height: 8, background: '#ff5a3c' }} />)}
          {edl.markers.drop != null && <div className="absolute top-0 bottom-0" style={{ left: px(edl.markers.drop), width: 2, background: 'var(--accent)' }}><span className="absolute top-0 left-1 text-[10px]" style={{ color: 'var(--accent)' }}>DROP</span></div>}
          {edl.markers.hero_peak != null && edl.markers.drop == null && <div className="absolute top-0 bottom-0" style={{ left: px(edl.markers.hero_peak), width: 2, background: 'var(--accent)' }}><span className="absolute top-0 left-1 text-[10px]" style={{ color: 'var(--accent)' }}>PEAK</span></div>}
        </Row>
        {dirty.map(([a, b], i) => <div key={i} className="absolute top-0 bottom-0 pointer-events-none" style={{ left: 64 + px(a), width: px(b - a), background: 'rgba(242,181,58,.07)' }} />)}
        <div className="absolute top-0 bottom-0 pointer-events-none" style={{ left: 64 + px(time), width: 2, background: '#fff', boxShadow: '0 0 4px #fff' }} />
      </div>
    </div>
  )
}
