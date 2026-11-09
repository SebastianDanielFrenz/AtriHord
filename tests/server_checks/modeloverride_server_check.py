"""A STAGED server.py, out of its AST: the upscale model set BY HAND for one
file. It wins over every rule, "photo" follows Settings' photo model, the cache
key separates the two copies, the route's refusals, and the answers carrying it.

Synthetic AI index and temp copies of config.json and upscale_models.json;
nothing under /data2 is written. plan_upscale needs a real file on disk, so the
picture that prompted the feature is used read-only (vault KSF-019).

  venv/bin/python modeloverride_server_check.py /tmp/modeloverride/server.py
"""
import ast
import json
import os
import shutil
import sys
import tempfile

BASE = os.environ.get("ATRIHORD_DIR") or os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SERVER = sys.argv[1]
STAGE = os.path.dirname(os.path.abspath(SERVER))
sys.path.insert(0, BASE)
sys.path.insert(0, STAGE)
tmp = tempfile.mkdtemp(prefix="modeloverride_check_")
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
ns["USERS_FILE"] = os.path.join(tmp, "users.json")
ns["INDEXER_CONFIG_FILE"] = os.path.join(tmp, "indexer_config.json")
ns["USAGE_FILE"] = os.path.join(tmp, "pipeline_usage.json")
ns["DOWNLOAD_VOLUME_FILE"] = os.path.join(tmp, "download_volume.json")
ns["get_authenticated_user"] = lambda: {"username": "alice", "role": "admin"}
for name in ("BASE_DIR", "CONFIG", "DEFAULT_CONFIG", "app", "_read_roots_unlocked", "_read_users_unlocked",
             "_read_usage_unlocked", "UPSCALE_PHOTO_MODELS", "UPSCALE_MODEL_LABELS", "photo_upscale_model",
             "choose_upscale_model", "upscale_settings", "plan_upscale", "media_upscale_model",
             "media_upscale_config", "upscale_model_override", "set_upscale_model_override",
             "UPSCALE_MODEL_KINDS", "UPSCALE_MODEL_STORE_MAX"):
    load(name)
app = ns["app"]
PHOTO = ns["DEFAULT_CONFIG"]["live_upscale"]["photo_model"]
# The store must be the temp one BEFORE anything reads it, or the check would
# write the real library's overrides.
ns["UPSCALE_MODEL_STORE"] = os.path.join(tmp, "upscale_models.json")
check("the store is the temp one, not the live file",
      ns["UPSCALE_MODEL_STORE"].startswith(tmp) and not os.path.exists(os.path.join(BASE, "upscale_models.json"))
      or ns["UPSCALE_MODEL_STORE"].startswith(tmp))

# ---------------------------------------------------------------- the store
R = "downloads"
P = "twitter/@_m_3030/2092241371868635209_1.jpg"


def override(root=R, path=P):
    return resolve(lambda: ns["upscale_model_override"](root, path))


def set_override(kind, root=R, path=P, by="alice"):
    return resolve(lambda: ns["set_upscale_model_override"](root, path, kind, by))


check("nothing set to begin with", override() is None)
got = set_override("photo")
check("set to photo", (got or {}).get("kind") == "photo" and got.get("by") == "alice", got)
check("... and read back", (override() or {}).get("kind") == "photo")
check("... and written to disk", json.load(open(ns["UPSCALE_MODEL_STORE"], encoding="utf-8"))
      .get(R, {}).get(P, {}).get("kind") == "photo")
set_override("anime")
check("changed to anime", (override() or {}).get("kind") == "anime")
# Forget what is in memory: a restart must read the same answer back.
ns["_model_overrides"]["loaded"] = False
ns["_model_overrides"]["entries"] = {}
check("survives a restart", (override() or {}).get("kind") == "anime")
check("another file is unaffected", override(path="twitter/@_m_3030/other.jpg") is None)
check("another root is unaffected", override(root="other") is None)
set_override(None)
check("cleared", override() is None)
check("... and the empty root is gone from the file",
      R not in json.load(open(ns["UPSCALE_MODEL_STORE"], encoding="utf-8")))
check("clearing what was never set is quiet", set_override(None) is None)

# The bound drops the OLDEST, so what was set today survives a runaway.
ns["_model_overrides"]["entries"] = {R: {f"f{i}.jpg": {"kind": "photo", "by": "alice", "at": float(i)}
                                        for i in range(ns["UPSCALE_MODEL_STORE_MAX"] + 5)}}
resolve(lambda: ns["_save_model_store"]())
kept = ns["_model_overrides"]["entries"][R]
check("the store is bounded", len(kept) == ns["UPSCALE_MODEL_STORE_MAX"], len(kept))
check("... dropping the oldest first", "f0.jpg" not in kept and "f4.jpg" not in kept and "f5.jpg" in kept)
ns["_model_overrides"]["entries"] = {}
resolve(lambda: ns["_save_model_store"]())

# ---------------------------------------------------------------- the rules
# A synthetic index row for the reported file: exactly what prod holds for it -
# an artwork word in the caption, no photo tag, 1girl, a face under 0.8. Three
# reasons for the anime model, which is the whole point of the escape hatch.
conn = media_ai.connect(tmp)
conn.execute("INSERT INTO media (id, root, path, kind, caption, tag_model) VALUES (?,?,?,?,?,?)",
             (1, R, P, "image",
              "The image shows a woman sitting on a chair in front of a chandelier. "
              "The image is animated, giving it a dynamic feel.", "wd"))
