"""The player's frame-interpolation routes (/api/media/interp/config and
/api/media/interp/stream) out of a STAGED server.py's AST, with the vision
worker replaced by a fake: config answers, the output rate (exact screen rates
?o= and the older ?m= multiplier, their ceilings and cache variant names),
upload once per key, the stream relay, and worker errors turned into answers.

  venv/bin/python interp_server_check.py /tmp/.../server.py
"""
import ast
import io
import json
import os
import sys
import tempfile
import urllib.error

BASE = os.environ.get("ATRIHORD_DIR") or os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SERVER = sys.argv[1]
sys.path.insert(0, BASE)

tree = ast.parse(open(SERVER, encoding="utf-8").read())
defs = {}
ns = {"__name__": "harness", "__file__": os.path.join(BASE, "server.py")}
for node in tree.body:
    if isinstance(node, (ast.Import, ast.ImportFrom)):
        try:
            exec(compile(ast.Module(body=[node], type_ignores=[]), SERVER, "exec"), ns)
        except Exception:
            pass
    elif isinstance(node, (ast.FunctionDef, ast.ClassDef)):
        defs[node.name] = node
    elif isinstance(node, ast.Assign):
        for t in node.targets:
            if isinstance(t, ast.Name):
                defs.setdefault(t.id, node)


def resolve(thunk):
    for _ in range(300):
        try:
            return thunk()
        except NameError as exc:
            missing = getattr(exc, "name", None) or str(exc).split("'")[1]
            if missing in ns or missing not in defs:
                raise
            load(missing)
    raise RuntimeError("names did not resolve")


def load(name):
    code = compile(ast.Module(body=[defs[name]], type_ignores=[]), SERVER, "exec")
    resolve(lambda: exec(code, ns))


RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append(ok)
    print(("PASS " if ok else "FAIL ") + name + (f" - {detail}" if detail and not ok else ""))


for n in ("app", "DEFAULT_CONFIG", "interp_settings", "_interp_worker", "_interp_upload_locks",
          "_interp_upload_locks_guard", "_interp_worker_request", "interp_worker_status",
          "interp_ensure_uploaded", "media_interp_config", "media_interp_stream",
          "_interp_fps_cache", "INTERP_SEG_NAME", "media_interp_segment", "_interp_seg_fetch_raw",
          "_interp_seg_fetch_wait",
          "INTERP_MAX_RATIO", "INTERP_MIN_RATIO", "INTERP_OUT_FPS", "interp_output_rate"):
    load(n)
app = ns["app"]
ns["CONFIG"] = {}
ns["DEFAULT_ROOT_ID"] = "downloads"
user = {"username": "alice", "role": "admin"}
ns["get_authenticated_user"] = lambda: user
ns["root_visible_to"] = lambda u, root: True
ns["vision_worker_address"] = lambda: ("http://worker:8410", "tok")
video = tempfile.NamedTemporaryFile(suffix=".webm", delete=False)
video.write(b"x" * 12345)
video.close()
ns["resolve_media_path"] = lambda root, rel: video.name
ns["classify_media_path"] = lambda p: "video"
FPS = {"value": 60}
ns["probe_media_details"] = lambda p: {"fps": FPS["value"]}


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


calls = []
worker = {"available": True, "cached": False, "stream_error": None}


def fake_urlopen(req, timeout=None):
    url = req.full_url
    calls.append((req.get_method(), url, dict(req.header_items())))
    if worker.get("down"):
        raise urllib.error.URLError("connection refused")
    if "/v1/interp/status" in url:
        body = {"available": worker["available"], "missing": [] if worker["available"] else ["lsfg-vk.dll"]}
        if "key=" in url:
            body["cached"] = worker["cached"]
        return FakeResponse(json.dumps(body).encode())
    if "/v1/interp/source/" in url:
        data = req.data.read()
        worker["uploaded"] = len(data)
        worker["cached"] = True
        return FakeResponse(b'{"bytes": %d}' % len(data))
    if "/v1/interp/stream/" in url:
        if worker["stream_error"]:
            code, body = worker["stream_error"]
            raise urllib.error.HTTPError(url, code, "err", {}, io.BytesIO(json.dumps(body).encode()))
        return FakeResponse(b"\x00\x00\x00\x18ftypmp42" + b"F" * 200000)
    if "/v1/interp/seg/" in url:
        if worker.get("seg_error"):
            code, body = worker["seg_error"]
            if worker.get("seg_error_once"):
                worker["seg_error"] = None
            raise urllib.error.HTTPError(url, code, "err", {}, io.BytesIO(json.dumps(body).encode()))
        r = FakeResponse(b'{"segments": 7}' if ".json" in url else b"\x00\x00\x00\x10moofSEGDATA")
        r.headers = {"Content-Type": "application/json" if ".json" in url else "video/mp4"}
        return r
    raise AssertionError("unexpected url " + url)


