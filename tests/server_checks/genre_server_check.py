"""The genre map route and the info panel's genre row, out of a server.py's
AST, over a temp AI store and library. The route's background build really
runs: genre_map.py (next to the server.py given) in a child process.

  venv/bin/python genre_server_check.py <server.py>
"""
import ast
import atexit
import os
import shutil
import subprocess
import sys
import tempfile
import time

BASE = os.environ.get("ATRIHORD_DIR") or os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SERVER = os.path.abspath(sys.argv[1])
STAGE = os.path.dirname(SERVER)
sys.path.insert(0, BASE)
sys.path.insert(0, STAGE)
tmp = tempfile.mkdtemp(prefix="genre_server_check_")
atexit.register(shutil.rmtree, tmp, True)
os.environ["KSF_MEDIA_AI_DIR"] = os.path.join(tmp, "ai")
os.makedirs(os.environ["KSF_MEDIA_AI_DIR"])
import numpy as np                   # noqa: E402
import media_ai                      # noqa: E402
import genre_map                     # noqa: E402

assert os.path.dirname(os.path.abspath(genre_map.__file__)) == STAGE
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


def load(name):
    exec(compile(ast.Module(body=[defs[name]], type_ignores=[]), SERVER, "exec"), ns)


def _names(code):
    out = set(code.co_names)
    for const in code.co_consts:
        if hasattr(const, "co_names"):
            out |= _names(const)
    return out


def F(name, seen=None):
    seen = set() if seen is None else seen
    if name not in ns and name in defs and name not in seen:
        seen.add(name)
        for used in _names(compile(ast.Module(body=[defs[name]], type_ignores=[]), SERVER, "exec")):
            if used not in ns:
                F(used, seen)
        load(name)
    return ns.get(name)


check("the server imports genre_map from beside it", ns.get("genre_map") is genre_map)

# The app's own directory for the child process: genre_map.py and media_ai.py.
for f in ("genre_map.py", "media_ai.py"):
    shutil.copy(os.path.join(STAGE, f), os.path.join(tmp, f))
lib = os.path.join(tmp, "lib")
priv = os.path.join(tmp, "private")
os.makedirs(os.path.join(lib, "music"))
os.makedirs(os.path.join(priv, "music"))
ADMIN = {"username": "alice", "role": "admin"}
GUEST = {"username": "gus", "role": "guest"}
USER = [ADMIN]
ROOTS = [{"id": "downloads", "name": "Downloads", "path": lib, "guest_visible": True},
         {"id": "private", "name": "Private", "path": priv, "guest_visible": False}]
ns["BASE_DIR"] = tmp
ns["DEFAULT_ROOT_ID"] = "downloads"
ns["get_authenticated_user"] = lambda: USER[0]
ns["is_admin"] = lambda user: bool(user) and user.get("role") == "admin"
ns["get_all_roots"] = lambda: ROOTS
ns["get_root"] = lambda root_id: next((r for r in ROOTS if r["id"] == root_id), None)
F("roots_for_user")
F("root_visible_to")
F("app")
app = ns["app"]
route = F("media_genre_map")
info_route = F("media_info")
F("start_genre_map_build")
F("genre_map_loaded")
F("genre_info")

rng = np.random.default_rng(5)
GENRES = [("Electronic", "Techno"), ("Rock", "Punk"), ("Classical", "Baroque")]
centres = rng.normal(0, 1, (3, 1280)).astype(np.float32)
conn = media_ai.connect(tmp)
ids = {}
for i in range(90):
    g = i % 3
    root = "private" if i % 10 == 9 else "downloads"
    path = f"music/{GENRES[g][1].lower()}{i:03d}.opus"
    mid = media_ai.upsert(conn, root, path, 500 + i, "audio", {"genre_model": "m", "tags": ""})
    media_ai.replace_genres(conn, mid, [(f"{GENRES[g][0]}---{GENRES[g][1]}", 0.7)])
    media_ai.store_embedding(conn, mid, "m", (centres[g] + rng.normal(0, 0.5, 1280)).astype("<f2").tobytes(), 1280)
    ids[(root, path)] = mid
# Analysed, nothing heard, no embedding: counts as analysed, is not on the map.
media_ai.upsert(conn, "downloads", "music/silence.opus", 10, "audio", {"genre_model": "m", "tags": ""})
# Not analysed yet.
media_ai.upsert(conn, "downloads", "music/later.opus", 11, "audio", {"tags": ""})
conn.commit()


def call(user=ADMIN, url="/api/media/genre-map"):
    USER[0] = user
    with app.test_request_context(url):
        rv = route() if url.startswith("/api/media/genre-map") else info_route()
    resp, code = rv if isinstance(rv, tuple) else (rv, rv.status_code)
    return code, resp.get_json()


def wait_build(seconds=300):
    # Done once the watcher thread has the exit code, not just the process.
    for _ in range(seconds * 4):
        if not ns["genre_map_building"]() and ns["_genre_map"]["exit"] is not None:
            return True
        time.sleep(0.25)
    return False


code, first = call()
check("before any map: an empty answer, and a build under way",
      code == 200 and first["tracks"] == [] and first["stale"] and first["building"], first)
check("... with the progress figures: 92 tracks, 91 analysed", (first["music"], first["analysed"]) == (92, 91),
      (first["music"], first["analysed"]))
