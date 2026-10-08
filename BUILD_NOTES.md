# Wild Cut build notes

Built autonomously against `PRD.md` (v2, with the Documentary mode section added mid-build).
Development machine: Windows 11, Python 3.12, Node 22, ffmpeg 9. Everything is cross-platform
(`pathlib`, ffmpeg on PATH, `tools/dev.py` as the run script; the Makefile wraps it).

## What was built

**Pipeline (all stages write JSON to the project folder so any stage can rerun alone)**

- Footage analysis: ffprobe + 540p proxies; PySceneDetect shots (<0.4 s merged); Farneback
  optical flow at 20 fps on a 320 px proxy with the median flow (camera motion) subtracted, a
  smoothed subject-motion curve, peak picking, and a moving-region box per sample; smoothed crop
  paths per aspect (9:16, 1:1, 4:5, 3:4) clamped inside the frame; candidate moments (peak ±
  lead/tail inside the shot, cut-adjacent peaks dropped); Claude vision tags for the top 40
  moments per clip (species, action, intensity, framing, visibility, habitat, lighting, color,
  outcome, documentary category) batched 4 moments × 4 frames per call; final score with idle /
  hidden-subject penalties; a Claude-written clip description for chat labels.
- Music analysis: librosa beats snapped to low-band (<150 Hz) onsets, downbeat phase by
  low-band energy, 808/kick hits from a log-RMS derivative on the low band, drop candidates from
  the biggest low-band energy jump after a quieter build confirmed by onset density (top 3, Tim
  can override on the waveform), auto song window with the drop at 60% of the window.
- Planner: beat-locked build slots tightening toward the drop (2 beats -> 1 beat -> half beats in
  the last bar), the hero clip's `in` solved so its peak lands on the drop frame through its
  speed ramp, post-drop cuts on downbeats, shakes on bass hits, flashes on the drop and every 2nd
  hit, chromatic aberration with flashes, zoom punches on downbeats, glitch frames on the drop,
  the animal's name as the only title. Visual-peaks mode: setup / escalation / payoff arc with
  effects on each clip's peak. Locks: order, range, anchors, title (with clip-anchored times),
  effects, song window; a fully pinned order is used verbatim and the song window moves so the
  drop meets the pinned hero.
- Renderer: deterministic (md5-identical renders); timeline -> source mapping through
  piecewise-linear speed curves; crop path + headroom zoom + zoom punch / push-in + shake with
  rotation in one affine resample (shake never shows edges); frame blending for slow-mo on
  previews and ffmpeg minterpolate on full exports; .cube LUT grades (128-level table) plus
  contrast / saturation / lift / gamma; grain, vignette, letterbox; serif titles from variable
  fonts with letter-spacing and shadow (flash-in, fade, slam); silent / song-mixed / original
  audio; segment renders; chunked previews (2 s chunks keyed by content hash, stitched by
  stream copy) so small edits re-render in seconds.
- Presets as JSON (`presets/*.json`, `extends` supported): Phonk, Cinematic, Chase (full Phonk
  treatment, warm grade, predator/prey pairing, caught / escaped outcome), Showdown (stats
  sheet, cards drawn from `presets/showdown_layouts/*.json`, ticking counters, stamps, winner
  reveal on the drop, stats.csv, export blocked while a value is unsourced and unconfirmed).
- Stock: Pexels and Pixabay adapters behind one interface (search / download / license_info),
  Claude query expansion (plus Chase pairs), de-duplication, thumbnail pre-scoring, a local
  library with credits and licenses. Nothing is downloaded from YouTube, TikTok, Instagram or
  any non-API source.
- App: FastAPI (projects, clips by path / upload / library / stock, song + window + drop,
  analyze, moments, plan / regenerate, EDL get / put / op / undo / redo, frame, preview, export,
  Showdown sheet, Director chat, documentary, jobs, SSE events, ranged media), a worker that
  runs one job at a time and watches `inbox/`, React + TypeScript + Vite + Tailwind screens
  (Projects, New project, Footage, Music with wavesurfer, Editor with timeline / inspector /
  Director chat / export dialog, Showdown stats sheet, Documentary shot bank).
- Director chat: a Messages API agent loop with 20 tools (`list_clips`, `get_moments`,
  `look_at` frames at 2-4 fps, `set_order`, `insert_clip`, `remove_clip`, `set_clip_range`,
  `set_title`, `add_effect`, `remove_effect`, `toggle_effect`, `set_intensity`, `set_speed_ramp`,
  `set_style`, `set_song_window`, `plan_auto`, `render_preview`, `undo`, `set_lock`), a clip
  reference resolver (numbers, labels, ids, descriptions), one EDL version per change, offline
  fallback ("undo", "clip 2 first, then clip 3").
