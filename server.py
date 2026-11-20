#!/usr/bin/env python3
"""
Clipzy render server v1 — cuts REAL video clips (actual YouTube footage + audio).

Endpoints:
  GET  /health  -> {"status": "ok", ...}
  POST /clip    -> JSON {"youtubeUrl": "...", "startSec": 376, "endSec": 404}
                   returns the cut segment as an MP4 file (video + audio).

How it works:
  1. yt-dlp downloads the video (tries player clients in order: tv_embedded
     for best quality up to 720p, then android as a fallback that usually
     survives YouTube's rate-limiting; the plain web client is 429-blocked
     from this network).
  2. ffmpeg re-encodes an accurate cut (-ss/-to + libx264/aac), because
     stream-copy cuts snap to keyframes and come out the wrong length.
  3. The MP4 is streamed back; temp files are deleted afterwards.

Free-tier notes:
  - No paid services, no accounts. Python stdlib only (no Flask/FastAPI),
    yt-dlp + ffmpeg are the only tools.
  - Long videos are slow: the whole video is downloaded before cutting.
    Videos over 20 minutes are fetched at 360p (format 18) to keep the
    download small; shorter videos go up to 720p.
  - One clip is capped at 300 seconds. Two jobs run at once at most.

Run locally:
  python3 -m venv .venv && .venv/bin/pip install yt-dlp
  python3 server.py            # listens on $PORT (default 8080)
"""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

VERSION = "1.0.0"
MAX_CLIP_SECONDS = 300          # longest clip we will render
MAX_VIDEO_SECONDS = 4 * 3600    # refuse videos longer than 4h
MAX_CONCURRENT_JOBS = 2
DOWNLOAD_TIMEOUT = 600          # seconds for the yt-dlp download
CUT_TIMEOUT = 600               # seconds for the ffmpeg cut
YTDLP = os.environ.get("YTDLP_BIN", "yt-dlp")
FFMPEG = os.environ.get("FFMPEG_BIN", "ffmpeg")
FFPROBE = os.environ.get("FFPROBE_BIN", "ffprobe")
LONG_VIDEO_THRESHOLD = 20 * 60  # >20 min videos are fetched at 360p

JOBS = threading.Semaphore(MAX_CONCURRENT_JOBS)

YOUTUBE_RE = re.compile(
    r"^(https?://)?(www\.|m\.)?(youtube\.com/watch\?[^ ]*v=[\w-]{11}|youtu\.be/[\w-]{11})([&#?][^ ]*)?$"
)


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def run(cmd, timeout):
    """Run a command, return (returncode, stdout, stderr)."""
    log("$ " + " ".join(cmd))
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    return p.returncode, p.stdout, p.stderr


def video_info(url):
    """Fetch metadata (id, title, duration) without downloading."""
    rc, out, err = run(
        [YTDLP, "--extractor-args", "youtube:player_client=android",
         "--no-playlist", "--socket-timeout", "30",
         "--dump-json", url],
        timeout=120,
    )
    if rc != 0:
        raise RuntimeError(f"yt-dlp metadata failed: {(err or out)[-500:]}")
    info = json.loads(out)
    return {
        "id": info.get("id", "video"),
        "title": info.get("title", ""),
        "duration": info.get("duration") or 0,
    }


def download_video(url, outdir, duration):
    """Download the video, trying player clients in order. Returns the file path."""
    if duration and duration > LONG_VIDEO_THRESHOLD:
        fmt = "b[ext=mp4]/b"  # 360p merged, small file for long videos
    else:
        fmt = "bv*[height<=720][ext=mp4]+ba[ext=m4a]/b[ext=mp4]/b"
    clients = ["tv_embedded", "android"]
    last_err = ""
    for client in clients:
        target = os.path.join(outdir, "source.%(ext)s")
        cmd = [
            YTDLP, "--extractor-args", f"youtube:player_client={client}",
            "-f", fmt, "--merge-output-format", "mp4",
            "--no-playlist", "--socket-timeout", "30", "--retries", "3",
            "-o", target, url,
        ]
        try:
            rc, out, err = run(cmd, DOWNLOAD_TIMEOUT)
        except subprocess.TimeoutExpired:
            last_err = f"download timed out with client {client}"
            log(last_err)
            continue
        if rc == 0:
            for f in os.listdir(outdir):
                if f.startswith("source."):
                    return os.path.join(outdir, f)
        last_err = (err or out)[-500:]
        log(f"client {client} failed: {last_err}")
    raise RuntimeError(f"download failed with all clients: {last_err}")


def probe(path):
    """Return (duration_seconds, has_video, has_audio) via ffprobe."""
    rc, out, err = run(
        [FFPROBE, "-v", "error", "-show_entries",
         "stream=codec_type", "-show_entries", "format=duration",
         "-of", "json", path],
        timeout=60,
    )
    if rc != 0:
        raise RuntimeError(f"ffprobe failed: {err[-300:]}")
    data = json.loads(out)
    types = {s.get("codec_type") for s in data.get("streams", [])}
    return (float(data["format"]["duration"]),
            "video" in types, "audio" in types)


