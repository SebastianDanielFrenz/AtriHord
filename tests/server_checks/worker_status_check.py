"""The admin worker-status view (GET /api/media/worker-status) and the tag bar of
/api/media/indexing, out of a STAGED server.py's AST in Flask test requests,
against a fake vision worker on localhost. Nothing outside a temp folder is
read or written, and no address, host name or model name of any real install is
used.

Covered: admin only; every source missing (no status file, no worker, no 3D
add-on, no queue file) still answers; the worker's token reaches the worker but
never the browser; the indexer's wait reason and next look; the worker's 3D job
and gate; the server's own 3D queue by state; tag progress counted against the
tagger the worker names (any stamp only when it names none); the worker calls
are cached.

  venv/bin/python worker_status_check.py <staged server.py>
"""
import ast
import json
import os
import shutil
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BASE = os.environ.get("ATRIHORD_DIR") or os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SERVER = os.path.abspath(sys.argv[1])
STAGE = os.path.dirname(SERVER)
sys.path.insert(0, BASE)
sys.path.insert(0, STAGE)
tmp = tempfile.mkdtemp(prefix="worker_status_check_")
store = os.path.join(tmp, "ai")
os.makedirs(store)
os.environ["KSF_MEDIA_AI_DIR"] = store
os.environ.pop("MEDIA_VISION_WORKER", None)
with open(os.path.join(tmp, "config.json"), "w") as fh:
    fh.write("{}")

import media_ai                      # noqa: E402

ok = True


def check(label, cond, detail=""):
    global ok
    ok = ok and bool(cond)
    print(("PASS " if cond else "FAIL ") + label + (f"  ({detail})" if detail and not cond else ""))


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
    resolve(lambda: exec(code, ns))


ns["CONFIG_FILE"] = os.path.join(tmp, "config.json")
ADMIN = {"username": "alice", "role": "admin"}
GUEST = {"username": "guest", "role": "guest"}
CURRENT = {"user": ADMIN}
ns["get_authenticated_user"] = lambda: CURRENT["user"]
# BASE_DIR first: helpers that catch their own exceptions hide a NameError.
for name in ("BASE_DIR", "CONFIG", "DEFAULT_CONFIG", "app", "DEFAULT_WORKER_URL", "is_admin", "require_admin",
             "WORKER_CALL_CACHE_S", "_worker_call_cache", "_worker_call_lock", "_worker_config", "worker_call",
             "vision_worker_status", "stereo_queue_counts", "_worker_status_cache", "indexer_heartbeat",
             "indexer_wait_reason", "media_worker_status", "media_indexing_status", "_indexing_cache",
             "INDEXING_CACHE_S", "stereo3d_settings"):
    load(name)
ns["BASE_DIR"] = tmp                                   # every file the server reads sits here
ns["STEREO3D_QUEUE_FILE"] = os.path.join(tmp, "stereo3d_queue.json")
ns["library_file_count"] = lambda rows: 0
ns["get_media_index"] = lambda: []
app = ns["app"]
check("server imports the staged media_ai", ns.get("media_ai") is media_ai)

# ---------------------------------------------------------------- a fake vision worker
TOKEN = "s3cret-token-value"
HITS = {"status": 0, "stereo": 0, "auth_seen": []}
WORKER = {"tagger": {"id": "tagger-new", "source": "test", "thresholds": {}},
          "stereo_code": 200, "stereo": {}}


