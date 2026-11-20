FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
RUN apt-get update && apt-get install -y --no-install-recommends fonts-dejavu-core ffmpeg libopus0 \
    && rm -rf /var/lib/apt/lists/*
# yt-dlp needs a JavaScript runtime to solve YouTube's challenges; Deno is the one it enables by default
COPY --from=denoland/deno:bin /deno /usr/local/bin/deno
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY orion ./orion
COPY scripts ./scripts
CMD ["python", "-m", "orion.main"]