def cut_clip(src, dst, start, end):
    """Accurate re-encode cut. Returns (duration, has_video, has_audio)."""
    cmd = [
        FFMPEG, "-y", "-v", "error",
        "-ss", str(start), "-to", str(end), "-i", src,
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
        "-c:a", "aac", "-b:a", "128k",
        "-movflags", "+faststart", dst,
    ]
    rc, out, err = run(cmd, CUT_TIMEOUT)
    if rc != 0:
        raise RuntimeError(f"ffmpeg cut failed: {err[-500:]}")
    return probe(dst)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _error(self, code, message):
        self._json(code, {"error": message})

    def do_GET(self):
        if urlparse(self.path).path == "/health":
            self._json(200, {"status": "ok", "service": "clipzy-render",
                             "version": VERSION})
        else:
            self._error(404, "not found (try GET /health or POST /clip)")

    def do_POST(self):
        if urlparse(self.path).path != "/clip":
            self._error(404, "not found (try GET /health or POST /clip)")
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0 or length > 4096:
                self._error(400, "invalid request body")
                return
            try:
                req = json.loads(self.rfile.read(length))
            except json.JSONDecodeError:
                self._error(400, "body must be JSON")
                return
            url = str(req.get("youtubeUrl") or "").strip()
            try:
                start = float(req.get("startSec"))
                end = float(req.get("endSec"))
            except (TypeError, ValueError):
                self._error(400, "startSec and endSec must be numbers")
                return
            if not YOUTUBE_RE.match(url):
                self._error(400, "youtubeUrl must be a youtube.com/watch or youtu.be link")
                return
            if start < 0 or end <= start or (end - start) > MAX_CLIP_SECONDS:
                self._error(400, f"bad range: need 0 <= start < end, clip <= {MAX_CLIP_SECONDS}s")
                return
            if not JOBS.acquire(blocking=False):
                self._error(429, "server busy, try again in a moment")
                return
            try:
                self._render(url, start, end)
            finally:
                JOBS.release()
        except Exception as e:  # noqa: BLE001 - report as 500, keep serving
            log(f"ERROR: {e}")
            try:
                self._error(500, f"internal error: {e}")
            except Exception:
                pass

    def _render(self, url, start, end):
        tmpdir = tempfile.mkdtemp(prefix="clipzy-")
        try:
            t0 = time.time()
            info = video_info(url)
            log(f"video {info['id']} '{info['title'][:60]}' duration={info['duration']}s")
            if info["duration"] and info["duration"] > MAX_VIDEO_SECONDS:
                self._error(400, "video too long (over 4 hours)")
                return
            if info["duration"] and end > info["duration"]:
                self._error(400, f"endSec {end} is past the video end ({info['duration']}s)")
                return
            src = download_video(url, tmpdir, info["duration"])
            t_dl = time.time() - t0
            log(f"downloaded {os.path.getsize(src)} bytes in {t_dl:.1f}s")
            out = os.path.join(tmpdir, "clip.mp4")
            dur, has_v, has_a = cut_clip(src, out, start, end)
            t_cut = time.time() - t0 - t_dl
            log(f"cut {dur:.2f}s video={has_v} audio={has_a} in {t_cut:.1f}s")
            if not (has_v and has_a):
                self._error(500, "rendered clip is missing video or audio")
                return
            size = os.path.getsize(out)
            fname = f"clipzy_{info['id']}_{int(start)}-{int(end)}.mp4"
            self.send_response(200)
            self.send_header("Content-Type", "video/mp4")
            self.send_header("Content-Disposition", f'attachment; filename="{fname}"')
            self.send_header("Content-Length", str(size))
            self.send_header("X-Clip-Duration", f"{dur:.2f}")
            self.end_headers()
            with open(out, "rb") as f:
                shutil.copyfileobj(f, self.wfile, length=1024 * 256)
            log(f"served {fname} ({size} bytes) total={time.time() - t0:.1f}s")
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def log_message(self, *args):
        pass  # keep stdout clean; we log ourselves


def main():
    port = int(os.environ.get("PORT", "8080"))
    for tool in (YTDLP, FFMPEG, FFPROBE):
        if not shutil.which(tool):
            print(f"missing required tool: {tool}", file=sys.stderr)
            sys.exit(1)
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    log(f"clipzy-render v{VERSION} listening on 0.0.0.0:{port}")
    log(f"GET  /health   health check")
    log(f"POST /clip     {{\"youtubeUrl\", \"startSec\", \"endSec\"}} -> MP4")
    server.serve_forever()


if __name__ == "__main__":
    main()
