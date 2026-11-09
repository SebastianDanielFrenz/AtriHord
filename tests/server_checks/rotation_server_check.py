"""Videos filmed sideways, against a STAGED server.py (out of its AST), media_ai.py and
media_indexer.py: the orientation pass's decision and how it is stored, the turn set by
hand, listings (turn + turned width/height), the portrait filter, thumbnails made turned,
and the /api/media/rotation route.

User, 2026-11-08: Morio CH and Matsurou Ch. videos lie on their side; "When something is
auto-rotated, there should always exist an override button".

Synthetic AI index, temp turn store, temp library with ffmpeg-made test videos; nothing
under /data2 is written.

  venv/bin/python rotation_server_check.py /tmp/rot-up/stage/server.py
"""
import ast
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile

BASE = os.environ.get("ATRIHORD_DIR") or os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SERVER = sys.argv[1]
STAGE = os.path.dirname(os.path.abspath(SERVER))
sys.path.insert(0, BASE)
sys.path.insert(0, STAGE)
tmp = tempfile.mkdtemp(prefix="rotation_check_")
store = os.path.join(tmp, "ai")
os.makedirs(store)
os.environ["KSF_MEDIA_AI_DIR"] = store
LIB = os.path.join(tmp, "lib")
os.makedirs(os.path.join(LIB, "youtube", "Morio CH"))
for name in ("config.json", "indexer_config.json"):
    if os.path.exists(os.path.join(BASE, name)):
        shutil.copy(os.path.join(BASE, name), os.path.join(tmp, name))

import media_ai                      # noqa: E402
import media_indexer                 # noqa: E402

ok = True


def check(label, cond, detail=""):
    global ok
    ok = ok and bool(cond)
    print(("PASS " if cond else "FAIL ") + label + (f"  ({detail})" if detail and not cond else ""))


check("media_ai is the staged one", os.path.dirname(os.path.abspath(media_ai.__file__)) == STAGE,
      media_ai.__file__)
check("media_indexer is the staged one", os.path.dirname(os.path.abspath(media_indexer.__file__)) == STAGE,
      media_indexer.__file__)

# ---------------------------------------------------------------- the decision
UP, LEFT = 2.0, -91.0


def frame(**turns):
    """A worker report: turns given as t90=[(score, up)], the rest empty."""
    rec = {"0": [], "90": [], "180": [], "270": []}
    for key, faces in turns.items():
        rec[key[1:]] = [[s, 120, u] for s, u in faces]
    return rec


sideways = frame(t0=[(0.75, LEFT)], t90=[(0.86, UP)], t180=[(0.7, 88.0)], t270=[(0.72, 179.0)])
upright = frame(t0=[(0.84, UP)], t90=[(0.7, 91.0)])
empty = frame()
decide = media_indexer.orient_decide
check("one frame votes for the turn holding its upright face", media_indexer.orient_vote(sideways) == 90)
check("a face that is not upright in any turn votes for nothing",
      media_indexer.orient_vote(frame(t0=[(0.9, LEFT)], t90=[(0.9, 60.0)])) is None)
check("an upright face below the vote threshold votes for nothing",
      media_indexer.orient_vote(frame(t90=[(0.55, UP)])) is None)
check("a face with no landmarks votes for nothing", media_indexer.orient_vote(frame(t90=[(0.9, None)])) is None)
d = decide([sideways] * 5)
check("five sideways frames: turned 90", d["deg"] == 90 and d["votes"]["90"] == 5 and d["frames"] == 5, d)
check("one sideways vote is not enough", decide([sideways, empty, empty, empty, empty])["deg"] == 0)
check("two sideways votes and nothing else are", decide([sideways, sideways, empty, empty, empty])["deg"] == 90)
check("two against one upright is enough (2:1)", decide([sideways, sideways, upright, empty])["deg"] == 90)
check("two against two upright is not", decide([sideways, sideways, upright, upright])["deg"] == 0)
check("upright frames: not turned", decide([upright] * 5)["deg"] == 0)
check("a stray upside-down frame does not turn an upright video",
      decide([upright, upright, frame(t180=[(0.66, UP)]), empty, empty])["deg"] == 0)
