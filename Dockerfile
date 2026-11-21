# syntax=docker/dockerfile:1

FROM python:3.12-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_DEFAULT_TIMEOUT=100 \
    PIP_RETRIES=10

# Wheels are preferred, but a few transitive dependencies still need headers to build if no wheel exists.
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential libffi-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build
COPY requirements.txt .
RUN python -m venv /opt/venv \
    && /opt/venv/bin/python -m pip install --no-cache-dir --upgrade pip setuptools wheel \
    && /opt/venv/bin/python -m pip install --no-cache-dir -r requirements.txt


FROM python:3.12-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_DEFAULT_TIMEOUT=100 \
    PIP_RETRIES=10 \
    PATH="/opt/venv/bin:$PATH" \
    HOME=/app \
    DATA_DIR=/app/data \
    HEALTH_HOST=127.0.0.1 \
    HEALTH_PORT=8080

# Runtime-only packages. Build tools stay in the builder stage and never reach the final image.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates ffmpeg libopus0 fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --system --gid 10001 orion \
    && useradd --system --uid 10001 --gid orion --home-dir /app --shell /usr/sbin/nologin orion

# yt-dlp needs a JavaScript runtime for some YouTube challenges. Use the official minimal Deno binary.
COPY --from=denoland/deno:bin /deno /usr/local/bin/deno
COPY --from=builder /opt/venv /opt/venv

WORKDIR /app
COPY --chown=orion:orion orion ./orion
COPY --chown=orion:orion assets ./assets
COPY --chown=orion:orion scripts/docker-healthcheck.py /usr/local/bin/docker-healthcheck.py

# Named volumes mount over these paths and are initialised with these ownership settings on first use.
RUN mkdir -p /app/data \
    && chown -R orion:orion /app /opt/venv

USER orion

HEALTHCHECK --interval=30s --timeout=5s --start-period=180s --retries=3 \
    CMD ["python", "/usr/local/bin/docker-healthcheck.py"]

ENTRYPOINT ["python", "-m", "orion.main"]
