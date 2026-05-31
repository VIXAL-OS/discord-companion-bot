# discord-companion-bot — chat / support / two-tier memory + 🎙️ YouTube
# transcription. Bundles ffmpeg + a CPU build of Whisper for transcription.
# No JRE — MTG is stripped from this fork (the lone `import mtg` is an optional
# try-guard that no-ops when the package is absent).
#
#   docker compose up -d --build      # start (or restart after a git pull)
#   docker compose logs -f            # live logs
#
FROM python:3.11-slim

# ffmpeg = Whisper transcription + yt-dlp audio extraction.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# --- Python deps (own layer so editing code doesn't re-run pip) ---------------
# CPU torch FIRST (no GPU on a VPS) so openai-whisper reuses it instead of
# pulling the ~2GB CUDA build. Then requirements.txt, then the `whisper` CLI
# that youtube_transcribe.py shells out to.
COPY requirements.txt .
RUN pip install --no-cache-dir torch --index-url https://download.pytorch.org/whl/cpu \
    && pip install --no-cache-dir -r requirements.txt \
    && pip install --no-cache-dir openai-whisper

# Application code. Secrets + mutable state (config.json, .env, memories/,
# logs/) are .dockerignore'd and bind-mounted at runtime — see compose.
COPY . .

ENV PYTHONUNBUFFERED=1 \
    PYTHONUTF8=1 \
    PYTHONIOENCODING=utf-8

CMD ["python", "bot.py"]
