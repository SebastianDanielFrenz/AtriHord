"""Main-face naming and on-demand dimensions. Out of a server.py's AST plus the
media_faces.py / media_ai.py next to it, over a temp AI store and library:
media_faces.auto_names, the real /api/media/people route, and
attach_media_dimensions with its background measuring.

  venv/bin/python mainface_check.py <server.py>
"""
import ast
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

BASE = os.environ.get("ATRIHORD_DIR") or os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SERVER = sys.argv[1]
sys.path.insert(0, BASE)
sys.path.insert(0, os.path.dirname(os.path.abspath(SERVER)))
tmp = tempfile.mkdtemp(prefix="mainface_check_")
os.environ["KSF_MEDIA_AI_DIR"] = os.path.join(tmp, "ai")
os.makedirs(os.environ["KSF_MEDIA_AI_DIR"])
import media_ai                      # noqa: E402
import media_faces                   # noqa: E402
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


R = "downloads"
lib = os.path.join(tmp, "lib")
os.makedirs(lib)
ns["BASE_DIR"] = tmp
ns["DEFAULT_ROOT_ID"] = R
USER = {"username": "alice", "role": "admin"}
ns["get_authenticated_user"] = lambda: USER
ns["get_root"] = lambda root_id: {"id": R, "path": lib} if root_id == R else None
ns["roots_for_user"] = lambda user: [{"id": R}]
F("app")
app = ns["app"]

# ------------------------------------------------------------ main faces --
conn = media_ai.connect(tmp)
nfile = [0]


def put(account, clusters, trashed=False, kind="image", unclustered=0):
    """One file from twitter/<account> (or elsewhere when account is None) with
    a face per cluster listed (repeat a cluster for a video's frames)."""
    nfile[0] += 1
    path = (f"twitter/{account}/f{nfile[0]}.jpg" if account else f"pixiv/1/f{nfile[0]}.jpg")
    cur = conn.execute("INSERT INTO media (root, path, size, kind, trashed) VALUES (?, ?, 1, ?, ?)",
                       (R, path, kind, 1.0 if trashed else None))
    for cid in clusters:
        conn.execute("INSERT INTO faces (media_id, bbox, cluster_id, quality) VALUES (?, '[0,0,9,9]', ?, 1)",
                     (cur.lastrowid, cid))
    for _ in range(unclustered):
        conn.execute("INSERT INTO faces (media_id, bbox, cluster_id, quality) VALUES (?, '[0,0,9,9]', NULL, 1)",
                     (cur.lastrowid,))


for cid in (1, 2, 3, 4, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15):
    conn.execute("INSERT INTO clusters (id, cover_face, face_count) VALUES (?, NULL, 50)", (cid,))
for _ in range(20):
    put("@solo", [1])                                   # 20 of 20 alone with face 1
for _ in range(12):
    put("@edge", [2])                                   # exactly 75%: 12 of 16
for _ in range(4):
    put("@edge", [9])                                   # ...the rest show someone else
for _ in range(10):
    put("@group", [7])                                  # alone in half the pictures,
for _ in range(10):
    put("@group", [7, 8])                               # beside face 8 in the other half
for _ in range(14):
    put("@small", [3])                                  # 14 files: too few
for _ in range(14):
    put("@split", [4])                                  # 70%: 14 of 20
for _ in range(6):
    put("@split", [6])
for _ in range(16):
    put("@second", [1])                                 # a second account of face 1
for _ in range(3):
    put("@second", [1, 1, 1], kind="video")             # one person in every frame: alone, once each
for _ in range(10):
    put("@solo", [1], trashed=True)                     # trashed: not counted
for _ in range(30):
    put("@solo", [], unclustered=1)                     # drawings: faces never clustered, not counted
for _ in range(5):
    put(None, [1])                                      # face 1 outside twitter: in its total only
for i in range(300):
    put("@big", [10] if i < 186 else [11])              # 62% of 300: over the 60% floor
for i in range(300):
    put("@bigno", [12] if i < 174 else [13])            # 58% of 300: under it
for i in range(100):
    put("@mid", [14] if i < 64 else [15])               # 64% of 100: a 100-file account needs 65.5%
conn.commit()

names = media_faces.auto_names(conn)
tied, _ = media_faces.account_main_faces(conn)
check("an account whose every face file shows one face is tied to it", tied.get("@solo", {}).get("cluster") == 1, tied)
check("exactly 75% is enough", tied.get("@edge", {}).get("cluster") == 2
      and tied["@edge"]["shows"] == 12 and tied["@edge"]["files"] == 16, tied.get("@edge"))
check("pictures with a second person still count for the account's face (50% alone, 100% showing it)",
      tied.get("@group", {}).get("cluster") == 7 and tied["@group"]["shows"] == 20, tied.get("@group"))
