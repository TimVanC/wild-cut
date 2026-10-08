"""Single-container server entrypoint: runs the worker as a child process and the API in the foreground.

Used by the Dockerfile (Railway, Docker Compose "server" mode). The API serves the built frontend
from frontend/dist, so one port exposes the whole app.
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"


def main() -> None:
    env = dict(os.environ)
    env.setdefault("WILDCUT_HOST", "0.0.0.0")
    # two workers: "heavy" (video / documentary analysis, exports) and "light" (song, plan, preview, chat),
    # so a 10-second song analysis never waits behind a 15-minute motion pass
    def start_worker(lane: str) -> subprocess.Popen:
        return subprocess.Popen([sys.executable, "-m", "wildcut.worker", "--lane", lane], cwd=BACKEND, env=env)

    procs = {"heavy": start_worker("heavy"), "light": start_worker("light")}

    def keep_workers_alive() -> None:
        while True:
            time.sleep(5)
            for lane, w in list(procs.items()):
                if w.poll() is not None:
                    print(f"[serve] {lane} worker exited with {w.returncode}; restarting", flush=True)
                    procs[lane] = start_worker(lane)

    threading.Thread(target=keep_workers_alive, daemon=True).start()
    api = subprocess.Popen([sys.executable, "-m", "wildcut.api"], cwd=BACKEND, env=env)

    def stop(*_: object) -> None:
        for p in (api, *procs.values()):
            try:
                p.terminate()
            except Exception:
                pass
        sys.exit(0)

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    code = api.wait()
    stop()
    sys.exit(code)


if __name__ == "__main__":
    main()
