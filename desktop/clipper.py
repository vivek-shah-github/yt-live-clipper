#!/usr/bin/env python3
"""
YT Live Clipper (Windows / Mac)
-------------------------------
1) Clip any part of a YouTube live stream that has ended (or any normal video)
   by start/end time - many clips in one go.
2) Record a stream that is LIVE right now: Start -> Stop & Save.
3) Optional vertical 9:16 output (Shorts / Reels).

The interface (ui/index.html) opens in its own app window (Edge / Chrome app mode,
or your default browser). This script runs a small local server that does the work.

Needs: Python 3.9+, yt-dlp, imageio-ffmpeg  (run.bat / run.command installs them)
"""
import json
import os
import platform
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.request import urlopen

APP_VERSION = "2.0"

# --------------------------------------------------------------------------- deps
try:
    import yt_dlp
except ImportError:
    yt_dlp = None


def find_ffmpeg():
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None


def find_deno():
    """YouTube needs a JavaScript runtime for yt-dlp. The 'deno' pip package ships one."""
    exe = shutil.which("deno")
    if exe:
        return exe
    try:
        import deno
        return deno.find_deno_bin()
    except Exception:
        return None


FFMPEG = find_ffmpeg()
DENO = find_deno()
SYSTEM = platform.system()
IS_WIN = SYSTEM == "Windows"
IS_MAC = SYSTEM == "Darwin"
NO_WINDOW = 0x08000000 if IS_WIN else 0  # CREATE_NO_WINDOW
CONFIG_PATH = os.path.join(os.path.expanduser("~"), ".yt_live_clipper.json")
HERE = os.path.dirname(os.path.abspath(__file__))
UI_FILE = os.path.join(HERE, "ui", "index.html")
DEFAULT_OUT = os.path.join(os.path.expanduser("~"), "Movies" if IS_MAC else "Videos", "YT Clips")
QUALITIES = ["Best", "2160p", "1440p", "1080p", "720p", "480p", "360p"]


# --------------------------------------------------------------------------- helpers
def fmt_time(sec):
    sec = max(0, int(round(sec)))
    return f"{sec // 3600:02d}:{sec % 3600 // 60:02d}:{sec % 60:02d}"


def safe_name(name, maxlen=80):
    name = re.sub(r'[\\/:*?"<>|\r\n\t]+', " ", name).strip().strip(".")
    name = re.sub(r"\s+", " ", name)
    return (name or "clip")[:maxlen].strip()


def unique_path(path):
    base, ext = os.path.splitext(path)
    i, out = 2, path
    while os.path.exists(out):
        out = f"{base} ({i}){ext}"
        i += 1
    return out


def header_args(headers):
    if not headers:
        return []
    return ["-headers", "".join(f"{k}: {v}\r\n" for k, v in headers.items())]


def load_config():
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_config(cfg):
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
    except Exception:
        pass


def open_path(path):
    if IS_WIN:
        os.startfile(path)  # noqa
    elif IS_MAC:
        subprocess.Popen(["open", path])
    else:
        subprocess.Popen(["xdg-open", path])


# --------------------------------------------------------------------------- yt-dlp
def fetch_info(url, cookies_browser=None):
    if yt_dlp is None:
        raise RuntimeError("yt-dlp is not installed. Start the app with run.bat / run.command.")
    opts = {"quiet": True, "no_warnings": True, "skip_download": True, "noplaylist": True}
    if cookies_browser:
        opts["cookiesfrombrowser"] = (cookies_browser,)
    if DENO:
        opts["js_runtimes"] = {"deno": {"path": DENO}}
    with yt_dlp.YoutubeDL(opts) as ydl:
        return ydl.extract_info(url, download=False)


def _codec_pref(f):
    vc = (f.get("vcodec") or "").lower()
    if vc.startswith("avc1") or vc.startswith("h264"):
        return 3
    if vc.startswith("vp09") or vc.startswith("vp9"):
        return 2
    if vc.startswith("av01"):
        return 1
    return 0


def _height_key(f, max_h):
    h = f.get("height") or 0
    return (1, h) if (max_h is None or h <= max_h) else (0, -h)