- Documentary mode: see its own section below.
- Export bundle: MP4 (H.264 yuv420p, AAC when audio), `credits.txt`, `caption.txt` with the
  sound offset for silent music-synced exports, `stats.csv` for Showdown.

**Tests** (`backend/tests`): synthetic ground truth for motion peaks (±0.2 s), pan rejection,
hard cuts, subject-in-frame after reframing, beats / downbeats / bass hits / drop (±50 ms),
planner drop alignment (±1 frame), cuts on beats, effects on hits, locks through Regenerate,
determinism (md5 of two renders), title and flash frames, no black edges, segment renders,
mixed audio, Chase pairing and variants, Showdown timing / gate / cards / csv, stock adapters
with mocked HTTP, library, end-to-end API flows, Director acceptance scenario, documentary
filters / classification / two distinct edits.

## Decisions and assumptions (chronological)

- PRD v2 supersedes v1: Phonk is the default treatment for every footage format, the title is a
  serif "THE GIBBON" in every preset, Anton / Bebas Neue are only used on Showdown cards.
- Documentary mode was added to the PRD mid-build (new section before "Config, scope, and build
  plan") and scheduled after export, before the final review.
- Motion scoring samples at 20 fps instead of 10: at 10 fps Farneback flow saturated on the
  fastest synthetic burst and ranked it below a slower one; at 20 fps the ranking matches ground
  truth. Analysis runs on a 320 px wide proxy so the cost stays small.
- Motion peaks within 0.2 s of an internal hard cut are discarded.
- Bass hits use a log-RMS derivative on the <150 Hz band (100% recall and precision on the
  synthetic 808 pattern); spectral-flux onsets on the low band were unreliable. Beat times from
  librosa are snapped to those onsets within 45 ms (removes ~24 ms spectrogram lag) and the grid
  is regularized (gaps filled, extrapolated to the file edges).
- Song window: the drop sits at 60% of the window so there is a post-drop payoff section rather
  than the window ending just after the drop.
- The Anthropic key Tim provided is user-scoped (`sk-ant-usr-...`): the API rejects every request
  without an `anthropic-workspace-id` header and the key cannot list workspaces, so no live
  Claude call succeeded during the overnight build. `ANTHROPIC_WORKSPACE_ID` is read from `.env`
  and sent as that header; without it the app reports Claude as disabled and uses heuristics
  (motion-only tags, species "animal" and the placeholder title "THE ANIMAL", offline Director
  commands). Every Claude path was tested with fake clients. Tim supplied the workspace id the
  next morning; a live `describe_clip` call then succeeded ($0.003) and the Director was
  exercised with the real model (see the UI verification list).
- Preview re-rendering uses fixed 2 s chunks keyed by a content hash; only changed chunks
  re-render and the chunks are stitched with ffmpeg's concat demuxer (stream copy).
- Undo / redo: EDL versions are append-only with an `edl_cursor` on the project; a new change
  after an undo saves a new version after the cursor.
- Manual timeline edits (swap, trim, reorder, text edit, effect toggle / intensity) lock the item
  so Regenerate keeps it; the timeline shows a pin that can be removed.
- Inbox: files in `inbox/` go to the library; files in `inbox/<project id>/` import into that
  project and queue analysis. Originals are never deleted; `.wildcut_seen.json` tracks them.
- Showdown ties: a challenger with the same value as the champion loses. Animals are ordered
  ascending so the record holder is revealed last. Without a source, a row blocks export until
  Tim confirms or edits it.
- "Original audio" export (visual mode) concatenates each clip's audio; a speed-ramped clip
  uses its average rate through atempo, not a true variable-speed warp.
- Director chat runs as a worker job. Each tool change saves an EDL version tagged "chat: ..."
  so "undo that" and the timeline undo share one history. Titles placed at "{clip, source
  time}" carry an anchor and follow the clip through re-trims and Regenerate; an anchored,
  locked title makes its clip the hero so the drop lands on that moment. `set_order` re-fits the
  pinned order onto the beat grid.
- Full exports interpolate slow-mo with ffmpeg minterpolate (mci / obmc / bilat, capped at
  60 fps); previews blend frames. On this machine a 5 s 1080x1920 export with one ramped hero
  took 88 s, most of it minterpolate.
