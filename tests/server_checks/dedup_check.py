"""One song, one file: server.py's music paths (a song under its artist, a hard
link in each playlist that holds it) and the index's one-row-per-file rule, out
of a server.py's AST over a temp library and AI store.

  venv/bin/python dedup_check.py <server.py>
"""
import ast
import atexit
import os
import shutil
import sys
import tempfile

BASE = os.environ.get("ATRIHORD_DIR") or os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SERVER = os.path.abspath(sys.argv[1])
STAGE = os.path.dirname(SERVER)
sys.path.insert(0, BASE)
sys.path.insert(0, STAGE)
tmp = tempfile.mkdtemp(prefix="dedup_check_")
atexit.register(shutil.rmtree, tmp, True)
os.environ["KSF_MEDIA_AI_DIR"] = os.path.join(tmp, "ai")
os.makedirs(os.environ["KSF_MEDIA_AI_DIR"])
import media_ai                        # noqa: E402
import media_indexer                   # noqa: E402
import youtube_music_extractor as music_store   # noqa: E402

assert os.path.dirname(os.path.abspath(media_indexer.__file__)) == STAGE
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


downloads = os.path.join(tmp, "downloads")
os.makedirs(downloads)
ns["DOWNLOADS_DIR"] = downloads
ns["BASE_DIR"] = tmp
check("the server uses the extractor's song store", ns.get("music_store") is music_store)
F("sanitize_folder_name")
F("clean_artist_name")
F("playlist_dir_for")
F("link_stored_songs")
F("_music_extraction_cmd")

# ---- where a song and its playlist name go --------------------------------
PLAYLIST_TRACK = {"id": "vid00000001", "title": "Astronomia", "artist": "Vicetone - Topic",
                  "folder_owner": "Crash Games UHD", "album": "Phonk", "thumbnail": ""}
SINGLE_TRACK = {"id": "vid00000002", "title": "Alone", "artist": "Alan Walker", "album": None}
target = os.path.join(tmp, "yt")
os.makedirs(target)
open(os.path.join(target, "Astronomia [vid00000001].webm"), "w").write("x")
open(os.path.join(target, "Alone [vid00000002].webm"), "w").write("x")
ns["find_downloaded_media_file"] = lambda d, vid, listing=None: next(
    (os.path.join(d, n) for n in os.listdir(d) if f"[{vid}]" in n), None)

cmd, out_dir = ns["_music_extraction_cmd"](PLAYLIST_TRACK, target)
check("a playlist track is stored under its artist, not under the playlist",
      out_dir == os.path.join(downloads, "youtube_music", "Vicetone"), out_dir)
check("... and the playlist folder is where it is linked",
      "--link" in cmd and cmd[cmd.index("--link") + 1]
      == os.path.join(downloads, "youtube_music", "Crash Games UHD", "Phonk"), cmd)
cmd2, out_dir2 = ns["_music_extraction_cmd"](SINGLE_TRACK, target)
check("a single video is stored under its artist and linked nowhere",
      out_dir2 == os.path.join(downloads, "youtube_music", "Alan Walker") and "--link" not in cmd2, [out_dir2, cmd2])
check("playlist_dir_for follows the playlist's owner", ns["playlist_dir_for"](PLAYLIST_TRACK)
      == os.path.join(downloads, "youtube_music", "Crash Games UHD", "Phonk"))
check("... and a single video has none", ns["playlist_dir_for"](SINGLE_TRACK) is None)

# ---- a song already stored is linked into a new playlist ------------------
song_dir = os.path.join(downloads, "youtube_music", "Vicetone")
os.makedirs(song_dir)
song = os.path.join(song_dir, "Astronomia.opus")
open(song, "wb").write(b"OggS" + b"\0" * 2048)
music_store.record_song(downloads, "vid00000001", song)
check("the songs index says which file a video id is",
      music_store.read_songs_index(downloads).get("vid00000001") == "youtube_music/Vicetone/Astronomia.opus",
      music_store.read_songs_index(downloads))

