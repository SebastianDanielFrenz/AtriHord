"""song_merge.py (one song, one title) over a temp library of real audio made
with ffmpeg, and its wiring out of a STAGED server.py's AST. Nothing of the real
library is read.

  venv/bin/python song_merge_check.py <staged server.py>
"""
import ast
import atexit
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time

BASE = os.environ.get("ATRIHORD_DIR") or os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SERVER = os.path.abspath(sys.argv[1])
STAGE = os.path.dirname(SERVER)
sys.path.insert(0, BASE)
sys.path.insert(0, STAGE)
tmp = tempfile.mkdtemp(prefix="song_merge_check_")
atexit.register(shutil.rmtree, tmp, True)
import music_library as ml               # noqa: E402
import song_merge as sm                  # noqa: E402
import youtube_music_extractor as ext    # noqa: E402

assert os.path.dirname(os.path.abspath(sm.__file__)) == STAGE, sm.__file__
ok = True


def check(label, cond, detail=""):
    global ok
    ok = ok and bool(cond)
    print(("PASS " if cond else "FAIL ") + label + (f"  ({detail})" if detail and not cond else ""), flush=True)


# ---- titles --------------------------------------------------------------------
check("folded: case, accents and punctuation do not count", sm.fold("Café - Déjà Vu!") == "cafedejavu")
check("folded: another script stays", sm.fold("万古 (原神)") == "万古原神")
check("a visualiser's tail and the video id go",
      sm.bare_title("Idonzzz - Real Speed | Music Visualization🖤🎶 [abcdefghijk]") == "Idonzzz - Real Speed")
check("upload noise goes, a real bracket stays",
      sm.bare_title("Sakura (Official Music Video)") == "Sakura"
      and sm.bare_title("Sakura (Slowed + Reverb)") == "Sakura (Slowed + Reverb)")
check("the artist in front, at every dash", sm.prefixed("A - B - C") == [("A", "B - C"), ("A - B", "C")])
check("a dash inside a word is no separator", sm.prefixed("Spider-Man Theme") == [])
check("a credit's artists", sm.artist_parts("Geoxor & Canonblade") == {"geoxorcanonblade", "geoxor", "canonblade"}
      and sm.artist_parts("Alan feat. Bert") == {"alanfeatbert", "alan", "bert"}, sm.artist_parts("Alan feat. Bert"))

S = lambda title, artist: {"title": title, "artist": artist}
songs = [S("Geoxor & Canonblade - Sakura", "Geoxor"), S("Sakura", "Geoxor & Canonblade"),
         S("Sakura", "Somebody Else"), S("MVDNES - Like That | Music Visualization", "MHM"),
         S("Like That", "MVDNES"), S("Alone", "Alan Walker"), S("Alone", "Alan Walker"),
         S("Sakura - Ending", "Nobody"), S("Ending", "Nobody Two")]
pairs = sm.candidates(songs)
got = sorted((p["kind"],) + sm.pair_songs(p) for p in pairs)
check("candidates: the artist in front (twice), one title twice - and nothing else",
      got == [("artist", 0, 1), ("artist", 3, 4), ("same", 5, 6)], got)
check("... a clean title by an unrelated artist is not one (Sakura by Somebody Else)",
      not any(2 in sm.pair_songs(p) for p in pairs))
check("better: bitrate first, more than 3% apart", sm.better({"kbps": 160, "cover": 0}, {"kbps": 128, "cover": 9}) == 1)
check("better: within 3%, the bigger cover", sm.better({"kbps": 128, "cover": 0}, {"kbps": 130, "cover": 9}) == -1)
check("better: a tie is a tie", sm.better({"kbps": 128, "cover": 4}, {"kbps": 128, "cover": 4}) == 0)
q = {0: {"kbps": 190, "cover": 0}, 1: {"kbps": 64, "cover": 0}}
check("choose: the better file stays with the clean title and artist",
      sm.choose({"kind": "artist", "prefixed": 0, "clean": 1}, songs, q) == (0, 1, "Sakura", "Geoxor & Canonblade"))
q = {0: {"kbps": 128, "cover": 0}, 1: {"kbps": 128, "cover": 0}}
check("choose: a tie keeps the clean file", sm.choose({"kind": "artist", "prefixed": 0, "clean": 1}, songs, q)[:2]
      == (1, 0))
two = [dict(S("Sakura", "Geoxor"), path="youtube_music/Geoxor/Sakura.opus"),
       dict(S("Sakura", "Geoxor & Canonblade"), path="music/x.mka")]
check("choose, one title: the fuller credit wins even on the lesser file's side",
      sm.choose({"kind": "same", "a": 0, "b": 1}, two, {0: {"kbps": 160, "cover": 0}, 1: {"kbps": 96, "cover": 0}})
      == (0, 1, "Sakura", "Geoxor & Canonblade"))
tie = {0: {"kbps": 133.7, "cover": 9}, 1: {"kbps": 133.7, "cover": 9}}
pair = [dict(S("Geoxor & Canonblade - Sakura", "Geoxor"), path="youtube_music/Geoxor/Geoxor & Canonblade - Sakura.opus"),
        dict(S("Sakura", "Geoxor & Canonblade"),
             path="animated_music_videos/audio/Geoxor/Geoxor & Canonblade - Sakura.mka")]
