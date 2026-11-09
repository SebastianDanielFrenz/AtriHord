"""2D -> 3D conversion (stereo3d.py + the /api/media/3d/* routes, the stereo3d
settings and the config merge of a STAGED server.py), against a FAKE vision worker
(a real HTTP server on localhost speaking /v1/stereo/*, vision_worker.py's contract)
and a temp cache folder. The server's functions come out of its AST like in every
other harness; stereo3d.py is imported from next to the server file.

  venv/bin/python stereo3d_check.py /tmp/.../server.py     (via writeguard.py in run_all.sh)

Nothing under /data2 is written: the queue file, the cache and the "library" are temp dirs.
"""
import ast
import hashlib
import http.server
import json
import os
import shutil
import socketserver
import sys
import tempfile
import threading
import time
import urllib.parse

BASE = os.environ.get("ATRIHORD_DIR") or os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SERVER = os.path.abspath(sys.argv[1])
sys.path.insert(0, BASE)
sys.path.insert(0, os.path.dirname(SERVER))      # stereo3d.py of the stage (or the repo) first

import stereo3d  # noqa: E402

TMP = tempfile.mkdtemp(prefix="stereo3d_check_")
LIB = os.path.join(TMP, "lib")
os.makedirs(LIB)
RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append(bool(ok))
    print(("PASS " if ok else "FAIL ") + name + (f" - {detail}" if detail and not ok else ""))


