# Wild Cut dev entrypoints. Requires: python3.12+, node 20+, ffmpeg 6+ on PATH.
PY ?= .venv/bin/python
ifeq ($(OS),Windows_NT)
PY := .venv/Scripts/python
endif

.PHONY: setup dev api worker frontend test assets lint

setup:
	python3 -m venv .venv || py -3.12 -m venv .venv
	$(PY) -m pip install --upgrade pip
	$(PY) -m pip install -e "backend[dev]"
	cd frontend && npm install
	cp -n .env.example .env || true

assets:
	$(PY) tools/make_test_assets.py
	$(PY) tools/make_documentary_asset.py

api:
	cd backend && ../$(PY) -m wildcut.api

worker:
	cd backend && ../$(PY) -m wildcut.worker

frontend:
	cd frontend && npm run dev

dev:
	$(PY) tools/dev.py

test:
	cd backend && ../$(PY) -m pytest -q

lint:
	cd backend && ../$(PY) -m ruff check wildcut tests
