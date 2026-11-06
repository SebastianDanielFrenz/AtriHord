"""A STAGED server.py, out of its AST in Flask test requests: the indexing bars'
total no longer counts the running pass's stored files twice, the planner hands
the worker its bounds, _run_upscale asks for keep_larger with them, and the
cache entry records the scale actually served. Synthetic AI index, temp config
copies, the worker call intercepted. Nothing under /data2 is written.

  venv/bin/python keep_server_check.py /tmp/keep/server.py
"""
import ast
import json
import os
import shutil
import sys
import tempfile
import time
import urllib.request

BASE = os.environ.get("ATRIHORD_DIR") or os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SERVER = sys.argv[1]
STAGE = os.path.dirname(os.path.abspath(SERVER))
sys.path.insert(0, BASE)
sys.path.insert(0, STAGE)
tmp = tempfile.mkdtemp(prefix="keep_check_")
store = os.path.join(tmp, "ai")
os.makedirs(store)
os.environ["KSF_MEDIA_AI_DIR"] = store
for name in ("config.json", "indexer_config.json"):
    if os.path.exists(os.path.join(BASE, name)):
        shutil.copy(os.path.join(BASE, name), os.path.join(tmp, name))

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
ns["get_authenticated_user"] = lambda: {"username": "alice", "role": "admin"}
# BASE_DIR first: routes that catch their own exceptions hide a NameError.
for name in ("BASE_DIR", "CONFIG", "DEFAULT_CONFIG", "app", "_read_roots_unlocked", "media_indexing_status",
             "_indexing_cache", "INDEXING_CACHE_S", "plan_upscale", "_run_upscale", "resolve_media_path",
             "choose_upscale_model", "upscale_settings", "photo_upscale_model", "UPSCALE_PHOTO_MODELS"):
    load(name)
app = ns["app"]
ns["get_media_index"] = lambda: []
check("server imports the staged media_ai", ns.get("media_ai") is media_ai)

# ---------------------------------------------------------------- indexing total
conn = media_ai.connect(tmp)
for mid in range(1, 6):
    conn.execute("INSERT INTO media (id, root, path, kind, tag_model, face_model) VALUES (?,?,?,?,?,?)",
                 (mid, "downloads", "p/%d.jpg" % mid, "image", "wd", "insightface" if mid <= 2 else None))
conn.execute("INSERT INTO media (id, root, path, kind, tag_model, trashed) VALUES (6, 'downloads', 'p/6.jpg', 'image', 'wd', 1.0)")
conn.commit()
real_urlopen = urllib.request.urlopen


def unreachable(*a, **k):
    raise OSError("worker not asked in this check")


def indexing(outstanding, task="faces", done=0):
    with open(os.path.join(store, "indexer_status.json"), "w") as fh:
        json.dump({"at": time.time(), "state": "indexing", "task": task, "done": done, "failed": 0,
                   "outstanding": outstanding, "started": time.time() - 60}, fh)
    ns["_indexing_cache"].update(at=0, data=None)
    urllib.request.urlopen = unreachable
    try:
        with app.test_request_context("/api/media/indexing"):
            return resolve(lambda: ns["media_indexing_status"]()).get_json()
    finally:
        urllib.request.urlopen = real_urlopen


d = indexing(3)
check("the face pass's 3 stored files still waiting are not counted twice: total 5, not 8",
      d.get("indexable") == 5 and d.get("faces_done") == 2 and d.get("left") == 3, {k: d.get(k) for k in ("indexable", "faces_done", "left")})
d = indexing(5)
check("2 new files the sweep knows but the store does not are added: total 7",
      d.get("indexable") == 7, {k: d.get(k) for k in ("indexable", "faces_done", "left")})
d = indexing(4, done=2)
check("the sweep's own progress is taken off what is left", d.get("left") == 2 and d.get("indexable") == 5, d.get("left"))
d = indexing(0, task="tags")
check("an idle tags pass: the store's count", d.get("indexable") == 5, d.get("indexable"))

# ---------------------------------------------------------------- planner and worker call
R = "downloads"
PIC = "twitter/@yoake_cos_/2091141486901694803_1.jpg"
conn.execute("INSERT INTO media (id, root, path, kind) VALUES (20, ?, ?, 'image')", (R, PIC))
conn.execute("INSERT INTO media_tags (media_id, tag, category, p) VALUES (20, 'realistic', 'general', 0.9)")
conn.commit()
ns["worker_upscale_limit_px"] = lambda refresh=False: 256_000_000
ns["CONFIG"]["live_upscale"] = {"max_output_megapixels": 64, "max_factor": 8}
pic_full = resolve(lambda: ns["resolve_media_path"](R, PIC))
if pic_full and os.path.isfile(pic_full):
    spec, _ = resolve(lambda: ns["plan_upscale"](R, PIC, 100000, 100000))
    check("the plan carries the bounds: the lower megapixel limit and Largest upscale factor",
          spec and spec.get("max_px") == 64_000_000 and spec.get("max_scale") == 8, spec and {k: spec.get(k) for k in ("max_px", "max_scale")})
else:
    check("the real picture for the planner exists", False, PIC)

sent = {}


class FakeResponse:
    headers = {"Content-Type": "image/webp", "X-Upscale-Size": "40x30", "X-Upscale-Seconds": "2.5"}

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return b"RIFF0000WEBP"


def fake_urlopen(req, timeout=None):
    sent["body"] = req.data
    return FakeResponse()


cache = os.path.join(tmp, "cache")
os.makedirs(cache)
img = os.path.join(tmp, "img.jpg")
open(img, "wb").write(b"\xff\xd8 not a real jpeg")
ns["upscale_cache_dir"] = lambda: cache
urllib.request.urlopen = fake_urlopen
try:
    entry = resolve(lambda: ns["_run_upscale"]({"key": "k" * 40, "full": img, "model": "nomoswebphoto-realplksr", "factor": 2,
                                                  "src": [10, 7], "target": [20, 14], "root": R, "path": "p/x.jpg",
                                                  "reason": "tag realistic", "limited": None, "scale": 2.0,
                                                  "max_px": 64_000_000, "max_scale": 8}))
finally:
    urllib.request.urlopen = real_urlopen
body = sent.get("body") or b""
for field, value in (("keep_larger", b"1"), ("max_px", b"64000000"), ("max_scale", b"8"), ("width", b"20"), ("height", b"14")):
    check("the worker is sent %s=%s" % (field, value.decode()), b'name="%s"\r\n\r\n%s\r\n' % (field.encode(), value) in body)
check("the cache entry records the scale served (40 of 10 = 4.0), not the 2.0 planned",
      entry.get("scale") == 4.0 and entry.get("out") == [40, 30], {k: entry.get(k) for k in ("scale", "out")})

shutil.rmtree(tmp, ignore_errors=True)
print("ALL PASS" if ok else "SOME CHECKS FAILED")