ns["urllib"].request.urlopen = fake_urlopen

# ---- config
with app.test_request_context("/api/media/interp/config"):
    r = ns["media_interp_config"]()
    d = r.get_json()
check("config: available with bitrates and default 20", d["available"] and d["default_mbps"] == 20
      and d["bitrates_mbps"] == [5, 10, 20, 40, 80, 0], d)
ns["_interp_worker"].update(at=0.0, status=None)
worker["down"] = True
with app.test_request_context("/api/media/interp/config"):
    d = ns["media_interp_config"]().get_json()
check("config: worker unreachable -> not available, says why", not d["available"] and "cannot be reached" in d["reason"], d)
worker["down"] = False
ns["_interp_worker"].update(at=0.0, status=None)
worker["available"] = False
with app.test_request_context("/api/media/interp/config"):
    d = ns["media_interp_config"]().get_json()
check("config: not installed -> names what is missing", "lsfg-vk.dll" in d["reason"], d)
worker["available"] = True
ns["_interp_worker"].update(at=0.0, status=None)

# ---- stream
ns["get_authenticated_user"] = lambda: None
with app.test_request_context("/api/media/interp/stream?path=a.webm&t=0&m=2&mbps=20"):
    r = ns["media_interp_stream"]()
check("stream: 401 without a login", (r[1] if isinstance(r, tuple) else r.status_code) == 401)
ns["get_authenticated_user"] = lambda: user

calls.clear()
with app.test_request_context("/api/media/interp/stream?root=downloads&path=a.webm&t=12.5&m=6&mbps=20"):
    r = ns["media_interp_stream"]()
    body = b"".join(r.response)
stream_calls = [c for c in calls if "/v1/interp/stream/" in c[1]]
check("stream: uploaded once with the whole file", worker.get("uploaded") == 12345, worker)
check("stream: 60 fps x6 capped to x4 by max_output_fps 240", stream_calls and "mult=4" in stream_calls[0][1], stream_calls)
check("stream: start and cap passed on", stream_calls and "start=12.500" in stream_calls[0][1] and "mbps=20" in stream_calls[0][1])
check("stream: token sent to the worker", stream_calls and stream_calls[0][2].get("Authorization") == "Bearer tok")
check("stream: body relayed as video/mp4", r.mimetype == "video/mp4" and len(body) == 200012 and body[4:8] == b"ftyp", (r.mimetype, len(body), body[:12]))
check("stream: X-Interp-Fps header (60 x4)", r.headers.get("X-Interp-Fps") == "240", dict(r.headers))

# ---- exact output rates (?o=): the screen's rate, whatever the ratio
def stream_rate(qs, fps=60):
    FPS["value"] = fps
    calls.clear()
    with app.test_request_context("/api/media/interp/stream?path=a.webm&t=0&mbps=20&" + qs):
        r = ns["media_interp_stream"]()
        if not isinstance(r, tuple):
            b"".join(r.response)
    FPS["value"] = 60
    sc = [c[1] for c in calls if "/v1/interp/stream/" in c[1]]
    return r, (sc[0] if sc else "")