def pick_vod_formats(info, max_h):
    """Return (video_fmt, audio_fmt). audio_fmt is None when video_fmt is muxed."""
    fmts = [f for f in info.get("formats") or [] if f.get("url")]
    direct = [f for f in fmts if (f.get("protocol") or "") in ("https", "http")]
    pool = direct or fmts

    vids = [f for f in pool if f.get("vcodec") not in (None, "none") and f.get("acodec") == "none"]
    auds = [f for f in pool if f.get("acodec") not in (None, "none") and f.get("vcodec") == "none"]
    muxed = [f for f in pool if f.get("acodec") not in (None, "none") and f.get("vcodec") not in (None, "none")]

    if vids and auds:
        v = max(vids, key=lambda f: (*_height_key(f, max_h), _codec_pref(f), f.get("tbr") or 0))
        a = max(auds, key=lambda f: ((f.get("ext") == "m4a"), f.get("abr") or f.get("tbr") or 0))
        return v, a
    if muxed:
        return max(muxed, key=lambda f: (*_height_key(f, max_h), f.get("tbr") or 0)), None
    # non-YouTube links often don't say which codecs a format has: use the best one as is
    unknown = [f for f in pool if f.get("vcodec") != "none"]
    if unknown:
        return max(unknown, key=lambda f: (*_height_key(f, max_h), f.get("tbr") or 0)), None
    raise RuntimeError("No downloadable formats found for this video.")


def pick_live_format(info, max_h):
    fmts = [f for f in info.get("formats") or [] if f.get("url")]
    hls = [f for f in fmts if "m3u8" in (f.get("protocol") or "") and f.get("vcodec") != "none"]
    if not hls:
        raise RuntimeError("No live stream found (is the stream really live?).")
    return max(hls, key=lambda f: (*_height_key(f, max_h), f.get("tbr") or 0))


def ui_info(info):
    """The small part of yt-dlp's info that the interface needs."""
    return {k: info.get(k) for k in ("id", "title", "duration", "is_live", "live_status", "uploader",
                                     "thumbnail", "webpage_url", "extractor_key")} | \
        {"extractor": (info.get("extractor_key") or "").lower()}


# --------------------------------------------------------------------------- ffmpeg
VERTICAL_VF = "crop=trunc(ih*9/16/2)*2:ih,scale=1080:1920:flags=lanczos,setsar=1"
ENCODE_ARGS = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
               "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart"]
HTTP_IN = ["-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "5"]


class Canceled(Exception):
    pass


def run_ffmpeg(args, duration, on_progress=None, on_proc=None):
    if not FFMPEG:
        raise RuntimeError("ffmpeg not found. Start the app with run.bat / run.command.")
    cmd = [FFMPEG, "-hide_banner", "-y", "-loglevel", "error", "-nostats", "-progress", "pipe:1"] + args
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL,
                         text=True, encoding="utf-8", errors="replace", creationflags=NO_WINDOW)
    if on_proc:
        on_proc(p)
    err_lines = []
    t = threading.Thread(target=lambda: err_lines.extend(p.stderr), daemon=True)
    t.start()
    for line in p.stdout:
        if line.startswith("out_time_us=") or line.startswith("out_time_ms="):
            try:
                done = int(line.split("=", 1)[1]) / 1_000_000
                if on_progress and duration:
                    on_progress(max(0.0, min(100.0, done / duration * 100)))
            except ValueError:
                pass
    p.wait()
    t.join(timeout=2)
    if on_proc:
        on_proc(None)
    if p.returncode != 0:
        raise RuntimeError("ffmpeg failed:\n" + "".join(err_lines[-15:]).strip())


def build_clip_args(video_fmt, audio_fmt, info, start, end, vertical, out_path):
    dur = end - start
    args = []
    for f in (video_fmt, audio_fmt):
        if f is None:
            continue
        hdr = f.get("http_headers") or info.get("http_headers")
        args += HTTP_IN + header_args(hdr) + ["-ss", f"{start:.3f}", "-t", f"{dur:.3f}", "-i", f["url"]]
    if audio_fmt is not None:
        args += ["-map", "0:v:0", "-map", "1:a:0"]
    else:
        args += ["-map", "0:v:0", "-map", "0:a:0?"]
    if vertical:
        args += ["-vf", VERTICAL_VF]
    args += ENCODE_ARGS + [out_path]
    return args


