// Typed API client for the Wild Cut backend.

export type Project = {
  id: string; name: string; style: string; aspect: string; target_length: string; mode: 'music' | 'visual'
  audio_export: 'silent' | 'mixed' | 'original'; song_path: string | null; song_window: { start: number; end: number } | null
  seed: number; status: string; progress: number; message: string; options: Record<string, any>; claude_spend_usd: number
  clip_count: number; thumb_url: string | null; last_export: ExportRow | null; edl_version: number | null; song_url: string | null
  budget: { spent: number; limit: number }; updated_at: string; created_at: string
}
export type Clip = {
  id: string; label: string; order: number; source: string; path: string; proxy_path: string | null; proxy_url: string | null
  thumb_url: string | null; duration: number; width: number; height: number; description: string; analyzed: boolean
  tags: Record<string, any>; credit: string | null; license: string | null; source_url: string | null
}
export type Moment = {
  id: string; clip_id: string; in_t: number; out_t: number; peak_t: number; motion_score: number; species: string; action: string
  intensity: number; framing: number; subject_visible: boolean | null; caption_hint: string; score: number; kind: string
  category: string; starred: boolean; banned: boolean; thumb_url: string | null
}
export type EdlClip = {
  id: string; clip_id: string | null; moment_id: string | null; label: string; src: string; proxy: string; in: number; out: number
  start: number; tl_duration: number; speed: { t: number; rate: number }[] | null; role: string; locked_order: boolean
  locked_range: boolean; anchor: string | null; peak: number | null; enabled: boolean; species?: string; action?: string
  caption_hint?: string; kind?: string; card?: any
}
export type EdlEffect = { id: string; type: string; t: number; duration: number; intensity: string; enabled: boolean; locked: boolean; params: Record<string, any> }
export type EdlText = { id: string; text: string; t: number; duration: number; animation: string; locked: boolean; enabled: boolean; style: Record<string, any>; kind?: string }
export type Edl = {
  version: number; project_id: string; style: string; aspect: string; fps: number; seed: number; mode: string; duration: number
  intensity: string; audio: { export: string; song_path: string | null; song_window: { start: number; end: number } | null; sound_offset: number | null; window_locked: boolean }
  clips: EdlClip[]; effects: EdlEffect[]; text: EdlText[]; markers: { drop: number | null; hero_peak: number | null; beats: number[]; downbeats: number[]; bass_hits: number[] }
  sections: { name: string; start: number; end: number }[]; notes: string[]; title: string | null; showdown: any; chase: any
}
export type EdlResponse = { edl: Edl; version: number; note?: string; versions: number[]; can_undo: boolean; can_redo: boolean }
export type Job = { id: string; project_id: string; kind: string; status: string; progress: number; message: string; error: string; result: any; payload: any }
export type BeatGrid = { tempo: number; duration: number; beats: number[]; downbeats: number[]; bass_hits: number[]; drop_candidates: { t: number; score: number }[]; chosen_drop: number | null; waveform: number[] }
export type ExportRow = { id: string; path: string; url: string; settings: any; sound_offset: number | null; edl_version: number; created_at: string; caption: string; credits_path: string; caption_path: string }
export type StockResult = { key: string; source: string; id: string; width: number; height: number; duration: number; thumbnail_url: string; preview_url: string; page_url: string; credit: string; license: string; query: string; score: number | null; orientation: string; in_library: string | null; kind: string }
export type ChatMsg = { id: number; role: string; content: string; tools: string[]; edl_version: number | null; created_at: string }
export type Config = {
  presets: { id: string; name: string; description: string; default_aspect: string; structure: string }[]
  showdown_layouts: { id: string; name: string; description: string }[]; aspects: string[]; target_lengths: string[]
  stock: { enabled: boolean; missing: string[]; message: string }
  claude: { enabled: boolean; model: string; budget_per_project_usd: number; needs_workspace: boolean }
  inbox_dir: string; exports_dir: string; showdown_stats: Record<string, { label: string; units: string[]; default_unit: string; stamp: string; winner_header: string }>
}

export class ApiError extends Error { status: number; constructor(status: number, msg: string) { super(msg); this.status = status } }

// On Vercel (or any static host) VITE_API_BASE points at the Railway backend; locally it is empty
// and Vite proxies /api to the local API.
// Without the env var, a static host that is not localhost (e.g. wild-cut.vercel.app) talks to the Railway backend.
const DEFAULT_REMOTE_API = 'https://wild-cut-production.up.railway.app'
const envBase = ((import.meta as any).env?.VITE_API_BASE ?? '').replace(/\/$/, '')
const isLocalHost = typeof location !== 'undefined' && /^(localhost|127\.0\.0\.1|\[::1\])$/.test(location.hostname)
export const API_BASE: string = envBase || (isLocalHost || typeof location === 'undefined' || location.hostname.endsWith('.railway.app') ? '' : DEFAULT_REMOTE_API)