- Planner notes are de-duplicated; the UI shows them under the player.
- Chase without predator/prey species tags (no Claude) alternates the first two clips as
  predator and prey and says so in a note.
- Emoji outcome marker (☠️) renders with the system emoji font (Segoe UI Emoji on Windows, Apple
  Color Emoji on Mac); if none is found the marker is skipped.

## Documentary mode decisions

- Shots are not extracted into files: a child edit project's clips point at the original film
  with `window_in` / `window_out` and share one cached 540p proxy, so generating edits is
  instant and the final export reads the full-resolution source directly. The letterbox crop
  is stored per clip (`src_crop`) and applied by the renderer only when it reads the uncropped
  source (the proxy is already cropped).
- Shot detection uses PySceneDetect's AdaptiveDetector (3.0 / min_content_val 10): on the
  synthetic film ContentDetector 27 found 53% of the cuts, AdaptiveDetector 97% with no false
  cuts.
- Cheap filters before any Claude call: black frames, shots under 0.5 s, burned-in text (rows of
  8+ letter-sized, equal-height, evenly spaced high-contrast components; no OCR dependency),
  duplicates (dHash of the first and middle keyframes, Hamming <= 6 against every kept shot).
  People / presenters and logos / watermarks are left to Claude's `has_people` / `has_text`
  tags; a static-corner logo heuristic was removed because it also fired on locked-off shots.
- Classification: one keyframe per shot, 12 shots per call, highest motion first, capped at
  1500 shots, under `CLAUDE_BUDGET_PER_DOCUMENTARY_USD` (default 6.0). Without Claude the
  category comes from motion and moving-region size.
- Animal auto-detect = the species with the most HERO/AURA seconds; HERO/AURA shots of other
  species become OTHER.
- No HERO or AURA shot appears in two edits (round-robin partition by score, each edit gets its
  own hero moment and opening BROLL shot). Inside one edit a long HERO shot may supply several
  cuts and HERO/AURA moments are reused only if the pool runs out before the drop (noted).
  BROLL is used only in the intro and outro.
- If HERO footage cannot cover the target, the edit is shortened and the note says why.
- Measured analysis on the synthetic 9.5-minute 960x540 film (Windows, 16 threads): letterbox
  2.4 s, proxy 39 s, shots 54 s, motion 113 s at 20 fps (now 12 fps for documentaries, roughly
  halving it), filters 18 s, classification with the fake client 3 s; about 0.3x to 0.4x of the
  film's duration before Claude calls. Extrapolated for a 2-hour 1080p film: roughly 45 to 70
  minutes before Claude plus ~125 classification calls. Tim should measure on the Mac and
  replace this estimate.

## Known gaps

- Claude only went live at the very end (workspace id supplied after the build), so vision tag
  quality, Showdown stats drafting, search expansion and thumbnail pre-scoring were not tuned
  against the real model; only the Director was exercised live. The fake clients exercise every
  code path and schema.
- No Pexels / Pixabay keys were available: the adapters are tested against mocked HTTP responses
  shaped like the official API docs; the PRD's integration test ("search Pexels for cheetah
  running, import 6 clips") needs real keys.
- Logos / watermarks are only caught through Claude's `has_text` tag.
- RIFE interpolation is not integrated (ffmpeg minterpolate or frame blending only).
- The Chase outcome "blur pass" is a horizontal motion blur, not a grass-specific effect.
- Original-audio export approximates ramped clips with atempo.
- `make dev` is a thin wrapper over `tools/dev.py`; on Windows use the PowerShell lines in the
  README.
- Documentary classification of people and logos depends on Claude; the heuristic fallback keeps
  presenter shots as OTHER only when they move little, so review the bank before generating
  when Claude is not configured.
- The frontend has no mobile layout (out of scope) and no automated UI tests; the UI flows were
  driven manually in the built-in browser (see below).

## What was verified through the real UI

- Phonk, music-synced, 9:16: create project, add 4 synthetic clips by path, attach the synthetic
  track on the Music tab (139.7 BPM, 71 beats, 43 bass hits, drop at 0:12 at 100%), Analyze ->
  edit with beat-locked cuts, the hero on the drop with the ramp, effect markers, the serif
  title, chunked preview; swap a moment from the inspector (pinned, preview chunks
  re-rendered), Director "clip 3 first, then clip 1, then clip 2" (exact order, song window
  moved so the drop hits the pinned hero, title on the drop), full 1080 export with the sound
  offset ("start it at 0:10") and caption. Frame check on the exported MP4: frame 64 (the drop,
  2.12 s at 30 fps) is the white flash, frames 63 and 67 are normal, and the serif title is on
  screen from frame 67. Regenerate in the editor afterwards (v6) kept the chat-pinned order
  3, 1, 2 with the hero still on the drop.
