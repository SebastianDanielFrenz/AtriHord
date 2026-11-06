"""A STAGED server.py, out of its AST in Flask test requests: live upscaling on
share pages. A temp library with a picture inside a shared folder and one
outside it, file/folder/expired/revoked shares, a fake vision worker (urlopen
replaced - nothing leaves this process) and the real job loop in a thread.
Temp config, cache and cover store; nothing under /data2 is written.

  venv/bin/python shareup_server_check.py /tmp/shareup/server.py
"""
import ast
import io
import os
import shutil
import sys
import tempfile
import threading
import time
import urllib.request
from urllib.parse import parse_qs, urlparse

BASE = os.environ.get("ATRIHORD_DIR") or os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SERVER = sys.argv[1]
STAGE = os.path.dirname(os.path.abspath(SERVER))
sys.path.insert(0, BASE)
sys.path.insert(0, STAGE)
tmp = tempfile.mkdtemp(prefix="shareup_check_")
store = os.path.join(tmp, "ai")
os.makedirs(store)
os.environ["KSF_MEDIA_AI_DIR"] = store
for name in ("config.json", "indexer_config.json"):
    if os.path.exists(os.path.join(BASE, name)):
        shutil.copy(os.path.join(BASE, name), os.path.join(tmp, name))

import media_ai                      # noqa: E402,F401
from PIL import Image                # noqa: E402

ok = True


def check(label, cond, detail=""):
    global ok
    ok = ok and bool(cond)
    print(("PASS " if cond else "FAIL ") + label + (f"  ({detail})" if detail and not cond else ""), flush=True)


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


lib = os.path.join(tmp, "lib")
cache = os.path.join(tmp, "cache")
os.makedirs(os.path.join(lib, "shared"))
os.makedirs(os.path.join(lib, "private"))
os.makedirs(cache)
ns["CONFIG_FILE"] = os.path.join(tmp, "config.json")
ns["USERS_FILE"] = os.path.join(tmp, "users.json")
ns["UPSCALE_COVER_STORE"] = os.path.join(tmp, "upscale_covers.json")
signed_in = {"user": {"username": "alice", "role": "admin"}}
ns["get_authenticated_user"] = lambda: signed_in["user"]
for name in ("BASE_DIR", "CONFIG", "DEFAULT_CONFIG", "app", "_read_roots_unlocked", "_read_users_unlocked",
             "SETTINGS_SCHEMA", "UPSCALE_PHOTO_MODELS", "UPSCALE_MODEL_LABELS", "upscale_settings", "plan_upscale",
             "_run_upscale", "_upscale_worker_loop", "media_upscale", "media_upscaled_file", "upscale_answer",
             # _run_upscale runs on the upscale thread, where a missing name is
             # not loaded on demand - everything it calls must be here.
             "parse_grain_header", "GRAIN_INFO_KEYS", "film_grain_hint",
             "public_share_upscale_config", "public_share_upscale", "public_share_upscaled", "share_upscale_enabled",
             "find_share", "find_share_including_expired", "share_expired", "share_root_of", "path_allowed_by_share",
             "_touch_upscale", "_upscale_entries", "_save_upscale_index", "vision_worker_address",
             "UpscaleLimitError", "note_worker_upscale_limit", "UPSCALE_MIN_SCALE", "UPSCALE_QUEUE_MAX",
             "_upscale_cv", "_upscale_queue", "_upscale_jobs", "_upscale_index", "choose_upscale_model",
             "upscale_model_from", "photo_upscale_model", "UPSCALE_CAPTION_PHOTO", "UPSCALE_CAPTION_ART",
             "UPSCALE_CAPTION_COSTUME", "UPSCALE_EXTS", "classify_media_type", "MEDIA_EXTENSIONS",
             "PUBLIC_SHARE_ASSETS", "GUEST_ALLOWED_FILES", "DEFAULT_ROOT_ID"):
    if name not in ns:
        load(name)
app = ns["app"]
R = "downloads"
# The real traversal guard, over a temp root.
ns["get_root"] = lambda root_id: {"id": root_id, "path": lib} if root_id == R else None
load("resolve_media_path")
ns["root_visible_to"] = lambda user, root: True
ns["media_dimensions_map"] = lambda: {}
ns["worker_upscale_limit_px"] = lambda refresh=False: 256_000_000
ns["upscale_cache_dir"] = lambda: cache
ns["CONFIG"]["live_upscale"] = {"enabled": True, "max_output_megapixels": 64, "max_factor": 4}
now = time.time()
SHARES = [
    {"id": "folderTok", "kind": "folder", "path": "shared", "root": R},
    {"id": "fileTok", "kind": "file", "path": "shared/one.png", "root": R},
    {"id": "expiredTok", "kind": "folder", "path": "shared", "root": R, "expires_at": now - 60},
    {"id": "revokedTok", "kind": "folder", "path": "shared", "root": R, "revoked": True},
]
ns["load_shares"] = lambda: SHARES
for rel, colour in (("shared/one.png", (200, 30, 90)), ("shared/two.png", (30, 200, 90)),
                    ("private/secret.png", (9, 9, 9))):
    Image.new("RGB", (300, 200), colour).save(os.path.join(lib, rel))

