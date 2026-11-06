"""Covers that show the wrong thing.

A song's cover is whatever youtube_music_extractor found when the song was
downloaded: the largest of YouTube's own thumbnail and a frame grabbed ten
seconds into the video. On an HD upload the frame wins (1920x1080 has more
pixels than a 1280x720 thumbnail), and ten seconds is:

  * inside the channel intro of a re-upload channel - Night Blue Music, MHM
    and Spirit of God all open with one, and not always the same length;
  * on a fade to or from black in plenty of other videos.

This finds those covers and replaces them with a frame chosen PER VIDEO: the
video is fetched once at low resolution to read its timeline (where the intro
ends, which stretch is the artwork, how bright each moment is), and the moment
that wins is then fetched again at full size. Nothing here guesses a fixed
timestamp - the whole point is that the right one differs per video.

Fetching thousands of videos again is weeks of work, so this runs as a slow
background job: a handful of songs an hour, never while a download is running,
with its state written to disk after every song. It picks up where it left off
when the server restarts, which - the unit being Restart=always - is also what
makes it survive a crash or a deploy.

THIS IS TEMPORARY. It repairs covers that were made wrong once; it is not a
feature the library needs. When the queue has drained and a full scan finds
nothing left to fix, take the whole thing out again (user, 2026-09-22):

  * this module and its state file web_files/downloads/song_covers.json
  * in server.py: the import, DEFAULT_CONFIG["song_covers"], the "song_covers"
    entry in the config-merge list, the SETTINGS_SCHEMA section, the helpers
    song_covers_settings()/downloads_busy(), the three /api/music/covers*
    routes and _song_covers.start()
  * in web_files/js/settings.js: the SECTIONS/RENDERERS entries and
    renderSongCovers()
  * in web_files/js/music.js: the song menu's "Improve this cover"
  * the "song_covers" block in config.json on prod

New downloads are not affected either way: youtube_music_extractor picks their
covers, and if it keeps picking badly, that is where the fix belongs.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time

# ---- measuring a picture ---------------------------------------------------

# Below this mean brightness (0-255) a cover is dark enough to be suspect...
BLACK_MEAN = 20
# ... but only a dark picture with no contrast left in it is the fade the frame
# grab landed on. A dark ARTWORK still has edges: the first song the job looked
# at was a 1080x1080 cover at mean 17 with std 32.7, and re-fetching it could
# only find the same picture again.
BLACK_STD = 12
# Or this much of it is nearly black even if a bright corner lifts the mean.
BLACK_DARK_SHARE = 0.9
# A frame this close to the one before it is the same shot (a slow pan over a
# still picture stays under it; a cut does not).
SAME_SHOT_BITS = 12


def measure(blob_or_image):
    """{w, h, mean, std, dark, dhash} of a picture, brightness on 0-255.

    dark is the share of nearly black pixels, dhash a 64-bit difference hash
    (each pixel against its right neighbour) - two frames of the same shot
    differ in only a few of its bits.
    """
    from PIL import Image
    import io as _io
    img = blob_or_image if hasattr(blob_or_image, "convert") else Image.open(_io.BytesIO(blob_or_image))
    w, h = img.size
    grey = img.convert("L")
    small = grey.resize((32, 32), Image.BOX)
    px = list(small.getdata())
    n = len(px)
    mean = sum(px) / n
    var = sum((p - mean) ** 2 for p in px) / n
    dark = sum(1 for p in px if p < 24) / n
    d = grey.resize((9, 8), Image.BOX)
    bits = 0
    for y in range(8):
        for x in range(8):
            bits = (bits << 1) | (1 if d.getpixel((x, y)) > d.getpixel((x + 1, y)) else 0)
    return {"w": w, "h": h, "mean": round(mean, 1), "std": round(var ** 0.5, 1),
            "dark": round(dark, 3), "dhash": f"{bits:016x}"}


def hamming(a, b):
    return bin(int(a, 16) ^ int(b, 16)).count("1")


def is_black(m):
    """A fade, not a dark picture: dark AND flat, or nearly all of it black."""
    return (m["mean"] < BLACK_MEAN and m["std"] < BLACK_STD) or m["dark"] > BLACK_DARK_SHARE


def picture_score(m):
    """How good a cover a picture makes: contrast first, then brightness in a
    middle band (a blown-out white frame is as useless as a black one), with
    nearly black pixels pulling it down."""
    contrast = min(m["std"], 70) / 70
    mean = m["mean"]
    light = max(0.0, 1 - abs(mean - 128) / 128)
    return round(contrast * (0.35 + 0.65 * light) * (1 - m["dark"]), 4)


# ---- reading and writing a song's cover ------------------------------------

MUTAGEN_EXTS = (".opus", ".ogg", ".m4a", ".mp3", ".flac")


def read_cover(path):
    """The picture embedded in a song, or None."""
    ext = os.path.splitext(path)[1].lower()
    try:
        if ext == ".m4a":
            from mutagen.mp4 import MP4
            covr = (MP4(path).tags or {}).get("covr")
            return bytes(covr[0]) if covr else None
        if ext in (".opus", ".ogg"):
            import base64
            from mutagen.flac import Picture
            audio = _ogg(path)
            blocks = (audio.tags or {}).get("metadata_block_picture") or []
            return Picture(base64.b64decode(blocks[0])).data if blocks else None
        if ext == ".mp3":
            from mutagen.id3 import ID3
            for frame in ID3(path).getall("APIC"):
                return frame.data
            return None
        if ext == ".flac":
            from mutagen.flac import FLAC
            pics = FLAC(path).pictures
            return pics[0].data if pics else None
    except Exception:
        return None
    # Matroska and friends keep the cover as a picture stream.
    try:
        out = subprocess.run(["ffmpeg", "-v", "error", "-i", path, "-an", "-vframes", "1",
                              "-f", "image2pipe", "-vcodec", "mjpeg", "-"],
                             capture_output=True, timeout=120)
        return out.stdout or None
    except Exception:
        return None


def _ogg(path):
    from mutagen.oggopus import OggOpus
    from mutagen.oggvorbis import OggVorbis
    return OggOpus(path) if path.lower().endswith(".opus") else OggVorbis(path)


def write_cover(path, jpeg):
    """Replaces the embedded picture, keeping everything else.

    In place, with mutagen: a song is hard-linked into every playlist that
    holds it (vault KSF-044), so writing a NEW file here would leave every
    playlist pointing at the old one.
    """
    ext = os.path.splitext(path)[1].lower()
    if ext == ".m4a":
        from mutagen.mp4 import MP4, MP4Cover
        audio = MP4(path)
        audio["covr"] = [MP4Cover(jpeg, imageformat=MP4Cover.FORMAT_JPEG)]
        audio.save()
        return True
    if ext in (".opus", ".ogg"):
        import base64
        from mutagen.flac import Picture
        audio = _ogg(path)
        pic = Picture()
        pic.data = jpeg
        pic.type = 3
        pic.mime = "image/jpeg"
        audio["metadata_block_picture"] = [base64.b64encode(pic.write()).decode("ascii")]
        audio.save()
        return True
    if ext == ".mp3":
        from mutagen.mp3 import MP3
        from mutagen.id3 import ID3, APIC
        audio = MP3(path, ID3=ID3)
        if audio.tags is None:
            audio.add_tags()
        audio.tags.delall("APIC")
        audio.tags["APIC"] = APIC(encoding=3, mime="image/jpeg", type=3, desc="Cover", data=jpeg)
        audio.save()
        return True
    if ext == ".flac":
        from mutagen.flac import FLAC, Picture
        audio = FLAC(path)
        pic = Picture()
        pic.data = jpeg
        pic.type = 3
        pic.mime = "image/jpeg"
        audio.clear_pictures()
        audio.add_picture(pic)
        audio.save()
        return True
    return False                      # Matroska: would need a remux, not yet


# ---- choosing the moment ---------------------------------------------------

def shot_runs(frames, step):
    """The frames grouped into shots: consecutive frames that look alike.

    A slow pan or zoom over one picture - which is what these videos are -
    stays inside one run; an intro, a cut or a fade ends it.
    """
    if not frames:
        return []
    runs = []
    start = 0
    for i in range(1, len(frames)):
        if hamming(frames[i]["dhash"], frames[i - 1]["dhash"]) > SAME_SHOT_BITS:
            runs.append((start, i - 1))
            start = i
    runs.append((start, len(frames) - 1))
    return runs


def choose_time(frames, step, min_start=12.0):
    """(seconds, why) - the moment to take the cover from.

    The longest shot is the song's own picture: an intro is short by nature,
    and everything after it is one long stretch of artwork. Inside that shot
    the best-looking frame wins (contrast, brightness, not black), with its
    first and last frame left out because those are the transition. When there
    is no long shot - a real music video, cut every few seconds - the best
    frame from the middle of the song is taken instead.
    """
    usable = [f for f in frames if f["t"] >= min_start] or frames
    if not usable:
        return None, "nothing to look at"
    # A video that goes black for a stretch has its longest "shot" there - all
    # black frames look alike. Only frames with a picture in them are candidates.
    lit = [f for f in usable if not is_black(f)]
    if not lit:
        return None, "every frame of the video is black"
    runs = shot_runs(frames, step)
    best_run = max(runs, key=lambda r: r[1] - r[0])
    run_len = (best_run[1] - best_run[0] + 1) * step
    if run_len >= max(20.0, 4 * step):
        lo, hi = best_run
        if hi - lo >= 2:              # drop the transition at either end
            lo, hi = lo + 1, hi - 1
        window = [f for f in frames[lo:hi + 1] if f["t"] >= min_start and not is_black(f)]
        if window:
            pick = max(window, key=lambda f: (picture_score(f), -abs(f["t"] - (window[0]["t"] + window[-1]["t"]) / 2)))
            return pick["t"], f"the longest shot runs {run_len:.0f}s from {frames[best_run[0]]['t']:.0f}s"
        # The long shot is the dark stretch: take the best picture elsewhere.
        pick = max(lit, key=picture_score)
        return pick["t"], f"the longest shot is black - the best frame of the rest, at {pick['t']:.0f}s"
    # Cut about: stay in the middle of the song, where a title card is over
    # and the credits have not started.
    end = frames[-1]["t"]
    middle = [f for f in lit if 0.2 * end <= f["t"] <= 0.8 * end] or lit
    pick = max(middle, key=picture_score)
    return pick["t"], f"no long shot - best frame in the middle of {end:.0f}s"


# ---- the job ---------------------------------------------------------------

DEFAULTS = {
    # On: the covers were asked to be repaired, over weeks if that is what it
    # takes. Settings is where it is stopped again.
    "enabled": True,
    # A few an hour: this is weeks of work by design, and it shares the line
    # with the downloads it must not disturb.
    "songs_per_hour": 12,
    # "black" - only covers that are black or nearly black;
    # "channels" - those plus the channels named below;
    # "all" - every song in the store, which is the longest job of the three.
    "scope": "channels",
    "channels": ["Nightblue Music", "MHM", "MHMX", "Spirit Of G", "Nightcore Spirit"],
    # The analysis copy and the frame that replaces the cover.
    "probe_height": 360,
    "frame_height": 1440,
    # Seconds between the frames of the analysis pass.
    "step_seconds": 2,
    # A new frame has to be this much better than the cover it replaces -
    # unless that cover is black, which anything beats.
    "min_gain": 0.05,
    # Stop before the disk is full (the media VM's thin pool, vault KSF-020).
    "min_free_gb": 10,
    # A song that failed this often is left alone until a scan is asked for.
    "max_tries": 3,
    # Look for new songs to fix this often.
    "rescan_days": 7,
}

STATE_NAME = "song_covers.json"

# What counts as a cover that needs fixing. Bump this whenever that changes
# (is_black, wanted, the reasons): a state file written under an older number
# has its songs read again, so decisions already taken are not carried on out
# of a file. 2: a dark artwork stopped counting as black.
RULES_VERSION = 2

# The video id the downloader leaves in a file name, as music_library reads it.
NAME_ID_RE = re.compile(r"\[([A-Za-z0-9_-]{11})\]")


class SongCovers:
    """The queue and the worker. One instance, started by server.py."""

    def __init__(self, base_dir, store_dir, settings, busy=lambda: False, log=print, forget_cover=None):
        self.base_dir = base_dir
        self.store_dir = store_dir          # downloads/youtube_music
        self.settings = settings            # -> dict, merged with DEFAULTS
        self.busy = busy                    # -> True while a download runs
        self.log = log
        # (full path, the stat before the write) -> drop what the server cached
        # of the old cover. The song keeps its old timestamp (below), so the
        # cache key can otherwise stay the same as before.
        self.forget_cover = forget_cover or (lambda path, old_stat: None)
        self.path = os.path.join(base_dir, STATE_NAME)
        self.work_dir = os.path.join(base_dir, "cover_work")
        self.lock = threading.RLock()
        self.state = {"songs": {}, "scanned_at": 0, "runs": 0, "changed": 0, "note": "",
                      "rules": RULES_VERSION}
        self.current = None
        self._load()

    # ---- state ----
    def _load(self):
        try:
            with open(self.path, encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, dict) and isinstance(data.get("songs"), dict):
                self.state = data
                self.state.setdefault("runs", 0)
                self.state.setdefault("changed", 0)
                if self.state.get("rules") != RULES_VERSION:
                    # Decided under older rules: read the songs again before
                    # fetching anything on the strength of them.
                    self.log(f"[covers] the rules changed (was {self.state.get('rules')}, "
                             f"now {RULES_VERSION}) - the songs are read again")
                    self.state["scanned_at"] = 0
                    self.state["rules"] = RULES_VERSION
        except FileNotFoundError:
            pass
        except Exception as exc:
            self.log(f"[covers] state unreadable, starting fresh: {exc}")

    def _save(self):
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(self.state, fh)
        os.replace(tmp, self.path)

    def cfg(self):
        merged = dict(DEFAULTS)
        merged.update(self.settings() or {})
        return merged

    # ---- what needs doing ----
    def wanted(self, rel, channel, cover, cfg):
        """(reason, why) for a song whose cover measures `cover`, or None."""
        if cover is None:
            return "missing", "the song carries no cover at all"
        if is_black(cover):
            return "black", f"the cover is black (brightness {cover['mean']}, {int(cover['dark'] * 100)}% dark)"
        if cfg["scope"] == "all":
            return "all", "every cover is being remade"
        if cfg["scope"] == "channels" and any(channel.lower().startswith(c.lower()) for c in cfg["channels"]):
            return "channel", f"{channel} opens its videos with an intro"
        return None

    def songs_on_disk(self):
        """Every song in the store, once each: [(rel, stat)].

        A song is hard-linked into every playlist that holds it (vault
        KSF-044), so the same file turns up under several names. The cover is
        written into the file, which fixes all of them at once - the name kept
        here is the one under the artist's own folder, the shortest path.
        """
        by_file = {}
        for root, _dirs, names in os.walk(self.store_dir):
            for name in names:
                if not name.lower().endswith(MUTAGEN_EXTS + (".mka",)):
                    continue
                path = os.path.join(root, name)
                try:
                    st = os.stat(path)
                except OSError:
                    continue
                rel = os.path.relpath(path, self.store_dir).replace(os.sep, "/")
                key = (st.st_dev, st.st_ino)
                best = by_file.get(key)
                if best is None or (rel.count("/"), rel) < (best[0].count("/"), best[0]):
                    by_file[key] = (rel, st)
        return sorted(by_file.values())

    def scan(self, force=False):
        """Walks the store and fills the queue. Reads files, downloads nothing."""
        cfg = self.cfg()
        seen = 0
        queued = 0
        with self.lock:
            songs = self.state["songs"]
        for rel, st in self.songs_on_disk():
            path = os.path.join(self.store_dir, rel)
            channel = rel.split("/")[0]
            seen += 1
            entry = songs.get(rel) or {}
            # A song already looked at, unchanged since, is left alone.
            if not force and entry.get("mtime") == st.st_mtime_ns and entry.get("state") in ("done", "kept", "skipped"):
                continue
            # What someone asked for by hand stays asked for.
            if not force and entry.get("state") == "queued" and entry.get("reason") == "asked":
                continue
            if entry.get("state") == "failed" and entry.get("tries", 0) >= cfg["max_tries"] and not force:
                continue
            blob = read_cover(path)
            cover = None
            if blob:
                try:
                    cover = measure(blob)
                except Exception:
                    cover = None
            want = self.wanted(rel, channel, cover, cfg)
            entry.update(path=rel, channel=channel, mtime=st.st_mtime_ns,
                         cover={k: cover[k] for k in ("w", "h", "mean", "std", "dark")} if cover else None)
            if not want:
                entry.update(state="skipped", reason="", why="the cover looks fine")
            elif rel.lower().endswith(".mka"):
                # Writing a picture back into Matroska needs a remux, which
                # this does not do yet - better to say so than to pretend.
                entry.update(state="skipped", reason=want[0], why="Matroska cannot be rewritten here yet")
            else:
                entry.update(state="queued", reason=want[0], why=want[1])
                queued += 1
            with self.lock:
                songs[rel] = entry
        with self.lock:
            self.state["scanned_at"] = time.time()
            self.state["note"] = f"{seen} songs read, {queued} queued"
            self._save()
        self.log(f"[covers] scan: {seen} songs, {queued} queued")
        return {"seen": seen, "queued": queued}

    def enqueue(self, rel, reason="asked"):
        """Puts one song at the front of the queue (the page's own button)."""
        path = os.path.join(self.store_dir, rel)
        if not os.path.isfile(path):
            return False
        with self.lock:
            entry = self.state["songs"].get(rel) or {"path": rel, "channel": rel.split("/")[0]}
            entry.update(state="queued", reason=reason, why="asked for by hand", tries=0, front=True)
            self.state["songs"][rel] = entry
            self._save()
        return True

    def next_item(self):
        with self.lock:
            queued = [e for e in self.state["songs"].values() if e.get("state") == "queued"]
        if not queued:
            return None
        order = {"asked": 0, "missing": 1, "black": 2, "channel": 3, "all": 4}
        queued.sort(key=lambda e: (0 if e.get("front") else 1, order.get(e.get("reason"), 9),
                                   e.get("tries", 0), e.get("path", "")))
        return queued[0]

    # ---- doing one ----
    def video_id(self, path):
        # Same two places music_library reads it from: the tags first, then
        # the "[<id>]" the downloader leaves in the file name.
        found = self._video_id_from_tags(path)
        if found:
            return found
        m = NAME_ID_RE.search(os.path.basename(path))
        return m.group(1) if m else None

    def _video_id_from_tags(self, path):
        try:
            import mutagen
            audio = mutagen.File(path)
            tags = (audio.tags if audio else None) or {}
            for key in ("purl", "PURL", "comment", "COMMENT", "\xa9cmt", "description"):
                try:
                    value = tags.get(key)
                except Exception:
                    continue
                if not value:
                    continue
                text = str(value[0] if isinstance(value, list) else value)
                if "watch?v=" in text:
                    return text.rsplit("watch?v=", 1)[-1].split("&")[0].strip()
                if "youtu.be/" in text:
                    return text.rsplit("youtu.be/", 1)[-1].split("?")[0].strip()
            for frame in (audio.tags.getall("WOAS") if hasattr(audio.tags, "getall") else []):
                url = str(getattr(frame, "url", ""))
                if "watch?v=" in url:
                    return url.rsplit("watch?v=", 1)[-1].split("&")[0].strip()
        except Exception:
            return None
        return None

    def ytdlp(self, *args, timeout=900):
        cmd = [sys.executable, "-m", "yt_dlp", "--remote-components", "ejs:github", "--no-warnings"]
        cookies = os.path.join(self.base_dir, "cookies_youtube.txt")
        if os.path.exists(cookies):
            cmd += ["--cookies", cookies]
        return subprocess.run(cmd + list(args), capture_output=True, text=True, timeout=timeout)

    def timeline(self, video_path, step):
        """Frames across the whole video, measured."""
        from PIL import Image
        out = os.path.join(os.path.dirname(video_path), "frames")
        os.makedirs(out, exist_ok=True)
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", video_path,
                        "-vf", f"fps=1/{step},scale=-1:180", os.path.join(out, "f%04d.jpg")],
                       capture_output=True, timeout=900)
        frames = []
        for i, name in enumerate(sorted(os.listdir(out))):
            try:
                m = measure(Image.open(os.path.join(out, name)))
            except Exception:
                continue
            m["t"] = i * step
            frames.append(m)
        return frames

    def apply_cover(self, path, jpeg):
        """The new picture into the song, and nothing else changed.

        The library dates a song by its file timestamp, so writing a cover
        would otherwise send a thousand old songs to the top of "Recently
        added": the song keeps the date it was downloaded on, and what the
        server cached of the old cover is dropped instead.
        """
        before = os.stat(path)
        if not write_cover(path, jpeg):
            return False
        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
        try:
            self.forget_cover(path, before)
        except Exception as exc:
            self.log(f"[covers] the old cover of {path} stayed in the cache: {exc}")
        return True

    def process(self, entry):
        """One song: analyse, fetch the frame, write it in."""
        cfg = self.cfg()
        rel = entry["path"]
        path = os.path.join(self.store_dir, rel)
        entry["tries"] = entry.get("tries", 0) + 1
        entry["last"] = time.time()
        entry.pop("front", None)

        def finish(state, note, **extra):
            entry.update(state=state, note=note, **extra)
            with self.lock:
                self.state["songs"][rel] = entry
                self.state["runs"] = self.state.get("runs", 0) + 1
                if state == "done":
                    self.state["changed"] = self.state.get("changed", 0) + 1
                self._save()
            self.log(f"[covers] {rel}: {state} - {note}")

        if not os.path.isfile(path):
            return finish("skipped", "the song is gone")
        vid = entry.get("vid") or self.video_id(path)
        if not vid:
            return finish("skipped", "no video id in the song's tags, nothing to fetch")
        entry["vid"] = vid
        free = shutil.disk_usage(self.base_dir).free / 1e9
        if free < cfg["min_free_gb"]:
            return finish("queued", f"waiting for disk space ({free:.1f} GB free)")

        os.makedirs(self.work_dir, exist_ok=True)
        work = tempfile.mkdtemp(dir=self.work_dir, prefix="song_")
        try:
            low = os.path.join(work, "probe.mp4")
            got = self.ytdlp("-f", f"bv*[height<={cfg['probe_height']}]/worstvideo/worst",
                             "--no-part", "-o", low, f"https://www.youtube.com/watch?v={vid}")
            if got.returncode != 0 or not os.path.exists(low):
                last = (got.stderr or "").strip().splitlines()
                return finish("failed", f"the video could not be fetched: {last[-1][:160] if last else 'unknown'}")
            frames = self.timeline(low, cfg["step_seconds"])
            if len(frames) < 3:
                return finish("failed", "the video gave no frames to look at")
            old_blob = read_cover(path)
            old = measure(old_blob) if old_blob else None
            # A video that is one still picture from start to end has nothing
            # else to offer - if the cover already shows it, the second (big)
            # download would only fetch the same picture again.
            if old and len(shot_runs(frames, cfg["step_seconds"])) == 1 \
                    and hamming(frames[len(frames) // 2]["dhash"], old["dhash"]) <= SAME_SHOT_BITS:
                return finish("kept", "the video is one still picture and the cover already shows it")
            when, why = choose_time(frames, cfg["step_seconds"])
            if when is None:
                return finish("failed", why)
            # The chosen moment again, this time at full size.
            hi = os.path.join(work, "frame.mp4")
            got = self.ytdlp("-f", f"bv*[height<={cfg['frame_height']}]/bv*",
                             "--download-sections", f"*{when}-{when + 3}",
                             "--force-keyframes-at-cuts", "--no-part", "-o", hi,
                             f"https://www.youtube.com/watch?v={vid}")
            frame_path = os.path.join(work, "cover.jpg")
            source = hi if got.returncode == 0 and os.path.exists(hi) else low
            seek = [] if source == hi else ["-ss", str(when)]
            subprocess.run(["ffmpeg", "-v", "error", "-y"] + seek + ["-i", source,
                            "-frames:v", "1", "-q:v", "2", frame_path],
                           capture_output=True, timeout=300)
            if not os.path.exists(frame_path):
                return finish("failed", "the frame could not be cut out")
            with open(frame_path, "rb") as fh:
                jpeg = fh.read()
            new = measure(jpeg)
            entry["at"] = when
            entry["why_time"] = why
            entry["new"] = {k: new[k] for k in ("w", "h", "mean", "std", "dark")}
            better = old is None or is_black(old) or picture_score(new) >= picture_score(old) + cfg["min_gain"]
            if is_black(new):
                return finish("failed", f"the frame at {when:.0f}s is black as well")
            if not better:
                return finish("kept", f"the frame at {when:.0f}s is no better than the cover "
                                      f"({picture_score(new)} against {picture_score(old)})")
            if not self.apply_cover(path, jpeg):
                return finish("skipped", "this container cannot carry a new cover yet")
            entry["mtime"] = os.stat(path).st_mtime_ns
            return finish("done", f"cover taken from {when:.0f}s - {why}")
        except Exception as exc:
            return finish("failed", f"{type(exc).__name__}: {exc}"[:200])
        finally:
            shutil.rmtree(work, ignore_errors=True)

    # ---- the loop ----
    def run_forever(self):
        self.log("[covers] worker started")
        while True:
            try:
                cfg = self.cfg()
                if not cfg["enabled"]:
                    time.sleep(60)
                    continue
                if self.busy():
                    # Downloads come first: they are what the user is waiting
                    # for, and two yt-dlp runs at once is how rate limits are
                    # earned.
                    time.sleep(120)
                    continue
                if not self.state.get("scanned_at") or \
                        time.time() - self.state["scanned_at"] > cfg["rescan_days"] * 86400:
                    self.scan()
                item = self.next_item()
                if not item:
                    time.sleep(600)
                    continue
                self.current = item.get("path")
                self.process(item)
                self.current = None
                time.sleep(max(5.0, 3600.0 / max(1, cfg["songs_per_hour"])))
            except Exception as exc:
                self.log(f"[covers] worker error: {type(exc).__name__}: {exc}")
                time.sleep(120)

    def start(self):
        # A restart in the middle of a fetch leaves its temp directory behind,
        # and nothing else uses this one.
        shutil.rmtree(self.work_dir, ignore_errors=True)
        thread = threading.Thread(target=self.run_forever, name="song-covers", daemon=True)
        thread.start()
        return thread

    # ---- what the page shows ----
    def status(self):
        with self.lock:
            songs = list(self.state["songs"].values())
            scanned_at = self.state.get("scanned_at")
            runs, changed = self.state.get("runs", 0), self.state.get("changed", 0)
        states = {}
        reasons = {}
        for e in songs:
            states[e.get("state", "?")] = states.get(e.get("state", "?"), 0) + 1
            if e.get("state") == "queued":
                reasons[e.get("reason", "?")] = reasons.get(e.get("reason", "?"), 0) + 1
        done = [e for e in songs if e.get("state") in ("done", "kept", "failed") and e.get("last")]
        done.sort(key=lambda e: -(e.get("last") or 0))
        cfg = self.cfg()
        queued = states.get("queued", 0)
        return {
            "enabled": cfg["enabled"], "scope": cfg["scope"], "songs_per_hour": cfg["songs_per_hour"],
            "scanned_at": scanned_at, "states": states, "queued_by_reason": reasons,
            "runs": runs, "changed": changed, "current": self.current,
            "hours_left": round(queued / max(1, cfg["songs_per_hour"]), 1),
            "recent": [{k: e.get(k) for k in ("path", "channel", "state", "reason", "note", "at", "last", "new")}
                       for e in done[:25]],
        }
