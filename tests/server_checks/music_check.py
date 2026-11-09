"""music.example.org: music_library's catalogue over a temp library of real
audio (made with ffmpeg), and the server's /api/music/* routes and host mapping
out of a STAGED server.py's AST. Nothing of the real library is read.

  venv/bin/python music_check.py <staged server.py>
"""
import ast
import atexit
import io
import json
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
tmp = tempfile.mkdtemp(prefix="music_check_")
atexit.register(shutil.rmtree, tmp, True)
os.environ["KSF_MEDIA_AI_DIR"] = os.path.join(tmp, "ai")
os.makedirs(os.environ["KSF_MEDIA_AI_DIR"])
import music_library as ml  # noqa: E402

assert os.path.dirname(os.path.abspath(ml.__file__)) == STAGE, ml.__file__
ok = True


def check(label, cond, detail=""):
    global ok
    ok = ok and bool(cond)
    print(("PASS " if cond else "FAIL ") + label + (f"  ({detail})" if detail and not cond else ""), flush=True)


def audio(path, seconds=1, freq=440, tags=None, cover=False):
    """A real audio file; `cover` adds an mjpeg picture stream the way the
    Jellyfin library's .mka files carry one (not flagged attached_pic)."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    cmd = ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"sine=frequency={freq}:duration={seconds}"]
    if cover:
        cmd += ["-f", "lavfi", "-i", "color=c=red:s=64x64:d=1", "-map", "0:a", "-map", "1:v", "-c:v", "mjpeg",
                "-frames:v", "1"]
    codec = {".opus": ["-c:a", "libopus"], ".m4a": ["-c:a", "aac"], ".mka": ["-c:a", "libopus"]}[os.path.splitext(path)[1]]
    cmd += codec
    for k, v in (tags or {}).items():
        cmd += ["-metadata", f"{k}={v}"]
    subprocess.run(cmd + [path], check=True)
    return path


down = os.path.join(tmp, "downloads")
lib = os.path.join(tmp, "lib")
store = os.path.join(down, "youtube_music")
astro = audio(os.path.join(store, "Vicetone", "Astronomia.opus"), freq=440,
              tags={"title": "Astronomia", "artist": "Vicetone", "album": "Legacy",
                    "comment": "https://www.youtube.com/watch?v=vid00000001"})
time.sleep(0.05)
nevada = audio(os.path.join(store, "Vicetone", "Nevada.opus"), freq=550,
               tags={"title": "Nevada", "artist": "Vicetone - Topic", "album": "Legacy"})
os.makedirs(os.path.join(store, "Crash Games UHD", "Phonk"))
os.makedirs(os.path.join(store, "Crash Games UHD", "Anime Epic"))
os.link(astro, os.path.join(store, "Crash Games UHD", "Phonk", "Astronomia.opus"))
os.link(nevada, os.path.join(store, "Crash Games UHD", "Phonk", "Nevada.opus"))
os.link(astro, os.path.join(store, "Crash Games UHD", "Anime Epic", "Astronomia.opus"))
single = audio(os.path.join(store, "Alan Walker", "Alone.opus"), freq=660,
               tags={"title": "Alone", "artist": "Alan Walker", "album": "Alone"})
scopin = audio(os.path.join(lib, "music", "Film", "Kordhell - Scopin [abcdefghijk].mka"), freq=330, cover=True)
bare = audio(os.path.join(lib, "music", "Film", "untitled thing.m4a"), freq=220)
json.dump({"vid00000002": "youtube_music/Vicetone/Nevada.opus"}, open(os.path.join(store, ".songs.json"), "w"))


def walk(roots):
    out = []
    for rid, base in roots:
        for d, dirs, files in os.walk(base):
            dirs[:] = [x for x in dirs if not x.startswith(".")]
            for f in files:
                if ml.is_audio(f) and not f.startswith("."):
                    full = os.path.join(d, f)
                    out.append((rid, os.path.relpath(full, base).replace(os.sep, "/"), full, os.stat(full)))
    return out


ROOTS = [("downloads", down), ("lib1", lib)]
tags = ml.TagCache(os.path.join(tmp, "tags.json"))
calls = {"n": 0}
real_probe = ml.probe


def counting_probe(path):
    calls["n"] += 1
    return real_probe(path)


ml.probe = counting_probe
registry = {"downloads:youtube_music/Crash Games UHD/Phonk": {
    "url": "https://music.youtube.com/playlist?list=PLphonk", "tracks": ["vid00000002", "vid00000001"],
    "title": "Phonk", "synced_at": 1790000000}}
cat = ml.build(walk(ROOTS), tags, "downloads",
               songs_index=json.load(open(os.path.join(store, ".songs.json"))), registry=registry,
               genres={("downloads", "youtube_music/Vicetone/Astronomia.opus"): "Progressive House"},
               root_names={"downloads": "Downloads", "lib1": "Lib"})
songs = cat["songs"]
by_title = {s["title"]: s for s in songs}

# ---- one song, one entry ---------------------------------------------------
check("five songs from eight names: a song in three places is one song", len(songs) == 5 and
      sorted(by_title) == ["Alone", "Astronomia", "Nevada", "Scopin", "untitled thing"], sorted(by_title))
check("... played from the song store's own name under its artist",
      by_title["Astronomia"]["path"] == "youtube_music/Vicetone/Astronomia.opus", by_title["Astronomia"]["path"])
check("... and it knows all its names", len(by_title["Astronomia"]["names"]) == 3)
check("tags read: artist, album, duration, a video id from the comment",
      by_title["Astronomia"]["artist"] == "Vicetone" and by_title["Astronomia"]["album"] == "Legacy"
      and 0.5 < by_title["Astronomia"]["duration"] < 1.5 and by_title["Astronomia"]["video_id"] == "vid00000001",
      by_title["Astronomia"])
check("\"<Artist> - Topic\" is shown as the artist", by_title["Nevada"]["artist"] == "Vicetone")
check("a video id from the song store's index when the file has none",
      by_title["Nevada"]["video_id"] == "vid00000002", by_title["Nevada"]["video_id"])
check("the AI index's style comes along", by_title["Astronomia"]["genre"] == "Progressive House")
sc = by_title["Scopin"]
check("an untagged \"Artist - Title [id]\" file: artist and title from its name, id too",
      sc["artist"] == "Kordhell" and sc["title"] == "Scopin" and sc["video_id"] == "abcdefghijk", sc)
check("a Matroska cover held as a plain mjpeg stream is found", sc["cover"] is True)
check("a file that says nothing: its name, and an unknown artist",
      by_title["untitled thing"]["artist"] == "Unknown artist")
check("opus with no picture: no cover", by_title["Astronomia"]["cover"] is False)

# ---- playlists, channels, albums ----------------------------------------------------
pls = {p["title"]: p for p in cat["playlists"]}
check("playlists: the two downloaded ones and the library folder", sorted(pls) == ["Anime Epic", "Film", "Phonk"],
      sorted(pls))
phonk = pls["Phonk"]
check("a downloaded playlist is its owner's, from YouTube Music, with its link and last sync",
      phonk["owner"] == "Crash Games UHD" and phonk["source"] == "youtube" and phonk["channel"] == "Crash Games UHD"
      and phonk["url"].endswith("PLphonk") and phonk["synced"] == 1790000000, phonk)
check("... in YouTube Music's order when its source is known (Nevada, then Astronomia)",
      [songs[i]["title"] for i in phonk["songs"]] == ["Nevada", "Astronomia"],
      [songs[i]["title"] for i in phonk["songs"]])
film = pls["Film"]
check("a library folder is its library's playlist, with the path above it",
      film["source"] == "library" and film["owner"] == "Lib" and film["where"] == "music" and len(film["songs"]) == 2,
      film)
chans = {c["name"]: c for c in cat["channels"]}
check("channels: every artist folder in the song store", sorted(chans) == ["Alan Walker", "Crash Games UHD", "Vicetone"],
      sorted(chans))
check("a channel holds its songs", sorted(songs[i]["title"] for i in chans["Vicetone"]["songs"]) == ["Astronomia", "Nevada"])
check("... and the playlists in its folder (the account they were made with)",
      len(chans["Crash Games UHD"]["playlists"]) == 2 and not chans["Crash Games UHD"]["songs"],
      chans["Crash Games UHD"])
check("an album: two songs with one album tag and artist", [(a["title"], a["artist"], len(a["songs"]))
                                                           for a in cat["albums"]] == [("Legacy", "Vicetone", 2)],
      cat["albums"])

# ---- the tag cache --------------------------------------------------------------------
tags.save()
first_reads = calls["n"]
tags2 = ml.TagCache(os.path.join(tmp, "tags.json"))
calls["n"] = 0
ml.build(walk(ROOTS), tags2, "downloads")
check("a rebuild reads no file again", calls["n"] == 0 and first_reads == 5, (first_reads, calls["n"]))
os.remove(bare)
ml.build(walk(ROOTS), tags2, "downloads")
check("... and forgets the files that are gone", len(tags2.data) == 4, len(tags2.data))
bare = audio(os.path.join(lib, "music", "Film", "untitled thing.m4a"), freq=220)

# ---- what one viewer sees ----------------------------------------------------------------
view = ml.for_viewer(cat, {"downloads"}, {"u00000000001": {"name": "Mine", "updated": 5,
                                                          "songs": ["v:vid00000001", "p:downloads:youtube_music/Alan Walker/Alone.opus",
                                                                    "p:lib1:music/Film/untitled thing.m4a", "v:gone0000000"]}},
                     can_sync=False)
titles = [r[0] for r in view["songs"]]
check("a viewer without the library root sees none of its songs", sorted(titles) == ["Alone", "Astronomia", "Nevada"],
      titles)
check("... nor its playlist", sorted(p["title"] for p in view["playlists"]) == ["Anime Epic", "Phonk"])
mine = view["mine"][0]
check("their own playlist: songs by id and by path, the hidden and the gone left out",
      [titles[i] for i in mine["songs"]] == ["Astronomia", "Alone"] and len(mine["refs"]) == 4, mine)
check("rows carry what the page reads", view["fields"][:3] == ["title", "artist", "album"] and view["can_sync"] is False)

# ---- the best copy of a music video ------------------------------------------------------------
# User, 2026-09-23: "If the video length of all options is identical, it should
# serve the highest resolution and framerate variant available."
orig = ("lib1", "MMD/4K60/Song [vid00000001].webm")
fast = ("lib1", "MMD/MMD 4K120/Song [vid00000001].webm")
small = ("lib2", "x/Song [vid00000001].mkv")
DIMS = {orig: {"width": 3840, "height": 2160, "fps": 60.0, "duration": 127.89},
        fast: {"width": 3840, "height": 2160, "fps": 120.0, "duration": 127.88},
        small: {"width": 1920, "height": 1080, "fps": 120.0, "duration": 127.9}}
measured = lambda r, p: DIMS.get((r, p))
check("no measurements: the plain original first, as before",
      ml._rank_videos([fast, orig], "lib1") == [orig, fast], ml._rank_videos([fast, orig], "lib1"))
got = ml._rank_videos([small, orig, fast], "lib1", measured)
check("the same cut: the most pixels first, then the most frames", got == [fast, orig, small], got)
longer = {**DIMS, fast: dict(DIMS[fast], duration=131.2)}
check("a copy of another length: the original stays first",
      ml._rank_videos([orig, fast], "lib1", lambda r, p: longer.get((r, p)))[0] == orig)
check("a copy not measured yet: the original stays first",
      ml._rank_videos([orig, fast], "lib1", lambda r, p: None if (r, p) == fast else DIMS.get((r, p)))[0] == orig)
astro_i = next(i for i, s in enumerate(cat["songs"]) if s["title"] == "Astronomia")
kept = dict(cat["songs"][astro_i])
cat["songs"][astro_i].update(video=["lib2", "best.webm"], videos=[["lib2", "best.webm"], ["downloads", "plain.webm"]])
seen = ml.for_viewer(cat, {"downloads"})
row = next(r for r in seen["songs"] if r[0] == "Astronomia")
check("a viewer who cannot see the best copy's root is given the best one they can",
      row[11] == ["downloads", "plain.webm"], row[11])
seen = ml.for_viewer(cat, {"downloads", "lib2"})
row = next(r for r in seen["songs"] if r[0] == "Astronomia")
check("... and one who can, the best", row[11] == ["lib2", "best.webm"], row[11])
cat["songs"][astro_i] = kept

# ---- your own playlists, stored ---------------------------------------------------------------
up = ml.UserPlaylists(os.path.join(tmp, "playlists.json"))
pid, pl = up.create("alice", "  Road trip  ")
check("a playlist is made, its name trimmed", pl["name"] == "Road trip" and pid.startswith("u") and len(pid) == 12)
up.update("alice", pid, add=["v:vid00000001", "v:vid00000002", "v:vid00000001"])
check("adding a song twice keeps it once", up.of("alice")[pid]["songs"] == ["v:vid00000001", "v:vid00000002"])
up.update("alice", pid, songs=["v:vid00000002", "v:vid00000001"], name="Night drive")
check("reordered and renamed", up.of("alice")[pid]["songs"] == ["v:vid00000002", "v:vid00000001"]
      and up.of("alice")[pid]["name"] == "Night drive")
up.update("alice", pid, remove=["v:vid00000002"])
check("a song removed", up.of("alice")[pid]["songs"] == ["v:vid00000001"])
check("another account sees none of it", up.of("bob") == {})
check("reloaded from disk it is the same", ml.UserPlaylists(os.path.join(tmp, "playlists.json")).of("alice") == up.of("alice"))
check("deleted", up.delete("alice", pid) and up.of("alice") == {} and not up.delete("alice", pid))

check("playlist links: YouTube Music and YouTube playlist links pass, others do not",
      ml.playlist_url_ok("https://music.youtube.com/playlist?list=PLx")
      and ml.playlist_url_ok("https://www.youtube.com/playlist?list=PLx")
      and not ml.playlist_url_ok("https://music.youtube.com/watch?v=abc")
      and not ml.playlist_url_ok("https://evil.example/playlist?list=PLx"))

# ---- radios: places on the genre map, and what each account listened to ---------------------------
LINKED = ("downloads", "youtube_music/Crash Games UHD/Phonk/Astronomia.opus")
view = ml.for_viewer(cat, {"downloads", "lib1"}, positions={LINKED: [0.123456, -0.5, 2]})
row = {r[0]: r for r in view["songs"]}
p3 = view["fields"].index("p3")
check("a song gets its genre-map place through ANY of its names (here a playlist's link), rounded",
      row["Astronomia"][p3] == [0.1235, -0.5, 2.0], row["Astronomia"][p3])
check("... a song the map does not know gets 0", row["Nevada"][p3] == 0 and row["Alone"][p3] == 0)
check("... and a viewer with no listening yet gets the defaults (autoplay on)",
      view["me"] == {"events": [], "radios": [], "autoplay": True}, view["me"])

ls = ml.Listening(os.path.join(tmp, "listening.json"))
check("a new account: nothing played, autoplay on", ls.summary("alice") == {"events": [], "radios": [], "autoplay": True})
ls.record("alice", "v:vid00000001", "user", now=100)
ls.record("alice", "v:vid00000002", "list", now=101)
RADIO = {"id": "song:v:vid00000001", "kind": "song", "key": "v:vid00000001", "title": "Astronomia radio"}
ls.record("alice", "v:vid00000001", "radio", RADIO, now=102)
ls.record("alice", "v:vid00000002", "radio", RADIO, now=103)
ls.record("alice", "v:vid00000001", "radio", RADIO, now=104)
s = ls.summary("alice")
check("plays are kept in order with how they came to play",
      [e[:2] for e in s["events"]] == [["v:vid00000001", "user"], ["v:vid00000002", "list"], ["v:vid00000001", "radio"],
                                       ["v:vid00000002", "radio"], ["v:vid00000001", "radio"]], s["events"])
r = ls.radio("alice", RADIO["id"])
check("a radio remembers what it played, a song played again moved to the end (it is the last one)",
      r["played"] == ["v:vid00000002", "v:vid00000001"] and r["started"] == 102 and r["t"] == 104, r)
check("... the summary names it without the played list, with its count and last song",
      s["radios"] == [{"id": RADIO["id"], "kind": "song", "key": "v:vid00000001", "title": "Astronomia radio",
                       "started": 102, "t": 104, "count": 2, "last": "v:vid00000001"}], s["radios"])
OTHER = {"id": "genre:Phonk", "kind": "genre", "key": "Phonk", "title": "Phonk radio"}
ls.record("alice", "v:vid00000002", "radio", OTHER, now=105)
ls.record("alice", "v:vid00000001", "radio", RADIO, now=106)
check("the radio listened to last comes first", [x["id"] for x in ls.summary("alice")["radios"]] == [RADIO["id"], OTHER["id"]])
# Where a song was played from, kept with the play for the history.
ls.record("alice", "v:vid00000001", "user", now=110, ctx="pl:u0123456789a")
ls.record("alice", "v:vid00000002", "list", now=111, ctx="pl:u0123456789a")
ls.record("alice", "v:vid00000001", "user", now=112)
ev = ls.summary("alice")["events"]
check("a play keeps where it came from, a play without one stays three long",
      ev[-3:] == [["v:vid00000001", "user", 110, "pl:u0123456789a"],
                  ["v:vid00000002", "list", 111, "pl:u0123456789a"],
                  ["v:vid00000001", "user", 112]], ev[-3:])
page = ls.history("alice", limit=3)
check("the history reads newest first, a page at a time",
      [e[2] for e in page["events"]] == [112, 111, 110] and page["more"] is True, page)
older = ls.history("alice", before=page["events"][-1][2], limit=2)
check("... and before= walks on into the ones behind it",
      [e[2] for e in older["events"]] == [106, 105] and older["more"] is True, older)
check("... the last page says so", ls.history("alice", before=100, limit=50)["more"] is False)
check("another account's history is empty, not theirs",
      ls.history("bob") == {"events": [], "more": False})

small = ml.Listening(os.path.join(tmp, "listening_small.json"))
small.MAX_EVENTS, small.MAX_RADIOS, small.MAX_PLAYED = 3, 2, 2
small.SUMMARY_EVENTS = 2
for n in range(5):
    small.record("bob", f"p:downloads:song{n}.opus", "radio",
                 {"id": f"genre:G{n % 3}", "kind": "genre", "key": f"G{n % 3}", "title": f"G{n % 3}"}, now=n)
small.record("bob", "p:downloads:a.opus", "radio", {"id": "genre:G1", "kind": "genre", "key": "G1", "title": "G1"}, now=9)
small.record("bob", "p:downloads:b.opus", "radio", {"id": "genre:G1", "kind": "genre", "key": "G1", "title": "G1"}, now=10)
sb = small.summary("bob")
check("the library is handed only the newest few plays, the history has them all",
      len(sb["events"]) == 2 and len(small.history("bob", limit=50)["events"]) == 3,
      (sb["events"], small.history("bob", limit=50)["events"]))
small.SUMMARY_EVENTS = 300
sb = small.summary("bob")
check("bounded: the newest events, radios and played songs are what stays",
      len(sb["events"]) == 3 and [x["id"] for x in sb["radios"]] == ["genre:G1", "genre:G0"]
      and small.radio("bob", "genre:G1")["played"] == ["p:downloads:a.opus", "p:downloads:b.opus"],
      (sb, small.radio("bob", "genre:G1")))
ls.set_autoplay("alice", False)
again = ml.Listening(os.path.join(tmp, "listening.json"))
check("the autoplay switch and everything else read back from disk", again.summary("alice") == ls.summary("alice")
      and again.summary("alice")["autoplay"] is False)
check("another account has none of it", ls.summary("bob") == {"events": [], "radios": [], "autoplay": True}
      and ls.radio("bob", RADIO["id"]) is None)

# ---- the server ----------------------------------------------------------------------------
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


check("the server imports music_library from its own directory", ns.get("music_library") is ml)
for name in ("app", "DEFAULT_ROOT_ID", "MUSIC_COVER_SIZES", "MUSIC_REF_RE", "USER_PLAYLIST_ID_RE", "_music_lock",
             "_music", "_music_stores", "GUEST_ALLOWED_FILES", "task_lock", "active_tasks", "MUSIC_REBUILD_MIN_S"):
    load(name)
ns["BASE_DIR"] = tmp
ns["DOWNLOADS_DIR"] = down
ns["WEB_FILES_DIR"] = os.path.join(tmp, "web_files")
os.makedirs(os.path.join(tmp, "web_files"))
for page in ("index.html", "music.html", "music_share.html", "public_share.html"):
    open(os.path.join(tmp, "web_files", page), "w").write(page)
ns["MUSIC_SOURCES_FILE"] = os.path.join(tmp, "music_sources.json")
ns["MUSIC_PLAYLISTS_FILE"] = os.path.join(tmp, "music_playlists.json")
ns["MUSIC_LISTENING_FILE"] = os.path.join(tmp, "music_listening.json")
# The genre map, faked: what genre_map.py last wrote, and the AI index's counts.
GMAP = {"data": {"count": 2, "tracks": [{"root": LINKED[0], "path": LINKED[1], "p3": [0.1, 0.2, 0.3]},
                                        {"root": "downloads", "path": "gone.opus", "p3": None}]}}
COUNTS = {"now": (2, 5)}
builds = []
ns["genre_map_loaded"] = lambda: GMAP["data"]
ns["_genre_map"] = {"mtime": 1.0, "idsum": 5}
ns["genre_map_counts"] = lambda conn: COUNTS["now"]
ns["start_genre_map_build"] = lambda: builds.append(1) or True
ns["media_ai"] = type("FakeAI", (), {"connect": staticmethod(lambda base: None)})
ns["GENRE_MAP_REBUILD_S"] = 600
# music_positions checks the map inside a try that only logs: a name missing
# there is swallowed, never loaded on demand - so it is loaded up front.
load("genre_map_behind")
ns["MUSIC_TAGS_FILE"] = os.path.join(tmp, "music_tags.json")
ns["MUSIC_COVER_DIR"] = os.path.join(tmp, "covers")
ADMIN = {"username": "alice", "role": "admin"}
GUEST = {"username": "gus", "role": "guest"}
who = {"user": ADMIN}
ns["get_authenticated_user"] = lambda: who["user"]
ns["roots_for_user"] = lambda user: [{"id": "downloads"}] + ([{"id": "lib1"}] if user["role"] == "admin" else [])
ns["root_visible_to"] = lambda user, rid: rid in {r["id"] for r in ns["roots_for_user"](user)}
ns["resolve_media_path"] = lambda rid, rel: os.path.join({"downloads": down, "lib1": lib}[rid], rel) \
    if rid in ("downloads", "lib1") and ".." not in rel else None
app = ns["app"]
state = {"catalog": None, "building": True}
ns["music_catalog"] = lambda force=False: (state["catalog"], state["building"])


def call(fn, path, method="GET", body=None, host="media.example.org", kw=None, **ctx):
    if body is not None:
        ctx["json"] = body
    if fn not in ns:
        load(fn)
    with app.test_request_context(path, method=method, base_url=f"https://{host}", **ctx):
        rv = resolve(lambda: ns[fn](**(kw or {})))
    if isinstance(rv, tuple):
        resp, code = rv[0], rv[1]
    else:
        resp, code = rv, rv.status_code
    resp.direct_passthrough = False          # send_file answers stream; the checks read them whole
    return code, resp


code, resp = call("music_library_api", "/api/music/library")
check("the library while the first build runs: 202, building", code == 202 and resp.get_json()["building"] is True)
state.update(catalog=cat, building=False)
code, resp = call("music_library_api", "/api/music/library", headers={"Accept-Encoding": "gzip"})
raw = resp.get_data()
import gzip  # noqa: E402
data = json.loads(gzip.decompress(raw) if resp.headers.get("Content-Encoding") == "gzip" else raw)
check("the library for an admin: every root, may sync", code == 200 and len(data["songs"]) == 5 and data["can_sync"] is True,
      (code, len(data.get("songs", [])), data.get("can_sync")))
check("... a small answer is sent as it is", resp.headers.get("Content-Encoding") is None)
big = {"songs": [dict(songs[0], title=f"Song {n}", path=f"youtube_music/X/Song {n}.opus", names=[]) for n in range(400)],
       "playlists": [], "channels": [], "albums": [], "built": 1}
state.update(catalog=big)
code, resp = call("music_library_api", "/api/music/library", headers={"Accept-Encoding": "gzip, br"})
check("... a library-sized one is gzipped when the browser takes it", code == 200
      and resp.headers.get("Content-Encoding") == "gzip"
      and len(json.loads(gzip.decompress(resp.get_data()))["songs"]) == 400)
state.update(catalog=cat)
who["user"] = GUEST
code, resp = call("music_library_api", "/api/music/library")
data = resp.get_json()
check("the library for a guest: only the roots they may see, no sync", code == 200 and len(data["songs"]) == 3
      and data["can_sync"] is False)
who["user"] = None
code, _ = call("music_library_api", "/api/music/library")
check("signed out: 401", code == 401)
who["user"] = ADMIN

code, resp = call("music_playlist_create", "/api/music/playlists", "POST",
                  {"name": "Mine", "songs": ["v:vid00000001", "not a ref", 5, "p:downloads:youtube_music/Alan Walker/Alone.opus"]})
body = resp.get_json()
pid = body.get("id")
check("a playlist made with songs, anything not a song reference dropped", code == 200 and
      body["playlist"]["songs"] == ["v:vid00000001", "p:downloads:youtube_music/Alan Walker/Alone.opus"], body)
code, resp = call("music_playlist_edit", f"/api/music/playlists/{pid}", "PATCH", {"songs": "nope"}, kw={"pid": pid})
check("songs must be a list", code == 400)
code, resp = call("music_playlist_edit", f"/api/music/playlists/{pid}", "PATCH", {"name": "Renamed", "remove": ["v:vid00000001"]},
                  kw={"pid": pid})
check("renamed and a song removed", code == 200 and resp.get_json()["playlist"]["name"] == "Renamed"
      and resp.get_json()["playlist"]["songs"] == ["p:downloads:youtube_music/Alan Walker/Alone.opus"], resp.get_json())
who["user"] = GUEST
code, _ = call("music_playlist_edit", f"/api/music/playlists/{pid}", "DELETE", kw={"pid": pid})
check("another account cannot touch it (it is not theirs to find)", code == 404)
who["user"] = ADMIN
code, _ = call("music_playlist_edit", "/api/music/playlists/../x", "DELETE", kw={"pid": "../x"})
check("a malformed id is simply not found", code == 404)
code, _ = call("music_playlist_edit", f"/api/music/playlists/{pid}", "DELETE", kw={"pid": pid})
check("deleted by its owner", code == 200)

# ---- radios ---------------------------------------------------------------------------------------
load("music_positions")
pos = ns["music_positions"]()
check("positions: every track the map placed, by (root, path); one without a place left out",
      pos == {LINKED: [0.1, 0.2, 0.3]} and builds == [], (pos, builds))
COUNTS["now"] = (3, 9)
ns["music_positions"]()
check("... a map behind the AI index is not checked again within GENRE_MAP_REBUILD_S", builds == [])
ns["_music_map"]["checked"] = 0.0
ns["music_positions"]()
check("... after that, a map behind it starts a rebuild (new songs join the radios without the map page)",
      builds == [1])
GMAP["data"] = {"count": 3, "tracks": GMAP["data"]["tracks"] + [{"root": "downloads", "path": "new.opus", "p3": [1, 1, 1]}]}
check("... the same map file is not read again", len(ns["music_positions"]()) == 1)
ns["_genre_map"]["mtime"] = 2.0
check("... a new one is", len(ns["music_positions"]()) == 2)

def events_of(user):
    return [e[:2] for e in ns["music_listening_store"]().summary(user)["events"]]


code, resp = call("music_played", "/api/music/played", "POST", {"ref": "v:vid00000001", "src": "user"})
check("a play picked by hand is kept", code == 200 and events_of("alice") == [["v:vid00000001", "user"]],
      (code, resp.get_json()))
code, resp = call("music_played", "/api/music/played", "POST",
                  {"ref": "v:vid00000002", "src": "list", "ctx": "pl:u0123456789a"})
check("a play says where it was played from", code == 200
      and ns["music_listening_store"]().summary("alice")["events"][-1]
      == ["v:vid00000002", "list", ns["music_listening_store"]().summary("alice")["events"][-1][2],
          "pl:u0123456789a"], resp.get_json())
code, _ = call("music_played", "/api/music/played", "POST",
               {"ref": "v:vid00000001", "src": "user", "ctx": "x" * 161})
check("refused: a context longer than the store takes", code == 400)
code, _ = call("music_played", "/api/music/played", "POST",
               {"ref": "v:vid00000001", "src": "user", "ctx": ["pl", "x"]})
check("refused: a context that is not a string", code == 400)
SONG_RADIO = {"id": "song:v:vid00000001", "kind": "song", "key": "v:vid00000001", "title": "Astronomia radio"}
code, _ = call("music_played", "/api/music/played", "POST", {"ref": "v:vid00000002", "src": "radio", "radio": SONG_RADIO})
check("a radio's play is kept with its radio", code == 200
      and ns["music_listening_store"]().radio("alice", SONG_RADIO["id"])["played"] == ["v:vid00000002"])
for label, body in (
        ("a ref that is not a song reference", {"ref": "../etc", "src": "user"}),
        ("an unknown src", {"ref": "v:vid00000001", "src": "robot"}),
        ("a radio play without its radio", {"ref": "v:vid00000001", "src": "radio"}),
        ("a radio sent with a play that is not a radio's", {"ref": "v:vid00000001", "src": "user", "radio": SONG_RADIO}),
        ("an unknown radio kind", {"ref": "v:vid00000001", "src": "radio", "radio": dict(SONG_RADIO, kind="mood",
                                                                                         id="mood:v:vid00000001")}),
        ("a radio whose id is not its kind and key", {"ref": "v:vid00000001", "src": "radio",
                                                      "radio": dict(SONG_RADIO, id="song:v:vid00000002")}),
        ("a song radio keyed by something not a song", {"ref": "v:vid00000001", "src": "radio",
                                                        "radio": {"kind": "song", "key": "hello", "title": "x"}}),
        ("a radio that is not an object", {"ref": "v:vid00000001", "src": "radio", "radio": "song:x"}),
        ("a title too long", {"ref": "v:vid00000001", "src": "radio", "radio": dict(SONG_RADIO, title="x" * 201)})):
    code, _ = call("music_played", "/api/music/played", "POST", body)
    check(f"refused: {label}", code == 400, code)
code, resp = call("music_history", "/api/music/history?limit=2")
first = resp.get_json()
check("the history route: the newest plays first, and that there are more",
      code == 200 and len(first["events"]) == 2 and first["events"][0][2] >= first["events"][1][2]
      and first["more"] is True, first)
code, resp = call("music_history", f"/api/music/history?before={first['events'][-1][2]}&limit=2")
check("... ?before= is the page behind it",
      code == 200 and all(e[2] < first["events"][-1][2] for e in resp.get_json()["events"]), resp.get_json())
code, resp = call("music_history", "/api/music/history?limit=nonsense&before=nonsense")
check("... unreadable numbers fall back instead of failing", code == 200)
code, resp = call("music_radio_state", "/api/music/radio?id=" + SONG_RADIO["id"])
check("a radio of one's own comes back with what it played", code == 200 and resp.get_json()["played"] == ["v:vid00000002"])
code, _ = call("music_radio_state", "/api/music/radio?id=genre:Nothing")
check("... an unknown one: 404", code == 404)
who["user"] = GUEST
code, _ = call("music_radio_state", "/api/music/radio?id=" + SONG_RADIO["id"])
check("... another account's: 404", code == 404)
code, _ = call("music_played", "/api/music/played", "POST", {"ref": "v:vid00000001", "src": "user"})
check("... and their plays are their own", code == 200 and len(events_of("gus")) == 1 and len(events_of("alice")) == 3,
      (len(events_of("gus")), len(events_of("alice"))))
who["user"] = GUEST
code, resp = call("music_history", "/api/music/history")
check("a history holds only its own account's plays", code == 200
      and len(resp.get_json()["events"]) == 1, resp.get_json())
who["user"] = None
code, _ = call("music_played", "/api/music/played", "POST", {"ref": "v:vid00000001", "src": "user"})
code2, _ = call("music_settings", "/api/music/settings", "PUT", {"autoplay": False})
code3, _ = call("music_radio_state", "/api/music/radio?id=" + SONG_RADIO["id"])
code4, _ = call("music_history", "/api/music/history")
check("signed out: 401 from every radio and history route",
      code == code2 == code3 == code4 == 401, (code, code2, code3, code4))
who["user"] = ADMIN
code, _ = call("music_settings", "/api/music/settings", "PUT", {"autoplay": "no"})
check("autoplay must be true or false", code == 400)
code, resp = call("music_settings", "/api/music/settings", "PUT", {"autoplay": False})
check("autoplay switched off", code == 200 and ns["music_listening_store"]().summary("alice")["autoplay"] is False)
code, resp = call("music_library_api", "/api/music/library")
data = resp.get_json()
arow = next(r for r in data["songs"] if r[0] == "Astronomia")
check("the library carries the places and the viewer's listening",
      arow[data["fields"].index("p3")] == [0.1, 0.2, 0.3] and data["me"]["autoplay"] is False
      and [x["id"] for x in data["me"]["radios"]] == [SONG_RADIO["id"]]
      and "played" not in data["me"]["radios"][0], data["me"])

# ---- sync ------------------------------------------------------------------------------------------
queued = []
ns["queue_download"] = lambda url: (queued.append(url), ("task1234", "youtube"))[1]
who["user"] = GUEST
code, _ = call("music_sync", "/api/music/sync", "POST", {"playlist": phonk["id"]})
check("a guest may not sync (it downloads)", code == 403)
who["user"] = ADMIN
code, _ = call("music_sync", "/api/music/sync", "POST", {"playlist": "fnothere"})
check("an unknown playlist: 404", code == 404)
code, _ = call("music_sync", "/api/music/sync", "POST", {"playlist": film["id"]})
check("a library folder has nothing to sync from: 404", code == 404)
anime = pls["Anime Epic"]
code, resp = call("music_sync", "/api/music/sync", "POST", {"playlist": anime["id"]})
check("a playlist from before links were kept: asks for its link", code == 400 and resp.get_json().get("need_url") is True)
code, resp = call("music_sync", "/api/music/sync", "POST", {"playlist": anime["id"], "url": "https://example.com/x?list=1"})
check("... and refuses a link that is not YouTube's", code == 400 and not queued)
code, resp = call("music_sync", "/api/music/sync", "POST",
                  {"playlist": anime["id"], "url": "https://www.youtube.com/playlist?list=PLanime&si=x"})
check("with its link: the download is queued, as a music.youtube.com link",
      code == 200 and queued == ["https://music.youtube.com/playlist?list=PLanime"] and resp.get_json()["task_id"] == "task1234",
      (code, queued, resp.get_json()))
saved = json.load(open(os.path.join(tmp, "music_sources.json")))
check("... and the link is kept for next time",
      saved.get("downloads:youtube_music/Crash Games UHD/Anime Epic", {}).get("url") == "https://music.youtube.com/playlist?list=PLanime",
      saved)
if "music_sources" not in ns:
    load("music_sources")
resolve(lambda: ns["music_sources"]()).put("downloads:youtube_music/Crash Games UHD/Phonk",
                                            url="https://music.youtube.com/playlist?list=PLphonk")
code, _ = call("music_sync", "/api/music/sync", "POST", {"playlist": phonk["id"]})
check("a playlist whose link is known syncs straight away",
      code == 200 and queued[-1] == "https://music.youtube.com/playlist?list=PLphonk", (code, queued))

with ns["task_lock"]:
    ns["active_tasks"]["task1234"] = {"id": "task1234", "status": "Running", "progress": "42%", "logs": ["secret"],
                                      "url": "u", "created_at": 1}
code, resp = call("music_task", "/api/music/task/task1234", kw={"task_id": "task1234"})
check("a sync's progress, without its logs", code == 200 and resp.get_json()["progress"] == "42%"
      and "logs" not in resp.get_json())
code, _ = call("music_task", "/api/music/task/nope", kw={"task_id": "nope"})
check("... and 404 for a task that is not there", code == 404)

# ---- the download records where a playlist came from ---------------------------------------------------------
load("record_music_source")
load("playlist_dir_for")
ns["sanitize_folder_name"] = lambda n: n
resolve(lambda: ns["record_music_source"]("https://music.youtube.com/playlist?list=PLnew",
                                         [{"id": "aaaaaaaaaaa", "folder_owner": "Owner", "album": "New PL"},
                                          {"id": "bbbbbbbbbbb", "folder_owner": "Owner", "album": "New PL"}]))
saved = json.load(open(os.path.join(tmp, "music_sources.json")))
got = saved.get("downloads:youtube_music/Owner/New PL", {})
check("a playlist download records its link and its order", got.get("url") == "https://music.youtube.com/playlist?list=PLnew"
      and got.get("tracks") == ["aaaaaaaaaaa", "bbbbbbbbbbb"] and got.get("list_id") == "PLnew", got)

# ---- covers ------------------------------------------------------------------------------------------------
MKA = "music/Film/Kordhell - Scopin [abcdefghijk].mka"
code, resp = call("music_cover", "/api/music/cover", query_string={"root": "lib1", "path": MKA, "s": "300"})
check("a Matroska song's cover, as WebP", code == 200 and resp.mimetype == "image/webp", (code, resp.get_data()[:80]))
if code == 200:
    resp.direct_passthrough = False
    from PIL import Image  # noqa: E402
    im = Image.open(io.BytesIO(resp.get_data()))
    check("... no larger than the original (64 px)", im.size[0] <= 64, im.size)
# A rendition is for a row, the bar and the lock screen. The now-playing screen
# draws the cover across a whole display and asks for the display's size, which
# is above the ladder: there the file's own picture is served, so that what is
# on the screen is the picture the upscale answer is about (user, 2026-09-23:
# "the info panel reports the cover doesn't need upscaling, but it is clearly
# not 4K" - it was judging a 3840 x 2160 cover behind a 600 px copy of it).
code, resp = call("music_cover", "/api/music/cover", query_string={"root": "lib1", "path": MKA, "s": "3840"})
resp.direct_passthrough = False
raw = resp.get_data() if code == 200 else b""
check("above the ladder: the cover inside the file", code == 200 and resp.mimetype == "image/jpeg",
      (code, resp.mimetype))
load("get_embedded_cover")
check("... byte for byte, not made again",
      raw == (resolve(lambda: ns["get_embedded_cover"](scopin)) or (None, b""))[1], len(raw))
check("... kept under its own type", any(f.endswith(".jpg")
                                         for _d, _s, fs in os.walk(os.path.join(tmp, "covers")) for f in fs))
code, resp = call("music_cover", "/api/music/cover", query_string={"root": "lib1", "path": MKA, "s": "3840"})
resp.direct_passthrough = False
check("... and served from that copy the next time", code == 200 and resp.get_data() == raw)
code, resp = call("music_cover", "/api/music/cover", query_string={"root": "lib1", "path": MKA, "s": "500"})
check("under the ladder's top: still a rendition", code == 200 and resp.mimetype == "image/webp", resp.mimetype)
load("music_forget_cover")
resolve(lambda: ns["music_forget_cover"](scopin, os.stat(scopin)))
kept = [f for _d, _s, fs in os.walk(os.path.join(tmp, "covers")) for f in fs if not f.endswith(".none")]
check("a repaired cover drops every copy of the old one, its own type included", not kept, kept)
code, _ = call("music_cover", "/api/music/cover", query_string={"root": "downloads", "path": "youtube_music/Vicetone/Astronomia.opus"})
check("a song without a cover: 404", code == 404)
nones = [f for d, _s, fs in os.walk(os.path.join(tmp, "covers")) for f in fs if f.endswith(".none")]
check("... remembered, so it is not looked for again", len(nones) == 1, nones)
who["user"] = GUEST
code, _ = call("music_cover", "/api/music/cover", query_string={"root": "lib1", "path": "music/Film/Kordhell - Scopin [abcdefghijk].mka"})
check("a root the viewer cannot see: 403", code == 403)
who["user"] = ADMIN

# ---- a link to songs ----------------------------------------------------------------------------------------
# A music link IS a pool share in the same store as every other link (user,
# 2026-09-23: "consider piggy-backing media-dl's system such that the shared
# can be managed there"). What marks it is one field, view "music", and that is
# what /share/<token> reads to send the music viewer instead of the file one.
ns["SHARES_FILE"] = os.path.join(tmp, "shares.json")
code, resp = call("music_share_create", "/api/music/share", method="POST",
                  body={"refs": ["v:vid00000002", "p:lib1:" + MKA], "kind": "playlist",
                        "label": "Night drive", "subtitle": "Vicetone"})
made = resp.get_json()
check("a link to songs is a pool share marked music", code == 200 and made.get("songs") == 2
      and made.get("kind") == "pool" and made.get("view") == "music", (code, made))
token = made.get("id") or ""
# The store holds one share per RUN of the route, and call() runs it again for
# every name it has to resolve on the way (resolve() retries the thunk), so the
# one to look at is the one the answer named.
def share_of(tok):
    for s in json.load(open(os.path.join(tmp, "shares.json"), encoding="utf-8")):
        if s["id"] == tok:
            return s
    return {}
mine = share_of(token)
check("... in media-dl's own store, in the order it was shared",
      [i["path"] for i in mine.get("items", [])] == ["youtube_music/Vicetone/Nevada.opus", MKA]
      and mine.get("music") == {"kind": "playlist", "subtitle": "Vicetone"}, mine)
load("share_category")
load("share_detail")
check("... and the Shares page reads it as music, not as a pool",
      resolve(lambda: ns["share_category"](mine)) == "music"
      and resolve(lambda: ns["share_detail"](mine)) == "2 songs from playlist",
      (resolve(lambda: ns["share_category"](mine)), resolve(lambda: ns["share_detail"](mine))))
check("... while an ordinary pool still reads as one",
      resolve(lambda: ns["share_category"]({"kind": "pool", "items": []})) == "pool"
      and resolve(lambda: ns["share_detail"]({"kind": "pool", "items": [1]})) == "1 item")
check("... and every other link still reads as a file link",
      resolve(lambda: ns["share_summary"]({"id": "x", "kind": "folder", "path": "p"}))["view"] == "files")
code, _ = call("music_share_create", "/api/music/share", method="POST", body={"refs": ["v:nothinghere"]})
check("songs that are not there: 404", code == 404)
code, _ = call("music_share_create", "/api/music/share", method="POST", body={"refs": []})
check("no songs at all: 400", code == 400)
code, _ = call("music_share_create", "/api/music/share", method="POST",
               body={"refs": ["v:vid00000001"] * (ns["MUSIC_SHARE_MAX"] + 1)})
check("more songs than a link holds: 400", code == 400)

code, resp = call("public_share_music", f"/api/public/share/{token}/music", kw={"token": token})
got = resp.get_json()
check("the viewer is given the songs, named as the page names them", code == 200
      and [s["title"] for s in got.get("songs", [])] == ["Nevada", "Scopin"]
      and got.get("title") == "Night drive" and got.get("kind") == "playlist"
      and got.get("subtitle") == "Vicetone", got)
check("... the file behind the token, and a cover only where there is one",
      all(s["url"].startswith(f"/api/public/share/{token}/file?") for s in got["songs"])
      and got["songs"][0]["cover_url"] is None and got["songs"][1]["cover_url"] is not None, got["songs"])
code, _ = call("public_share_music", "/api/public/share/nosuch/music", kw={"token": "nosuch"})
check("a token that is not there: 404", code == 404)

code, resp = call("public_share_music_cover", f"/api/public/share/{token}/music/cover",
                  query_string={"root": "lib1", "path": MKA}, kw={"token": token})
check("a cover the link holds", code == 200 and resp.mimetype.startswith("image/"), (code, resp.mimetype))
code, _ = call("public_share_music_cover", f"/api/public/share/{token}/music/cover",
               query_string={"root": "downloads", "path": "youtube_music/Alan Walker/Alone.opus"},
               kw={"token": token})
check("a song the link does NOT hold: 403", code == 403)

code, resp = call("public_share_page", f"/share/{token}", kw={"token": token})
check("a music link opens the music viewer", code == 200 and b"music_share.html" in resp.get_data(),
      (code, resp.get_data()[:60]))

# The preview a chat app draws for the link (user, 2026-09-23: "A music share
# should expose a preview to discord"): the viewer's own header in one line.
import re  # noqa: E402
load("share_preview_meta")


def preview_of(share, tok):
    with app.test_request_context(f"/share/{tok}", base_url="https://music.example.org"):
        html = resolve(lambda: ns["share_preview_meta"](share, tok))
    return dict(re.findall(r'<meta (?:property|name)="([^"]+)" content="([^"]*)">', html))


tags = preview_of(mine, token)
check("a music link's preview: Music, the playlist, its owner and its songs",
      tags.get("og:site_name") == "Music" and tags.get("og:type") == "music.playlist"
      and tags.get("og:title") == "Night drive" and tags.get("og:description") == "Playlist · Vicetone · 2 songs"
      and tags.get("twitter:description") == tags.get("og:description"), tags)
check("... the link's cover as its picture, on the host it was opened on",
      tags.get("og:image") == f"https://music.example.org/api/public/share/{token}/cover"
      and tags.get("og:url") == f"https://music.example.org/share/{token}", tags)
check("... and no second theme-color next to the viewer's own", "theme-color" not in tags, tags)
code, resp = call("music_share_create", "/api/music/share", method="POST",
                  body={"refs": ["v:vid00000002"], "kind": "song", "label": "Nevada"})
song_tok = resp.get_json().get("id") or ""
tags = preview_of(share_of(song_tok), song_tok)
check("a song's preview: its artist (the catalogue's name, no subtitle given) and its length",
      tags.get("og:type") == "music.song"
      and re.fullmatch(r"Song · Vicetone · 0:0\d", tags.get("og:description", "")), tags)
tags = preview_of({"id": "p", "kind": "pool", "items": [{"kind": "file", "path": "x"}]}, "p")
check("... while a file link's preview is as it was",
      tags.get("og:site_name") == "Media Library" and tags.get("og:type") == "website"
      and tags.get("og:description") == "1 item" and tags.get("theme-color") == "#38bdf8", tags)
all_shares = json.load(open(os.path.join(tmp, "shares.json"), encoding="utf-8"))
for s in all_shares:
    s.pop("view", None)
json.dump(all_shares, open(os.path.join(tmp, "shares.json"), "w", encoding="utf-8"))
code, resp = call("public_share_page", f"/share/{token}", kw={"token": token})
check("... and every other link still opens the file viewer",
      code == 200 and b"public_share.html" in resp.get_data(), resp.get_data()[:60])

# ---- one loudness for every song (vault KSF-058) -------------------------------------------------------------
# User, 2026-09-27: "All media files should play at equal volume on the music
# page", and "target the highest adjusted felt volume that does not cause
# cut-off". Real measurements (ffmpeg) of the temp library; Astronomia gets a
# music video 12 dB louder than its song, as a separate quiet .m4a copy was.
import music_loudness as mlo  # noqa: E402
check("the server imports music_loudness from its own directory", ns.get("music_loudness") is mlo)
ns["MUSIC_LOUDNESS_FILE"] = os.path.join(tmp, "music_loudness.json")
load("MUSIC_LOUDNESS_WORKERS")
rebuilds = []
ns["music_catalog"] = lambda force=False: (rebuilds.append(force), (state["catalog"], state["building"]))[1]
VID = "music_videos/Vicetone/Astronomia [vid00000001].mkv"
vid_full = os.path.join(down, VID)
os.makedirs(os.path.dirname(vid_full))
subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
                "-f", "lavfi", "-i", "color=c=blue:s=64x64:d=1", "-map", "0:a", "-map", "1:v",
                "-af", "volume=12dB", "-c:a", "libopus", "-c:v", "mjpeg", "-frames:v", "1", vid_full], check=True)
loud_cat = dict(cat, songs=[dict(s) for s in cat["songs"]])
next(s for s in loud_cat["songs"] if s["title"] == "Astronomia").update(
    video=["downloads", VID], videos=[["downloads", VID], ["downloads", "music_videos/gone.mkv"], ["lib9", "x.mkv"]])
lroots = {"downloads": {"id": "downloads", "path": down, "name": "Downloads"},
          "lib1": {"id": "lib1", "path": lib, "name": "Lib"}}
lnames = walk(ROOTS)
load("_music_loudness_plan")
first = resolve(lambda: ns["_music_loudness_plan"](loud_cat, lnames, lroots))
check("nothing measured yet: no target, and every song and song video waiting - a song's links are one file",
      first["target"] is None and first["gains"] == {} and first["measured"] == 0 and first["missing"] == 6, first)
meas = resolve(lambda: ns["music_loudness_measurer"]())
deadline = time.time() + 120
while time.time() < deadline and (meas.status()["queued"] or meas.status()["measuring"] or meas.running):
    time.sleep(0.1)
check("... the measurer did them in the background and asked for a rebuild", meas.status()["measured"] == 6
      and True in rebuilds, (meas.status(), rebuilds))
plan2 = resolve(lambda: ns["_music_loudness_plan"](loud_cat, lnames, lroots))
lgains = plan2["gains"]
g_song = lgains.get("downloads", {}).get("youtube_music/Vicetone/Astronomia.opus")
g_vid = lgains.get("downloads", {}).get(VID)
check("measured: a target, every file measured, none waiting",
      isinstance(plan2["target"], float) and plan2["measured"] == 6 and plan2["missing"] == 0, plan2)
check("a song and its video 12 dB louder play at one loudness: the video is given 12 dB less",
      g_song is not None and g_vid is not None and abs((g_song - g_vid) - 12) <= 0.4, (g_song, g_vid))
lcache = resolve(lambda: ns["music_loudness_cache"]())
stats = {(r, p): st for r, p, _f, st in lnames}
stats[("downloads", VID)] = os.stat(vid_full)
over = [(k, p) for k, p in ((k, lcache.get(stats[(r, k)])[1] + g) for r, d in lgains.items() for k, g in d.items())
        if p > 0]
check("... and no file is turned up past a true peak of 0 dB", not over, over)
check("... kept on disk, one entry per file", len(json.load(open(ns["MUSIC_LOUDNESS_FILE"]))) == 6)

lcat = dict(loud_cat, loudness=plan2)
view = ml.for_viewer(lcat, {"downloads", "lib1"})
fl = view["fields"]
arow = next(r for r in view["songs"] if r[0] == "Astronomia")
nrow = next(r for r in view["songs"] if r[0] == "Nevada")
check("each row carries its song's gain and its video's", arow[fl.index("gain")] == g_song
      and arow[fl.index("video_gain")] == g_vid and nrow[fl.index("video_gain")] is None,
      (arow[fl.index("gain")], arow[fl.index("video_gain")], nrow[fl.index("video_gain")]))
check("... and the page is told the target and what an unmeasured file gets",
      view["loudness"] == {"target": plan2["target"], "unknown": plan2["unknown"]}, view["loudness"])
old = ml.for_viewer(cat, {"downloads"})
check("a catalogue with no loudness in it: no gains, no target (the page changes nothing)",
      all(r[old["fields"].index("gain")] is None for r in old["songs"])
      and old["loudness"] == {"target": None, "unknown": 0.0}, old["loudness"])

state["catalog"] = lcat
code, resp = call("music_share_create", "/api/music/share", method="POST",
                  body={"refs": ["v:vid00000001", "p:lib1:" + MKA], "kind": "songs", "label": "Loud"})
ltok = resp.get_json().get("id") or ""
code, resp = call("public_share_music", f"/api/public/share/{ltok}/music", kw={"token": ltok})
got = resp.get_json()
check("a shared song plays at the page's loudness: its gain, and the link's target",
      code == 200 and [s.get("gain") for s in got.get("songs", [])] == [g_song, lgains["lib1"][MKA]]
      and got.get("loudness") == view["loudness"], got)
state["catalog"] = cat

# The catalogue build puts the plan in, and asks for one more build when the
# measurer reports while a build runs (that build has not seen the numbers).
ns["get_all_roots"] = lambda: list(lroots.values())
ns["media_dimensions_map"] = lambda: {}
load("_build_music_catalog")
lindex = [(r, p, False, None) for r, p, _f, _st in lnames] + [("downloads", VID, False, None),
                                                               ("downloads", "youtube_music", True, None)]
built = resolve(lambda: ns["_build_music_catalog"](lindex))
check("a built catalogue carries the loudness plan, the video it found for a song included",
      built.get("loudness", {}).get("measured") == 6 and built["loudness"]["target"] == plan2["target"]
      and built["loudness"]["gains"]["downloads"].get(VID) == g_vid, built.get("loudness"))
load("_music_build_thread")
fake_catalog = ns["music_catalog"]
with ns["_music_lock"]:
    ns["_music"].update(building=True, again=True)
rebuilds.clear()
merge_rounds = []
ns["maybe_merge_songs"] = lambda catalog: merge_rounds.append(catalog) or False
resolve(lambda: ns["_music_build_thread"](lindex))
check("... a rebuild asked for during a build runs once it ends",
      rebuilds == [True] and ns["_music"]["again"] is False and ns["_music"]["building"] is False,
      (rebuilds, ns["_music"]["again"]))
check("... and the new catalogue is handed to the song merge (song_merge.py)",
      len(merge_rounds) == 1 and merge_rounds[0] is ns["_music"]["catalog"], merge_rounds)
ns["get_media_index"] = lambda: lindex
load("music_catalog")
with ns["_music_lock"]:
    ns["_music"].update(building=True, again=False)
ns["music_catalog"](force=True)
check("... and music_catalog(force=True) during a build is what asks", ns["_music"]["again"] is True)
with ns["_music_lock"]:
    ns["_music"].update(building=False, again=False)
ns["music_catalog"] = fake_catalog
resolve(lambda: ns["_music_loudness_plan"](cat, lnames, lroots))
check("a file no song plays any more leaves the cache", len(lcache.data) == 5 and lcache.get(os.stat(vid_full)) is None,
      len(lcache.data))

# ---- the host -----------------------------------------------------------------------------------------------
load("serve_static")
ns["login_redirect_for"] = lambda url: ("login", 302)
code, resp = call("serve_static", "/", host="music.example.org", kw={"filename": "index.html"})
resp.direct_passthrough = False
check("music.example.org/ is the music page", code == 200 and resp.get_data() == b"music.html", resp.get_data()[:40])
code, resp = call("serve_static", "/", host="media.example.org", kw={"filename": "index.html"})
resp.direct_passthrough = False
check("every other host keeps its own front page", code == 200 and resp.get_data() == b"index.html")
check("guests may open the music page and its script", {"music.html", "js/music.js"} <= ns["GUEST_ALLOWED_FILES"])

print("ALL PASS" if ok else "SOME CHECKS FAILED")
sys.exit(0 if ok else 1)
