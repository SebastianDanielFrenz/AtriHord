"""yt-dual/<link>: a YouTube download kept as music AND as video.

The link and its gate out of a STAGED server.py's AST (the prefix parsed, a
web page session let through, an API key refused, anything but YouTube
refused), the dual run's own download archive merged into the shared one,
"done" meaning a song AND a video - and the real extractor and hook, end to
end on a temp tree: the video kept under music_videos/<artist>/ instead of
deleted, and a song already stored given only its video.

  venv/bin/python dual_check.py <staged server.py>
youtube_music_extractor.py, youtube_music_hook.py, music_library.py and
recent_media_store.py are taken from beside it (run_all.sh copies the
production ones there when they are not part of the change).
"""
import ast
import atexit
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
tmp = tempfile.mkdtemp(prefix="dual_check_")
atexit.register(shutil.rmtree, tmp, True)
import music_library                          # noqa: E402
import youtube_music_extractor as extractor   # noqa: E402
from flask import Flask, request              # noqa: E402

assert os.path.dirname(os.path.abspath(extractor.__file__)) == STAGE, extractor.__file__
EXTRACTOR = os.path.join(STAGE, "youtube_music_extractor.py")
HOOK = os.path.join(STAGE, "youtube_music_hook.py")
if not os.path.exists(HOOK):
    HOOK = os.path.join(BASE, "youtube_music_hook.py")
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


downloads = os.path.join(tmp, "web_files", "downloads")
os.makedirs(downloads)
app = Flask("dual_check")
ns.update(app=app, require_admin=lambda f: f, DOWNLOADS_DIR=downloads,
          music_store=extractor, music_library=music_library)
queued = []
ns["queue_download"] = lambda url: (queued.append(url), ("t1", ns["classify_url"](url)))[1]
ns["get_sso_user"] = lambda: {"username": "me"} if request.headers.get("X-Test-SSO") else None
for name in ("split_dual_link", "trigger_download", "merge_download_archive", "dual_complete_ids",
             "classify_url", "DUAL_PREFIX", "DUAL_LINK_RE"):
    load(name)


def split(url):
    return resolve(lambda: ns["split_dual_link"](url))

# ---- the link --------------------------------------------------------------------------------
PL = "https://music.youtube.com/playlist?list=PLabc"
check("yt-dual/<link>: the link, and dual", split("yt-dual/" + PL) == (PL, True), split("yt-dual/" + PL))
check("yt-dual: and a missing https:// work too",
      split("yt-dual:www.youtube.com/watch?v=abcdefghijk") == ("https://www.youtube.com/watch?v=abcdefghijk", True))
check("... and a space after it", split("  yt-dual  " + PL) == (PL, True))
check("an ordinary link is left alone", split(PL) == (PL, False))
check("... and so is a site that merely has the word in it",
      split("https://yt-dual.example/x") == ("https://yt-dual.example/x", False))


def post(url, sso):
    with app.test_request_context("/api/download", method="POST", json={"url": url},
                                  headers={"X-Test-SSO": "1"} if sso else {"X-API-Key": "k"}):
        resp = resolve(lambda: ns["trigger_download"]())
        return (resp[1], resp[0].get_json()) if isinstance(resp, tuple) else (200, resp.get_json())


queued.clear()
code, body = post("yt-dual:music.youtube.com/playlist?list=PLabc", sso=True)
check("from the web page: queued, with the prefix kept in the task's link",
      code == 200 and queued == ["yt-dual/" + PL] and body.get("category") == "youtube", (code, body, queued))
queued.clear()
code, body = post("yt-dual/" + PL, sso=False)
check("with an API key: refused, as asked", code == 400 and not queued and "web page" in body.get("error", ""),
      (code, body))
code, body = post("yt-dual/https://www.pixiv.net/users/1", sso=True)
check("anything but YouTube: refused", code == 400 and not queued, (code, body))
code, body = post(PL, sso=False)
check("an ordinary link from the API still goes through", code == 200 and queued == [PL], (code, queued))

# ---- the run's own archive ---------------------------------------------------------------------
shared = os.path.join(tmp, "shared.txt")
run = os.path.join(tmp, "run.txt")
open(shared, "w").write("youtube aaaaaaaaaaa\nyoutube bbbbbbbbbbb\n")
open(run, "w").write("youtube bbbbbbbbbbb\nyoutube ccccccccccc\nyoutube ccccccccccc\n")
resolve(lambda: ns["merge_download_archive"](run, shared))
check("a dual run's archive is merged into the shared one, nothing twice, and removed",
      open(shared).read().split("\n")[:-1] == ["youtube aaaaaaaaaaa", "youtube bbbbbbbbbbb", "youtube ccccccccccc"]
      and not os.path.exists(run), open(shared).read())

# ---- "done" means both halves ------------------------------------------------------------------
music = os.path.join(downloads, "youtube_music")
os.makedirs(os.path.join(music, "A"))
for n in ("both0000001", "song0000002", "catv0000003"):
    open(os.path.join(music, "A", n + ".opus"), "w").write("x")
json.dump({"both0000001": "youtube_music/A/both0000001.opus", "song0000002": "youtube_music/A/song0000002.opus",
           "catv0000003": "youtube_music/A/catv0000003.opus", "gone0000004": "youtube_music/A/gone.opus"},
          open(os.path.join(music, ".songs.json"), "w"))
