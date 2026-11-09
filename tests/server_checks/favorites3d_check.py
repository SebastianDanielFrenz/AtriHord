"""Favourites -> 3D automation (P5): an ADMIN's favourite video or picture is queued in the
favourites lane; audio, folders and guests are ignored; unfavouriting takes a still-waiting
item back out, leaves a running job alone and drops the eviction protection (unless another
admin keeps the file); a manual request moves an item to the manual lane; the backfill queues
exactly the favourites inside the window, newest first, idempotently, and respects the
evicted-for-space record. Out of a STAGED server.py's AST (set_favorite, the hook, the
admin-favourites feed, media_favorite, put_settings, the backfill loop) over the real
stereo3d.py next to it (a temp queue, cache and library; the vision worker is never reached -
nothing here needs one, and a dead address proves it).

  venv/bin/python favorites3d_check.py /tmp/.../server.py     (via writeguard.py in run_all.sh)
"""
import ast
import hashlib
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import types

BASE = os.environ.get("ATRIHORD_DIR") or os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SERVER = os.path.abspath(sys.argv[1])
sys.path.insert(0, BASE)
sys.path.insert(0, os.path.dirname(SERVER))      # stereo3d.py of the stage (or the repo) first

import stereo3d  # noqa: E402

TMP = tempfile.mkdtemp(prefix="favorites3d_check_")
LIB = os.path.join(TMP, "lib")
CACHE = os.path.join(TMP, "cache")
os.makedirs(LIB)
os.makedirs(CACHE)
RESULTS = []
DAY = 86400.0


def check(name, ok, detail=""):
    RESULTS.append(bool(ok))
    print(("PASS " if ok else "FAIL ") + name + (f" - {detail}" if detail and not ok else ""))


