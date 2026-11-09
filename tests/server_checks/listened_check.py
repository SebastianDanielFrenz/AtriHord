"""Time listened per account and song (music_listened.py, vault KSF-052): the
store itself, and /api/music/listened out of a STAGED server.py's AST with a
fake catalogue. Nothing of the real library or the real store is touched.

  venv/bin/python listened_check.py <staged server.py>
"""
import ast
import atexit
import json
import os
import shutil
import sqlite3
import sys
import tempfile

BASE = os.environ.get("ATRIHORD_DIR") or os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SERVER = os.path.abspath(sys.argv[1])
STAGE = os.path.dirname(SERVER)
sys.path.insert(0, BASE)
sys.path.insert(0, STAGE)
tmp = tempfile.mkdtemp(prefix="listened_check_")
atexit.register(shutil.rmtree, tmp, True)
os.environ["KSF_MEDIA_AI_DIR"] = os.path.join(tmp, "ai")
os.makedirs(os.environ["KSF_MEDIA_AI_DIR"])
import music_library as ml  # noqa: E402
import music_listened as mlt  # noqa: E402

assert os.path.dirname(os.path.abspath(mlt.__file__)) == STAGE, mlt.__file__
ok = True


def check(label, cond, detail=""):
    global ok
    ok = ok and bool(cond)
    print(("PASS " if cond else "FAIL ") + label + (f"  ({detail})" if detail and not cond else ""), flush=True)


# ---- the store ------------------------------------------------------------------
H = 1790240000 // 3600 * 3600          # the start of an hour
SONG = {"title": "Astronomia", "artist": "Vicetone", "album": "Legacy", "genre": "Progressive House",
        "duration": 201.5, "root": "downloads", "path": "youtube_music/Vicetone/Astronomia.opus"}