check("the build finishes", wait_build())
check("... cleanly", ns["_genre_map"]["exit"] == 0, ns["_genre_map"]["last_line"])
code, data = call()
check("then the map: every track with an embedding, for an admin", code == 200 and len(data["tracks"]) == 90
      and not data["stale"] and not data["building"], (len(data["tracks"]), data["stale"]))
row = data["tracks"][0]
check("rows follow 'fields'", data["fields"] == ["id", "root", "path", "size", "genre", "styles", "xy", "xyz"]
      and len(row) == 8 and len(row[6]) == 2 and len(row[7]) == 3, row)
check("... the root as an index into 'roots'", {r["id"] for r in data["roots"]} == {"downloads", "private"}
      and all(0 <= t[1] < len(data["roots"]) for t in data["tracks"]))
check("genres and styles come with the map", set(data["parents"]) == {"Electronic", "Rock", "Classical"}
      and data["parents"][row[4]] and data["styles"][row[5][0][0]].startswith(data["parents"][row[4]] + "---"),
      (data["parents"], row))
code, g = call(GUEST)
check("a guest gets the tracks of the roots guests may see, and no others",
      len(g["tracks"]) == 81 and [r["id"] for r in g["roots"]] == ["downloads"], (len(g["tracks"]), g["roots"]))
USER[0] = None
with app.test_request_context("/api/media/genre-map"):
    rv = route()
check("signed out: 401", isinstance(rv, tuple) and rv[1] == 401)

# A track goes to the trash: gone from the map at once, rebuilt later.
victim = ("downloads", "music/techno000.opus")
media_ai.set_trashed(conn, [victim], True)
conn.commit()
code, data = call()
check("a trashed track leaves the map at once", len(data["tracks"]) == 89
      and victim[1] not in {t[2] for t in data["tracks"]}, len(data["tracks"]))
check("... the map is stale, but no second build within GENRE_MAP_REBUILD_S", data["stale"] and not data["building"],
      (data["stale"], data["building"]))
ns["GENRE_MAP_REBUILD_S"] = 0
code, data = call()
check("once allowed, the rebuild starts", data["building"], data["building"])
wait_build()
code, data = call()
check("... and the rebuilt map has caught up", not data["stale"] and len(data["tracks"]) == 89, data["stale"])
check("... starting from the previous one", "previous map" in (data["method"] or {}).get("2d", ""), data["method"])

# A build that fails: the reason for an admin, a plain message for a guest.
os.rename(os.path.join(tmp, "genre_map.py"), os.path.join(tmp, "genre_map.good"))
open(os.path.join(tmp, "genre_map.py"), "w").write("raise SystemExit('numpy exploded at /secret/path')\n")
ns["GENRE_MAP_REBUILD_S"] = 600      # one failing build, not one per call
ns["_genre_map"]["started"] = 0.0
conn2 = media_ai.connect(tmp)
mid = media_ai.upsert(conn2, "downloads", "music/new.opus", 12, "audio", {"genre_model": "m", "tags": ""})
media_ai.store_embedding(conn2, mid, "m", centres[0].astype("<f2").tobytes(), 1280)
conn2.commit()
call()
wait_build()
code, data = call()
check("a failed build: the map before it is still served", len(data["tracks"]) == 89, len(data["tracks"]))
check("... with the reason, for an admin", "numpy exploded" in (data["error"] or ""), data["error"])
code, g = call(GUEST)
check("... and without server details for a guest", g["error"] and "/secret" not in g["error"], g["error"])
os.replace(os.path.join(tmp, "genre_map.good"), os.path.join(tmp, "genre_map.py"))
code, data = call()
check("a failed build is not retried at once (the throttle holds)", not data["building"] and data["error"], data["building"])
ns["_genre_map"]["started"] = 0.0
call()
wait_build()
code, data = call()
check("the next build clears the error", data["error"] is None and len(data["tracks"]) == 90, data["error"])

# The info panel's row.
song = os.path.join(lib, "music", "punk001.opus")
subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "sine=frequency=330:duration=2", "-c:a", "libopus", song],
               check=True)
for rel in ("music/silence.opus", "music/later.opus"):
    shutil.copy(song, os.path.join(lib, rel))
from PIL import Image  # noqa: E402
Image.new("RGB", (8, 8)).save(os.path.join(lib, "music", "cover.jpg"))


def info(path, user=ADMIN):
    return call(user, f"/api/media/info?root=downloads&path={path}")[1]


i1 = info("music/punk001.opus")
check("info for an analysed track: its styles, and that it is on the map",
      i1.get("ai_genres") == {"done": True, "styles": [["Rock---Punk", 0.7]], "on_map": True}, i1.get("ai_genres"))
i2 = info("music/silence.opus")
check("... analysed with nothing heard: done, no styles, not on the map",
      i2.get("ai_genres") == {"done": True, "styles": [], "on_map": False}, i2.get("ai_genres"))
i3 = info("music/later.opus")
check("... not analysed yet: not done", i3.get("ai_genres", {}).get("done") is False, i3.get("ai_genres"))
i4 = info("music/cover.jpg")
check("a picture has no genre row", "ai_genres" not in i4 and i4.get("name") == "cover.jpg", sorted(i4))

allowed = F("GUEST_ALLOWED_FILES")
check("guests may load the map page and its script", {"genres.html", "js/genres.js"} <= allowed)

print("ALL PASS" if ok else "SOME CHECKS FAILED")
sys.exit(0 if ok else 1)
