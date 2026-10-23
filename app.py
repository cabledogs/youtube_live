"""yt-live-tv: stream YouTube channels as live TV channels, no downloads.

Pretends to be an HDHomeRun tuner so Plex (or Jellyfin/Emby) can add it under
Live TV & DVR. Each configured channel plays its YouTube videos back-to-back on
a fixed, repeating schedule, pulled from YouTube on demand with yt-dlp and
remuxed to MPEG-TS with ffmpeg.
"""

import hashlib
import json
import logging
import os
import random
import re
import subprocess
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from xml.sax.saxutils import escape

import yaml

from local_guide import LocalGuide

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("yt-live-tv")

CONFIG_PATH = os.environ.get("CONFIG", "/config/config.yaml")
with open(CONFIG_PATH) as f:
    CFG = yaml.safe_load(f)

PORT = int(CFG.get("port", 5004))
BASE_URL = CFG.get("base_url", f"http://localhost:{PORT}").rstrip("/")
FORMAT = CFG.get("format", "bv*[height<=1080][vcodec^=avc1]+ba[ext=m4a]/b[height<=1080]")
REFRESH_HOURS = float(CFG.get("refresh_hours", 6))
TUNERS = int(CFG.get("tuners", 4))
COOKIES = CFG.get("cookies")
# Extra yt-dlp options used when getting video links, e.g.
# ["--extractor-args", "youtube:player_client=web_safari"]
YTDLP_ARGS = [str(a) for a in CFG.get("ytdlp_args", [])]
GUIDE_HOURS = int(CFG.get("guide_hours", 48))
# Fixed reference point so the schedule is the same across restarts.
ANCHOR = 1_700_000_000


def ytdlp(*args):
    cmd = ["yt-dlp", "--no-warnings", *args]
    if COOKIES:
        cmd[1:1] = ["--cookies", COOKIES]
    return subprocess.run(cmd, capture_output=True, text=True, timeout=600)


def normalize_url(url):
    # A bare channel URL lists its tabs (Videos/Shorts/Live); we want the videos tab.
    if re.search(r"youtube\.com/(@[^/]+|channel/[^/]+|c/[^/]+|user/[^/]+)/?$", url):
        return url.rstrip("/") + "/videos"
    return url


class Channel:
    def __init__(self, c):
        self.number = str(c["number"])
        self.name = c["name"]
        self.urls = [normalize_url(u) for u in (c["urls"] if "urls" in c else [c["url"]])]
        self.shuffle = c.get("shuffle", True)
        self.max_videos = int(c.get("max_videos", 500))
        self.min_duration = int(c.get("min_duration", 60))  # skips Shorts that slip through
        # Skip videos longer than this many seconds (e.g. 3600 = under an hour). 0 = no limit.
        self.max_duration = int(c.get("max_duration", CFG.get("max_duration", 0)) or 0)
        self.videos = []  # list of dicts: id, title, duration, description

    def refresh(self):
        videos = []
        for url in self.urls:
            r = ytdlp("--flat-playlist", "-J", "--playlist-end", str(self.max_videos), url)
            if r.returncode != 0:
                log.error("[%s] failed to list %s: %s", self.name, url, r.stderr.strip()[-500:])
                continue
            for e in json.loads(r.stdout).get("entries") or []:
                dur = e.get("duration")
                if not e.get("id") or not dur or dur < self.min_duration:
                    continue
                if self.max_duration and dur > self.max_duration:
                    continue
                if e.get("live_status") in ("is_live", "is_upcoming"):
                    continue
                videos.append({
                    "id": e["id"],
                    "title": e.get("title") or e["id"],
                    "duration": float(dur),
                    "description": (e.get("description") or "")[:500],
                })
        if not videos:
            log.warning("[%s] no playable videos found, keeping previous list", self.name)
            return
        if self.shuffle:
            random.Random(self.number).shuffle(videos)
        self.videos = videos  # atomic swap; running streams keep their own snapshot
        log.info("[%s] %d videos, %.1f h per cycle", self.name, len(videos),
                 sum(v["duration"] for v in videos) / 3600)

    @staticmethod
    def locate(videos, ts):
        """Return (index, offset_seconds, start_ts) of what is airing at ts."""
        total = sum(v["duration"] for v in videos)
        pos = (ts - ANCHOR) % total
        for i, v in enumerate(videos):
            if pos < v["duration"]:
                return i, pos, ts - pos
            pos -= v["duration"]
        return 0, 0.0, ts

    def schedule(self, start_ts, end_ts):
        videos = self.videos
        if not videos:
            return
        i, offset, t = self.locate(videos, start_ts)
        while t < end_ts:
            v = videos[i]
            yield t, t + v["duration"], v
            t += v["duration"]
            i = (i + 1) % len(videos)