st = mlt.Listened(os.path.join(tmp, "store.sqlite"))
st.add("alice", [("v:vid00000001", 60, SONG)], now=H + 10)
st.add("alice", [("v:vid00000001", 30.5, SONG)], now=H + 3000)
st.add("alice", [("v:vid00000001", 45, SONG), ("p:lib1:music/x.m4a", 20, None)], now=H + 3600 + 5)
rows = sqlite3.connect(st.path).execute("SELECT user, ref, hour, seconds FROM listened ORDER BY hour, ref").fetchall()
check("one row per account, song and hour; the same hour adds up",
      rows == [("alice", "v:vid00000001", H // 3600, 90.5), ("alice", "p:lib1:music/x.m4a", H // 3600 + 1, 20.0),
               ("alice", "v:vid00000001", H // 3600 + 1, 45.0)], rows)
st.add("alice", [("v:vid00000001", 0, SONG), ("v:vid00000001", -5, SONG)], now=H + 20)
check("nothing or less than nothing adds nothing",
      sqlite3.connect(st.path).execute("SELECT SUM(seconds) FROM listened").fetchone()[0] == 155.5)
st.add("bob", [("v:vid00000001", 10 ** 9, SONG)], now=H + 86400)
check("one report adds at most MAX_ITEM_SECONDS to a song",
      sqlite3.connect(st.path).execute("SELECT seconds FROM listened WHERE user = 'bob'").fetchone()[0]
      == mlt.MAX_ITEM_SECONDS)
snap = sqlite3.connect(st.path).execute("SELECT title, artist, album, genre, duration, root, path FROM songs"
                                        " WHERE ref = 'v:vid00000001'").fetchone()
check("the song's names are kept for the charts", snap == ("Astronomia", "Vicetone", "Legacy", "Progressive House",
                                                          201.5, "downloads", SONG["path"]), snap)
st.add("alice", [("v:vid00000001", 1, dict(SONG, title="Astronomia (Remastered)"))], now=H + 30)
check("... and follow a rename", sqlite3.connect(st.path).execute(
    "SELECT title FROM songs WHERE ref = 'v:vid00000001'").fetchone()[0] == "Astronomia (Remastered)")
check("a ref heard without a catalogue entry has no names row",
      sqlite3.connect(st.path).execute("SELECT COUNT(*) FROM songs WHERE ref = 'p:lib1:music/x.m4a'").fetchone()[0] == 0)
s = st.summary("alice")
check("summary: the total, songs by time listened, time per UTC day",
      s["seconds"] == 156.5 and [x["ref"] for x in s["songs"]] == ["v:vid00000001", "p:lib1:music/x.m4a"]
      and s["songs"][0]["seconds"] == 136.5 and s["songs"][0]["title"] == "Astronomia (Remastered)"
      and s["songs"][1]["title"] is None and s["days"] == [[H // 86400 * 86400, 156.5]], s)
check("... since a time: only the hours from then", st.summary("alice", since=H + 3600)["seconds"] == 65.0,
      st.summary("alice", since=H + 3600))
check("... limited", len(st.summary("alice", limit=1)["songs"]) == 1)
check("another account has only its own", st.summary("bob")["seconds"] == mlt.MAX_ITEM_SECONDS
      and st.summary("carol") == {"seconds": 0, "songs": [], "days": []}, st.summary("carol"))
again = mlt.Listened(st.path)
check("read back from disk", again.summary("alice") == s)

# ---- the server -----------------------------------------------------------------
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
    for _ in range(300):
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


check("the server imports music_listened from its own directory", ns.get("music_listened") is mlt)
for name in ("app", "DEFAULT_ROOT_ID", "MUSIC_REF_RE", "MUSIC_LISTENED_MAX_ITEMS", "_music_lock", "_music_stores",
             "_music_file_songs"):
    load(name)
ns["MUSIC_LISTENED_FILE"] = os.path.join(tmp, "music_listened.sqlite")
ADMIN = {"username": "alice", "role": "admin"}
GUEST = {"username": "gus", "role": "guest"}
who = {"user": ADMIN}
ns["get_authenticated_user"] = lambda: who["user"]
ns["roots_for_user"] = lambda user: [{"id": "downloads"}] + ([{"id": "lib1"}] if user["role"] == "admin" else [])
ASTRO = {"title": "Astronomia", "artist": "Vicetone", "album": "Legacy", "genre": "Progressive House",
         "duration": 201.5, "root": "downloads", "path": "youtube_music/Vicetone/Astronomia.opus",
         "video_id": "vid00000001", "cover": False, "added": 1,
         "names": [["downloads", "youtube_music/Crash Games UHD/Phonk/Astronomia.opus"]],
         "video": ["downloads", "Music Videos/Vicetone/Astronomia.webm"],
         "videos": [["downloads", "Music Videos/Vicetone/Astronomia.webm"], ["lib1", "mv/Astronomia.mkv"]]}
UNTITLED = {"title": "untitled", "artist": "Unknown artist", "album": "", "genre": None, "duration": 100,
            "root": "lib1", "path": "music/untitled.m4a", "video_id": None, "cover": False, "added": 1,
            "names": [], "video": None, "videos": []}
OLDCAT = {"title": "Old", "artist": "Someone", "album": "", "genre": None, "duration": 50,
          "root": "downloads", "path": "youtube_music/Someone/Old.opus", "video_id": "vid00000009", "cover": False,
          "added": 1, "names": [], "video": ["downloads", "Music Videos/Someone/Old.mp4"]}   # no "videos": older catalogue
catalog = {"songs": [ASTRO, UNTITLED, OLDCAT], "playlists": [], "channels": [], "albums": [], "built": 1}
state = {"catalog": catalog}
ns["music_catalog"] = lambda force=False: (state["catalog"], False)
app = ns["app"]


def call(fn, path, method="GET", body=None):
    if fn not in ns:
        load(fn)
    kw = {"json": body} if body is not None else {}
    with app.test_request_context(path, method=method, base_url="https://music.example.org", **kw):
        rv = resolve(lambda: ns[fn]())
    resp, code = (rv[0], rv[1]) if isinstance(rv, tuple) else (rv, rv.status_code)
    return code, resp.get_json()


def post(items):
    return call("music_listened_add", "/api/music/listened", "POST", {"items": items})


def totals(user="alice"):
    return {r: s for r, s in sqlite3.connect(ns["MUSIC_LISTENED_FILE"]).execute(
        "SELECT ref, SUM(seconds) FROM listened WHERE user = ? GROUP BY ref", (user,))}


who["user"] = None
check("signed out: 401", post([{"ref": "v:vid00000001", "s": 5}])[0] == 401)
who["user"] = ADMIN
for label, body in (("no items", {}), ("items not a list", {"items": "x"}), ("an empty list", {"items": []}),
                    ("more than the most", {"items": [{"ref": "v:vid00000001", "s": 1}] * 51})):
    code, _ = call("music_listened_add", "/api/music/listened", "POST", body)
    check(f"400 for {label}", code == 400, code)
for label, item in (("no seconds", {"ref": "v:vid00000001"}), ("seconds as text", {"ref": "v:vid00000001", "s": "5"}),
                    ("seconds true", {"ref": "v:vid00000001", "s": True}), ("no seconds at all", {"ref": "v:vid00000001", "s": 0}),
                    ("negative seconds", {"ref": "v:vid00000001", "s": -3}), ("not a ref", {"ref": "hello", "s": 5}),
                    ("neither ref nor file", {"s": 5}), ("not an object", 5)):
    code, _ = post([item])
    check(f"400 for {label}", code == 400, code)
check("nothing was written by any of those", not os.path.exists(ns["MUSIC_LISTENED_FILE"]) or totals() == {})

code, data = post([{"ref": "v:vid00000001", "s": 61.5}])
check("the music page's ref: counted", code == 200 and data == {"counted": [True]} and totals() == {"v:vid00000001": 61.5},
      (code, data, totals()))
code, data = post([{"ref": "p:downloads:youtube_music/Crash Games UHD/Phonk/Astronomia.opus", "s": 10}])
check("a playlist link's ref counts as the song, under the song's own ref",
      data == {"counted": [True]} and totals() == {"v:vid00000001": 71.5}, totals())
code, data = post([{"ref": "v:vid0000000X", "s": 10}])
check("a ref no longer in the library: not counted", data == {"counted": [False]} and totals() == {"v:vid00000001": 71.5})

code, data = post([{"root": "downloads", "path": "youtube_music/Vicetone/Astronomia.opus", "s": 5},
                   {"root": "downloads", "path": "youtube_music/Crash Games UHD/Phonk/Astronomia.opus", "s": 5},
                   {"root": "downloads", "path": "Music Videos/Vicetone/Astronomia.webm", "s": 5},
                   {"root": "lib1", "path": "mv/Astronomia.mkv", "s": 5},
                   {"root": "downloads", "path": "Music Videos/Someone/Old.mp4", "s": 7},
                   {"root": "downloads", "path": "twitter/@a/1_1.mp4", "s": 30}])
check("media-dl's files: the song under any name, and every copy of its music video, count as the song;"
      " a video that is no song does not",
      data == {"counted": [True, True, True, True, True, False]}
      and totals() == {"v:vid00000001": 91.5, "v:vid00000009": 7.0}, (data, totals()))
code, data = post([{"root": "", "path": "youtube_music/Vicetone/Astronomia.opus", "s": 1.5}])
check("a blank root is the downloads root", data == {"counted": [True]} and totals()["v:vid00000001"] == 93.0)
snap = sqlite3.connect(ns["MUSIC_LISTENED_FILE"]).execute("SELECT title, artist, path FROM songs WHERE ref = 'v:vid00000009'").fetchone()
check("... and the song's names were kept", snap == ("Old", "Someone", "youtube_music/Someone/Old.opus"), snap)

who["user"] = GUEST
code, data = post([{"root": "lib1", "path": "mv/Astronomia.mkv", "s": 5}, {"ref": "p:lib1:music/untitled.m4a", "s": 5},
                   {"root": "lib1", "path": "music/untitled.m4a", "s": 5},
                   {"root": "downloads", "path": "Music Videos/Vicetone/Astronomia.webm", "s": 4}])
check("a guest: nothing in a root they cannot see counts; what they can see does",
      data == {"counted": [False, False, False, True]} and totals("gus") == {"v:vid00000001": 4.0}, (data, totals("gus")))
who["user"] = ADMIN
code, data = post([{"ref": "p:lib1:music/untitled.m4a", "s": 2}])
check("a song with no video id is filed under its path ref",
      data == {"counted": [True]} and totals()["p:lib1:music/untitled.m4a"] == 2.0)

state["catalog"] = None
code, data = post([{"ref": "v:vid00000077", "s": 3}])
check("while the first catalogue is built a ref is taken as it is", data == {"counted": [True]}
      and totals()["v:vid00000077"] == 3.0)
code, data = post([{"root": "downloads", "path": "Music Videos/Vicetone/Astronomia.webm", "s": 3}])
check("... but a file cannot be told from a song yet: 503, the page sends it again", code == 503, (code, data))
state["catalog"] = catalog

code, data = call("music_listened_summary", "/api/music/listened")
check("GET: the viewer's own summary", code == 200 and data["songs"][0]["ref"] == "v:vid00000001"
      and data["songs"][0]["title"] == "Astronomia" and abs(data["seconds"] - 105.0) < 0.01, data)
code, data = call("music_listened_summary", "/api/music/listened?days=1&limit=1")
check("... ?days and ?limit", code == 200 and len(data["songs"]) == 1 and abs(data["seconds"] - 105.0) < 0.01, data)
who["user"] = GUEST
code, data = call("music_listened_summary", "/api/music/listened?days=abc")
check("... another account sees only its own (a bad days is everything)", code == 200 and data["seconds"] == 4.0, data)
who["user"] = None
check("... signed out: 401", call("music_listened_summary", "/api/music/listened")[0] == 401)

print("ALL PASS" if ok else "SOME FAILED")
sys.exit(0 if ok else 1)
