"""The real extractor, end to end, on a temp tree: a song is stored once under
its artist, hard-linked into the playlist that asked for it, and listed in the
songs index. Nothing of the real library is touched.
  venv/bin/python extractor_check.py <staged server.py, or its directory>
youtube_music_extractor.py and recent_media_store.py are taken from beside it
(run_all.sh copies the production ones there when they are not part of the
change).
"""
import atexit
import json
import os
import shutil
import subprocess
import sys
import tempfile

STAGE = os.path.abspath(sys.argv[1] if len(sys.argv) > 1 else os.path.dirname(__file__))
if os.path.isfile(STAGE):
    STAGE = os.path.dirname(STAGE)
EXTRACTOR = os.path.join(STAGE, "youtube_music_extractor.py")
tmp = tempfile.mkdtemp(prefix="extractor_check_")
atexit.register(shutil.rmtree, tmp, True)
ok = True


def check(label, cond, detail=""):
    global ok
    ok = ok and bool(cond)
    print(("PASS " if cond else "FAIL ") + label + (f"  ({detail})" if detail and not cond else ""), flush=True)


downloads = os.path.join(tmp, "web_files", "downloads")
os.makedirs(downloads)
music = os.path.join(downloads, "youtube_music")
artist_dir = os.path.join(music, "Vicetone")
phonk = os.path.join(music, "Crash Games UHD", "Phonk")
anime = os.path.join(music, "Crash Games UHD", "Anime Epic")


def source_video(name, seconds=3, freq=440):
    path = os.path.join(tmp, name)
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"testsrc2=size=64x48:rate=5",
                    "-f", "lavfi", "-i", f"sine=frequency={freq}:duration={seconds}",
                    "-t", str(seconds), "-c:v", "libvpx", "-c:a", "libopus", "-shortest", path], check=True)
    return path


def run(video, out_dir, title, artist, vid, link=None):
    cmd = [sys.executable, EXTRACTOR, video, out_dir, title, artist, vid]
    if link:
        cmd += ["--link", link]
    return subprocess.run(cmd, capture_output=True, text=True)


def index():
    try:
        with open(os.path.join(music, ".songs.json"), encoding="utf-8") as fh:
            return json.load(fh)
    except OSError:
        return {}


# 1. A playlist track: stored under the artist, linked into the playlist.
video = source_video("song1.webm")
res = run(video, artist_dir, "Astronomia", "Vicetone", "vid00000001", phonk)
song = os.path.join(artist_dir, "Astronomia.opus")
link = os.path.join(phonk, "Astronomia.opus")
check("the song is stored under its artist", os.path.isfile(song), res.stdout + res.stderr)
check("... and the playlist holds the same file, not a copy",
      os.path.isfile(link) and os.path.samefile(link, song) and os.stat(song).st_nlink == 2,
      [os.path.isfile(link), os.stat(song).st_nlink if os.path.exists(song) else None])
check("... which is one file's worth of disk", os.stat(song).st_blocks == os.stat(link).st_blocks)
check("the songs index points at the stored song",
      index().get("vid00000001") == "youtube_music/Vicetone/Astronomia.opus", index())
check("the source video is gone", not os.path.exists(video))
check("it plays: opus audio with a cover", subprocess.run(
    ["ffprobe", "-v", "error", "-show_entries", "stream=codec_name", "-of", "csv=p=0", song],
    capture_output=True, text=True).stdout.strip().splitlines()[0] == "opus")

# 2. The same song wanted by a second playlist, downloaded again: one file
#    still, every name up to date, nothing orphaned.
before_ino = os.stat(song).st_ino
video2 = source_video("song1b.webm", seconds=4, freq=550)
res = run(video2, artist_dir, "Astronomia", "Vicetone", "vid00000001", anime)
link2 = os.path.join(anime, "Astronomia.opus")
check("downloaded again: still one stored song, now with three names",
      os.path.isfile(link2) and os.path.samefile(link2, song) and os.stat(song).st_nlink == 3,
      [os.path.isfile(link2), os.stat(song).st_nlink])
check("... the old names were not left behind on an old file",
      os.stat(song).st_ino == before_ino and os.path.samefile(link, song),
      [before_ino, os.stat(song).st_ino])
check("... and every name has the new audio",
      abs(float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", link],
                               capture_output=True, text=True).stdout.strip()) - 4.0) < 0.5)
check("one song in the artist folder, not two", sorted(os.listdir(artist_dir)) == ["Astronomia.opus"],
      os.listdir(artist_dir))

# 3. A different song that happens to share the title: neither is lost.
video3 = source_video("song2.webm", freq=880)
res = run(video3, artist_dir, "Astronomia", "Vicetone", "vid00000002", phonk)
other = os.path.join(artist_dir, "Astronomia [vid00000002].opus")
check("a different song with the same title gets its own name",
      os.path.isfile(other) and not os.path.samefile(other, song), os.listdir(artist_dir))
check("... and its own name in the playlist too",
      os.path.isfile(os.path.join(phonk, "Astronomia [vid00000002].opus"))
      and os.path.samefile(os.path.join(phonk, "Astronomia [vid00000002].opus"), other), os.listdir(phonk))
check("... the first song's name in the playlist still points at the first song",
      os.path.samefile(link, song))

# 4. A single video (no playlist): artist folder only.
video4 = source_video("single.webm", freq=330)
res = run(video4, os.path.join(music, "Alan Walker"), "Alone", "Alan Walker", "vid00000003")
alone = os.path.join(music, "Alan Walker", "Alone.opus")
check("a single video is stored under its artist, with one name",
      os.path.isfile(alone) and os.stat(alone).st_nlink == 1, res.stdout + res.stderr)
check("... and is in the index too", index().get("vid00000003") == "youtube_music/Alan Walker/Alone.opus")

print("ALL PASS" if ok else "SOME CHECKS FAILED")
sys.exit(0 if ok else 1)