# ---------------------------------------------------------------- fake vision worker
calls = []


class FakeResponse:
    def __init__(self, body, headers):
        self.body, self.headers = body, headers

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self.body


def fake_urlopen(req, timeout=None):
    url = req.full_url if hasattr(req, "full_url") else str(req)
    if url.endswith("/v1/upscale"):
        calls.append(url)
        return FakeResponse(b"RIFF0000WEBP", {"Content-Type": "image/webp", "X-Upscale-Size": "600x400",
                                               "X-Upscale-Seconds": "1.0"})
    raise AssertionError("the harness let a request out: " + url)


real_urlopen = urllib.request.urlopen
urllib.request.urlopen = fake_urlopen
threading.Thread(target=ns["_upscale_worker_loop"], daemon=True).start()


def call(fn, path, *args, method="GET", body=None):
    with app.test_request_context(path, method=method, json=body):
        rv = resolve(lambda: ns[fn](*args))
    if isinstance(rv, tuple):
        resp, status = rv[0], rv[1]
    else:
        resp, status = rv, rv.status_code
    if resp.mimetype == "application/json":
        return status, resp.get_json()
    resp.direct_passthrough = False       # send_file streams; the harness wants the bytes
    data = resp.get_data()
    resp.close()
    return status, data


def ask(tok, rel, check_only=False, root=None):
    body = {"path": rel, "need_w": 600, "need_h": 400}
    if root is not None:
        body["root"] = root
    if check_only:
        body["check_only"] = True
    return call("public_share_upscale", f"/api/public/share/{tok}/upscale", tok, method="POST", body=body)


def until_ready(tok, rel):
    for _ in range(100):
        status, data = ask(tok, rel)
        if (data or {}).get("status") not in ("queued", "working"):
            return status, data
        time.sleep(0.05)
    return status, data