check("under 15 files with a face: not tied", "@small" not in tied)
check("70% of 20 files: not tied (20 files need 73.6%)", "@split" not in tied)
need = media_faces.main_face_share_needed
check("the share needed falls with size, to a floor: 75% at 15, 65.5% at 100, 60% from 300",
      need(15) == 0.75 and need(10) == 0.75 and round(need(100), 3) == 0.655 and need(300) == need(5000) == 0.6
      and need(20) > need(50) > need(200) > need(300), [round(need(n), 4) for n in (15, 20, 50, 100, 200, 300)])
check("a big account is tied at 62%", tied.get("@big", {}).get("cluster") == 10, tied.get("@big"))
check("... not at 58%, under the floor", "@bigno" not in tied)
check("a 100-file account at 64% is not (it needs 65.5%)", "@mid" not in tied)
check("a video with the same person in every frame counts once",
      tied.get("@second", {}).get("files") == 19 and tied["@second"]["shows"] == 19, tied.get("@second"))
check("trashed files and never-clustered (drawn) faces are not counted", tied["@solo"]["files"] == 20, tied["@solo"])
check("face 1 is named after BOTH its accounts, most pictures first",
      names.get(1, {}).get("name") == "@solo, @second" and names[1]["handles"] == ["@solo", "@second"], names.get(1))
check("... its share counts every file of the face, twitter or not",
      names[1]["share"] == round(39 / 44, 3) and names[1]["total"] == 44, names[1])
check("the faces of untied accounts get no name", set(names) == {1, 2, 7, 10}, sorted(names))
check("face 8 (only ever beside @group's face) is not named after @group", 8 not in names)

with app.test_request_context("/api/media/people?min_size=1"):
    rv = F("media_people")()
people = {p["id"]: p for p in rv.get_json()["items"]}
p1 = people.get(1, {})
check("the People route sends the whole name", p1.get("auto_name") == "@solo, @second", p1)
check("... and every account, with its numbers",
      p1.get("auto_accounts") == [{"handle": "@solo", "files": 20, "account_files": 20},
                                  {"handle": "@second", "files": 19, "account_files": 19}],
      p1.get("auto_accounts"))
check("... an unnamed face sends an empty list", people.get(3, {}).get("auto_accounts") == [], people.get(3))

# ------------------------------------------------- on-demand dimensions --
vid = os.path.join(lib, "clip.av1.mp4")
subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=24:duration=2",
                "-c:v", "libx264", "-pix_fmt", "yuv420p", vid], check=True)
Image.new("RGB", (40, 30)).save(os.path.join(lib, "pic.png"))
open(os.path.join(lib, "broken.mp4"), "wb").write(b"not a video")
media_dimensions = ns["media_dimensions"]
media_dimensions.save(tmp, {"downloads|other.mp4": [1, 2, 3, 24.0, 9.0],
                            "downloads|pic.png": [40, 30, 999]})      # measured at another size


def listing():
    entries = [{"name": n, "path": n, "root": R, "size": os.path.getsize(os.path.join(lib, n))}
               for n in ("clip.av1.mp4", "pic.png", "broken.mp4")]
    return {e["path"]: e for e in F("attach_media_dimensions")(entries)}


F("attach_media_dimensions")
real_queue = ns["queue_dimensions"]
queued = []


def recording_queue(items):
    items = list(items)
    queued.extend(k for k, _full in items)
    return real_queue(items)


ns["queue_dimensions"] = recording_queue
with ns["_dimensions_lock"]:
    ns["_dimensions_cache"]["checked_at"] = 0.0
first = listing()
check("first listing: the new video has no length yet", "duration" not in first["clip.av1.mp4"], first)
check("... and it was queued, with the replaced picture and the broken file",
      sorted(queued) == ["downloads|broken.mp4", "downloads|clip.av1.mp4", "downloads|pic.png"], queued)
for _ in range(100):
    if not ns["_dims_worker"]["running"]:
        break
    time.sleep(0.1)
store = media_dimensions.load(tmp)
check("the background thread measured the video", store.get("downloads|clip.av1.mp4", [None] * 5)[4] == 2.0,
      store.get("downloads|clip.av1.mp4"))
check("... re-measured the picture at its new size", store.get("downloads|pic.png") ==
      [40, 30, os.path.getsize(os.path.join(lib, "pic.png"))], store.get("downloads|pic.png"))
check("... and kept what it did not touch", store.get("downloads|other.mp4") == [1, 2, 3, 24.0, 9.0])
check("an unmeasurable file is remembered, not retried", "downloads|broken.mp4" in ns["_dims_failed"])
second = listing()
check("the next listing shows the length", second["clip.av1.mp4"].get("duration") == 2.0, second["clip.av1.mp4"])
check("... and queues nothing new (the broken file is not asked again)",
      queued[3:] == ["downloads|broken.mp4"] or queued[3:] == [], queued)
check("... the worker is idle again", not ns["_dims_queue"] and not ns["_dims_worker"]["running"],
      (ns["_dims_queue"], ns["_dims_worker"]))

shutil.rmtree(tmp, ignore_errors=True)
print("ALL PASS" if ok else "SOME CHECKS FAILED")
