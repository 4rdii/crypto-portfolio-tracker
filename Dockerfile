# ─── Portfolio Tracker (FastAPI) ──────────────────────────────────────────────
# Runs under the docker-stack reverse proxy. The sqlite DB + logs are mounted
# as volumes from the host so nothing persistent lives inside the container.

FROM python:3.12-slim

# WeasyPrint needs Pango/Cairo/GDK-Pixbuf native libs at runtime.
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    libpango-1.0-0 \
    libpangoft2-1.0-0 \
    libcairo2 \
    libgdk-pixbuf-2.0-0 \
    libjpeg62-turbo \
    shared-mime-info \
    fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Python deps — install first so layer is cached across code changes.
COPY webapp/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# App code.
COPY webapp/ ./webapp/
COPY scanner.py ./scanner.py

# Runtime — uvicorn binds inside the container; Traefik forwards to :8787.
WORKDIR /app/webapp
ENV PYTHONUNBUFFERED=1
EXPOSE 8787

# Healthcheck hits the FastAPI /api/health endpoint.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fs http://localhost:8787/api/health || exit 1

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8787"]