# ------------------------------------------------------------------ the fake worker
class FakeWorker:
    """vision_worker.py's /v1/stereo/* as far as media-dl uses it. Jobs only move when the test says so."""

    def __init__(self):
        self.jobs = {}
        self.log = []                  # (method, route, key)
        self.available = True
        self.disk_low = False
        self.payload_size = 70000
        self.sbs_head = b"\x00\x00\x00\x18ftypmp42"
        fw = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def send(self, code, body=None, raw=None, headers=None):
                data = raw if raw is not None else json.dumps(body if body is not None else {}).encode()
                self.send_response(code)
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Content-Type", "application/octet-stream" if raw is not None else "application/json")
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(data)

            def body(self):
                n = int(self.headers.get("Content-Length") or 0)
                return self.rfile.read(n) if n else b""

            def route(self):
                u = urllib.parse.urlparse(self.path)
                return u.path, urllib.parse.parse_qs(u.query)

            def do_GET(self):
                path, qs = self.route()
                if path == "/v1/stereo/status":
                    fw.log.append(("GET", "status", None))
                    return self.send(200, dict({"available": fw.available, "missing": [] if fw.available else ["stereo venv python"]},
                                               **({"max_upload_bytes": fw.max_upload} if fw.max_upload else {})))
                parts = path.split("/")
                if len(parts) >= 5 and parts[3] == "jobs":
                    key = parts[4]
                    job = fw.jobs.get(key)
                    if len(parts) == 5:
                        fw.log.append(("GET", "job", key))
                        if job is None:
                            return self.send(404, {"error": "no such job"})
                        return self.send(200, fw.view(job))
                    if parts[5] == "result":
                        name = "/".join(parts[6:])
                        fw.log.append(("GET", "result", key))
                        if job is None or job["state"] != "done" or name not in job["payloads"]:
                            return self.send(404, {"error": "no such result"})
                        data = job["payloads"][name]
                        rng = self.headers.get("Range")
                        if rng:
                            start = int(rng.split("=")[1].split("-")[0])
                            fw.log.append(("GET", "result-range", key))
                            return self.send(206, raw=data[start:], headers={
                                "Content-Range": "bytes %d-%d/%d" % (start, len(data) - 1, len(data))})
                        if fw.cut_downloads.get(key):
                            # Announce the whole file, send half, drop the connection: a pull interrupted.
                            fw.cut_downloads[key] = 0
                            self.send_response(200)
                            self.send_header("Content-Length", str(len(data)))
                            self.end_headers()
                            self.wfile.write(data[: len(data) // 2])
                            self.wfile.flush()
                            self.close_connection = True
                            return
                        return self.send(200, raw=data)
                self.send(404, {"error": "unknown route"})

            def do_PUT(self):
                path, qs = self.route()
                parts = path.split("/")
                key = parts[4]
                if key in fw.reset_puts:
                    # waitress over its max_request_body_size: no answer the client can read, the
                    # connection is dropped while the body is still coming.
                    fw.log.append(("PUT", "reset", key))
                    self.close_connection = True
                    return
                data = self.body()
                if fw.disk_low:
                    return self.send(507, {"error": "not enough free space"})
                job = fw.jobs.get(key)
                if job is not None and job["state"] != "uploaded":
                    return self.send(409, {"error": "already submitted"})
                if job is None:
                    job = fw.jobs[key] = {"key": key, "state": "uploaded", "puts": [], "payloads": {}, "names": []}
                name = (qs.get("name") or [None])[0]
                job["puts"].append(name or ("src." + (qs.get("ext") or ["bin"])[0]))
                job["sizes"] = job.get("sizes", []) + [len(data)]
                if name:
                    job["names"].append(name)
                fw.log.append(("PUT", "source", key))
                return self.send(200, {"key": key, "bytes": len(data), "state": "uploaded"})

            def do_POST(self):
                path, qs = self.route()
                body = json.loads(self.body() or b"{}")
                if path == "/v1/stereo/jobs":
                    key = body.get("key")
                    fw.log.append(("POST", "jobs", key))
                    job = fw.jobs.get(key)
                    if job is None:
                        return self.send(404, {"error": "source not uploaded", "upload": True})
                    fw.posted.append(body)
                    if job["state"] == "uploaded":
                        job.update(state="waiting_gpu", kind=body.get("kind"), profile=body.get("profile"), label=body.get("label"))
                        return self.send(201, fw.view(job))
                    if job["state"] in ("held", "failed"):
                        job["state"] = "waiting_gpu"
                    return self.send(200, fw.view(job))
                parts = path.split("/")
                key = parts[4]
                job = fw.jobs.get(key)
                action = parts[5] if len(parts) > 5 else ""
                fw.log.append(("POST", action, key))
                if job is None:
                    return self.send(404, {"error": "no such job"})
                if action == "hold":
                    if any(j["state"] == "held" for k, j in fw.jobs.items() if k != key):
                        return self.send(409, {"error": "another job is already held"})
                    if job["state"] not in ("waiting_gpu", "converting"):
                        return self.send(409, {"error": "cannot be held"})
                    job["state"] = "held"
                    return self.send(200, fw.view(job))
                if action == "resume":
                    if job["state"] != "held":
                        return self.send(409, {"error": "not held"})
                    job["state"] = "waiting_gpu"
                    return self.send(200, fw.view(job))
                self.send(404, {})

            def do_DELETE(self):
                path, _ = self.route()
                key = path.split("/")[4]
                fw.log.append(("DELETE", "job", key))
                existed = fw.jobs.pop(key, None) is not None
                return self.send(200, {"key": key, "deleted": existed})

        class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
            daemon_threads = True

        self.posted = []
        self.cut_downloads = {}
        self.max_upload = None         # max_upload_bytes in the status (None: an older worker that does not say)
        self.reset_puts = set()        # keys whose upload is cut off
        self.server = Server(("127.0.0.1", 0), Handler)
        self.url = "http://127.0.0.1:%d" % self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def view(self, job):
        v = {"key": job["key"], "state": job["state"], "kind": job.get("kind"), "error": job.get("error"),
             "progress": job.get("progress"), "files": len(job["names"]) or None}
        if job["state"] == "done":
            v["results"] = [{"name": n, "bytes": len(b)} for n, b in job["payloads"].items()]
        return v

    def count(self, method, route, key=None):
        return sum(1 for m, r, k in self.log if m == method and r == route and (key is None or k == key))

    def set_state(self, key, state, **kw):
        self.jobs[key].update(state=state, **kw)

    def finish(self, key):
        """The driver's outputs, as vision_worker.stereo_collect lists them."""
        job = self.jobs[key]
        n = self.payload_size
        if job.get("kind") == "video":
            meta = {"eye_w": 1920, "eye_h": 1080, "fps": 120.0, "duration": 12.5, "frames": 1500, "source_fps": 30.0,
                    "interpolated": True, "gpu_seconds": 77.0}
            job["payloads"] = {"sbs.mp4": b"\x00\x00\x00\x18ftypmp42" + bytes([7]) * n, "depth.mp4": bytes([9]) * (n // 2),
                               "meta.json": json.dumps(meta).encode()}
        else:
            payloads = {}
            for name in job["names"]:
                stem = os.path.splitext(name)[0]
                payloads[stem + "/sbs.jpg"] = b"\xff\xd8" + bytes([5]) * n
                payloads[stem + "/depth.png"] = b"\x89PNG" + bytes([3]) * (n // 2)
                payloads[stem + "/meta.json"] = json.dumps({"eye_w": 800, "eye_h": 600}).encode()
            job["payloads"] = payloads
        job["state"] = "done"


FW = FakeWorker()
DEAD = "http://127.0.0.1:1"            # nothing listens: connection refused at once
ADDR = {"url": FW.url}

# ------------------------------------------------------------------ the service under test
CLOCK = [1_000_000.0]
CACHE = os.path.join(TMP, "cache")
os.makedirs(CACHE)
QUEUE_FILE = os.path.join(TMP, "stereo3d_queue.json")
FAVS = set()
CFG = {"enabled": True, "cache_dir": CACHE, "cache_max_gb": 150, "max_eye_long_side": 2160, "divergence": 2.0,
       "convergence": 0.5, "mbps_1080": 30, "mbps_1440": 50, "mbps_2160": 80, "favorites_auto": True,
       "favorites_backfill_days": 7, "max_attempts": 2}


def resolve(root, path):
    if root not in ("downloads", "jellyfin"):
        return None
    base = os.path.abspath(os.path.join(LIB, root))
    full = os.path.abspath(os.path.join(base, path.strip("/").replace("\\", "/")))
    return full if full == base or full.startswith(base + os.sep) else None      # the traversal guard of resolve_media_path


def make_service(queue_file=QUEUE_FILE):
    return stereo3d.Service(queue_file, lambda: dict(CFG), resolve, lambda: (ADDR["url"], "tok"),
                            lambda root, path: (root, path) in FAVS, forbidden_dirs=(BASE, "/data2"),
                            clock=lambda: CLOCK[0])


def media(root, rel, size=1000, mtime=None):
    full = os.path.join(LIB, root, rel)
    os.makedirs(os.path.dirname(full), exist_ok=True)
    with open(full, "wb") as fh:
        fh.write(hashlib.sha256(rel.encode()).digest() * (size // 32 + 1))
        fh.truncate(size)
    if mtime:
        os.utime(full, (mtime, mtime))
    return full


def drive(svc, n=6):
    out = []
    for _ in range(n):
        out.append(svc.step())
    return out


def reset_worker():
    FW.jobs.clear()
    FW.log.clear()
    FW.posted.clear()
    FW.disk_low = False
    FW.available = True
    FW.max_upload = None
    FW.reset_puts.clear()
    ADDR["url"] = FW.url


def fresh(queue_name="q.json"):
    reset_worker()
    shutil.rmtree(CACHE, ignore_errors=True)
    os.makedirs(CACHE)
    CLOCK[0] += 10
    FAVS.clear()
    CFG.update(enabled=True, cache_dir=CACHE, cache_max_gb=150, max_attempts=2)
    path = os.path.join(TMP, queue_name)
    if os.path.exists(path):
        os.remove(path)
    return make_service(path), path


# ================================================================== keys, profile, files
media("downloads", "k/a.mp4", 5000, 1_600_000_000)
v1 = stereo3d.file_version(os.path.join(LIB, "downloads/k/a.mp4"))
_, pid = stereo3d.profile_for("video", CFG)
key1 = stereo3d.item_key(v1, pid)
os.rename(os.path.join(LIB, "downloads/k/a.mp4"), os.path.join(LIB, "downloads/k/renamed.mp4"))
v2 = stereo3d.file_version(os.path.join(LIB, "downloads/k/renamed.mp4"))
check("key: stable across a rename (same inode)", stereo3d.item_key(v2, pid) == key1 and len(key1) == 32)
check("key: changes with size", stereo3d.item_key(dict(v2, size=v2["size"] + 1), pid) != key1)
check("key: changes with mtime", stereo3d.item_key(dict(v2, mtime_ns=v2["mtime_ns"] + 1), pid) != key1)
check("key: changes with the inode", stereo3d.item_key(dict(v2, ino=v2["ino"] + 1), pid) != key1)
_, pid2 = stereo3d.profile_for("video", dict(CFG, divergence=2.5))
check("key: changes with the profile (divergence)", stereo3d.item_key(v2, pid2) != key1)
_, pid3 = stereo3d.profile_for("video", dict(CFG, max_eye_long_side=1440))
check("key: changes with the long-side cap (videos)", stereo3d.item_key(v2, pid3) != key1)
_, ppid = stereo3d.profile_for("picture", CFG)
_, ppid_cap = stereo3d.profile_for("picture", dict(CFG, max_eye_long_side=1440))
check("profile: a picture's id ignores the video cap (pictures are never capped)", ppid == ppid_cap)
_, ppid_div = stereo3d.profile_for("picture", dict(CFG, divergence=3))
check("profile: a picture's id follows the divergence", ppid != ppid_div)
check("profile ids differ between video and picture", pid != ppid)

# The profile must be what stereo_job.py --profile accepts: only keys of its DEFAULT_PROFILE
# (copied from the deployed stereo_job.py 8037f70d), and the worker's value rules.
DRIVER_KEYS = {"depth_model", "picture_model", "method", "divergence", "convergence", "fps", "max_eye_long_side",
               "chunk_s", "cq", "nvenc_preset", "bitrate_anchors", "interp_quality", "depth_cq",
               "depth_maxrate_mbps", "depth_scale", "picture_jpeg_quality", "turn_cw"}
WORD = __import__("re").compile(r"^[A-Za-z0-9_.-]{1,40}$")


def profile_ok(p):
    if not set(p) <= DRIVER_KEYS:
        return "unknown keys %s" % sorted(set(p) - DRIVER_KEYS)
    for k, v in p.items():
        if isinstance(v, str):
            if not WORD.match(v):
                return "bad word " + k
        elif isinstance(v, bool) or not isinstance(v, (int, float, list)):
            return "bad type " + k
        elif isinstance(v, list) and not all(isinstance(r, list) and len(r) == 2 and all(
                isinstance(x, (int, float)) and not isinstance(x, bool) for x in r) for r in v):
            return "bad list " + k
    return None


vp, _ = stereo3d.profile_for("video", CFG)
pp, _ = stereo3d.profile_for("picture", CFG)
check("profile: video keys/values accepted by the worker's rules", profile_ok(vp) is None, profile_ok(vp))
check("profile: picture keys/values accepted by the worker's rules", profile_ok(pp) is None, profile_ok(pp))
check("profile: video says Any_V2_S row_flow_v3 120 fps, cap 2160, anchors 30/50/80",
      vp["depth_model"] == "Any_V2_S" and vp["method"] == "row_flow_v3" and vp["fps"] == 120
      and vp["max_eye_long_side"] == 2160 and [a[1] for a in vp["bitrate_anchors"]] == [30.0, 50.0, 80.0]
      and [a[0] for a in vp["bitrate_anchors"]] == [1920 * 1080, 2560 * 1440, 3840 * 2160], vp)
check("profile: picture says Distill_Any_L and carries no resolution cap",
      pp["picture_model"] == "Distill_Any_L" and "max_eye_long_side" not in pp, pp)
check("kinds: videos, pictures, rest", stereo3d.kind_of("a/b.MKV") == "video" and stereo3d.kind_of("x.jfif") == "picture"
      and stereo3d.kind_of("s.mp3") is None and stereo3d.kind_of("a.gif") is None and stereo3d.kind_of("z.mka") is None)
check("worker extension: jfif goes as jpg", stereo3d.worker_ext("x.JFIF") == "jpg" and stereo3d.worker_ext("a.mp4") == "mp4")

# ================================================================== queue: order, duplicates, reload
svc, qpath = fresh()
for rel in ("m1.mp4", "m2.mp4", "m3.mp4", "f_old.mp4", "f_new.mp4", "m_pic.jpg"):
    media("downloads", rel, 2000 + len(rel))
r = svc.request("downloads", "f_old.mp4", stereo3d.LANE_FAVORITE, "alice", fav_at=100)
r2 = svc.request("downloads", "f_new.mp4", stereo3d.LANE_FAVORITE, "alice", fav_at=200)
m = [svc.request("downloads", p, by="alice")["status"] for p in ("m2.mp4", "m1.mp4", "m_pic.jpg", "m3.mp4")]
order = [i["path"] for i in svc.queue.ordered()]
check("queue: manual first in selection order, then favourites newest first",
      order == ["m2.mp4", "m1.mp4", "m_pic.jpg", "m3.mp4", "f_new.mp4", "f_old.mp4"], order)
check("queue: statuses queued", r["status"] == "queued" and m == ["queued"] * 4, (r, m))
d1 = svc.request("downloads", "m1.mp4", by="bob")
os.link(os.path.join(LIB, "downloads/m1.mp4"), os.path.join(LIB, "downloads/m1_link.mp4"))
d2 = svc.request("downloads", "m1_link.mp4", by="bob")
check("queue: a duplicate request is collapsed", d1["status"] == "duplicate" and d2["status"] == "duplicate"
      and len(svc.queue.items) == 6, (d1, d2, len(svc.queue.items)))
p = svc.request("downloads", "f_old.mp4", by="alice")
order = [i["path"] for i in svc.queue.ordered()]
check("queue: a manual request for a waiting favourite moves it to the manual lane (at the end)",
      p["status"] == "promoted" and order[:5] == ["m2.mp4", "m1.mp4", "m_pic.jpg", "m3.mp4", "f_old.mp4"]
      and order[5] == "f_new.mp4", (p, order))
reloaded = stereo3d.Queue(qpath)
check("queue: order survives a reload from stereo3d_queue.json", [i["path"] for i in reloaded.ordered()] == order)
check("queue: file is valid JSON written atomically (no .tmp left)", not os.path.exists(qpath + ".tmp")
      and json.load(open(qpath))["version"] == 1)
fav_dup = svc.request("downloads", "f_new.mp4", stereo3d.LANE_FAVORITE, "alice")
check("queue: a favourite request for a queued favourite is a duplicate", fav_dup["status"] == "duplicate")
sk = svc.request("downloads", "song.mp3")
sk2 = svc.request("downloads", "gone.mp4")
sk3 = svc.request("nowhere", "m1.mp4")
check("request: audio / missing file / unknown root are skipped with a reason",
      all(x["status"] == "skipped" and x["reason"] for x in (sk, sk2, sk3)), (sk, sk2, sk3))
CFG["enabled"] = False
off = svc.request("downloads", "m1.mp4")
CFG["enabled"] = True
check("request: skipped while 3D conversion is off", off["status"] == "skipped")

# ================================================================== a video from request to cache
svc, qpath = fresh()
media("downloads", "vid/one.mp4", 30000)
svc.request("downloads", "vid/one.mp4", by="alice")
key = next(iter(svc.queue.items))
res = drive(svc, 1)
check("dispatch: first step uploads the source and submits the job", res == ["submitted"]
      and FW.count("PUT", "source", key) == 1 and FW.count("POST", "jobs", key) == 1, (res, FW.log))
check("dispatch: PUT carried the whole file with ?ext=", FW.jobs[key]["sizes"] == [30000] and FW.jobs[key]["puts"] == ["src.mp4"])
posted = FW.posted[0]
check("dispatch: the POST carries kind, label and the profile",
      posted["kind"] == "video" and posted["label"] == "downloads/vid/one.mp4" and profile_ok(posted["profile"]) is None
      and posted["profile"]["max_eye_long_side"] == 2160, posted)
check("dispatch: item is submitted with its worker key", svc.queue.items[key]["state"] == "submitted"
      and svc.queue.items[key]["worker_key"] == key)
FW.set_state(key, "converting", progress={"phase": "converting", "chunk": 2, "chunks": 5, "eta_s": 99})
drive(svc, 3)
check("dispatch: polling does not upload or submit again", FW.count("PUT", "source", key) == 1
      and FW.count("POST", "jobs", key) == 1)
st = svc.status_for("downloads", "vid/one.mp4")
check("status: converting shows phase, progress and ETA", st["state"] == "converting" and st["phase"] == "converting"
      and st["eta_s"] == 99 and st["progress"]["chunk"] == 2, st)
FW.finish(key)
res = drive(svc, 1)
idx = svc.index()
entry = idx.get(key)
base = os.path.join(CACHE, "v", key[:2], key)
check("done: pulled into v/<k2>/<key>/ with all three files", all(os.path.getsize(os.path.join(base, n)) > 0
      for n in ("sbs.mp4", "depth.mp4", "meta.json")) and open(os.path.join(base, "sbs.mp4"), "rb").read()[4:8] == b"ftyp", os.listdir(base) if os.path.isdir(base) else "no dir")
check("done: sizes equal the worker's", os.path.getsize(os.path.join(base, "sbs.mp4")) == len(FW.sbs_head) + FW.payload_size
      and os.path.getsize(os.path.join(base, "depth.mp4")) == FW.payload_size // 2)
check("done: index entry carries meta and bookkeeping", entry and entry["kind"] == "video" and entry["root"] == "downloads"
      and entry["path"] == "vid/one.mp4" and entry["eye_w"] == 1920 and entry["eye_h"] == 1080 and entry["fps"] == 120.0
      and entry["frames"] == 1500 and entry["bytes"] == entry["sbs_bytes"] + entry["depth_bytes"] + os.path.getsize(os.path.join(base, "meta.json"))
      and entry["watch_count"] == 0 and entry["last_watched"] == CLOCK[0] and entry["profile"].startswith("v1:"), entry)
check("done: index.json on disk", key in json.load(open(os.path.join(CACHE, "index.json"))))
check("done: worker job deleted, queue empty, partial folder gone", FW.count("DELETE", "job", key) == 1
      and not svc.queue.items and not os.path.exists(os.path.join(CACHE, ".partial", key)), FW.log)
st = svc.status_for("downloads", "vid/one.mp4")
check("status: done with urls", st["state"] == "done" and st["sbs_url"].endswith("name=sbs.mp4")
      and st["depth_url"].endswith("name=depth.mp4"), st)
again = svc.request("downloads", "vid/one.mp4")
check("request: a cached file answers cached (cache hit = done), nothing queued", again["status"] == "cached" and not svc.queue.items)
check("step: nothing queued -> idle, the worker is not even asked", svc.step() == "idle")

# ---- a queued item whose key got cached meanwhile is dropped at dispatch
svc.queue.items.clear()
media("downloads", "vid/one.mp4", 30000)            # same file again: not modified
with svc.queue.lock:
    svc.queue.items[key] = {"key": key, "root": "downloads", "path": "vid/one.mp4", "ext": "mp4", "kind": "video",
                            "lane": "manual", "seq": 1, "by": "x", "at": 1, "fav_at": 1, "version": v1, "profile_id": pid,
                            "attempts": 0, "state": "queued", "error": None, "worker_key": None, "worker_state": None,
                            "progress": None, "not_before": 0}
# the file was rewritten above (new mtime): the queued key is stale and gets re-keyed, not served from cache
FW.log.clear()
res = svc.step()
check("dispatch: a queue item whose file changed is re-keyed to the new version", key not in svc.queue.items
      and len(svc.queue.items) == 1 and FW.count("PUT", "source") == 1, (res, list(svc.queue.items)))
svc.clear_queue()
check("cancel: clear_queue deletes the worker's job too", not svc.queue.items and FW.count("DELETE", "job") >= 1)

# ---- queued item whose file vanished is dropped; a changed file is re-keyed
svc, qpath = fresh()
media("downloads", "x/vanish.mp4", 4000)
media("downloads", "x/change.mp4", 4000)
svc.request("downloads", "x/vanish.mp4")
svc.request("downloads", "x/change.mp4")
old_keys = set(svc.queue.items)
os.remove(os.path.join(LIB, "downloads/x/vanish.mp4"))
media("downloads", "x/change.mp4", 4100)
res = drive(svc, 3)
paths = {i["path"] for i in svc.queue.items.values()}
check("dispatch: vanished source dropped, changed one re-keyed (new key, same path)", paths == {"x/change.mp4"}
      and not (set(svc.queue.items) & old_keys), (paths, res))
check("dispatch: re-keyed item uploaded once", FW.count("PUT", "source") == 1)

# ================================================================== restart polls, never resubmits
svc, qpath = fresh()
media("downloads", "r/restart.mp4", 9000)
svc.request("downloads", "r/restart.mp4")
key = next(iter(svc.queue.items))
drive(svc, 1)
FW.set_state(key, "converting", progress={"phase": "converting"})
svc2 = make_service(qpath)                      # a restart: nothing but the queue file survives
FW.log.clear()
drive(svc2, 3)
check("restart: submitted item is polled, not uploaded or submitted again", FW.count("GET", "job", key) >= 1
      and FW.count("PUT", "source") == 0 and FW.count("POST", "jobs") == 0, FW.log)
check("restart: state kept", svc2.queue.items[key]["state"] == "submitted")
FW.finish(key)
drive(svc2, 1)
check("restart: finished job pulled by the restarted service", svc2.index().files_ok(key))

# a job interrupted halfway through its download resumes with a Range request
svc, qpath = fresh()
media("downloads", "r/cut.mp4", 9000)
svc.request("downloads", "r/cut.mp4")
key = next(iter(svc.queue.items))
drive(svc, 1)
FW.finish(key)
FW.cut_downloads[key] = 1
r1 = svc.step()
part = os.path.join(CACHE, ".partial", key, key)
partial_sizes = {n: os.path.getsize(os.path.join(part, n)) for n in os.listdir(part)} if os.path.isdir(part) else {}
check("download: an interrupted pull leaves its partial and backs off", r1 == "worker down" and partial_sizes.get("sbs.mp4", 0) > 0
      and not svc.index().files_ok(key) and svc.queue.items[key]["state"] == "downloading", (r1, partial_sizes))
CLOCK[0] += 40
FW.log.clear()
svc.step()
check("download: resumed with a Range request and completed byte for byte", FW.count("GET", "result-range") >= 1
      and svc.index().files_ok(key)
      and open(os.path.join(CACHE, "v", key[:2], key, "sbs.mp4"), "rb").read() == FW.sbs_head + bytes([7]) * FW.payload_size)

# ================================================================== one active job at a time
svc, qpath = fresh()
for n in range(3):
    media("downloads", "one/v%d.mp4" % n, 3000 + n)
    svc.request("downloads", "one/v%d.mp4" % n)
drive(svc, 6)
active = [j for j in FW.jobs.values() if j["state"] != "uploaded"]
check("one active job: three videos queued, the worker holds exactly one", len(FW.jobs) == 1 and len(active) == 1, len(FW.jobs))
check("one active job: the others wait in the queue", sorted(i["state"] for i in svc.queue.items.values()) == ["queued", "queued", "submitted"])
k1 = next(k for k, i in svc.queue.items.items() if i["state"] == "submitted")
FW.finish(k1)
drive(svc, 1)
check("one active job: when it is done the next one starts", len(FW.jobs) == 1 and k1 not in FW.jobs and FW.count("PUT", "source") == 2)

# ================================================================== retry, backoff, max attempts
svc, qpath = fresh()
CFG["max_attempts"] = 2
media("downloads", "f/fail.mp4", 3000)
svc.request("downloads", "f/fail.mp4")
key = next(iter(svc.queue.items))
drive(svc, 1)
FW.set_state(key, "failed", error="NVENC open failed")
res = drive(svc, 1)
it = svc.queue.items[key]
check("retry: a failed job -> queued again with attempts 1, its error, a 30 min wait",
      it["state"] == "queued" and it["attempts"] == 1 and "NVENC" in it["error"]
      and it["not_before"] == CLOCK[0] + stereo3d.FAILED_RETRY_S and FW.count("DELETE", "job", key) == 1, (res, it))
drive(svc, 2)
check("retry: not started during the wait", FW.count("PUT", "source") == 1)
CLOCK[0] += stereo3d.FAILED_RETRY_S + 1
drive(svc, 1)
check("retry: after the wait the item is uploaded and submitted again", FW.count("PUT", "source") == 2
      and svc.queue.items[key]["state"] == "submitted")
FW.set_state(key, "failed", error="second failure")
drive(svc, 1)
it = svc.queue.items[key]
check("retry: after max_attempts the item is failed, with Retry available", it["state"] == "failed" and it["attempts"] == 2
      and svc.status_for("downloads", "f/fail.mp4")["state"] == "failed")
check("retry: a failed item is not worked on and the dispatcher idles", svc.step() == "idle" and FW.count("PUT", "source") == 2)
n = svc.retry(key)
check("retry: Retry puts it back with attempts 0", n == 1 and svc.queue.items[key]["state"] == "queued"
      and svc.queue.items[key]["attempts"] == 0 and svc.queue.items[key]["error"] is None)
drive(svc, 1)
check("retry: and it goes to the worker again", FW.count("PUT", "source") == 3)
svc.cancel(key)

# the worker lost the job (restart wiped it, 72 h ttl): back to the queue without counting an attempt
svc, qpath = fresh()
media("downloads", "f/lost.mp4", 3000)
svc.request("downloads", "f/lost.mp4")
key = next(iter(svc.queue.items))
drive(svc, 1)
FW.jobs.clear()
res = svc.step()
it = svc.queue.items[key]
check("lost job: worker answers 404 -> queued again, no attempt counted", it["attempts"] == 0 and it["state"] in ("queued", "submitted")
      and res in ("lost", "submitted"), (res, it))

# ================================================================== uploads that break off, files too big
# 2026-10-29: a 9.4 GiB episode over the worker's 8 GiB body limit was answered with a dropped
# connection every 30 s for a day, and as the first item in line it blocked everything behind it.
svc, qpath = fresh()
media("downloads", "u/big.mp4", 30000)
media("downloads", "u/ok.mp4", 10000)
svc.request("downloads", "u/big.mp4")
svc.request("downloads", "u/ok.mp4")
kbig = next(k for k, i in svc.queue.items.items() if i["path"] == "u/big.mp4")
kok = next(k for k, i in svc.queue.items.items() if i["path"] == "u/ok.mp4")
FW.reset_puts.add(kbig)
r1 = svc.step()
it = svc.queue.items[kbig]
check("upload cut off: backs off like a worker that is down, the item is queued again with one break counted",
      r1 == "worker down" and it["state"] == "queued" and it.get("upload_fails") == 1 and it["attempts"] == 0
      and "upload interrupted" in (svc.worker_error or "") and FW.count("PUT", "reset", kbig) == 1, (r1, it, svc.worker_error))
CLOCK[0] += stereo3d.BACKOFF_MIN_S + 1
r2 = svc.step()
check("upload cut off: a second break is still a backoff", r2 == "worker down" and svc.queue.items[kbig].get("upload_fails") == 2, r2)
CLOCK[0] += stereo3d.BACKOFF_MIN_S + 1
r3 = svc.step()
it = svc.queue.items[kbig]
check("upload cut off %d times in a row: counts as a failed attempt, to the back for FAILED_RETRY_S, no backoff"
      % stereo3d.UPLOAD_FAILS_MAX,
      r3 == "upload failed" and it["state"] == "queued" and it["attempts"] == 1 and it.get("upload_fails") == 0
      and it["not_before"] == CLOCK[0] + stereo3d.FAILED_RETRY_S and "broke off 3 times" in (it["error"] or "")
      and svc.down_until <= CLOCK[0], (r3, it, svc.down_until))
r4 = svc.step()
check("upload cut off: the item behind it is uploaded and submitted next", r4 == "submitted"
      and FW.count("PUT", "source", kok) == 1 and svc.queue.items[kok]["state"] == "submitted", (r4, FW.log[-4:]))
check("upload cut off: the given-up item was not uploaded again meanwhile", FW.count("PUT", "reset", kbig) == 3)
svc.clear_queue()

# one break, then the upload goes through: no attempt counted, the count starts again
svc, qpath = fresh()
media("downloads", "u/blip.mp4", 12000)
svc.request("downloads", "u/blip.mp4")
kb = next(iter(svc.queue.items))
FW.reset_puts.add(kb)
svc.step()
FW.reset_puts.clear()
CLOCK[0] += stereo3d.BACKOFF_MIN_S + 1
r = svc.step()
it = svc.queue.items[kb]
check("upload cut off once, then through: submitted, no attempt, break count back to 0",
      r == "submitted" and it["state"] == "submitted" and it["attempts"] == 0 and it.get("upload_fails") == 0, (r, it))
svc.clear_queue()

# the worker says how big a file it takes: a bigger one fails at once, with no upload, and the next goes
svc, qpath = fresh()
FW.max_upload = 20000
media("downloads", "t/huge.mkv", 30000)
media("downloads", "t/fits.mp4", 10000)
svc.request("downloads", "t/huge.mkv")
svc.request("downloads", "t/fits.mp4")
kh = next(k for k, i in svc.queue.items.items() if i["path"] == "t/huge.mkv")
kf = next(k for k, i in svc.queue.items.items() if i["path"] == "t/fits.mp4")
r = svc.step()
it = svc.queue.items[kh]
check("too big: failed at once with the sizes in the error, never sent; the next item is submitted in the same step",
      r == "submitted" and it["state"] == "failed" and "too big for the vision worker" in (it["error"] or "")
      and "at most 0.0 GiB" in it["error"] and FW.count("PUT", "source", kh) == 0 and FW.count("PUT", "reset", kh) == 0
      and svc.queue.items[kf]["state"] == "submitted", (r, it))
check("too big: shows as failed for the file", svc.status_for("downloads", "t/huge.mkv")["state"] == "failed")
FW.finish(kf)
svc.step()
FW.max_upload = 100000                          # the worker's limit was raised
n = svc.retry(kh)
r = svc.step()
check("too big: Retry after the worker's limit went up uploads it", n == 1 and r == "submitted"
      and FW.count("PUT", "source", kh) == 1 and svc.queue.items[kh]["state"] == "submitted", (n, r))
svc.clear_queue()
FW.max_upload = None
media("downloads", "t/old.mkv", 30000)
svc.request("downloads", "t/old.mkv")
ko = next(iter(svc.queue.items))
r = svc.step()
check("too big: a worker that does not announce a limit gets the file as before", r == "submitted"
      and FW.count("PUT", "source", ko) == 1, (r, FW.log[-3:]))
svc.clear_queue()

# pictures: one over the limit is left out of the batch, the others go together
svc, qpath = fresh()
FW.max_upload = 5000
for name, size in (("p1.jpg", 3000), ("p2.jpg", 8000), ("p3.jpg", 3100)):
    media("downloads", "b/" + name, size)
    svc.request("downloads", "b/" + name)
kp = {i["path"]: k for k, i in svc.queue.items.items()}
r = svc.step()
batch = [j for j in FW.jobs.values() if j["names"]]
check("too big: a picture over the limit stays out of the batch, the two that fit go as one job",
      r == "submitted" and len(batch) == 1 and sorted(batch[0]["names"]) == sorted(
          ["%s.jpg" % kp["b/p1.jpg"], "%s.jpg" % kp["b/p3.jpg"]])
      and svc.queue.items[kp["b/p2.jpg"]]["state"] == "queued", (r, [j["names"] for j in batch]))
svc.clear_queue()

# empty files (what a deleted download leaves behind): never queued, never uploaded. 2026-10-29: the
# worker refused their empty upload with 411 and that failed every picture in the same batch.
svc, qpath = fresh()
media("downloads", "e/empty.jpg", 0)
r = svc.request("downloads", "e/empty.jpg", stereo3d.LANE_FAVORITE, "alice", fav_at=5)
check("empty: a request for an empty file is skipped, nothing queued", r["status"] == "skipped"
      and "empty" in r["reason"] and not svc.queue.items, r)
for name in ("first.jpg", "a.jpg", "b.jpg"):
    media("downloads", "e/" + name, 3000)
    svc.request("downloads", "e/" + name)
ke = {i["path"]: k for k, i in svc.queue.items.items()}
media("downloads", "e/first.jpg", 0)            # emptied while it waited (deleted: the marker stays)
ve = stereo3d.file_version(os.path.join(LIB, "downloads/e/empty.jpg"))
kempty = stereo3d.item_key(ve, ppid)
with svc.queue.lock:                            # an empty one queued before the fix, between a and b
    svc.queue.items[kempty] = dict(svc.queue.items[ke["e/a.jpg"]], key=kempty, path="e/empty.jpg", version=ve,
                                   seq=svc.queue.items[ke["e/a.jpg"]]["seq"])
r = svc.step()
batch = [j for j in FW.jobs.values() if j["names"]]
left = {i["path"] for i in svc.queue.items.values() if i["state"] == "queued"}
check("empty: the emptied first item is dropped, the empty one stays out of the batch, a+b go as one job",
      r == "submitted" and len(batch) == 1 and sorted(batch[0]["names"]) == sorted(
          ["%s.jpg" % ke["e/a.jpg"], "%s.jpg" % ke["e/b.jpg"]])
      and "e/first.jpg" not in {i["path"] for i in svc.queue.items.values()} and left == {"e/empty.jpg"},
      (r, [j["names"] for j in batch], left))
check("empty: nothing empty was sent", not any(0 in j.get("sizes", []) for j in FW.jobs.values()), FW.jobs)
svc.clear_queue()

# at start: empty items leave the queue, pictures that one of them failed with its 411 are queued again
svc, qpath = fresh()
media("downloads", "s/real.jpg", 3000)
media("downloads", "s/other.mp4", 3000)
svc.request("downloads", "s/real.jpg")
svc.request("downloads", "s/other.mp4")
kr = next(k for k, i in svc.queue.items.items() if i["path"] == "s/real.jpg")
ko = next(k for k, i in svc.queue.items.items() if i["path"] == "s/other.mp4")
with svc.queue.lock:
    svc.queue.items[kr].update(state="failed", attempts=2, error="the worker refused: 411: Content-Length required",
                               not_before=CLOCK[0] + 99)
    svc.queue.items[ko].update(state="failed", attempts=2, error="vidinterp failed rc=1")
    svc.queue.items[kempty] = dict(svc.queue.items[kr], key=kempty, path="e/empty.jpg", version=ve)
    svc.queue.save()
svc2 = make_service(qpath)
it = svc2.queue.items.get(kr)
check("start: the empty item is dropped, the 411 batch-mate queued again with attempts 0, other failures stay",
      kempty not in svc2.queue.items and it and it["state"] == "queued" and it["attempts"] == 0 and it["error"] is None
      and it["not_before"] == 0 and svc2.queue.items[ko]["state"] == "failed" and svc2.queue.items[ko]["attempts"] == 2,
      (sorted(svc2.queue.items), it))
check("start: the repair is saved (a second start finds nothing to do)",
      kempty not in stereo3d.Queue(qpath).items and stereo3d.Queue(qpath).items[kr]["state"] == "queued")
svc2.clear_queue()

# ================================================================== a permanent folder taken off the list
# User, 2026-10-29: "I took the series folder off the list of folders to convert to 3D" - its 50
# episodes stayed queued and kept being uploaded; the scan only ever added.
PINNED = {"pin/"}


def pinned(root, path):
    return any(path.startswith(p) for p in PINNED)


svc, qpath = fresh()
svc = stereo3d.Service(qpath, lambda: dict(CFG), resolve, lambda: (ADDR["url"], "tok"),
                       lambda root, path: (root, path) in FAVS or pinned(root, path),
                       forbidden_dirs=(BASE, "/data2"), clock=lambda: CLOCK[0], pinned_fn=pinned)
rows = [(1003, "downloads", "pin/s/e0.mp4"), (1002, "downloads", "pin/s/e1.mp4"), (1001, "downloads", "pin/s/e2.mp4"),
        (900, "downloads", "pin/keep/k.mp4"), (500, "downloads", "pin/s/m.mp4")]
for n, (_, _, rel) in enumerate(rows):
    media("downloads", rel, 4000 + n)
r = svc.pin_scan(rows)
kpin = {i["path"]: k for k, i in svc.queue.items.items()}
check("pins: the scan queues every file in the favourites lane, by the permanent folder, nothing dropped",
      r["queued"] == 5 and r["dropped"] == 0 and all(i["lane"] == stereo3d.LANE_FAVORITE and i["by"] == stereo3d.PIN_BY
                                                     for i in svc.queue.items.values()), r)
svc.step()
check("pins: the newest file is on the worker", svc.queue.items[kpin["pin/s/e0.mp4"]]["state"] == "submitted")
svc.request("downloads", "pin/s/m.mp4", by="bob")                       # somebody asked for it: manual now
FAVS.add(("downloads", "pin/s/e2.mp4"))                                 # an admin favourited it
svc.queue.update(kpin["pin/s/e1.mp4"], state="failed", attempts=2, error="x")
PINNED.clear()
PINNED.add("pin/keep/")                                                 # pin/s/ came off the list
r = svc.pin_scan([(900, "downloads", "pin/keep/k.mp4")])
left = {i["path"]: (i["state"], i["lane"]) for i in svc.queue.items.values()}
check("pins: taken off the list -> its waiting items leave the queue (a failed one too); a job the worker has, "
      "a manual request, an admin's favourite and what is still pinned stay",
      r["dropped"] == 1 and "pin/s/e1.mp4" not in left and left.get("pin/s/e0.mp4") == ("submitted", "favorite")
      and left.get("pin/s/m.mp4", ("", ""))[1] == "manual" and left.get("pin/s/e2.mp4") == ("queued", "favorite")
      and left.get("pin/keep/k.mp4") == ("queued", "favorite"), (r, left))
check("pins: the drop is saved", "pin/s/e1.mp4" not in {i["path"] for i in stereo3d.Queue(qpath).items.values()})
PINNED.clear()                          # the last folder came off too: the server stops scanning
started = []
for _ in range(3):                      # e0 done -> m (manual) -> e2 (favourite) -> k (no longer pinned)
    k = next(k for k, i in svc.queue.items.items() if i["state"] == "submitted")
    started.append(svc.queue.items[k]["path"])
    FW.finish(k)
    svc.step()
check("pins: with no scan any more the dispatcher drops it when its turn comes, never uploading it",
      started == ["pin/s/e0.mp4", "pin/s/m.mp4", "pin/s/e2.mp4"] and not svc.queue.items
      and FW.count("PUT", "source", kpin["pin/keep/k.mp4"]) == 0, (started, list(svc.queue.items)))
FAVS.clear()

# cancelled while the upload runs: the job that arrives at the worker is deleted again
svc, qpath = fresh()
media("downloads", "f/cancelled.mp4", 3000)
svc.request("downloads", "f/cancelled.mp4")
key = next(iter(svc.queue.items))
real_put = svc.worker.put_source


def put_then_cancel(*a, **kw):
    real_put(*a, **kw)
    svc.cancel(key)


svc.worker.put_source = put_then_cancel
res = svc.step()
svc.worker.put_source = real_put
check("cancel during upload: nothing is submitted, the worker's copy is deleted, the queue stays empty",
      res == "cancelled" and not svc.queue.items and key not in FW.jobs and FW.count("DELETE", "job", key) >= 1, (res, FW.log))

# ================================================================== worker down
svc, qpath = fresh()
media("downloads", "d/down.mp4", 3000)
svc.request("downloads", "d/down.mp4")
ADDR["url"] = DEAD
res = [svc.step()]
key = next(iter(svc.queue.items))
check("worker down: step reports it, the item stays queued, nothing lost", res == ["worker down"]
      and svc.queue.items[key]["state"] == "queued" and svc.queue.items[key]["attempts"] == 0 and svc.worker_error)
check("worker down: the next tick backs off without trying", svc.step() == "backoff")
CLOCK[0] += 31
check("worker down: a second failure doubles the wait", svc.step() == "worker down" and svc.down_until - CLOCK[0] == 60)
ADDR["url"] = FW.url
CLOCK[0] += 61
res = svc.step()
check("worker down: back up -> the item goes through", res == "submitted" and svc.fail_count == 0 and svc.worker_error is None, res)
svc.cancel(key)
FW.available = False
svc.request("downloads", "d/down.mp4")
CLOCK[0] += 1000
check("worker not installed (available false) is treated as down", svc.step() == "worker down" and "missing" in svc.worker_error)
FW.available = True
svc.clear_queue()

# 507: worker disk low -> back off 10 min, item stays queued (no attempt)
svc, qpath = fresh()
media("downloads", "d/big.mp4", 3000)
svc.request("downloads", "d/big.mp4")
FW.disk_low = True
res = svc.step()
key = next(iter(svc.queue.items))
check("507: worker disk low -> queued with a note, backoff, no attempt counted", res == "worker disk low"
      and svc.queue.items[key]["state"] == "queued" and svc.queue.items[key]["attempts"] == 0
      and "disk" in (svc.queue.items[key]["error"] or ""), (res, svc.queue.items[key]))
check("507: nothing is tried during the backoff", svc.step() == "backoff")
FW.disk_low = False
CLOCK[0] += stereo3d.DISK_LOW_BACKOFF_S + 1
check("507: afterwards it goes through", svc.step() == "submitted")

# ================================================================== manual holds a running favourite
svc, qpath = fresh()
media("downloads", "h/fav.mp4", 5000)
media("downloads", "h/man.mp4", 5001)
FAVS.add(("downloads", "h/fav.mp4"))
svc.request("downloads", "h/fav.mp4", stereo3d.LANE_FAVORITE, "alice")
favkey = next(iter(svc.queue.items))
drive(svc, 1)
FW.set_state(favkey, "converting", progress={"phase": "converting"})
svc.step()
check("hold: a running favourite is left alone while nothing manual waits", FW.count("POST", "hold") == 0)
svc.request("downloads", "h/man.mp4", by="alice")
mankey = [k for k in svc.queue.items if k != favkey][0]
svc.step()
check("hold: a manual request holds the running favourite video", FW.count("POST", "hold", favkey) == 1
      and FW.jobs[favkey]["state"] == "held" and svc.queue.items[favkey]["state"] == "held", FW.log)
svc.step()
check("hold: and then the manual item is uploaded and submitted", FW.count("PUT", "source", mankey) == 1
      and svc.queue.items[mankey]["state"] == "submitted" and FW.jobs[favkey]["state"] == "held")
FW.set_state(mankey, "converting")
svc.step()
check("hold: the favourite stays held while the manual job runs", FW.jobs[favkey]["state"] == "held"
      and FW.count("POST", "resume") == 0)
FW.finish(mankey)
svc.step()
check("hold: manual done -> cached", svc.index().files_ok(mankey))
svc.step()
check("hold: afterwards the held favourite is resumed (not re-uploaded)", FW.count("POST", "resume", favkey) == 1
      and FW.count("PUT", "source", favkey) == 1 and svc.queue.items[favkey]["state"] == "submitted"
      and FW.jobs[favkey]["state"] == "waiting_gpu", (FW.log[-6:], svc.queue.items[favkey]["state"]))
svc.clear_queue()

# a favourite PICTURE batch is not interrupted by a manual request
svc, qpath = fresh()
media("downloads", "h/p1.jpg", 3000)
media("downloads", "h/vm.mp4", 3000)
svc.request("downloads", "h/p1.jpg", stereo3d.LANE_FAVORITE, "alice")
drive(svc, 1)
svc.request("downloads", "h/vm.mp4")
svc.step()
check("hold: a running favourite picture batch is not held", FW.count("POST", "hold") == 0)
svc.clear_queue()

# ================================================================== picture batching
svc, qpath = fresh()
N = 60
for n in range(N):
    media("downloads", "pics/p%03d.%s" % (n, "jfif" if n == 0 else "jpg"), 1500 + n)
reqs = [{"root": "downloads", "path": "pics/p%03d.%s" % (n, "jfif" if n == 0 else "jpg")} for n in range(N)]
out = svc.request_many(reqs, by="alice")
check("pictures: 60 queued in one save", sum(1 for r in out if r["status"] == "queued") == N)
res = drive(svc, 1)
batch1 = [k for k, j in FW.jobs.items()]
job = FW.jobs[batch1[0]] if batch1 else {"names": []}
check("pictures: the first job batches 50, all manual, one job", res == ["submitted"] and len(FW.jobs) == 1
      and len(job["names"]) == 50 and FW.count("POST", "jobs") == 1, (res, len(FW.jobs), len(job["names"])))
check("pictures: names are <key>.<ext> with unique stems, jfif sent as jpg",
      len({os.path.splitext(n)[0] for n in job["names"]}) == 50 and all(n.endswith((".jpg",)) for n in job["names"])
      and FW.posted[0]["kind"] == "picture" and profile_ok(FW.posted[0]["profile"]) is None
      and "max_eye_long_side" not in FW.posted[0]["profile"], job["names"][:2])
wk1 = batch1[0]
check("pictures: the job key is a batch key, items share it", all(i["worker_key"] == wk1 for i in svc.queue.items.values() if i["state"] == "submitted")
      and sum(1 for i in svc.queue.items.values() if i["state"] == "submitted") == 50)
FW.finish(wk1)
svc.step()
cached = [k for k in svc.index().entries if svc.index().files_ok(k)]
sample = cached[0]
check("pictures: 50 results land in p/<k2>/<key>/{sbs.jpg,depth.png,meta.json}", len(cached) == 50
      and all(os.path.isfile(os.path.join(CACHE, "p", k[:2], k, "sbs.jpg")) and os.path.isfile(os.path.join(CACHE, "p", k[:2], k, "depth.png"))
              for k in cached), len(cached))
check("pictures: entry says picture with eye size", svc.index().get(sample)["kind"] == "picture" and svc.index().get(sample)["eye_w"] == 800)
check("pictures: the next 10 go as a second job (after the first was deleted)", wk1 not in FW.jobs and len(FW.jobs) == 1
      and len(list(FW.jobs.values())[0]["names"]) == 10)
wk2 = next(iter(FW.jobs))
FW.finish(wk2)
svc.step()
check("pictures: all 60 cached, queue empty", len([k for k in svc.index().entries]) == 60 and not svc.queue.items)

# manual and favourite pictures are never mixed in one batch; videos are never batched
svc, qpath = fresh()
for n in range(3):
    media("downloads", "mix/m%d.jpg" % n, 1700 + n)
    media("downloads", "mix/f%d.jpg" % n, 1800 + n)
    svc.request("downloads", "mix/m%d.jpg" % n, by="alice")
    svc.request("downloads", "mix/f%d.jpg" % n, stereo3d.LANE_FAVORITE, "alice", fav_at=10 + n)
drive(svc, 1)
j = next(iter(FW.jobs.values()))
mkeys = {k for k, i in svc.queue.items.items() if i["lane"] == "manual"}
check("pictures: a batch holds one lane only", len(j["names"]) == 3 and {os.path.splitext(n)[0] for n in j["names"]} == mkeys, j["names"])
svc.clear_queue()

# ================================================================== cache eviction
svc, qpath = fresh()
idx = svc.index()
now = CLOCK[0]


def fake_entry(name, last_watched, root="downloads", size=400_000, missing_since=None, kind="video"):
    media(root, "ev/" + name, 100)
    k = hashlib.sha256(name.encode()).hexdigest()[:32]
    d = os.path.join(CACHE, stereo3d.cache_rel(kind, k))
    os.makedirs(d, exist_ok=True)
    for n in stereo3d.RESULT_NAMES[kind]:
        open(os.path.join(d, n), "wb").write(b"x" * (size // 3))
    idx.put(k, {"kind": kind, "root": root, "path": "ev/" + name, "version": stereo3d.file_version(resolve(root, "ev/" + name)),
                "profile": "v1", "created": last_watched - 5, "bytes": size, "sbs_bytes": size // 3, "depth_bytes": size // 3,
                "last_watched": last_watched, "watch_count": 0, "sbs_atime": 0, "missing_since": missing_since})
    return k


kA = fake_entry("a_plain_old.mp4", now - 9000)
kB = fake_entry("b_plain_new.mp4", now - 100)
kC = fake_entry("c_fav_old.mp4", now - 20000)
kD = fake_entry("d_fav_new.mp4", now - 50)
kE = fake_entry("e_orphan.mp4", now - 10, missing_since=now - 8 * 86400)
kF = fake_entry("f_recent_missing.mp4", now - 30000, missing_since=now - 86400)
FAVS.update({("downloads", "ev/c_fav_old.mp4"), ("downloads", "ev/d_fav_new.mp4")})
order = stereo3d.eviction_order(idx.entries, lambda r, p: (r, p) in FAVS, now)
check("eviction order: orphans, then non-favourites LRU-watched, then favourites LRU-watched",
      order == [kE, kF, kA, kB, kC, kD], [idx.entries[k]["path"] for k in order])
total = idx.total_bytes()
CFG["cache_max_gb"] = (total - 3 * 400_000 + 1) / 1024 ** 3        # must drop three entries
gone = svc.evict(CFG, idx)
check("eviction: goes down the order until the cache fits the cap", gone == [kE, kF, kA] and idx.total_bytes() <= CFG["cache_max_gb"] * 1024 ** 3, [idx.entries.get(k) for k in gone])
check("eviction: files are deleted with their entries and index.json is saved", not os.path.exists(os.path.join(CACHE, "v", kE[:2], kE))
      and kE not in json.load(open(os.path.join(CACHE, "index.json"))) and svc.queue.evicted == {})
CFG["cache_max_gb"] = 0.0004         # ~430 KB: only one entry fits: b (non-fav) goes first, then favourites oldest first
gone = svc.evict(CFG, idx)
check("eviction: non-favourites before favourites, favourites oldest first, the newest survives", gone == [kB, kC]
      and list(idx.entries) == [kD], [idx.entries.get(k) for k in gone])
check("eviction: an evicted favourite is recorded with the cap", ("downloads\nev/c_fav_old.mp4" in svc.queue.evicted)
      and svc.queue.evicted["downloads\nev/c_fav_old.mp4"]["cap_gb"] == 0.0004 and "downloads\nev/a_plain_old.mp4" not in svc.queue.evicted)
check("eviction: the record survives a reload", "downloads\nev/c_fav_old.mp4" in stereo3d.Queue(qpath).evicted)
rf = svc.request("downloads", "ev/c_fav_old.mp4", stereo3d.LANE_FAVORITE, "alice")
check("evicted favourite: not auto-converted again (no convert-evict loop)", rf["status"] == "skipped" and not svc.queue.items, rf)
CFG["cache_max_gb"] = 0.0003
rf = svc.request("downloads", "ev/c_fav_old.mp4", stereo3d.LANE_FAVORITE, "alice")
check("evicted favourite: still blocked at a smaller cap", rf["status"] == "skipped")
CFG["cache_max_gb"] = 0.001
rf = svc.request("downloads", "ev/c_fav_old.mp4", stereo3d.LANE_FAVORITE, "alice")
check("evicted favourite: allowed again once the cap grew", rf["status"] == "queued", rf)
svc.clear_queue()
CFG["cache_max_gb"] = 0.0004
svc.queue.record_evicted("downloads", "ev/c_fav_old.mp4", 0.0004)
rm = svc.request("downloads", "ev/c_fav_old.mp4", by="alice")
check("evicted favourite: a manual request is accepted and clears the record", rm["status"] == "queued"
      and "downloads\nev/c_fav_old.mp4" not in svc.queue.evicted, rm)
svc.clear_queue()

# Eviction runs before a new job: room is made for what is expected to come (EST_*), and a
# favourite never pushes out another favourite - a full cache of favourites keeps it waiting
# while a manual request still goes ahead.
stereo3d.EST_VIDEO_BYTES = 300_000
svc, qpath = fresh()
idx = svc.index()
kA = fake_entry("old1.mp4", CLOCK[0] - 5000)                 # not a favourite
kB = fake_entry("old2.mp4", CLOCK[0] - 4000)                 # a favourite
FAVS.add(("downloads", "ev/old2.mp4"))
media("downloads", "ev/new_manual.mp4", 4000)
media("downloads", "ev/new_fav.mp4", 4000)
svc.request("downloads", "ev/new_fav.mp4", stereo3d.LANE_FAVORITE, "alice")
FAVS.add(("downloads", "ev/new_fav.mp4"))
CFG["cache_max_gb"] = 0.0009         # 966 KB: 800 KB held + 300 KB coming does not fit
res = svc.step()
check("full cache: before a job starts the least recently watched NON-favourite goes, the favourite stays",
      res == "submitted" and kA not in idx.entries and kB in idx.entries and svc.queue.evicted == {}, (res, list(idx.entries)))
svc.clear_queue()
svc, qpath = fresh()
idx = svc.index()
kB = fake_entry("old2.mp4", CLOCK[0] - 4000)
kC = fake_entry("old3.mp4", CLOCK[0] - 3000)
FAVS.update({("downloads", "ev/old2.mp4"), ("downloads", "ev/old3.mp4")})
svc.request("downloads", "ev/new_fav.mp4", stereo3d.LANE_FAVORITE, "alice")
CFG["cache_max_gb"] = 0.0009
res = svc.step()
check("full cache: a favourite waits when only favourites could make room (nothing evicted, no churn)",
      res != "submitted" and set(idx.entries) == {kB, kC} and FW.count("PUT", "source") == 0 and svc.queue.evicted == {}
      and "full" in (svc.queue.items[next(iter(svc.queue.items))]["error"] or ""), (res, list(idx.entries)))
svc.step()
check("full cache: and is not retried before its wait is over", FW.count("PUT", "source") == 0)
svc.request("downloads", "ev/new_manual.mp4", by="alice")
res = svc.step()
check("full cache: a manual item still goes ahead", res == "submitted" and FW.count("PUT", "source") == 1, res)
svc.clear_queue()
stereo3d.EST_VIDEO_BYTES = 1 << 30

# ================================================================== watched tracking
svc, qpath = fresh()
idx = svc.index()
kW = fake_entry("watch.mp4", CLOCK[0] - 5000)
t0 = CLOCK[0]
a = svc.note_play(kW, None)
b = svc.note_play(kW, None)
c = svc.note_play(kW, "bytes=500-")
check("watched: a play start counts once, a repeat within 10 min and a mid-file Range do not",
      a and not b and not c and idx.get(kW)["watch_count"] == 1 and idx.get(kW)["last_watched"] == t0)
CLOCK[0] += 601
d = svc.note_play(kW, "bytes=0-")
check("watched: Range from 0 counts after the gap", d and idx.get(kW)["watch_count"] == 2 and idx.get(kW)["last_watched"] == CLOCK[0])
sbs = os.path.join(CACHE, stereo3d.cache_rel("video", kW), "sbs.mp4")
new_atime = CLOCK[0] + 5000
os.utime(sbs, (new_atime, os.stat(sbs).st_mtime))
svc.maintenance()
check("watched: maintenance notices a new atime on sbs.mp4 (SKYBOX over SMB) and moves last_watched",
      idx.get(kW)["last_watched"] == new_atime and idx.get(kW)["sbs_atime"] == new_atime and idx.get(kW)["watch_count"] == 3)
os.utime(sbs, (new_atime - 4000, os.stat(sbs).st_mtime))
svc.maintenance()
check("watched: an older atime does not move it back", idx.get(kW)["last_watched"] == new_atime)

# maintenance: orphan marks, entries whose files are gone, stray folders
os.rename(os.path.join(LIB, "downloads/ev/watch.mp4"), os.path.join(LIB, "downloads/ev/watch.mp4.away"))
svc.maintenance()
check("maintenance: a missing source is marked (missing_since), kept", idx.get(kW)["missing_since"] == CLOCK[0])
os.rename(os.path.join(LIB, "downloads/ev/watch.mp4.away"), os.path.join(LIB, "downloads/ev/watch.mp4"))
svc.maintenance()
check("maintenance: a source that is back clears the mark", idx.get(kW)["missing_since"] is None)
shutil.rmtree(os.path.join(CACHE, stereo3d.cache_rel("video", kW)))
svc.maintenance()
check("maintenance: an entry whose files vanished leaves the index", kW not in idx.entries)
stray = os.path.join(CACHE, "v", "ab", "f" * 32)
os.makedirs(stray)
os.utime(stray, (CLOCK[0] - 3 * 86400, CLOCK[0] - 3 * 86400))
fake_age = time.time()
CLOCK[0] = fake_age + 1
os.utime(stray, (fake_age - 2 * 86400, fake_age - 2 * 86400))
svc.maintenance()
check("maintenance: a folder no entry owns goes after a day", not os.path.exists(stray))

# ================================================================== the cache folder rules
svc, qpath = fresh()
missing = os.path.join(TMP, "no_such_cache")
CFG["cache_dir"] = missing
media("downloads", "c/idle.mp4", 3000)
r = svc.request("downloads", "c/idle.mp4")
res = svc.step()
check("cache folder missing: nothing is created, the dispatcher idles and says how to create it",
      not os.path.exists(missing) and "create it with the service account as owner" in svc.problem and missing in svc.problem
      and res.startswith("cache folder") and FW.log == [] and r["status"] == "queued", (res, svc.problem))
check("cache folder missing: the request still queues, and the overview carries the problem",
      len(svc.queue.items) == 1 and svc.overview()["cache"]["problem"] == svc.problem)
for bad in ("/data2/ksf_web_dl_3d", os.path.join(BASE, "cache3d"), BASE):
    CFG["cache_dir"] = bad
    c, why = svc.cache_dir()
    check("cache folder %s is refused (never the app disk)" % bad, c is None and why and "media disk" in why, (c, why))
CFG["cache_dir"] = ""
c, why = svc.cache_dir()
check("cache folder default is MEDIA_3D_CACHE, read when asked", (c or "") == stereo3d.default_cache_dir()
      or stereo3d.default_cache_dir() in (why or ""), (c, why))
os.makedirs(os.path.join(TMP, "ro_cache"))
CFG["cache_dir"] = os.path.join(TMP, "ro_cache")
os.chmod(CFG["cache_dir"], 0o555)
c, why = svc.cache_dir()
if os.geteuid() != 0:
    check("cache folder not writable is reported", c is None and "not writable" in why, (c, why))
os.chmod(CFG["cache_dir"], 0o755)
CFG["cache_dir"] = CACHE

# ================================================================== clear cache
svc, qpath = fresh()
idx = svc.index()
fake_entry("clear1.mp4", CLOCK[0] - 5)
fake_entry("clear2.mp4", CLOCK[0] - 6)
os.makedirs(os.path.join(CACHE, ".partial", "x"))
n = svc.clear_cache()
check("clear cache: every entry and its files go, .partial too", n == 2 and not idx.entries
      and not os.path.exists(os.path.join(CACHE, "v", next(iter(os.listdir(os.path.join(CACHE, "v"))), "zz"), "x"))
      and not os.path.exists(os.path.join(CACHE, ".partial")) and json.load(open(os.path.join(CACHE, "index.json"))) == {})

# ================================================================== the turn (videos filmed on their side)
tp0, tpid0 = stereo3d.profile_for("video", CFG)
tp90, tpid90 = stereo3d.profile_for("video", CFG, 90)
tp180, tpid180 = stereo3d.profile_for("video", CFG, 180)
check("turn: 0 changes nothing (profile has no turn_cw, id unchanged: old cache keys stay valid)",
      "turn_cw" not in tp0 and tpid0 == pid and stereo3d.profile_for("video", CFG, 0) == (tp0, tpid0))
check("turn: 90 adds turn_cw to the profile and a suffix to the id; 90 and 180 differ",
      tp90.get("turn_cw") == 90 and tpid90 != tpid0 and tpid90 != tpid180 and tp180["turn_cw"] == 180)
check("turn: the turned profile is still what the worker accepts", profile_ok(tp90) is None and profile_ok(tp180) is None)
check("turn: a picture ignores it", stereo3d.profile_for("picture", CFG, 90) == stereo3d.profile_for("picture", CFG))
check("turn: an odd value (45) is not applied", "turn_cw" not in stereo3d.profile_for("video", CFG, 45)[0])

TURNS = {}
svc_t, _ = fresh("turn.json")
svc_t.turn_fn = lambda r, p: TURNS.get((r, p), 0)
media("downloads", "t/side.mp4", 4000, 1_600_000_100)
media("downloads", "t/upright.mp4", 4001, 1_600_000_100)
media("downloads", "t/pic.jpg", 4002, 1_600_000_100)
TURNS[("downloads", "t/side.mp4")] = 90
TURNS[("downloads", "t/pic.jpg")] = 90
r_side = svc_t.request("downloads", "t/side.mp4")
r_up = svc_t.request("downloads", "t/upright.mp4")
r_pic = svc_t.request("downloads", "t/pic.jpg")
v_side = stereo3d.file_version(os.path.join(LIB, "downloads/t/side.mp4"))
check("turn: a turned video is queued under the turned id; an upright one and a picture under the plain ids",
      r_side["key"] == stereo3d.item_key(v_side, tpid90)
      and r_up["key"] == stereo3d.item_key(stereo3d.file_version(os.path.join(LIB, "downloads/t/upright.mp4")), tpid0)
      and r_pic["key"] == stereo3d.item_key(stereo3d.file_version(os.path.join(LIB, "downloads/t/pic.jpg")),
                                            stereo3d.profile_for("picture", CFG)[1]))
drive(svc_t)
sent = [p for p in FW.posted if p.get("kind") == "video"] if isinstance(FW.posted, list) else []
turned_posts = [p for p in sent if (p.get("profile") or {}).get("turn_cw") == 90]
check("turn: the job POST of the turned video carries turn_cw 90 (and an upright one would carry none)",
      len(turned_posts) == 1 and all(p["label"] != "downloads/t/upright.mp4" or "turn_cw" not in p["profile"] for p in sent), FW.posted)
# a hand override arrives later: the next request is a different key (converts again), status follows the turn
TURNS[("downloads", "t/upright.mp4")] = 270
r_over = svc_t.request("downloads", "t/upright.mp4")
check("turn: a hand-set turn on an upright-detected video re-keys it (270) - an override is honoured",
      r_over["key"] != r_up["key"] and r_over["status"] in ("queued", "duplicate", "promoted"), r_over)
check("turn: status_for follows the turn (the old unturned conversion no longer counts)",
      svc_t.status_for("downloads", "t/upright.mp4").get("key") == r_over["key"])
# a queued item whose turn changed is re-keyed before it is sent
svc_t2, _ = fresh("turn2.json")
svc_t2.turn_fn = lambda r, p: TURNS.get((r, p), 0)
TURNS.clear()
media("downloads", "t/late.mp4", 4010, 1_600_000_100)
k_before = svc_t2.request("downloads", "t/late.mp4")["key"]
TURNS[("downloads", "t/late.mp4")] = 90          # the orientation pass finished while it waited
verdict, it2 = svc_t2._validate(svc_t2.queue.items[k_before])
check("turn: a queued video that turns up sideways is re-keyed (not sent unturned)",
      verdict == "rekeyed" and it2["key"] != k_before and it2["profile_id"] == tpid90, (verdict, it2))
svc_t3, _ = fresh("turn3.json")
svc_t3.turn_fn = lambda r, p: 1 / 0               # a broken turn source must not break conversion
media("downloads", "t/broken.mp4", 4020, 1_600_000_100)
check("turn: an error in the turn lookup means no turn", svc_t3.request("downloads", "t/broken.mp4")["status"] == "queued")
src_s = open(SERVER, encoding="utf-8").read()
check("wiring: the server hands the service its effective turn (turn_of)", "turn_fn=lambda r, p: turn_of(r, p)[0]" in src_s)
svc_t.clear_queue()


# ================================================================== viewing lane (P8 V1: the VR app opened it)
L_V, L_M, L_F = stereo3d.LANE_VIEWING, stereo3d.LANE_MANUAL, stereo3d.LANE_FAVORITE


def vk(svc, root, rel):
    """The queue key of a file (its key at the current profile)."""
    kind = stereo3d.kind_of(rel)
    _, p = stereo3d.profile_for(kind, CFG)
    return stereo3d.item_key(stereo3d.file_version(os.path.join(LIB, root, rel)), p)


def order_paths(svc):
    return [i["path"] for i in svc.queue.ordered()]


for n in "abcdef":
    media("downloads", "vw/%s.mp4" % n, 4000 + ord(n))
media("downloads", "vw/p1.jpg", 3100)
check("viewing: the lane constant and the urgent lanes", L_V == "viewing" and stereo3d.URGENT_LANES == (L_V, L_M)
      and stereo3d.VIEWING_MAX_PER_USER == 3)

# lane order: viewing (newest first) > manual (FIFO) > favourite (newest first)
svc, qpath = fresh()
svc.request("downloads", "vw/a.mp4", L_F, "alice", fav_at=CLOCK[0] - 50)
svc.request("downloads", "vw/b.mp4", L_F, "alice", fav_at=CLOCK[0] - 10)
svc.request("downloads", "vw/c.mp4", L_M, "alice")
svc.request("downloads", "vw/d.mp4", L_M, "alice")
r1 = svc.view("downloads", "vw/e.mp4", "alice")
r2 = svc.view("downloads", "vw/f.mp4", "alice")
check("viewing lane: a new view is queued in the viewing lane", r1["status"] == "queued" and r2["status"] == "queued"
      and svc.queue.items[r1["key"]]["lane"] == L_V, r1)
check("viewing lane order: viewing (newest first) > manual (FIFO) > favourites (newest first)",
      order_paths(svc) == ["vw/f.mp4", "vw/e.mp4", "vw/c.mp4", "vw/d.mp4", "vw/b.mp4", "vw/a.mp4"], order_paths(svc))
check("viewing lane: position/ahead in /status follow that order", svc.status_for("downloads", "vw/e.mp4")["position"] == 2
      and svc.status_for("downloads", "vw/e.mp4")["lane"] == L_V and svc.status_for("downloads", "vw/a.mp4")["position"] == 6)
check("viewing lane: a viewing item that was retried sorts behind fresh viewing items, still ahead of manual",
      (lambda: (svc.queue.update(r2["key"], attempts=1, not_before=0), order_paths(svc)[:3])[1])()
      == ["vw/e.mp4", "vw/f.mp4", "vw/c.mp4"])
svc.queue.update(r2["key"], attempts=0)
svc.clear_queue()

# promotion from manual / favourite; a manual request does not pull a viewing item down
svc, qpath = fresh()
svc.request("downloads", "vw/a.mp4", L_F, "alice")
svc.request("downloads", "vw/b.mp4", L_M, "alice")
svc.request("downloads", "vw/c.mp4", L_M, "alice")
ra = svc.view("downloads", "vw/a.mp4", "bob")
rb = svc.view("downloads", "vw/b.mp4", "bob")
check("promotion: a queued favourite and a queued manual item move up to the viewing lane",
      ra["status"] == "promoted" and rb["status"] == "promoted"
      and svc.queue.items[ra["key"]]["lane"] == L_V and svc.queue.items[rb["key"]]["lane"] == L_V
      and order_paths(svc) == ["vw/b.mp4", "vw/a.mp4", "vw/c.mp4"], order_paths(svc))
check("promotion: viewing the same item again is a duplicate and puts it first (newest view)",
      svc.view("downloads", "vw/a.mp4", "bob")["status"] == "duplicate" and order_paths(svc)[0] == "vw/a.mp4")
check("promotion: a manual request for an item in the viewing lane leaves it there",
      svc.request("downloads", "vw/b.mp4", L_M, "alice")["status"] == "duplicate"
      and svc.queue.items[rb["key"]]["lane"] == L_V)
check("promotion: a favourite request for an item in the viewing lane leaves it there",
      svc.favourite_added("downloads", "vw/b.mp4", "alice")["status"] == "duplicate"
      and svc.queue.items[rb["key"]]["lane"] == L_V)
check("promotion: an item queued for a retry keeps its retry time when viewed",
      (lambda: (svc.queue.update(vk(svc, "downloads", "vw/c.mp4"), attempts=1, not_before=CLOCK[0] + 900),
                svc.view("downloads", "vw/c.mp4", "bob"),
                svc.queue.items[vk(svc, "downloads", "vw/c.mp4")])[2])()["not_before"] == CLOCK[0] + 900)
svc.clear_queue()

# done is a no-op that says so; failed items obey the retry rule
svc, qpath = fresh()
svc.request("downloads", "vw/a.mp4", L_M, "alice")
drive(svc, 1)
ak = vk(svc, "downloads", "vw/a.mp4")
FW.finish(ak)
svc.step()
before = len(svc.queue.items)
rd = svc.view("downloads", "vw/a.mp4", "bob")
check("done: viewing a converted item is a no-op that returns cached/done", rd["status"] == "cached"
      and len(svc.queue.items) == before == 0 and svc.status_for("downloads", "vw/a.mp4")["state"] == "done")
svc.request("downloads", "vw/b.mp4", L_M, "alice")
bk = vk(svc, "downloads", "vw/b.mp4")
svc.queue.update(bk, state="failed", attempts=2, error="boom")
rf = svc.view("downloads", "vw/b.mp4", "bob")
check("failed: viewing a failed item does not retry or move it (only an admin retry does)", rf["status"] == "duplicate"
      and svc.queue.items[bk]["state"] == "failed" and svc.queue.items[bk]["lane"] == L_M)
svc.queue.update(bk, state="queued", attempts=1, error="x", not_before=CLOCK[0] + stereo3d.FAILED_RETRY_S)
rf2 = svc.view("downloads", "vw/b.mp4", "bob")
check("failed: an item waiting for its retry time is promoted but still waits (FAILED_RETRY_S kept)",
      rf2["status"] == "promoted" and svc.queue.items[bk]["not_before"] == CLOCK[0] + stereo3d.FAILED_RETRY_S
      and not svc.queue.candidates(CLOCK[0]))
svc.queue.update(bk, state="failed", attempts=2)
svc.request("downloads", "vw/b.mp4", L_M, "alice")
check("failed: an admin's manual request retries it and takes it out of the viewing lane",
      svc.queue.items[bk]["state"] == "queued" and svc.queue.items[bk]["lane"] == L_M
      and "viewers" not in svc.queue.items[bk])
check("viewing: disabled 3D and missing/unsupported files are skipped", (lambda: (CFG.update(enabled=False),
      svc.view("downloads", "vw/c.mp4", "bob")["status"], CFG.update(enabled=True))[1])() == "skipped"
      and svc.view("downloads", "vw/nope.mp4", "bob")["status"] == "skipped"
      and svc.view("downloads", "vw/x.mp3", "bob")["status"] == "skipped")
svc.clear_queue()

# leaving: demoted to the END of the manual lane, never cancelled; several viewers
svc, qpath = fresh()
svc.request("downloads", "vw/c.mp4", L_M, "alice")
svc.request("downloads", "vw/d.mp4", L_M, "alice")
svc.view("downloads", "vw/a.mp4", "alice")
svc.view("downloads", "vw/b.mp4", "alice")
lv = svc.leave("downloads", "vw/a.mp4", "alice")
ak = vk(svc, "downloads", "vw/a.mp4")
check("leave: a still-waiting viewing item goes to the END of the manual lane", lv["status"] == "demoted"
      and svc.queue.items[ak]["lane"] == L_M and order_paths(svc) == ["vw/b.mp4", "vw/c.mp4", "vw/d.mp4", "vw/a.mp4"],
      order_paths(svc))
check("leave: it is not cancelled (still queued, the viewer's mark is gone)", svc.queue.items[ak]["state"] == "queued"
      and "viewers" not in svc.queue.items[ak])
check("leave: leaving an item that is not in the viewing lane, or unknown, changes nothing",
      svc.leave("downloads", "vw/c.mp4", "alice")["status"] == "none" and svc.leave("downloads", "vw/zz.mp4", "alice")["status"] == "none"
      and svc.leave("downloads", "vw/x.mp3", "alice")["status"] == "none" and order_paths(svc)[1:] == ["vw/c.mp4", "vw/d.mp4", "vw/a.mp4"])
svc.view("downloads", "vw/b.mp4", "bob")
check("leave: with another viewer left the item stays in the viewing lane",
      svc.leave("downloads", "vw/b.mp4", "alice")["status"] == "kept"
      and svc.queue.items[vk(svc, "downloads", "vw/b.mp4")]["lane"] == L_V)
check("leave: the last viewer leaving demotes it",
      svc.leave("downloads", "vw/b.mp4", "bob")["status"] == "demoted"
      and order_paths(svc)[-1] == "vw/b.mp4")
check("leave: the user's later leave of a demoted item is a no-op", svc.leave("downloads", "vw/b.mp4", "bob")["status"] == "none")
svc.clear_queue()

# the per-user bound demotes that user's oldest view
svc, qpath = fresh()
for n in "abcde":
    media("downloads", "vw/%s.mp4" % n, 4000 + ord(n))
svc.request("downloads", "vw/f.mp4", L_M, "alice")
for n in "abc":
    svc.view("downloads", "vw/%s.mp4" % n, "alice")
svc.view("downloads", "vw/e.mp4", "bob")
check("bound: three views per user are kept (default 3)", [svc.queue.items[vk(svc, "downloads", "vw/%s.mp4" % n)]["lane"] for n in "abc"] == [L_V] * 3)
svc.view("downloads", "vw/d.mp4", "alice")
lanes = {n: svc.queue.items[vk(svc, "downloads", "vw/%s.mp4" % n)]["lane"] for n in "abcde"}
check("bound: a fourth view demotes that user's oldest to the manual lane, at its end; other users untouched",
      lanes == {"a": L_M, "b": L_V, "c": L_V, "d": L_V, "e": L_V} and order_paths(svc)[-1] == "vw/a.mp4"
      and order_paths(svc)[:4] == ["vw/d.mp4", "vw/e.mp4", "vw/c.mp4", "vw/b.mp4"], (lanes, order_paths(svc)))
svc.view("downloads", "vw/b.mp4", "alice")
svc.view("downloads", "vw/a.mp4", "alice")
lanes = {n: svc.queue.items[vk(svc, "downloads", "vw/%s.mp4" % n)]["lane"] for n in "abcd"}
check("bound: a re-view refreshes the stamp, so the next demotion takes the genuinely oldest view",
      lanes == {"a": L_V, "b": L_V, "c": L_M, "d": L_V}, lanes)
CFG["viewing_max_per_user"] = 1
svc.view("downloads", "vw/c.mp4", "alice")
check("bound: the setting viewing_max_per_user is honoured",
      sum(1 for i in svc.queue.items.values() if i["lane"] == L_V and "alice" in (i.get("viewers") or {})) == 1)
CFG.pop("viewing_max_per_user")
svc.clear_queue()

# persistence round trip, and an old queue file without the lane
svc, qpath = fresh()
svc.request("downloads", "vw/c.mp4", L_M, "alice")
svc.request("downloads", "vw/d.mp4", L_F, "alice")
svc.view("downloads", "vw/a.mp4", "alice")
svc.view("downloads", "vw/b.mp4", "bob")
want = order_paths(svc)
svc2 = make_service(qpath)
check("persistence: lanes, viewers and the order survive a restart", order_paths(svc2) == want == ["vw/b.mp4", "vw/a.mp4", "vw/c.mp4", "vw/d.mp4"]
      and svc2.queue.items[vk(svc, "downloads", "vw/a.mp4")]["viewers"].keys() == {"alice"}
      and svc2.queue.seq >= max(max(i["viewers"].values()) for i in svc2.queue.items.values() if i.get("viewers")))
svc2.view("downloads", "vw/e.mp4", "alice")
check("persistence: a view after the restart is newer than every earlier one", order_paths(svc2)[0] == "vw/e.mp4")
svc2.leave("downloads", "vw/e.mp4", "alice")
check("persistence: a demotion after the restart goes behind the existing manual items, ahead of favourites",
      order_paths(svc2) == ["vw/b.mp4", "vw/a.mp4", "vw/c.mp4", "vw/e.mp4", "vw/d.mp4"], order_paths(svc2))
old = json.load(open(qpath))
for it in old["items"]:
    it.pop("viewers", None)
    if it["lane"] == L_V:
        it["lane"] = L_M
json.dump(old, open(qpath, "w"))
svc3 = make_service(qpath)
check("persistence: an old queue file (no viewing lane, no viewers) loads unchanged",
      len(svc3.queue.items) == 5 and all(i["lane"] in (L_M, L_F) for i in svc3.queue.items.values())
      and [i["lane"] for i in svc3.queue.ordered()] == [L_M, L_M, L_M, L_M, L_F])
svc3.view("downloads", "vw/d.mp4", "alice")
check("persistence: and the old file takes a view afterwards", order_paths(svc3)[0] == "vw/d.mp4")
svc3.clear_queue()

# a running job is untouched by a leave, a view and the bound
svc, qpath = fresh()
svc.view("downloads", "vw/a.mp4", "alice")
drive(svc, 1)
ak = vk(svc, "downloads", "vw/a.mp4")
check("running: the viewing item was submitted to the worker", svc.queue.items[ak]["state"] == "submitted" and FW.count("PUT", "source", ak) == 1)
FW.set_state(ak, "converting")
svc.step()
check("running: leaving does not touch it (lane, state, the worker's job)", svc.leave("downloads", "vw/a.mp4", "alice")["status"] == "none"
      and svc.queue.items[ak]["lane"] == L_V and svc.queue.items[ak]["state"] == "submitted"
      and ak in FW.jobs and FW.count("DELETE", "job", ak) == 0)
for n in "bcd":
    media("downloads", "vw/%s.mp4" % n, 4000 + ord(n))
    svc.view("downloads", "vw/%s.mp4" % n, "alice")
check("running: a running item does not count against the bound and is never demoted by it",
      svc.queue.items[ak]["lane"] == L_V and sum(1 for i in svc.queue.items.values() if i["lane"] == L_V and i["state"] == "queued") == 3)
svc.view("downloads", "vw/e.mp4", "alice")
check("running: a view of a running favourite does not change its lane",
      (lambda: (svc.request("downloads", "vw/f.mp4", L_F, "alice"), svc.queue.update(vk(svc, "downloads", "vw/f.mp4"), state="submitted"),
                svc.view("downloads", "vw/f.mp4", "bob")["status"], svc.queue.items[vk(svc, "downloads", "vw/f.mp4")]["lane"])[2:])()
      == ("duplicate", L_F))
FW.finish(ak)
svc.step()
check("running: the job finishes and is cached as usual", svc.index().files_ok(ak))
svc.clear_queue()

# gating: viewing counts like manual - it beats a favourite, holds a running favourite video, and the
# next job is the viewing one
svc, qpath = fresh()
svc.request("downloads", "vw/a.mp4", L_F, "alice")
svc.request("downloads", "vw/b.mp4", L_M, "alice")
svc.view("downloads", "vw/c.mp4", "alice")
drive(svc, 1)
check("gating: the first job started is the viewing item, ahead of manual and favourite",
      [k for k, i in svc.queue.items.items() if i["state"] == "submitted"] == [vk(svc, "downloads", "vw/c.mp4")])
svc.clear_queue()
svc, qpath = fresh()
svc.request("downloads", "vw/a.mp4", L_F, "alice")
fk = vk(svc, "downloads", "vw/a.mp4")
drive(svc, 1)
FW.set_state(fk, "converting", progress={"phase": "converting"})
svc.step()
check("gating: a running favourite video is left alone while nothing urgent waits", FW.count("POST", "hold") == 0)
svc.view("downloads", "vw/b.mp4", "alice")
svc.step()
check("gating: a viewing request holds the running favourite video like a manual one", FW.count("POST", "hold", fk) == 1
      and FW.jobs[fk]["state"] == "held")
svc.step()
vkey = vk(svc, "downloads", "vw/b.mp4")
check("gating: and the viewing item is uploaded next", FW.count("PUT", "source", vkey) == 1 and svc.queue.items[vkey]["state"] == "submitted")
FW.set_state(vkey, "converting")
svc.step()
check("gating: the held favourite stays held while a viewing job runs", FW.count("POST", "resume") == 0)
FW.finish(vkey)
svc.step()
svc.step()
check("gating: afterwards the favourite is resumed", FW.count("POST", "resume", fk) == 1)
svc.clear_queue()
svc, qpath = fresh()
svc.request("downloads", "vw/p1.jpg", L_F, "alice")
drive(svc, 1)
svc.view("downloads", "vw/e.mp4", "alice")
svc.step()
check("gating: a running favourite picture batch is not held for a viewing request", FW.count("POST", "hold") == 0)
svc.clear_queue()
svc, qpath = fresh()
media("downloads", "vw/p2.jpg", 3200)
media("downloads", "vw/p3.jpg", 3300)
svc.view("downloads", "vw/p1.jpg", "alice")
svc.view("downloads", "vw/p2.jpg", "bob")
svc.request("downloads", "vw/p3.jpg", L_M, "alice")
drive(svc, 1)
jobs = [j for j in FW.jobs.values()]
check("gating: picture batching groups one lane only (viewing pictures together, not with manual)",
      len(jobs) == 1 and len(jobs[0]["names"]) == 2, [j["names"] for j in jobs])
svc.clear_queue()

# ================================================================== the routes, settings and config of server.py
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


def resolve_names(thunk):
    for _ in range(400):
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
    resolve_names(lambda: exec(code, ns))


check("server.py imports stereo3d", "stereo3d" in {a.name for n in tree.body if isinstance(n, ast.Import) for a in n.names})
ns["stereo3d"] = stereo3d
ns["CONFIG_FILE"] = os.path.join(TMP, "config.json")
ns["INDEXER_CONFIG_FILE"] = os.path.join(TMP, "indexer_config.json")
ns["BASE_DIR"] = TMP
user = {"username": "alice", "role": "admin"}
ns["get_authenticated_user"] = lambda: user
for n in ("app", "DEFAULT_CONFIG", "load_config", "stereo3d_settings", "is_admin", "require_admin", "media_3d_list",
          "media_3d_status", "media_3d_convert", "media_3d_queue_delete", "media_3d_retry", "media_3d_file",
          "settings_3d", "settings_3d_cache_clear", "content_disposition_header", "get_settings", "put_settings",
          "_settings_field", "_coerce_setting", "settings_values", "SETTINGS_SCHEMA", "_settings_write_lock",
          "_atomic_json_write", "_dig", "_read_indexer_config", "auto_trash_reasons", "_stereo3d_clean_path",
          "media_3d_view", "_stereo3d_view_retry_after", "_STEREO3D_VIEW_RATE_PER_MIN", "_stereo3d_view_hits",
          "_stereo3d_view_lock"):
    load(n)
app = ns["app"]
ns["CONFIG"] = json.loads(json.dumps(ns["DEFAULT_CONFIG"]))
ns["DEFAULT_ROOT_ID"] = "downloads"

# ---- defaults, schema, config merge
d3 = ns["DEFAULT_CONFIG"]["stereo3d"]
check("defaults: the user's answers (2160 long side, 150 GB, 7 days backfill)", d3["max_eye_long_side"] == 2160
      and d3["cache_max_gb"] == 150 and d3["favorites_backfill_days"] == 7 and d3["divergence"] == 2.0
      and d3["convergence"] == 0.5 and d3["favorites_auto"] is True and (d3["mbps_1080"], d3["mbps_1440"], d3["mbps_2160"]) == (30, 50, 80), d3)
check("defaults: no vr_share / SMB settings in the stereo3d section", not any("share" in k for k in d3))
stereo_defaults_ast = next(v for k, v in zip(defs["DEFAULT_CONFIG"].value.keys,
                                           defs["DEFAULT_CONFIG"].value.values)
                           if isinstance(k, ast.Constant) and k.value == "stereo3d")
try:
    literal_defaults = ast.literal_eval(stereo_defaults_ast)
except (ValueError, TypeError):
    literal_defaults = None
check("defaults: no module reference needed (plain literals)", literal_defaults == d3
      and all(isinstance(v, (int, float, bool, str)) for k, v in d3.items() if k != "permanent_folders")
      and isinstance(d3.get("permanent_folders"), list)
      and all(isinstance(v, str) for v in d3["permanent_folders"]))
sections = {s["id"]: s for s in ns["SETTINGS_SCHEMA"]}
keys = [f["key"] for f in sections["stereo3d"]["fields"]]
check("schema: a stereo3d section covers every default key", sorted(k.split(".", 1)[1] for k in keys) == sorted(d3), keys)
for k in keys:
    f = ns["_settings_field"](k)
    check("schema: %s has a label and its default is accepted" % k, f and f.get("label")
          and ns["_coerce_setting"](f, d3[k.split(".", 1)[1]]) == d3[k.split(".", 1)[1]] or (k.endswith("cache_dir") and d3["cache_dir"] == ""))


def coerce(key, value):
    try:
        return ns["_coerce_setting"](ns["_settings_field"]("stereo3d." + key), value)
    except ValueError as exc:
        return exc


check("settings bounds: cache_max_gb 10..5000", isinstance(coerce("cache_max_gb", 9), ValueError) and coerce("cache_max_gb", 10) == 10
      and coerce("cache_max_gb", 5000) == 5000 and isinstance(coerce("cache_max_gb", 5001), ValueError)
      and isinstance(coerce("cache_max_gb", True), ValueError) and isinstance(coerce("cache_max_gb", "lots"), ValueError))
check("settings bounds: max_eye_long_side is one of 1440/2160/2560/3840 (a string from the page is accepted)",
      coerce("max_eye_long_side", "1440") == 1440 and coerce("max_eye_long_side", 3840) == 3840
      and isinstance(coerce("max_eye_long_side", 1080), ValueError) and isinstance(coerce("max_eye_long_side", 4000), ValueError))
check("settings bounds: divergence 1..5, convergence 0..1", coerce("divergence", 1) == 1 and coerce("divergence", 5) == 5
      and isinstance(coerce("divergence", 0.9), ValueError) and isinstance(coerce("divergence", 5.1), ValueError)
      and coerce("convergence", 0) == 0 and coerce("convergence", 1) == 1 and isinstance(coerce("convergence", 1.01), ValueError))
check("settings bounds: favorites_backfill_days 0..3650, max_attempts 1..10, mbps 5..300",
      coerce("favorites_backfill_days", 0) == 0 and isinstance(coerce("favorites_backfill_days", 3651), ValueError)
      and isinstance(coerce("favorites_backfill_days", -1), ValueError) and isinstance(coerce("max_attempts", 0), ValueError)
      and coerce("max_attempts", 10) == 10 and isinstance(coerce("max_attempts", 11), ValueError)
      and isinstance(coerce("mbps_2160", 4), ValueError) and coerce("mbps_2160", 300) == 300 and isinstance(coerce("mbps_1080", 301), ValueError))
check("settings bounds: booleans are booleans, cache_dir is text", isinstance(coerce("enabled", "yes"), ValueError)
      and isinstance(coerce("favorites_auto", 1), ValueError) and coerce("cache_dir", " /mnt/x ") == "/mnt/x")
# put_settings end to end into the temp config.json
with app.test_request_context("/api/settings", method="PUT", json={"values": {"stereo3d.cache_max_gb": 200, "stereo3d.max_eye_long_side": "2560"}}):
    resp = resolve_names(lambda: ns["put_settings"]())
saved = json.load(open(ns["CONFIG_FILE"]))
check("settings: PUT stores the stereo3d keys in config.json and stereo3d_settings() merges them over the defaults",
      saved["stereo3d"]["cache_max_gb"] == 200 and saved["stereo3d"]["max_eye_long_side"] == 2560
      and ns["stereo3d_settings"]()["cache_max_gb"] == 200 and ns["stereo3d_settings"]()["divergence"] == 2.0, saved.get("stereo3d"))
with app.test_request_context("/api/settings", method="PUT", json={"values": {"stereo3d.cache_max_gb": 1, "stereo3d.divergence": 2.5}}):
    resp = resolve_names(lambda: ns["put_settings"]())
status = resp[1] if isinstance(resp, tuple) else resp.status_code
check("settings: an out-of-range value rejects the whole PUT, nothing written",
      status == 400 and json.load(open(ns["CONFIG_FILE"]))["stereo3d"].get("divergence") != 2.5)
# load_config merges a partial stereo3d block instead of replacing it
with open(ns["CONFIG_FILE"], "w") as fh:
    json.dump({"stereo3d": {"cache_max_gb": 99}}, fh)
cfg = ns["load_config"]()
check("config: load_config deep-merges the stereo3d block (partial file keeps the other defaults)",
      cfg["stereo3d"]["cache_max_gb"] == 99 and cfg["stereo3d"]["divergence"] == 2.0 and cfg["stereo3d"]["max_eye_long_side"] == 2160, cfg["stereo3d"])

# ---- routes against the real service and fake worker
svc, qpath = fresh()
ns["stereo3d_service"] = lambda: svc
ns["CONFIG"] = {"stereo3d": dict(CFG)}
VISIBLE = {"alice": {"downloads", "jellyfin"}, "guest1": {"downloads"}, None: set()}
CUR = {"user": {"username": "alice", "role": "admin"}}
ns["get_authenticated_user"] = lambda: CUR["user"]
ns["root_visible_to"] = lambda u, root: root in VISIBLE.get((u or {}).get("username"), set())
ns["user_favorites"] = lambda name: {"downloads": {"r/route.mp4": 1.0}} if name == "alice" else {}
ns["stereo3d_settings"] = lambda: dict(CFG)
client = app.test_client()


def as_user(name):
    CUR["user"] = {"username": name, "role": "admin" if name == "alice" else "guest"} if name else None


def do(method, url, **kw):
    return getattr(client, method)(url, **kw)


media("downloads", "r/route.mp4", 20000)
media("jellyfin", "j/other.mp4", 20000)
as_user("alice")
r = do("post", "/api/media/3d/convert", json={"items": [{"root": "downloads", "path": "r/route.mp4"}, {"root": "jellyfin", "path": "j/other.mp4"}]})
check("route convert: admin queues, counts per status, order kept", r.status_code == 200 and r.get_json()["counts"] == {"queued": 2}
      and [i["path"] for i in svc.queue.ordered()] == ["r/route.mp4", "j/other.mp4"], r.get_json())
check("route convert: bad bodies are 400", do("post", "/api/media/3d/convert", json={"items": []}).status_code == 400
      and do("post", "/api/media/3d/convert", json={"items": [{"root": "downloads"}]}).status_code == 400
      and do("post", "/api/media/3d/convert", json={"items": [5]}).status_code == 400
      and do("post", "/api/media/3d/convert", json={"items": [{"path": "x.mp4"}] * 501}).status_code == 400)
r = do("post", "/api/media/3d/convert", json={"items": [{"root": "downloads", "path": "../../etc/passwd.mp4"}, {"root": "downloads", "path": "no.mp4"}]})
check("route convert: traversal and missing files are skipped, nothing queued", r.status_code == 200
      and r.get_json()["counts"] == {"skipped": 2} and len(svc.queue.items) == 2, r.get_json())
st = do("get", "/api/media/3d/status?root=downloads&path=r/route.mp4").get_json()
check("route status: queued with position", st["state"] == "queued" and st["position"] == 1 and st["ahead"] == 0, st)
check("route status: unknown file is none, audio unsupported", do("get", "/api/media/3d/status?root=downloads&path=zzz.mp4").get_json()["state"] == "none"
      and do("get", "/api/media/3d/status?root=downloads&path=a.mp3").get_json()["state"] == "unsupported")
check("route status: path required", do("get", "/api/media/3d/status?root=downloads").status_code == 400)
# a finished conversion
drive(svc, 1)
rk = next(k for k, i in svc.queue.items.items() if i["state"] == "submitted")
FW.finish(rk)
svc.step()
FW.jobs.clear()
rows = do("get", "/api/media/3d/list").get_json()
row = rows["items"][0] if rows["items"] else {}
check("route list: finished conversion with urls, size, favourite flag", len(rows["items"]) == 1 and row["path"] == "r/route.mp4"
      and row["title"] == "route" and row["kind"] == "video" and row["sbs_url"].endswith("&name=sbs.mp4")
      and row["depth_url"].endswith("&name=depth.mp4") and row["eye_w"] == 1920 and row["favourite"] is True
      and row["bytes"] > 0 and row["key"] == rk and row["source_missing"] is False, rows)
r = do("get", row["sbs_url"], headers={"Range": "bytes=0-9"})
check("route file: 206 with Content-Range for a range request", r.status_code == 206 and len(r.data) == 10
      and r.headers["Content-Range"].startswith("bytes 0-9/") and r.mimetype == "video/mp4", (r.status_code, dict(r.headers)))
r = do("get", row["sbs_url"], headers={"Range": "bytes=100-199"})
check("route file: a later range works and is not a play start", r.status_code == 206 and r.data[:1] == bytes([7]), r.status_code)
r = do("get", row["depth_url"])
check("route file: depth.mp4 served whole (200)", r.status_code == 200 and r.headers["Accept-Ranges"] == "bytes")
r = do("get", "/api/media/3d/file?key=%s&name=meta.json" % rk)
check("route file: meta.json served as JSON", r.status_code == 200 and json.loads(r.data)["eye_w"] == 1920)
r = do("get", row["sbs_url"] + "&download=1")
check("route file: ?download=1 names it <title>_Full_SBS.mp4 for SKYBOX", "route_Full_SBS.mp4" in r.headers.get("Content-Disposition", ""), r.headers.get("Content-Disposition"))
check("route file: a play start through the route counted by the watched clock", svc.index().get(rk)["watch_count"] >= 1)
check("route file: unknown name / key / traversal refused", do("get", "/api/media/3d/file?key=%s&name=../index.json" % rk).status_code == 400
      and do("get", "/api/media/3d/file?key=%s&name=sbs.jpg" % rk).status_code == 404
      and do("get", "/api/media/3d/file?key=%s&name=sbs.mp4" % ("0" * 32)).status_code == 404
      and do("get", "/api/media/3d/file?key=..%%2F..%%2Fx&name=sbs.mp4").status_code == 404)
# root visibility
e = svc.index().get(rk)
e["root"] = "jellyfin"
as_user("guest1")
check("route list: a guest sees no conversions of a root hidden from them", do("get", "/api/media/3d/list").get_json()["items"] == [])
check("route file: a hidden root's file is 403 for a guest", do("get", row["sbs_url"]).status_code == 403)
check("route status: a hidden root is 403 for a guest", do("get", "/api/media/3d/status?root=jellyfin&path=j/other.mp4").status_code == 403)
e["root"] = "downloads"
check("route list/file: a visible root works for a guest", len(do("get", "/api/media/3d/list").get_json()["items"]) == 1
      and do("get", row["sbs_url"]).status_code == 200)
check("route list: a guest has no favourite flag", do("get", "/api/media/3d/list").get_json()["items"][0]["favourite"] is False)
# auth
check("route convert: a guest is 403", do("post", "/api/media/3d/convert", json={"items": [{"root": "downloads", "path": "r/route.mp4"}]}).status_code == 403)
check("route queue/retry/settings/cache: a guest is 403", do("delete", "/api/media/3d/queue?all=1").status_code == 403
      and do("post", "/api/media/3d/retry", json={"all": True}).status_code == 403
      and do("get", "/api/settings/3d").status_code == 403 and do("delete", "/api/settings/3d/cache").status_code == 403)
as_user(None)
unauth = [do("get", "/api/media/3d/list").status_code, do("get", "/api/media/3d/status?root=downloads&path=a.mp4").status_code,
          do("get", row["sbs_url"]).status_code, do("post", "/api/media/3d/convert", json={"items": [{"root": "downloads", "path": "x.mp4"}]}).status_code,
          do("delete", "/api/media/3d/queue?all=1").status_code, do("post", "/api/media/3d/retry", json={"all": True}).status_code,
          do("get", "/api/settings/3d").status_code, do("delete", "/api/settings/3d/cache").status_code]
check("routes: every one is 401 without a login", unauth == [401] * 8, unauth)
as_user("alice")
# queue delete / retry / settings
r = do("delete", "/api/media/3d/queue")
check("route queue: DELETE needs a key or all", r.status_code == 400)
qk = next(iter(svc.queue.items))
r = do("delete", "/api/media/3d/queue?key=" + qk)
check("route queue: DELETE ?key= removes one item", r.get_json() == {"removed": 1} and qk not in svc.queue.items)
media("downloads", "r/two.mp4", 3100)
do("post", "/api/media/3d/convert", json={"items": [{"root": "downloads", "path": "r/two.mp4"}]})
tk = next(iter(svc.queue.items))
svc.queue.update(tk, state="failed", attempts=2, error="boom")
r = do("post", "/api/media/3d/retry", json={"key": tk})
check("route retry: a failed item is queued again", r.get_json() == {"retried": 1} and svc.queue.items[tk]["state"] == "queued")
check("route retry: needs key or all", do("post", "/api/media/3d/retry", json={}).status_code == 400)
ov = do("get", "/api/settings/3d").get_json()
check("route settings/3d: queue, cache and worker info", ov["enabled"] is True and ov["cache"]["entries"] == 1
      and ov["cache"]["dir"] == CACHE and any(i["key"] == tk for i in ov["items"]) and "worker" in ov and ov["cache"]["max_gb"] == CFG["cache_max_gb"], ov)
r = do("delete", "/api/media/3d/queue?all=1")
check("route queue: DELETE ?all=1 empties the queue", r.get_json()["removed"] >= 1 and not svc.queue.items)
r = do("delete", "/api/settings/3d/cache")
check("route settings/3d/cache: DELETE clears the cache", r.get_json() == {"removed": 1} and not svc.index().entries)

# ---- POST /api/media/3d/view (P8 V1): any signed-in role that sees the root, same answer as /status
svc, qpath = fresh()
ns["stereo3d_service"] = lambda: svc
VISIBLE["user1"] = {"downloads"}
ns["_stereo3d_view_hits"].clear()
for rel in ("v/one.mp4", "v/two.mp4", "v/three.mp4", "v/pic.jpg"):
    media("downloads", rel, 6000 + len(rel))
media("jellyfin", "j/hidden.mp4", 6100)


def as_role(name, role):
    CUR["user"] = {"username": name, "role": role} if name else None


def view(root, path, leave=None, **extra):
    body = {"root": root, "path": path}
    if leave is not None:
        body["leave"] = leave
    body.update(extra)
    return do("post", "/api/media/3d/view", json=body)


def status_of(root, path):
    return do("get", "/api/media/3d/status?root=%s&path=%s" % (root, path)).get_json()


for who, role, rel in (("alice", "admin", "v/one.mp4"), ("guest1", "guest", "v/two.mp4"), ("user1", "user", "v/three.mp4")):
    as_role(who, role)
    r = view("downloads", rel)
    st = status_of("downloads", rel)
    check("route view: %s (%s) may request it for a visible root: queued in the viewing lane, answer = /status" % (who, role),
          r.status_code == 200 and r.get_json() == st and st["state"] == "queued" and st["lane"] == "viewing"
          and st.get("position") is not None and "key" in st, (r.status_code, r.get_json(), st))
as_role("alice", "admin")
check("route view: the queue holds the three items with their viewer",
      sorted((i["path"], list(i["viewers"])[0]) for i in svc.queue.items.values())
      == [("v/one.mp4", "alice"), ("v/three.mp4", "user1"), ("v/two.mp4", "guest1")])
r = view("downloads", "v/pic.jpg")
check("route view: a picture works too", r.status_code == 200 and r.get_json()["state"] == "queued")
r = view("downloads", "v/one.mp4")
check("route view: asking again is harmless and answers the same shape", r.status_code == 200 and r.get_json()["state"] == "queued"
      and r.get_json()["lane"] == "viewing" and len(svc.queue.items) == 4)

# a finished conversion: same shape as /status (state done, key)
dk = vk(svc, "downloads", "v/one.mp4")
drive(svc, 1)
FW.finish(dk)
svc.step()
FW.jobs.clear()
r = view("downloads", "v/one.mp4")
st = status_of("downloads", "v/one.mp4")
check("route view: an item that is already 3D answers done with its key and queues nothing", r.get_json() == st
      and st["state"] == "done" and st["key"] == dk and dk not in svc.queue.items, (r.get_json(), st))
as_role("guest1", "guest")
st_g = status_of("downloads", "v/one.mp4")
check("route view: a guest gets the same done answer", view("downloads", "v/one.mp4").get_json() == st_g and st_g["state"] == "done")
as_role("alice", "admin")

# visibility, traversal, kind, bad bodies
n0 = len(svc.queue.items)
as_role("guest1", "guest")
r = view("jellyfin", "j/hidden.mp4")
check("route view: a root hidden from the caller is 403 and nothing is queued", r.status_code == 403
      and len(svc.queue.items) == n0 and not any(i["root"] == "jellyfin" for i in svc.queue.items.values()))
check("route view: an unknown root is 403 too", view("nowhere", "x.mp4").status_code == 403)
as_role("alice", "admin")
open(os.path.join(TMP, "outside.mp4"), "wb").write(b"x" * 500)
r = view("downloads", "../../outside.mp4")
check("route view: path traversal to an EXISTING file outside the root queues nothing (state none, file not found)", r.status_code == 200
      and r.get_json().get("state") == "none" and len(svc.queue.items) == n0, r.get_json())
r = view("downloads", "..\\..\\etc\\passwd.mp4")
check("route view: backslash traversal queues nothing", r.get_json().get("state") == "none" and len(svc.queue.items) == n0)
r = view("downloads", "/etc/passwd.mp4")
check("route view: an absolute path is treated as inside the root (no such file)", r.get_json().get("state") == "none"
      and len(svc.queue.items) == n0)
check("route view: a non-media kind is 400", view("downloads", "v/song.mp3").status_code == 400
      and view("downloads", "v/readme.txt").status_code == 400 and len(svc.queue.items) == n0)
check("route view: a missing path is 400, a non-JSON body is 400", view("downloads", "").status_code == 400
      and do("post", "/api/media/3d/view", data="nope").status_code == 400
      and do("post", "/api/media/3d/view", json=[1, 2]).status_code == 400
      and do("post", "/api/media/3d/view", json={"root": "downloads", "path": 5}).status_code == 400)
check("route view: a missing file is the same answer /status gives (none), nothing queued",
      view("downloads", "v/nope.mp4").get_json() == status_of("downloads", "v/nope.mp4") and len(svc.queue.items) == n0)
as_role(None, None)
check("route view: 401 without a login", do("post", "/api/media/3d/view", json={"root": "downloads", "path": "v/one.mp4"}).status_code == 401)
as_role("alice", "admin")

# settings: 3D off, view_auto off, admin only
CFG["enabled"] = False
r = view("downloads", "v/two.mp4")
check("route view: 3D conversion off is 409", r.status_code == 409 and r.get_json()["error"])
CFG["enabled"] = True
CFG["view_auto"] = False
n1 = len(svc.queue.items)
for who, role in (("alice", "admin"), ("guest1", "guest"), ("user1", "user")):
    as_role(who, role)
    r = view("downloads", "v/two.mp4")
    check("route view: view_auto off refuses %s (403), nothing queued" % who, r.status_code == 403 and len(svc.queue.items) == n1)
as_role("alice", "admin")
check("route view: view_auto off also refuses a leave", view("downloads", "v/two.mp4", leave=True).status_code == 403)
CFG["view_auto"] = True
media("downloads", "v/four.mp4", 6200)
CFG["view_auto_admin_only"] = True
n2 = len(svc.queue.items)
for who, role in (("guest1", "guest"), ("user1", "user")):
    as_role(who, role)
    r = view("downloads", "v/four.mp4")
    check("route view: admin-only on refuses %s (403), nothing queued" % who, r.status_code == 403 and len(svc.queue.items) == n2)
as_role("alice", "admin")
r = view("downloads", "v/four.mp4")
check("route view: admin-only on still lets an admin through", r.status_code == 200 and r.get_json()["state"] == "queued")
CFG["view_auto_admin_only"] = False
check("route view: defaults are view_auto on, admin-only off, bound 3",
      ns["DEFAULT_CONFIG"]["stereo3d"]["view_auto"] is True and ns["DEFAULT_CONFIG"]["stereo3d"]["view_auto_admin_only"] is False
      and ns["DEFAULT_CONFIG"]["stereo3d"]["viewing_max_per_user"] == 3)

# leave
as_role("guest1", "guest")
r = view("downloads", "v/two.mp4", leave=True)
st = status_of("downloads", "v/two.mp4")
k2 = vk(svc, "downloads", "v/two.mp4")
check("route view: leave:true demotes to the end of the manual lane and answers /status's shape",
      r.status_code == 200 and r.get_json() == st and st["lane"] == "manual" and st["state"] == "queued"
      and svc.queue.ordered()[-1]["key"] == k2, (r.get_json(), st))
check("route view: leave:true for a hidden root is 403, for a non-media kind 400",
      (lambda: (as_role("guest1", "guest"), view("jellyfin", "j/hidden.mp4", leave=True).status_code)[1])() == 403
      and view("downloads", "v/song.mp3", leave=True).status_code == 400)
check("route view: leave:false is a view", view("downloads", "v/two.mp4", leave=False).get_json()["lane"] == "viewing")
as_role("alice", "admin")

# rate limit: per user, 30 a minute, 429 with retry_after_s; other users unaffected
ns["_stereo3d_view_hits"].clear()
codes = [view("downloads", "v/three.mp4").status_code for _ in range(32)]
r = view("downloads", "v/three.mp4")
check("route view: 30 requests a minute pass, the 31st is 429", codes[:30] == [200] * 30 and codes[30:] == [429, 429]
      and r.status_code == 429, codes)
body = r.get_json()
check("route view: the 429 names retry_after_s (1..60) and sets Retry-After",
      isinstance(body.get("retry_after_s"), int) and 1 <= body["retry_after_s"] <= 60
      and r.headers.get("Retry-After") == str(body["retry_after_s"]), (body, dict(r.headers)))
as_role("guest1", "guest")
check("route view: another user is not limited by it", view("downloads", "v/three.mp4").status_code == 200)
as_role("alice", "admin")
retry = ns["_stereo3d_view_retry_after"]
t0 = time.time()
ns["_stereo3d_view_hits"].clear()
for _ in range(30):
    retry("rate_user", t0)
check("route view: the window slides - blocked at +30 s, free again after a minute",
      retry("rate_user", t0 + 30) > 0 and retry("rate_user", t0 + 59) > 0 and retry("rate_user", t0 + 60.5) == 0)
check("route view: refused requests are not counted (a 429 does not extend the block)",
      (lambda: (ns["_stereo3d_view_hits"].clear(), [retry("u2", t0) for _ in range(30)], [retry("u2", t0 + 20) for _ in range(50)],
                retry("u2", t0 + 61))[3])() == 0)
ns["_stereo3d_view_hits"].clear()
svc.clear_queue()

# ---- the cache folder is never touched when it is missing: routes answer, nothing is created
CFG["cache_dir"] = os.path.join(TMP, "still_missing")
ns["stereo3d_settings"] = lambda: dict(CFG)
check("routes with no cache folder: list is empty, settings/3d reports the problem, nothing created",
      do("get", "/api/media/3d/list").get_json()["items"] == []
      and "does not exist" in do("get", "/api/settings/3d").get_json()["cache"]["problem"]
      and not os.path.exists(CFG["cache_dir"]))
CFG["cache_dir"] = CACHE

# ---- threads and wiring present in the source
src = open(SERVER, encoding="utf-8").read()
check("wiring: dispatcher and maintenance threads are started", "target=_stereo3d_loop" in src and "target=_stereo3d_maintenance_loop" in src)
check("wiring: the service is built with the app dir and /data2 forbidden", 'forbidden_dirs=(BASE_DIR, "/data2")' in src)
check("wiring: no vr_share/SMB hook in the stereo3d code", "_vr_sync_now" not in src[src.index("# 2D -> 3D conversion (stereo3d.py)"):src.index("# Settings page (web_files")])

shutil.rmtree(TMP, ignore_errors=True)
print("ALL PASS" if all(RESULTS) else f"{RESULTS.count(False)} FAILED")