def finalize_recording(ts_path, out_path, vertical, on_progress=None, duration=None):
    if vertical:
        args = ["-i", ts_path, "-vf", VERTICAL_VF] + ENCODE_ARGS + [out_path]
    else:
        args = ["-i", ts_path, "-map", "0:v:0", "-map", "0:a:0?", "-c", "copy",
                "-bsf:a", "aac_adtstoasc", "-movflags", "+faststart", out_path]
    run_ffmpeg(args, duration, on_progress)


class LiveRecorder:
    def __init__(self, fmt, info, ts_path):
        hdr = fmt.get("http_headers") or info.get("http_headers")
        cmd = [FFMPEG, "-hide_banner", "-y", "-loglevel", "error"] + header_args(hdr) + \
              ["-i", fmt["url"], "-map", "0:v:0", "-map", "0:a:0?", "-c", "copy", "-f", "mpegts", ts_path]
        self.ts_path = ts_path
        self.started = time.time()
        self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL, creationflags=NO_WINDOW)

    def elapsed(self):
        return time.time() - self.started

    def size(self):
        try:
            return os.path.getsize(self.ts_path)
        except OSError:
            return 0

    def alive(self):
        return self.proc.poll() is None

    def stop(self):
        if self.alive():
            try:
                self.proc.stdin.write(b"q")
                self.proc.stdin.flush()
            except Exception:
                pass
            try:
                self.proc.wait(timeout=20)
            except subprocess.TimeoutExpired:
                self.proc.terminate()
                self.proc.wait(timeout=10)
        return self.elapsed()


