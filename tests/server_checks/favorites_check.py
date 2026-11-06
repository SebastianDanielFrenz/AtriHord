"""Favourites, out of a STAGED server.py's AST, against a temp favorites.json and
real library files (only read): the store, POST /api/media/favorite (auth,
validation, visibility, missing files), the flag on browse, recent and search
entries without touching the shared recent cache, the favorites=1 search (newest
first, filters, paging, per account) - plus "Download upscaled" on the
upscaled-file route and the ext on the ready answer.

  venv/bin/python favorites_check.py /tmp/.../server.py
"""
import ast
import json
import os
import shutil
import sys
import tempfile
import time

BASE = os.environ.get("ATRIHORD_DIR") or os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SERVER = sys.argv[1]
sys.path.insert(0, BASE)
tmp = tempfile.mkdtemp(prefix="fav_check_")

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


ok = True


def check(label, cond, detail=""):
    global ok
    ok = ok and bool(cond)
    print(("PASS " if cond else "FAIL ") + label + (f"  ({detail})" if detail and not cond else ""))


ns["FAVORITES_FILE"] = os.path.join(tmp, "favorites.json")
ALICE = {"username": "alice-harness", "role": "admin"}
GUEST = {"username": "guest-harness", "role": "guest"}
CURRENT = {"user": ALICE}
ns["get_authenticated_user"] = lambda: CURRENT["user"]
for name in ("_read_roots_unlocked", "app", "DEFAULT_ROOT_ID", "_favorites", "_favorites_lock", "user_favorites",
             "set_favorite", "attach_favorites", "media_favorite", "browse_media", "recent_media", "search_media",
             "get_recent_media_entries", "media_upscaled_file", "media_upscale", "resolve_media_path", "root_visible_to"):
    load(name)
app = ns["app"]


def call(fn, path, method="GET", body=None, query=None, args=()):
    with app.test_request_context(path, method=method, json=body, query_string=query):
        resp = resolve(lambda: ns[fn](*args))
        status = 200
        if isinstance(resp, tuple):
            resp, status = resp[0], resp[1]
        else:
            status = resp.status_code
        data = resp.get_json(silent=True) if getattr(resp, "mimetype", "") == "application/json" else None
        headers = dict(resp.headers) if hasattr(resp, "headers") else {}
        if hasattr(resp, "close"):
            resp.close()
    return status, data, headers


def reset():
    ns["_favorites"].update(loaded=False, data={})
    if os.path.exists(ns["FAVORITES_FILE"]):
        os.remove(ns["FAVORITES_FILE"])


ROOT, FOLDER = "downloads", "twitter/@yoake_cos_"
PIC = FOLDER + "/2091141486901694803_1.jpg"
folder_full = resolve(lambda: ns["resolve_media_path"](ROOT, FOLDER))
others = sorted(n for n in os.listdir(folder_full)
                if n.lower().endswith((".jpg", ".png", ".webp")) and FOLDER + "/" + n != PIC)
OTHER = FOLDER + "/" + others[0]

# -------------------------------------------------------------------- store
reset()
sf = lambda *a: resolve(lambda: ns["set_favorite"](*a))
check("favouriting counts, and again is not a second one", sf("alice-harness", ROOT, "a/b.jpg", True) == 1
      and sf("alice-harness", ROOT, "a/b.jpg", True) == 1 and sf("alice-harness", ROOT, "c/d.jpg", True) == 2)
check("unfavouriting counts down", sf("alice-harness", ROOT, "a/b.jpg", False) == 1)
stored = json.load(open(ns["FAVORITES_FILE"]))
check("stored per account, per root, per path", list(stored) == ["alice-harness"]
      and list(stored["alice-harness"][ROOT]) == ["c/d.jpg"], stored)
sf("alice-harness", ROOT, "c/d.jpg", False)
check("an account with none left disappears from the file", json.load(open(ns["FAVORITES_FILE"])) == {})
ns["_favorites"].update(loaded=False, data={})
sf("alice-harness", ROOT, "x/y.jpg", True)
ns["_favorites"].update(loaded=False, data={})
check("survives a reload from disk", "x/y.jpg" in resolve(lambda: ns["user_favorites"]("alice-harness")).get(ROOT, {}))
reset()

# -------------------------------------------------------------------- route
CURRENT["user"] = None
check("not signed in: 401", call("media_favorite", "/api/media/favorite", "POST", {"path": PIC, "favorite": True})[0] == 401)
CURRENT["user"] = ALICE
bad = [call("media_favorite", "/api/media/favorite", "POST", b)[0] for b in (
    {}, {"path": PIC, "favorite": "yes"}, {"root": 5, "path": PIC, "favorite": True}, {"path": "/", "favorite": True})]
check("bad bodies: 400", bad == [400, 400, 400, 400], bad)
check("favouriting a file that does not exist: 404",
      call("media_favorite", "/api/media/favorite", "POST", {"root": ROOT, "path": FOLDER + "/nope.jpg", "favorite": True})[0] == 404)
status, data, _ = call("media_favorite", "/api/media/favorite", "POST", {"root": ROOT, "path": FOLDER + "/nope.jpg", "favorite": False})
check("unfavouriting one that does not exist is fine", status == 200 and data == {"favorite": False, "count": 0}, (status, data))
status, data, _ = call("media_favorite", "/api/media/favorite", "POST", {"root": ROOT, "path": PIC, "favorite": True})
check("favouriting a real file", status == 200 and data == {"favorite": True, "count": 1}, (status, data))
real_visible = ns["root_visible_to"]
ns["root_visible_to"] = lambda user, root: False
CURRENT["user"] = GUEST
check("a library the account may not see: 403",
      call("media_favorite", "/api/media/favorite", "POST", {"root": ROOT, "path": PIC, "favorite": True})[0] == 403)
