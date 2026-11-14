#!/usr/bin/env python3
"""Stub server for trying Settings -> Modules without the real server (which would start its downloaders):

    py -3 tests/ui/modules_settings_stub.py [port]     # default 8792 -> http://127.0.0.1:8792/settings.html#modules

Serves web_files/, answers /api/settings with an empty schema and the module endpoints the way server.py does,
through module_manager. Installs go into a temp folder; the "online repo" is this checkout's modules/ folder
(served at /modules-src/), so the real signed index and the real public key are checked.
"""
import json
import os
import re
import sys
import tempfile
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
sys.path.insert(0, ROOT)
import module_manager as mm  # noqa: E402

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8792
TMP = tempfile.mkdtemp(prefix="modstub-")
mm.INSTALL_DIR = os.path.join(TMP, "installed")
mm.PREVIOUS_DIR = os.path.join(mm.INSTALL_DIR, ".previous")
mm.REMOVED_DIR = os.path.join(mm.INSTALL_DIR, ".removed")
mm.BASE_URL = "http://127.0.0.1:%d/modules-src/" % PORT
mm.SOURCE_DIR = os.path.join(TMP, "no-checkout")      # pretend the server has no local modules/ folder


class H(SimpleHTTPRequestHandler):
    def __init__(self, *a, **k):
        super().__init__(*a, directory=os.path.join(ROOT, "web_files"), **k)

    def log_message(self, *a):
        pass

    def translate_path(self, path):
        if path.startswith("/modules-src/"):
            rel = path[len("/modules-src/"):].split("?")[0]
            return os.path.join(ROOT, "modules", *rel.split("/"))
        return super().translate_path(path)

    def _json(self, code, data):
        body = json.dumps(data).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/api/settings":
            return self._json(200, {"schema": {}, "values": {}, "server_time": "--"})
        if self.path.startswith("/api/modules"):
            if "refresh=1" in self.path:
                try:
                    mm.online_index(force=True)
                except mm.ModuleError:
                    pass
            return self._json(200, mm.overview(online=True))
        return super().do_GET()

    def do_POST(self):
        m = re.fullmatch(r"/api/modules/([^/]+)/install", self.path)
        if not m:
            return self._json(404, {"error": "not found"})
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n) or b"{}")
        try:
            got = mm.install_local(m.group(1)) if body.get("source") == "local" else mm.install_online(m.group(1))
        except mm.ModuleError as e:
            return self._json(400, {"error": str(e)})
        return self._json(200, {"installed": got.get("version"), "overview": mm.overview(online=True)})

    def do_DELETE(self):
        m = re.fullmatch(r"/api/modules/([^/]+)", self.path)
        if not m:
            return self._json(404, {"error": "not found"})
        try:
            mm.remove(m.group(1))
        except mm.ModuleError as e:
            return self._json(400, {"error": str(e)})
        return self._json(200, {"removed": m.group(1), "overview": mm.overview(online=True)})


print("installs go to " + mm.INSTALL_DIR, flush=True)
ThreadingHTTPServer(("127.0.0.1", PORT), H).serve_forever()