r, url = stream_rate("o=144")
check("stream o=144 on 60 fps: fps=144 to the worker, no mult", "&fps=144&" in url and "mult=" not in url, url)
check("stream o=144: X-Interp-Fps 144", r.headers.get("X-Interp-Fps") == "144", dict(r.headers))
r, url = stream_rate("o=120", fps=25)
check("stream o=120 on 25 fps: fps=120 (x4.8)", "&fps=120&" in url, url)
r, url = stream_rate("o=100", fps=25)
check("stream o=100 on 25 fps: a whole 4x goes as mult=4", "&mult=4&" in url, url)
# probe_media_details rounds to 2 decimals: 24000/1001 is 23.98, 30000/1001 29.97.
r, url = stream_rate("o=119.88", fps=23.98)
check("stream o=119.88 on 23.98 fps: decimals kept", "&fps=119.88&" in url, url)
r, url = stream_rate("o=59.94", fps=29.97)
check("stream o=59.94 on 29.97 fps: a whole 2x goes as mult=2", "&mult=2&" in url, url)
r, url = stream_rate("o=abc")
check("stream o=abc -> 400", isinstance(r, tuple) and r[1] == 400, r)
r, url = stream_rate("o=nan")
check("stream o=nan -> 400", isinstance(r, tuple) and r[1] == 400, r)
r, url = stream_rate("o=60", fps=59.94)
check("stream o=60 on 59.94 fps -> 400 (x1.001 is not worth it)", isinstance(r, tuple) and r[1] == 400
      and "highest frame rate" in r[0].get_json()["error"], r)

calls.clear()
with app.test_request_context("/api/media/interp/stream?path=a.webm&t=0&m=2&mbps=0"):
    r = ns["media_interp_stream"]()
    b"".join(r.response)
check("stream: second request does not upload again", not any("/v1/interp/source/" in c[1] for c in calls), calls)
check("stream: Max = mbps 0", any("mbps=0" in c[1] for c in calls if "/v1/interp/stream/" in c[1]))

calls.clear()
with app.test_request_context("/api/media/interp/stream?path=a.webm&t=3&m=2&mbps=20&loop=1"):
    r = ns["media_interp_stream"]()
    b"".join(r.response)
check("stream: loop=1 passed to the worker", any("loop=1" in c[1] for c in calls if "/v1/interp/stream/" in c[1]), calls)
calls.clear()
with app.test_request_context("/api/media/interp/stream?path=a.webm&t=3&m=2&mbps=20"):
    r = ns["media_interp_stream"]()
    b"".join(r.response)
check("stream: no loop param -> loop=0", any("loop=0" in c[1] for c in calls if "/v1/interp/stream/" in c[1]), calls)

FPS["value"] = 144
with app.test_request_context("/api/media/interp/stream?path=a.webm&t=0&m=2&mbps=20"):
    r = ns["media_interp_stream"]()
check("stream: 144 fps video -> 400 (240 cap leaves no 2x)", isinstance(r, tuple) and r[1] == 400, r)
FPS["value"] = 60

worker["stream_error"] = (503, {"error": "the GPU is busy with another interpolated stream", "busy": True})
with app.test_request_context("/api/media/interp/stream?path=a.webm&t=0&m=2&mbps=20"):
    r = ns["media_interp_stream"]()
check("stream: worker busy -> 503 with busy flag", isinstance(r, tuple) and r[1] == 503 and r[0].get_json()["busy"], r)
worker["stream_error"] = (500, {"error": "boom"})
with app.test_request_context("/api/media/interp/stream?path=a.webm&t=0&m=2&mbps=20"):
    r = ns["media_interp_stream"]()
check("stream: worker error -> 502 with its message", isinstance(r, tuple) and r[1] == 502
      and "boom" in r[0].get_json()["error"], r)
worker["stream_error"] = None

# ---- segment cache route
worker.update(cached=False, uploaded=None)
calls.clear()
with app.test_request_context("/api/media/interp/seg?path=a.webm&m=6&mbps=20&name=meta.json&from=3"):
    r = ns["media_interp_segment"]()
seg_calls = [c for c in calls if "/v1/interp/seg/" in c[1]]
check("seg: meta.json uploads the source first", worker.get("uploaded") == 12345, worker)
check("seg: variant m4 (60 fps x6 capped by 240), h2160, r20", seg_calls
      and "/m4_h2160_r20/meta.json" in seg_calls[0][1], seg_calls)
check("seg: meta.json passes ?from=", seg_calls and seg_calls[0][1].endswith("?from=3"), seg_calls)
check("seg: meta.json not cached by the browser", r.headers.get("Cache-Control") == "no-store")
calls.clear()
with app.test_request_context("/api/media/interp/seg?path=a.webm&m=2&mbps=0&name=seg_00042.m4s"):
    r = ns["media_interp_segment"]()
check("seg: segment relayed as video/mp4, cacheable",
      r.mimetype == "video/mp4" and b"moof" in r.get_data() and "max-age" in r.headers.get("Cache-Control", ""))