check("upside down when it clearly is", decide([frame(t180=[(0.8, UP)])] * 3)["deg"] == 180)
check("no faces anywhere: not turned", decide([empty] * 5) == {"deg": 0, "votes": {"0": 0, "90": 0, "180": 0, "270": 0},
                                                             "frames": 5})

# ---------------------------------------------------------------- the store
conn = media_ai.connect(tmp)
cols = {r[1] for r in conn.execute("PRAGMA table_info(media)")}
check("the index has the orient columns", {"orient", "orient_model"} <= cols, cols)
check("the orient stamp is a model key", "orient_model" in media_ai.MODEL_KEYS)
MODELS = media_indexer.models_for("orient")
check("the orient pass stamps orient_model", list(MODELS) == ["orient_model"], MODELS)

R = "downloads"
SIDE = "youtube/Morio CH/side [aaa].webm"
FLAT = "youtube/Morio CH/upright [bbb].webm"
NONE = "youtube/Morio CH/never looked [ccc].webm"


def make_video(rel, size="320x180", seconds=2):
    path = os.path.join(LIB, rel)
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"testsrc=size={size}:rate=10",
                    "-t", str(seconds), "-c:v", "libvpx-vp9", "-b:v", "200k", path], check=True)
    return path


for rel in (SIDE, FLAT, NONE):
    make_video(rel)
open(os.path.join(LIB, "youtube", "Morio CH", "poster.jpg"), "wb").write(b"\xff\xd8not really")


def fake_post(cfg, payloads, urgent=False):
    """The worker: every frame of SIDE is sideways, every other frame upright."""
    results = []
    for label, _blob in payloads:
        rec = sideways if "side [aaa]" in label else upright
        results.append({"name": label, "orient": rec})
    return {"results": results, "failed": [], "models": {"orient": "buffalo_l"}}


def fake_old_worker(cfg, payloads, urgent=False):
    return {"results": [{"name": label} for label, _b in payloads], "failed": [], "models": {"faces": "buffalo_l"}}


def entry(rel):
    p = os.path.join(LIB, rel)
    return (R, rel, p, os.path.getsize(p), "video")


cfg = {"tasks": "orient", "prep_workers": 2, "worker": "http://nowhere", "token": "",
       "request_timeout_s": 5}
media_indexer.post_batch = fake_old_worker
try:
    media_indexer.flush(conn, cfg, [entry(SIDE)], MODELS, False, False)
    raised = False
except RuntimeError:
    raised = True
check("a worker without the orient pass fails the sweep...", raised)
check("... and stamps nothing", media_ai.orientation(conn, R, SIDE) is None)

media_indexer.post_batch = fake_post
done, failed = media_indexer.flush(conn, cfg, [entry(SIDE), entry(FLAT)], MODELS, False, False)
check("a batch of two: both done", (done, failed) == (2, 0), (done, failed))
rec = media_ai.orientation(conn, R, SIDE)
check("the sideways one is stored turned 90, from all five frames",
      rec and rec["deg"] == 90 and rec["frames"] == len(media_indexer.ORIENT_POSITIONS), rec)
check("the upright one is stored 0", (media_ai.orientation(conn, R, FLAT) or {}).get("deg") == 0)
check("not looked at yet reads None", media_ai.orientation(conn, R, NONE) is None)
check("orientations lists only the turned one", media_ai.orientations(conn) == {(R, SIDE): 90},
      media_ai.orientations(conn))
check("the pass does not ask again for a done file",
      not media_ai.needs_work(conn, R, SIDE, os.path.getsize(os.path.join(LIB, SIDE)), MODELS))
check("... but does for a file it has not seen",
      media_ai.needs_work(conn, R, NONE, os.path.getsize(os.path.join(LIB, NONE)), MODELS))

# A file ffmpeg gets nothing out of is stamped "" and not retried.
broken = "youtube/Morio CH/broken [ddd].webm"
open(os.path.join(LIB, broken), "wb").write(b"not a video at all")
done, failed = media_indexer.flush(conn, cfg, [entry(broken)], MODELS, False, False)
check("an unreadable video is counted unusable", (done, failed) == (0, 1), (done, failed))
check("... stamped as looked at with nothing to see", media_ai.orientation(conn, R, broken) == {})
check("... and not asked for again",
      not media_ai.needs_work(conn, R, broken, os.path.getsize(os.path.join(LIB, broken)), MODELS))

