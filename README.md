# Clipzy render server v1

Backend that cuts **real video clips** — actual YouTube footage + audio — so
Clipzy downloads stop being slideshows. The site (a static page) cannot do
this in the browser: YouTube never hands the video file to the browser, only
an embedded player. This server downloads the video with yt-dlp, cuts the
requested segment with ffmpeg, and returns a real MP4.

## Endpoints

| Method | Path | Description |
|---|---|---|
| GET | `/health` | `{"status":"ok","service":"clipzy-render","version":"1.0.0"}` |
| POST | `/clip` | JSON `{"youtubeUrl","startSec","endSec"}` → MP4 file download |

Example:

```bash
curl -X POST http://localhost:8080/clip \
  -H "Content-Type: application/json" \
  -d '{"youtubeUrl":"https://www.youtube.com/watch?v=jNQXAC9IVRw","startSec":5,"endSec":15}' \
  -o clip.mp4
```

Rules: `youtubeUrl` must be a `youtube.com/watch?v=…` or `youtu.be/…` link;
`0 <= startSec < endSec`; clip at most 300 seconds; video at most 4 hours.
Errors come back as JSON `{"error": "..."}` with HTTP 400/429/500.

## Run locally

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt   # installs yt-dlp
# ffmpeg + ffprobe must be on PATH (apt install ffmpeg)
PORT=8080 python3 server.py
```

Requires Python 3.11+, ffmpeg 7+ (tested with ffmpeg 8.1.2), yt-dlp
2026.10.26. No other dependencies — the server is Python stdlib only.

## How it works

1. **Metadata** — `yt-dlp --dump-json` (android client) gets duration/title.
   The plain web client is HTTP 429 rate-limited from this network, so the
   server tries player clients in order: `tv_embedded` (best quality, up to
   720p), then `android` (usually survives rate-limiting, 360p merged).
2. **Download** — yt-dlp fetches the video and merges it to MP4.
   Videos over 20 minutes are fetched at 360p to keep the download small.
3. **Cut** — ffmpeg re-encodes an accurate cut (`-ss`/`-to`, libx264 + AAC,
   `+faststart`). Stream-copy was tried and rejected: it snaps to keyframes,
   so a 5–15s request came back 15s long.
4. The MP4 is streamed back (`Content-Disposition: attachment`) and the
   temp files are deleted. At most 2 jobs run at once.

## Local test evidence (2026-11-20, this machine)

| Test | Result |
|---|---|
| `GET /health` | `{"status":"ok","service":"clipzy-render","version":"1.0.0"}` |
| "Me at the zoo" (19 s video), clip 5–15 s | HTTP 200, 356,488 bytes, **9.8 s total** (download 9.0 s, cut 0.8 s). ffprobe: h264 320×240 + aac, **10.000 s exactly**. Full decode: no errors. |
| "Big Buck Bunny" (635 s video), clip 376–404 s | HTTP 200, 2,730,034 bytes, **90.2 s total** (download 78.0 s for 81.8 MB at 720p, cut 12.2 s). ffprobe: h264 **1280×720** + aac, **28.000 s exactly**. Full decode: no errors. |
| Bad URL / reversed range / end past video end / clip > 300 s / unknown path | Correct JSON errors (400/404). |

## Known limits (honest)

- **YouTube blocking is real.** From this machine the web client gets HTTP
  429 (Too Many Requests) and plain page fetches time out; the android +
  tv_embedded clients work today, but YouTube changes its defences often and
  yt-dlp needs regular updates (`pip install -U yt-dlp`). On Render the IP
  will be different, so behaviour may differ — better or worse.
- **Quality is capped at 720p** in v1 (YouTube's SABR experiment hides
  higher formats from these clients). Old/low-res videos stay low-res.
- **The whole video downloads before cutting.** A 50-minute video at 720p
  is ~1 GB and slow; long videos fall back to 360p. (Future work:
  `--download-sections` to fetch only the needed range — tested once, it
  worked but was slow on this network, 42 s for a 10 s section.)
- **Render free tier is small**: 512 MB RAM, shared CPU, sleeps after
  15 min idle (first request after sleep is slow), ephemeral disk.
  A 28 s clip from a 10-min video took 90 s here; on free Render it will
  likely be slower, and very long videos may time out. A paid instance
  fixes most of that.
- **Copyright**: downloading other people's / TV-show videos (e.g. Kapil
  Sharma clips) and re-serving them can violate YouTube's terms and
  copyright law. Use it for your own videos or properly licensed content.

## Deploy on Render free tier (not done yet — needs your accounts)

1. **GitHub account** (free): create a repo, push this folder
   (`server.py`, `requirements.txt`, `Dockerfile`, `render.yaml`).
2. **Render account** (free, render.com): New → Blueprint → connect the
   repo. Render reads `render.yaml` and creates a free web service
   `clipzy-render` with Docker. (Or: New → Web Service → pick the repo,
   Runtime Docker, Plan Free.)
3. Wait for the first deploy, then check
   `https://clipzy-render.onrender.com/health` returns `{"status":"ok"...}`.
   The first request after idle takes ~1 min (free tier wakes from sleep).
4. **Wire it into Clipzy**: in the site's code, change the Download buttons
   to `POST {render-url}/clip` with `{youtubeUrl, startSec, endSec}` and
   save the returned MP4. Then redeploy the Clipzy site
   (`~/workspace/firebase-deploy-clipzy/` → `firebase deploy`).
5. Keep yt-dlp updated: rebuild/redeploy every few weeks, or when YouTube
   downloads start failing.

## Files

- `server.py` — the whole service (stdlib only)
- `requirements.txt` — yt-dlp (pinned, tested version)
- `Dockerfile` — python:3.12-slim + ffmpeg + yt-dlp
- `render.yaml` — Render Blueprint (free web service, `/health` check)