ns["root_visible_to"] = real_visible
CURRENT["user"] = ALICE

# ------------------------------------------------------------ attach helper
entries = [{"path": PIC, "root": ROOT}, {"path": OTHER, "root": ROOT}, {"path": PIC, "root": ROOT, "is_dir": True},
           {"path": PIC}]
resolve(lambda: ns["attach_favorites"](entries, ALICE))
check("flag only on favourited files (default root included, folders never)",
      [e.get("favorite") for e in entries] == [True, None, None, True], [e.get("favorite") for e in entries])
guest_entries = [{"path": PIC, "root": ROOT}]
resolve(lambda: ns["attach_favorites"](guest_entries, GUEST))
check("another account sees none of them", "favorite" not in guest_entries[0])

# ----------------------------------------------------------- browse, search
time.sleep(0.05)
call("media_favorite", "/api/media/favorite", "POST", {"root": ROOT, "path": OTHER, "favorite": True})
status, data, _ = call("browse_media", "/api/media/browse", query={"path": FOLDER, "root": ROOT})
flagged = sorted(f["path"] for f in (data or {}).get("files", []) if f.get("favorite"))
check("browse flags exactly the two favourites", status == 200 and flagged == sorted([PIC, OTHER]), (status, flagged))

status, data, _ = call("search_media", "/api/media/search", query={"q": "", "favorites": "1"})
paths = [i["path"] for i in (data or {}).get("items", [])]
check("favorites=1 without words: newest favourite first, all flagged",
      status == 200 and paths == [OTHER, PIC] and data["total"] == 2
      and all(i.get("favorite") for i in data["items"]), (status, paths, data and data.get("total")))
status, data, _ = call("search_media", "/api/media/search", query={"q": "Favorites", "favorites": "1", "offset": "1", "limit": "1"})
check("paging", [i["path"] for i in data["items"]] == [PIC] and data["has_more"] is False, data and [i["path"] for i in data["items"]])
status, data, _ = call("search_media", "/api/media/search", query={"q": "Favorites", "favorites": "1", "kind": "video"})
check("filters apply (kind=video: none)", status == 200 and data["total"] == 0, data and data.get("total"))
CURRENT["user"] = GUEST
status, data, _ = call("search_media", "/api/media/search", query={"q": "Favorites", "favorites": "1"})
check("another account's favourites view is its own (empty)", status == 200 and data["total"] == 0, data and data.get("total"))
CURRENT["user"] = ALICE
status, data, _ = call("search_media", "/api/media/search", query={"q": "2091141486901694803_1"})
check("an ordinary filename search still works and flags", status == 200
      and any(i["path"] == PIC and i.get("favorite") for i in data["items"]), data and [i["path"] for i in data["items"]][:3])

# ------------------------------------------------------------------- recent
shared = resolve(lambda: ns["get_recent_media_entries"](needed=40))
first = shared[0]
sf("alice-harness", first.get("root") or ROOT, first["path"], True)
status, data, _ = call("recent_media", "/api/media/recent", query={"offset": "0", "limit": "40"})
check("recent flags a favourited item", status == 200 and data["items"][0].get("favorite") is True,
      (status, data and data["items"][0].get("path")))
shared = resolve(lambda: ns["get_recent_media_entries"](needed=40))
check("the shared recent cache is not written into", not any("favorite" in e for e in shared))

# ------------------------------------------------------ download upscaled
cache = os.path.join(tmp, "cache")
os.makedirs(cache)
open(os.path.join(cache, "abc.webp"), "wb").write(b"RIFF....WEBP")
ns["upscale_cache_dir"] = lambda: cache
ns["_touch_upscale"] = lambda key: {"root": ROOT, "path": PIC, "factor": 3, "file": "abc.webp", "model": "general",
                                    "reason": "tag realistic", "out": [8190, 12288], "seconds": 30.1,
                                    "created": time.time(), "limited": None}
key = "0123456789abcdef0123456789abcdef01234567"
status, _, headers = call("media_upscaled_file", "/api/media/upscaled", query={"key": key, "download": "1"})
cd = headers.get("Content-Disposition", "")
check("Download upscaled: an attachment named after the original",
      status == 200 and cd.startswith("attachment") and "2091141486901694803_1.upscaled-3x.webp" in cd, (status, cd))
status, _, headers = call("media_upscaled_file", "/api/media/upscaled", query={"key": key})
check("shown in the preview: not an attachment", status == 200 and not headers.get("Content-Disposition", "").startswith("attachment"),
      headers.get("Content-Disposition"))
ns["plan_upscale"] = lambda r, p, w, h, **kw: ({"key": key, "factor": 3, "model": "general", "reason": "r", "limited": None}, None)
status, data, _ = call("media_upscale", "/api/media/upscale", "POST", {"root": ROOT, "path": PIC, "need_w": 8000, "need_h": 12000})
check("the ready answer carries the file type", status == 200 and data.get("status") == "ready" and data.get("ext") == ".webp", data)

shutil.rmtree(tmp, ignore_errors=True)
print("ALL PASS" if ok else "SOME CHECKS FAILED")