CHANNELS = {str(c["number"]): Channel(c) for c in CFG["channels"]}


def refresh_loop():
    while True:
        for ch in CHANNELS.values():
            try:
                ch.refresh()
            except Exception:
                log.exception("[%s] refresh crashed", ch.name)
        time.sleep(REFRESH_HOURS * 3600)


_url_cache = {}  # video_id -> (expires_ts, urls)
_url_lock = threading.Lock()
LAST_ERROR = {"time": None, "video": None, "error": None}


def resolve(video_id):
    """Get direct media URL(s) for a video, cached until shortly before YouTube expires them."""
    with _url_lock:
        hit = _url_cache.get(video_id)
    if hit and hit[0] > time.time():
        return hit[1]
    r = ytdlp("-f", FORMAT, "-j", *YTDLP_ARGS, f"https://www.youtube.com/watch?v={video_id}")
    urls = []
    if r.returncode == 0 and r.stdout.strip():
        info = json.loads(r.stdout.splitlines()[0])
        # Each stream: (url, headers). googlevideo answers 403 unless we send the same
        # headers (User-Agent etc.) yt-dlp used when it got the link.
        for f in info.get("requested_formats") or [info]:
            if f.get("url"):
                urls.append((f["url"], f.get("http_headers") or info.get("http_headers") or {}))
    if not urls:
        err = r.stderr.strip()[-500:]
        log.warning("resolve %s failed: %s", video_id, err)
        LAST_ERROR.update(time=time.strftime("%Y-%m-%d %H:%M:%S"), video=video_id, error=err)
        return []
    m = re.search(r"[?&/]expire[=/](\d+)", urls[0][0])
    expires = min(int(m.group(1)) - 900, time.time() + 4 * 3600) if m else time.time() + 3600
    with _url_lock:
        _url_cache[video_id] = (expires, urls)
    return urls


BAD_VIDEOS = {}   # video_id -> skip until (YouTube refuses to stream these to us)
_checked = {}     # video_id -> link expiry; links already confirmed to stream
BAD_HOURS = 6


def is_bad(video_id):
    return BAD_VIDEOS.get(video_id, 0) > time.time()


def mark_bad(video_id, why):
    if not is_bad(video_id):
        log.warning("skipping video %s for %dh: %s", video_id, BAD_HOURS, why)
    BAD_VIDEOS[video_id] = time.time() + BAD_HOURS * 3600
    forget(video_id)


def link_works(urls):
    for u, headers in urls:
        if u.startswith("http"):
            try:
                fetch_range({"url": u, "headers": dict(headers)}, 0, 1023).close()
            except Exception as e:
                return str(e)
    return None


def check_playable(video_id):
    """Resolve and test-download 1 KB of each stream. Marks refused videos as bad."""
    if is_bad(video_id):
        return False
    if _checked.get(video_id, 0) > time.time():
        return True
    for attempt in range(2):
        urls = resolve(video_id)
        if not urls:
            mark_bad(video_id, "no stream links")
            return False
        err = link_works(urls)
        if not err:
            with _url_lock:
                _checked[video_id] = _url_cache.get(video_id, (time.time() + 3600,))[0]
            return True
        forget(video_id)
    mark_bad(video_id, err)
    return False


def prefetch_loop():
    """Keep tested links ready for what's airing now and next on every channel, so
    tuning starts within a second instead of Plex giving up ("Could not tune channel")."""
    while True:
        for ch in CHANNELS.values():
            videos = ch.videos
            if not videos:
                continue
            i, _, _ = Channel.locate(videos, time.time())
            ready = 0
            for k in range(min(12, len(videos))):  # stop after 2 good ones
                try:
                    if check_playable(videos[(i + k) % len(videos)]["id"]):
                        ready += 1
                except Exception:
                    log.exception("[%s] prefetch failed", ch.name)
                if ready >= 2:
                    break
        now = time.time()
        with _url_lock:
            for vid in [k for k, (exp, _) in _url_cache.items() if exp < now]:
                del _url_cache[vid]
        for d in (_checked, BAD_VIDEOS):
            for vid in [k for k, exp in d.items() if exp < now]:
                del d[vid]
        # Check again soon while channels are still loading at startup.
        time.sleep(60 if all(ch.videos for ch in CHANNELS.values()) else 5)