/** Absolute URL for an API-relative path, with the access token as a query param when calling
 *  another origin (cookies do not cross origins; <img>, <video> and EventSource cannot set headers). */
export function abs(u: string | null | undefined): string {
  if (!u) return ''
  if (/^https?:\/\//.test(u)) return u
  let out = API_BASE + u
  const tok = getToken()
  if (tok && API_BASE) out += (out.includes('?') ? '&' : '?') + 'token=' + encodeURIComponent(tok)
  return out
}

export const isRemote = () => API_BASE !== ''

/** Multipart upload with progress (fetch cannot report upload progress). */
export function uploadFiles(url: string, files: File[], field = 'files', onProgress?: (pct: number) => void): Promise<any> {
  return new Promise((resolve, reject) => {
    const fd = new FormData()
    files.forEach(f => fd.append(field, f))
    const xhr = new XMLHttpRequest()
    xhr.open('POST', API_BASE + url)
    const tok = getToken()
    if (tok) xhr.setRequestHeader('X-Wildcut-Token', tok)
    xhr.upload.onprogress = (e) => { if (e.lengthComputable && onProgress) onProgress(Math.round((e.loaded / e.total) * 100)) }
    xhr.onload = () => {
      if (xhr.status === 401) window.dispatchEvent(new CustomEvent('wc-auth-required'))
      if (xhr.status >= 200 && xhr.status < 300) { try { resolve(JSON.parse(xhr.responseText)) } catch { resolve({}) } }
      else { let msg = xhr.statusText; try { const j = JSON.parse(xhr.responseText); msg = typeof j.detail === 'string' ? j.detail : JSON.stringify(j.detail ?? j) } catch { /* ignore */ } reject(new ApiError(xhr.status, msg)) }
    }
    xhr.onerror = () => reject(new ApiError(0, 'upload failed (network)'))
    xhr.send(fd)
  })
}

export function getToken(): string { try { return localStorage.getItem('wc_token') ?? '' } catch { return '' } }
export function setToken(t: string) {
  try { localStorage.setItem('wc_token', t) } catch { /* ignore */ }
  // the cookie lets <img>, <video> and EventSource requests carry the token too
  document.cookie = `wc_token=${encodeURIComponent(t)}; path=/; max-age=31536000; SameSite=Lax`
}

async function req<T>(method: string, url: string, body?: unknown, raw?: BodyInit): Promise<T> {
  const headers: Record<string, string> = {}
  if (!raw && body !== undefined) headers['Content-Type'] = 'application/json'
  const tok = getToken()
  if (tok) headers['X-Wildcut-Token'] = tok
  const r = await fetch(API_BASE + url, { method, headers, body: raw ?? (body !== undefined ? JSON.stringify(body) : undefined) })
  if (r.status === 401) { window.dispatchEvent(new CustomEvent('wc-auth-required')) }
  if (!r.ok) {
    let msg = r.statusText
    try { const j = await r.json(); msg = typeof j.detail === 'string' ? j.detail : JSON.stringify(j.detail ?? j) } catch { /* ignore */ }
    throw new ApiError(r.status, msg)
  }
  if (r.status === 204) return undefined as T
  return r.json() as Promise<T>
}

export const api = {
  config: () => req<Config>('GET', '/api/config'),
  projects: () => req<Project[]>('GET', '/api/projects'),
  project: (id: string) => req<Project>('GET', `/api/projects/${id}`),
  createProject: (body: Partial<Project>) => req<Project>('POST', '/api/projects', body),
  patchProject: (id: string, body: Partial<Project>) => req<Project>('PATCH', `/api/projects/${id}`, body),
  deleteProject: (id: string) => req<{ ok: boolean }>('DELETE', `/api/projects/${id}`),
  clips: (id: string) => req<Clip[]>('GET', `/api/projects/${id}/clips`),
  addClipPath: (id: string, path: string) => req<Clip>('POST', `/api/projects/${id}/clips`, { path }),
  uploadClips: (id: string, files: File[], onProgress?: (pct: number) => void) => uploadFiles(`/api/projects/${id}/clips/upload`, files, 'files', onProgress) as Promise<{ clips: Clip[]; job: Job | null }>,
  deleteClip: (id: string, clipId: string) => req<{ ok: boolean }>('DELETE', `/api/projects/${id}/clips/${clipId}`),
  library: (q?: string) => req<any[]>('GET', `/api/library${q ? `?q=${encodeURIComponent(q)}` : ''}`),
  addFromLibrary: (id: string, libId: string) => req<Clip>('POST', `/api/projects/${id}/library/${libId}`),
  inbox: () => req<{ dir: string; files: string[] }>('GET', '/api/inbox'),
  browse: (path?: string) => req<{ path: string; parent: string | null; dirs: string[]; files: { name: string; size: number }[] }>('GET', `/api/browse${path ? `?path=${encodeURIComponent(path)}` : ''}`),
  stockSearch: (q: string, orientation?: string, kind = 'video') => req<{ queries: string[]; results: StockResult[] }>('GET', `/api/stock/search?q=${encodeURIComponent(q)}${orientation ? `&orientation=${orientation}` : ''}&kind=${kind}`),
  stockImport: (project_id: string, results: StockResult[], then_plan = false) => req<{ job: Job }>('POST', '/api/stock/import', { project_id, results, then_plan }),
  setSong: (id: string, path: string) => req<{ project: Project; job: Job }>('POST', `/api/projects/${id}/song`, { path }),
  uploadSong: (id: string, file: File, onProgress?: (pct: number) => void) => uploadFiles(`/api/projects/${id}/song/upload`, [file], 'file', onProgress) as Promise<{ project: Project; job: Job }>,
  clearSong: (id: string) => req<Project>('DELETE', `/api/projects/${id}/song`),
  beatgrid: (id: string) => req<BeatGrid>('GET', `/api/projects/${id}/beatgrid`),
  setWindow: (id: string, start: number, end: number, locked = true) => req<Project>('PUT', `/api/projects/${id}/song/window`, { start, end, locked }),
  setDrop: (id: string, t: number) => req<BeatGrid>('PUT', `/api/projects/${id}/song/drop`, { t }),
  analyze: (id: string, then_plan = true, force = false) => req<{ job: Job }>('POST', `/api/projects/${id}/analyze?then_plan=${then_plan}&force=${force}`),
  moments: (id: string, clipId?: string) => req<Moment[]>('GET', `/api/projects/${id}/moments${clipId ? `?clip_id=${clipId}` : ''}`),
  flagMoment: (id: string, mid: string, body: { starred?: boolean; banned?: boolean }) => req<Moment>('PATCH', `/api/projects/${id}/moments/${mid}`, body),
  plan: (id: string, body: { seed?: number; keep_locks?: boolean; text_only?: boolean; preview?: boolean } = {}) => req<{ edl: Edl; version: number; job: Job | null }>('POST', `/api/projects/${id}/plan`, body),
  edl: (id: string) => req<EdlResponse>('GET', `/api/projects/${id}/edl`),
  edlVersions: (id: string) => req<{ version: number; note: string; created_at: string; duration: number }[]>('GET', `/api/projects/${id}/edl/versions`),
  edlOp: (id: string, op: string, args: Record<string, unknown>, preview = true) => req<{ edl: Edl; version: number; note: string; changed: [number, number][]; job: Job | null }>('POST', `/api/projects/${id}/edl/op`, { op, args, preview }),
  undo: (id: string) => req<EdlResponse>('POST', `/api/projects/${id}/edl/undo`),
  redo: (id: string) => req<EdlResponse>('POST', `/api/projects/${id}/edl/redo`),
  preview: (id: string) => req<{ job: Job }>('POST', `/api/projects/${id}/preview`),
  previewStatus: (id: string) => req<{ ready: boolean; url: string | null; version: number | null; job: Job | null }>('GET', `/api/projects/${id}/preview`),
  exportProject: (id: string, body: { quality?: string; audio?: string | null }) => req<{ job: Job }>('POST', `/api/projects/${id}/export`, body),
  exports: (id: string) => req<ExportRow[]>('GET', `/api/projects/${id}/exports`),
  reveal: (path: string) => req<{ ok: boolean }>('POST', '/api/reveal', { path }),
  showdown: (id: string) => req<any>('GET', `/api/projects/${id}/showdown`),
  putShowdown: (id: string, body: any, draft = true) => req<{ showdown: any; job: Job | null }>('PUT', `/api/projects/${id}/showdown?draft=${draft}`, body),
  putStats: (id: string, stats: any[]) => req<{ showdown: any; blockers: string[] }>('PUT', `/api/projects/${id}/showdown/stats`, { stats }),
  chat: (id: string) => req<ChatMsg[]>('GET', `/api/projects/${id}/chat`),
  sendChat: (id: string, message: string) => req<{ job: Job }>('POST', `/api/projects/${id}/chat`, { message }),
  jobs: (id: string) => req<Job[]>('GET', `/api/projects/${id}/jobs`),
  job: (jobId: string) => req<Job>('GET', `/api/jobs/${jobId}`),
}

export const frameUrl = (projectId: string, t: number, width = 360) => abs(`/api/projects/${projectId}/frame?t=${t.toFixed(3)}&width=${width}`)
export const clipFrameUrl = (projectId: string, clipId: string, t: number, width = 320) => abs(`/api/projects/${projectId}/clips/${clipId}/frame?t=${t.toFixed(2)}&width=${width}`)
export const fileUrl = (path: string) => abs(`/api/file?path=${encodeURIComponent(path)}`)
export const fmt = (s: number) => `${Math.floor(s / 60)}:${(s % 60).toFixed(1).padStart(4, '0')}`
export const fmtShort = (s: number) => `${Math.floor(s / 60)}:${String(Math.round(s % 60)).padStart(2, '0')}`
