# Wild Cut

Local-first web app that turns raw animal footage into finished short-form edits:
finds peak action moments, cuts them to a phonk track (or to the footage's own
motion peaks), and adds shake, flashes, zoom punches, speed ramps, a color grade,
and the animal's name as a title at the right moment.

See [PRD.md](PRD.md) for the full specification and [BUILD_NOTES.md](BUILD_NOTES.md)
for what was built, decisions, known gaps, and what to test first.

## Setup (Mac or Windows)

1. Install prerequisites: Python 3.12+, Node 20+, and ffmpeg 6+ (`brew install ffmpeg` on Mac).
2. `git clone https://github.com/TimVanC/wild-cut && cd wild-cut`
3. `cp .env.example .env` and fill in `ANTHROPIC_API_KEY` (required). `PEXELS_API_KEY` and
   `PIXABAY_API_KEY` are optional; stock search is disabled with a message if either is missing.
4. `make setup` (creates `.venv`, installs the backend and the frontend).
5. `make assets` (optional) builds the synthetic test clips and phonk-like track in `tests_out/`.
6. `make dev` starts the API (http://localhost:8787), the worker, and the frontend (http://localhost:5173).
7. Open http://localhost:5173, create a project, add footage, optionally add a song, and hit Analyze.

Without `make` (Windows PowerShell):

```powershell
py -3.12 -m venv .venv
.venv\Scripts\python -m pip install -e "backend[dev]"
cd frontend; npm install; cd ..
.venv\Scripts\python tools\dev.py
```

Docker alternative: `docker compose up` (ffmpeg bundled; see `docker-compose.yml`). Set `MEDIA_DIR` to the
folder with your footage so the containers can read it at `/media/...`.

Claude key note: a user-scoped key (`sk-ant-usr-...`) only works with `ANTHROPIC_WORKSPACE_ID` set in `.env`.
A workspace-scoped key (`sk-ant-api03-...`) needs no extra setting. Without a working key the app still
runs (motion-only tags, placeholder title, offline Director commands) and the UI says so.

## Modes and presets

- **Music-synced** (phonk track as timing reference) or **visual peaks** (no music). Silent export by default;
  the export dialog shows the sound offset to use on TikTok.
- **Phonk** (default for everything), **Cinematic**, **Chase** (predator / prey / outcome, Escape variant),
  **Showdown** (stat comparison cards with a reviewed, sourced stats sheet).
- **Documentary mode**: "New from documentary", register a full-length film by path (or drop it in `inbox/`),
  Analyze, review the shot bank (HERO / AURA / BROLL / OTHER, star or ban), Generate N edits. Each edit is a
  normal project that opens in the editor and Director chat.
- **Director chat** in the editor: "clip 2 first, title when it lets go of the branch, then clip 3". Anything you
  specify is pinned and survives Regenerate.

## Layout

- `backend/wildcut/` Python package: `api` (FastAPI), `worker` (job runner), `analysis/`
  (shots, motion, tracking, vision), `music/` (beats, bass hits, drop), `planner/` (EDL),
  `render/` (effects, grade, text, encode), `stock/` (Pexels, Pixabay), `director/` (chat agent).
- `presets/*.json` style presets; `presets/showdown_layouts/*.json` Showdown card layouts.
- `assets/fonts/`, `assets/luts/` bundled open-license fonts and LUTs.
- `frontend/` React + TypeScript + Vite + Tailwind.
- `tools/make_test_assets.py` synthetic ground-truth test clips and track.
- `data/` (SQLite + per-project folders), `inbox/` (watched drop folder), `exports/`.

## Tests

`make test` runs the backend suite (beat/drop detection, motion peaks, planner drop alignment,
deterministic rendering, EDL versioning, Director tools) against the synthetic assets.
