"""One song, one result. A song is stored once under its artist and hard-linked
into every playlist that holds it, so the library holds one file under several
names - and search matches names. Out of a server.py's AST over a temp library:
the path index's link keys, one_row_per_file, the search route (words, "*",
favourites), the library file count and the recent feed.

  venv/bin/python songlinks_check.py <server.py> [old]

With "old" it shows the fault instead: the same song once per playlist.
"""
import ast
import os
import shutil
import sys
import tempfile

BASE = os.environ.get("ATRIHORD_DIR") or os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SERVER = sys.argv[1]
OLD = len(sys.argv) > 2 and sys.argv[2] == "old"
sys.path.insert(0, BASE)
tmp = tempfile.mkdtemp(prefix="songlinks_check_")
os.environ["KSF_MEDIA_AI_DIR"] = os.path.join(tmp, "ai")
os.makedirs(os.environ["KSF_MEDIA_AI_DIR"])
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


def _names(code):
    out = set(code.co_names)
    for const in code.co_consts:
        if hasattr(const, "co_names"):
            out |= _names(const)
    return out


def preload(name, seen=None):
    seen = set() if seen is None else seen
    if name in seen or name not in defs:
        return
    seen.add(name)
    code = compile(ast.Module(body=[defs[name]], type_ignores=[]), SERVER, "exec")
    for used in _names(code):
        if used not in ns:
            preload(used, seen)
    if name not in ns:
        load(name)


def F(name):
    preload(name)
    return ns[name]


# The library: one song under its artist with a name in two playlists, a
# single-playlist song, a DIFFERENT song that happens to share the title, and
# pictures - one of which is hard-linked too, to show that only the music store
# is treated this way.
lib = os.path.join(tmp, "lib")
music = os.path.join(lib, "youtube_music")


