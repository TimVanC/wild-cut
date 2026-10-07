# Wild Cut backend image: Python 3.12 + ffmpeg. Used by docker-compose for the api and worker services.
FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg libgl1 libglib2.0-0 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY backend/pyproject.toml backend/pyproject.toml
COPY backend/wildcut backend/wildcut
RUN pip install --no-cache-dir -e backend

COPY presets presets
COPY assets assets
COPY tools tools

ENV DATA_DIR=/app/data INBOX_DIR=/app/inbox EXPORTS_DIR=/app/exports API_PORT=8787
WORKDIR /app/backend
CMD ["python", "-m", "wildcut.api"]