try:
    # ------------------------------------------------------------ the setting
    lu = ns["DEFAULT_CONFIG"]["live_upscale"]
    check("default: shares on, beside enabled", lu.get("shares") is True and lu.get("enabled") is True, lu.get("shares"))
    fields = [f for s in ns["SETTINGS_SCHEMA"] if s.get("id") == "live_upscale" for f in s["fields"]]
    keys = [f["key"] for f in fields]
    check("Settings: 'Also on shared links' right after the main switch, a bool",
          "live_upscale.shares" in keys and keys.index("live_upscale.shares") == keys.index("live_upscale.enabled") + 1
          and next(f for f in fields if f["key"] == "live_upscale.shares")["type"] == "bool", keys[:3])

    # ------------------------------------------------------------ static files
    check("upscale.js is a public share asset", "js/upscale.js" in ns["PUBLIC_SHARE_ASSETS"])
    check("... and a guest may load it on the library page", "js/upscale.js" in ns["GUEST_ALLOWED_FILES"])

    # ------------------------------------------------------------ config
    status, data = call("public_share_upscale_config", "/api/public/share/folderTok/upscale/config", "folderTok")
    check("share config: enabled, with the view time, zoom and model labels", status == 200 and data.get("enabled") is True
          and data.get("view_seconds") == 5 and data.get("zoom_factor") == 2 and "anime" in data.get("model_labels", {}),
          (status, data))
    for tok, why in (("nopeTok", "unknown"), ("expiredTok", "expired"), ("revokedTok", "revoked")):
        status, _ = call("public_share_upscale_config", f"/api/public/share/{tok}/upscale/config", tok)
        check(f"share config for an {why} link: 404", status == 404, status)
    ns["CONFIG"]["live_upscale"]["shares"] = False
    status, data = call("public_share_upscale_config", "/api/public/share/folderTok/upscale/config", "folderTok")
    check("'Also on shared links' off: disabled for the share", data.get("enabled") is False, data)
    ns["CONFIG"]["live_upscale"]["shares"] = True
    ns["CONFIG"]["live_upscale"]["enabled"] = False
    status, data = call("public_share_upscale_config", "/api/public/share/folderTok/upscale/config", "folderTok")
    check("live upscaling off altogether: disabled for the share too", data.get("enabled") is False, data)
    ns["CONFIG"]["live_upscale"]["enabled"] = True

    # ------------------------------------------------------------ what a link may ask for
    signed_in["user"] = None          # a share visitor has no session
    status, data = ask("folderTok", "private/secret.png")
    check("a file outside the shared folder: 403", status == 403, (status, data))
    status, data = ask("folderTok", "shared/../private/secret.png")
    check("... also through ..", status == 403, (status, data))
    status, data = ask("fileTok", "shared/two.png")
    check("a file share: its neighbour is 403", status == 403, (status, data))
    status, data = ask("folderTok", "shared/one.png", root="other")
    check("the right path under another root: 403", status == 403, (status, data))
    for tok in ("expiredTok", "revokedTok", "nopeTok"):
        status, data = ask(tok, "shared/one.png")
        check(f"{tok}: 404", status == 404, (status, data))
    check("... and no job was queued by any of them", calls == [] and not ns["_upscale_jobs"], ns["_upscale_jobs"])

    # ------------------------------------------------------------ the upscale itself
    status, data = ask("folderTok", "shared/one.png", check_only=True)
    check("a share visitor's check on opening: none", status == 200 and data.get("status") == "none", (status, data))
    status, data = ask("folderTok", "shared/one.png")
    check("the ask after viewing: queued", status == 200 and data.get("status") in ("queued", "working"), (status, data))
    status, data = until_ready("folderTok", "shared/one.png")
    url = data.get("url") or ""
    check("ready, with the SHARE's route for the copy", data.get("status") == "ready"
          and url.startswith("/api/public/share/folderTok/upscaled?key="), data)
    check("... and nothing about where the file lives", not any(k in data for k in ("path", "root", "folder")), sorted(data))
    key = parse_qs(urlparse(url).query)["key"][0]
    status, blob = call("public_share_upscaled", url, "folderTok")
    check("the copy is served to the link", status == 200 and blob == b"RIFF0000WEBP", status)
    status, _ = call("public_share_upscaled", url.replace("folderTok", "fileTok"), "fileTok")
    check("a file share whose file it is fetches the same copy", status == 200, status)
    status, data = ask("fileTok", "shared/one.png", check_only=True)
    check("the file share finds the same copy at once", data.get("status") == "ready" and key in data.get("url", ""), data)

    signed_in["user"] = {"username": "alice", "role": "admin"}
    status, data = call("media_upscale", "/api/media/upscale", method="POST",
                        body={"root": R, "path": "shared/one.png", "need_w": 600, "need_h": 400, "check_only": True})
    made = int(ns["_upscale_entries"]()[key]["created"])
    check("the library finds the same copy, on its own route - the url naming when it was made (a copy made "
          "again under the same key must not come from the browser's cache)", data.get("status") == "ready"
          and data.get("url") == f"/api/media/upscaled?key={key}&v={made}" and made > 0, (data, made))
    status, data = call("media_upscale", "/api/media/upscale", method="POST",
                        body={"root": R, "path": "private/secret.png", "need_w": 600, "need_h": 400})
    for _ in range(100):
        if data.get("status") not in ("queued", "working"):
            break
        time.sleep(0.05)
        status, data = call("media_upscale", "/api/media/upscale", method="POST",
                            body={"root": R, "path": "private/secret.png", "need_w": 600, "need_h": 400})
    check("the library upscales a file no share covers", data.get("status") == "ready", data)
    private_key = parse_qs(urlparse(data["url"]).query)["key"][0]
    signed_in["user"] = None

    status, data = call("public_share_upscaled", f"/api/public/share/folderTok/upscaled?key={private_key}", "folderTok")
    check("a link cannot fetch a copy of a file outside it: 404, not 403", status == 404, status)
    status, _ = call("public_share_upscaled", "/api/public/share/folderTok/upscaled?key=nothex", "folderTok")
    check("a malformed key: 400", status == 400, status)
    status, _ = call("public_share_upscaled", f"/api/public/share/expiredTok/upscaled?key={key}", "expiredTok")
    check("an expired link cannot fetch the copy", status == 404, status)

    # ------------------------------------------------------------ the setting, live
    ns["CONFIG"]["live_upscale"]["shares"] = False
    status, data = ask("folderTok", "shared/two.png")
    check("setting off: the ask answers disabled and queues nothing", data == {"status": "disabled"}
          and len(calls) == 2, (data, len(calls)))
    status, _ = call("public_share_upscaled", url, "folderTok")
    check("... and the cached copy is no longer served to the link", status == 404, status)
    signed_in["user"] = {"username": "alice", "role": "admin"}
    status, data = call("media_upscale", "/api/media/upscale", method="POST",
                        body={"root": R, "path": "shared/one.png", "need_w": 600, "need_h": 400, "check_only": True})
    check("... while the library keeps upscaling", data.get("status") == "ready", data)
    ns["CONFIG"]["live_upscale"]["shares"] = True
    signed_in["user"] = None
    status, data = ask("folderTok", "shared/two.png", check_only=True)
    check("setting back on: the share asks again", data.get("status") == "none", data)
finally:
    urllib.request.urlopen = real_urlopen
    shutil.rmtree(tmp, ignore_errors=True)
print("ALL PASS" if ok else "SOME CHECKS FAILED")