# --------------------------------------------------------------------------- engine
class Engine:
    """All app state + actions. The interface calls these through the local server."""

    def __init__(self):
        cfg = load_config()
        self.settings = {
            "out_dir": cfg.get("out_dir", DEFAULT_OUT),
            "quality": cfg.get("quality", "1080p") if cfg.get("quality") in QUALITIES else "1080p",
            "cookies": cfg.get("cookies", "none"),
            "vertical": bool(cfg.get("vertical", False)),
        }
        self.lock = threading.RLock()
        self.logs, self.log_id = [], 0
        self.info, self.info_seq, self.load_fail = None, 0, 0
        self.busy, self.task, self.progress = False, None, None
        self.export = None
        self.rec, self.rec_title, self.rec_state = None, None, None
        self.outputs = []
        self.ff_proc = None
        self.cancel_flag = False
        self.last_poll = time.time()
        self.quit_event = threading.Event()
        threading.Thread(target=self._watch_recorder, daemon=True).start()

    # ---- helpers
    def log(self, msg, level="info", toast=False):
        with self.lock:
            self.log_id += 1
            self.logs.append({"id": self.log_id, "t": time.strftime("%H:%M:%S"), "msg": msg,
                              "level": level, "toast": toast})
            self.logs = self.logs[-500:]

    def cookies(self):
        c = self.settings.get("cookies")
        return None if c in (None, "", "none") else c

    def max_height(self, q=None):
        q = q or self.settings["quality"]
        return None if q == "Best" else int(q.rstrip("p"))

    def run_bg(self, task, fn):
        with self.lock:
            if self.busy:
                return {"ok": False, "error": "Please wait, the app is still busy with: " + (self.task or "a task")}
            self.busy, self.task, self.progress, self.cancel_flag = True, task, None, False

        def wrapper():
            try:
                fn()
            except Canceled:
                self.log("Canceled.", "info", toast=True)
            except Exception as e:
                msg = str(e).strip() or e.__class__.__name__
                self.log("ERROR: " + msg, "error", toast=True)
            finally:
                with self.lock:
                    self.busy, self.task, self.progress = False, None, None
        threading.Thread(target=wrapper, daemon=True).start()
        return {"ok": True}

    def add_output(self, path):
        with self.lock:
            self.outputs.append({"name": os.path.basename(path), "path": path,
                                 "size": os.path.getsize(path) if os.path.exists(path) else 0})

    def out_dir(self):
        d = (self.settings.get("out_dir") or DEFAULT_OUT).strip()
        os.makedirs(d, exist_ok=True)
        return d

    def persist(self):
        save_config(self.settings)

    # ---- API
    def api_init(self, _):
        return {"ok": True, "platform": "windows" if IS_WIN else "mac" if IS_MAC else "linux",
                "version": APP_VERSION, "settings": self.settings,
                "ytdlp": getattr(getattr(yt_dlp, "version", None), "__version__", None),
                "ffmpeg": bool(FFMPEG), "canBrowse": True, "canCookies": True, "canQuit": True,
                "canOpenFolder": True,
                "setupHint": "yt-dlp or ffmpeg is missing. Close the app and start it with run.bat (Windows) or run.command (Mac)."}

    def api_state(self, a):
        self.last_poll = time.time()
        since = int(a.get("since") or 0)
        with self.lock:
            logs = [l for l in self.logs if l["id"] > since]
            r = {"ok": True, "busy": self.busy, "task": self.task, "progress": self.progress,
                 "logs": logs, "logNext": self.log_id, "lastLog": self.logs[-1]["msg"] if self.logs else None,
                 "infoSeq": self.info_seq, "loadFail": self.load_fail,
                 "export": self.export, "outputsCount": len(self.outputs), "outputs": self.outputs}
            if a.get("infoSeq") != self.info_seq:
                r["info"] = ui_info(self.info) if self.info else None
            if self.rec is not None:
                r["rec"] = {"state": self.rec_state, "elapsed": self.rec.elapsed(), "size": self.rec.size(),
                            "title": self.rec_title}
        return r

    def api_load(self, a):
        url = (a.get("url") or "").strip()
        if not url:
            return {"ok": False, "error": "Paste a link first."}
        if self.rec is not None:
            return {"ok": False, "error": "Stop the recording first."}

        def job():
            self.log(f"Loading: {url}")
            try:
                info = fetch_info(url, self.cookies())
            except Exception:
                with self.lock:
                    self.load_fail += 1
                raise
            if info.get("_type") == "playlist":
                with self.lock:
                    self.load_fail += 1
                raise RuntimeError("That is a playlist or channel link. Open a single video or stream and copy its link.")
            with self.lock:
                self.info = info
                self.info_seq += 1
                self.export = None
            status = info.get("live_status") or ("is_live" if info.get("is_live") else "not_live")
            self.log(f"Loaded: {info.get('title')} [{status}]", "ok")
        return self.run_bg("Loading link…", job)

    def api_export(self, a):
        info = self.info
        clips = a.get("clips") or []
        if not info:
            return {"ok": False, "error": "Load a video first."}
        if info.get("is_live"):
            return {"ok": False, "error": "This stream is still live. Record it, or wait until it ends to clip it."}
        if not clips:
            return {"ok": False, "error": "Add at least one clip."}
        q = a.get("quality") or self.settings["quality"]
        vertical = bool(a.get("vertical", self.settings["vertical"]))
        mh = self.max_height(q)
        with self.lock:
            self.export = {"items": {c["uid"]: {"status": "queued", "progress": 0} for c in clips}}

        def set_item(uid, **kw):
            with self.lock:
                self.export["items"][uid].update(kw)

        def job():
            out_dir = self.out_dir()
            self.log("Refreshing stream links…")
            fresh = fetch_info(info.get("webpage_url") or info.get("original_url"), self.cookies())
            v, au = pick_vod_formats(fresh, mh)
            self.log(f"Using {v.get('height') or '?'}p ({v.get('vcodec')})" + (" + audio" if au else ""))
            base = safe_name(fresh.get("title") or "clip", 60)
            dur_total = fresh.get("duration")
            n_ok = 0
            for n, c in enumerate(clips, 1):
                if self.cancel_flag:
                    for c2 in clips[n - 1:]:
                        set_item(c2["uid"], status="canceled")
                    raise Canceled()
                s, e = float(c["start"]), float(c["end"])
                if dur_total:
                    e = min(e, float(dur_total))
                if e <= s:
                    set_item(c["uid"], status="error")
                    self.log(f"[{n}/{len(clips)}] skipped: end is not after start", "error")
                    continue
                name = (c.get("name") or "").strip()
                label = safe_name(name) if name else f"{base} {fmt_time(s).replace(':', '-')}"
                out = unique_path(os.path.join(out_dir, label + (" (vertical)" if vertical else "") + ".mp4"))
                self.task = f"Exporting clip {n} of {len(clips)}"
                self.log(f"[{n}/{len(clips)}] {fmt_time(s)} - {fmt_time(e)} -> {os.path.basename(out)}")
                set_item(c["uid"], status="running", progress=0)

                def prog(p, uid=c["uid"]):
                    self.progress = p
                    set_item(uid, progress=p)

                def on_proc(p):
                    self.ff_proc = p
                try:
                    run_ffmpeg(build_clip_args(v, au, fresh, s, e, vertical, out), e - s, prog, on_proc)
                except Exception:
                    try:
                        os.remove(out)  # don't leave a broken half-written file behind
                    except OSError:
                        pass
                    if self.cancel_flag:
                        for c2 in clips[n - 1:]:
                            set_item(c2["uid"], status="canceled")
                        raise Canceled()
                    set_item(c["uid"], status="error")
                    raise
                set_item(c["uid"], status="done", progress=100)
                self.add_output(out)
                n_ok += 1
                self.log(f"    Saved: {out}", "ok")
            self.log(f"Done. {n_ok} clip{'s' if n_ok != 1 else ''} saved to {out_dir}", "ok", toast=True)
        return self.run_bg("Exporting clips…", job)

    def api_cancel(self, _):
        self.cancel_flag = True
        p = self.ff_proc
        if p and p.poll() is None:
            try:
                p.terminate()
            except Exception:
                pass
        return {"ok": True}

    def api_record_start(self, a):
        info = self.info
        if not info or not info.get("is_live"):
            return {"ok": False, "error": "Load a stream that is live right now."}
        if self.rec is not None:
            return {"ok": False, "error": "Already recording."}
        mh = self.max_height(a.get("quality"))

        def job():
            self.out_dir()
            self.log("Getting the live stream…")
            fresh = fetch_info(info.get("webpage_url"), self.cookies())
            if not fresh.get("is_live"):
                raise RuntimeError("The stream isn't live anymore. Load it again and clip it instead.")
            fmt = pick_live_format(fresh, mh)
            ts = os.path.join(tempfile.gettempdir(), f"ytlc_{int(time.time())}.ts")
            with self.lock:
                self.rec = LiveRecorder(fmt, fresh, ts)
                self.rec_title = fresh.get("title") or "live"
                self.rec_state = "recording"
            self.log(f"Recording at {fmt.get('height') or '?'}p. Press the button again to stop and save.", "ok", toast=True)
        return self.run_bg("Starting recording…", job)

    def api_record_stop(self, a):
        with self.lock:
            rec = self.rec
            if rec is None or self.rec_state != "recording":
                return {"ok": False, "error": "Not recording."}
            self.rec_state = "saving"
        vertical = bool(a.get("vertical", self.settings["vertical"]))
        threading.Thread(target=self._save_recording, args=(rec, vertical), daemon=True).start()
        return {"ok": True}

    def _save_recording(self, rec, vertical):
        with self.lock:
            self.busy, self.task, self.progress = True, "Saving recording…", None
        try:
            dur = rec.stop()
            if not os.path.exists(rec.ts_path) or os.path.getsize(rec.ts_path) < 1024:
                raise RuntimeError("Nothing was recorded (the stream may have blocked the download).")
            out_dir = self.out_dir()
            stamp = time.strftime("%Y-%m-%d %H-%M-%S")
            out = unique_path(os.path.join(out_dir, f"{safe_name(self.rec_title or 'live', 60)} LIVE {stamp}" +
                                           (" (vertical)" if vertical else "") + ".mp4"))
            self.log(f"Saving {fmt_time(dur)} of recording…")
            finalize_recording(rec.ts_path, out, vertical, lambda p: setattr(self, "progress", p), dur)
            try:
                os.remove(rec.ts_path)
            except OSError:
                pass
            self.add_output(out)
            self.log(f"Recording saved: {out}", "ok", toast=True)
        except Exception as e:
            self.log(f"ERROR: {e}", "error", toast=True)
        finally:
            with self.lock:
                self.rec, self.rec_state, self.rec_title = None, None, None
                self.busy, self.task, self.progress = False, None, None

    def _watch_recorder(self):
        while not self.quit_event.is_set():
            time.sleep(1)
            rec = self.rec
            if rec is not None and self.rec_state == "recording" and not rec.alive():
                self.log("The live stream ended or the connection dropped. Saving what was recorded.", "info", toast=True)
                self.api_record_stop({"vertical": self.settings["vertical"]})

    def api_update(self, _):
        def job():
            self.log("Updating yt-dlp…")
            r = subprocess.run([sys.executable, "-m", "pip", "install", "-U", "yt-dlp[default]"],
                               capture_output=True, text=True, creationflags=NO_WINDOW)
            out = (r.stdout or r.stderr or "").strip().splitlines()
            if r.returncode != 0:
                raise RuntimeError("Update failed: " + (out[-1] if out else "unknown error"))
            self.log(out[-1] if out else "Updated.")
            self.log("yt-dlp updated. Close the app and start it again to use the new version.", "ok", toast=True)
        return self.run_bg("Updating yt-dlp…", job)

    def api_save_settings(self, a):
        s = a.get("settings") or {}
        for k in ("out_dir", "quality", "cookies", "vertical"):
            if k in s:
                self.settings[k] = s[k]
        if self.settings["quality"] not in QUALITIES:
            self.settings["quality"] = "1080p"
        self.persist()
        return {"ok": True}

    def api_browse(self, _):
        start = self.settings.get("out_dir") or os.path.expanduser("~")
        if not os.path.isdir(start):
            start = os.path.expanduser("~")
        code = ("import tkinter as tk, tkinter.filedialog as fd, sys\n"
                "r = tk.Tk(); r.withdraw(); r.attributes('-topmost', True)\n"
                "d = fd.askdirectory(initialdir=sys.argv[1], title='Choose where to save clips')\n"
                "sys.stdout.write(d or '')\n")
        try:
            r = subprocess.run([sys.executable, "-c", code, start], capture_output=True, text=True, timeout=600)
            d = r.stdout.strip()
        except Exception:
            d = ""
        if d:
            self.settings["out_dir"] = os.path.normpath(d)
            self.persist()
        return {"ok": True, "dir": self.settings["out_dir"] if d else None}

    def api_open_folder(self, _):
        try:
            open_path(self.out_dir())
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def api_open_file(self, a):
        p = a.get("path") or ""
        if not any(o["path"] == p for o in self.outputs) or not os.path.exists(p):
            return {"ok": False, "error": "That file isn't there anymore."}
        open_path(p)
        return {"ok": True}

    def api_quit(self, _):
        threading.Thread(target=self.shutdown, daemon=True).start()
        return {"ok": True}

    def shutdown(self):
        rec = self.rec
        if rec is not None and self.rec_state == "recording":
            self.log("Window closed during recording. Saving it first…")
            self.api_record_stop({"vertical": self.settings["vertical"]})
            for _ in range(3600):
                if self.rec is None:
                    break
                time.sleep(0.5)
        self.persist()
        self.quit_event.set()


