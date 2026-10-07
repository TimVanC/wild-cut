"""Start API, worker, and frontend together. Ctrl+C stops all three."""
from __future__ import annotations

import os
import signal
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PY = sys.executable
NPM = "npm.cmd" if os.name == "nt" else "npm"


def main() -> None:
    env = dict(os.environ)
    procs = [
        subprocess.Popen([PY, "-m", "wildcut.api"], cwd=ROOT / "backend", env=env),
        subprocess.Popen([PY, "-m", "wildcut.worker"], cwd=ROOT / "backend", env=env),
    ]
    if (ROOT / "frontend" / "node_modules").exists():
        procs.append(subprocess.Popen([NPM, "run", "dev"], cwd=ROOT / "frontend", env=env, shell=(os.name == "nt")))
    else:
        print("frontend/node_modules missing; run `cd frontend && npm install` to start the UI")

    def stop(*_: object) -> None:
        for p in procs:
            try:
                p.terminate()
            except Exception:
                pass
        sys.exit(0)

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    api_port = env.get("API_PORT", "8787")
    ui_port = env.get("FRONTEND_PORT", "5173")
    print(f"Wild Cut dev: API http://localhost:{api_port}  UI http://localhost:{ui_port}")
    for p in procs:
        p.wait()


if __name__ == "__main__":
    main()
