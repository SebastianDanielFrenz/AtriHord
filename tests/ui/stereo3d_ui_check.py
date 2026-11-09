#!/usr/bin/env python3
"""Browser check of the 2D -> 3D UI (media page: context menu, preview button,
select mode; settings page: the 2D to 3D card) against a STUBBED API.

    python tests/ui/stereo3d_ui_check.py [--chrome PATH] [--overlay DIR]
    python tests/ui/stereo3d_ui_check.py --serve 8766 --media-dir DIR [--overlay DIR]   # by hand, see below

A small stub server serves the real web_files/ pages (external stylesheet and
script tags are dropped, a console/error hook is injected) and answers /api/*
from a scripted configuration, logging every request. Headless Chrome loads
stereo3d_harness.html, which drives the real pages in iframes and reports each
check back (/__stub/result). Output follows the server harnesses: PASS/FAIL
lines and ALL PASS. Needs only Python and Chrome/Chromium - no node, no
playwright. Nothing here touches a live server.

--serve PORT keeps the stub server running instead of testing, with a fake library
(sample.mp4 and sample.jpg, already "3D ready", plus plain.mp4 and plain.jpg, not converted) whose side-by-side files come from
--media-dir (sbs.mp4 and xr_sbs.jpg, e.g. the xrspike folder): open
http://localhost:PORT/media.html in Chrome (localhost is a secure context, so WebXR works)
and press VR in a preview or the VR button in the top bar (or open http://localhost:PORT/vr.html). The pages are the real ones; the API is the stub."""
import argparse
import json
import os
import posixpath
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

HERE = os.path.dirname(os.path.abspath(__file__))
WEB = os.path.normpath(os.path.join(HERE, "..", "..", "web_files"))

OVERLAY = None
MEDIA_DIR = None                      # --media-dir: real sbs files for the by-hand mode
SBS_FILES = {"sbs.mp4": "sbs.mp4", "sbs.jpg": "xr_sbs.jpg"}
# by hand: the 2D "originals" of the unconverted sample items (the side-by-side samples, shown flat)
PLAIN_FILES = {"plain.mp4": "sbs.mp4", "plain.jpg": "xr_sbs.jpg"}