kept_dir = os.path.join(downloads, "music_videos", "A")
os.makedirs(kept_dir)
open(os.path.join(kept_dir, "Both [both0000001].webm"), "w").write("v")
open(os.path.join(kept_dir, "Only video [vidonly0005].webm"), "w").write("v")
ns["music_catalog"] = lambda: ({"songs": [{"video_id": "catv0000003", "video": ["lib", "x [catv0000003].mp4"]},
                                          {"video_id": "song0000002", "video": None}]}, False)
done = resolve(lambda: ns["dual_complete_ids"]())
check("done: a song AND a video - kept here, or one the catalogue matched",
      done == {"both0000001", "catv0000003"}, done)

# ---- the extractor keeps the video ---------------------------------------------------------------


def source_video(path, seconds=2):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=64x48:rate=5",
                    "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
                    "-t", str(seconds), "-c:v", "libvpx", "-c:a", "libopus", "-shortest", path], check=True)
    return path


dl = os.path.join(downloads, "youtube", "Owner", "Mix")
video = source_video(os.path.join(dl, "Night Ferry [newsong0001].webm"))
res = subprocess.run([sys.executable, EXTRACTOR, video, os.path.join(music, "Kessler Line"), "Night Ferry",
                      "Kessler Line - Topic", "newsong0001", "--keep-video"], capture_output=True, text=True)
kept = os.path.join(downloads, "music_videos", "Kessler Line", "Night Ferry [newsong0001].webm")
song = os.path.join(music, "Kessler Line", "Night Ferry.opus")
check("--keep-video: the song is made as always", os.path.isfile(song), res.stdout + res.stderr)
check("... and the video is kept under music_videos/<artist>/, not deleted",
      os.path.isfile(kept) and not os.path.exists(video), res.stdout + res.stderr)
check("... the song index has it",
      extractor.read_songs_index(downloads).get("newsong0001") == "youtube_music/Kessler Line/Night Ferry.opus")

before = os.stat(song)
video = source_video(os.path.join(dl, "Night Ferry [newsong0001].webm"))
os.remove(kept)
other_pl = os.path.join(music, "Owner", "Other mix")
res = subprocess.run([sys.executable, EXTRACTOR, video, os.path.join(music, "Kessler Line"), "Night Ferry",
                      "Kessler Line", "newsong0001", "--link", other_pl, "--keep-video"],
                     capture_output=True, text=True)
after = os.stat(song)
check("a song already stored: only its video is kept, the song not made again",
      os.path.isfile(kept) and not os.path.exists(video) and (before.st_ino, before.st_mtime_ns)
      == (after.st_ino, after.st_mtime_ns) and "keeping only the video" in res.stdout, res.stdout + res.stderr)
check("... and this playlist gets its name for the stored song at once",
      os.path.isfile(os.path.join(other_pl, "Night Ferry.opus"))
      and os.path.samefile(os.path.join(other_pl, "Night Ferry.opus"), song), res.stdout + res.stderr)

video = source_video(os.path.join(dl, "Low Tide [plainsong01].webm"))
res = subprocess.run([sys.executable, EXTRACTOR, video, os.path.join(music, "Kessler Line"), "Low Tide",
                      "Kessler Line", "plainsong01"], capture_output=True, text=True)
check("without the flag the video is deleted, as before",
      not os.path.exists(video) and not os.path.exists(os.path.join(downloads, "music_videos", "Kessler Line",
                                                                    "Low Tide [plainsong01].webm")),
      res.stdout + res.stderr)

# ---- the hook passes it on -------------------------------------------------------------------
video = source_video(os.path.join(dl, "Harbour Lights [hooksong01].webm"))
res = subprocess.run([sys.executable, HOOK, video, "hooksong01", "Harbour Lights", "Kessler Line - Topic", "NA",
                      "--keep-video"], capture_output=True, text=True)
check("the per-track hook: the video is kept too",
      os.path.isfile(os.path.join(downloads, "music_videos", "Kessler Line", "Harbour Lights [hooksong01].webm"))
      and not os.path.exists(video), res.stdout + res.stderr)
# The song store's layout (KSF-044), as the end-of-run path makes it: the song
# once under its artist, the playlist folder a hard link to it. The hook used to
# write the song INTO the playlist folder.
home = os.path.join(music, "Kessler Line", "Harbour Lights.opus")
in_pl = os.path.join(music, "Owner", "Mix", "Harbour Lights.opus")
check("... the song stored under its artist (\" - Topic\" dropped), the playlist a link to it",
      os.path.isfile(home) and os.path.isfile(in_pl) and os.path.samefile(home, in_pl),
      sorted(os.path.relpath(os.path.join(d, f), music) for d, _s, fs in os.walk(music) for f in fs))
check("... and the song index names the artist folder",
      extractor.read_songs_index(downloads).get("hooksong01") == "youtube_music/Kessler Line/Harbour Lights.opus",
      extractor.read_songs_index(downloads).get("hooksong01"))
video = source_video(os.path.join(dl, "Tidewater [hooksong02].webm"))
res = subprocess.run([sys.executable, HOOK, video, "hooksong02", "Tidewater", "Mara Lune", "NA"],
                     capture_output=True, text=True)
home = os.path.join(music, "Mara Lune", "Tidewater.opus")
in_pl = os.path.join(music, "Owner", "Mix", "Tidewater.opus")
check("an ordinary music playlist through the hook: the same layout, and the video deleted",
      os.path.isfile(home) and os.path.isfile(in_pl) and os.path.samefile(home, in_pl)
      and not os.path.exists(video), res.stdout + res.stderr)

print("ALL PASS" if ok else "SOME FAILED")
sys.exit(0 if ok else 1)
