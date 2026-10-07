import logging

import uvicorn

from wildcut.config import get_settings
from wildcut.db import get_engine

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    get_engine()
    uvicorn.run("wildcut.api.app:app", host="127.0.0.1", port=get_settings().api_port, reload=False, log_level="info")
