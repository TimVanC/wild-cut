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