def make_png(w, h):
    """A valid w x h RGB gradient PNG (a 1x1 image gave WebGL INVALID_VALUE on texSubImage2D under SwiftShader)."""
    import struct
    import zlib

    def chunk(tag, data):
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
    rows = b"".join(bytes([0]) + bytes(v for x in range(w) for v in (x * 255 // max(1, w - 1), y * 255 // max(1, h - 1), 128)) for y in range(h))
    sig = bytes([0x89]) + b"PNG" + bytes([13, 10, 26, 10])
    return sig + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b"")


PNG = make_png(64, 32)
PNG_TALL = make_png(32, 64)
VIDEO_MP4 = None                      # a tiny playable H.264 file (made with ffmpeg at start, None when ffmpeg is missing)
VIDEO_KEY = "p" * 32                  # a 3D status key whose sbs.mp4 is that file
BIG_N = 2000                          # the "big" folder of the V1 wall tests


STANDARD_EDGES = {"720p": 1280, "fhd": 1920, "wqhd": 2560, "4k": 3840, "8k": 7680}


def filter_items(items, q, search=False):
    """The server's search/browse parameters, as far as the stub needs them (V2a): q (substring of the name, "*" = all),
    kind (video|image), favorites=1, orientation, min_res. Only used when the test config sets search_filter."""
    def one(name):
        return (q.get(name) or [""])[0]
    text = one("q").strip().lower()
    out = []
    for it in items:
        if search and text and text != "*" and text not in it.get("name", "").lower():
            continue
        kind = one("kind")
        if kind and (it.get("type") or "image") != kind:
            continue
        if one("favorites") == "1" and not it.get("favorite"):
            continue
        w, h = it.get("width", 0), it.get("height", 0)
        ori = one("orientation")
        if ori == "horizontal" and not w > h or ori == "vertical" and not h > w or ori == "square" and w != h:
            continue
        edge = STANDARD_EDGES.get(one("min_res"))
        if edge and max(w, h) < edge:
            continue
        out.append(it)
    return out


def big_files():
    """2000 stub files: mixed video/picture, mixed sizes (the aspect comes from width/height)."""
    out = []
    for i in range(BIG_N):
        video = i % 5 == 0
        ext = "mp4" if video else "jpg"
        w, h = ((1920, 1080), (1080, 1920), (1000, 1000), (1600, 1200), (1200, 1600))[i % 5]
        out.append({"name": "f%04d.%s" % (i, ext), "type": "video" if video else "image", "size": 1000 + i, "mtime": 1.7e9 + i, "added": 1.7e9 + i,
                    "root": "downloads", "path": "big/f%04d.%s" % (i, ext), "url": "/api/media/file?root=downloads&path=big/f%04d.%s" % (i, ext),
                    "favorite": False, "width": w, "height": h, "duration": 61.0 if video else 0})
    return out

HOOK = ("<script>window.__errors=[];(function(){var ce=console.error;console.error=function(){"
        "window.__errors.push(Array.prototype.map.call(arguments,String).join(' '));ce.apply(console,arguments)};"
        "window.addEventListener('error',function(e){window.__errors.push('error: '+e.message)});"
        "window.addEventListener('unhandledrejection',function(e){window.__errors.push('rejection: '+"
        "(e.reason&&e.reason.message||e.reason))});"
        # a fake navigator.xr supplied by the harness page (parent.__fakeXr), or none at all
        "try{if(window.parent&&window.parent!==window&&window.parent.__fakeXr){var fx=window.parent.__fakeXr;"
        "Object.defineProperty(navigator,'xr',{get:function(){return fx(window)},configurable:true})}}catch(e){}"
        "})();</script>")

# server.py GUEST_ALLOWED_FILES as deployed before P8 (P4/P5), copied so the harness can replay the redirect
GUEST_ALLOWED_FILES = {"media.html", "js/media.js", "js/gallery.js", "js/player.js",
                       "js/overlay-dismiss.js", "js/user-profile.js", "js/upscale.js",
                       "js/interp.js", "genres.html", "js/genres.js", "music.html", "js/music.js",
                       "js/listen_time.js"}


def guest_script_ok(filename):
    """server.py guest_script_ok (stage/stereo3d_p8srv): every plain js/**/*.js for a signed-in non-admin."""
    return (filename.startswith("js/") and filename.endswith(".js") and "\\" not in filename
            and posixpath.normpath(filename) == filename)

STATE = {"config": {"role": "admin", "files": [], "status": {}, "overview": {}}, "log": [], "results": [], "notes": [],
         "done": threading.Event(), "lock": threading.Lock()}


def page_html(path):
    text = open(path, encoding="utf-8").read()
    text = re.sub(r"<script[^>]*src=\"https?://[^>]*></script>", "", text)
    text = re.sub(r"<link[^>]*href=\"https?://[^>]*>", "", text)
    return text.replace("<head>", "<head>" + HOOK, 1).encode("utf-8")


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def handle(self):
        try:
            super().handle()
        except (ConnectionResetError, BrokenPipeError, ConnectionAbortedError):
            pass                      # Chrome dropped a connection (an iframe was replaced)

    def send(self, code, body=b"", ctype="application/json"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def body_json(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b""
        try:
            return json.loads(raw) if raw else None
        except ValueError:
            return None

    def do_GET(self):
        self.route()

    def do_POST(self):
        self.route()

    def do_DELETE(self):
        self.route()

    def do_PUT(self):
        self.route()

    def route(self):
        url = urlparse(self.path)
        body = self.body_json() if self.command in ("POST", "PUT", "DELETE") else None
        if url.path.startswith("/__stub/"):
            return self.stub(url.path[len("/__stub/"):], body)
        if url.path.startswith("/__harness/"):
            name = os.path.basename(url.path)
            full = os.path.join(HERE, name)
            if not os.path.isfile(full):
                return self.send(404, b"no")
            ctype = "text/html" if name.endswith(".html") else "application/javascript"
            return self.send(200, open(full, "rb").read(), ctype)
        if url.path.startswith("/api/"):
            with STATE["lock"]:
                STATE["log"].append({"method": self.command, "path": self.path, "body": body})
            return self.api(url, body)
        rel = url.path.lstrip("/") or "index.html"
        cfg = STATE["config"]
        if cfg.get("role") in ("user", "guest") and cfg.get("allowlist") in ("current", "extended", "p8srv", "p8v0"):
            # server.py serve_static for a non-admin: GUEST_ALLOWED_FILES (+ css/, downloads/, and with the P8 server
            # rule every plain js/**/*.js); anything else is a redirect to /media.html.
            #   current  the allowlist before P4/P5 (no new scripts at all)
            #   extended / p8srv  the P8 server as deployed (guest_script_ok, vr.html NOT listed)
            #   p8v0     the V0 server package: p8srv + "vr.html" in GUEST_ALLOWED_FILES
            mode = cfg["allowlist"]
            allowed = set(GUEST_ALLOWED_FILES)
            if mode == "p8v0":
                allowed.add("vr.html")
            scripts = mode != "current" and guest_script_ok(rel)
            if not (rel in allowed or scripts or rel.startswith(("css/", "downloads/"))):
                self.send_response(302)
                self.send_header("Location", "/media.html")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
        full = os.path.normpath(os.path.join(WEB, rel))
        if OVERLAY:
            over = os.path.normpath(os.path.join(OVERLAY, rel))
            if over.startswith(OVERLAY) and os.path.isfile(over):
                full = over
        if not full.startswith((WEB, OVERLAY or WEB)) or not os.path.isfile(full):
            if rel.startswith("media/") or rel == "media":
                full = os.path.join(WEB, "media.html")
            else:
                return self.send(404, b"not found", "text/plain")
        if full.endswith(".html"):
            return self.send(200, page_html(full), "text/html; charset=utf-8")
        ctype = {".js": "application/javascript", ".css": "text/css"}.get(os.path.splitext(full)[1], "application/octet-stream")
        return self.send(200, open(full, "rb").read(), ctype)

    def stub(self, name, body):
        if name == "config":
            with STATE["lock"]:
                STATE["config"].update(body or {})
            return self.send(200, {"ok": True})
        if name == "log":
            with STATE["lock"]:
                return self.send(200, list(STATE["log"]))
        if name == "clear":
            with STATE["lock"]:
                STATE["log"].clear()
            return self.send(200, {"ok": True})
        if name == "note":
            with STATE["lock"]:
                STATE["notes"].append(str((body or {}).get("text", "")))
            return self.send(200, {"ok": True})
        if name == "info":
            return self.send(200, {"has_video": bool(VIDEO_MP4), "big_n": BIG_N})
        if name == "result":
            with STATE["lock"]:
                STATE["results"].append(body)
            return self.send(200, {"ok": True})
        if name == "done":
            STATE["done"].set()
            return self.send(200, {"ok": True})
        return self.send(404, {"error": name})

    def serve_3d_file(self, name, key=""):
        if VIDEO_MP4 and key == VIDEO_KEY and name == "sbs.mp4":
            return self.serve_path(VIDEO_MP4, name)
        full = os.path.join(MEDIA_DIR, SBS_FILES.get(name, name)) if MEDIA_DIR else None
        return self.serve_path(full, name)

    def serve_plain_file(self, path):
        """/api/media/file: the 2D original. By hand (--media-dir) the sample files stand in for a library's own."""
        name = os.path.basename(path or "")
        if VIDEO_MP4 and name.startswith("play") and name.endswith(".mp4"):
            return self.serve_path(VIDEO_MP4, name)
        full = os.path.join(MEDIA_DIR, PLAIN_FILES[name]) if MEDIA_DIR and name in PLAIN_FILES else None
        if not full or not os.path.isfile(full):
            return self.send(200, PNG, "image/png")
        return self.serve_path(full, name)

    def serve_path(self, full, name):
        if not full or not os.path.isfile(full):
            return self.send(200, PNG if name.endswith((".jpg", ".png")) else b"\x00" * 64,
                             "image/png" if name.endswith((".jpg", ".png")) else "video/mp4")
        ctype = {"mp4": "video/mp4", "jpg": "image/jpeg", "png": "image/png"}.get(name.rsplit(".", 1)[-1], "application/octet-stream")
        size = os.path.getsize(full)
        start, end, code = 0, size - 1, 200
        m = re.match(r"bytes=(\d*)-(\d*)", self.headers.get("Range") or "")
        if m and (m.group(1) or m.group(2)):
            start = int(m.group(1)) if m.group(1) else size - int(m.group(2))
            end = min(int(m.group(2)), size - 1) if m.group(1) and m.group(2) else size - 1
            code = 206
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(end - start + 1))
        self.send_header("Cache-Control", "no-store")
        if code == 206:
            self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, size))
        self.end_headers()
        if self.command == "HEAD":
            return
        with open(full, "rb") as f:
            f.seek(start)
            left = end - start + 1
            while left > 0:
                chunk = f.read(min(1 << 20, left))
                if not chunk:
                    break
                self.wfile.write(chunk)
                left -= len(chunk)

    def api(self, url, body):
        cfg = STATE["config"]
        path, q = url.path, parse_qs(url.query)
        role = cfg.get("role", "admin")
        if path == "/api/me":
            if role == "guest":
                return self.send(200, {"role": "guest", "username": "guest"})
            return self.send(200, {"role": role, "username": "tester"})
        if path == "/api/media/browse":
            bpath = (q.get("path") or [""])[0].strip("/")
            broot = (q.get("root") or [""])[0]
            if cfg.get("multiroot") and not broot:
                return self.send(200, {"is_root_listing": True, "multi_root": True, "roots": [{"id": "downloads", "name": "Downloads"}, {"id": "movies", "name": "Movies"}]})
            if bpath == "big":
                files = big_files()
                return self.send(200, {"root": "downloads", "root_name": "Downloads", "path": "big", "is_root_listing": False, "multi_root": bool(cfg.get("multiroot")),
                                       "filtered": False, "total_files": len(files), "dirs": [], "files": files})
            tree = cfg.get("tree")
            if tree is not None and bpath in tree:
                node = tree[bpath]
                files = node.get("files", [])
                dirs = node.get("dirs", [])
                return self.send(200, {"root": broot or "downloads", "root_name": "Downloads" if broot in ("", "downloads") else broot, "path": bpath, "is_root_listing": False,
                                       "multi_root": bool(cfg.get("multiroot")), "filtered": False, "total_files": len(files), "dirs": dirs, "files": files})
            files = filter_items(cfg.get("files", []), q) if cfg.get("search_filter") else cfg.get("files", [])
            dirs = ["sub", "big"] if cfg.get("with_big") else ["sub"]
            return self.send(200, {"root": "downloads", "root_name": "Downloads", "path": bpath, "is_root_listing": False,
                                   "multi_root": bool(cfg.get("multiroot")), "filtered": False, "total_files": len(files),
                                   "dirs": dirs, "dir_info": [{"name": d, "artist": d, "id": None} for d in dirs],
                                   "files": files})
        if path == "/api/media/filters" and cfg.get("filter_options"):
            return self.send(200, cfg["filter_options"])
        if path == "/api/media/3d/view":
            vmode = cfg.get("view_mode", "ok")
            if vmode == "429":
                return self.send(429, {"error": "Too many requests", "retry_after_s": cfg.get("retry_after_s", 1)})
            if vmode in ("403", "off"):
                return self.send(403 if vmode == "403" else 409, {"error": "off"})
            vpath = (body or {}).get("path", "")
            if not (body or {}).get("leave"):
                with STATE["lock"]:
                    st = (cfg.setdefault("status", {})).get(vpath)
                    if not st or st.get("state") in (None, "none"):
                        cfg["status"][vpath] = {"state": "queued", "position": 1, "lane": "viewing"}
            return self.send(200, (cfg.get("status") or {}).get(vpath) or {"state": "none"})
        if path == "/api/media/file":
            return self.serve_plain_file((q.get("path") or [""])[0])
        if path.startswith("/api/media/thumb"):
            if cfg.get("thumb_404") and "/".join((q.get("path") or [""])[0:1]).endswith(tuple(cfg["thumb_404"])):
                return self.send(404, {"error": "no thumbnail"})
            return self.send(200, PNG_TALL if "tall" in (q.get("path") or [""])[0] else PNG, "image/png")
        if path == "/api/media/3d/status":
            st = (cfg.get("status") or {}).get((q.get("path") or [""])[0])
            return self.send(200, st or {"state": "none"})
        if path == "/api/media/3d/list":
            return self.send(200, {"enabled": True, "items": cfg.get("list", [])})
        if path == "/api/media/3d/file":
            return self.serve_3d_file((q.get("name") or [""])[0], (q.get("key") or [""])[0])
        if path == "/api/media/3d/convert":
            if role != "admin":
                return self.send(403, {"error": "Forbidden"})
            items = (body or {}).get("items") or []
            return self.send(200, {"results": [{"root": i.get("root"), "path": i.get("path"), "status": "queued"} for i in items],
                                   "counts": {"queued": len(items)}})
        if path == "/api/media/3d/retry":
            return self.send(200, {"retried": 1})
        if path == "/api/media/3d/queue":
            return self.send(200, {"removed": 1})
        if path == "/api/settings/3d/cache":
            return self.send(200, {"removed": 7})
        if path == "/api/settings/3d":
            return self.send(200, cfg.get("overview") or {})
        if path == "/api/settings":
            return self.send(200, {"server_time": time.time(), "values": {"stereo3d.enabled": True, "stereo3d.cache_max_gb": 150},
                                   "schema": [{"id": "stereo3d", "title": "2D to 3D", "fields": [
                                       {"key": "stereo3d.enabled", "type": "bool", "label": "Allow 3D conversion", "help": "On demand."},
                                       {"key": "stereo3d.cache_max_gb", "type": "int", "min": 10, "max": 5000, "unit": "GB",
                                        "label": "3D cache size limit"}]}]})
        if path in ("/api/roots", "/api/share", "/api/media/characters", "/api/media/people", "/api/media/profiles"):
            return self.send(200, [] if path in ("/api/roots", "/api/share") else {"items": [], "people": [], "characters": [], "profiles": []})
        if path in ("/api/media/search", "/api/media/recent"):
            items = cfg.get("recent" if path.endswith("recent") else "search_items", cfg.get("files", []) if path.endswith("search") else [])
            if path.endswith("search") and cfg.get("search_filter"):
                items = filter_items(items, q, search=True)
            off = int((q.get("offset") or ["0"])[0])
            lim = int((q.get("limit") or ["100"])[0])
            page = items[off:off + lim]
            return self.send(200, {"items": page, "total": len(items), "has_more": off + lim < len(items)})
        return self.send(200, {})


def make_test_video(dest_dir):
    """A 20 s 128x64 (two 64x64 eyes) H.264 file for the video checks; None when there is no ffmpeg."""
    ff = shutil.which("ffmpeg") or next((c for c in (r"S:fmpegfmpeg.exe", "/s/ffmpeg/ffmpeg.exe") if os.path.isfile(c)), None)
    if not ff:
        return None
    out = os.path.join(dest_dir, "play.mp4")
    try:
        subprocess.run([ff, "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc=size=128x64:rate=5:duration=20", "-pix_fmt", "yuv420p",
                        "-c:v", "libx264", "-preset", "ultrafast", "-g", "5", "-movflags", "+faststart", out], check=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return None
    return out if os.path.isfile(out) else None


def find_chrome(explicit):
    cands = [explicit, os.environ.get("CHROME"), r"C:\Program Files\Google\Chrome\Application\chrome.exe",
             r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
             r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"]
    cands += [shutil.which(n) for n in ("google-chrome", "chromium", "chromium-browser", "chrome", "msedge")]
    return next((c for c in cands if c and os.path.isfile(c)), None)


def serve_by_hand(port):
    key_v, key_p = "a" * 32, "b" * 32
    base = "/api/media/3d/file?key=%s&name="
    video = {"state": "done", "key": key_v, "kind": "video", "path": "sample.mp4", "sbs_url": base % key_v + "sbs.mp4",
             "depth_url": base % key_v + "depth.mp4", "eye_w": 1080, "eye_h": 1440}
    picture = {"state": "done", "key": key_p, "kind": "picture", "path": "sample.jpg", "sbs_url": base % key_p + "sbs.jpg",
               "depth_url": base % key_p + "depth.png", "eye_w": 1080, "eye_h": 1440}
    files = [{"name": n, "type": t, "size": 1000, "mtime": 1.7e9, "added": 1.7e9, "root": "downloads", "path": n,
              "url": "/api/media/file?root=downloads&path=" + n, "favorite": False}
             for n, t in (("sample.mp4", "video"), ("sample.jpg", "image"), ("plain.mp4", "video"), ("plain.jpg", "image"))]
    global VIDEO_MP4
    VIDEO_MP4 = make_test_video(tempfile.mkdtemp(prefix="ui3d_video_"))
    if VIDEO_MP4:
        files.append({"name": "play.mp4", "type": "video", "size": 1000, "mtime": 1.7e9, "added": 1.7e9, "root": "downloads", "path": "play.mp4",
                      "url": "/api/media/file?root=downloads&path=play.mp4", "favorite": False, "duration": 20})
    STATE["config"].update({"with_big": True, "recent": big_files()[:300], "view_mode": "ok"})
    STATE["config"].update({"role": "admin", "files": files,
                            "status": {"sample.mp4": video, "sample.jpg": picture},
                            "list": [dict(video, eye_w=1080, eye_h=1440, title="sample video"),
                                     dict(picture, title="sample picture")]})
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print("stub media-dl on http://localhost:%d/media.html  and /vr.html  (Ctrl+C to stop)" % port, flush=True)
    print("  sample.mp4 / sample.jpg are 3D ready (open in 3D), plain.mp4 / plain.jpg are not converted (open in 2D)")
    print("  folder 'big' = 2000 stub files (tiny thumbnails); POST /api/media/3d/view is emulated (status goes to 'queued' only - edit it via /__stub/config)")
    if VIDEO_MP4:
        print("  play.mp4 is a tiny playable 2D video (its 3D file plays when the status key is 'p' x 32)")
    if not MEDIA_DIR:
        print("note: no --media-dir, the 3D files are empty placeholders")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--chrome")
    ap.add_argument("--overlay", help="folder whose files replace those of web_files (the staged deploy files)")
    ap.add_argument("--timeout", type=int, default=150)
    ap.add_argument("--only", help="run only this part of the browser harness (wall)")
    ap.add_argument("--serve", type=int, metavar="PORT", help="keep the stub server running on this port (by-hand VR test)")
    ap.add_argument("--media-dir", help="folder with sbs.mp4 and xr_sbs.jpg served as the 3D files")
    args = ap.parse_args()
    global OVERLAY, MEDIA_DIR, VIDEO_MP4
    if args.overlay:
        OVERLAY = os.path.normpath(os.path.abspath(args.overlay))
    if args.media_dir:
        MEDIA_DIR = os.path.normpath(os.path.abspath(args.media_dir))
    if args.serve:
        return serve_by_hand(args.serve)
    chrome = find_chrome(args.chrome)
    if not chrome:
        print("FAIL no Chrome/Chromium found (pass --chrome PATH)")
        return 1
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    profile = tempfile.mkdtemp(prefix="ui3d_chrome_")
    VIDEO_MP4 = make_test_video(profile)
    if not VIDEO_MP4:
        print("note: no ffmpeg - the video checks that need a playable file are skipped")
    proc = subprocess.Popen(
        [chrome, "--headless=new", "--disable-gpu", "--no-first-run", "--no-default-browser-check",
         "--window-size=1300,900", "--use-angle=swiftshader", "--enable-unsafe-swiftshader", "--ignore-gpu-blocklist", f"--user-data-dir={profile}",
         f"http://127.0.0.1:{port}/__harness/stereo3d_harness.html" + (f"?only={args.only}" if args.only else "")],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    finished = STATE["done"].wait(args.timeout)
    proc.terminate()
    try:
        proc.wait(10)
    except subprocess.TimeoutExpired:
        proc.kill()
    server.shutdown()
    shutil.rmtree(profile, ignore_errors=True)
    passed = failed = 0
    for r in STATE["results"]:
        if r["ok"]:
            passed += 1
            print("PASS", r["name"])
        else:
            failed += 1
            print("FAIL", r["name"], "-", r["detail"][:400])
    if not finished:
        failed += 1
        print("FAIL the harness did not finish within %d s" % args.timeout)
    for n in STATE["notes"]:
        print("NOTE", n)
    print("%d pass, %d fail" % (passed, failed))
    if not failed:
        print("ALL PASS")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
