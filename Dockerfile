# syntax=docker/dockerfile:1

FROM python:3.11-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# curl is kept for the HEALTHCHECK below. No libpq is needed: psycopg[binary]
# bundles its own client library.
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl \
 && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install -r requirements.txt

# Non-root. The app never needs to write anywhere except its SQLite file.
RUN groupadd --gid 1001 mizaan \
 && useradd --uid 1001 --gid mizaan --no-create-home --shell /usr/sbin/nologin mizaan

COPY app ./app

RUN mkdir -p /data && chown -R mizaan:mizaan /data /app
USER mizaan

ENV DATABASE_URL=sqlite:////data/mizaan.db \
    PORT=8000

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD curl -fsS "http://127.0.0.1:${PORT}/api/health" || exit 1

# One worker on purpose: SQLite cannot take multiple writers, and the
# in-process rate limiter is per-worker. See ARCHITECTURE.md.
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT} --workers 1"]