ENGINE = None


# --------------------------------------------------------------------------- server
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            try:
                with open(UI_FILE, "rb") as f:
                    self._send(200, f.read(), "text/html; charset=utf-8")
            except OSError:
                self._send(500, b"ui/index.html is missing next to clipper.py", "text/plain")
        elif path == "/ping":
            self._send(200, b"ytlc", "text/plain")
        else:
            self._send(404, b"not found", "text/plain")

    def do_POST(self):
        if not self.path.startswith("/api/"):
            return self._send(404, b"{}", "application/json")
        # only accept calls from our own page
        origin = self.headers.get("Origin")
        if origin and not re.match(r"^http://(127\.0\.0\.1|localhost):\d+$", origin):
            return self._send(403, b'{"ok":false}', "application/json")
        name = self.path[5:].split("?", 1)[0]
        try:
            n = int(self.headers.get("Content-Length") or 0)
            args = json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            args = {}
        fn = getattr(ENGINE, "api_" + name, None)
        if fn is None:
            res = {"ok": False, "error": "unknown action"}
        else:
            try:
                res = fn(args)
            except Exception as e:
                res = {"ok": False, "error": str(e)}
        self._send(200, json.dumps(res, default=str).encode("utf-8"), "application/json")


def find_app_browser():
    """Edge / Chrome / Brave, to show the interface in its own window (no tabs or address bar)."""
    c = []
    if IS_WIN:
        for base in (os.environ.get("ProgramFiles(x86)"), os.environ.get("ProgramFiles"), os.environ.get("LOCALAPPDATA")):
            if base:
                c += [os.path.join(base, r"Microsoft\Edge\Application\msedge.exe"),
                      os.path.join(base, r"Google\Chrome\Application\chrome.exe"),
                      os.path.join(base, r"BraveSoftware\Brave-Browser\Application\brave.exe")]
    elif IS_MAC:
        for app in ("Google Chrome", "Microsoft Edge", "Brave Browser", "Chromium"):
            c += [f"/Applications/{app}.app/Contents/MacOS/{app}",
                  os.path.expanduser(f"~/Applications/{app}.app/Contents/MacOS/{app}")]
    else:
        c += [shutil.which(x) or "" for x in ("google-chrome", "chromium", "chromium-browser", "microsoft-edge")]
    return next((p for p in c if p and os.path.isfile(p)), None)


