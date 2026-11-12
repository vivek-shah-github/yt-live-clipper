#!/bin/bash
cd "$(dirname "$0")"
PY=$(command -v python3 || command -v python)
if [ -z "$PY" ]; then echo "Install Python 3 from https://www.python.org/downloads/ and run again."; read; exit 1; fi
echo "Installing / updating yt-dlp, ffmpeg and deno (first run takes a minute)..."
"$PY" -m pip install --upgrade --quiet --disable-pip-version-check "yt-dlp[default]" imageio-ffmpeg 2>/dev/null || \
"$PY" -m pip install --upgrade --quiet --user --break-system-packages "yt-dlp[default]" imageio-ffmpeg
"$PY" -m pip install --upgrade --quiet --disable-pip-version-check deno >/dev/null 2>&1 || \
"$PY" -m pip install --upgrade --quiet --user --break-system-packages deno >/dev/null 2>&1
echo
echo "Starting YT Live Clipper... the app window opens in a moment."
echo "Keep this Terminal window open while you use the app."
"$PY" clipper.py
