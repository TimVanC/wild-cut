import { useState } from 'react'
import { useMutation, useQuery } from '@tanstack/react-query'
import { useNavigate, useSearchParams } from 'react-router-dom'
import { api, isRemote } from '../api'

export default function NewProject() {
  const nav = useNavigate()
  const [params] = useSearchParams()
  const isDoc = params.get('kind') === 'documentary'
  const cfg = useQuery({ queryKey: ['config'], queryFn: api.config })
  const [name, setName] = useState('')
  const [style, setStyle] = useState('phonk')
  const [aspect, setAspect] = useState<string>('')
  const [length, setLength] = useState('30')
  const [mode, setMode] = useState<'music' | 'visual'>('music')
  const [audio, setAudio] = useState<'silent' | 'mixed' | 'original'>('silent')
  const [intensity, setIntensity] = useState('med')
  const [docPath, setDocPath] = useState('')
  const [animal, setAnimal] = useState('')
  const [subject, setSubject] = useState('')
  const [action, setAction] = useState('')
  const [edits, setEdits] = useState('as_many')
  const create = useMutation({
    mutationFn: async () => {
      const preset = cfg.data?.presets.find(p => p.id === style)
      const options: Record<string, any> = { intensity }
      if (!isDoc && subject.trim()) options.focus = { subject: subject.trim(), action: action.trim() }
      if (isDoc) options.documentary = { path: docPath.trim() || undefined, animal: animal.trim() || 'auto', edits, target_min: 60, target_max: 70 }
      return api.createProject({ name: name.trim() || (isDoc ? 'Documentary edits' : 'Untitled'), style, aspect: aspect || preset?.default_aspect, target_length: isDoc ? '65' : length, mode, audio_export: audio, options } as any)
    },
    onSuccess: (p) => nav(`/p/${p.id}/${isDoc ? 'documentary' : style === 'showdown' ? 'showdown' : 'footage'}`),
  })
  const presets = (cfg.data?.presets ?? []).filter(p => !isDoc || p.structure === 'single')
  const chosen = presets.find(p => p.id === style)
  return (
    <div className="max-w-xl mx-auto p-6">
      <h1 className="text-lg font-semibold mb-4">{isDoc ? 'New edits from a documentary' : 'New project'}</h1>
      <div className="card p-5 flex flex-col gap-4">
        <div><label className="label">Name</label><input className="input" value={name} onChange={e => setName(e.target.value)} placeholder={isDoc ? 'Cheetahs of the Serengeti' : 'The Gibbon'} autoFocus /></div>
        {isDoc && (<>
          {isRemote() ? (
            <div className="card p-3 text-sm muted">Next screen: click <b style={{ color: 'var(--text)' }}>Upload film</b> to pick the documentary from your computer, and <b style={{ color: 'var(--text)' }}>Upload song</b> for your phonk track.</div>
          ) : (
            <div><label className="label">Documentary file (optional here: local path or a file in inbox/; you can also upload it on the next screen)</label><input className="input" value={docPath} onChange={e => setDocPath(e.target.value)} placeholder="/Users/tim/Movies/cheetah_documentary.mkv" /></div>
          )}
          <div className="grid grid-cols-2 gap-3">
            <div><label className="label">Animal</label><input className="input" value={animal} onChange={e => setAnimal(e.target.value)} placeholder="auto-detect" /></div>
            <div><label className="label">Number of edits</label><select className="input" value={edits} onChange={e => setEdits(e.target.value)}><option value="1">1</option><option value="2">2</option><option value="3">3</option><option value="as_many">as many as the footage supports</option></select></div>
          </div>
        </>)}
        {!isDoc && (
          <div>
            <label className="label">This edit is about (you can change it later on the Footage tab)</label>
            <div className="grid grid-cols-2 gap-2">
              <input className="input" placeholder="the animal, e.g. iguana" value={subject} onChange={e => setSubject(e.target.value)} />
              <input className="input" placeholder="what it does, e.g. escapes the snakes" value={action} onChange={e => setAction(e.target.value)} />
            </div>
          </div>
        )}
        <div>
          <label className="label">Style</label>
          <div className="grid grid-cols-2 gap-2">
            {presets.map(p => (
              <button key={p.id} onClick={() => { setStyle(p.id); setAspect('') }} className={`text-left rounded-lg border p-3 transition ${style === p.id ? 'border-[var(--accent)] bg-[#1f1b14]' : 'border-[var(--line)] hover:border-[#3a3a46]'}`}>
                <div className="font-medium">{p.name} {p.id === 'phonk' && <span className="pill pill-warn ml-1">default</span>}</div>
                <div className="text-xs muted mt-1 leading-snug">{p.description}</div>
              </button>
            ))}
          </div>
        </div>
        <div className="grid grid-cols-3 gap-3">
          <div><label className="label">Aspect</label><select className="input" value={aspect || chosen?.default_aspect || '9:16'} onChange={e => setAspect(e.target.value)}>{(cfg.data?.aspects ?? ['9:16', '1:1', '4:5', '3:4']).map(a => <option key={a}>{a}</option>)}</select></div>
          {!isDoc && <div><label className="label">Length</label><select className="input" value={length} onChange={e => setLength(e.target.value)}>{(cfg.data?.target_lengths ?? ['15', '30', '45', '60', 'song']).map(l => <option key={l} value={l}>{l === 'song' ? 'match song' : `${l} s`}</option>)}</select></div>}
          {isDoc && <div><label className="label">Length</label><div className="input muted">60 to 70 s</div></div>}
          <div><label className="label">Intensity</label><select className="input" value={intensity} onChange={e => setIntensity(e.target.value)}><option value="low">low</option><option value="med">med</option><option value="high">high</option></select></div>
        </div>
        <div className="grid grid-cols-2 gap-3">
          <div><label className="label">Timing</label><select className="input" value={mode} onChange={e => { const m = e.target.value as any; setMode(m); if (m === 'visual' && audio === 'mixed') setAudio('silent') }}><option value="music">Music-synced (I provide a phonk track)</option><option value="visual">Visual peaks (no music)</option></select></div>
          <div><label className="label">Export audio</label><select className="input" value={audio} onChange={e => setAudio(e.target.value as any)}><option value="silent">Silent (attach the sound on TikTok)</option>{mode === 'music' && <option value="mixed">Song mixed in</option>}{mode === 'visual' && !isDoc && <option value="original">Original clip audio</option>}</select></div>
        </div>
        <div className="flex gap-2 justify-end">
          <button className="btn" onClick={() => nav('/')}>Cancel</button>
          <button className="btn btn-primary" disabled={create.isPending} onClick={() => create.mutate()}>{isDoc ? 'Create (upload the film next)' : 'Create'}</button>
        </div>
        {create.error && <div className="text-[#ff8a73] text-sm">{String((create.error as Error).message)}</div>}
      </div>
    </div>
  )
}
