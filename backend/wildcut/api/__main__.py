import logging
import os

import uvicorn

from wildcut.config import get_settings
from wildcut.db import get_engine

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    get_engine()
    # On a server (Railway sets PORT) bind every interface; locally stay on loopback.
    port = int(os.environ.get("PORT") or get_settings().api_port)
    host = "0.0.0.0" if os.environ.get("PORT") or os.environ.get("WILDCUT_HOST") == "0.0.0.0" else "127.0.0.1"
    uvicorn.run("wildcut.api.app:app", host=host, port=port, reload=False, log_level="info")