check("choose, one stream twice (the user's Sakura): the song store's tagged file stays, retitled - "
      "not the library's untagged .mka",
      sm.choose({"kind": "artist", "prefixed": 0, "clean": 1}, pair, tie) == (0, 1, "Sakura", "Geoxor & Canonblade"))
pair = [dict(S("Nightcore - Mad Love (Lyrics)", "Chino"), path="youtube_music/Chino/Nightcore - Mad Love (Lyrics).opus"),
        dict(S("Mad Love (Lyrics)", "Nightcore"), path="youtube_music/Chino/Nightcore - Mad Love (Lyrics).mka")]
check("choose, both in the store: the one that is not Matroska",
      sm.choose({"kind": "artist", "prefixed": 0, "clean": 1}, pair, tie)[:3] == (0, 1, "Mad Love (Lyrics)"))
pair = [dict(S("Badman ｜ Music Visualization\U0001F5A4_waifu2x_3840x2160_mp4_W2xEX_VFI_mp4", "Sonny Denja, Rayna"),
             path="animated_music_videos/audio/MHM_4K120/x.mka"),
        dict(S("Sonny Denja, Rayna - Badman | Music Visualization\U0001F5A4", "MHM"), path="youtube_music/MHM/x.opus")]
check("choose: the better file wins over the store; the title loses the visualiser's tail and the upscaler's junk",
      sm.choose({"kind": "artist", "prefixed": 1, "clean": 0}, pair,
                {0: {"kbps": 322, "cover": 8294400}, 1: {"kbps": 125, "cover": 2073600}})
      == (0, 1, "Badman", "Sonny Denja, Rayna"))
pair = [dict(S("Got a Date (From Honkai Star Rail)", "tnbee"), path="youtube_music/tnbee/Got a Date.mka"),
        dict(S('Got a Date? (From "Honkai: Star Rail")', "tnbee"), path="youtube_music/tnbee/Got a Date.opus")]
check("choose, one title: the spelling that kept its punctuation (a name lost it to sanitising)",
      sm.choose({"kind": "same", "a": 0, "b": 1}, pair, {0: {"kbps": 261, "cover": 0}, 1: {"kbps": 132, "cover": 0}})
      == (0, 1, 'Got a Date? (From "Honkai: Star Rail")', "tnbee"))
check("a bar that is the title's own stays",
      sm.display_title("EMINEM ｜ TILL I COLLAPSE ｜ Medieval Bardcore Version")
      == "EMINEM ｜ TILL I COLLAPSE ｜ Medieval Bardcore Version")