conn.execute("INSERT INTO media_tags (media_id, tag, category, p) VALUES (?,?,?,?)", (1, "1girl", "general", 0.99))
try:
    conn.execute("INSERT INTO faces (media_id, bbox, det_score) VALUES (?,?,?)", (1, "[0, 0, 10, 10]", 0.672))
except Exception:
    conn.execute("INSERT INTO faces (media_id, bbox, det_score, embedding, quality) VALUES (?,?,?,?,?)",
                 (1, "[0, 0, 10, 10]", 0.672, b"\0" * 8, 10.0))
conn.commit()

check("the rules alone send the reported file to the anime model",
      resolve(lambda: ns["choose_upscale_model"](R, P)) == ("anime", "caption describes artwork"),
      resolve(lambda: ns["choose_upscale_model"](R, P)))

real_settings = ns["upscale_settings"]


def plan(**settings):
    cfg = dict(resolve(lambda: real_settings()))
    cfg.update(enabled=True, **settings)
    ns["upscale_settings"] = lambda: cfg
    try:
        # A need far above the file's own size, so it is always planned.
        return resolve(lambda: ns["plan_upscale"](R, P, 20000, 20000))
    finally:
        ns["upscale_settings"] = real_settings


spec, status = plan()
check("plan_upscale without an override: the anime model",
      spec and spec["model"] == "anime" and spec["reason"] == "caption describes artwork"
      and spec["override"] is None, spec and (spec["model"], spec["reason"]) or status)
rule_key = spec and spec["key"]

set_override("photo")
spec, status = plan()
check("set to photo: the photo model, over all three rules",
      spec and spec["model"] == PHOTO and spec["override"] == "photo", spec and spec["model"] or status)
check("... and the panel is told who", spec and spec["reason"] == "set by hand (alice)", spec and spec["reason"])
check("... under its own cache key", spec and spec["key"] != rule_key)
photo_key = spec and spec["key"]

spec, status = plan(photo_model="nmkd-siax")
check("\"photo\" follows Settings' photo model rather than pinning one",
      spec and spec["model"] == "nmkd-siax", spec and spec["model"] or status)

set_override("anime")
spec, status = plan()
check("set to anime: the anime model, and said to be set by hand",
      spec and spec["model"] == "anime" and spec["override"] == "anime"
      and spec["reason"] == "set by hand (alice)", spec and (spec["model"], spec["reason"]) or status)
check("... and keys as the rules' own copy, which is the same picture",
      spec and spec["key"] == rule_key)

set_override(None)
spec, status = plan()
check("cleared: back to the rules", spec and spec["model"] == "anime"
      and spec["reason"] == "caption describes artwork" and spec["override"] is None,
      spec and (spec["model"], spec["reason"]) or status)

# ---------------------------------------------------------------- the route
def call(fn, path, method="GET", body=None):
    with app.test_request_context(path, method=method, json=body):
        rv = resolve(lambda: ns[fn]())
    if isinstance(rv, tuple):
        resp, status = rv[0], rv[1]
    else:
        resp, status = rv, rv.status_code
    return status, resp.get_json()


ROUTE = "/api/media/upscale/model"
status, data = call("media_upscale_model", ROUTE, "POST", {"root": R, "path": P, "model": "photo"})
check("POST sets it", status == 200 and data.get("override") == "photo" and data.get("model") == PHOTO,
      (status, data))
check("... and names the model for the panel", data.get("label") == ns["UPSCALE_MODEL_LABELS"][PHOTO], data)
check("... and the next plan uses it", plan()[0]["model"] == PHOTO)
status, data = call("media_upscale_model", ROUTE, "POST", {"root": R, "path": P, "model": None})
check("POST null clears it", status == 200 and data.get("override") is None and data.get("model") is None,
      (status, data))
check("... and the next plan is back to the rules", plan()[0]["model"] == "anime")

for bad in ("general", "nmkd-siax", "", 1, True):
    status, data = call("media_upscale_model", ROUTE, "POST", {"root": R, "path": P, "model": bad})
    check("%r is refused" % (bad,), status == 400, (status, data))
status, data = call("media_upscale_model", ROUTE, "POST", {"root": R, "path": "", "model": "photo"})
check("an empty path is refused", status == 403, (status, data))

real_visible = ns.get("root_visible_to")
ns["root_visible_to"] = lambda user, root: False
status, data = call("media_upscale_model", ROUTE, "POST", {"root": R, "path": P, "model": "photo"})
check("a root the caller may not see is refused", status == 403, (status, data))
ns["root_visible_to"] = real_visible
check("... and nothing was written", override() is None)

ns["get_authenticated_user"] = lambda: None
status, data = call("media_upscale_model", ROUTE, "POST", {"root": R, "path": P, "model": "photo"})
check("a signed-out caller is refused", status == 401, (status, data))
ns["get_authenticated_user"] = lambda: {"username": "alice", "role": "admin"}

status, data = call("media_upscale_config", "/api/media/upscale/config")
check("the config tells the page which model \"photograph\" means",
      status == 200 and data.get("photo_model") == PHOTO, (status, data))
check("... and still carries the labels", (data or {}).get("model_labels", {}).get("anime"))

shutil.rmtree(tmp, ignore_errors=True)
print("ALL PASS" if ok else "SOME CHECKS FAILED")
