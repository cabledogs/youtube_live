"""Free local TV listings (broadcast or cable) by zip code, as XMLTV fragments.

Pulls from the public Gracenote listings grid (the API behind the former
Zap2it site, which shut down in 2025) and caches the result. Optional: enabled by `local_guide:` in config.yaml.
"""

import json
import logging
import threading
import time
from datetime import datetime
from urllib.parse import urlencode
from urllib.request import Request, urlopen

log = logging.getLogger("yt-live-tv")

GRID_URL = "https://tvlistings.gracenote.com/api/grid"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")
WINDOW_HOURS = 3  # the grid API returns at most a few hours per request


def _iso_to_ts(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()


def _img(url):
    if not url:
        return None
    if url.startswith("//"):
        return "https:" + url
    if url.startswith("http"):
        return url
    return f"https://zap2it.tmsimg.com/assets/{url}.jpg"  # Gracenote's image CDN, still on this name


class LocalGuide:
    def __init__(self, cfg, xml_text, xmltv_time):
        self.postal_code = str(cfg["postal_code"])
        self.country = cfg.get("country", "USA")
        self.lineup_id = cfg.get("lineup_id", f"{self.country}-OTA{self.postal_code}-DEFAULT")
        self.device = cfg.get("device", "-")
        self.hours = int(cfg.get("days", 3)) * 24
        self.refresh_hours = float(cfg.get("refresh_hours", 12))
        self.merge = cfg.get("merge", True)
        self.prefix = cfg.get("id_prefix", "local.")
        self.grid_url = cfg.get("grid_url", GRID_URL)
        self.aid = cfg.get("aid", "orbebb")
        self._x = xml_text
        self._t = xmltv_time
        self.channels_xml = []
        self.programmes_xml = []

    def _fetch_window(self, ts):
        params = {
            "lineupId": self.lineup_id, "timespan": WINDOW_HOURS, "headendId": "lineupId",
            "country": self.country, "timezone": "", "device": self.device,
            "postalCode": self.postal_code, "isOverride": "true", "time": ts,
            "pref": "16,128", "userId": "-", "aid": self.aid, "languagecode": "en-us",
        }
        req = Request(f"{self.grid_url}?{urlencode(params)}",
                      headers={"User-Agent": UA, "Accept": "application/json",
                               "Referer": f"https://tvlistings.gracenote.com/grid-affiliates.html?aid={self.aid}"})
        with urlopen(req, timeout=30) as r:
            return json.load(r)

    def refresh(self):
        start = int(time.time() // 3600 * 3600)
        channels, events = {}, {}
        for ts in range(start, start + self.hours * 3600, WINDOW_HOURS * 3600):
            try:
                data = self._fetch_window(ts)
            except Exception as e:
                log.warning("[local guide] fetch failed for %s: %s", datetime.fromtimestamp(ts), e)
                continue
            for c in data.get("channels") or []:
                cid = f'{self.prefix}{c.get("channelId") or c.get("callSign")}'
                channels.setdefault(cid, c)
                for e in c.get("events") or []:
                    if e.get("startTime") and e.get("endTime"):
                        events[(cid, e["startTime"])] = e  # windows overlap; dedupe
            time.sleep(0.5)
        if not channels:
            log.error("[local guide] no listings returned; check postal_code / lineup_id")
            return
        self.channels_xml = [self._channel(cid, c) for cid, c in channels.items()]
        self.programmes_xml = [self._programme(cid, e)
                               for (cid, _), e in sorted(events.items(), key=lambda kv: kv[0])]
        log.info("[local guide] %d channels, %d programmes", len(channels), len(events))

    def _channel(self, cid, c):
        x = self._x
        num, call = str(c.get("channelNo") or ""), c.get("callSign") or ""
        names = [f"{num} {call}".strip(), call, num, c.get("affiliateName") or ""]
        out = [f'<channel id="{x(cid)}">']
        out += [f"<display-name>{x(n)}</display-name>" for n in dict.fromkeys(names) if n]
        if icon := _img(c.get("thumbnail")):
            out.append(f'<icon src="{x(icon)}"/>')
        out.append("</channel>")
        return "".join(out)

    def _programme(self, cid, e):
        x = self._x
        p = e.get("program") or {}
        start, stop = _iso_to_ts(e["startTime"]), _iso_to_ts(e["endTime"])
        out = [f'<programme start="{self._t(start)}" stop="{self._t(stop)}" channel="{x(cid)}">',
               f'<title lang="en">{x(p.get("title") or "To Be Announced")}</title>']
        if p.get("episodeTitle"):
            out.append(f'<sub-title lang="en">{x(p["episodeTitle"])}</sub-title>')
        if p.get("shortDesc"):
            out.append(f'<desc lang="en">{x(p["shortDesc"])}</desc>')
        if p.get("releaseYear"):
            out.append(f'<date>{x(str(p["releaseYear"]))}</date>')
        for f in e.get("filter") or []:
            out.append(f'<category lang="en">{x(f.removeprefix("filter-").title())}</category>')
        if icon := _img(e.get("thumbnail")):
            out.append(f'<icon src="{x(icon)}"/>')
        season, episode = str(p.get("season") or ""), str(p.get("episode") or "")
        if season.isdigit() and episode.isdigit():
            out.append(f'<episode-num system="xmltv_ns">{int(season) - 1}.{int(episode) - 1}.</episode-num>')
            out.append(f'<episode-num system="onscreen">S{int(season):02d}E{int(episode):02d}</episode-num>')
        if p.get("id"):
            out.append(f'<episode-num system="dd_progid">{x(p["id"])}</episode-num>')
        if "New" in (e.get("flag") or []):
            out.append("<new/>")
        else:
            out.append("<previously-shown/>")
        if e.get("rating"):
            out.append(f'<rating system="VCHIP"><value>{x(e["rating"])}</value></rating>')
        out.append("</programme>")
        return "".join(out)

    def loop(self):
        while True:
            try:
                self.refresh()
            except Exception:
                log.exception("[local guide] refresh crashed")
            time.sleep(self.refresh_hours * 3600)

    def start(self):
        threading.Thread(target=self.loop, daemon=True).start()
