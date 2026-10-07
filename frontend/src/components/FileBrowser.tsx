import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { api } from '../api'

const VIDEO = ['.mp4', '.mov', '.mkv', '.m4v', '.webm']
const AUDIO = ['.mp3', '.wav', '.m4a']
const IMAGE = ['.jpg', '.jpeg', '.png']

export default function FileBrowser({ kinds, onPick, onClose }: { kinds: ('video' | 'audio' | 'image')[]; onPick: (path: string) => void; onClose: () => void }) {
  const [path, setPath] = useState<string | undefined>(undefined)
  const listing = useQuery({ queryKey: ['browse', path], queryFn: () => api.browse(path) })
  const exts = kinds.flatMap(k => k === 'video' ? VIDEO : k === 'audio' ? AUDIO : IMAGE)
  const sep = listing.data?.path.includes('\\') ? '\\' : '/'
  return (
    <div className="fixed inset-0 bg-black/60 flex items-center justify-center z-50" onClick={onClose}>
      <div className="card w-[640px] max-h-[80vh] flex flex-col" onClick={e => e.stopPropagation()}>
        <div className="p-3 border-b flex items-center gap-2" style={{ borderColor: 'var(--line)' }}>
          <button className="btn btn-sm" disabled={!listing.data?.parent} onClick={() => setPath(listing.data?.parent ?? undefined)}>↑</button>
          <input className="input" value={listing.data?.path ?? ''} onChange={e => setPath(e.target.value)} />
          <button className="btn btn-sm" onClick={onClose}>close</button>
        </div>
        <div className="overflow-auto scroll p-2 text-sm">
          {listing.data?.dirs.map(d => <div key={d} className="px-2 py-1 rounded hover:bg-[#23232b] cursor-pointer" onClick={() => setPath(`${listing.data!.path}${sep}${d}`)}>📁 {d}</div>)}
          {listing.data?.files.filter(f => exts.some(e => f.name.toLowerCase().endsWith(e))).map(f => (
            <div key={f.name} className="px-2 py-1 rounded hover:bg-[#23232b] cursor-pointer flex" onClick={() => onPick(`${listing.data!.path}${sep}${f.name}`)}>
              <span>🎞 {f.name}</span><span className="ml-auto muted text-xs">{(f.size / 1e6).toFixed(1)} MB</span>
            </div>
          ))}
          {listing.error && <div className="text-[#ff8a73]">{(listing.error as Error).message}</div>}
        </div>
      </div>
    </div>
  )
}