def status():
    out = {"last_youtube_error": LAST_ERROR, "skipped_videos": len(BAD_VIDEOS), "channels": []}
    for ch in CHANNELS.values():
        videos = ch.videos
        entry = {"number": ch.number, "name": ch.name, "videos": len(videos)}
        if videos:
            i, offset, _ = Channel.locate(videos, time.time())
            with _url_lock:
                ready = videos[i]["id"] in _checked
            entry.update(now_playing=videos[i]["title"], video_id=videos[i]["id"],
                         position=f"{int(offset // 60)}m of {int(videos[i]['duration'] // 60)}m",
                         link_ready=ready, skipped=is_bad(videos[i]["id"]))
        out["channels"].append(entry)
    return out


# --- Relay -------------------------------------------------------------------
# googlevideo.com rejects (403) the single open-ended request ffmpeg makes for a
# whole file; it only serves bounded ranges, like yt-dlp's own downloader uses.
# So ffmpeg reads from this local relay, which supports seeking and fetches from
# YouTube in chunks.

RELAY_CHUNK = 10 * 1024 * 1024
_relay = {}  # token -> {"url", "headers", "size", "expires"}
_relay_lock = threading.Lock()


def relay_url(url, headers):
    token = hashlib.sha1(url.encode()).hexdigest()[:20]
    m = re.search(r"[?&]clen=(\d+)", url)
    with _relay_lock:
        now = time.time()
        for t in [t for t, e in _relay.items() if e["expires"] < now]:
            del _relay[t]
        _relay.setdefault(token, {"url": url, "headers": dict(headers),
                                  "size": int(m.group(1)) if m else None,
                                  "expires": now + 6 * 3600})
    return f"http://127.0.0.1:{PORT}/relay/{token}"


def fetch_range(entry, start, end):
    """Open a bounded byte range upstream. Falls back to YouTube's ?range= form."""
    url, headers = entry["url"], entry["headers"]
    try:
        return urlopen(Request(url, headers={**headers, "Range": f"bytes={start}-{end}"}), timeout=30)
    except HTTPError as e:
        if e.code not in (403, 416):
            raise
        log.info("relay: Range header refused (%s), trying range= parameter", e.code)
    sep = "&" if "?" in url else "?"
    return urlopen(Request(f"{url}{sep}range={start}-{end}", headers=headers), timeout=30)


def relay_size(entry):
    if entry["size"] is None:
        with fetch_range(entry, 0, 0) as r:
            cr = r.headers.get("Content-Range", "")
            entry["size"] = int(cr.rsplit("/", 1)[1]) if "/" in cr else int(r.headers.get("Content-Length", 0))
    return entry["size"]


def serve_relay(handler, token):
    with _relay_lock:
        entry = _relay.get(token)
    if not entry:
        handler.send_error(404)
        return
    try:
        size = relay_size(entry)
    except Exception as e:
        log.warning("relay: upstream refused: %s", e)
        handler.send_error(502)
        return
    start, end = 0, size - 1
    rng = handler.headers.get("Range")
    if rng and (m := re.match(r"bytes=(\d*)-(\d*)", rng)):
        if m.group(1):
            start = int(m.group(1))
        if m.group(2):
            end = min(int(m.group(2)), size - 1)
    if start >= size:
        handler.send_response(416)
        handler.send_header("Content-Range", f"bytes */{size}")
        handler.send_header("Content-Length", "0")
        handler.end_headers()
        return
    handler.send_response(206 if rng else 200)
    handler.send_header("Content-Type", "application/octet-stream")
    handler.send_header("Accept-Ranges", "bytes")
    handler.send_header("Content-Length", str(end - start + 1))
    if rng:
        handler.send_header("Content-Range", f"bytes {start}-{end}/{size}")
    handler.end_headers()
    if handler.command == "HEAD":
        return
    pos = start
    try:
        while pos <= end:
            stop = min(pos + RELAY_CHUNK - 1, end)
            before = pos
            with fetch_range(entry, pos, stop) as r:
                while pos <= stop and (data := r.read(min(65536, stop - pos + 1))):
                    handler.wfile.write(data)
                    pos += len(data)
            if pos == before:
                raise IOError("upstream returned no data")
    except (BrokenPipeError, ConnectionResetError):
        pass  # ffmpeg closed or seeked elsewhere
    except Exception as e:
        log.warning("relay: upstream error at byte %d: %s", pos, e)
        handler.close_connection = True


