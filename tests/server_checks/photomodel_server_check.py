"""The photo upscaler choice in a STAGED server.py, out of its AST in Flask test
requests: the model for photos, artwork and undecided files from tags and
Settings, the planner's model and cache key, the model name sent to the vision
worker, the labels for the page, and the Settings field. The AI index is
synthetic, config files are temp copies, the worker call is intercepted.
Nothing under /data2 is written.

  venv/bin/python photomodel_server_check.py /tmp/photomodels/server.py
"""
import ast
import json
import os
import shutil
import sys
import tempfile
import urllib.request

BASE = os.environ.get("ATRIHORD_DIR") or os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SERVER = sys.argv[1]
STAGE = os.path.dirname(os.path.abspath(SERVER))
sys.path.insert(0, BASE)
sys.path.insert(0, STAGE)
tmp = tempfile.mkdtemp(prefix="photomodel_check_")
os.makedirs(os.path.join(tmp, "ai"))
os.environ["KSF_MEDIA_AI_DIR"] = os.path.join(tmp, "ai")
for name in ("config.json", "indexer_config.json", "pipeline_usage.json", "download_volume.json"):
    if os.path.exists(os.path.join(BASE, name)):
        shutil.copy(os.path.join(BASE, name), os.path.join(tmp, name))
with open(os.path.join(tmp, "users.json"), "w") as fh:
    json.dump([{"username": "alice", "role": "admin"}], fh)

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
ns["USERS_FILE"] = os.path.join(tmp, "users.json")
ns["INDEXER_CONFIG_FILE"] = os.path.join(tmp, "indexer_config.json")
ns["USAGE_FILE"] = os.path.join(tmp, "pipeline_usage.json")
ns["DOWNLOAD_VOLUME_FILE"] = os.path.join(tmp, "download_volume.json")
ns["get_authenticated_user"] = lambda: {"username": "alice", "role": "admin"}
# BASE_DIR up front: choose_upscale_model catches its own exceptions, so a name
# first needed inside its try would be reported as "not indexed yet".
for name in ("BASE_DIR", "CONFIG", "DEFAULT_CONFIG", "app", "_read_roots_unlocked", "_read_users_unlocked", "_read_usage_unlocked",
             "UPSCALE_PHOTO_MODELS", "UPSCALE_MODEL_LABELS", "photo_upscale_model", "choose_upscale_model",
             "upscale_settings", "plan_upscale", "_run_upscale", "media_upscale_config", "get_settings", "put_settings",
             "resolve_media_path", "UPSCALE_CAPTION_ART", "UPSCALE_CAPTION_PHOTO"):
    load(name)
app = ns["app"]
check("server imports the staged media_ai", ns.get("media_ai") is media_ai)

PHOTO_KEYS = ["nomoswebphoto-realplksr", "nmkd-siax", "ultrasharp", "nomos2-otf-esrgan"]
check("the four photo models, RealPLKSR first and the default",
      list(ns["UPSCALE_PHOTO_MODELS"]) == PHOTO_KEYS and ns["DEFAULT_CONFIG"]["live_upscale"]["photo_model"] == PHOTO_KEYS[0])
check("labels for them, the anime model and x2plus",
      set(ns["UPSCALE_MODEL_LABELS"]) == set(PHOTO_KEYS) | {"anime", "general"}
      and ns["UPSCALE_MODEL_LABELS"]["nomoswebphoto-realplksr"] == "4xNomosWebPhoto RealPLKSR")

# ---------------------------------------------------------------- choice
R = "downloads"
PIC = "twitter/@yoake_cos_/2091141486901694803_1.jpg"
conn = media_ai.connect(tmp)
PHOTO_CAPTION = "A photograph of a woman in a costume standing in front of a wall."
ART_CAPTION = "An anime illustration of a girl with long hair."
rows = [(1, "p/photo.jpg", None, [("realistic", "general", 0.9), ("1girl", "general", 0.9)]),
        (2, "a/art.jpg", None, [("1girl", "general", 0.9)]),
        (3, "c/photo_caption.jpg", PHOTO_CAPTION, []),
        (4, "c/art_caption.jpg", ART_CAPTION, [("realistic", "general", 0.1)]),
        (5, "u/undecided.jpg", "", [("flower", "general", 0.9)]),
        (6, PIC, None, [("realistic", "general", 0.9)])]
for mid, path, caption, tags in rows:
    conn.execute("INSERT INTO media (id, root, path, kind, caption) VALUES (?,?,?,?,?)", (mid, R, path, "image", caption))
    for tag, cat, p in tags:
        conn.execute("INSERT INTO media_tags (media_id, tag, category, p) VALUES (?,?,?,?)", (mid, tag, cat, p))
conn.commit()
check("the harness captions are what the regexes look for",
      ns["UPSCALE_CAPTION_PHOTO"].search(PHOTO_CAPTION) and ns["UPSCALE_CAPTION_ART"].search(ART_CAPTION))


def choose(path, **settings):
    ns["CONFIG"]["live_upscale"] = dict(settings)
    return resolve(lambda: ns["choose_upscale_model"](R, path))