- Cinematic, music-synced, 1:1 (wizard switches the aspect automatically): 5 shots of 3 to 4 s,
  only push-in and fade-to-black effects, the Cormorant title fading in over the hero shot, hero
  on the drop.
- Chase, music-synced, 3:4: alternating two-clip build (no species tags without Claude, noted),
  the outcome on the drop with the speed ramp, motion-blur pass, hard cut to black and the emoji
  marker, full Phonk effect set, 15 s.
- Phonk, visual peaks, 9:16, no song: no beat grid, shake / flash / chromatic / zoom punch on each
  clip's peak, the hero with the ramp and the title on its peak, 15 s.
- Showdown, music-synced, 9:16: wizard -> stats sheet (top speed, km/h; lion / cheetah / peregrine
  falcon; the offline draft creates empty rows), values, sources, facts and media per row, the
  lion left unsourced -> red row and "export blocked: lion", confirmed it -> "sheet ok", winner
  clip chosen, "Save and build" -> intro card, two versus cards on downbeats, winner card on the
  drop, winner clip; export through the dialog -> 540 wide MP4 (18.9 s, h264) with credits,
  caption and `stats.csv` listing every value, source and confirmation.
- Documentary: "New from documentary" with the synthetic 9.5-minute letterboxed film, Analyze
  (228 s on this machine: letterbox 3.5 s, proxy 47 s, shots 77 s, motion 77 s at 12 fps,
  filters 22.5 s; 78 shots, 67 kept, 3 black / 2 short / 4 text / 2 duplicate rejected), the
  90 s track on the Music tab, "Generate 2 edits" -> two 65.0 s edits with intro (3 BROLL/AURA
  clips), beat-locked build, the hero on the drop (38.995 s), post on downbeats, outro; zero
  HERO/AURA shots shared between the two edits; both open in the editor with Director chat.

## What Tim should test first with real footage and a real phonk track

1. Put a workspace-scoped Anthropic key in `.env` (or add `ANTHROPIC_WORKSPACE_ID` next to the
   user-scoped key). Start `make dev`, open the Projects screen: the yellow Claude banner should
   be gone.
2. New project, Phonk, 9:16, 30 s, music-synced, silent. Footage tab: add 5 to 8 gibbon or
   cheetah clips by path. Music tab: pick your phonk track; check that the DROP marker sits on
   the real drop (click an "alt" candidate to override) and drag the window if you want a
   different section. Footage tab: Analyze.
3. In the editor, check: cuts land on beats (ticks under the clips), shakes/flashes sit on the
   red bass-hit ticks, the hero clip's slow-mo peak lands on the DROP line, and the title is the
   animal's name from Claude's species tag. Play the preview.
4. Director chat: "clip 2 first, title when it lets go of the branch, then clip 3, then clip 1".
   Expect the exact order, the title within 0.3 s of the moment, pins on those clips, and the
   same choices after Regenerate.
5. Export (1080, silent). Attach the same sound on TikTok and start it at the offset shown in the
   dialog. Also try "Song mixed in" once and confirm the cuts sit on the beats of the mixed audio.
6. Cinematic 1:1 on the gibbon footage: square frame, long shots, dark moody grade, the serif
   title fading in over the hero shot, no shake.
7. Chase: cheetah + gazelle clips, music-synced; confirm the predator/prey pairing, the outcome
   on the drop, and force "escaped" from the project options if Claude's outcome tag is wrong.
8. Showdown: top speed, "suggest", draft the sheet, fix any unsourced row (red), assign media,
   build, export (check stats.csv).
9. Documentary: "New from documentary", point at a 45+ minute film, Analyze (note the time
   reported on the page and update the estimate above), review the shot bank (ban presenter or
   map shots the filters missed), add your track on the Music tab, Generate 2 edits, open each
   in the editor and try "swap the drop for the river crossing around 34:10" in the Director.
10. Stock search with Pexels / Pixabay keys: "cheetah hunting" should return pre-scored results
    with hover previews; import 6 and run the full flow.