def _ffmpeg_pacing():
    """Real-time pacing, but with an initial burst when this ffmpeg supports it, so
    Plex gets a few seconds of buffer right away instead of timing out."""
    try:
        h = subprocess.run(["ffmpeg", "-hide_banner", "-h", "full"], capture_output=True, text=True).stdout
    except OSError:
        h = ""
    if "readrate_initial_burst" in h:
        return ["-readrate", "1", "-readrate_initial_burst", str(CFG.get("initial_burst", 10))]
    return ["-re"]


PACING = _ffmpeg_pacing()


def forget(video_id):
    with _url_lock:
        _url_cache.pop(video_id, None)


def play_video(v, offset, elapsed, out, ch):
    """Stream one video into out. Returns (bytes_sent, completed_normally)."""
    if is_bad(v["id"]):
        return 0, False
    urls = resolve(v["id"])
    if not urls:
        return 0, False
    log.info("[%s] playing %s (%s) from %.0fs", ch.name, v["title"], v["id"], offset)
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", *PACING]
    for u, headers in urls:
        if u.startswith("http"):
            u = relay_url(u, headers)
        cmd += ["-ss", f"{offset:.2f}", "-i", u]
    if len(urls) > 1:
        cmd += ["-map", "0:v:0", "-map", "1:a:0"]
    else:
        cmd += ["-map", "0:v:0?", "-map", "0:a:0?"]
    cmd += ["-c", "copy", "-output_ts_offset", f"{elapsed:.3f}",
            "-muxdelay", "0", "-f", "mpegts", "pipe:1"]
    started = time.time()
    sent = 0
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE)
    try:
        while chunk := p.stdout.read1(65536):
            if not sent:
                log.info("[%s] video started after %.1fs", ch.name, time.time() - started)
            out.write(chunk)
            sent += len(chunk)
    finally:
        p.kill()
        rc = p.wait()
    return sent, rc == 0


def stream_channel(ch, out):
    videos = ch.videos
    if not videos:
        raise RuntimeError("channel has no videos yet")
    i, offset, _ = Channel.locate(videos, time.time())
    elapsed = 0.0  # keeps timestamps increasing across videos so players don't stall
    failures = 0
    while True:
        v = videos[i]
        started = time.time()
        sent, ok = play_video(v, offset, elapsed, out, ch)
        if not sent and not is_bad(v["id"]):
            # Links can go stale or be refused; get a fresh one and try once more.
            forget(v["id"])
            log.info("[%s] retrying %s with a fresh link", ch.name, v["id"])
            started = time.time()
            sent, ok = play_video(v, offset, elapsed, out, ch)
            if not sent:
                mark_bad(v["id"], "refused twice while playing")
        if not sent:
            failures += 1
            if failures >= 25:
                log.error("[%s] too many failures in a row, giving up", ch.name)
                return
        else:
            failures = 0
            # Media time actually played, so timestamps stay continuous even with the burst.
            elapsed += (v["duration"] - offset) if ok else (time.time() - started)
        i, offset = (i + 1) % len(videos), 0.0


# Characters XML 1.0 forbids; YouTube titles/descriptions occasionally contain them.
_INVALID_XML = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\ufffe\uffff]")


def xml_text(s):
    return escape(_INVALID_XML.sub("", s), {'"': "&quot;"})


def xmltv_time(ts):
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y%m%d%H%M%S +0000")


LOCAL = LocalGuide(CFG["local_guide"], xml_text, xmltv_time) if CFG.get("local_guide") else None


def build_local_xmltv():
    return "\n".join(['<?xml version="1.0" encoding="UTF-8"?>', '<tv generator-info-name="yt-live-tv">',
                      *LOCAL.channels_xml, *LOCAL.programmes_xml, "</tv>"])


def build_xmltv():
    now = time.time()
    parts = ['<?xml version="1.0" encoding="UTF-8"?>', '<tv generator-info-name="yt-live-tv">']
    if LOCAL and LOCAL.merge:
        parts += LOCAL.channels_xml  # all <channel> elements must come before <programme>s
    for ch in CHANNELS.values():
        parts.append(f'<channel id="{ch.number}"><display-name>{xml_text(ch.name)}</display-name>'
                     f'<display-name>{ch.number}</display-name></channel>')
    for ch in CHANNELS.values():
        for start, stop, v in ch.schedule(now - 3600, now + GUIDE_HOURS * 3600):
            parts.append(
                f'<programme start="{xmltv_time(start)}" stop="{xmltv_time(stop)}" channel="{ch.number}">'
                f'<title lang="en">{xml_text(v["title"])}</title>'
                f'<desc lang="en">{xml_text(v["description"] or v["title"])}</desc>'
                f'<icon src="https://i.ytimg.com/vi/{v["id"]}/hqdefault.jpg"/></programme>')
    if LOCAL and LOCAL.merge:
        parts += LOCAL.programmes_xml
    parts.append("</tv>")
    return "\n".join(parts)


