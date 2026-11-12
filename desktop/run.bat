@echo off
title YT Live Clipper
cd /d "%~dp0"
set PY=
where py >nul 2>nul && set PY=py
if not defined PY (where python >nul 2>nul && set PY=python)
if not defined PY (
  echo Python is not installed.
  echo Install it from https://www.python.org/downloads/  ^(tick "Add python.exe to PATH"^), then run this again.
  pause
  exit /b
)
echo Installing / updating yt-dlp, ffmpeg and deno ^(first run takes a minute^)...
%PY% -m pip install --upgrade --quiet --disable-pip-version-check "yt-dlp[default]" imageio-ffmpeg
%PY% -m pip install --upgrade --quiet --disable-pip-version-check deno >nul 2>nul
echo.
echo Starting YT Live Clipper... the app window opens in a moment.
echo Keep this black window open while you use the app.
%PY% clipper.py
if errorlevel 1 pause