def song(rel, data=b"OggS" + b"\0" * 900):
    path = os.path.join(music, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    open(path, "wb").write(data)
    return path


def link(src, rel):
    path = os.path.join(music, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    os.link(src, path)
    return path


stored = song("Vicetone/Astronomia.opus")
phonk = link(stored, "Crash Games UHD/Phonk/Astronomia.opus")
anime = link(stored, "Crash Games UHD/Anime Epic/Astronomia.opus")
alone = song("Alan Walker/Alone.opus")
other = song("Other Artist/Astronomia.opus", b"OggS" + b"\1" * 900)   # a different song
pics = os.path.join(lib, "twitter", "@alice")
os.makedirs(pics)
Image.new("RGB", (40, 30), (1, 2, 3)).save(os.path.join(pics, "astronomia.jpg"))
os.link(os.path.join(pics, "astronomia.jpg"), os.path.join(pics, "astronomia copy.jpg"))

# Jellyfin's sidecars: they describe an item instead of being one.
amv = os.path.join(lib, "amv")
os.makedirs(amv)
open(os.path.join(amv, "clip.mp4"), "wb").write(b"\0" * 64)
Image.new("RGB", (40, 30), (7, 7, 7)).save(os.path.join(amv, "clip-poster.jpg"))
open(os.path.join(amv, "clip.nfo"), "w", encoding="utf-8").write("<movie />")
open(os.path.join(amv, "album.NFO"), "w", encoding="utf-8").write("<album />")
Image.new("RGB", (40, 30), (8, 8, 8)).save(os.path.join(amv, "lonely-poster.jpg"))

R = "downloads"
ns["BASE_DIR"] = tmp
ns["DOWNLOADS_DIR"] = lib
ns["DEFAULT_ROOT_ID"] = R
ns["THUMB_CACHE_DIR"] = os.path.join(tmp, "thumbs")
ADMIN = {"username": "alice", "role": "admin"}
FAVORITES = {}
ns["get_authenticated_user"] = lambda: ADMIN
ns["get_root"] = lambda root_id: {"id": R, "path": lib, "name": "Downloads"} if root_id == R else None
ns["get_all_roots"] = lambda: [{"id": R, "path": lib, "name": "Downloads"}]
ns["roots_for_user"] = lambda user: [{"id": R}]
ns["root_visible_to"] = lambda user, root_id: root_id == R
ns["attach_media_dimensions"] = lambda *a, **k: None
ns["attach_favorites"] = lambda entries, user: entries
ns["user_favorites"] = lambda username: FAVORITES
ns["download_category_of"] = lambda root_id, rel: rel.split("/")[0]
for name in ("app", "_media_index_cache", "_media_index_lock", "_media_index_build_lock",
             "MEDIA_INDEX_CACHE_SECONDS", "_folder_stats_lock", "_folder_stats_cache",
             "_everything_cache", "TRASH_DIR_NAME", "SEARCH_MODES"):
    if name not in ns and name in defs:
        load(name)
app = ns["app"]


def index_rows():
    ns["_media_index_cache"]["index"] = None
    ns["_everything_cache"]["index"] = None
    return resolve(lambda: F("get_media_index")())


def search(q, extra=""):
    ns["_media_index_cache"]["index"] = None
    ns["_everything_cache"]["index"] = None
    with app.test_request_context(f"/api/media/search?q={q}&limit=100{extra}"):
        rv = resolve(lambda: F("search_media")())
    data = rv.get_json()
    return data, [i["path"] for i in data["items"]]


try:
    rows = index_rows()
    files = [r for r in rows if not r[2]]
    names = sorted(r[1] for r in files)
    check("every name is still in the index - the files are all there",
          names.count("youtube_music/Vicetone/Astronomia.opus") == 1
          and names.count("youtube_music/Crash Games UHD/Phonk/Astronomia.opus") == 1
          and len(files) == 8, names)

    if OLD:
        _data, hits = search("astronomia")
        check(f"OLD: searching the title finds the one song {len([h for h in hits if h.endswith('.opus')])} times",
              len([h for h in hits if h.endswith(".opus")]) > 2, hits)
        print("ALL PASS" if ok else "SOME CHECKS FAILED")
        sys.exit(0 if ok else 1)

    # ---- the index says which names are one file ---------------------------
    keys = {r[1]: (r[3] if len(r) > 3 else None) for r in files}
    song_key = keys["youtube_music/Vicetone/Astronomia.opus"]
    check("a song with several names carries a link key", song_key is not None, keys)
    check("... the same key under every one of its names",
          keys["youtube_music/Crash Games UHD/Phonk/Astronomia.opus"] == song_key
          and keys["youtube_music/Crash Games UHD/Anime Epic/Astronomia.opus"] == song_key, keys)
    check("... and the key is the file itself (device, inode)",
          song_key == (os.stat(stored).st_dev, os.stat(stored).st_ino), song_key)
    check("a song with one name carries none", keys["youtube_music/Alan Walker/Alone.opus"] is None)
    check("a different song with the same title is not tied to it",
          keys["youtube_music/Other Artist/Astronomia.opus"] is None, keys)
    check("a hard-linked picture is left alone - only the music store makes links",
          keys["twitter/@alice/astronomia.jpg"] is None
          and keys["twitter/@alice/astronomia copy.jpg"] is None, keys)
    check("link_key: a lone file has no key", F("link_key")(os.path.dirname(alone), "Alone.opus") is None)
    check("link_key: a linked song has one",
          F("link_key")(os.path.dirname(stored), "Astronomia.opus") == song_key)
    check("link_key: a missing file is not an error",
          F("link_key")(os.path.dirname(stored), "gone.opus") is None)

    # ---- one_row_per_file --------------------------------------------------
    one = F("one_row_per_file")
    kept = one([(R, "a", False, ("d", 1)), (R, "b", False, ("d", 1)), (R, "c", False, ("d", 2))])
    check("one_row_per_file keeps the first name of a file and drops its others",
          [r[1] for r in kept] == ["a", "c"], kept)
    check("... rows with no key all stay",
          [r[1] for r in one([(R, "a", False, None), (R, "b", False, None)])] == ["a", "b"])
    check("... and a row from elsewhere (no key at all) is passed through",
          [r[1] for r in one([(R, "a", False), (R, "b", True)])] == ["a", "b"])

    # ---- the search route --------------------------------------------------
    data, hits = search("astronomia")
    songs = [h for h in hits if h.endswith(".opus")]
    check("the song is one result, not one per playlist", len(songs) == 2, hits)
    check("... and it is one of its real names",
          sum(1 for h in songs if h.endswith("Astronomia.opus")) == 2
          and any(h == "youtube_music/Other Artist/Astronomia.opus" for h in songs), songs)
    check("the different song with the same title is still found",
          "youtube_music/Other Artist/Astronomia.opus" in hits, hits)
    check("the total counts files, not names", data["total"] == len(hits), [data["total"], len(hits)])
    check("the two pictures are both still listed - they are two names the app did not make",
          len([h for h in hits if h.endswith(".jpg")]) == 2, hits)

    data, hits = search("phonk")
    check("searching a playlist finds the song under THAT name",
          "youtube_music/Crash Games UHD/Phonk/Astronomia.opus" in hits, hits)

    data, hits = search("*")
    check("listing everything lists the song once",
          len([h for h in hits if h.endswith("Astronomia.opus")]) == 2, hits)
    check("... and the total is files, not names", data["total"] == len(hits), data["total"])

    FAVORITES[R] = {"youtube_music/Crash Games UHD/Phonk/Astronomia.opus": 2.0,
                    "youtube_music/Vicetone/Astronomia.opus": 1.0}
    data, hits = search("", extra="&favorites=1")
    check("a song favourited under two names is one favourite", len(hits) == 1, hits)
    FAVORITES.clear()

    # ---- sidecars are not items -------------------------------------------
    check("a .nfo sidecar is not in the library", "amv/clip.nfo" not in names, names)
    check("... whatever its case", "amv/album.NFO" not in names, names)
    check("a poster next to its video is not either", "amv/clip-poster.jpg" not in names, names)
    check("a poster with no video is hidden too - it is still only a poster",
          "amv/lonely-poster.jpg" not in names, names)
    check("the video itself is listed", "amv/clip.mp4" in names, names)
    _data, hits = search("clip")
    check("searching finds the video once, with no sidecar beside it",
          hits == ["amv/clip.mp4"], hits)
    check("_is_hidden_or_partial says so directly",
          all(F("_is_hidden_or_partial")(n) for n in
              ("clip.nfo", "album.NFO", "clip-poster.jpg", "lonely-poster.JPG", ".hidden.jpg"))
          and not any(F("_is_hidden_or_partial")(n) for n in
                      ("clip.mp4", "poster.jpg", "a-poster-child.opus", "Astronomia.opus")))

    # ---- counts and the recent feed ---------------------------------------
    check("the library count counts files, not names", F("library_file_count")(rows) == 6,
          F("library_file_count")(rows))
    fresh = F("scan_new_media_since")(music, 0)
    paths = [p["path"] for p in fresh]
    check("a freshly downloaded song reaches the recent feed once",
          len([p for p in paths if p.endswith("Astronomia.opus")]) == 2, paths)
    check("... and every other new file is still there", len(paths) == 3, paths)
finally:
    shutil.rmtree(tmp, True)

print("ALL PASS" if ok else "SOME CHECKS FAILED")
sys.exit(0 if ok else 1)
