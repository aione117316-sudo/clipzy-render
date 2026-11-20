# Clipzy render server — Docker image for Render free tier.
# Python stdlib only for the app itself; ffmpeg + yt-dlp are the tools.
FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY server.py .

ENV PORT=10000
EXPOSE 10000

# yt-dlp is on PATH from pip; server finds ffmpeg/ffprobe on PATH too.
CMD ["python3", "server.py"]