class Fake(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def reply(self, code, body):
        raw = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        HITS["auth_seen"].append(self.headers.get("Authorization"))
        if self.path == "/v1/status":
            HITS["status"] += 1
            body = {"paused": False, "loaded": ["tagger", "faces"], "last_error": None, "idle_for_s": 12,
                    "seconds_per_image": 0.5, "upscales": 7, "upscale_waiting": 1, "in_window": True,
                    "windows": "always", "vram_allocated_mb": 4096, "images": 10, "batches": 2}
            if WORKER["tagger"]:
                body["tagger"] = WORKER["tagger"]
            self.reply(200, body)
        elif self.path == "/v1/stereo/status":
            HITS["stereo"] += 1
            if self.headers.get("Authorization") != "Bearer " + TOKEN:
                self.reply(401, {"error": "unauthorised"})
            elif WORKER["stereo_code"] != 200:
                self.reply(WORKER["stereo_code"], {"error": "no"})
            else:
                self.reply(200, WORKER["stereo"])
        else:
            self.reply(404, {})


httpd = ThreadingHTTPServer(("127.0.0.1", 0), Fake)
threading.Thread(target=httpd.serve_forever, daemon=True).start()
URL = "http://127.0.0.1:%d" % httpd.server_address[1]
STEREO_OK = {"available": True, "missing": [], "paused": False,
             "running": {"key": "k1", "kind": "video", "label": "holiday.mp4", "files": 1, "running_s": 340,
                         "state": "converting", "progress": {"phase": "x"}},
             "waiting": [{"key": "a"}, {"key": "b"}], "held": [], "counts": {"converting": 1, "waiting_gpu": 2},
             "disk_free_gb": 501.4, "min_free_gb": 20, "nunif_commit": "abc", "work_gb": 1.0,
             "gate": {"open": False, "reason": "the card is busy", "manager": {"secret": 1}}}


def reset():
    ns["_worker_call_cache"].clear()
    ns["_worker_status_cache"].update(at=0.0, data=None)
    ns["_indexing_cache"].update(at=0.0, data=None)
    HITS.update(status=0, stereo=0, auth_seen=[])


def write_cfg(**cfg):
    path = os.path.join(tmp, "indexer_config.json")
    with open(path, "w") as fh:
        json.dump(cfg, fh)


def beat(**fields):
    with open(os.path.join(store, "indexer_status.json"), "w") as fh:
        json.dump(fields, fh)


def get(path="/api/media/worker-status"):
    with app.test_request_context(path):
        resp = resolve(lambda: ns["media_worker_status" if "worker-status" in path else "media_indexing_status"]())
    if isinstance(resp, tuple):
        return resp[1], resp[0].get_json()
    return resp.status_code, resp.get_json()


# ---------------------------------------------------------------- a bare install: nothing configured
reset()
st, d = get()
check("a bare install still answers (200)", st == 200, st)
check("... no indexer has reported", d["indexer"]["seen"] is False and d["indexer"]["state"] == "stopped", d["indexer"])
check("... no worker is configured", d["worker"]["state"] == "not_configured", d["worker"])
check("... 3D: the worker and the queue are absent, not an error",
      d["stereo"]["worker"]["state"] == "not_configured" and d["stereo"]["queue"]["state"] == "none"
      and isinstance(d["stereo"]["enabled"], bool), d["stereo"])
check("... tag progress counts any stamp and says so",
      d["tagging"]["model"] is None and d["tagging"]["counted"] == "any stamp", d["tagging"])

# ---------------------------------------------------------------- access
CURRENT["user"] = GUEST
reset()
st, d = get()
check("a non-admin is refused (403)", st == 403, st)
CURRENT["user"] = None
st, d = get()
check("a signed-out visitor is refused (401)", st == 401, st)
CURRENT["user"] = ADMIN

# ---------------------------------------------------------------- the index the tag bar reads
conn = media_ai.connect(tmp)
for mid in range(1, 6):
    conn.execute("INSERT INTO media (id, root, path, kind, tag_model) VALUES (?,?,?,?,?)",
                 (mid, "downloads", "p/%d.jpg" % mid, "image", "tagger-new" if mid <= 3 else "tagger-old"))
conn.execute("INSERT INTO media (id, root, path, kind, tag_model) VALUES (6, 'downloads', 'p/6.jpg', 'image', NULL)")
conn.execute("INSERT INTO media (id, root, path, kind, tag_model, trashed) VALUES (7, 'downloads', 'p/7.jpg', 'image', 'tagger-new', 1.0)")
conn.commit()

# ---------------------------------------------------------------- a reachable worker
write_cfg(worker=URL, token=TOKEN)
WORKER["stereo"] = STEREO_OK
with open(os.path.join(tmp, "stereo3d_queue.json"), "w") as fh:
    json.dump({"version": 1, "items": [{"key": "a" * 8, "state": "queued"}, {"key": "b" * 8, "state": "queued"},
                                       {"key": "c" * 8, "state": "failed"}, "junk"], "evicted": {}}, fh)
beat(at=time.time(), state="waiting-for-window", task="tags", until=time.time() + 600,
     detail=json.dumps({"error": "3D conversion running", "retry_after_s": 600}))
reset()
st, d = get()
raw = json.dumps(d)
check("with a worker: 200 and the worker is ok", st == 200 and d["worker"]["state"] == "ok", (st, d["worker"]))
check("... its numbers come through (loaded models, idle, upscales, vram)",
      d["worker"]["loaded"] == ["tagger", "faces"] and d["worker"]["idle_for_s"] == 12
      and d["worker"]["upscales"] == 7 and d["worker"]["upscale_waiting"] == 1
      and d["worker"]["vram_allocated_mb"] == 4096 and d["worker"]["seconds_per_image"] == 0.5, d["worker"])
check("... the token went to the worker as a Bearer header", ("Bearer " + TOKEN) in HITS["auth_seen"], HITS["auth_seen"])
check("... and is nowhere in what the browser gets", TOKEN not in raw and "token" not in raw.lower(), "token leaked")
check("... nor the worker's address", URL not in raw and "127.0.0.1" not in raw, "address leaked")
check("the indexer: waiting, and WHY (the worker's own words)",
      d["indexer"]["state"] == "waiting" and d["indexer"]["why"] == "3D conversion running", d["indexer"])
check("... and when it looks again", 590 <= (d["indexer"]["retry_in_s"] or 0) <= 600, d["indexer"]["retry_in_s"])
sw = d["stereo"]["worker"]
check("3D on the worker: the running job, its counts and the gate",
      sw["state"] == "ok" and sw["running"] == {"label": "holiday.mp4", "kind": "video", "files": 1,
                                                "running_s": 340, "state": "converting"}
      and sw["counts"] == {"converting": 1, "waiting_gpu": 2} and sw["waiting"] == 2
      and sw["gate"] == {"open": False, "reason": "the card is busy"} and sw["disk_free_gb"] == 501.4, sw)
check("... the gate's manager internals are not passed on", "secret" not in raw, "manager leaked")
check("the server's own 3D queue by state (junk rows ignored)",
      d["stereo"]["queue"] == {"state": "ok", "counts": {"queued": 2, "failed": 1}, "total": 3}, d["stereo"]["queue"])
check("tag progress: files with the worker's tagger of those still in the library (3 of 6)",
      d["tagging"] == {"model": "tagger-new", "counted": "model", "done": 3, "total": 6}, d["tagging"])

# the old indexing route reads the same worker and counts the same way
reset()
st, ix = get("/api/media/indexing")
check("/api/media/indexing: tags_done counts the worker's tagger (3), not any stamp (5)",
      st == 200 and ix.get("tags_done") == 3 and ix.get("tag_model") == "tagger-new"
      and ix["worker"].get("reachable") is True, {k: ix.get(k) for k in ("tags_done", "tag_model")})

# ---------------------------------------------------------------- cached
reset()
get()
first = (HITS["status"], HITS["stereo"])
get()
check("the worker is asked once per window, however often the page polls", (HITS["status"], HITS["stereo"]) == first, HITS)
ns["_worker_status_cache"].update(at=0.0, data=None)
get()
check("... and the answers are reused for the whole payload too (call cache holds)",
      (HITS["status"], HITS["stereo"]) == first, HITS)

# ---------------------------------------------------------------- a worker that names no tagger
WORKER["tagger"] = None
reset()
st, d = get()
check("no tagger named: any stamp is counted (5 of 6) and the page says so",
      d["tagging"] == {"model": None, "counted": "any stamp", "done": 5, "total": 6}, d["tagging"])
reset()
st, ix = get("/api/media/indexing")
check("... and /api/media/indexing falls back the same way", ix.get("tags_done") == 5 and ix.get("tag_model") is None,
      ix.get("tags_done"))
WORKER["tagger"] = {"id": "tagger-new", "source": "test", "thresholds": {}}

# ---------------------------------------------------------------- the worker's answers when things are wrong
write_cfg(worker=URL, token="wrong")
reset()
st, d = get()
check("a wrong token: the 3D part says refused, the open status part still works",
      d["stereo"]["worker"]["state"] == "refused" and d["worker"]["state"] == "ok", (d["stereo"]["worker"], d["worker"]["state"]))
write_cfg(worker=URL, token=TOKEN)
WORKER["stereo_code"] = 404
reset()
st, d = get()
check("a worker without 3D (404): not offered, no error",
      d["stereo"]["worker"]["state"] == "unavailable", d["stereo"]["worker"])
WORKER["stereo_code"] = 200
WORKER["stereo"] = {"available": False, "missing": ["nunif"]}
reset()
st, d = get()
check("3D installed but not available: unavailable, and what is missing",
      d["stereo"]["worker"]["state"] == "unavailable" and d["stereo"]["worker"]["missing"] == ["nunif"], d["stereo"]["worker"])
WORKER["stereo"] = STEREO_OK
dead = ThreadingHTTPServer(("127.0.0.1", 0), Fake)
dead_url = "http://127.0.0.1:%d" % dead.server_address[1]
dead.server_close()
write_cfg(worker=dead_url, token=TOKEN)
reset()
st, d = get()
check("a configured worker that does not answer: not reachable, the page still loads",
      st == 200 and d["worker"]["state"] == "unreachable" and d["stereo"]["worker"]["state"] == "unreachable"
      and d["indexer"]["state"] == "waiting", (st, d["worker"], d["stereo"]["worker"]))
reset()
st, ix = get("/api/media/indexing")
check("... and /api/media/indexing still answers with the worker marked unreachable",
      st == 200 and ix["worker"] == {"reachable": False} and ix.get("tag_model") is None, ix.get("worker"))
write_cfg(worker=URL, token=TOKEN)

# ---------------------------------------------------------------- the indexer's states
reset()
beat(at=time.time(), state="waiting-for-window", task="tags", until=time.time() + 300, detail="outside the hours")
d = get()[1]["indexer"]
check("a plain-text reason is shown as written", d["state"] == "waiting" and d["why"] == "outside the hours", d)
beat(at=time.time(), state="waiting-for-window", task="tags", until=time.time() + 300)
reset()
d = get()[1]["indexer"]
check("no reason given: empty, and the retry time is still there", d["why"] == "" and d["retry_in_s"] is not None, d)
beat(at=time.time(), state="indexing", task="faces", outstanding=10, done=4, failed=1)
reset()
d = get()[1]["indexer"]
check("indexing: what is left of the sweep (5), no wait reason",
      d["state"] == "indexing" and d["left"] == 5 and d["why"] == "" and d["retry_at"] is None and d["task"] == "faces", d)
beat(at=time.time() - 3600, state="indexing", task="faces", outstanding=10, done=4)
reset()
d = get()[1]["indexer"]
check("a heartbeat an hour old: not running", d["state"] == "stopped" and d["seen"] is True, d)
with open(os.path.join(store, "indexer_status.json"), "w") as fh:
    fh.write("not json")
reset()
st, body = get()
check("a broken status file does not break the page", st == 200 and body["indexer"]["seen"] is False, st)

# ---------------------------------------------------------------- a broken queue file
with open(os.path.join(tmp, "stereo3d_queue.json"), "w") as fh:
    fh.write("{")
reset()
st, d = get()
check("an unreadable 3D queue file is reported, the rest still shows",
      st == 200 and d["stereo"]["queue"]["state"] == "unreadable" and d["worker"]["state"] == "ok", d["stereo"]["queue"])

httpd.shutdown()
shutil.rmtree(tmp, ignore_errors=True)
print("ALL PASS" if ok else "SOME CHECKS FAILED")
sys.exit(0 if ok else 1)
