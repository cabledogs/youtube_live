"""Diagnose YouTube 403s: compare our requests with what yt-dlp's own downloader sends.

Run inside the container:
    docker exec -i yt-live-tv python - VIDEO_ID < diag.py
"""

import sys
from urllib.parse import parse_qsl, urlparse

import yt_dlp
from yt_dlp.networking import Request

vid = sys.argv[1] if len(sys.argv) > 1 else "QjvpjXdgugA"
url = f"https://www.youtube.com/watch?v={vid}"
FORMAT = "bv*[height<=1080][vcodec^=avc1]+ba[ext=m4a]/b[height<=1080]"
opts = yt_dlp.parse_options(["-f", FORMAT, "--quiet", "--no-warnings"]).ydl_opts
opts["ignoreerrors"] = False


def params(u):
    return dict(parse_qsl(urlparse(u).query))


print(f"yt-dlp {yt_dlp.version.__version__}, video {vid}\n")
print("=== 1. How our relay fetches ===")
ydl = yt_dlp.YoutubeDL(opts)
info = ydl.extract_info(url, download=False)
fmts = info.get("requested_formats") or [info]
orig_params = {}
for f in fmts:
    orig_params[f["format_id"]] = params(f["url"])
    print(f"format {f['format_id']} protocol={f.get('protocol')} "
          f"downloader_options={f.get('downloader_options')} client={params(f['url']).get('c')}")
    print(f"  http_headers: {f.get('http_headers')}")
    print(f"  other keys: {sorted(k for k in f if k not in ('url', 'http_headers', 'fragments'))}")
    for label, extra in [("Range bytes=0-1023", {"Range": "bytes=0-1023"}), ("no Range", {})]:
        try:
            r = ydl.urlopen(Request(f["url"], headers={**(f.get("http_headers") or {}), **extra}))
            print(f"  {label:20} -> HTTP {r.status}, got {len(r.read(2048))} bytes")
            r.close()
        except Exception as e:
            print(f"  {label:20} -> {e}")

print("\n=== 2. How yt-dlp's own downloader fetches (test mode, first ~10 KB) ===")
orig_urlopen = yt_dlp.YoutubeDL.urlopen


def spy(self, req):
    u = req.url if isinstance(req, Request) else str(req)
    if "googlevideo" in u:
        p = params(u)
        base = next((v for v in orig_params.values() if v.get("itag") == p.get("itag")), {})
        added = {k: v for k, v in p.items() if k not in base}
        changed = sorted(k for k in p if k in base and base[k] != p[k])
        method = getattr(req, "method", "GET")
        headers = dict(req.headers) if isinstance(req, Request) else {}
        print(f"REQUEST {method} itag={p.get('itag')} added_params={added} changed_params={changed}")
        print(f"  headers: {headers}")
        try:
            r = orig_urlopen(self, req)
            print(f"  -> HTTP {r.status}")
            return r
        except Exception as e:
            print(f"  -> {e}")
            raise
    return orig_urlopen(self, req)


yt_dlp.YoutubeDL.urlopen = spy
dl_opts = {**opts, "test": True, "outtmpl": "/tmp/diag_%(format_id)s.%(ext)s", "quiet": True,
           "overwrites": True, "keepvideo": True}
try:
    yt_dlp.YoutubeDL(dl_opts).download([url])
    print("yt-dlp download: OK")
except Exception as e:
    print(f"yt-dlp download failed: {e}")