# The walk of the orient pass offers videos only.
found = media_indexer.iter_candidates([(R, LIB)], conn, MODELS, 64 * 1024 * 1024)
check("the orient walk offers videos only, and only the ones not done",
      sorted(rel for _r, rel, _p, _s, _k in found) == [NONE], found)

media_ai.set_trashed(conn, [(R, SIDE)], True)
check("a trashed video leaves the turned list", media_ai.orientations(conn) == {})
media_ai.set_trashed(conn, [(R, SIDE)], False)

# The turned ones live in a table of their own, so the server's map never
# scans the media table (14 GB on prod: minutes per read).
side_rows = [tuple(r) for r in conn.execute("SELECT root, path, deg FROM media_orient")]
check("the side table holds the turned one only", side_rows == [(R, SIDE, 90)], side_rows)
seen_sql = []
conn.set_trace_callback(seen_sql.append)
media_ai.orientations(conn)
conn.set_trace_callback(None)
selects = [s for s in seen_sql if s.lstrip().upper().startswith("SELECT")]
plan = " | ".join(str(r[-1]) for r in conn.execute("EXPLAIN QUERY PLAN " + selects[0]))
check("the map reads the side table and looks each one up by (root, path)",
      "SCAN o" in plan and "SEARCH m USING INDEX" in plan and "SCAN m" not in plan, plan)
for _ in range(3):   # statistics that make "trashed IS NULL" look selective
    conn.execute("ANALYZE")
plan = " | ".join(str(r[-1]) for r in conn.execute("EXPLAIN QUERY PLAN " + selects[0]))
check("... in that order whatever the statistics say", plan.startswith("SCAN o"), plan)
conn.execute("DROP TABLE IF EXISTS sqlite_stat1")
conn.commit()

# A later look that finds it upright again takes the row out.
media_ai.set_orient(conn, R, FLAT, 270)
check("a turn found later adds a row", media_ai.orientations(conn).get((R, FLAT)) == 270)
media_ai.set_orient(conn, R, FLAT, 0)
check("... and upright again removes it", (R, FLAT) not in media_ai.orientations(conn))

# Forgetting a file forgets its turn (keyed by path: no cascade reaches it).
GONE = "youtube/Morio CH/gone [eee].webm"
media_ai.upsert(conn, R, GONE, 1, "video", {})
media_ai.set_orient(conn, R, GONE, 90)
conn.commit()
media_ai.forget(conn, [(R, GONE)])
check("forget drops the side-table row too",
      conn.execute("SELECT COUNT(*) FROM media_orient WHERE path = ?", (GONE,)).fetchone()[0] == 0)


# Two threads migrating at once: the second ALTER finds the column there.
class StaleLook:
    """A connection whose PRAGMA was taken before the other thread's ALTER."""
    def __init__(self, real):
        self.real = real

    def execute(self, sql, *a):
        if sql.startswith("PRAGMA table_info"):
            return iter([])
        return self.real.execute(sql, *a)


try:
    media_ai._add_column(StaleLook(conn), "media", "orient", "TEXT")
    race_ok = True
except Exception as exc:   # noqa: BLE001
    race_ok = exc
check("a column added by another connection meanwhile is not an error", race_ok is True, race_ok)
try:
    media_ai._add_column(StaleLook(conn), "no_such_table", "x", "TEXT")
    other = False
except sqlite3.OperationalError:
    other = True
check("... while other ALTER failures still raise", other)

# ---------------------------------------------------------------- the server
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


