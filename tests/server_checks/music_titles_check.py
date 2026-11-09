"""music.example.org shows song titles without a visualiser channel's "Music
Visualization" / "... Visualizer", emoji and a transcode's file name tail
(music_library.shown_title);
the catalogue, the files and media-dl keep the full title. Pure functions plus
the staged server.py's share songs, read from its AST. Nothing real is read.

  venv/bin/python music_titles_check.py <staged server.py>
"""
import ast
import os
import sys

BASE = os.environ.get("ATRIHORD_DIR") or os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SERVER = os.path.abspath(sys.argv[1])
STAGE = os.path.dirname(SERVER)
sys.path.insert(0, BASE)
sys.path.insert(0, STAGE)
import music_library as ml  # noqa: E402

assert os.path.dirname(os.path.abspath(ml.__file__)) == STAGE, ml.__file__
ok = True


def check(label, cond, detail=""):
    global ok
    ok = ok and bool(cond)
    print(("PASS " if cond else "FAIL ") + label + (f"  ({detail})" if detail and not cond else ""), flush=True)


def same(raw, want, artist=None):
    got = ml.shown_title(raw, artist)
    check(f"{raw!r} ({artist}) -> {want!r}", got == want, f"got {got!r}")


# The visualiser label, with and without a bar, dash or emoji after it (real titles).
same("Code Red Music Visualization 💜 🎶 💎", "Code Red")
same("Radioactive | Music Visualization🖤🎶💎", "Radioactive")
same("Do It Right ｜ Music Visualization🖤🎶💎", "Do It Right")
same("Slip and slide (Bershka Mix) Music Visualization", "Slip and slide (Bershka Mix)")
same("CHANGZ  Music Visualization🖤🎶💎✨", "CHANGZ")
same("You Are The One (Remix) | Musical Visualization🖤🎶💎", "You Are The One (Remix)")
same("Halo of Blood - Music Visualization_waifu2x_3840x2160_webm_W2xEX_VFI_mp4_1", "Halo of Blood")
same("fim do mundo ｜ Music Visualization🖤🎶💎_waifu2x_3840x2160_mp4", "fim do mundo")
same("Song [Music Visualization]", "Song")
# Emoji anywhere, joined sequences, flags, keycaps, skin tones.
same("🐫 Anubis  (Brandberg)  - Nightcore 🐫", "Anubis (Brandberg) - Nightcore")
same("High Energy Cyberpunk Synthwave⚡| Velocity Strike", "High Energy Cyberpunk Synthwave | Velocity Strike")
same("💜Geoxor Epic Playlist | 2021💜 by YouCute", "Geoxor Epic Playlist | 2021 by YouCute")
same("Alfons Feat. Sayfro😈 - Milkshake", "Alfons Feat. Sayfro - Milkshake")
same("Family 👨‍👩‍👧 Song", "Family Song")
same("Wave 👋🏽 Song 🇩🇪", "Wave Song")
same("Love ❤️ You #️⃣1", "Love You 1")
# Text symbols stay; so does a title's own spacing when nothing is removed.
same("!PVNDEMIK ✖ Galiv - CHANGZ", "!PVNDEMIK ✖ Galiv - CHANGZ")
same("TRANCE ◉ Lil Nas X - Old Town Road", "TRANCE ◉ Lil Nas X - Old Town Road")
same("Lunak - Missing You ♪", "Lunak - Missing You ♪")
same("ZD - ™  Music Visualization🖤🎶💎", "ZD - ™")
same("バトルオブクローンズ　俺大戦Ⅶ", "バトルオブクローンズ　俺大戦Ⅶ")
same("Nightcore -  No Glory (Lyrics)", "Nightcore -  No Glory (Lyrics)")
# UNFINISH's "... Visualizer" (real titles, artist UNFINISH from the tags): the
# artist's name goes with it only when it IS the song's artist.
same("Here Goes Nothing - UNFINISH EDM Music Visualizer", "Here Goes Nothing", "UNFINISH")
same("Inner Tyrant - UNFINISH / EDM Animate Visualizer", "Inner Tyrant", "UNFINISH")
same("Inner Tyrant - UNFINISH  EDM Animate Visualizer", "Inner Tyrant", "UNFINISH")
same("I Just Wanna Love U - EDM Music Video Visualizer", "I Just Wanna Love U", "UNFINISH")
same("Here Goes Nothing - UNFINISH EDM Music Visualizer", "Here Goes Nothing - UNFINISH EDM", "Someone Else")
same("Artist - Song Name EDM Music Visualizer", "Artist - Song Name EDM", "Artist")
# The song's own artist repeated at the end goes; anyone else's name stays.
same("Maniac Candy - UNFINISH", "Maniac Candy", "UNFINISH")
same("T.K.O - UNFINISH", "T.K.O", "UNFINISH")
same("Maniac Candy - UNFINISH", "Maniac Candy - UNFINISH", "Crash Games UHD")
same("Maniac Candy - UNFINISH", "Maniac Candy - UNFINISH")
same("Song - Artist | Music Visualization🖤🎶💎", "Song", "Artist")
same("Nightcore - Song", "Nightcore - Song", "Nightcore")
same("UNFINISH", "UNFINISH", "UNFINISH")
same("Song - ★★", "Song - ★★", "♪")
same("Radioactive | Music Visualization🖤🎶💎", "Radioactive", "Bullet For My Valentine")
same("-ERROR-", "-ERROR-")
same("Visualization of Love", "Visualization of Love")
# Nothing left: the full title.
same("🖤🎶💎", "🖤🎶💎")
same("Music Visualization", "Music Visualization")
same("", "")
check("None -> ''", ml.shown_title(None) == "")

