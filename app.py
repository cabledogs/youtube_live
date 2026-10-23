"""yt-live-tv: stream YouTube channels as live TV channels, no downloads.

Pretends to be an HDHomeRun tuner so Plex (or Jellyfin/Emby) can add it under
Live TV & DVR. Each configured channel plays its YouTube videos back-to-back on
a fixed, repeating schedule, pulled from YouTube on demand with yt-dlp and
remuxed to MPEG-TS with ffmpeg.
"""

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
from xml.sax.saxutils import escape

import yaml

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("yt-live-tv")

CONFIG_PATH = os.environ.get("CONFIG", "/config/config.yaml")
with open(CONFIG_PATH) as f:
    CFG = yaml.safe_load(f)

PORT = int(CFG.get("port", 5004))
BASE_URL = CFG.get("base_url", f"http://localhost:{PORT}").rstrip("/")
FORMAT = CFG.get("format", "bv*[height<=720][vcodec^=avc1]+ba[ext=m4a]/b[height<=720]")
REFRESH_HOURS = float(CFG.get("refresh_hours", 6))
TUNERS = int(CFG.get("tuners", 4))
COOKIES = CFG.get("cookies")
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


def resolve(video_id):
    """Get direct media URL(s) for a video. These expire after a few hours, so resolve right before playing."""
    r = ytdlp("-f", FORMAT, "-g", f"https://www.youtube.com/watch?v={video_id}")
    if r.returncode != 0:
        log.warning("resolve %s failed: %s", video_id, r.stderr.strip()[-300:])
        return []
    return [u for u in r.stdout.splitlines() if u.strip()]


def stream_channel(ch, out):
    videos = ch.videos
    if not videos:
        raise RuntimeError("channel has no videos yet")
    i, offset, _ = Channel.locate(videos, time.time())
    elapsed = 0.0  # keeps timestamps increasing across videos so players don't stall
    failures = 0
    while True:
        v = videos[i]
        urls = resolve(v["id"])
        if not urls:
            failures += 1
            if failures >= 5:
                log.error("[%s] too many failures in a row, giving up", ch.name)
                return
            i, offset = (i + 1) % len(videos), 0.0
            continue
        log.info("[%s] playing %s (%s) from %.0fs", ch.name, v["title"], v["id"], offset)
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-re"]
        for u in urls:
            cmd += ["-ss", f"{offset:.2f}"]
            if u.startswith("http"):
                cmd += ["-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "5"]
            cmd += ["-i", u]
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
            while chunk := p.stdout.read(188 * 1000):
                out.write(chunk)
                sent += len(chunk)
        finally:
            p.kill()
            p.wait()
        elapsed += time.time() - started
        failures = 0 if sent else failures + 1
        if failures >= 5:
            log.error("[%s] too many failures in a row, giving up", ch.name)
            return
        i, offset = (i + 1) % len(videos), 0.0


# Characters XML 1.0 forbids; YouTube titles/descriptions occasionally contain them.
_INVALID_XML = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\ufffe\uffff]")


def xml_text(s):
    return escape(_INVALID_XML.sub("", s), {'"': "&quot;"})


def xmltv_time(ts):
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y%m%d%H%M%S +0000")


def build_xmltv():
    now = time.time()
    parts = ['<?xml version="1.0" encoding="UTF-8"?>', '<tv generator-info-name="yt-live-tv">']
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
        elif path in ("/playlist.m3u", "/channels.m3u"):
            lines = ["#EXTM3U"] + [
                f'#EXTINF:-1 tvg-id="{ch.number}" tvg-chno="{ch.number}",{ch.name}\n{BASE_URL}/stream/{ch.number}'
                for ch in CHANNELS.values()]
            self.send("\n".join(lines) + "\n", "audio/x-mpegurl")
        elif m := re.fullmatch(r"/(?:stream|auto/v)/?(\w+)", path):
            ch = CHANNELS.get(m.group(1))
            if not ch or not ch.videos:
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
            self.send_error(404)

    do_HEAD = do_GET

    def log_message(self, fmt, *args):
        log.debug(fmt, *args)


if __name__ == "__main__":
    threading.Thread(target=refresh_loop, daemon=True).start()
    log.info("listening on :%d (base url %s)", PORT, BASE_URL)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