ns["media_ai"] = media_ai
ns["CONFIG_FILE"] = os.path.join(tmp, "config.json")
ns["USERS_FILE"] = os.path.join(tmp, "users.json")
ns["get_authenticated_user"] = lambda: {"username": "alice", "role": "admin"}
for name in ("BASE_DIR", "app", "ROTATION_TURNS", "ROTATION_STORE_MAX", "ORIENT_REFRESH_S", "_orient_cache",
             "_orient_lock", "_orient_build_lock", "_rotation_lock", "_rotation_overrides", "TURN_FILTERS",
             "THUMB_MAX_DIMENSION", "_thumb_build_semaphore", "DEFAULT_ROOT_ID",
             "orientation_map", "rotation_overrides", "set_rotation_override", "turn_of", "turned_dims",
             "rotation_info", "media_rotation", "attach_media_dimensions", "thumb_version",
             "get_video_frame_thumbnail", "_cached_thumbnail", "kinds_of"):
    load(name)
# Everything that would touch the live app points into the temp dir, BEFORE first use.
ns["BASE_DIR"] = tmp
ns["ROTATION_STORE"] = os.path.join(tmp, "rotations.json")
ns["THUMB_CACHE_DIR"] = os.path.join(tmp, "thumbs")
ns["resolve_media_path"] = lambda root, rel: os.path.join(LIB, rel) if root == R else None
ns["root_visible_to"] = lambda user, root: root == R
DIMS = {f"{R}|{SIDE}": [320, 180, 1, 2.0], f"{R}|{FLAT}": [320, 180, 1, 2.0]}
ns["media_dimensions_map"] = lambda: DIMS
ns["queue_dimensions"] = lambda items: None
app = ns["app"]


def call(name, *args):
    return resolve(lambda: ns[name](*args))


check("the turn store is the temp one", ns["ROTATION_STORE"].startswith(tmp))
check("the pass's turn is read from the index", call("orientation_map") == {(R, SIDE): 90})
check("a sideways video turns 90 automatically", call("turn_of", R, SIDE)[:2] == (90, 90))
check("an upright one does not turn", call("turn_of", R, FLAT)[:2] == (0, 0))
check("turned_dims swaps a quarter turn", call("turned_dims", [320, 180, 5, 2.0], 90) == [180, 320, 5, 2.0])
check("... not a half turn", call("turned_dims", [320, 180, 5], 180) == [320, 180, 5])
check("... and leaves unknown alone", call("turned_dims", None, 90) is None)


def listing():
    entries = [{"name": os.path.basename(p), "path": p, "root": R, "mtime": 1000.0, "size": 1}
               for p in (SIDE, FLAT)]
    for e in entries:
        e["size"] = DIMS[f"{R}|{e['path']}"][2]
    return {e["path"]: e for e in call("attach_media_dimensions", entries, R)}


got = listing()
check("listing: the sideways one carries its turn",
      got[SIDE].get("rotate") == 90 and got[SIDE].get("rotate_auto") == 90 and got[SIDE].get("rotate_set") is False,
      got[SIDE])
check("listing: ... and its upright shape", (got[SIDE]["width"], got[SIDE]["height"]) == (180, 320), got[SIDE])
check("listing: the upright one carries nothing", "rotate" not in got[FLAT] and got[FLAT]["width"] == 320, got[FLAT])
check("thumbnail URL key follows the turn", call("thumb_version", got[SIDE]) == "1000r90"
      and call("thumb_version", got[FLAT]) == 1000)

# ---------------------------------------------------------------- set by hand
client = app.test_client()
# Inside a route a missing name is a 500, not a NameError the loader sees: call
# what the route uses once directly, so everything it needs is loaded.
check("a .webm is a video", "video" in call("kinds_of", "a.webm"))
call("rotation_info", R, SIDE)
call("set_rotation_override", R, "warm.webm", None, "nobody")


def post(body):
    r = client.post("/api/media/rotation", json=body)
    return r.status_code, r.get_json()


code, data = post({"root": R, "path": SIDE, "deg": 0})
check("set to as filmed by hand", code == 200 and data["deg"] == 0 and data["auto"] == 90 and data["set"] == 0
      and data["by"] == "alice", data)
check("... the pass's answer is still reported", (data.get("pass") or {}).get("deg") == 90, data)
got = listing()
check("listing: as filmed by hand", got[SIDE].get("rotate") == 0 and got[SIDE].get("rotate_set") is True
      and got[SIDE]["width"] == 320, got[SIDE])