# for_viewer: rows and album lists show it; the catalogue keeps the full title.
RAW = "Code Red | Music Visualization🖤🎶💎"
song = {"title": RAW, "artist": "Tenebrax", "album": RAW, "duration": 120, "root": "downloads",
        "path": "youtube_music/Tenebrax/Code Red.opus", "video_id": "", "genre": None, "added": 1, "cover": False,
        "names": [("downloads", "youtube_music/Tenebrax/Code Red.opus")], "videos": [], "video": None}
other = dict(song, title="Plain", album="Bürger 🔥 Album", path="youtube_music/Tenebrax/Plain.opus",
             names=[("downloads", "youtube_music/Tenebrax/Plain.opus")])
catalog = {"songs": [song, other], "playlists": [], "channels": [],
           "albums": [{"id": "a1", "title": "Bürger 🔥 Album", "artist": "Tenebrax", "songs": [0, 1]}],
           "loudness": {}}
view = ml.for_viewer(catalog, {"downloads"})
f = view["fields"]
row = dict(zip(f, view["songs"][0]))
check("row title shown", row["title"] == "Code Red", row["title"])
check("row album shown the same way (the page hides an album equal to the title)",
      row["album"] == row["title"], row["album"])
check("album list title shown", view["albums"][0]["title"] == "Bürger Album", view["albums"][0]["title"])
check("catalogue keeps the full title", catalog["songs"][0]["title"] == RAW and catalog["albums"][0]["title"]
      == "Bürger 🔥 Album")
check("row path untouched (files keep their names)", row["path"] == song["path"])
check("empty album stays empty", dict(zip(f, ml.for_viewer(dict(catalog, songs=[dict(song, album=None)],
                                                                  albums=[]), {"downloads"})["songs"][0]))["album"] == "")

# server.py: a music share's songs are named the same way.
tree = ast.parse(open(SERVER, encoding="utf-8").read())
fn = next((n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "_music_share_songs"), None)
check("server.py has _music_share_songs", fn is not None)
keys = {}
for node in ast.walk(fn) if fn else ():
    if isinstance(node, ast.Dict):
        for k, v in zip(node.keys, node.values):
            if isinstance(k, ast.Constant) and k.value in ("title", "album"):
                keys[k.value] = ast.unparse(v)
check("share song title uses shown_title with the artist",
      "music_library.shown_title(title, artist)" in keys.get("title", ""), keys)
check("share song album uses shown_title with the artist",
      "music_library.shown_title(album, artist)" in keys.get("album", ""), keys)

print("ALL PASS" if ok else "SOME FAILED")
sys.exit(0 if ok else 1)