def media(root, rel, size=1000):
    full = os.path.join(LIB, root, rel)
    os.makedirs(os.path.dirname(full), exist_ok=True)
    with open(full, "wb") as fh:
        fh.write(hashlib.sha256(rel.encode()).digest() * (size // 32 + 1))
        fh.truncate(size)
    return full


# ------------------------------------------------------------------ the server's functions
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


src = open(SERVER, encoding="utf-8").read()
ns["stereo3d"] = stereo3d
ns["FAVORITES_FILE"] = os.path.join(TMP, "favorites.json")
ns["CONFIG_FILE"] = os.path.join(TMP, "config.json")
ns["INDEXER_CONFIG_FILE"] = os.path.join(TMP, "indexer_config.json")
ns["BASE_DIR"] = TMP
for n in ("app", "DEFAULT_ROOT_ID", "DEFAULT_CONFIG", "is_admin", "require_admin", "_favorites", "_favorites_lock",
          "user_favorites", "_favorite_listeners", "set_favorite", "media_favorite", "_stereo3d", "_stereo3d_admin_names",
          "stereo3d_is_favourite", "stereo3d_favorite_changed", "stereo3d_admin_favourites",
          "_stereo3d_backfill_signature", "_stereo3d_backfill_wake", "_stereo3d_backfill_loop", "_atomic_json_write",
          "get_settings", "put_settings", "_settings_field", "_coerce_setting", "settings_values", "SETTINGS_SCHEMA",
          "_settings_write_lock", "_dig", "_read_indexer_config", "auto_trash_reasons", "load_config"):
    load(n)
app = ns["app"]
ns["_favorite_listeners"].append(ns["stereo3d_favorite_changed"])     # what server.py does at import
ns["CONFIG"] = json.loads(json.dumps(ns["DEFAULT_CONFIG"]))

USERS = [{"username": "alice", "role": "admin"}, {"username": "bob", "role": "admin"},
         {"username": "guest1", "role": "guest"}]
ns["load_users"] = lambda: [dict(u) for u in USERS]
CUR = {"user": USERS[0]}
ns["get_authenticated_user"] = lambda: CUR["user"]
ns["root_visible_to"] = lambda u, root: True


def lib_resolve(root, path):
    if root not in ("downloads", "jellyfin"):
        return None
    return os.path.join(LIB, root, path)


ns["resolve_media_path"] = lib_resolve

CFG = {"enabled": True, "cache_dir": CACHE, "cache_max_gb": 150, "max_eye_long_side": 2160, "divergence": 2.0,
       "convergence": 0.5, "mbps_1080": 30, "mbps_1440": 50, "mbps_2160": 80, "favorites_auto": True,
       "favorites_backfill_days": 7, "max_attempts": 2}
ns["stereo3d_settings"] = lambda: dict(CFG)
DEAD = "http://127.0.0.1:1"          # nothing listens: any worker call fails at once
svc_box = {}


def new_service():
    shutil.rmtree(CACHE, ignore_errors=True)
    os.makedirs(CACHE)
    for f in (ns["FAVORITES_FILE"],):
        if os.path.exists(f):
            os.remove(f)
    ns["_favorites"].update(loaded=False, data={})
    ns["_stereo3d"]["admins"] = (0.0, frozenset())
    CFG.update(enabled=True, cache_dir=CACHE, cache_max_gb=150, favorites_auto=True, favorites_backfill_days=7)
    q = os.path.join(TMP, "queue.json")
    if os.path.exists(q):
        os.remove(q)
    svc = stereo3d.Service(q, lambda: dict(CFG), lib_resolve, lambda: (DEAD, "tok"), ns["stereo3d_is_favourite"],
                           forbidden_dirs=(BASE, "/data2"))
    svc_box["svc"] = svc
    ns["stereo3d_service"] = lambda: svc
    return svc


def fav(user, root, path, on=True):
    return resolve_names(lambda: ns["set_favorite"](user, root, path, on))


def items(svc):
    return {(i["root"], i["path"]): i for i in svc.queue.items.values()}


def cached(svc, root, rel, last_watched=1.0):
    """A finished conversion of an existing library file, files and index entry in place."""
    ver = stereo3d.file_version(lib_resolve(root, rel))
    kind = stereo3d.kind_of(rel)
    _, pid = stereo3d.profile_for(kind, CFG)
    key = stereo3d.item_key(ver, pid)
    folder = os.path.join(CACHE, stereo3d.cache_rel(kind, key))
    os.makedirs(folder)
    names = stereo3d.RESULT_NAMES[kind]
    sizes = []
    for n in names:
        with open(os.path.join(folder, n), "wb") as fh:
            fh.write(b"x" * 5000)
        sizes.append(5000)
    svc.index().put(key, {"kind": kind, "root": root, "path": rel, "version": ver, "profile": pid, "created": 1.0,
                          "bytes": sum(sizes), "last_watched": last_watched, "watch_count": 0, "missing_since": None})
    svc.index().save()
    return key


for rel in ("a/one.mp4", "a/two.mp4", "a/pic1.jpg", "a/pic2.png", "a/song.mp3", "a/three.webm", "a/pic3.jpg",
            "a/old.mp4", "a/mid.mp4", "a/fresh.mp4", "a/edge.mp4"):
    media("downloads", rel)
media("jellyfin", "j/film.mkv")

# ================================================================== the hook
check("wiring: set_favorite calls the 3D hook after its lock, the backfill thread is started and the settings page wakes it",
      "listener(username, root_id, rel_path, on, stamp)" in src and "_favorite_listeners.append(stereo3d_favorite_changed)" in src
      and "target=_stereo3d_backfill_loop" in src and "_stereo3d_backfill_wake.set()" in src)

svc = new_service()
t0 = time.time()
fav("alice", "downloads", "a/one.mp4")
fav("alice", "downloads", "a/pic1.jpg")
q = items(svc)
check("hook: an admin's favourite video and picture land in the queue, favourites lane, queued",
      set(q) == {("downloads", "a/one.mp4"), ("downloads", "a/pic1.jpg")}
      and all(i["lane"] == stereo3d.LANE_FAVORITE and i["state"] == "queued" and i["by"] == "alice" for i in q.values()), q)
stored = json.load(open(ns["FAVORITES_FILE"]))["alice"]["downloads"]
check("hook: the item carries the favourite's own timestamp (fav_at) - that is what orders the lane",
      abs(q[("downloads", "a/one.mp4")]["fav_at"] - stored["a/one.mp4"]) < 1e-6
      and abs(q[("downloads", "a/pic1.jpg")]["fav_at"] - stored["a/pic1.jpg"]) < 1e-6)
check("hook: a picture is a picture, a video a video", q[("downloads", "a/one.mp4")]["kind"] == "video"
      and q[("downloads", "a/pic1.jpg")]["kind"] == "picture")
fav("alice", "downloads", "a/song.mp3")
check("hook: audio is ignored", ("downloads", "a/song.mp3") not in items(svc) and len(svc.queue.items) == 2)
fav("alice", "downloads", "a")                    # a folder-like path has no media extension
check("hook: a path that is no video or picture (a folder) is ignored", len(svc.queue.items) == 2)
fav("guest1", "downloads", "a/two.mp4")
check("hook: a guest's favourite does not queue anything", ("downloads", "a/two.mp4") not in items(svc) and len(svc.queue.items) == 2)
fav("alice", "downloads", "a/one.mp4")
check("hook: favouriting again is a duplicate, not a second item", len(svc.queue.items) == 2)
n_before = len(svc.queue.items)
fav("alice", "downloads", "a/three.webm")
order = [i["path"] for i in svc.queue.ordered()]
check("hook: the lane is newest favourite first", order[0] == "a/three.webm" and order.index("a/one.mp4") > order.index("a/pic1.jpg"), order)
check("hook: the queue file holds it (survives a restart)",
      len(stereo3d.Queue(os.path.join(TMP, "queue.json")).items) == n_before + 1)

CFG["favorites_auto"] = False
fav("alice", "downloads", "a/two.mp4")
check("hook: favorites_auto off queues nothing", ("downloads", "a/two.mp4") not in items(svc))
CFG["favorites_auto"] = True
CFG["enabled"] = False
fav("alice", "downloads", "a/pic2.png")
check("hook: 3D switched off queues nothing", ("downloads", "a/pic2.png") not in items(svc))
CFG["enabled"] = True
fav("alice", "downloads", "a/two.mp4", False)
fav("alice", "downloads", "a/pic2.png", False)
fav("alice", "downloads", "a/two.mp4")
check("hook: with the settings back on, the next favourite queues", ("downloads", "a/two.mp4") in items(svc))

# the route
svc = new_service()
CUR["user"] = USERS[0]
with app.test_request_context("/api/media/favorite", method="POST", json={"root": "downloads", "path": "a/one.mp4", "favorite": True}):
    resp = resolve_names(lambda: ns["media_favorite"]())
check("route: POST /api/media/favorite by an admin queues the file", resp.get_json()["count"] == 1
      and ("downloads", "a/one.mp4") in items(svc))
CUR["user"] = USERS[2]
with app.test_request_context("/api/media/favorite", method="POST", json={"root": "downloads", "path": "a/two.mp4", "favorite": True}):
    resp = resolve_names(lambda: ns["media_favorite"]())
check("route: the same by a guest works for the guest and queues nothing", resp.get_json()["favorite"] is True
      and ("downloads", "a/two.mp4") not in items(svc))
CUR["user"] = USERS[0]

# a failing 3D side never breaks favouriting
svc = new_service()
ns["stereo3d_service"] = lambda: (_ for _ in ()).throw(RuntimeError("queue file unreadable"))
count = fav("alice", "downloads", "a/one.mp4")
check("hook: a failing 3D side does not break favouriting (count returned, favourite stored)", count == 1
      and "a/one.mp4" in ns["user_favorites"]("alice")["downloads"])
count = fav("alice", "downloads", "a/one.mp4", False)
check("hook: ... nor unfavouriting", count == 0)
ns["stereo3d_service"] = lambda: svc

# ================================================================== unfavourite
svc = new_service()
fav("alice", "downloads", "a/one.mp4")
fav("alice", "downloads", "a/pic1.jpg")
fav("alice", "downloads", "a/pic1.jpg", False)
q = items(svc)
check("unfavourite: a queued favourites-lane item leaves the queue, the other stays",
      ("downloads", "a/pic1.jpg") not in q and ("downloads", "a/one.mp4") in q and len(q) == 1)
fav("alice", "downloads", "a/one.mp4", False)
check("unfavourite: ... and the last one too (empty queue)", not svc.queue.items
      and not stereo3d.Queue(os.path.join(TMP, "queue.json")).items)
fav("alice", "downloads", "a/one.mp4")
fav("guest1", "downloads", "a/one.mp4")
fav("guest1", "downloads", "a/one.mp4", False)
check("unfavourite: a guest removing their own favourite of the same file leaves the admin's item",
      ("downloads", "a/one.mp4") in items(svc))
fav("bob", "downloads", "a/one.mp4")
fav("alice", "downloads", "a/one.mp4", False)
check("unfavourite: another admin still has it - the item stays queued and protected",
      ("downloads", "a/one.mp4") in items(svc) and ns["stereo3d_is_favourite"]("downloads", "a/one.mp4"))
fav("bob", "downloads", "a/one.mp4", False)
check("unfavourite: the last admin lets go - item dequeued, protection gone",
      ("downloads", "a/one.mp4") not in items(svc) and not ns["stereo3d_is_favourite"]("downloads", "a/one.mp4"))

# a running job keeps running
fav("alice", "downloads", "a/two.mp4")
fav("alice", "downloads", "a/pic1.jpg")
fav("alice", "downloads", "a/pic2.png")
run_keys = {}
for rel, state in (("a/two.mp4", "submitted"), ("a/pic1.jpg", "uploading"), ("a/pic2.png", "downloading")):
    k = items(svc)[("downloads", rel)]["key"]
    run_keys[rel] = k
    svc.queue.update(k, state=state, worker_key="w" * 32, worker_state="converting")
for rel in ("a/two.mp4", "a/pic1.jpg", "a/pic2.png"):
    fav("alice", "downloads", rel, False)
q = items(svc)
check("unfavourite: a job the worker has (running, uploading, downloading) is left alone",
      all(("downloads", r) in q and q[("downloads", r)]["state"] == s for r, s in
          (("a/two.mp4", "submitted"), ("a/pic1.jpg", "uploading"), ("a/pic2.png", "downloading")))
      and svc.worker_error is None, q)
# a held favourite waiting to resume is also left alone
fav("alice", "downloads", "a/three.webm")
hk = items(svc)[("downloads", "a/three.webm")]["key"]
svc.queue.update(hk, state="held", worker_key="h" * 32, worker_state="held")
fav("alice", "downloads", "a/three.webm", False)
check("unfavourite: a held job is left alone too", items(svc).get(("downloads", "a/three.webm"), {}).get("state") == "held")
svc.queue.remove(list(svc.queue.items))
# failed favourites-lane item waiting for a retry: dropped
fav("alice", "downloads", "a/edge.mp4")
ek = items(svc)[("downloads", "a/edge.mp4")]["key"]
svc.queue.update(ek, state="failed", attempts=2, error="boom")
fav("alice", "downloads", "a/edge.mp4", False)
check("unfavourite: a failed favourites-lane item is dropped as well", ("downloads", "a/edge.mp4") not in items(svc))

# eviction protection follows the favourite
svc = new_service()
fav("alice", "downloads", "a/one.mp4")
fav("bob", "downloads", "a/two.mp4")
k1 = cached(svc, "downloads", "a/one.mp4", last_watched=100.0)
k2 = cached(svc, "downloads", "a/two.mp4", last_watched=200.0)
k3 = cached(svc, "downloads", "a/pic3.jpg", last_watched=300.0)
fav("alice", "downloads", "a/one.mp4")
order = stereo3d.eviction_order(svc.index().entries, ns["stereo3d_is_favourite"], time.time())
check("protection: with both favourited the non-favourite goes first, the favourites last in LRU order",
      order == [k3, k1, k2], order)
fav("alice", "downloads", "a/one.mp4", False)
order = stereo3d.eviction_order(svc.index().entries, ns["stereo3d_is_favourite"], time.time())
check("protection: unfavourited, the cached entry stays but loses its protection (now evicted among the plain ones, LRU)",
      order == [k1, k3, k2] and k1 in svc.index().entries and svc.index().files_ok(k1), order)

# the evicted-for-space record goes with the favourite
svc = new_service()
fav("alice", "downloads", "a/one.mp4")
svc.queue.remove(list(svc.queue.items))
svc.queue.record_evicted("downloads", "a/one.mp4", 150)
fav("alice", "downloads", "a/one.mp4", False)
check("unfavourite: the evicted-for-space record is cleared", not svc.queue.evicted)

# ================================================================== manual vs favourite
svc = new_service()
fav("alice", "downloads", "a/one.mp4")
fav("alice", "downloads", "a/pic1.jpg")
fav("alice", "downloads", "a/two.mp4")
res = svc.request_many([{"root": "downloads", "path": "a/pic1.jpg"}], stereo3d.LANE_MANUAL, "alice")
q = items(svc)
check("manual: a manual request for a queued favourite promotes it to the manual lane", res[0]["status"] == "promoted"
      and q[("downloads", "a/pic1.jpg")]["lane"] == stereo3d.LANE_MANUAL and len(q) == 3, res)
check("manual: the manual lane goes first", svc.queue.ordered()[0]["path"] == "a/pic1.jpg")
fav("alice", "downloads", "a/pic1.jpg")             # favouriting again must not demote it
check("manual: favouriting a manually queued file does not demote it", items(svc)[("downloads", "a/pic1.jpg")]["lane"] == stereo3d.LANE_MANUAL)
fav("alice", "downloads", "a/pic1.jpg", False)
check("manual: unfavouriting a promoted item leaves it queued (it was asked for by hand)",
      ("downloads", "a/pic1.jpg") in items(svc))
res = svc.request_many([{"root": "downloads", "path": "a/three.webm"}], stereo3d.LANE_MANUAL, "alice")
fav("alice", "downloads", "a/three.webm")
check("manual: a manual item for a file not favourited stays manual when it gets favourited",
      items(svc)[("downloads", "a/three.webm")]["lane"] == stereo3d.LANE_MANUAL)
# cached: a favourite of an already converted file queues nothing
svc = new_service()
cached(svc, "downloads", "a/one.mp4")
fav("alice", "downloads", "a/one.mp4")
check("hook: a file that is already converted is not queued again", not svc.queue.items)
# evicted for space: a new favourite of such a file stays out until a bigger cache or a manual request
svc = new_service()
svc.queue.record_evicted("downloads", "a/two.mp4", 150)
fav("alice", "downloads", "a/two.mp4")
check("hook: a favourite the cache evicted for space is not queued while the cap is the same", not svc.queue.items)
CFG["cache_max_gb"] = 200
fav("alice", "downloads", "a/two.mp4")
check("hook: ... but is once the cache has grown", ("downloads", "a/two.mp4") in items(svc))
CFG["cache_max_gb"] = 150

# ================================================================== backfill
svc = new_service()
NOW = time.time()
rows = [
    (NOW - 0.5 * DAY, "downloads", "a/fresh.mp4"),
    (NOW - 2.0 * DAY, "downloads", "a/pic1.jpg"),
    (NOW - 6.9 * DAY, "downloads", "a/mid.mp4"),
    (NOW - 7.1 * DAY, "downloads", "a/old.mp4"),          # just outside the window
    (NOW - 30 * DAY, "jellyfin", "j/film.mkv"),
    (NOW - 1.0 * DAY, "downloads", "a/song.mp3"),         # audio
    (NOW - 3.0 * DAY, "downloads", "a/pic2.png"),
    (NOW - 4.0 * DAY, "downloads", "a/gone.mp4"),         # file no longer there
]
res = svc.backfill(rows)
q = items(svc)
want = {("downloads", "a/fresh.mp4"), ("downloads", "a/pic1.jpg"), ("downloads", "a/mid.mp4"), ("downloads", "a/pic2.png")}
check("backfill: exactly the videos and pictures inside the 7-day window that exist are queued, favourites lane",
      set(q) == want and all(i["lane"] == stereo3d.LANE_FAVORITE for i in q.values()), sorted(q))
check("backfill: the counts say so (window 5 incl. the missing file, 4 queued, 1 skipped)",
      res["status"] == "ok" and res["window"] == 5 and res["queued"] == 4 and res["skipped"] == 1, res)
check("backfill: newest favourite first", [i["path"] for i in svc.queue.ordered()] ==
      ["a/fresh.mp4", "a/pic1.jpg", "a/pic2.png", "a/mid.mp4"], [i["path"] for i in svc.queue.ordered()])
rev = svc.backfill(list(reversed(rows)))
check("backfill: idempotent - a second run (rows in another order) adds nothing, changes nothing",
      rev["queued"] == 0 and rev["duplicate"] == 4 and set(items(svc)) == want
      and [i["path"] for i in svc.queue.ordered()] == ["a/fresh.mp4", "a/pic1.jpg", "a/pic2.png", "a/mid.mp4"], rev)
check("backfill: the result is kept for the settings card", svc.overview()["backfill"]["queued"] == 0
      and svc.last_backfill is rev)
svc2 = new_service()
CFG["favorites_backfill_days"] = 0
r0 = svc2.backfill(rows)
check("backfill: 0 days = off, nothing queued", r0["status"] == "off" and not svc2.queue.items)
CFG["favorites_backfill_days"] = 31
r31 = svc2.backfill(rows)
check("backfill: a wider window takes in the 30-day favourite (jellyfin, other root)",
      ("jellyfin", "j/film.mkv") in items(svc2) and ("downloads", "a/old.mp4") in items(svc2) and r31["queued"] == 6, r31)
CFG["favorites_backfill_days"] = 7
svc3 = new_service()
CFG["favorites_auto"] = False
check("backfill: favorites_auto off = nothing", svc3.backfill(rows)["status"] == "off" and not svc3.queue.items)
CFG["favorites_auto"] = True
CFG["enabled"] = False
check("backfill: 3D off = nothing", svc3.backfill(rows)["status"] == "off" and not svc3.queue.items)
CFG["enabled"] = True
# two admins, one file: the newer stamp counts
svc3.backfill([(NOW - 10 * DAY, "downloads", "a/old.mp4"), (NOW - 1 * DAY, "downloads", "a/old.mp4")])
check("backfill: a file favourited by two admins counts with its newest stamp",
      items(svc3).get(("downloads", "a/old.mp4"), {}).get("fav_at") == NOW - 1 * DAY)

# cached / manual / failed / evicted
svc = new_service()
cached(svc, "downloads", "a/fresh.mp4")
svc.request("downloads", "a/pic1.jpg", stereo3d.LANE_MANUAL, "alice")
svc.request("downloads", "a/mid.mp4", stereo3d.LANE_FAVORITE, "alice", fav_at=NOW - 6 * DAY)
mk = items(svc)[("downloads", "a/mid.mp4")]["key"]
svc.queue.update(mk, state="failed", attempts=2, error="boom")
svc.queue.record_evicted("downloads", "a/pic2.png", 150)
res = svc.backfill(rows)
q = items(svc)
check("backfill: a converted file is not queued again", ("downloads", "a/fresh.mp4") not in q and res["cached"] == 1, res)
check("backfill: a manual item stays manual, a failed one stays failed (no retry storm)",
      q[("downloads", "a/pic1.jpg")]["lane"] == stereo3d.LANE_MANUAL and q[("downloads", "a/mid.mp4")]["state"] == "failed")
check("backfill: the evicted-favourite record is respected (not queued, counted)",
      ("downloads", "a/pic2.png") not in q and res["evicted"] == 1, res)
check("backfill: a second run still adds nothing", svc.backfill(rows)["queued"] == 0)
CFG["cache_max_gb"] = 400
res = svc.backfill(rows)
check("backfill: once the cache has grown the evicted favourite is queued again",
      ("downloads", "a/pic2.png") in items(svc) and res["queued"] == 1, res)
CFG["cache_max_gb"] = 150
# the manual lane stays ahead of a big favourites backlog
check("backfill: a manual item is ahead of every favourites item", svc.queue.ordered()[0]["lane"] == stereo3d.LANE_MANUAL)

# ================================================================== the server's feed and signature
svc = new_service()
ns["_favorites"].update(loaded=True, data={
    "alice": {"downloads": {"a/one.mp4": NOW - DAY, "a/song.mp3": NOW}},
    "bob": {"jellyfin": {"j/film.mkv": NOW - 2 * DAY}},
    "guest1": {"downloads": {"a/two.mp4": NOW}}})
feed = sorted(ns["stereo3d_admin_favourites"]())
check("feed: the admins' favourites only, with their stamps and roots (guests left out)",
      {(r, p) for _, r, p in feed} == {("downloads", "a/one.mp4"), ("downloads", "a/song.mp3"), ("jellyfin", "j/film.mkv")}
      and {p: ts for ts, r, p in feed}["j/film.mkv"] == NOW - 2 * DAY, feed)
res = svc.backfill(ns["stereo3d_admin_favourites"]())
check("feed -> backfill: the guest's file, audio and nothing else are left out", set(items(svc)) ==
      {("downloads", "a/one.mp4"), ("jellyfin", "j/film.mkv")} and res["queued"] == 2, sorted(items(svc)))
sig0 = ns["_stereo3d_backfill_signature"]()
CFG["favorites_backfill_days"] = 14
check("signature: the window, favorites_auto, enabled and the cache cap all move it",
      ns["_stereo3d_backfill_signature"]() != sig0)
CFG["favorites_backfill_days"] = 7
check("signature: back to the old values, the old signature", ns["_stereo3d_backfill_signature"]() == sig0)
CFG["cache_max_gb"] = 151
check("signature: a bigger cache (unblocks evicted favourites) moves it", ns["_stereo3d_backfill_signature"]() != sig0)
CFG["cache_max_gb"] = 150

# ================================================================== the loop: start-up run, then only on a change
svc = new_service()
ns["_favorites"].update(loaded=True, data={"alice": {"downloads": {"a/one.mp4": NOW - DAY, "a/pic1.jpg": NOW - 2 * DAY}}})
calls = []
real_backfill = svc.backfill


def counted(rows_):
    calls.append(time.time())
    return real_backfill(rows_)


svc.backfill = counted


class FastEvent:
    """_stereo3d_backfill_wake with a short wait, so the test does not sit through 30 s."""

    def __init__(self):
        self.ev = threading.Event()

    def wait(self, timeout=None):
        return self.ev.wait(0.05)

    def set(self):
        self.ev.set()

    def clear(self):
        self.ev.clear()

    def is_set(self):
        return self.ev.is_set()


wake = FastEvent()
ns["_stereo3d_backfill_wake"] = wake
ns["time"] = types.SimpleNamespace(sleep=lambda s: None, time=time.time, strftime=time.strftime)
th = threading.Thread(target=ns["_stereo3d_backfill_loop"], daemon=True)
th.start()
time.sleep(0.6)
n1 = len(calls)
time.sleep(0.4)
check("loop: one backfill after start-up, then none while nothing changes (not a tight loop)",
      n1 == 1 and len(calls) == 1 and set(items(svc)) == {("downloads", "a/one.mp4"), ("downloads", "a/pic1.jpg")}, (n1, len(calls)))
CFG["favorites_backfill_days"] = 1
wake.set()
time.sleep(0.5)
check("loop: a changed setting runs it again", len(calls) == 2, len(calls))
CFG["favorites_backfill_days"] = 3650
ns["_favorites"]["data"]["alice"]["downloads"]["a/mid.mp4"] = NOW - 9 * DAY
time.sleep(0.5)
check("loop: ... also without a wake-up (it looks every 30 s in production), and takes in the new window",
      len(calls) == 3 and ("downloads", "a/mid.mp4") in items(svc), len(calls))
ns["time"] = time

# ================================================================== the settings page wakes it
CUR["user"] = USERS[0]
ns["_stereo3d_backfill_wake"] = threading.Event()
with app.test_request_context("/api/settings", method="PUT", json={"values": {"stereo3d.favorites_backfill_days": 14}}):
    resp = resolve_names(lambda: ns["put_settings"]())
status = resp[1] if isinstance(resp, tuple) else resp.status_code
check("settings: saving a stereo3d setting wakes the backfill", status == 200 and ns["_stereo3d_backfill_wake"].is_set())
check("settings: the backfill window is a setting (0..3650 days, default 7)",
      ns["DEFAULT_CONFIG"]["stereo3d"]["favorites_backfill_days"] == 7
      and ns["_settings_field"]("stereo3d.favorites_backfill_days")["max"] == 3650)
ns["_stereo3d_backfill_wake"].clear()
other = next(f["key"] for s in ns["SETTINGS_SCHEMA"] if s["id"] != "stereo3d" for f in s["fields"]
             if f.get("type") == "bool" and not f["key"].startswith("indexer."))
cur = ns["_dig"](ns["CONFIG"], other) if "_dig" in ns else None
with app.test_request_context("/api/settings", method="PUT", json={"values": {other: bool(cur)}}):
    resp = resolve_names(lambda: ns["put_settings"]())
check("settings: saving something else does not wake it", not ns["_stereo3d_backfill_wake"].is_set())

shutil.rmtree(TMP, ignore_errors=True)
print("ALL PASS" if all(RESULTS) else f"{RESULTS.count(False)} FAILED")
