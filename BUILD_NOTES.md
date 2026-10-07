# Wild Cut build notes

(Work in progress. Decisions are appended as they are made; the final summary is written at the end.)

## Decisions log

- 2026-10-07: Development machine is Windows 11 (the PRD targets Tim's Mac). Everything is written
  to be cross-platform: paths via `pathlib`, ffmpeg found on PATH, `tools/dev.py` instead of a
  bash-only run script (the Makefile wraps it). No Windows-only dependencies.
- Python 3.12 venv in `.venv`; backend is an installable package in `backend/` so tests import `wildcut`.
- PRD v2 (received mid-build) supersedes v1: Phonk is the default treatment for every footage format,
  the animal-name title is a serif ("THE GIBBON" style) in every preset, Anton/Bebas are reserved for
  Showdown cards, and Chase gets the full Phonk treatment. The committed `PRD.md` is v2.
- 2026-10-07: Documentary mode added to the PRD (new section before "Config, scope, and build plan")
  at Tim's request mid-build. It is scheduled after step 10 (export) and before the final review,
  because it reuses shots, motion, vision, the planner, the renderer, the editor, and Director chat.
- Motion scoring samples at 20 fps instead of the PRD's 10 fps: at 10 fps Farneback flow saturated
  on the fastest synthetic burst (950 px/s) and ranked it below a 500 px/s burst. At 20 fps the
  ranking matches ground truth. Analysis runs on a 320 px wide proxy so the cost stays small.
- Motion peaks within 0.2 s of an internal hard cut are discarded (flow across a cut is garbage).
- Bass hits use a log-RMS derivative on the <150 Hz band (sample-accurate, 100% recall/precision
  on the synthetic 808 pattern); spectral-flux onsets on the low band were unreliable. Beat times
  from librosa are snapped onto those low-band onsets within 45 ms, which removes librosa's ~24 ms
  spectrogram lag, and the grid is regularized (gaps filled, extrapolated to the file edges).
- Song window: the drop sits at 60% of the window (DROP_POSITION) so there is a post-drop payoff
  section, rather than the window ending "just after the drop". Tim can override on the waveform.
- The Anthropic key Tim provided is a user-scoped key (`sk-ant-usr-...`). The API rejects every
  request from it unless an `anthropic-workspace-id` header is sent, and the key cannot list
  workspaces, so the build could not make live Claude calls. `ANTHROPIC_WORKSPACE_ID` is now read
  from `.env` and sent as that header. All Claude-dependent code paths were tested with a fake
  client, and every path degrades to heuristics (motion-only tags, species "animal", the
  Director chat reports Claude unavailable) when calls fail. See "What to test first".
- Preview re-rendering: the timeline is split into fixed 2 s chunks; each chunk is rendered
  to its own MP4 named by a hash of everything that touches it (clips, effects, text, grade,
  overlays, showdown data). After an edit only chunks whose hash changed re-render, and the
  chunks are stitched with ffmpeg's concat demuxer (stream copy). A one-clip tweak on a 30 s
  edit re-renders 1 to 2 chunks (a few seconds at 540p).
- Undo/redo: EDL versions are append-only; the project keeps an `edl_cursor`. Undo moves the
  cursor back, redo forward, and any new change saves a new version after the cursor (the
  later versions stay in history but are no longer reachable by redo).
- Manual timeline edits (swap, trim, reorder, text edit, effect toggle/intensity) lock the
  item so Regenerate keeps it; the timeline shows a pin and the pin can be removed.
- Inbox: files dropped in `inbox/` go to the library (reusable by any project, shown on the
  Footage screen); files dropped in `inbox/<project id>/` import straight into that project
  and queue analysis. Originals are never deleted; a `.wildcut_seen.json` tracks what was seen.
- Showdown ties: a challenger with the same value as the champion loses (it must beat the
  champion). The animals are ordered ascending so the record holder is revealed last.
- "Original audio" export for visual-peaks mode concatenates each clip's own audio; a
  speed-ramped clip uses its average rate through ffmpeg atempo (not a true variable-speed
  audio warp).
- Director chat runs as a worker job (the agent loop can take 10 to 40 s). Each tool call that
  changes the edit saves an EDL version tagged "chat: ...", so "undo that" and the timeline's
  undo button share the same history. Frames from look_at are sampled at 2 to 4 fps from the
  540p proxy (max 12 frames per call) and are stripped before the turn is stored.
- Without Claude the Director still handles "undo" and "clip 2 first, then clip 3" ordering
  offline and tells Tim that Claude is not configured for anything else.
