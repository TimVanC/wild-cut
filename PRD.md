# Wild Cut: Auto Animal Edit Generator PRD

Oct 7, 2026 · @Tim Van Cauwenberge

## Overview

Wild Cut is a local-first web app that turns raw animal footage into a finished short-form edit: it finds the peak action moments, cuts them together, and adds shake, flashes, zoom punches, speed ramps, color grade, and animated text at the right moments.

**Two timing modes:**

- **Music-synced mode.** Tim provides a phonk track as a timing reference. The app detects beats, bass hits, and the drop, then places cuts and effects on the beat and lands the biggest action moment on the drop. Export can be **silent** (default; Tim attaches the same sound from TikTok's library when posting) or **with the track mixed in**. When silent, the app reports the exact sound offset to use (e.g. "start the sound at 0:12").
- **Visual-peaks mode.** No music. Cuts and effects are placed on the footage's own action peaks. Export is silent or keeps the original clip audio.

**Reference styles:**

- **Phonk (aggressive):** fast beat-locked cuts, camera shake and white flashes on bass hits, slow-mo ramp into the payoff on the drop, the animal's name flashing in at the peak as a serif title like the gibbon reference ("THE GIBBON"), crushed blacks and high contrast.
- **Cinematic (calm):** like Tim's gibbon reference video. Square frame, long shots of the animal moving, dark moody grade, vignette, a serif title card (e.g. "THE GIBBON") fading in, few or no shake effects.

Two more presets, **Chase** and **Showdown**, are specified in their own section below.

**Phonk is the default treatment for every footage format:** single-animal edits like the gibbon reference, Chase, and Escape all get beat-locked cuts, shake and flashes on bass hits, and a speed ramp into the payoff on the drop. Cinematic stays available as an optional calm preset but is never the default.

**Content focus:** animals displaying skills (hunting, leaping, swinging, diving, sprinting) and fighting.

**Primary user:** Tim (single user, technical). No auth. Runs on his Mac.

**Goal for this build:** a complete web app: create a project, add footage (local files or licensed stock search), optionally add a song, pick a style, review the auto-generated edit on a timeline, tweak, and export a ready-to-post MP4.

## User flow

1. **New project.** Name it and pick a style preset (Phonk or Cinematic), an aspect ratio (9:16 default, 1:1, 4:5), and a target length (15, 30, 45, 60 s, or "match song").
2. **Add footage.** Any mix of:
   - Local video files (file picker by path, or drop files into a watched `inbox/` folder).
   - Stock search: type a query ("cheetah hunting", "eagle catching fish"), see results with previews from Pexels and Pixabay, tick the ones to import. Imported clips keep their source URL and license info.
3. **Add music (optional).** Pick a local audio file (MP3/WAV/M4A). Choose the song section to use: auto (the strongest 15 to 60 s window ending just after the drop) or manual start/end on a waveform. Choose export audio: silent (default) or mixed in.
4. **Analyze.** Background job with progress: shot detection, motion scoring, Claude vision tagging, and (if music) beat/drop detection.
5. **Auto-edit.** The planner builds the edit. Tim sees a timeline: clip blocks, effect markers (shake, flash, zoom, speed ramp), text blocks, and the song waveform with beat ticks and the drop marked.
6. **Tweak.** Swap a clip for another candidate moment, drag clip boundaries, edit or delete text, toggle individual effects, change effect intensity (low/med/high), regenerate the whole edit with a new random seed, or regenerate just the text.
7. **Export.** Renders the MP4, shows it in a player, saves to `exports/` with a `credits.txt` (stock sources and licenses), `caption.txt` (suggested post caption + hashtags), and, for silent music-synced exports, the sound offset to use when posting.

## Pipeline

Four stages, each writing JSON to the project folder so any stage can be rerun alone: footage analysis produces scored **moments**, music analysis produces a **beat grid**, the planner produces an **edit decision list (EDL)**, and the renderer turns the EDL into an MP4.

**1. Footage analysis**

- **Normalize:** probe each clip; build a 540p proxy for fast analysis and preview. Full-res source is used only at render.
- **Shot detection:** PySceneDetect splits clips into shots; very short shots (<0.4 s) merge with neighbors.
- **Motion score:** dense optical flow (OpenCV Farneback) on the proxy at 10 fps, with global camera motion removed (median flow subtracted), giving a per-frame "subject motion" curve. Smooth it and find peaks.
- **Subject tracking:** for each candidate moment, find the subject box (largest moving region; Claude vision box as fallback) so reframing to 9:16 keeps the animal in frame. Store a smoothed crop path per moment.
- **Claude vision tagging:** for the top \~40 candidate peaks, send 3 to 4 frames each to Claude with a JSON schema: species, action ("pounce", "strike", "leap", "swing", "fight", "catch", "dive", "sprint", "idle"), intensity 1 to 10, framing quality 1 to 10, and whether the subject is clearly visible. Batch calls; respect the per-project budget.
- **Moment record:** clip, in/out, peak time, motion score, Claude tags, crop path. Final moment score = weighted motion + intensity + framing, with "idle" and unclear-subject moments penalized.

**2. Music analysis (music-synced mode only)**

- librosa (madmom optional) for tempo, beat times, and downbeats.
- **Bass hits:** onset detection on a low-pass (<150 Hz) band; keep strong onsets. Phonk 808/cowbell patterns make these clear.
- **Drop detection:** largest jump in low-band RMS energy after a quieter build, confirmed by onset density; expose the top 3 candidates and let Tim override on the waveform.
- Output: beat grid with labels (beat, downbeat, bass hit, drop) for the chosen song window.

**3. Edit planner**

- **Music-synced:** split the song window into sections (intro, build, drop, post-drop). Fill the build with short clips cut on beats (every 1 to 2 beats, tightening toward the drop). Put the single highest-scoring moment on the drop, with a speed ramp so the moment's peak lands exactly on the drop frame (slow-mo 0.3 to 0.5x into and through the peak, then back to 1x). Post-drop uses the next-best moments cut on downbeats. Shakes and flashes go on bass hits; zoom punches on downbeats.
- **Visual-peaks:** order moments by a simple arc (setup, escalation, payoff). Each clip's cut point is its motion peak plus a short tail. Effects fire on each clip's peak; the strongest moment gets the speed ramp and the main text.
- **Clip choice rules:** no clip reused twice unless the footage pool is too small; vary species/shots when the pool allows; prefer moments where the subject is clearly visible; respect the target length.
- **Text:** in every single-animal edit (Cinematic, Phonk, Chase), the only text is the animal's name as a title, e.g. "THE GIBBON", taken from Claude's species tag and editable by Tim. Phonk slams it in at the biggest peak or the drop; Cinematic fades it in over the hero shot. No hype phrases, slogans, or stat captions. Showdown is the only preset with other text.
- **Output:** an EDL JSON: ordered clips (source, in, out, speed curve, crop path), effects (type, time, duration, intensity), text items (string, time, duration, animation), grade, global settings. The EDL is the single source of truth; the UI edits it and the renderer reads it.

**4. Renderer**

- Python renderer that reads the EDL and composes frames: decode with ffmpeg (PyAV), apply crop path, speed curve (frame interpolation via ffmpeg `minterpolate` or RIFE if available for smooth slow-mo; fall back to frame blending), effects, grade, and text, then encode with ffmpeg.
- Must be deterministic: the same EDL always renders the same video.
- Fast preview render at 540p for the review screen; full render at 1080 wide.

## Styles and effects

A style preset is a JSON file that sets cut pacing, which effects fire, their intensity, the grade, and the text look. Two presets ship; adding a third must need only a new JSON file.

| Setting | Phonk | Cinematic |
| --- | --- | --- |
| Default aspect | 9:16 | 1:1 |
| Cut pacing | every 1 to 2 beats, faster before the drop | 3 to 8 s shots, cut on motion peaks or downbeats |
| Shake | on every bass hit; 120 to 250 ms decaying | off (optional subtle drift) |
| Flash | white flash on drop and every 2nd bass hit, 2 to 4 frames | off; fades to black between sections |
| Zoom punch | 1.0 to 1.12x scale snap on downbeats, eased back over 200 ms | slow 1.0 to 1.06x push-in across each shot |
| Speed ramp | slow-mo into the payoff on the drop | gentle 0.7x on the hero shot |
| Overlays | film grain, light chromatic aberration on hits, optional glitch frames | grain, vignette, letterbox bars optional |
| Grade | crushed blacks, high contrast, slight desaturation, cool shadows | dark moody grade, lifted greens/teals, vignette |
| Text | "THE \<ANIMAL>" in an elegant serif, all caps, wide letter-spacing, white (matching the gibbon reference); flashes in on the peak or drop with a white flash and a short shake, held 0.6 to 1.2 s | elegant serif title, wide letter-spacing, slow fade in/out over 1 s |

**Effect details**

- **Shake:** translate + small rotation from deterministic noise, amplitude decaying to zero; crop slightly oversized so shake never shows black edges.
- **Flash:** additive white overlay, opacity curve 1.0 to 0 over the flash length.
- **Chromatic aberration:** offset R and B channels 2 to 6 px for 2 to 3 frames on hits.
- **Grade:** LUT (.cube) per preset via ffmpeg `lut3d` or NumPy, plus contrast curve; ship 2 LUTs and allow dropping in custom .cube files.
- **Intensity:** every effect has low/med/high, mapped to amplitude/duration multipliers in the preset.
- **Fonts:** bundle open-license fonts (a serif such as Cinzel or Cormorant Garamond for animal-name titles in every preset; Anton or Bebas Neue for Showdown cards) in `assets/fonts/`.

## Chase and Showdown presets

Chase is a footage preset that tells a short predator, prey, outcome story. Showdown is a data-driven template that compares animals on one stat using cards, counters, and stamps, and needs only one still or short clip per animal.

**Chase** (reference: 13 s cheetah vs gazelle edit)

- **Structure:** three beats. (1) Predator charging toward camera, (2) prey fleeing, (3) outcome: a blurred motion pass through grass or a cut to black with an optional emoji marker and no added text (default ☠️), implied rather than shown.
- **Clip pairing:** the planner needs a predator clip and a prey clip that read as the same scene. Claude vision tags habitat, lighting, and dominant color; the planner pairs clips with matching habitat and close color, then a shared grade unifies them. Ship a built-in pairs table (cheetah/gazelle, lion/zebra, wolf/elk, orca/seal, eagle/fish, falcon/pigeon) used to build stock search queries.
- **Look:** 3:4 default, warm golden grade, slight motion blur, full Phonk treatment (beat-locked cuts, shake and flashes on bass hits, speed ramp into the outcome or escape on the drop). Length 10 to 20 s.
- **Music:** works in both modes; in music-synced mode the outcome lands on the drop.

**Escape variant:** a Chase option where the prey wins. Beat 3 becomes the evasion (a leap over the predator, a hard cut, a jink) with the slow-mo ramp on the escape moment and the escaping animal's name as the only text. Claude vision adds an `escape` action tag and an outcome field (caught / escaped / unclear) so the planner picks the right variant automatically, or Tim forces one.

**Showdown** (reference: "fastest animal" speed comparison)

- **Input:** a stat (top speed, bite force, weight, jump height, lifespan, or custom) and either a list of animals or "suggest". With suggest, Claude proposes 4 to 8 animals in an escalating order ending on the record holder.
- **Stats and sourcing:** Claude drafts each value with a source URL; the app stores them in a **stats sheet** that Tim reviews and edits in the UI before render. Values with no source are flagged in red and block export until Tim confirms or edits them. Units are configurable (km/h or mph, etc.).
- **Layout:** black-and-white grade; two cards side by side, each with the animal name header in a bold condensed font, a stat pill with a counter that ticks up to the value, the animal still or looping clip in the middle, and a one-line fact at the bottom (Claude-written, sourced).
- **Sequence:** the current champion stays on screen; each challenger slides in, counters tick up, the loser gets a "SLOW" (or stat-appropriate: "WEAK", "SMALL") stamp, then slides out. Final card expands with a header like "FASTEST ANIMAL", followed by an optional 3 to 6 s action clip of the winner.
- **Media per animal:** Pexels/Pixabay photos or clips via the stock adapters, or local files. Background removal is optional (rembg) for cleaner cards.
- **Timing:** in music-synced mode, each challenger enters on a downbeat and the winner reveal lands on the drop. In visual mode, a fixed rhythm (default 1.6 s per challenger).
- **Rendering:** the card layout is drawn by the renderer from a template definition (JSON: positions, fonts, colors, animation curves), so new layouts can be added without code changes.
- **Export extras:** `stats.csv` with every value and its source alongside the MP4.

## Director chat

The editor has a chat panel where Tim directs the edit in plain language ("start with clip 2, put the title right when the gibbon lets go of the branch, then clip 5, end on clip 1"), and Claude edits the EDL through tools. Presets are only the starting point; chat and manual timeline edits can override anything.

- **Upload in chat:** Tim can drag multiple videos into the chat panel. They import into the project like any other clip, are analyzed in the background, and get short labels (Clip 1, Clip 2, ... plus a Claude-written description like "gibbon swinging left to right, low angle") with thumbnails shown in the chat.
- **How Claude edits:** an agent loop (Anthropic SDK with tool use) with tools that read and change the EDL: `list_clips`, `get_moments(clip)`, `look_at(clip, time_range)` (returns sampled frames so Claude can find a moment Tim describes, e.g. "when it lets go of the branch"), `set_order`, `set_clip_range`, `set_title(text, time)`, `add_effect` / `remove_effect`, `set_speed_ramp`, `set_style`, `set_song_window`, `plan_auto(scope)` to auto-fill whatever Tim did not specify, and `render_preview`.
- **Mixing manual and auto:** anything Tim pins in chat (order, a title time, a trim) is marked locked in the EDL; auto-planning and Regenerate only change unlocked parts. Locked items show a pin icon on the timeline.
- **Feedback loop:** after each change Claude summarizes what it did in one or two lines and the preview re-renders the affected segment. Tim can say "undo that"; every chat change is an EDL version, so undo/redo works across chat and manual edits.
- **References:** Tim can refer to clips by number, by description ("the falcon one"), or by timestamp ("at 0:04 in clip 3"). Claude asks a short clarifying question only when a reference is genuinely ambiguous.
- **Music in chat:** "put the title on the drop" or "make clip 2 hit on the second bass hit" resolve against the beat grid.
- **Budget:** chat calls count toward the per-project Claude budget; frame sampling for `look_at` uses the 540p proxies at 2 to 4 fps to keep costs low.

## Footage sources

The app accepts footage two ways: files Tim provides, and searches of licensed stock libraries through their official APIs. It never scrapes or downloads from YouTube, TikTok, Instagram, or any other site.

- **Local files:** any MP4/MOV/MKV Tim supplies (his own footage or footage he has licensed, e.g. from Storyblocks or Artgrid).
- **Pexels Video API:** search by query, filter by min resolution and orientation, download the best rendition through the API. Store photographer name, page URL, and license.
- **Pixabay Video API:** same, as a second source.
- **Stock adapter interface** (`search(query, orientation, min_height)`, `download(id)`, `license_info(id)`) so a paid library can be added later.
- **Search helper:** Claude expands a theme ("predators hunting") into 5 to 8 concrete queries, the app runs them across both sources, de-duplicates, and pre-scores results by thumbnail with Claude vision so the best action clips sort first.
- **Library:** every imported clip lands in a local library (`data/library/`) with tags, so later projects can reuse footage without re-downloading.

## Architecture and screens

Same shape as Possession Cut: React frontend, FastAPI backend, and a separate worker process for all video and audio work, all running on Tim's Mac.

**Stack**

| Layer | Choice |
| --- | --- |
| Frontend | React + TypeScript + Vite, Tailwind, TanStack Query, wavesurfer.js for the waveform |
| API | Python 3.12, FastAPI, Pydantic v2 |
| Worker | separate Python process polling a jobs table, one job at a time, progress in the DB |
| Storage | SQLite (SQLModel); per-project folder for proxies, analysis JSON, EDL, renders |
| Video | ffmpeg 6+, PyAV, OpenCV (headless), NumPy, PySceneDetect, Pillow for text rendering |
| Audio | librosa, soundfile (madmom optional) |
| AI | `anthropic` SDK: vision tagging, text writing, search query expansion |
| Stock | `httpx` clients for Pexels and Pixabay |
| Run | `make dev` starts API, worker, frontend; Docker Compose alternative with ffmpeg bundled |

**Data model**

- `Project`: id, name, style, aspect, target\_length, mode (music/visual), audio\_export (silent/mixed), song\_path, song\_window, seed, status, progress, timestamps.
- `Clip`: id, source (local/pexels/pixabay), path, proxy\_path, duration, fps, resolution, source\_url, credit, license, tags.
- `Moment`: id, clip\_id, in, out, peak, motion\_score, species, action, intensity, framing, caption\_hint, crop\_path, score.
- `BeatGrid`: project\_id, tempo, beats, downbeats, bass\_hits, drop\_candidates, chosen\_drop.
- `Edl`: project\_id, version, json (single source of truth), created\_at.
- `Export`: id, project\_id, path, settings, sound\_offset, created\_at.

**Endpoints** (REST + SSE for progress): projects CRUD; `POST /projects/{id}/clips` (local path), `GET /stock/search`, `POST /stock/import`; `POST /projects/{id}/song`; `POST /projects/{id}/analyze`; `POST /projects/{id}/plan` (optional seed); `GET/PUT /projects/{id}/edl`; `POST /projects/{id}/preview`; `POST /projects/{id}/export`; `GET /media/...` with range support for proxies, previews, exports; `GET /projects/{id}/events` (SSE).

**Screens**

1. **Projects:** cards with thumbnail, style, status, last export.
2. **Footage:** local file picker + stock search grid with hover previews, import checkboxes, and the library of past clips.
3. **Music:** waveform with beat ticks, bass hits, and drop candidates; drag to choose the song window; silent/mixed toggle. Hidden in visual-peaks mode.
4. **Editor (main screen):** a 9:16 or 1:1 preview player on the left; a timeline below with rows for clips, effects, text, and audio. Click a clip to see alternate candidate moments and swap one in. Click an effect marker to toggle or change intensity. Click text to edit it inline. Buttons: Regenerate (new seed), Regenerate text, Preview render. Keyboard: Space play/pause, J/K previous/next clip, Delete removes the selected item.
5. **Export dialog:** resolution, audio option, render progress, finished player, copy caption, copy sound offset, reveal in folder.

Live preview in the editor uses the 540p preview render for accuracy (effects baked in). Re-render only affected segments when the EDL changes, so small edits preview in seconds.

## Config, scope, and build plan

**Keys** (in `.env`, with a committed `.env.example`): `ANTHROPIC_API_KEY` (required), `PEXELS_API_KEY` and `PIXABAY_API_KEY` (both free; stock search is disabled with a clear message if either is missing). Also `CLAUDE_MODEL` (default `claude-sonnet-5-5`), `CLAUDE_BUDGET_PER_PROJECT_USD` (default 1.50), `DATA_DIR`, `INBOX_DIR`, `EXPORTS_DIR`.

**Out of scope**

- Downloading footage or music from YouTube, TikTok, Instagram, or any non-API source.
- Supplying music. Tim provides audio files.
- Auto-posting. Leave a `publishers/` interface for a future scheduler integration.
- Auth, cloud hosting, mobile layout.

**Testing**

- **Synthetic test assets** (`tools/make_test_assets.py`): generate test clips with a moving shape that accelerates at known timestamps (ground-truth peaks) over static and panning backgrounds, plus a synthetic phonk-like track (kick/808 pattern at a known BPM, quiet build, energy drop at a known time).
- Unit tests: beat/bass/drop detection within 50 ms of ground truth on the synthetic track; motion peak detection within 0.2 s; camera-pan footage does not score as high motion; planner puts the top moment's peak on the drop frame (±1 frame); EDL render is deterministic (hash two renders).
- Integration: with real keys present, search Pexels for "cheetah running", import 6 clips, and run the full flow end to end.

**Acceptance criteria**

- [ ] `make dev` on a Mac with ffmpeg installed starts everything; README setup in under 10 steps.
- [ ] Music-synced Phonk edit: cuts land on beats, shakes/flashes on bass hits, hero moment's peak on the drop (verified from the EDL and by frame check).
- [ ] Silent export reports the correct sound offset; mixed export has the song aligned to the cuts.
- [ ] Visual-peaks mode produces an edit with effects on motion peaks and no music.
- [ ] Cinematic preset produces a 1:1 edit with a serif title card and no shake, matching the gibbon reference's feel.
- [ ] Subject stays in frame after 9:16 reframing on the test clips; shake never shows black edges.
- [ ] Editor supports swap moment, toggle effect, edit text, regenerate; edits persist across reloads.
- [ ] Export is a valid H.264/AAC MP4 (or video-only when silent) that plays in QuickTime; `credits.txt` lists every stock clip used.

* [ ] Chase preset pairs a predator and prey clip with matching habitat and grade, and produces the three-beat structure.
* [ ] Showdown preset builds a 5-animal speed comparison from a stat and a list, with ticking counters, SLOW stamps, a winner reveal, and a reviewed stats sheet; export is blocked while any value is unsourced and unconfirmed.

- [ ] Director chat: uploading 3 clips and saying "clip 2 first with the title when the animal jumps, then clip 3, then clip 1" produces that exact order, places the title within 0.3 s of the jump, and keeps those choices locked through Regenerate.

**Build order**

1. Scaffold, `.env.example`, run script, README skeleton.
2. Synthetic test assets.
3. Clip import, proxies, shot detection, motion scoring, subject tracking.
4. Music analysis (beats, bass hits, drop).
5. Claude vision tagging and moment scoring.
6. EDL schema and planner for both modes.
7. Renderer with all effects, grade, text; presets as JSON.
8. Stock adapters (Pexels, Pixabay), search helper, library.
9. Frontend screens and API wiring, segment re-render for fast previews.
10. Export bundle (MP4, credits, caption, sound offset), Docker option.
11. Final self-review: full test suite, one full synthetic project through the UI in each mode and preset, then `BUILD_NOTES.md` with what was built, decisions, known gaps, and what Tim should test first with real footage and a real phonk track.

Build the Chase and Showdown presets after step 7 (renderer) and before step 8, including the stats sheet review screen for Showdown.

Build Director chat right after the editor screen in step 9, once the EDL, locking, and segment previews exist.