def pick_port():
    for port in range(8765, 8790):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if not IS_WIN:
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                try:
                    if urlopen(f"http://127.0.0.1:{port}/ping", timeout=1).read() == b"ytlc":
                        return -port  # already running
                except Exception:
                    pass
    raise RuntimeError("No free port found.")


def open_window(url):
    exe = find_app_browser()
    if exe:
        profile = os.path.join(os.path.expanduser("~"), ".yt_live_clipper_window")
        try:
            return subprocess.Popen([exe, f"--app={url}", f"--user-data-dir={profile}", "--window-size=1320,900",
                                     "--no-first-run", "--no-default-browser-check", "--autoplay-policy=no-user-gesture-required"],
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            pass
    webbrowser.open(url)
    return None


def main():
    global ENGINE
    port = pick_port()
    if port < 0:
        url = f"http://127.0.0.1:{-port}/"
        print("YT Live Clipper is already running. Opening its window…")
        open_window(url)
        return
    ENGINE = Engine()
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{port}/"
    if yt_dlp is None or not FFMPEG:
        ENGINE.log("Missing: " + ", ".join(x for x, ok in (("yt-dlp", yt_dlp), ("ffmpeg", FFMPEG)) if not ok) +
                   ". Close the app and start it with run.bat / run.command.", "error")
    if not DENO:
        ENGINE.log("Note: 'deno' is not installed. Some YouTube videos may load with fewer qualities. "
                   "Starting the app with run.bat / run.command installs it.")
    print(f"YT Live Clipper is running at {url}")
    print("Keep this window open while you use the app. Closing the app window stops it.")
    headless = "--no-window" in sys.argv
    launched = time.time()
    win = None if headless else open_window(url)
    try:
        while not ENGINE.quit_event.is_set():
            time.sleep(1)
            if headless:
                continue
            if win is not None and win.poll() is not None:
                if time.time() - launched > 8:
                    # the app window was closed by the user
                    ENGINE.shutdown()
                    break
                win = None  # browser handed the window to an already-running process: use the heartbeat below
            if win is None:
                # quit when the page has been closed for a while and nothing is running
                idle = time.time() - ENGINE.last_poll
                if idle > 150 and not ENGINE.busy and ENGINE.rec is None:
                    ENGINE.shutdown()
    except KeyboardInterrupt:
        ENGINE.shutdown()
    server.shutdown()


if __name__ == "__main__":
    main()
