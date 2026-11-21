#!/usr/bin/env python3
"""
Clipzy render server v1.1 — cuts REAL video clips (actual YouTube footage + audio).

Endpoints:
  GET  /health  -> {"status": "ok", ...}
  POST /clip    -> JSON {"youtubeUrl": "...", "startSec": 376, "endSec": 404}
                   returns the cut segment as an MP4 file (video + audio).

How it works:
  1. yt-dlp downloads the video (tries player clients in order: tv_embedded
     for best quality up to 720p, then android as a fallback that usually
     survives YouTube's rate-limiting, then the plain web client).
  2. If every yt-dlp client fails, it falls back to a public Invidious
     instance (best-effort: Invidious instances die often — see README).
  3. ffmpeg re-encodes an accurate cut (-ss/-to + libx264/aac), because
     stream-copy cuts snap to keyframes and come out the wrong length.
  4. The MP4 is streamed back; temp files are deleted afterwards.

Beating YouTube's bot-block ("Sign in to confirm you're not a bot"):
  The server runs on a datacenter IP, which YouTube often blocks. Two
  optional escapes, both configured by environment — see README:
    YTDLP_COOKIES=/path/to/cookies.txt   (or a cookies.txt next to server.py)
        A cookies file exported from a logged-in YouTube browser session.
        yt-dlp then looks like a real signed-in user. Never commit it.
    YTDLP_PROXY=http://user:pass@host:port
        Route yt-dlp (and Invidious fetches) through a proxy. Only a
        *residential* proxy helps — datacenter proxies get blocked the
        same way. There is no free residential proxy.

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
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

VERSION = "1.1.1"
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

# Public Invidious instances to try (best-effort fallback only). Instances
# die, block the API, or shut down without warning — the server logs each
# failure and fails cleanly when none of them works.
INVIDIOUS_INSTANCES = [
    "inv.tux.pizza",
    "vid.puffyan.us",
    "invidious.0011.lt",
    "iv.datura.network",
    "yewtu.be",
    "inv.nadeko.net",
    "invidious.projectsegfau.lt",
    "invidious.lunar.icu",
]
INVIDIOUS_TIMEOUT = 10          # seconds per instance for the API call
BROWSER_UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")

_cookies_warned = False


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


# --------------------------------------------------------------------------
# Anti-bot options: YouTube cookies + proxy (both optional, env-configured)
# --------------------------------------------------------------------------

def _cookies_arg():
    """Return ["--cookies", path] for yt-dlp if a cookies file is available.

    YTDLP_COOKIES wins; otherwise cookies.txt next to server.py, if present.
    If YTDLP_COOKIES names a file that does not exist, warn once and
    continue without cookies (never crash the request over this).

    The file is COPIED to a writable temp path first: yt-dlp opens the
    cookies file for writing (to save updated cookies), so a read-only
    source such as Render's /etc/secrets/cookies.txt fails with
    "OSError: [Errno 30] Read-only file system". The copy is what
    yt-dlp receives; the original is never written to.
    """
    global _cookies_warned
    path = os.environ.get("YTDLP_COOKIES")
    if path:
        if os.path.isfile(path):
            return ["--cookies", _writable_cookies_copy(path)]
        if not _cookies_warned:
            log(f"WARNING: YTDLP_COOKIES={path} not found, continuing without cookies")
            _cookies_warned = True
        return []
    default = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cookies.txt")
    if os.path.isfile(default):
        return ["--cookies", _writable_cookies_copy(default)]
    return []


def _writable_cookies_copy(src):
    """Copy the cookies file to a writable temp path and return that path.

    Falls back to the original path if the copy fails (yt-dlp will then
    report the real error instead of this helper hiding it).
    """
    import shutil
    import tempfile
    dst = os.path.join(tempfile.gettempdir(), "clipzy-cookies.txt")
    try:
        shutil.copyfile(src, dst)
        return dst
    except OSError as e:
        log(f"WARNING: could not copy cookies file to temp ({e}), using original path")
        return src


def _proxy_arg():
    """Return ["--proxy", url] for yt-dlp if YTDLP_PROXY is set."""
    proxy = os.environ.get("YTDLP_PROXY")
    return ["--proxy", proxy] if proxy else []


def _ytdlp_args():
    """Extra yt-dlp flags from the anti-bot options (cookies + proxy)."""
    return _cookies_arg() + _proxy_arg()


def _url_opener():
    """urllib opener honouring YTDLP_PROXY (http/https proxies only)."""
    proxy = os.environ.get("YTDLP_PROXY")
    if proxy:
        return urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    return urllib.request.build_opener()


def run(cmd, timeout):
    """Run a command, return (returncode, stdout, stderr)."""
    # Never log a proxy URL: it may contain user:pass credentials.
    shown = []
    hide_next = False
    for tok in cmd:
        if hide_next:
            shown.append("***")
            hide_next = False
        elif tok == "--proxy":
            shown.append(tok)
            hide_next = True
        else:
            shown.append(tok)
    log("$ " + " ".join(shown))
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    return p.returncode, p.stdout, p.stderr


def _extract_video_id(url):
    """Return the 11-char YouTube video id, or None."""
    m = re.search(r"[?&]v=([\w-]{11})", url)
    if m:
        return m.group(1)
    m = re.search(r"youtu\.be/([\w-]{11})", url)
    return m.group(1) if m else None


# --------------------------------------------------------------------------
# Invidious fallback (best-effort; instances die often)
# --------------------------------------------------------------------------

def _http_get_json(url, timeout=INVIDIOUS_TIMEOUT):
    """GET a URL and parse the JSON body. Raises on any failure."""
    req = urllib.request.Request(
        url, headers={"User-Agent": BROWSER_UA, "Accept": "application/json"})
    with _url_opener().open(req, timeout=timeout) as r:
        if r.status != 200:
            raise RuntimeError(f"HTTP {r.status}")
        return json.loads(r.read().decode("utf-8", "replace"))


def _invidious_metadata(video_id):
    """Fetch {id, title, duration} from any Invidious instance. None if all fail."""
    for inst in INVIDIOUS_INSTANCES:
        try:
            data = _http_get_json(f"https://{inst}/api/v1/videos/{video_id}")
        except Exception as e:  # noqa: BLE001 - try the next instance
            log(f"Invidious {inst} metadata failed: {e}")
            continue
        if not isinstance(data, dict) or data.get("error"):
            err = data.get("error") if isinstance(data, dict) else "bad response"
            log(f"Invidious {inst} metadata error: {err}")
            continue
        try:
            dur = int(data.get("lengthSeconds") or 0)
        except (TypeError, ValueError):
            dur = 0
        return {"id": data.get("videoId") or video_id,
                "title": data.get("title") or "", "duration": dur}
    return None


def _quality_height(stream):
    """Height of an Invidious stream from its qualityLabel ('720p') or height."""
    label = str(stream.get("qualityLabel") or "")
    m = re.match(r"(\d+)p", label)
    if m:
        return int(m.group(1))
    try:
        return int(stream.get("height") or 0)
    except (TypeError, ValueError):
        return 0


def _download_url(url, dst, timeout=60):
    """Stream a URL to a file. Raises on failure or empty file."""
    req = urllib.request.Request(url, headers={"User-Agent": BROWSER_UA})
    with _url_opener().open(req, timeout=timeout) as r, open(dst, "wb") as f:
        shutil.copyfileobj(r, f, length=1024 * 256)
    if os.path.getsize(dst) == 0:
        raise RuntimeError("downloaded empty file")
    return dst


def _download_invidious_streams(data, outdir, duration):
    """Download the best Invidious stream(s) for a video. Returns the file path.

    Prefers a combined (video+audio) mp4 <= 720p (<= 360p for long videos).
    Otherwise downloads the best video-only mp4 + best audio and merges them
    with ffmpeg. Raises if the response has no downloadable stream.
    """
    max_h = 360 if (duration and duration > LONG_VIDEO_THRESHOLD) else 720

    combined = []
    video_only = []
    audio_only = []
    for s in data.get("formatStreams", []) or []:
        typ = str(s.get("type") or "")
        if s.get("container") != "mp4":
            continue
        if "mp4a" in typ:  # has audio -> combined stream
            combined.append(s)
    for s in data.get("adaptiveFormats", []) or []:
        typ = str(s.get("type") or "")
        if typ.startswith("video/mp4"):
            video_only.append(s)
        elif typ.startswith("audio/"):
            audio_only.append(s)

    combined = [s for s in combined if _quality_height(s) <= max_h]
    combined.sort(key=_quality_height, reverse=True)
    if combined:
        url = combined[0].get("url")
        if url:
            log(f"Invidious: combined {combined[0].get('qualityLabel')} mp4")
            return _download_url(url, os.path.join(outdir, "source.mp4"))

    video_only = [s for s in video_only if _quality_height(s) <= max_h]
    video_only.sort(key=_quality_height, reverse=True)
    audio_only.sort(key=lambda s: int(s.get("bitrate") or 0), reverse=True)
    if video_only and audio_only:
        vurl = video_only[0].get("url")
        aurl = audio_only[0].get("url")
        if vurl and aurl:
            log(f"Invidious: adaptive {video_only[0].get('qualityLabel')} + audio, merging")
            vpath = _download_url(vurl, os.path.join(outdir, "v.mp4"))
            apath = _download_url(aurl, os.path.join(outdir, "a.m4a"))
            out = os.path.join(outdir, "source.mp4")
            rc, _o, err = run(
                [FFMPEG, "-y", "-v", "error", "-i", vpath, "-i", apath,
                 "-c:v", "copy", "-c:a", "aac", "-b:a", "128k",
                 "-movflags", "+faststart", out],
                timeout=CUT_TIMEOUT)
            if rc != 0:
                raise RuntimeError(f"Invidious merge failed: {err[-300:]}")
            return out
    raise RuntimeError("no downloadable stream in Invidious response")


def _invidious_download(video_id, outdir, duration):
    """Download a video via a public Invidious instance. Returns the file path."""
    last_err = ""
    for inst in INVIDIOUS_INSTANCES:
        try:
            data = _http_get_json(f"https://{inst}/api/v1/videos/{video_id}")
        except Exception as e:  # noqa: BLE001 - try the next instance
            last_err = f"{inst}: {e}"
            log(f"Invidious {inst} failed: {e}")
            continue
        if not isinstance(data, dict) or data.get("error"):
            last_err = f"{inst}: {data.get('error') if isinstance(data, dict) else 'bad response'}"
            log(f"Invidious {inst} error: {last_err}")
            continue
        try:
            return _download_invidious_streams(data, outdir, duration)
        except Exception as e:  # noqa: BLE001 - try the next instance
            last_err = f"{inst}: {e}"
            log(f"Invidious {inst} stream download failed: {e}")
            continue
    raise RuntimeError(f"no Invidious instance returned a usable stream ({last_err})")


# --------------------------------------------------------------------------
# yt-dlp download / metadata
# --------------------------------------------------------------------------

def video_info(url):
    """Fetch metadata (id, title, duration) without downloading.

    If yt-dlp fails *or hangs* (TimeoutExpired), fall through to the
    Invidious metadata fallback instead of dying on the spot.
    """
    try:
        rc, out, err = run(
            [YTDLP, "--extractor-args", "youtube:player_client=android",
             "--no-playlist", "--socket-timeout", "20", "--retries", "2"]
            + _ytdlp_args() + ["--dump-json", url],
            timeout=120,
        )
    except subprocess.TimeoutExpired:
        rc, out, err = -1, "", "yt-dlp metadata timed out after 120s"
    if rc == 0:
        info = json.loads(out)
        return {
            "id": info.get("id", "video"),
            "title": info.get("title", ""),
            "duration": info.get("duration") or 0,
        }
    ytdlp_err = (err or out)[-500:]
    log(f"yt-dlp metadata failed, trying Invidious: {ytdlp_err[-200:]}")
    vid = _extract_video_id(url)
    if vid:
        info = _invidious_metadata(vid)
        if info:
            log(f"metadata via Invidious: '{info['title'][:60]}' duration={info['duration']}s")
            return info
    raise RuntimeError(f"yt-dlp metadata failed: {ytdlp_err}")


def download_video(url, outdir, duration):
    """Download the video, trying player clients in order, then Invidious."""
    if duration and duration > LONG_VIDEO_THRESHOLD:
        fmt = "b[ext=mp4]/b"  # 360p merged, small file for long videos
    else:
        fmt = "bv*[height<=720][ext=mp4]+ba[ext=m4a]/b[ext=mp4]/b"
    clients = ["tv_embedded", "android", "web"]
    last_err = ""
    for client in clients:
        target = os.path.join(outdir, "source.%(ext)s")
        cmd = [
            YTDLP, "--extractor-args", f"youtube:player_client={client}",
            "-f", fmt, "--merge-output-format", "mp4",
            "--no-playlist", "--socket-timeout", "30", "--retries", "3",
        ] + _ytdlp_args() + ["-o", target, url]
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
    log("all yt-dlp clients failed, trying Invidious fallback")
    vid = _extract_video_id(url)
    if vid:
        try:
            return _invidious_download(vid, outdir, duration)
        except Exception as e:  # noqa: BLE001 - report combined failure
            last_err = f"Invidious fallback failed: {e}"
            log(last_err)
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

    def _cors(self):
        # The Clipzy website (clipzy-ai.web.app) calls this server from the
        # browser, so every response must allow cross-origin requests.
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def _json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self._cors()
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        # Browser preflight for the cross-origin POST /clip call.
        self.send_response(204)
        self.send_header("Content-Length", "0")
        self._cors()
        self.end_headers()

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
            self._cors()
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
    log(f"cookies: {'enabled' if _cookies_arg() else 'not configured'}")
    log(f"proxy: {'set' if os.environ.get('YTDLP_PROXY') else 'not configured'}")
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    log(f"clipzy-render v{VERSION} listening on 0.0.0.0:{port}")
    log(f"GET  /health   health check")
    log(f"POST /clip     {{\"youtubeUrl\", \"startSec\", \"endSec\"}} -> MP4")
    server.serve_forever()


if __name__ == "__main__":
    main()
