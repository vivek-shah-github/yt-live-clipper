#!/usr/bin/env python3
"""
YT Live Clipper - web version
-----------------------------
Runs the clipper on this computer and shares it as a password-protected website,
so anyone you give the link + password can use it from any browser (phone or PC)
without installing anything.

- Uses the same engine as the desktop app (../desktop/clipper.py) and the same interface.
- Each browser gets its own clips, jobs and downloads.
- Finished clips download straight to the visitor's device. Server copies are
  deleted after 24 hours.
- A free Cloudflare quick tunnel gives the public https link (no account needed).

Start it with start-web.command (Mac) or start-web.bat (Windows).
Options:  --port 8765   --no-tunnel   --password <new password>
"""
import hashlib
import hmac
import html
import io
import json
import os
import platform
import re
import secrets
import shutil
import subprocess
import sys
import tarfile
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "desktop"))
import clipper  # noqa: E402  (the desktop engine)

DATA = os.path.join(HERE, "data")
FILES = os.path.join(DATA, "files")
CONF = os.path.join(DATA, "web-config.json")
UI_FILE = os.path.join(HERE, "..", "desktop", "ui", "index.html")
KEEP_FILES_H = 24          # delete finished clips from this computer after this many hours
SESSION_IDLE_H = 12        # forget idle sessions after this many hours
MAX_PARALLEL_FFMPEG = 2    # how many exports/recording-saves may run at the same time
SYSTEM = platform.system()

os.makedirs(FILES, exist_ok=True)