# ---- a library of real audio ---------------------------------------------------
def audio(path, f1, f2, codec, rate, tags=None, delay=0, seconds=20):
    """A "song": a tone whose loudness moves in a pattern set by f1/f2 - what
    the envelope measure listens to. `delay` s of silence in front (an
    upload's intro)."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    expr = f"sin(2*PI*440*t)*(0.15+0.4*abs(sin(2*PI*{f1}*t))+0.35*gt(sin(2*PI*{f2}*t),0.4))"
    cmd = ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"aevalsrc='{expr}':s=48000:d={seconds}"]
    if delay:
        cmd += ["-af", f"adelay={int(delay * 1000)}:all=1"]
    cmd += {"opus": ["-c:a", "libopus", "-vbr", "off", "-b:a", rate],
            "aac": ["-c:a", "aac", "-b:a", rate]}[codec]
    for k, v in (tags or {}).items():
        cmd += ["-metadata", f"{k}={v}"]
    subprocess.run(cmd + [path], check=True)
    return path


down = os.path.join(tmp, "downloads")
lib = os.path.join(tmp, "lib")
store = os.path.join(down, "youtube_music")
yt = lambda vid: {"comment": f"https://www.youtube.com/watch?v={vid}"}
P = {
    "sakura_up": "youtube_music/Geoxor/Geoxor & Canonblade - Sakura.opus",
    "sakura": "youtube_music/Geoxor & Canonblade/Sakura.m4a",
    "sakura_pl": "youtube_music/Me/Mix/Sakura.m4a",
    "remix": "youtube_music/Geoxor/Geoxor - Sakura.opus",
    "mhm": "youtube_music/MHM/MVDNES - Like That  Music Visualization.opus",
    "like": "youtube_music/MVDNES/Like That.m4a",
    "alone": "youtube_music/Alan Walker/Alone.opus",
}
audio(os.path.join(down, P["sakura_up"]), 0.7, 0.23, "opus", "192k",
      dict(yt("vidAAAAAAAA"), title="Geoxor & Canonblade - Sakura", artist="Geoxor"))
audio(os.path.join(down, P["sakura"]), 0.7, 0.23, "aac", "64k",
      dict(yt("vidBBBBBBBB"), title="Sakura", artist="Geoxor & Canonblade"))
os.makedirs(os.path.dirname(os.path.join(down, P["sakura_pl"])))
os.link(os.path.join(down, P["sakura"]), os.path.join(down, P["sakura_pl"]))
audio(os.path.join(down, P["remix"]), 1.9, 0.61, "opus", "192k",
      dict(yt("vidRRRRRRRR"), title="Geoxor - Sakura", artist="Geoxor"))
# A visualiser's upload: three seconds of intro, a lower bitrate than the track.
audio(os.path.join(down, P["mhm"]), 0.45, 0.31, "opus", "48k",
      dict(yt("vidMMMMMMMM"), title="MVDNES - Like That | Music Visualization", artist="MHM"), delay=3)
audio(os.path.join(down, P["like"]), 0.45, 0.31, "aac", "192k",
      dict(yt("vidLLLLLLLL"), title="Like That", artist="MVDNES"))
audio(os.path.join(down, P["alone"]), 1.1, 0.17, "opus", "160k",
      dict(yt("vidDDDDDDDD"), title="Alone", artist="Alan Walker"))
JELLY = "music/Alan Walker - Alone [vidCCCCCCCC].mka"
os.makedirs(os.path.join(lib, "music"))
subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", audio(os.path.join(tmp, "x.opus"), 1.1, 0.17, "opus", "96k"),
                "-c", "copy", "-map_metadata", "-1", os.path.join(lib, JELLY)], check=True)
# One title twice, the better file an untagged .mka in the other root: it stays, and gets tags.
P["twilight"] = "youtube_music/Geoxor/Twilight.opus"
audio(os.path.join(down, P["twilight"]), 0.9, 0.4, "opus", "96k",
      dict(yt("vidTTTTTTTT"), title="Twilight", artist="Geoxor"))
TWI = "music/Geoxor - Twilight.mka"
subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", audio(os.path.join(tmp, "t.opus"), 0.9, 0.4, "opus", "160k"),
                "-c", "copy", "-map_metadata", "-1", os.path.join(lib, TWI)], check=True)
json.dump({"vidAAAAAAAA": P["sakura_up"], "vidBBBBBBBB": P["sakura"], "vidMMMMMMMM": P["mhm"],
           "vidLLLLLLLL": P["like"], "vidDDDDDDDD": P["alone"], "vidRRRRRRRR": P["remix"]},
          open(os.path.join(store, ".songs.json"), "w"))
ROOTS = {"downloads": down, "lib1": lib}


def walk():
    out = []
    for rid, base in ROOTS.items():
        for d, dirs, files in os.walk(base):
            dirs[:] = [x for x in dirs if not x.startswith(".")]
            for f in files:
                if ml.is_audio(f) and not f.startswith("."):
                    full = os.path.join(d, f)
                    out.append((rid, os.path.relpath(full, base).replace(os.sep, "/"), full, os.stat(full)))
    return out


def catalog(renamed=None):
    return ml.build(walk(), ml.TagCache(os.path.join(tmp, "tags.json")), "downloads",
                    songs_index=ext.read_songs_index(down), renamed=renamed)


full = lambda key: os.path.join(down, P[key])
v = sm.same(full("sakura_up"), full("sakura"), 20, 20)
check("the same recording, two encodes: the same", v and v["same"] and v["loudness"] > 0.9, v)
v = sm.same(full("mhm"), full("like"), 23, 20)
check("... three seconds of intro in front: still the same", v and v["same"], v)
v = sm.same(full("remix"), full("sakura"), 20, 20)
check("a different song of that name: not the same", v and not v["same"] and v["loudness"] < 0.5, v)
check("lengths more than six seconds apart are a different edit, not even decoded",
      sm.same("/nonexistent-a", "/nonexistent-b", 200, 180) == {"same": False, "loudness": 0.0, "attacks": 0.0,
                                                               "apart": 20.0})
check("a file that does not decode: no verdict", sm.same(full("sakura"), os.path.join(tmp, "none.opus")) is None)
qa, qb = sm.quality(full("sakura_up")), sm.quality(full("sakura"))
check("quality: the opus's bitrate from its packets (no stream bitrate in Ogg), the aac's from its stream",
      170 < qa["kbps"] < 215 and 50 < qb["kbps"] < 80, (qa, qb))

cat = catalog()
songs = cat["songs"]
check("the library: nine songs, the playlist a name of one", len(songs) == 9, [s["title"] for s in songs])

# ---- the report: nothing changes ---------------------------------------------
before = {k: os.stat(full(k)).st_ino for k in P}
rep = sm.run(songs, ROOTS, "downloads", down, os.path.join(tmp, "report_store.json"), apply=False)
# Sakura/upload, Sakura/remix ("Geoxor - Sakura"), Like That (artist in front); Alone, Twilight (one title twice).
check("report: five candidate pairs, all listened to", rep["candidates"] == 5 and rep["listened"] == 5,
      {k: rep[k] for k in ("candidates", "listened", "same", "different")})
got = sorted((p["drop_was"][1], p["title"], p["artist"]) for p in rep["pairs"])
check("... four are one recording, the remix is not",
      got == [("Alone", "Alone", "Alan Walker"), ("MVDNES - Like That | Music Visualization", "Like That", "MVDNES"),
              ("Sakura", "Sakura", "Geoxor & Canonblade"), ("Twilight", "Twilight", "Geoxor")]
      and rep["different"] == 1, got)
check("... and nothing on disk changed", all(os.stat(full(k)).st_ino == before[k] for k in P)
      and not os.path.exists(os.path.join(down, ".trash")))


# ---- the server's round, out of the staged server.py ----------------------------
tree = ast.parse(open(SERVER, encoding="utf-8").read())
defs = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}
calls = {"fav": [], "trashed": [], "forget": [], "rebuild": 0, "invalidate": 0}
favorites = {"alice": {"downloads": {P["sakura"]: 1.0, P["alone"]: 2.0}}}
MERGE_FILE = os.path.join(tmp, "song_merges.json")
ns = {
    "os": os, "time": time, "threading": threading, "song_merge": sm, "music_store": ext, "print": print,
    "CONFIG": {"song_merge": {"enabled": False}}, "busy": [False],
    "_song_merge_lock": threading.Lock(),
    "_song_merge": {"running": False, "started": 0.0, "finished": 0.0, "last": None, "error": None},
    "SONG_MERGE_FILE": MERGE_FILE, "DEFAULT_ROOT_ID": "downloads", "DOWNLOADS_DIR": down,
    "get_all_roots": lambda: [{"id": k, "path": p} for k, p in ROOTS.items()],
    "shares_lock": threading.Lock(), "_favorites_lock": threading.Lock(),
    "_read_shares_unlocked": lambda: [{"kind": "file", "path": P["mhm"]}, {"kind": "pool", "items": [
        {"kind": "file", "path": "elsewhere.jpg", "root": "lib1"}]}],
    "share_root_of": lambda obj: (obj or {}).get("root") or "downloads",
    "_favorites_data": lambda: favorites,
    "set_favorite": lambda u, r, p, on: calls["fav"].append((u, r, p, on)),
    "ai_index_trashed": lambda items, trashed=True: calls["trashed"].extend(items),
    "music_forget_cover": lambda full_path, st: calls["forget"].append(full_path),
    "resolve_media_path": lambda r, p: os.path.join(ROOTS[r], p),
    "invalidate_media_caches": lambda: calls.__setitem__("invalidate", calls["invalidate"] + 1),
    "music_catalog": lambda force=False: calls.__setitem__("rebuild", calls["rebuild"] + 1),
}
ns["downloads_busy"] = lambda: ns["busy"][0]
for name in ("song_merge_enabled", "maybe_merge_songs", "_shared_names", "_song_merged", "_song_merge_round"):
    exec(compile(ast.Module(body=[defs[name]], type_ignores=[]), SERVER, "exec"), ns)


def wait_round():
    for _ in range(600):
        if not ns["_song_merge"]["running"]:
            return
        time.sleep(0.1)


check("server: the setting off, no round", ns["maybe_merge_songs"](cat) is False)
ns["CONFIG"]["song_merge"]["enabled"] = True
ns["busy"][0] = True
check("server: a download running, no round", ns["maybe_merge_songs"](cat) is False)
ns["busy"][0] = False
check("server: the shared names are every share's files",
      ns["_shared_names"]() == {("downloads", P["mhm"]), ("lib1", "elsewhere.jpg")})
kept_sakura = os.stat(full("sakura_up"))
check("server: idle and on, a round starts", ns["maybe_merge_songs"](cat) is True)
wait_round()
last = ns["_song_merge"]["last"] or {}
check("... and merges the four", last.get("merged") == 4 and last.get("failed") == 0 and not ns["_song_merge"]["error"],
      (last.get("merged"), last.get("failed"), ns["_song_merge"]["error"], [p.get("result") for p in last.get("pairs", [])]))

# Sakura: the upload's opus (192k) stays, as "Sakura" by Geoxor & Canonblade.
new_home = "youtube_music/Geoxor & Canonblade/Sakura.opus"
info = ml.probe(full("sakura_up"))
check("Sakura: the better file (the upload, 192k) takes the clean title and artist",
      info.get("title") == "Sakura" and info.get("artist") == "Geoxor & Canonblade" and info.get("video_id") == "vidAAAAAAAA",
      info)
st = os.stat(full("sakura_up"))
check("... written in place, its date kept", st.st_ino == kept_sakura.st_ino and st.st_mtime_ns == kept_sakura.st_mtime_ns)
check("... the track's names are names of it now, with its extension",
      os.path.samefile(os.path.join(down, new_home), full("sakura_up"))
      and os.path.samefile(os.path.join(down, "youtube_music/Me/Mix/Sakura.opus"), full("sakura_up"))
      and not os.path.lexists(full("sakura")) and not os.path.lexists(full("sakura_pl")))
batch = last.get("batch")
grave = os.path.join(down, ".trash", batch or "-", P["sakura"])
check("... the track itself is in the trash, whole", os.path.isfile(grave) and os.path.getsize(grave) > 10000
      and ml.probe(grave).get("title") == "Sakura", grave)
man = json.load(open(os.path.join(down, ".trash", batch, "_manifest.json"), encoding="utf-8"))
check("... in a batch the trash page can restore and empty",
      len(batch) == 22 and P["sakura"] in man["items"] and P["mhm"] in man["items"]
      and man["mode"] == "release" and man["deleted_at"] and len(man["notes"]) == len(man["items"]), man)
index = ext.read_songs_index(down)
check("... both video ids name the song under its clean name now",
      index["vidAAAAAAAA"] == new_home and index["vidBBBBBBBB"] == new_home, index)
check("... the favourite follows the renamed name",
      ("alice", "downloads", new_home, True) in calls["fav"] and ("alice", "downloads", P["sakura"], False) in calls["fav"],
      calls["fav"])
check("... the AI index hears of the trashed name, the retitled file's covers are forgotten",
      ("downloads", P["sakura"]) in calls["trashed"] and full("sakura_up") in calls["forget"], calls)
check("the remix stays as it was", ml.probe(full("remix")).get("title") == "Geoxor - Sakura"
      and not os.path.samefile(full("remix"), full("sakura_up")))

# Like That: the track (192k) stays; the visualiser's name is shared, so it keeps its exact name.
check("Like That: the better file is the clean one - kept, not retitled",
      ml.probe(full("like")).get("title") == "Like That" and full("like") not in calls["forget"])
check("... the visualiser's name, which a share link uses, keeps its name (and .opus) as a name of the track",
      os.path.samefile(full("mhm"), full("like")))
check("... its video id points at the track", index["vidMMMMMMMM"] == P["like"], index.get("vidMMMMMMMM"))

# Alone: the store's 160k opus stays; the Jellyfin root's 96k .mka name becomes a name of it.
jelly_new = os.path.join(lib, JELLY[:-4] + ".opus")
check("Alone (one title twice, two roots): the store's file stays, the library's name takes its extension",
      os.path.samefile(jelly_new, full("alone")) and not os.path.lexists(os.path.join(lib, JELLY)))
check("... the library's own trash holds the .mka",
      os.path.isfile(os.path.join(lib, ".trash", batch, JELLY)))
check("... the untouched favourite stays", not any(p == P["alone"] for _u, _r, p, _on in calls["fav"]))
check("... not retitled: the kept file's own tags already say it", full("alone") not in calls["forget"])

# Twilight: the library's 160k .mka stays; it had no tags, so it gets them (in place, through ffmpeg).
twi = os.path.join(lib, TWI)
info = ml.probe(twi)
check("Twilight: the better file is the other root's untagged .mka - kept, and given the title and artist",
      info.get("title") == "Twilight" and info.get("artist") == "Geoxor" and twi in calls["forget"], info)
check("... the store's name is a name of it now, as .mka",
      os.path.samefile(os.path.join(down, "youtube_music/Geoxor/Twilight.mka"), twi)
      and not os.path.lexists(full("twilight")))
check("the round asked for a new catalogue", calls["rebuild"] == 1 and calls["invalidate"] == 1, calls)

store_data = json.load(open(MERGE_FILE, encoding="utf-8"))
check("the renamed names are remembered",
      store_data["renamed"].get("downloads:" + P["sakura"]) == "downloads:" + new_home
      and store_data["renamed"].get("lib1:" + JELLY) == "lib1:" + JELLY[:-4] + ".opus", store_data["renamed"])

# ---- the catalogue after: one song each, old refs still find it -------------------
cat2 = catalog(renamed=sm.load_renamed(MERGE_FILE))
songs2 = cat2["songs"]
by = {(s["artist"], s["title"]): s for s in songs2}
check("five songs left: Sakura, the remix, Like That, Alone, Twilight", len(songs2) == 5, sorted(by))
tw = by.get(("Geoxor", "Twilight")) or {}
check("Twilight plays from the store's name, titled by its new tags", tw.get("path") == "youtube_music/Geoxor/Twilight.mka",
      tw.get("path"))
sak = by.get(("Geoxor & Canonblade", "Sakura")) or {}
check("Sakura plays from its clean name", sak.get("path") == new_home, sak.get("path"))
check("... and answers to the track's video id and old name",
      "v:vidBBBBBBBB" in sak.get("aliases", []) and f"p:downloads:{P['sakura']}" in sak.get("aliases", []),
      sak.get("aliases"))
view = ml.for_viewer(cat2, {"downloads", "lib1"},
                     {"p1": {"name": "Mine", "songs": ["v:vidBBBBBBBB", "v:vidMMMMMMMM", f"p:lib1:{JELLY}"],
                             "updated": 1}})
rows = view["songs"]
title_of = lambda i: rows[i][0]
check("a user playlist of the dropped ids and a renamed name still holds its three songs",
      [title_of(i) for i in view["mine"][0]["songs"]] == ["Sakura", "Like That", "Alone"], view["mine"])
check("the page gets the aliases to resolve history and radios",
      title_of(view["aliases"]["v:vidBBBBBBBB"]) == "Sakura" and title_of(view["aliases"]["v:vidMMMMMMMM"]) == "Like That",
      view["aliases"])
check("a song's own ref is never an alias", not any(r == "v:vidAAAAAAAA" for r in view["aliases"]))

# ---- the next round: nothing left, nothing listened to again ----------------------
calls["rebuild"] = 0
ns["maybe_merge_songs"](cat2)
wait_round()
check("the next round merges nothing (it may listen once to the remix beside the retitled file)",
      (ns["_song_merge"]["last"] or {}).get("merged") == 0 and calls["rebuild"] == 0)
ns["maybe_merge_songs"](catalog(renamed=sm.load_renamed(MERGE_FILE)))
wait_round()
last = ns["_song_merge"]["last"] or {}
check("... and the one after: the remix pair is remembered, nothing listened to",
      last.get("merged") == 0 and last.get("listened") == 0 and last.get("different") == 1 and calls["rebuild"] == 0,
      {k: last.get(k) for k in ("candidates", "listened", "same", "different", "merged")})
os.utime(full("remix"), ns=(time.time_ns(), time.time_ns() + 5_000_000_000))
ns["maybe_merge_songs"](catalog(renamed=sm.load_renamed(MERGE_FILE)))
wait_round()
check("... a file changed since: listened to again", (ns["_song_merge"]["last"] or {}).get("listened") == 1)

# ---- retitling a Matroska file, in place -----------------------------------------
mka = os.path.join(tmp, "t", "song.mka")
os.makedirs(os.path.dirname(mka))
subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", full("remix"), "-c", "copy", "-metadata", "title=Old", mka],
               check=True)
os.link(mka, mka + ".link.mka")
ino = os.stat(mka).st_ino
check("retag: a .mka goes through ffmpeg, in place (its hard link sees it)",
      sm.retag(mka, "New Title", "New Artist") and os.stat(mka).st_ino == ino
      and ml.probe(mka + ".link.mka").get("title") == "New Title"
      and ml.probe(mka).get("artist") == "New Artist" and not [f for f in os.listdir(os.path.dirname(mka))
                                                              if f.startswith(".")], ml.probe(mka))
aac = os.path.join(tmp, "t", "raw.aac")
subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "sine=d=2", "-c:a", "aac", aac], check=True)
check("retag: a raw .aac takes no title", sm.retag(aac, "x", "y") is False)

# ---- a merge that cannot happen leaves everything as it was ------------------------
a = audio(os.path.join(store, "Q", "Q - Song.opus"), 0.3, 0.9, "opus", "192k",
          dict(yt("vidQQQQQQQQ"), title="Q - Song", artist="Q"))
b = audio(os.path.join(store, "Q", "Song.m4a"), 0.3, 0.9, "aac", "64k", dict(title="Song", artist="Q"))
qs = {"title": "Q - Song", "artist": "Q", "root": "downloads", "path": "youtube_music/Q/Q - Song.opus",
      "names": [("downloads", "youtube_music/Q/Q - Song.opus")]}
qd = {"title": "Song", "artist": "Q", "root": "downloads", "path": "youtube_music/Q/Song.m4a",
      "names": [("downloads", "youtube_music/Q/Song.m4a")]}
res = sm.merge(qs, qd, "Song", "Q", ROOTS, down, sm.new_batch(), expect={"keep": "0:0:0:0", "drop": "x"})
check("a file changed since it was listened to: nothing done",
      res["error"] == "changed since it was listened to" and os.path.isfile(b)
      and ml.probe(a).get("title") == "Q - Song", res)
check("the lock files are the song store's per-artist ones",
      sm._Locks(down, [a, os.path.join(lib, JELLY)]).paths == [os.path.join(store, "Q.lock")])

# ---- a round that dies on one pair keeps going, and keeps its verdicts -------------
# (as songs "Qq - Song" / "Song" by Qq: a one-letter artist is never a candidate)
qs2 = dict(qs, title="Qq - Song", artist="Qq", video_id="vidQQQQQQQQ", added=1.0, duration=20)
qd2 = dict(qd, artist="Qq", added=2.0, duration=20)
real_merge = sm.merge
sm.merge = lambda *a, **k: (_ for _ in ()).throw(PermissionError(1, "Operation not permitted"))
crash_store = os.path.join(tmp, "crash_store.json")
try:
    rep = sm.run([qs2, qd2], ROOTS, "downloads", down, crash_store)
except Exception as exc:
    rep = {"error": repr(exc)}
finally:
    sm.merge = real_merge
check("a merge that raises: the round goes on and counts it failed",
      rep.get("failed") == 1 and rep.get("merged") == 0 and rep["pairs"][0]["result"]["error"].startswith("failed:"), rep)
check("... and its verdict is saved", len(json.load(open(crash_store, encoding="utf-8"))["pairs"]) == 1)

# ---- the kept file is another user's (Jellyfin's) ----------------------------------
# Only a file's owner may set its times, so an edit in place loses its date
# (2026-09-28, the first live round). Ownership is faked through geteuid.
fk = audio(os.path.join(store, "F", "F - Tune.opus"), 0.8, 0.27, "opus", "192k",
           dict(yt("vidFFFFFFFF"), title="F - Tune", artist="F"))
fl = os.path.join(lib, "music", "F - Tune.opus")
os.link(fk, fl)
fd = audio(os.path.join(store, "F", "Tune.m4a"), 0.8, 0.27, "aac", "64k", dict(title="Tune", artist="F"))
os.utime(fk, ns=(1_600_000_000_000_000_000, 1_600_000_000_123_456_789))
os.chmod(fk, 0o646)
f_keep = {"title": "F - Tune", "artist": "F", "root": "downloads", "path": "youtube_music/F/F - Tune.opus",
          "names": [("downloads", "youtube_music/F/F - Tune.opus"), ("lib1", "music/F - Tune.opus")]}
f_drop = {"title": "Tune", "artist": "F", "root": "downloads", "path": "youtube_music/F/Tune.m4a",
          "names": [("downloads", "youtube_music/F/Tune.m4a")]}
f_before = os.stat(fk)
real_euid = os.geteuid
os.geteuid = lambda: real_euid() + 1
try:
    res = sm.merge(dict(f_keep, names=f_keep["names"][:1]), f_drop, "Tune", "F", ROOTS, down, sm.new_batch())
    check("another user's file with a name outside the library: nothing done",
          res["error"] == "it has names outside the library" and os.stat(fk).st_ino == f_before.st_ino
          and ml.probe(fk).get("title") == "F - Tune" and os.path.isfile(fd), res)
    os.chmod(fk, 0o640)
    res = sm.merge(f_keep, f_drop, "Tune", "F", ROOTS, down, sm.new_batch())
    check("... one its owner could not read as a copy of ours: nothing done",
          res["error"] == "a copy of ours could not be read by its owner" and os.stat(fk).st_ino == f_before.st_ino
          and os.path.isfile(fd), res)
    os.chmod(fk, 0o646)
    f_before = os.stat(fk)
    f_batch = sm.new_batch()
    res = sm.merge(f_keep, f_drop, "Tune", "F", ROOTS, down, f_batch)
finally:
    os.geteuid = real_euid
st = os.stat(fk)
check("another user's file, retitled: replaced by a copy of ours under every name",
      res["error"] is None and res["retagged"] and st.st_ino != f_before.st_ino and os.path.samefile(fk, fl)
      and st.st_nlink == 3, (res, st.st_nlink))
check("... with its date and mode", st.st_mtime_ns == f_before.st_mtime_ns and (st.st_mode & 0o777) == 0o646,
      (st.st_mtime_ns, oct(st.st_mode)))
check("... and the clean title", ml.probe(fl).get("title") == "Tune" and ml.probe(fl).get("artist") == "F",
      ml.probe(fl))
check("... the dropped file in the trash, its name a name of the copy, no temp files left",
      os.path.isfile(os.path.join(down, ".trash", f_batch, "youtube_music/F/Tune.m4a"))
      and os.path.samefile(os.path.join(store, "F", "Tune.opus"), fk)
      and not [f for d in (os.path.dirname(fk), os.path.dirname(fl)) for f in os.listdir(d) if f.startswith(".")])

# ---- a failed pair is tried again when a link count changes ------------------------
# The live case: the kept file's other name was in a trash batch, which the
# user empties later - no stat key sees that.
hk = audio(os.path.join(store, "Hh", "Hh - Wave.opus"), 0.55, 0.19, "opus", "192k", dict(title="Hh - Wave", artist="Hh"))
hd = audio(os.path.join(store, "Hh", "Wave.m4a"), 0.55, 0.19, "aac", "64k", dict(title="Wave", artist="Hh"))
outside = os.path.join(tmp, "outside", "Hh - Wave.opus")
os.makedirs(os.path.dirname(outside))
os.link(hk, outside)
h_songs = [{"title": "Hh - Wave", "artist": "Hh", "root": "downloads", "path": "youtube_music/Hh/Hh - Wave.opus",
            "names": [("downloads", "youtube_music/Hh/Hh - Wave.opus")], "duration": 20, "added": 1.0},
           {"title": "Wave", "artist": "Hh", "root": "downloads", "path": "youtube_music/Hh/Wave.m4a",
            "names": [("downloads", "youtube_music/Hh/Wave.m4a")], "duration": 20, "added": 2.0}]
h_store = os.path.join(tmp, "h_store.json")
tries = []
real_merge = sm.merge
sm.merge = lambda *a, **k: tries.append(1) or real_merge(*a, **k)
real_euid = os.geteuid
os.geteuid = lambda: real_euid() + 1
try:
    r1 = sm.run(h_songs, ROOTS, "downloads", down, h_store)
    t1 = len(tries)
    r2 = sm.run(h_songs, ROOTS, "downloads", down, h_store)
    t2 = len(tries)
    os.remove(outside)                                    # the trash batch emptied
    r3 = sm.run(h_songs, ROOTS, "downloads", down, h_store)
finally:
    os.geteuid = real_euid
    sm.merge = real_merge
check("another user's file with a name outside the library: not merged, remembered with the link counts",
      r1["failed"] == 1 and r1["merged"] == 0 and len(tries) >= 1
      and r1["pairs"][0]["result"]["error"] == "it has names outside the library"
      and any(v.get("links") == [2, 1] for v in json.load(open(h_store, encoding="utf-8"))["pairs"].values()),
      (r1, json.load(open(h_store, encoding="utf-8"))))
check("... the next round does not try again (held, not failed: the round stays quiet)",
      r2["held"] == 1 and r2["failed"] == 0 and t2 == t1, (r1, r2, t1, t2))
check("... once that name is gone (a link count changed), it is tried again and merged",
      r3["merged"] == 1 and len(tries) == t2 + 1 and os.path.samefile(os.path.join(store, "Hh", "Wave.opus"), hk)
      and ml.probe(hk).get("title") == "Wave", (r3, len(tries)))

# ---- a dropped file of the kept file's own extension leaves its name free ----------
# The first live rounds (2026-09-28) gave 62 such names " (2)": the new name was
# picked while the dropped file still stood under it.
jk = audio(os.path.join(store, "Jj", "Jj - Glow.opus"), 0.65, 0.29, "opus", "192k", dict(title="Jj - Glow", artist="Jj"))
jd = audio(os.path.join(store, "Jj", "Glow.opus"), 0.65, 0.29, "opus", "64k", dict(title="Glow", artist="Jj"))
os.makedirs(os.path.join(store, "Me", "Mix2"))
os.link(jd, os.path.join(store, "Me", "Mix2", "Glow.opus"))
res = sm.merge({"root": "downloads", "path": "youtube_music/Jj/Jj - Glow.opus",
                "names": [("downloads", "youtube_music/Jj/Jj - Glow.opus")]},
               {"names": [("downloads", "youtube_music/Jj/Glow.opus"), ("downloads", "youtube_music/Me/Mix2/Glow.opus")]},
               "Glow", "Jj", ROOTS, down, sm.new_batch())
check("one extension: the dropped file's names are the kept file's under the SAME names, no \" (2)\"",
      res["error"] is None and not res["moves"]
      and os.path.samefile(os.path.join(store, "Jj", "Glow.opus"), jk)
      and os.path.samefile(os.path.join(store, "Me", "Mix2", "Glow.opus"), jk)
      and not [f for f in os.listdir(os.path.join(store, "Jj")) + os.listdir(os.path.join(store, "Me", "Mix2"))
               if "(2)" in f], (res, os.listdir(os.path.join(store, "Jj"))))

# ---- names made " (2)" before that fix go back ------------------------------------
kk = audio(os.path.join(store, "Kk", "Kk - Rain.opus"), 0.35, 0.21, "opus", "128k", dict(title="Rain", artist="Kk"))
for rel in ("youtube_music/Kk/Rain (2).opus", "youtube_music/Kk/Taken (2).opus", "youtube_music/Kk/Shared (2).opus"):
    os.link(kk, os.path.join(down, rel))
open(os.path.join(store, "Kk", "Taken.opus"), "wb").write(b"another file")
t_store = os.path.join(tmp, "tidy_store.json")
json.dump({"pairs": {}, "renamed": {
    "downloads:youtube_music/Kk/Rain.opus": "downloads:youtube_music/Kk/Rain (2).opus",
    "downloads:youtube_music/Kk/Taken.opus": "downloads:youtube_music/Kk/Taken (2).opus",
    "downloads:youtube_music/Kk/Shared.opus": "downloads:youtube_music/Kk/Shared (2).opus",
    "downloads:youtube_music/Kk/Older.m4a": "downloads:youtube_music/Kk/Rain (2).opus"}}, open(t_store, "w"))
idx_file = os.path.join(store, ".songs.json")
idx = json.load(open(idx_file, encoding="utf-8"))
idx["vidKKKKKKKK"] = "youtube_music/Kk/Rain (2).opus"
json.dump(idx, open(idx_file, "w", encoding="utf-8"))
recorded, moved = [], []
rep = sm.run([], ROOTS, "downloads", down, t_store, shared={("downloads", "youtube_music/Kk/Shared (2).opus")},
             record_song=lambda d, vid, full: recorded.append((vid, full)),
             on_merged=lambda res, entry: moved.append(res["moves"]))
ren = json.load(open(t_store, encoding="utf-8"))["renamed"]
check("tidy: \"Rain (2).opus\" is \"Rain.opus\" again, the same file",
      rep.get("tidied") == 1 and os.path.samefile(os.path.join(store, "Kk", "Rain.opus"), kk)
      and not os.path.lexists(os.path.join(store, "Kk", "Rain (2).opus")), rep)
check("... a name another file holds, and a share link's name, stay as they are",
      os.path.samefile(os.path.join(store, "Kk", "Taken (2).opus"), kk)
      and open(os.path.join(store, "Kk", "Taken.opus"), "rb").read() == b"another file"
      and os.path.samefile(os.path.join(store, "Kk", "Shared (2).opus"), kk))
check("... the renamed names follow: the numbered name leads back, an older ref too, the own name is no rename",
      ren.get("downloads:youtube_music/Kk/Rain (2).opus") == "downloads:youtube_music/Kk/Rain.opus"
      and ren.get("downloads:youtube_music/Kk/Older.m4a") == "downloads:youtube_music/Kk/Rain.opus"
      and "downloads:youtube_music/Kk/Rain.opus" not in ren
      and ren.get("downloads:youtube_music/Kk/Taken.opus") == "downloads:youtube_music/Kk/Taken (2).opus", ren)
check("... the songs index and the favourites follow",
      recorded == [("vidKKKKKKKK", os.path.join(store, "Kk", "Rain.opus"))]
      and moved == [{("downloads", "youtube_music/Kk/Rain (2).opus"): ("downloads", "youtube_music/Kk/Rain.opus")}],
      (recorded, moved))
check("... and the round waits for the catalogue built again", rep.get("stopped") and rep["candidates"] == 0, rep)
rep = sm.run([], ROOTS, "downloads", down, t_store, shared={("downloads", "youtube_music/Kk/Shared (2).opus")})
check("... the next round has nothing to tidy", not rep.get("tidied"), rep)

# ---- an own file whose date cannot be set back -------------------------------------
g = audio(os.path.join(store, "G", "G - Air.opus"), 0.6, 0.37, "opus", "192k", dict(title="G - Air", artist="G"))
gd = audio(os.path.join(store, "G", "Air.m4a"), 0.6, 0.37, "aac", "64k", dict(title="Air", artist="G"))
real_utime = os.utime
os.utime = lambda *a, **k: (_ for _ in ()).throw(PermissionError(1, "Operation not permitted"))
try:
    res = sm.merge({"root": "downloads", "path": "youtube_music/G/G - Air.opus",
                    "names": [("downloads", "youtube_music/G/G - Air.opus")]},
                   {"names": [("downloads", "youtube_music/G/Air.m4a")]}, "Air", "G", ROOTS, down, sm.new_batch())
except Exception as exc:
    res = {"error": "raised " + repr(exc)}
finally:
    os.utime = real_utime
check("a date that cannot be set back: an error, not an exception, and the other file stays",
      str(res.get("error")).startswith("retitled, but its date could not be kept") and os.path.isfile(gd), res)

print("ALL PASS" if ok else "SOME CHECKS FAILED")