linked = ns["link_stored_songs"]([PLAYLIST_TRACK])
playlist_song = os.path.join(downloads, "youtube_music", "Crash Games UHD", "Phonk", "Astronomia.opus")
check("a song already stored is linked into the playlist that holds it, not downloaded again",
      linked == 1 and os.path.exists(playlist_song) and os.path.samefile(playlist_song, song),
      [linked, os.path.exists(playlist_song)])
check("... one file on disk, two names", os.stat(song).st_nlink == 2, os.stat(song).st_nlink)
check("... and it is the same bytes", open(playlist_song, "rb").read() == open(song, "rb").read())
again = ns["link_stored_songs"]([PLAYLIST_TRACK])
check("asking again changes nothing", again == 0 and os.stat(song).st_nlink == 2, [again, os.stat(song).st_nlink])

# A different song with the same title keeps its place; ours takes "[id]".
other_dir = os.path.join(downloads, "youtube_music", "Crash Games UHD", "Anime Epic")
os.makedirs(other_dir)
open(os.path.join(other_dir, "Astronomia.opus"), "wb").write(b"a different song")
other_track = dict(PLAYLIST_TRACK, album="Anime Epic")
n = ns["link_stored_songs"]([other_track])
taken = os.path.join(other_dir, "Astronomia [vid00000001].opus")
check("a different song with the same title is not touched; ours takes the [id] name",
      n == 1 and os.path.exists(taken) and os.path.samefile(taken, song)
      and open(os.path.join(other_dir, "Astronomia.opus"), "rb").read() == b"a different song", n)

missing = dict(PLAYLIST_TRACK, id="vid00000009", album="Gone")
music_store.record_song(downloads, "vid00000009", os.path.join(song_dir, "Vanished.opus"))
check("an index entry whose file is gone links nothing", ns["link_stored_songs"]([missing]) == 0)

# ---- the index: one row per FILE, not per name ---------------------------
conn = media_ai.connect(tmp)
root = ("downloads", downloads)
models = media_indexer.models_for("genre")
todo = media_indexer.iter_candidates([root], conn, models, 1 << 30)
paths = sorted(p for _r, p, _a, _s, _k in todo)
check("before anything is indexed, every name is a candidate",
      paths == ["youtube_music/Crash Games UHD/Anime Epic/Astronomia [vid00000001].opus",
                "youtube_music/Crash Games UHD/Anime Epic/Astronomia.opus",
                "youtube_music/Crash Games UHD/Phonk/Astronomia.opus",
                "youtube_music/Vicetone/Astronomia.opus"], paths)
media_ai.upsert(conn, "downloads", "youtube_music/Vicetone/Astronomia.opus", os.path.getsize(song), "audio",
                {**models, "tags": "", "ino": media_indexer.inode_key(song)})
conn.commit()
todo = media_indexer.iter_candidates([root], conn, models, 1 << 30)
paths = sorted(p for _r, p, _a, _s, _k in todo)
check("once the song is indexed, its other names are not candidates: one song, one row",
      paths == ["youtube_music/Crash Games UHD/Anime Epic/Astronomia.opus"], paths)
check("... the different song with the same title still is a candidate",
      paths == ["youtube_music/Crash Games UHD/Anime Epic/Astronomia.opus"])
check("indexed_elsewhere: another name for the same file",
      media_ai.indexed_elsewhere(conn, media_indexer.inode_key(song), "downloads",
                                 "youtube_music/Crash Games UHD/Phonk/Astronomia.opus") is True)
check("... but not for the row itself",
      media_ai.indexed_elsewhere(conn, media_indexer.inode_key(song), "downloads",
                                 "youtube_music/Vicetone/Astronomia.opus") is False)
check("... and never without an inode", media_ai.indexed_elsewhere(conn, None, "downloads", "x") is False)
row = conn.execute("SELECT ino FROM media WHERE path = 'youtube_music/Vicetone/Astronomia.opus'").fetchone()
check("the inode is stored with the row", row["ino"] == media_indexer.inode_key(song), row["ino"])

print("ALL PASS" if ok else "SOME CHECKS FAILED")
sys.exit(0 if ok else 1)
