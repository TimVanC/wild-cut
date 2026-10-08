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
    procs = {"worker": subprocess.Popen([sys.executable, "-m", "wildcut.worker"], cwd=BACKEND, env=env)}

    def keep_worker_alive() -> None:
        while True:
            time.sleep(5)
            w = procs["worker"]
            if w.poll() is not None:
                print(f"[serve] worker exited with {w.returncode}; restarting", flush=True)
                procs["worker"] = subprocess.Popen([sys.executable, "-m", "wildcut.worker"], cwd=BACKEND, env=env)

    threading.Thread(target=keep_worker_alive, daemon=True).start()
    api = subprocess.Popen([sys.executable, "-m", "wildcut.api"], cwd=BACKEND, env=env)

    def stop(*_: object) -> None:
        for p in (api, procs["worker"]):
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
