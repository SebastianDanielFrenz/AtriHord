#!/usr/bin/env python3
"""Serves the repo for tests/ui/vis_preset_sheet.html and takes the picked presets back:

    py -3 tests/ui/vis_preset_sheet_server.py [port] [out.json]   # default 8768, tests/ui/picked_presets.json
    -> http://127.0.0.1:8768/tests/ui/vis_preset_sheet.html

POST /save with a JSON body { name: preset } writes it to the out file (replacing it).
"""
import json
import os
import sys
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8768
OUT = sys.argv[2] if len(sys.argv) > 2 else os.path.join(ROOT, "tests", "ui", "picked_presets.json")


class H(SimpleHTTPRequestHandler):
    def __init__(self, *a, **k):
        super().__init__(*a, directory=ROOT, **k)

    def log_message(self, *a):
        pass

    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def do_POST(self):
        if self.path != "/save":
            self.send_error(404)
            return
        data = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)))
        with open(OUT, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        self.send_response(200)
        self.end_headers()


print("out file: " + OUT, flush=True)
ThreadingHTTPServer(("127.0.0.1", PORT), H).serve_forever()
