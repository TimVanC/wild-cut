# Wild Cut server image: builds the React frontend, then runs API + worker in one container.
# Used by Railway (Dockerfile auto-detected) and by docker-compose.

FROM node:22-alpine AS frontend
WORKDIR /app/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend ./
RUN npm run build

FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg libgl1 libglib2.0-0 fonts-noto-color-emoji \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY backend/pyproject.toml backend/pyproject.toml
COPY backend/wildcut backend/wildcut
RUN pip install --no-cache-dir -e backend

COPY presets presets
COPY assets assets
COPY tools tools
COPY --from=frontend /app/frontend/dist frontend/dist

ENV DATA_DIR=/app/data INBOX_DIR=/app/data/inbox EXPORTS_DIR=/app/data/exports PYTHONUNBUFFERED=1
VOLUME ["/app/data"]
EXPOSE 8787
CMD ["python", "tools/serve.py"]