check("seg: Max cap is r0, no from= on segments",
      any(c[1].endswith("/m2_h2160_r0/seg_00042.m4s") for c in calls), calls)
check("seg: a segment request does not re-check the upload",
      not any("/v1/interp/status" in c[1] or "/source/" in c[1] for c in calls), calls)

def seg_variant(qs, fps=60):
    FPS["value"] = fps
    ns["_interp_fps_cache"].clear()
    calls.clear()
    with app.test_request_context("/api/media/interp/seg?path=a.webm&mbps=20&name=seg_00003.m4s&" + qs):
        r = ns["media_interp_segment"]()
    FPS["value"] = 60
    ns["_interp_fps_cache"].clear()
    sc = [c[1] for c in calls if "/v1/interp/seg/" in c[1]]
    return r, (sc[0].split("/")[-2] if sc else None)

for qs, fps, want, why in (
        ("o=120", 60, "m2_h2160_r20", "a whole 2x keeps the old cache name"),
        ("o=144", 60, "f144_h2160_r20", "x2.4"),
        ("o=120", 25, "f120_h2160_r20", "x4.8"),
        ("o=100", 25, "m4_h2160_r20", "a whole 4x keeps the old cache name"),
        ("o=300", 60, "m4_h2160_r20", "capped by max_output_fps 240 = 4x"),
        ("o=100", 23.98, "f100_h2160_r20", "x4.17"),
        ("o=120", 23.98, "f120_h2160_r20", "x5.004, not a whole 5x"),
        ("o=82.5", 25, "f82.5_h2160_r20", "a half-rate screen rate"),
        ("o=240", 10, "f160_h2160_r20", "capped at 16x the video"),
        ("o=120", 10, "f120_h2160_r20", "x12, not a whole 2-8"),
        ("m=3", 25, "m3_h2160_r20", "an old player's ?m= still works")):
    r, v = seg_variant(qs, fps)
    check(f"seg {qs} on {fps} fps -> {want} ({why})", v == want, (v, r))
for qs, fps in (("o=144", 144), ("o=60", 59.94), ("o=5", 4), ("o=x", 60)):
    r, v = seg_variant(qs, fps)
    check(f"seg {qs} on {fps} fps -> 400", isinstance(r, tuple) and r[1] == 400 and v is None, (v, r))

worker.update(seg_error=(404, {"error": "source not uploaded", "upload": True}), seg_error_once=True,
              cached=False, uploaded=None)
with app.test_request_context("/api/media/interp/seg?path=a.webm&m=2&mbps=20&name=seg_00001.m4s"):
    r = ns["media_interp_segment"]()
check("seg: worker evicted the source -> uploaded again and served",
      worker.get("uploaded") == 12345 and r.mimetype == "video/mp4", (worker, r))
worker.update(seg_error=(503, {"error": "still being made", "pending": True}), seg_error_once=False)
with app.test_request_context("/api/media/interp/seg?path=a.webm&m=2&mbps=20&name=seg_00002.m4s"):
    r = ns["media_interp_segment"]()
check("seg: still being made -> 503 pending with Retry-After",
      isinstance(r, tuple) and r[1] == 503 and r[0].get_json()["pending"] and r[2].get("Retry-After") == "1", r)
worker.update(seg_error=None)
with app.test_request_context("/api/media/interp/seg?path=a.webm&m=2&mbps=20&name=../../etc/passwd"):
    r = ns["media_interp_segment"]()
check("seg: file names other than the cache's refused", isinstance(r, tuple) and r[1] == 400, r)
ns["get_authenticated_user"] = lambda: None
with app.test_request_context("/api/media/interp/seg?path=a.webm&m=2&mbps=20&name=init.mp4"):
    r = ns["media_interp_segment"]()
check("seg: 401 without a login", (r[1] if isinstance(r, tuple) else r.status_code) == 401)
ns["get_authenticated_user"] = lambda: user

ns["interp_settings"] = lambda: {**ns["DEFAULT_CONFIG"]["interpolation"], "enabled": False}
with app.test_request_context("/api/media/interp/stream?path=a.webm&t=0&m=2&mbps=20"):
    r = ns["media_interp_stream"]()
check("stream: disabled on the server -> 403", isinstance(r, tuple) and r[1] == 403, r)

os.unlink(video.name)
print("ALL PASS" if all(RESULTS) else f"{RESULTS.count(False)} FAILED")
