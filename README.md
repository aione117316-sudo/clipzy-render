# Clipzy render server v1.1

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
   If that fails, the server tries a public Invidious instance for metadata.
   The plain web client is HTTP 429 rate-limited from this network, so the
   server tries player clients in order: `tv_embedded` (best quality, up to
   720p), then `android` (usually survives rate-limiting, 360p merged),
   then `web`, then — if every yt-dlp client fails — a public Invidious
   instance for a direct stream URL (best-effort; see below).
2. **Download** — yt-dlp fetches the video and merges it to MP4.
   Videos over 20 minutes are fetched at 360p to keep the download small.
3. **Cut** — ffmpeg re-encodes an accurate cut (`-ss`/`-to`, libx264 + AAC,
   `+faststart`). Stream-copy was tried and rejected: it snaps to keyframes,
   so a 5–15s request came back 15s long.
4. The MP4 is streamed back (`Content-Disposition: attachment`) and the
   temp files are deleted. At most 2 jobs run at once.

## Unblocking YouTube: cookies, proxy, Invidious (v1.1)

YouTube blocks datacenter IPs ("Sign in to confirm you're not a bot"). On
Render the live server hit exactly that: every cut returned HTTP 500, while
the same code works from a home network. Three escapes, in order of
reliability. All are optional env configuration — no code changes needed.

### 1. YouTube cookies (most reliable, free)

Give yt-dlp a cookies file exported from a **logged-in YouTube session in
your own browser**, so YouTube sees a real signed-in user instead of a bot.

- **Export:** in Chrome/Firefox install the extension **"Get cookies.txt
  LOCALLY"**, open youtube.com while logged in, export `cookies.txt`.
  Or from a terminal: `yt-dlp --cookies-from-browser chrome --cookies
  cookies.txt "https://www.youtube.com/watch?v=jNQXAC9IVRw"`.
- **Local:** put `cookies.txt` next to `server.py` (it is auto-detected),
  or set `YTDLP_COOKIES=/path/to/cookies.txt`.
- **On Render:** Dashboard → service → Environment → **Secret Files** →
  add a file named `cookies.txt` with the exported contents, then set the
  env var `YTDLP_COOKIES=/etc/secrets/cookies.txt`. (The Dockerfile only
  copies `server.py`, so a cookies file in the repo would never reach the
  image anyway.)
- **NEVER commit `cookies.txt` to git** — it is a login session for your
  account. It is in `.gitignore`.
- **Cookies expire** (typically weeks) and must be re-exported when cuts
  start failing with 500s again. Use a secondary/throwaway YouTube account,
  not your main one — if YouTube flags the session, only that account is
  affected.

### 2. Proxy (only residential proxies help)

Set `YTDLP_PROXY=http://user:pass@host:port` (locally as an env var, or as
an env var on the Render service). yt-dlp — and the Invidious fetches —
route through it.

- Honest truth: **there is no free residential proxy.** Free proxy lists
  are datacenter IPs and get blocked exactly like Render's IP, so they do
  not help. A cheap residential proxy costs a few dollars a month; only
  pay for one if the free cookies route stops working.

### 3. Invidious fallback (best-effort, often dead)

If every yt-dlp client fails, the server queries public Invidious
instances (`/api/v1/videos/<id>`) for a direct stream URL and downloads
that instead. The instance list lives in `server.py`
(`INVIDIOUS_INSTANCES`). Invidious instances shut down, block their API,
or go behind Cloudflare without warning — during local testing on
2026-11-20 **no public instance returned usable data** from this network
(nadeko: API disabled; yewtu.be: 403; projectsegfau.lt: shut down;
f5.si: Cloudflare 530; the rest unreachable). So treat this as a bonus,
not a fix: it may work from Render's network where some instances are
reachable, and it fails cleanly with a clear error when none of them work.

## Local test evidence (2026-11-20, this machine)

### v1.0 baseline (morning, before the bot-block worsened)

| Test | Result |
|---|---|
| `GET /health` | `{"status":"ok","service":"clipzy-render","version":"1.0.0"}` |
| "Me at the zoo" (19 s video), clip 5–15 s | HTTP 200, 356,488 bytes, **9.8 s total** (download 9.0 s, cut 0.8 s). ffprobe: h264 320×240 + aac, **10.000 s exactly**. Full decode: no errors. |
| "Big Buck Bunny" (635 s video), clip 376–404 s | HTTP 200, 2,730,034 bytes, **90.2 s total** (download 78.0 s for 81.8 MB at 720p, cut 12.2 s). ffprobe: h264 **1280×720** + aac, **28.000 s exactly**. Full decode: no errors. |
| Bad URL / reversed range / end past video end / clip > 300 s / unknown path | Correct JSON errors (400/404). |

### v1.1 tests (afternoon — YouTube blocking got worse during the day)

**Important: the v1.0 cuts above passed in the morning; by the afternoon
every yt-dlp client (tv_embedded, android, web) hangs on read timeouts or
gets the explicit "Sign in to confirm you're not a bot" page — even
locally.** So the v1.1 baseline cuts could NOT be re-verified end-to-end
here. The download and cut code is unchanged from v1.0 (which passed
both cuts), and the new code paths were verified as follows:

