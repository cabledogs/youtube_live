#!/bin/sh
# YouTube changes often; update yt-dlp on every start so streams keep working.
pip install --no-cache-dir -q -U "yt-dlp[default]" || echo "yt-dlp update failed, using installed version"
exec python -u app.py