DEVICE_ID = CFG.get("device_id", "1A2B3C4D")


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def send(self, body, ctype="application/json"):
        data = body.encode() if isinstance(body, str) else body
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def do_GET(self):
        path = self.path.split("?")[0]
        if path in ("/discover.json", "/"):
            self.send(json.dumps({
                "FriendlyName": CFG.get("name", "YouTube Live TV"),
                "Manufacturer": "Silicondust", "ModelNumber": "HDTC-2US",
                "FirmwareName": "hdhomeruntc_atsc", "FirmwareVersion": "20150826",
                "DeviceID": DEVICE_ID, "DeviceAuth": "yt-live-tv",
                "BaseURL": BASE_URL, "LineupURL": f"{BASE_URL}/lineup.json",
                "TunerCount": TUNERS,
            }))
        elif path == "/lineup_status.json":
            self.send(json.dumps({"ScanInProgress": 0, "ScanPossible": 1,
                                  "Source": "Cable", "SourceList": ["Cable"]}))
        elif path == "/lineup.json":
            self.send(json.dumps([{"GuideNumber": ch.number, "GuideName": ch.name,
                                   "URL": f"{BASE_URL}/stream/{ch.number}"}
                                  for ch in CHANNELS.values()]))
        elif path == "/device.xml":
            self.send(f"""<?xml version="1.0"?><root xmlns="urn:schemas-upnp-org:device-1-0">
<URLBase>{BASE_URL}</URLBase><device><deviceType>urn:schemas-upnp-org:device:MediaServer:1</deviceType>
<friendlyName>{escape(CFG.get("name", "YouTube Live TV"))}</friendlyName><manufacturer>Silicondust</manufacturer>
<modelName>HDTC-2US</modelName><modelNumber>HDTC-2US</modelNumber><serialNumber>{DEVICE_ID}</serialNumber>
<UDN>uuid:{DEVICE_ID}</UDN></device></root>""", "application/xml")
        elif path in ("/xmltv.xml", "/guide.xml"):
            self.send(build_xmltv(), "text/xml; charset=utf-8")
        elif path.startswith("/relay/"):
            serve_relay(self, path[len("/relay/"):])
        elif path == "/status":
            self.send(json.dumps(status(), indent=2))
        elif path == "/local.xml" and LOCAL:
            self.send(build_local_xmltv(), "text/xml; charset=utf-8")
        elif path in ("/playlist.m3u", "/channels.m3u"):
            lines = ["#EXTM3U"] + [
                f'#EXTINF:-1 tvg-id="{ch.number}" tvg-chno="{ch.number}",{ch.name}\n{BASE_URL}/stream/{ch.number}'
                for ch in CHANNELS.values()]
            self.send("\n".join(lines) + "\n", "audio/x-mpegurl")
        elif m := re.fullmatch(r"/(?:stream|auto/v)/?(\w+)", path):
            ch = CHANNELS.get(m.group(1))
            log.info("tune request %s %s from %s", self.command, self.path, self.client_address[0])
            if not ch or not ch.videos:
                log.warning("cannot tune %s: %s", m.group(1), "unknown channel" if not ch else "no videos loaded yet")
                self.send_error(404 if not ch else 503)
                return
            self.send_response(200)
            self.send_header("Content-Type", "video/mp2t")
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
            if self.command == "HEAD":
                return
            try:
                stream_channel(ch, self.wfile)
            except (BrokenPipeError, ConnectionResetError):
                log.info("[%s] viewer disconnected", ch.name)
        else:
            log.info("404 for %s %s from %s", self.command, self.path, self.client_address[0])
            self.send_error(404)

    do_HEAD = do_GET

    def log_message(self, fmt, *args):
        log.debug(fmt, *args)


if __name__ == "__main__":
    threading.Thread(target=refresh_loop, daemon=True).start()
    threading.Thread(target=prefetch_loop, daemon=True).start()
    if LOCAL:
        LOCAL.start()
    log.info("listening on :%d (base url %s)", PORT, BASE_URL)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