check("a photo gets the default photo upscaler", choose("p/photo.jpg") == (PHOTO_KEYS[0], "tag realistic"), choose("p/photo.jpg"))
check("artwork keeps the anime model", choose("a/art.jpg") == ("anime", "tag 1girl"), choose("a/art.jpg"))
check("a photo caption picks the photo upscaler", choose("c/photo_caption.jpg")[0] == PHOTO_KEYS[0], choose("c/photo_caption.jpg"))
check("an artwork caption picks anime", choose("c/art_caption.jpg")[0] == "anime", choose("c/art_caption.jpg"))
check("undecided, default_model general = the photo upscaler", choose("u/undecided.jpg") == (PHOTO_KEYS[0], "no deciding tag"))
check("undecided, default_model anime", choose("u/undecided.jpg", default_model="anime")[0] == "anime")
check("not indexed at all = the photo upscaler", choose("x/unknown.jpg") == (PHOTO_KEYS[0], "not indexed yet"))
for key in PHOTO_KEYS:
    check("Settings picks %s for photos" % key, choose("p/photo.jpg", photo_model=key)[0] == key)
check("Settings never change artwork", choose("a/art.jpg", photo_model="nmkd-siax")[0] == "anime")
check("an unknown or retired name falls back to the default",
      choose("p/photo.jpg", photo_model="bogus")[0] == PHOTO_KEYS[0] and choose("p/photo.jpg", photo_model="general")[0] == PHOTO_KEYS[0])

# ---------------------------------------------------------------- planner
ns["worker_upscale_limit_px"] = lambda refresh=False: 256_000_000
pic_full = resolve(lambda: ns["resolve_media_path"](R, PIC))
if pic_full and os.path.isfile(pic_full):
    def plan(**settings):
        ns["CONFIG"]["live_upscale"] = dict(settings)
        return resolve(lambda: ns["plan_upscale"](R, PIC, 100000, 100000))
    a, _ = plan()
    b, _ = plan(photo_model="nmkd-siax")
    a2, _ = plan()
    check("the plan names the photo upscaler", a and a["model"] == PHOTO_KEYS[0] and b["model"] == "nmkd-siax", (a, b))
    check("a different photo upscaler is a different cached copy", a["key"] != b["key"] and a["key"] == a2["key"])
else:
    check("the real picture for the planner exists", False, PIC)

# ---------------------------------------------------------------- sent to the worker
sent = {}


class FakeResponse:
    headers = {"Content-Type": "image/webp", "X-Upscale-Size": "20x20", "X-Upscale-Seconds": "1.5"}

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return b"RIFF0000WEBP"


def fake_urlopen(req, timeout=None):
    sent["url"], sent["body"] = req.full_url, req.data
    return FakeResponse()


cache = os.path.join(tmp, "cache")
os.makedirs(cache)
img = os.path.join(tmp, "img.jpg")
open(img, "wb").write(b"\xff\xd8not really a jpeg")
ns["upscale_cache_dir"] = lambda: cache
real_urlopen = urllib.request.urlopen
urllib.request.urlopen = fake_urlopen
try:
    ns["CONFIG"]["live_upscale"] = {}
    entry = resolve(lambda: ns["_run_upscale"]({"key": "k" * 40, "full": img, "model": "ultrasharp", "factor": 2,
                                                  "src": [10, 10], "target": [20, 20], "root": R, "path": "p/photo.jpg",
                                                  "reason": "tag realistic", "limited": None, "scale": 2.0}))
finally:
    urllib.request.urlopen = real_urlopen
check("the worker is asked for the model by its key", b'name="model"\r\n\r\nultrasharp\r\n' in (sent.get("body") or b"")
      and sent.get("url", "").endswith("/v1/upscale"), sent.get("url"))
check("the cache entry keeps the model", entry["model"] == "ultrasharp" and entry["out"] == [20, 20], entry)

# ---------------------------------------------------------------- page config
with app.test_request_context("/api/media/upscale/config"):
    data = resolve(lambda: ns["media_upscale_config"]()).get_json()
check("the upscale config carries the labels", data.get("model_labels") == ns["UPSCALE_MODEL_LABELS"]
      and {"enabled", "view_seconds", "zoom_factor"} <= set(data), data)

# ---------------------------------------------------------------- Settings


def call(fn, path, method="GET", body=None):
    with app.test_request_context(path, method=method, json=body):
        resp = resolve(lambda: ns[fn]())
    status = 200
    if isinstance(resp, tuple):
        resp, status = resp[0], resp[1]
    return status, resp.get_json()


status, data = call("get_settings", "/api/settings")
fields = {f["key"]: f for s in data["schema"] for f in s["fields"]}
f = fields.get("live_upscale.photo_model") or {}
check("Settings offer the photo upscaler with names", status == 200 and f.get("type") == "select"
      and f.get("options") == PHOTO_KEYS and f.get("labels") == ns["UPSCALE_PHOTO_MODELS"], f)
check("... next to the model for undecided files, labelled", (fields.get("live_upscale.default_model") or {}).get("labels")
      == {"general": "Photo upscaler", "anime": "Anime model"})
check("the current value is shown", data["values"].get("live_upscale.photo_model") in PHOTO_KEYS, data["values"].get("live_upscale.photo_model"))
status, data = call("put_settings", "/api/settings", "PUT", {"values": {"live_upscale.photo_model": "nmkd-siax"}})
saved = json.load(open(os.path.join(tmp, "config.json")))
check("saving a photo upscaler", status == 200 and saved["live_upscale"]["photo_model"] == "nmkd-siax", (status, data))
check("... and the next plan uses it", resolve(lambda: ns["choose_upscale_model"](R, "p/photo.jpg"))[0] == "nmkd-siax")
status, data = call("put_settings", "/api/settings", "PUT", {"values": {"live_upscale.photo_model": "general"}})
check("a model outside the list is refused, by name", status == 400 and "live_upscale.photo_model" in (data or {}).get("errors", {}), (status, data))

shutil.rmtree(tmp, ignore_errors=True)
print("ALL PASS" if ok else "SOME CHECKS FAILED")
