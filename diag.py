"""Diagnose YouTube 403s: which way of getting/fetching stream links works here.

Run inside the container:
    docker exec -i yt-live-tv python - VIDEO_ID < diag.py
"""

import os
import sys

import yt_dlp
from yt_dlp.networking import Request

vid = sys.argv[1] if len(sys.argv) > 1 else "QjvpjXdgugA"
url = f"https://www.youtube.com/watch?v={vid}"
FORMAT = "bv*[height<=1080][vcodec^=avc1]+ba[ext=m4a]/b[height<=1080]"


def opts(client=None, **extra):
    argv = ["-f", FORMAT, "--quiet", "--no-warnings"]
    if client:
        argv += ["--extractor-args", f"youtube:player_client={client}"]
    o = yt_dlp.parse_options(argv).ydl_opts
    o["ignoreerrors"] = False
    o.update(extra)
    return o


def fetch(ydl, f, accept_identity):
    h = {**(f.get("http_headers") or {}), "Range": "bytes=0-1023"}
    if accept_identity:
        h["Accept-Encoding"] = "identity"
    try:
        r = ydl.urlopen(Request(f["url"], headers=h))
        r.read(1024)
        r.close()
        return f"OK {r.status}"
    except Exception as e:
        return f"FAIL ({getattr(e, 'status', e)})"


print(f"yt-dlp {yt_dlp.version.__version__}, video {vid}\n")

print("=== A. Look up links only, then fetch 1 KB ourselves ===")
for client in [None, "tv", "tv_simply", "web_embedded", "mweb", "android_vr", "web_safari"]:
    name = client or "default"
    try:
        ydl = yt_dlp.YoutubeDL(opts(client))
        info = ydl.extract_info(url, download=False)
        fmts = info.get("requested_formats") or [info]
        res = []
        for f in fmts:
            res.append(f"{f['format_id']}: plain={fetch(ydl, f, False)} identity={fetch(ydl, f, True)}")
        print(f"{name:13} " + " | ".join(res))
    except Exception as e:
        print(f"{name:13} lookup failed: {str(e)[:120]}")

print("\n=== B. Look up links only, then let yt-dlp's downloader use THOSE links ===")
try:
    ydl = yt_dlp.YoutubeDL(opts())
    info = ydl.extract_info(url, download=False)
    ydl.params.update({"test": True, "outtmpl": {"default": "/tmp/diagB_%(format_id)s.%(ext)s"},
                       "overwrites": True, "keepvideo": True})
    ydl.process_ie_result(info, download=True)
    print("B: yt-dlp downloader with looked-up links: OK")
except Exception as e:
    print(f"B: yt-dlp downloader with looked-up links: FAIL ({str(e)[:150]})")

print("\n=== C. Full yt-dlp download (looks up links itself) ===")
try:
    yt_dlp.YoutubeDL(opts(test=True, outtmpl={"default": "/tmp/diagC_%(format_id)s.%(ext)s"},
                          overwrites=True, keepvideo=True)).download([url])
    print("C: full yt-dlp download: OK")
except Exception as e:
    print(f"C: full yt-dlp download: FAIL ({str(e)[:150]})")

print("\n=== D. Take links from yt-dlp's download path (the new fix), then fetch 1 KB ourselves ===")
try:
    ydl = yt_dlp.YoutubeDL(opts())
    info = ydl.extract_info(url, download=False)
    wanted = len(info.get("requested_formats") or [info])
    got = []

    class Got(Exception):
        pass

    def capture(name, new_info, *a, **k):
        got.append(new_info)
        if len(got) >= wanted:
            raise Got()
        return True, True

    ydl.dl = capture
    ydl.params.update(overwrites=True, outtmpl={"default": "/tmp/diagD_%(format_id)s.%(ext)s"})
    try:
        ydl.process_ie_result(info, download=True)
    except Exception:
        pass
    print(f"D: captured {len(got)} of {wanted} links: " +
          " | ".join(f"{f.get('format_id')}: {fetch(ydl, f, True)}" for f in got))
except Exception as e:
    print(f"D: FAIL ({str(e)[:150]})")

for f in os.listdir("/tmp"):
    if f.startswith("diag"):
        os.remove(os.path.join("/tmp", f))
