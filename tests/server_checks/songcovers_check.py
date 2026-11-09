"""song_covers.py: the job that remakes a cover from a better frame, and the
/api/music/covers* routes out of a STAGED server.py's AST.

Nothing here reaches YouTube: the timeline of a video is handed in as measured
frames, and the songs are real audio files made with ffmpeg in a temp store.
The one thing that must be true of the real thing is checked on real files -
that a new cover is written INTO the song, so the hard links every playlist is
made of keep pointing at it (vault KSF-044).

  venv/bin/python songcovers_check.py <staged server.py>
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

BASE = os.environ.get("ATRIHORD_DIR") or os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SERVER = os.path.abspath(sys.argv[1])
STAGE = os.path.dirname(SERVER)
sys.path.insert(0, BASE)
sys.path.insert(0, STAGE)
tmp = tempfile.mkdtemp(prefix="song_covers_check_")
atexit.register(shutil.rmtree, tmp, True)
import song_covers as sc  # noqa: E402

assert os.path.dirname(os.path.abspath(sc.__file__)) == STAGE, sc.__file__
ok = True


def check(label, cond, detail=""):
    global ok
    ok = ok and bool(cond)
    print(("PASS " if cond else "FAIL ") + label + (f"  ({detail})" if detail and not cond else ""), flush=True)


# ---- pictures ------------------------------------------------------------------------------
from PIL import Image  # noqa: E402


def jpeg(img):
    buf = io.BytesIO()
    img.convert("RGB").save(buf, "JPEG", quality=90)
    return buf.getvalue()


black = Image.new("RGB", (640, 360), (0, 0, 0))
lit_corner = Image.new("RGB", (640, 360), (15, 15, 15))
lit_corner.paste(Image.new("RGB", (170, 100), (255, 255, 255)), (0, 0))   # ~7% of the frame
photo = Image.new("RGB", (640, 360))
for x in range(640):                      # a gradient with a hard edge: contrast and mid brightness
    for y in range(0, 360, 40):
        photo.paste(Image.new("RGB", (1, 40), (x // 3, 40 + (y * 2 // 3), 200 - x // 4)), (x, y))
photo2 = photo.rotate(180)
dim = Image.new("RGB", (640, 360), (18, 20, 22))

m_black, m_corner, m_photo, m_dim = (sc.measure(jpeg(i)) for i in (black, lit_corner, photo, dim))
check("a black frame is black", sc.is_black(m_black), m_black)
check("... and so is one with a bright corner, which lifts the mean over the line",
      m_corner["mean"] >= sc.BLACK_MEAN and m_corner["dark"] > sc.BLACK_DARK_SHARE and sc.is_black(m_corner), m_corner)
check("a picture is not", not sc.is_black(m_photo), m_photo)
# The first song the job looked at on prod: a 1080x1080 artwork at mean 17 with
# real contrast. Dark is not the same as faded, and re-fetching it can only
# find the same picture again.
check("a dark ARTWORK is not a fade, however dim it is",
      not sc.is_black({"mean": 17.1, "std": 32.7, "dark": 0.801}))
check("... while a dark flat frame is", sc.is_black({"mean": 17.1, "std": 4.0, "dark": 0.8}))
check("a picture scores above a dim flat frame", sc.picture_score(m_photo) > sc.picture_score(m_dim),
      (sc.picture_score(m_photo), sc.picture_score(m_dim)))
check("measure reads the size of the picture", (m_photo["w"], m_photo["h"]) == (640, 360))


# ---- the timeline --------------------------------------------------------------------------
def frame(t, dh, mean=120, std=40, dark=0.0):
    return {"t": float(t), "dhash": f"{dh:016x}", "mean": mean, "std": std, "dark": dark, "w": 1280, "h": 720}


INTRO, ART = 0x0000000000000000, 0xf0f0f0f0f0f0f0f0
check("frames that look alike are one shot, a cut starts another",
      sc.shot_runs([frame(0, INTRO), frame(2, INTRO), frame(4, INTRO), frame(6, ART), frame(8, ART)], 2)
      == [(0, 2), (3, 4)])

# An intro (bright, high contrast, before the song's own picture) and then 160
# seconds of artwork that slowly pans - the shape of a re-upload channel.
intro_then_art = [frame(t, INTRO, mean=128, std=70) for t in range(0, 14, 2)]
for n, t in enumerate(range(14, 180, 2)):
    drift = ART ^ ((0b111 << (n % 20 * 3)) & ((1 << 64) - 1))      # 3 bits a step: the same shot
    intro_then_art.append(frame(t, drift, mean=120, std=65 if t == 100 else 40))
when, why = sc.choose_time(intro_then_art, 2)
check("the cover comes from the artwork, not the intro", when is not None and when >= 16, (when, why))
check("... from the best frame inside it", when == 100, (when, why))
check("... and says which shot it took", "longest shot" in why, why)

# The artwork opens on black: the fade must not be what is taken.
fade = [frame(t, INTRO, mean=128, std=70) for t in range(0, 12, 2)]
for n, t in enumerate(range(12, 160, 2)):
    dark = t < 40
    fade.append(frame(t, ART ^ (0b11 << (n % 30 * 2)), mean=2 if dark else 130,
                      std=1 if dark else 45, dark=1.0 if dark else 0.0))
when, why = sc.choose_time(fade, 2)
check("a fade to black inside the shot is skipped", when is not None and when >= 40, (when, why))

# A real music video: cut every couple of seconds, nothing that is a long shot.
cut = [frame(t, (t * 0x9e3779b97f4a7c15) & ((1 << 64) - 1),
             mean=128, std=70 if t == 8 else (60 if t == 100 else 30)) for t in range(0, 200, 2)]
when, why = sc.choose_time(cut, 2)
check("with no long shot the middle of the song is used", when == 100 and "no long shot" in why, (when, why))
check("... and never the first seconds, however good they look", when > 12, (when, why))
check("a video too short to say anything still answers", sc.choose_time([frame(0, ART)], 2)[0] is not None)

# The longest stretch of the video is black - all black frames look alike, so
# the darkness itself would win the "longest shot" if nothing stopped it.
BLACKED = [frame(t, 0x0, mean=1, std=0.5, dark=1.0) for t in range(0, 140, 2)]
BLACKED += [frame(t, ART ^ (0b11 << (t % 20)), mean=120, std=50) for t in range(140, 200, 2)]
when, why = sc.choose_time(BLACKED, 2)
check("the dark stretch does not win just for being the longest",
      when is not None and when >= 140 and "black" in why, (when, why))
check("a video that is black from end to end has no frame to offer",
      sc.choose_time([frame(t, 0x0, mean=1, std=0.5, dark=1.0) for t in range(0, 60, 2)], 2)
      == (None, "every frame of the video is black"))


# ---- what needs doing ----------------------------------------------------------------------
store = os.path.join(tmp, "downloads", "youtube_music")
CFG = dict(sc.DEFAULTS)
CFG.update(scope="channels", channels=["Nightblue Music", "MHM"])
job = sc.SongCovers(tmp, store, lambda: CFG, lambda: False, log=lambda text: None)

good, blackish = sc.measure(jpeg(photo)), sc.measure(jpeg(black))
check("a song with no cover at all is wanted", job.wanted("A/x.opus", "A", None, CFG)[0] == "missing")
check("a black cover is wanted whatever the scope",
      job.wanted("A/x.opus", "A", blackish, dict(CFG, scope="black"))[0] == "black")
check("a good cover from another channel is left alone", job.wanted("A/x.opus", "A", good, CFG) is None)
check("... but one from a channel with an intro is not",
      job.wanted("Nightblue Music/x.opus", "Nightblue Music", good, CFG)[0] == "channel")
check("... the channel is matched on how the folder starts, whatever its case",
      job.wanted("MHMX/x.opus", "MHMX", good, CFG)[0] == "channel"
      and job.wanted("mhm anime/x.opus", "mhm anime", good, CFG)[0] == "channel")
check("scope 'black' leaves an intro channel's good cover alone",
      job.wanted("MHM/x.opus", "MHM", good, dict(CFG, scope="black")) is None)
check("scope 'all' takes everything", job.wanted("A/x.opus", "A", good, dict(CFG, scope="all"))[0] == "all")


# ---- real songs ----------------------------------------------------------------------------
def audio(rel, seconds=1, tags=None):
    path = os.path.join(store, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    codec = {".opus": ["-c:a", "libopus"], ".mka": ["-c:a", "libopus"],
             ".m4a": ["-c:a", "aac"], ".mp3": ["-c:a", "libmp3lame"]}[os.path.splitext(path)[1]]
    cmd = ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}"] + codec
    for k, v in (tags or {}).items():
        cmd += ["-metadata", f"{k}={v}"]
    subprocess.run(cmd + [path], check=True)
    return path


def streams(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_type",
                          "-of", "csv=p=0", path], capture_output=True, text=True)
    return [line.strip() for line in (out.stdout or "").splitlines() if line.strip()]


nb = audio("Nightblue Music/NB - Runner [aaaaaaaaaaa].opus",
           tags={"title": "Runner", "comment": "https://www.youtube.com/watch?v=bbbbbbbbbbb"})
dark_song = audio("Some Artist/Faded.opus", tags={"title": "Faded"})
fine = audio("Some Artist/Fine.opus", tags={"title": "Fine"})
nothing = audio("Some Artist/Bare.opus", tags={"title": "Bare"})
mka = audio("Some Artist/Matroska [ccccccccccc].mka", tags={"title": "Matroska"})
for path, img in ((nb, photo), (dark_song, black), (fine, photo)):
    sc.write_cover(path, jpeg(img))

check("a cover is read back out of a song", sc.read_cover(fine) is not None and sc.measure(sc.read_cover(fine))["w"] == 640)
check("a song without one says so", sc.read_cover(nothing) is None)
check("ffmpeg sees the written cover as a picture stream", "video" in streams(fine), streams(fine))

# The hard links a playlist is made of must survive the rewrite.
link = os.path.join(store, "Some Artist", "Playlist", "Fine.opus")
os.makedirs(os.path.dirname(link), exist_ok=True)
os.link(fine, link)
before = os.stat(fine)
new_jpeg = jpeg(photo2)
check("a new cover is written in", sc.write_cover(fine, new_jpeg) is True)
after = os.stat(fine)
check("... into the same file, so every playlist holding it sees the new one",
      after.st_ino == before.st_ino and after.st_nlink == 2
      and sc.measure(sc.read_cover(link))["dhash"] == sc.measure(new_jpeg)["dhash"])
check("... and the song still plays", streams(fine).count("audio") == 1)
check("Matroska is refused rather than half-written", sc.write_cover(mka, new_jpeg) is False)
for ext, name in ((".mp3", "Some Artist/Mp3 song.mp3"), (".m4a", "Some Artist/M4a song.m4a")):
    other = audio(name)
    check(f"a cover goes into {ext} too", sc.write_cover(other, new_jpeg) is True
          and sc.measure(sc.read_cover(other))["dhash"] == sc.measure(new_jpeg)["dhash"])

# What the job itself writes: the picture changes, the date does not, and the
# server is told to drop the cover it had cached.
forgotten = []
dated = sc.SongCovers(tmp, store, lambda: CFG, lambda: False, log=lambda text: None,
                      forget_cover=lambda path, old: forgotten.append((path, old.st_ino, old.st_size)))
was = os.stat(nb)
check("the job writes a cover", dated.apply_cover(nb, jpeg(photo2)) is True)
now = os.stat(nb)
check("... and the song keeps the date it was downloaded on, so it does not look new",
      now.st_mtime_ns == was.st_mtime_ns, (was.st_mtime_ns, now.st_mtime_ns))
check("... with the new picture inside it all the same",
      sc.measure(sc.read_cover(nb))["dhash"] == sc.measure(jpeg(photo2))["dhash"])
check("... and the cached old cover is dropped",
      forgotten == [(nb, was.st_ino, was.st_size)], forgotten)
check("a container that cannot carry one is left alone", dated.apply_cover(mka, jpeg(photo2)) is False
      and len(forgotten) == 1)

check("the video id comes out of the tags", job.video_id(nb) == "bbbbbbbbbbb")
check("... or out of the file name when the tags have none", job.video_id(mka) == "ccccccccccc")
check("... and is None when there is nothing to go on", job.video_id(fine) is None)


# ---- the queue -----------------------------------------------------------------------------
out = job.scan()
songs = job.state["songs"]
check("every song in the store is read", out["seen"] == 7, out)
check("the black cover is queued", songs["Some Artist/Faded.opus"]["reason"] == "black")
check("the song with no cover is queued", songs["Some Artist/Bare.opus"]["reason"] == "missing")
check("the intro channel is queued", songs["Nightblue Music/NB - Runner [aaaaaaaaaaa].opus"]["reason"] == "channel")
check("a good cover elsewhere is left alone", songs["Some Artist/Fine.opus"]["state"] == "skipped")
check("Matroska is put aside with the reason said out loud",
      songs["Some Artist/Matroska [ccccccccccc].mka"]["state"] == "skipped"
      and "Matroska" in songs["Some Artist/Matroska [ccccccccccc].mka"]["why"])
check("a song hard-linked into a playlist is one song, under its artist's name",
      "Some Artist/Playlist/Fine.opus" not in songs and "Some Artist/Fine.opus" in songs and out["queued"] == 3, out)

again = sc.SongCovers(tmp, store, lambda: CFG, lambda: False, log=lambda text: None)
check("the queue is on disk, so a restart carries on where it stopped",
      {k: v.get("state") for k, v in again.state["songs"].items()} == {k: v.get("state") for k, v in songs.items()})
check("the state file is the one the job documents", os.path.isfile(os.path.join(tmp, sc.STATE_NAME)))

# A state file written when "needs fixing" meant something else must not be
# acted on: the songs are read again first.
state_file = os.path.join(tmp, sc.STATE_NAME)
saved = json.load(open(state_file))
saved["rules"] = sc.RULES_VERSION - 1
saved["scanned_at"] = 9.9e9
json.dump(saved, open(state_file, "w"))
older = sc.SongCovers(tmp, store, lambda: CFG, lambda: False, log=lambda text: None)
check("a state file from older rules asks for another scan",
      older.state["scanned_at"] == 0 and older.state["rules"] == sc.RULES_VERSION,
      (older.state["scanned_at"], older.state.get("rules")))
saved["rules"] = sc.RULES_VERSION
json.dump(saved, open(state_file, "w"))
same = sc.SongCovers(tmp, store, lambda: CFG, lambda: False, log=lambda text: None)
check("... and one from these rules is taken as it is", same.state["scanned_at"] == 9.9e9)

check("the next song is the most pressing one", job.next_item()["reason"] == "missing", job.next_item())
check("a song asked for by hand goes to the front", job.enqueue("Some Artist/Fine.opus")
      and job.next_item()["path"] == "Some Artist/Fine.opus")
check("... a song that is not there cannot be asked for", job.enqueue("Some Artist/nope.opus") is False)
st = job.status()
check("the status says what the page shows", st["states"]["queued"] == 4 and st["enabled"] is True
      and st["queued_by_reason"]["asked"] == 1 and isinstance(st["hours_left"], float), st)

# A scan again leaves the work already done alone, and does not lose the queue.
out2 = job.scan()
check("a second scan reads the same songs and does not drop what was asked for by hand",
      out2["seen"] == 7 and job.state["songs"]["Some Artist/Fine.opus"]["state"] == "queued"
      and job.state["songs"]["Some Artist/Fine.opus"]["reason"] == "asked", out2)


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


check("the server imports song_covers from its own directory", ns.get("song_covers") is sc)
for name in ("app", "DEFAULT_CONFIG", "DEFAULT_ROOT_ID", "task_lock", "active_tasks", "is_admin", "require_admin",
             "song_covers_settings", "downloads_busy"):
    load(name)
check("the defaults carry the job, running over the black covers and the intro channels",
      ns["DEFAULT_CONFIG"]["song_covers"]["enabled"] is True
      and ns["DEFAULT_CONFIG"]["song_covers"]["scope"] == "channels")
check("a config of its own is merged over them, not swapped for them",
      "song_covers" in open(SERVER, encoding="utf-8").read().split("def load_config")[1].split("def ")[0])

srv_tmp = os.path.join(tmp, "server")
os.makedirs(srv_tmp, exist_ok=True)
ns["BASE_DIR"] = srv_tmp
ns["DOWNLOADS_DIR"] = os.path.join(tmp, "downloads")
ns["CONFIG"] = {"song_covers": {"enabled": True, "songs_per_hour": 4}}
check("the settings are the defaults with the user's own on top",
      resolve(lambda: ns["song_covers_settings"]()) == dict(sc.DEFAULTS, enabled=True, songs_per_hour=4))

ns["active_tasks"].clear()
check("with nothing downloading the job may run", resolve(lambda: ns["downloads_busy"]()) is False)
ns["active_tasks"]["t1"] = {"status": "Done"}
ns["active_tasks"]["t2"] = {"status": "Queued"}
check("a queued download holds it back", ns["downloads_busy"]() is True)
ns["active_tasks"]["t2"] = {"status": "Running"}
check("a running one too", ns["downloads_busy"]() is True)
ns["active_tasks"].clear()

# The cached cover of a song whose picture was replaced: gone, both under the
# key it had before the write and under the one it has now.
import hashlib as _hashlib  # noqa: E402

load("MUSIC_COVER_SIZES")
load("music_forget_cover")
ns["MUSIC_COVER_DIR"] = os.path.join(srv_tmp, "covers")
cached = []
old_stat = os.stat(nb)
sc.write_cover(nb, jpeg(photo))                      # a different picture: another size
for st in (old_stat, os.stat(nb)):
    for size in ns["MUSIC_COVER_SIZES"]:
        key = _hashlib.sha1(f"{st.st_dev}:{st.st_ino}:{st.st_size}:{st.st_mtime_ns}:{size}".encode()).hexdigest()
        folder = os.path.join(ns["MUSIC_COVER_DIR"], key[:2])
        os.makedirs(folder, exist_ok=True)
        name = os.path.join(folder, key + ".webp")
        open(name, "wb").close()
        cached.append(name)
stray = os.path.join(ns["MUSIC_COVER_DIR"], "zz", "another.webp")
os.makedirs(os.path.dirname(stray), exist_ok=True)
open(stray, "wb").close()
resolve(lambda: ns["music_forget_cover"](nb, old_stat))
check("the pictures cached from the old cover are dropped, whichever key they were under",
      not any(os.path.exists(p) for p in cached) and os.path.exists(stray),
      [p for p in cached if os.path.exists(p)])
check("... and a song whose cached files are already gone is no trouble",
      ns["music_forget_cover"](nb, old_stat) is None)

ADMIN = {"username": "alice", "role": "admin"}
GUEST = {"username": "gus", "role": "guest"}
who = {"user": ADMIN}
ns["get_authenticated_user"] = lambda: who["user"]
ns["resolve_media_path"] = lambda rid, rel: (os.path.join(ns["DOWNLOADS_DIR"], rel)
                                             if rid == "downloads" and ".." not in rel else None)
app = ns["app"]


def call(fn, path, method="GET", body=None, **ctx):
    if body is not None:
        ctx["json"] = body
    if fn not in ns:
        load(fn)
    with app.test_request_context(path, method=method, base_url="https://music.example.org", **ctx):
        rv = resolve(lambda: ns[fn]())
    return (rv[1], rv[0]) if isinstance(rv, tuple) else (rv.status_code, rv)


code, resp = call("music_covers_status", "/api/music/covers")
body = resp.get_json()
check("an admin is told what the job is doing", code == 200 and "states" in body and "hours_left" in body, (code, body))
who["user"] = GUEST
check("a guest is not", call("music_covers_status", "/api/music/covers")[0] == 403)
who["user"] = None
check("signed out: 401", call("music_covers_status", "/api/music/covers")[0] == 401)
who["user"] = GUEST
check("a guest cannot start a scan", call("music_covers_scan", "/api/music/covers/scan", "POST", {})[0] == 403)
check("... nor ask for a song", call("music_covers_song", "/api/music/covers/song", "POST",
                                     {"root": "downloads", "path": "youtube_music/Some Artist/Fine.opus"})[0] == 403)
who["user"] = ADMIN

code, resp = call("music_covers_song", "/api/music/covers/song", "POST",
                  {"root": "downloads", "path": "youtube_music/Some Artist/Fine.opus"})
body = resp.get_json()
check("a song in the store is queued at the front", code == 200 and body["queued"] is True
      and body["path"] == "Some Artist/Fine.opus", (code, body))
shutil.copy(fine, os.path.join(tmp, "downloads", "elsewhere.opus"))
code, resp = call("music_covers_song", "/api/music/covers/song", "POST",
                  {"root": "downloads", "path": "elsewhere.opus"})
check("a file outside the song store is refused, with the reason", code == 400 and "store" in resp.get_json()["error"],
      (code, resp.get_json()))
code, _ = call("music_covers_song", "/api/music/covers/song", "POST", {"root": "downloads", "path": "youtube_music/nope.opus"})
check("a song that is not there: 404", code == 404)
code, _ = call("music_covers_song", "/api/music/covers/song", "POST",
               {"root": "downloads", "path": "../../etc/passwd"})
check("a path climbing out of the root: 404", code == 404)

code, resp = call("music_covers_scan", "/api/music/covers/scan", "POST", {})
body = resp.get_json()
check("an admin can read the songs again by hand", code == 200 and body["started"] is True, (code, body))

print(("\nALL PASS" if ok else "\nFAILURES ABOVE"), flush=True)
sys.exit(0 if ok else 1)
