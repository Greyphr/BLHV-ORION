# Standalone Orion admin panel (backend + static UI in one image).
FROM python:3.12-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"
COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir --upgrade pip \
 && pip install --no-cache-dir -r /tmp/requirements.txt

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH" \
    ADMIN_HOST=0.0.0.0 \
    ADMIN_PORT=8080

RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl \
 && rm -rf /var/lib/apt/lists/* \
 && groupadd --gid 10001 orion \
 && useradd --uid 10001 --gid orion --create-home --shell /usr/sbin/nologin orion

COPY --from=builder /opt/venv /opt/venv
COPY --chown=orion:orion app /app/app
WORKDIR /app

HEALTHCHECK --interval=30s --timeout=5s --retries=3 --start-period=20s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/api/health', timeout=5).read()"

USER orion
EXPOSE 8080
CMD ["python", "-m", "app.main"]