| Test | Result |
|---|---|
| `GET /health` (v1.1.0) | `{"status":"ok","service":"clipzy-render","version":"1.1.0"}` |
| POST /clip "Me at the zoo" 5–15 s, no env | HTTP 500 after 146.5 s: `yt-dlp metadata failed: yt-dlp metadata timed out after 120s`. Server log proves the fix: the hang is now caught and the Invidious metadata fallback ran through all 8 instances, each failing cleanly, then a clean 500 (no crash). |
| POST /clip with dummy cookies.txt (`YTDLP_COOKIES`) | Startup log `cookies: enabled`; yt-dlp command line shows `--cookies <file>` passed. HTTP 500 after 144.5 s (a fake cookie cannot unblock — expected). Proves the flag is wired end-to-end. |
| POST /clip with `YTDLP_COOKIES` pointing at a missing file | Startup warns once (`WARNING: YTDLP_COOKIES=... not found, continuing without cookies`), `cookies: not configured`, request proceeds without `--cookies`. This run got YouTube's explicit bot-check page: `Sign in to confirm you're not a bot. Use --cookies-from-browser or --cookies...` — yt-dlp itself prescribes the cookies fix. |
| POST /clip with dead proxy (`YTDLP_PROXY=http://127.0.0.1:9`) | Startup log `proxy: set`; HTTP 500 in 1.0 s (`[Errno 111] Connection refused`), clean JSON error, server stayed up. Invidious fallback also ran (failed fast through the same dead proxy). Log shows `--proxy ***` — proxy credentials are redacted from logs. |
| Direct tests of the new code paths | `_extract_video_id`, `_cookies_arg` (no file / missing file / real file), `_proxy_arg` all pass. `_invidious_metadata` → `None` after 25.1 s (all 8 instances failed cleanly, each logged). `_invidious_download` → clean `RuntimeError: no Invidious instance returned a usable stream`. `_download_invidious_streams({})` → clean `RuntimeError: no downloadable stream in Invidious response`. |
| Invidious instance probe (2026-11-20) | No public instance returned usable data from this network: nadeko API disabled (403), yewtu.be 403, nerdvpn.de 403, projectsegfau.lt "Invidious has shutdown", f5.si Cloudflare 530, lunar.icu 502, materialio.us 500, the rest unreachable; Piped API backends unreachable; api.invidious.io unreachable; cobalt unreachable. |
| Bug fixed during testing | v1.0 `video_info()` let a yt-dlp hang raise `TimeoutExpired` uncaught, so the Invidious fallback never ran. v1.1 catches it and falls through to Invidious (verified in the test log). |

## Known limits (honest)

- **YouTube blocking is real — and it hit the live server.** From this
  machine the web client gets HTTP 429 (Too Many Requests) and plain page
  fetches time out; the android + tv_embedded clients work here, but on
  Render's IP YouTube returns "Sign in to confirm you're not a bot" and
  every cut fails with HTTP 500 (verified 2026-11-20; a live retest after
  the CORS redeploy timed out after 180 s with no response at all). The
  v1.1 escapes above (cookies → proxy → Invidious) are the fixes: cookies
  are the one that actually works for free, but they must be supplied by
  the account owner and refreshed when they expire.
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

## Deploy on Render free tier

Live since 2026-11-20: https://clipzy-render.onrender.com (repo
https://github.com/aione117316-sudo/clipzy-render, Render account
aione117316@gmail.com, free plan, Blueprint from `render.yaml`).

1. **GitHub account** (free): create a repo, push this folder
   (`server.py`, `requirements.txt`, `Dockerfile`, `render.yaml`,
   `.gitignore` — never `cookies.txt`).
2. **Render account** (free, render.com): New → Blueprint → connect the
   repo. Render reads `render.yaml` and creates a free web service
   `clipzy-render` with Docker. (Or: New → Web Service → pick the repo,
   Runtime Docker, Plan Free.)
3. Wait for the first deploy, then check
   `https://clipzy-render.onrender.com/health` returns `{"status":"ok"...}`.
   The first request after idle takes ~1 min (free tier wakes from sleep).
   Render auto-deploys on every push to the repo's main branch.
4. **Wire it into Clipzy**: in the site's code, change the Download buttons
   to `POST {render-url}/clip` with `{youtubeUrl, startSec, endSec}` and
   save the returned MP4. Then redeploy the Clipzy site
   (`~/workspace/firebase-deploy-clipzy/` → `firebase deploy`).
   (Already done: the live site posts to the server first and falls back
   to an in-browser slideshow when the server fails.)
5. **If YouTube blocks the server (HTTP 500 cuts):** add a cookies file
   as a Render Secret File and set env `YTDLP_COOKIES=/etc/secrets/cookies.txt`,
   or set env `YTDLP_PROXY` to a residential proxy URL — see the
   "Unblocking YouTube" section above. Either change needs only a Render
   redeploy (or a service restart for env vars).
6. Keep yt-dlp updated: rebuild/redeploy every few weeks, or when YouTube
   downloads start failing.

## Files

- `server.py` — the whole service (stdlib only)
- `requirements.txt` — yt-dlp (pinned, tested version)
- `Dockerfile` — python:3.12-slim + ffmpeg + yt-dlp
- `render.yaml` — Render Blueprint (free web service, `/health` check)
- `.gitignore` — keeps `cookies.txt`, `.venv/`, `*.mp4` out of git