check("thumbnail URL key back to unturned", call("thumb_version", got[SIDE]) == 1000)
code, data = post({"root": R, "path": FLAT, "deg": 270})
check("a missed one turned by hand", code == 200 and data["deg"] == 270 and data["auto"] == 0, data)
check("listing: ... turned, upright shape", listing()[FLAT].get("rotate") == 270
      and listing()[FLAT]["height"] == 320)
code, data = post({"root": R, "path": FLAT, "deg": -90})
check("-90 is 270", code == 200 and data["deg"] == 270, data)
ns["_rotation_overrides"]["loaded"] = False
ns["_rotation_overrides"]["entries"] = {}
check("hand-set turns survive a restart", call("turn_of", R, FLAT)[0] == 270 and call("turn_of", R, SIDE)[0] == 0)
code, data = post({"root": R, "path": SIDE, "deg": None})
check("back to automatic", code == 200 and data["deg"] == 90 and data["set"] is None, data)
check("... and gone from the file", SIDE not in json.load(open(ns["ROTATION_STORE"], encoding="utf-8")).get(R, {}))
check("a turn of 45 is refused", post({"root": R, "path": SIDE, "deg": 45})[0] == 400)
check("a word is refused", post({"root": R, "path": SIDE, "deg": "left"})[0] == 400)
check("a picture is refused", post({"root": R, "path": "youtube/Morio CH/poster.jpg", "deg": 90})[0] == 400)
check("a missing file is 404", post({"root": R, "path": "youtube/Morio CH/gone.webm", "deg": 90})[0] == 404)
check("a root the user cannot see is refused", post({"root": "secret", "path": SIDE, "deg": 90})[0] == 403)
ns["get_authenticated_user"] = lambda: None
check("signed out is refused", post({"root": R, "path": SIDE, "deg": 90})[0] == 401)
ns["get_authenticated_user"] = lambda: {"username": "alice", "role": "admin"}

# ---------------------------------------------------------------- the portrait filter
load("apply_media_filters")
filters = ("vertical", None, None, None, None, None, None, None)
entries = [{"name": os.path.basename(p), "path": p, "root": R} for p in (SIDE, FLAT)]
kept = [e["path"] for e in call("apply_media_filters", entries, filters, R)]
check("the portrait filter finds the turned videos", sorted(kept) == sorted([SIDE, FLAT]), kept)
post({"root": R, "path": FLAT, "deg": 0})
entries = [{"name": os.path.basename(p), "path": p, "root": R} for p in (SIDE, FLAT)]
kept = [e["path"] for e in call("apply_media_filters", entries, filters, R)]
check("... and not one shown as filmed", kept == [SIDE], kept)

# ---------------------------------------------------------------- thumbnails
def thumb_size(turn):
    got = call("get_video_frame_thumbnail", os.path.join(LIB, SIDE), turn)
    if not got:
        return None
    path = os.path.join(tmp, f"t{turn}.jpg")
    open(path, "wb").write(got[1])
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                          "stream=width,height", "-of", "csv=p=0", path], capture_output=True, text=True).stdout
    return tuple(int(x) for x in out.strip().split(","))


check("the thumbnail as filmed is landscape", thumb_size(0) == (320, 180), thumb_size(0))
check("turned 90 it is portrait", thumb_size(90) == (180, 320), thumb_size(90))
check("turned 270 it is portrait", thumb_size(270) == (180, 320))
check("turned 180 it stays landscape", thumb_size(180) == (320, 180))
check("each turn is its own cache entry", len(os.listdir(ns["THUMB_CACHE_DIR"])) == 4,
      os.listdir(ns["THUMB_CACHE_DIR"]))
check("... and the unturned one keeps the key it always had (old cache stays valid)",
      __import__("hashlib").sha256(os.path.join(LIB, SIDE).encode()).hexdigest() + ".jpg"
      in os.listdir(ns["THUMB_CACHE_DIR"]))

shutil.rmtree(tmp, ignore_errors=True)
print("ALL PASS" if ok else "SOME FAILED")
sys.exit(0 if ok else 1)