# --------------------------------------------------------------------------- config / password
def load_conf():
    try:
        with open(CONF, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_conf(c):
    with open(CONF, "w", encoding="utf-8") as f:
        json.dump(c, f, indent=2)


def hash_pw(pw, salt):
    return hashlib.pbkdf2_hmac("sha256", pw.encode(), bytes.fromhex(salt), 200_000).hex()


CFG = load_conf()
if not CFG.get("secret"):
    CFG["secret"] = secrets.token_hex(32)
    save_conf(CFG)
SECRET = bytes.fromhex(CFG["secret"])


def set_password(pw):
    salt = secrets.token_hex(16)
    CFG["pw_salt"], CFG["pw_hash"] = salt, hash_pw(pw, salt)
    CFG["pw_version"] = secrets.token_hex(4)  # logs everyone out when the password changes
    save_conf(CFG)


def check_password(pw):
    if not CFG.get("pw_hash"):
        return False
    return hmac.compare_digest(hash_pw(pw, CFG["pw_salt"]), CFG["pw_hash"])


def sign(sid):
    mac = hmac.new(SECRET, f"{sid}:{CFG.get('pw_version', '')}".encode(), "sha256").hexdigest()[:32]
    return f"{sid}.{mac}"


def verify(token):
    if not token or "." not in token:
        return None
    sid, mac = token.split(".", 1)
    if not re.fullmatch(r"[a-f0-9]{24}", sid):
        return None
    return sid if hmac.compare_digest(sign(sid), token) else None


# --------------------------------------------------------------------------- engine per visitor
_ff_slots = threading.BoundedSemaphore(MAX_PARALLEL_FFMPEG)
_orig_run_ffmpeg = clipper.run_ffmpeg


def _limited_run_ffmpeg(*a, **kw):
    with _ff_slots:
        return _orig_run_ffmpeg(*a, **kw)


clipper.run_ffmpeg = _limited_run_ffmpeg  # finalize_recording and exports both go through this


class WebEngine(clipper.Engine):
    def __init__(self, sid):
        super().__init__()
        self.sid = sid
        self.settings = {"out_dir": "Downloads to your device", "quality": "1080p",
                         "cookies": "none", "vertical": False}
        self.touched = time.time()

    # files go to this visitor's own folder on the server
    def out_dir(self):
        d = os.path.join(FILES, self.sid)
        os.makedirs(d, exist_ok=True)
        return d

    def persist(self):
        pass

    def log(self, msg, level="info", toast=False):
        # don't show this computer's folder paths to visitors
        d = os.path.join(FILES, self.sid)
        msg = msg.replace(d + os.sep, "").replace(d, "the Saved list")
        super().log(msg, level, toast)

    def cookies(self):
        return None

    def add_output(self, path):
        name = os.path.basename(path)
        with self.lock:
            self.outputs.append({"name": name, "path": name,
                                 "url": f"/files/{urllib.parse.quote(name)}",
                                 "size": os.path.getsize(path) if os.path.exists(path) else 0})

    def api_init(self, a):
        r = super().api_init(a)
        r.update({"platform": "web", "canBrowse": False, "canCookies": False, "canQuit": False,
                  "canOpenFolder": False, "canDownload": True, "settings": self.settings,
                  "setupHint": "The server is missing yt-dlp or ffmpeg. Restart it with start-web."})
        return r

    def api_save_settings(self, a):
        s = a.get("settings") or {}
        for k in ("quality", "vertical"):
            if k in s:
                self.settings[k] = s[k]
        if self.settings["quality"] not in clipper.QUALITIES:
            self.settings["quality"] = "1080p"
        return {"ok": True}

    def api_open_file(self, a):
        return {"ok": False, "error": "Use the Download button."}

    def api_browse(self, a):
        return {"ok": False, "error": "Clips download to your device."}

    api_open_folder = api_browse

    def api_quit(self, a):
        return {"ok": False, "error": "Not available in the web version."}

    def idle(self):
        return not self.busy and self.rec is None


ENGINES = {}
ENG_LOCK = threading.Lock()


def engine_for(sid):
    with ENG_LOCK:
        e = ENGINES.get(sid)
        if e is None:
            e = ENGINES[sid] = WebEngine(sid)
        e.touched = time.time()
        return e


def janitor():
    while True:
        time.sleep(600)
        now = time.time()
        with ENG_LOCK:
            for sid, e in list(ENGINES.items()):
                if e.idle() and now - e.touched > SESSION_IDLE_H * 3600:
                    e.quit_event.set()
                    del ENGINES[sid]
        for root, _, files in os.walk(FILES):
            for f in files:
                p = os.path.join(root, f)
                try:
                    if now - os.path.getmtime(p) > KEEP_FILES_H * 3600:
                        os.remove(p)
                except OSError:
                    pass
        # tidy recordings left in the temp folder by stopped sessions
        tmp = clipper.tempfile.gettempdir()
        for f in os.listdir(tmp):
            if f.startswith("ytlc_") and f.endswith(".ts"):
                p = os.path.join(tmp, f)
                try:
                    if now - os.path.getmtime(p) > KEEP_FILES_H * 3600:
                        os.remove(p)
                except OSError:
                    pass


# --------------------------------------------------------------------------- login page
LOGIN_HTML = """<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>YT Live Clipper</title>
<link href="https://fonts.googleapis.com/css2?family=Barlow+Semi+Condensed:wght@600;700&display=swap" rel="stylesheet">
<style>
body{margin:0;min-height:100vh;display:grid;place-items:center;background:#141829;color:#e9ebf4;font:16px/1.5 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
form{box-sizing:border-box;width:min(380px,calc(100% - 32px));background:#1b2035;border:1px solid #363e60;border-radius:16px;padding:28px}
.b{display:flex;align-items:center;gap:10px;font:700 24px "Barlow Semi Condensed",sans-serif;margin-bottom:20px}
.m{width:28px;height:28px;border-radius:7px;background:#ff4f5e;display:grid;place-items:center}
.m::after{content:"";border-left:10px solid #fff;border-top:7px solid transparent;border-bottom:7px solid transparent;margin-left:3px}
label{display:block;font-weight:600;margin-bottom:6px}
input{width:100%;box-sizing:border-box;height:48px;border-radius:10px;border:1px solid #363e60;background:#141829;color:inherit;padding:0 14px;font-size:16px}
input:focus{outline:none;border-color:#f4b740}
button{margin-top:16px;width:100%;height:48px;border:0;border-radius:10px;background:#f4b740;color:#1b1404;font-weight:700;font-size:16px;cursor:pointer}
.e{color:#ff8b95;margin:12px 0 0;font-size:14px}.n{color:#8d94b0;font-size:13px;margin:16px 0 0}
</style></head><body>
<form method="post" action="/login"><div class="b"><span class="m"></span>YT Live Clipper</div>
<label for="p">Password</label><input id="p" name="password" type="password" autocomplete="current-password" autofocus required>
<input type="hidden" name="next" value="%NEXT%">
<button type="submit">Sign in</button>%ERR%
<p class="n">Ask the person who shared this link for the password.</p></form></body></html>"""


# --------------------------------------------------------------------------- server
class Handler(BaseHTTPRequestHandler):
    server_version = "YTLiveClipper"

    def log_message(self, *a):
        pass

    def _cookie(self, name):
        for part in (self.headers.get("Cookie") or "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == name:
                return v
        return None

    def _sid(self):
        return verify(self._cookie("ytlc"))

    def _secure(self):
        return (self.headers.get("X-Forwarded-Proto") or "") == "https" or "trycloudflare.com" in (self.headers.get("Host") or "")

    def _send(self, code, body, ctype, extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "strict-origin-when-cross-origin")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _redirect(self, to, extra=None):
        h = {"Location": to}
        h.update(extra or {})
        self._send(303, b"", "text/plain", h)

    def _login_page(self, err="", nxt="/"):
        body = LOGIN_HTML.replace("%ERR%", f'<p class="e">{html.escape(err)}</p>' if err else "") \
                         .replace("%NEXT%", html.escape(nxt, quote=True))
        self._send(401 if err else 200, body.encode(), "text/html; charset=utf-8")

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        path, _, query = self.path.partition("?")
        if path == "/ping":
            return self._send(200, b"ytlc-web", "text/plain")
        if path == "/login":
            return self._login_page()
        sid = self._sid()
        if path == "/logout":
            return self._redirect("/login", {"Set-Cookie": "ytlc=; Path=/; Max-Age=0"})
        if not sid:
            nxt = self.path if path == "/" else "/"
            return self._redirect("/login" + ("?next=" + urllib.parse.quote(nxt) if query else ""))
        if path in ("/", "/index.html"):
            try:
                with open(UI_FILE, "rb") as f:
                    return self._send(200, f.read(), "text/html; charset=utf-8")
            except OSError:
                return self._send(500, b"desktop/ui/index.html is missing", "text/plain")
        if path.startswith("/files/"):
            return self._serve_file(sid, urllib.parse.unquote(path[len("/files/"):]))
        self._send(404, b"not found", "text/plain")

    def _serve_file(self, sid, name):
        if "/" in name or "\\" in name or name.startswith("."):
            return self._send(404, b"not found", "text/plain")
        p = os.path.join(FILES, sid, name)
        if not os.path.isfile(p):
            return self._send(404, b"This file has expired. Export it again.", "text/plain")
        size = os.path.getsize(p)
        start, end = 0, size - 1
        rng = self.headers.get("Range")
        m = re.match(r"bytes=(\d*)-(\d*)$", rng or "")
        if m and (m.group(1) or m.group(2)):
            if m.group(1):
                start = int(m.group(1))
                end = int(m.group(2)) if m.group(2) else size - 1
            else:
                start = max(0, size - int(m.group(2)))
            end = min(end, size - 1)
            if start > end:
                return self._send(416, b"", "text/plain", {"Content-Range": f"bytes */{size}"})
        partial = bool(m and (m.group(1) or m.group(2)))
        self.send_response(206 if partial else 200)
        self.send_header("Content-Type", "video/mp4")
        self.send_header("Content-Length", str(end - start + 1))
        self.send_header("Accept-Ranges", "bytes")
        if partial:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.send_header("Content-Disposition", "attachment; filename*=UTF-8''" + urllib.parse.quote(name))
        self.end_headers()
        if self.command == "HEAD":
            return
        with open(p, "rb") as f:
            f.seek(start)
            left = end - start + 1
            while left > 0:
                chunk = f.read(min(1 << 20, left))
                if not chunk:
                    break
                try:
                    self.wfile.write(chunk)
                except (BrokenPipeError, ConnectionResetError):
                    return
                left -= len(chunk)

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        n = min(int(self.headers.get("Content-Length") or 0), 2_000_000)
        raw = self.rfile.read(n) if n else b""
        if path == "/login":
            form = urllib.parse.parse_qs(raw.decode("utf-8", "replace"))
            pw = (form.get("password") or [""])[0]
            nxt = (form.get("next") or ["/"])[0]
            if not nxt.startswith("/") or nxt.startswith("//"):
                nxt = "/"
            time.sleep(0.6)  # slows down password guessing
            if not check_password(pw):
                return self._login_page("That password isn't right.", nxt)
            sid = self._sid() or secrets.token_hex(12)
            flags = "; Secure" if self._secure() else ""
            return self._redirect(nxt, {"Set-Cookie": f"ytlc={sign(sid)}; Path=/; Max-Age=2592000; HttpOnly; SameSite=Lax{flags}"})
        if not path.startswith("/api/"):
            return self._send(404, b"{}", "application/json")
        sid = self._sid()
        if not sid:
            return self._send(401, b'{"ok":false,"auth":false,"error":"Please sign in again."}', "application/json")
        origin = self.headers.get("Origin")
        if origin and urllib.parse.urlparse(origin).netloc != self.headers.get("Host"):
            return self._send(403, b'{"ok":false}', "application/json")
        try:
            args = json.loads(raw or b"{}")
        except Exception:
            args = {}
        eng = engine_for(sid)
        fn = getattr(eng, "api_" + path[5:], None)
        try:
            res = fn(args) if fn else {"ok": False, "error": "unknown action"}
        except Exception as e:
            res = {"ok": False, "error": str(e)}
        self._send(200, json.dumps(res, default=str).encode(), "application/json")


# --------------------------------------------------------------------------- cloudflare quick tunnel
def cloudflared_path():
    exe = shutil.which("cloudflared")
    if exe:
        return exe
    bindir = os.path.join(HERE, "bin")
    local = os.path.join(bindir, "cloudflared.exe" if SYSTEM == "Windows" else "cloudflared")
    if os.path.isfile(local):
        return local
    arch = platform.machine().lower()
    base = "https://github.com/cloudflare/cloudflared/releases/latest/download/"
    if SYSTEM == "Darwin":
        url = base + ("cloudflared-darwin-arm64.tgz" if arch in ("arm64", "aarch64") else "cloudflared-darwin-amd64.tgz")
    elif SYSTEM == "Windows":
        url = base + ("cloudflared-windows-arm64.exe" if "arm" in arch else "cloudflared-windows-amd64.exe")
    else:
        url = base + ("cloudflared-linux-arm64" if arch in ("arm64", "aarch64") else "cloudflared-linux-amd64")
    print("Downloading Cloudflare tunnel (one time)...")
    os.makedirs(bindir, exist_ok=True)
    data = urllib.request.urlopen(url, timeout=120).read()
    if url.endswith(".tgz"):
        with tarfile.open(fileobj=io.BytesIO(data)) as t:
            member = next(m for m in t.getmembers() if m.name.endswith("cloudflared"))
            with open(local, "wb") as f:
                f.write(t.extractfile(member).read())
    else:
        with open(local, "wb") as f:
            f.write(data)
    os.chmod(local, 0o755)
    return local


def start_tunnel(port):
    try:
        exe = cloudflared_path()
    except Exception as e:
        print(f"Couldn't get the Cloudflare tunnel ({e}). The app still works on this computer at http://127.0.0.1:{port}/")
        return None, None
    p = subprocess.Popen([exe, "tunnel", "--no-autoupdate", "--url", f"http://127.0.0.1:{port}"],
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="replace",
                         creationflags=clipper.NO_WINDOW)
    found = {"url": None}
    ready = threading.Event()

    def reader():
        for line in p.stdout:
            m = re.search(r"https://[a-z0-9-]+\.trycloudflare\.com", line)
            if m and not found["url"]:
                found["url"] = m.group(0)
                ready.set()
        ready.set()
    threading.Thread(target=reader, daemon=True).start()
    ready.wait(60)
    return p, found["url"]


def keep_awake():
    """Stop the Mac from sleeping while the server runs (the screen may still turn off)."""
    if SYSTEM == "Darwin" and shutil.which("caffeinate"):
        try:
            return subprocess.Popen(["caffeinate", "-i", "-w", str(os.getpid())])
        except Exception:
            pass
    return None


# --------------------------------------------------------------------------- main
def main():
    args = sys.argv[1:]
    port = 8765
    if "--port" in args:
        port = int(args[args.index("--port") + 1])
    if "--password" in args:
        set_password(args[args.index("--password") + 1])
        print("Password changed. Everyone has to sign in again.")
    if not CFG.get("pw_hash"):
        print()
        print("Choose a password for your clipper website (people need it to use the link).")
        while True:
            pw = input("New password (at least 6 characters): ").strip()
            if len(pw) >= 6:
                break
        set_password(pw)
        print("Saved. To change it later: start-web with --password <new password>")

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    threading.Thread(target=janitor, daemon=True).start()
    keep_awake()

    print()
    print(f"yt-dlp {getattr(getattr(clipper.yt_dlp, 'version', None), '__version__', 'MISSING')}, "
          f"ffmpeg {'ok' if clipper.FFMPEG else 'MISSING'}, deno {'ok' if clipper.DENO else 'missing'}")
    print(f"On this computer: http://127.0.0.1:{port}/")
    tunnel = None
    if "--no-tunnel" not in args:
        tunnel, url = start_tunnel(port)
        if url:
            with open(os.path.join(HERE, "current-link.txt"), "w") as f:
                f.write(url + "\n")
            print()
            print("=" * 64)
            print(f"  Your clipper website:  {url}")
            print("  Share this link + the password. It works on any phone or PC.")
            print("  The link changes each time you restart this window.")
            print("=" * 64)
        elif tunnel is not None:
            print("The Cloudflare tunnel didn't give a link. Check your internet and restart.")
    print()
    print("Keep this window open. Closing it stops the website.")
    started, fails = time.time(), 0
    try:
        while True:
            time.sleep(5)
            if tunnel is not None and tunnel.poll() is not None:
                fails = fails + 1 if time.time() - started < 120 else 1
                if fails >= 5:
                    print("The Cloudflare tunnel keeps disconnecting. Check the internet connection, or whether a")
                    print("firewall/VPN blocks Cloudflare, then restart this window. The site still works on this computer.")
                    tunnel = None
                    continue
                wait = min(120, 10 * 2 ** (fails - 1))
                print(f"The Cloudflare tunnel stopped; trying again in {wait} s...")
                time.sleep(wait)
                tunnel, url = start_tunnel(port)
                started = time.time()
                if url:
                    print(f"New link (the old one no longer works): {url}")
                    with open(os.path.join(HERE, "current-link.txt"), "w") as f:
                        f.write(url + "\n")
    except KeyboardInterrupt:
        pass
    finally:
        if tunnel is not None:
            tunnel.terminate()
        server.shutdown()


if __name__ == "__main__":
    main()
