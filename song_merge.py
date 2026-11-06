"""One song, one title (user, 2026-09-28, of "Geoxor & Canonblade - Sakura" by
Geoxor playing next to "Sakura" by Geoxor & Canonblade: "This happens often.
When such cases occur, keep the title that does not contain the artist name
again. Keep the higher audio bitrate/resolution version around with the new
title.").

The same recording arrives twice when it is downloaded from two places: an
artist's or a visualiser channel's own upload, titled "<Artist> - <Song>" and
filed under the uploader, and YouTube Music's track of it, titled "<Song>" under
its artist. Two video ids, so the song store (vault KSF-044: identity is the
video id) keeps both; two titles, so song_content_dedup.py (same title = a
candidate) never compared them. It ran once, too: nothing caught what came
after.

CANDIDATES (candidates()): two songs of the catalogue where one title is the
other's with an artist in front ("Geoxor & Canonblade - Sakura" / "Sakura"),
the name in front is an artist of either song, and the clean song's artist is
among them. Two songs of one title whose artists overlap are candidates as well
(2026-09-20: "the same song twice is not" fine). Titles are compared folded:
case, accents, punctuation, a " [videoid]", a trailing "| Music Visualization"
and an "(Official Video)" do not count. A candidate is only a guess: the SOUND
decides (same(), song_content_dedup's measure), so a remix, a slowed edit or
another take never merges.

WHICH FILE (better()): the higher audio bitrate when they are more than 3%
apart, then the bigger cover (the "resolution"); a tie keeps the song store's
own file (youtube_music/<Artist>/) over a copy elsewhere, then a file that takes
tags in place over a Matroska one (_home), then the file that already has the
clean title (or, one title twice, the older file). The kept file takes the clean title and artist
into its own tags - written in place, because its other names are hard links -
and keeps its own timestamp, which the music page dates the song by.

THE OTHER FILE goes to its root's trash, never deleted (the trash page restores
it), and every name it had becomes a name of the kept file: with the kept
file's extension (fix_link_extensions.py: a name must say what format is
inside) unless a share link uses the name. The songs index points both video
ids at the song, which is how the music page still finds the dropped id in
playlists, history and radios (music_library "aliases"); a renamed name is
remembered for the same reason.

Decisions are kept per pair of file versions (song_merges.json): a pair that
sounds different is listened to once, not after every catalogue build.

Library code: server.py runs a round after a catalogue build (song merge);
song_merge_report.py runs the listening half, read-only, over the library.
"""
from __future__ import annotations

import collections
import fcntl
import json
import os
import re
import secrets
import shutil
import stat
import subprocess
import threading
import time
import unicodedata

import numpy as np

TRASH_DIR_NAME = ".trash"
MUSIC_DIR = "youtube_music"
# The sound, as song_content_dedup.py measures it (calibrated 2026-09-20 on
# this library): 8 kHz mono in 50 ms frames, the first 150 s.
RATE = 8000
FRAME = 400
SECONDS = 150
SAME_LOUDNESS = 0.85
SAME_ATTACKS = 0.90
# Wider than song_content_dedup's one second: an upload can start a few
# seconds later than the track (a visualiser's intro) and run as much longer.
# A different edit is further apart than that.
MAX_LAG = 120                 # frames: six seconds either way
MAX_APART_S = 6.0
BITRATE_TIE = 0.03

_ID_RE = re.compile(r"\s*\[[A-Za-z0-9_-]{11}\]")
_TAIL_RE = re.compile(r"\s*[|│｜].*$")
# "(Official Video)", "[Monstercat Release]", "(Lyrics)": what an upload adds
# to a title and the track does not have.
_NOISE_RE = re.compile(r"\s*[(\[][^()\[\]]*\b(?:official|video|audio|lyrics?|visuali[sz]\w*|release|"
                       r"mv|hd|4k|uhd|hq)\b[^()\[\]]*[)\]]", re.I)
_DASH_RE = re.compile(r"\s+[-–—]\s+")
_SPLIT_RE = re.compile(r"\s*(?:&|,|\+|/|;|\bx\b|\bvs\.?(?=\s)|\bfeat\.?(?=\s)|\bft\.?(?=\s)|\bfeaturing\b|\band\b)\s*",
                       re.I)


def fold(text):
    """Lower case, accents off, letters and digits only (any script)."""
    text = unicodedata.normalize("NFKD", text or "")
    return "".join(c for c in text.lower() if c.isalnum())


def bare_title(title):
    """The title without a video id, a "| ..." tail and upload noise."""
    t = _TAIL_RE.sub("", _ID_RE.sub("", title or ""))
    return _NOISE_RE.sub("", t).strip()


_VIS_TAIL_RE = re.compile(r"\s*[|│｜]\s*(?:music\s+)?visuali[sz]\w*.*$", re.I)


def display_title(title):
    """The title a merged song is given: without a video id and a visualiser's
    "| Music Visualization..." tail (the Jellyfin copies of those carry
    "_waifu2x_3840x2160_mp4..." after it too). Anything else in brackets or
    after a bar is the song's own and stays."""
    return _VIS_TAIL_RE.sub("", _ID_RE.sub("", title or "")).strip() or (title or "").strip()


def _fuller(first, other):
    """`other` when it is `first` with more punctuation - a title read from a
    file name lost its "?", ":" and quotes to sanitising - else `first`."""
    return other if fold(other) == fold(first) and len(other) > len(first) else first


def artist_parts(name):
    """The artists a credit names, folded: "Geoxor & Canonblade" ->
    {"geoxorcanonblade", "geoxor", "canonblade"}."""
    out = {fold(name)}
    out.update(fold(p) for p in _SPLIT_RE.split(name or ""))
    return {p for p in out if len(p) >= 2}


def prefixed(title):
    """[(front, rest)] for every " - " in a title."""
    t = bare_title(title)
    out = []
    for m in _DASH_RE.finditer(t):
        front, rest = t[:m.start()].strip(), t[m.end():].strip()
        if front and rest:
            out.append((front, rest))
    return out


def candidates(songs):
    """Pairs of catalogue songs that may be one recording, as dicts:
    {"kind": "artist", "prefixed": i, "clean": j} - song i's title is song j's
    with an artist in front; {"kind": "same", "a": i, "b": j} - one title,
    overlapping artists. `songs` as music_library.build makes them."""
    by_title = collections.defaultdict(list)
    for i, s in enumerate(songs):
        key = fold(bare_title(s.get("title")))
        if key:
            by_title[key].append(i)
    parts = [artist_parts(s.get("artist") or "") for s in songs]
    out, seen = [], set()
    for i, s in enumerate(songs):
        for front, rest in prefixed(s.get("title")):
            fp = artist_parts(front)
            for j in by_title.get(fold(rest), ()):
                if j == i or frozenset((i, j)) in seen:
                    continue
                if fp & (parts[i] | parts[j]) and parts[j] & (fp | parts[i]):
                    seen.add(frozenset((i, j)))
                    out.append({"kind": "artist", "prefixed": i, "clean": j})
    for members in by_title.values():
        for x, i in enumerate(members):
            for j in members[x + 1:]:
                if frozenset((i, j)) not in seen and parts[i] & parts[j]:
                    seen.add(frozenset((i, j)))
                    out.append({"kind": "same", "a": i, "b": j})
    return out


def pair_songs(pair):
    return (pair["prefixed"], pair["clean"]) if pair["kind"] == "artist" else (pair["a"], pair["b"])


# ---- the sound ----------------------------------------------------------------

def envelope(path):
    """The track's loudness over time (50 ms frames), or None if it will not
    decode."""
    try:
        out = subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-t", str(SECONDS), "-i", path,
                              "-map", "0:a:0", "-ac", "1", "-ar", str(RATE), "-f", "s16le", "-"],
                             capture_output=True, timeout=600)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if out.returncode != 0 or len(out.stdout) < RATE * 4:
        return None
    pcm = np.frombuffer(out.stdout, dtype="<i2").astype(np.float32)
    n = len(pcm) // FRAME
    return np.abs(pcm[:n * FRAME].reshape(n, FRAME)).mean(axis=1)


def onsets(env):
    """Where the attacks are, not how loud they are - what a remastered upload
    keeps when its loudness has been changed on purpose."""
    return np.maximum(np.diff(np.log1p(env)), 0)


def similarity(a, b, max_lag=MAX_LAG):
    """The best correlation of two envelopes within max_lag frames of shift."""
    n = min(len(a), len(b))
    if n < 40:
        return 0.0
    best = -1.0
    for lag in range(-max_lag, max_lag + 1):
        if lag >= 0:
            x, y = a[lag:n], b[:n - lag]
        else:
            x, y = a[:n + lag], b[-lag:n]
        if len(x) < 40:
            continue
        x = x - x.mean()
        y = y - y.mean()
        d = float(np.linalg.norm(x) * np.linalg.norm(y))
        if d > 0:
            best = max(best, float(np.dot(x, y)) / d)
    return best


def same(path_a, path_b, dur_a=0.0, dur_b=0.0):
    """{"same", "loudness", "attacks", "apart"}, or None when either file
    does not decode."""
    apart = abs((dur_a or 0) - (dur_b or 0)) if dur_a and dur_b else 0.0
    if apart > MAX_APART_S:
        return {"same": False, "loudness": 0.0, "attacks": 0.0, "apart": round(apart, 2)}
    a, b = envelope(path_a), envelope(path_b)
    if a is None or b is None:
        return None
    loud = similarity(a, b)
    attacks = loud if loud >= SAME_LOUDNESS else similarity(onsets(a), onsets(b))
    return {"same": loud >= SAME_LOUDNESS or attacks >= SAME_ATTACKS,
            "loudness": round(loud, 3), "attacks": round(attacks, 3), "apart": round(apart, 2)}


# ---- which file ---------------------------------------------------------------

def quality(path):
    """{"kbps", "cover"}: the audio's bitrate and the cover's pixel count."""
    try:
        res = subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                              "format=duration:stream=index,codec_type,codec_name,bit_rate,width,height",
                              "-of", "json", path], capture_output=True, text=True, timeout=120)
        data = json.loads(res.stdout or "{}")
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return {"kbps": 0.0, "cover": 0}
    streams = data.get("streams") or []
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    cover = max((int(s.get("width") or 0) * int(s.get("height") or 0)
                 for s in streams if s.get("codec_type") == "video"), default=0)
    duration = float((data.get("format") or {}).get("duration") or 0)
    rate = float((audio or {}).get("bit_rate") or 0)
    if audio and not rate and duration > 0:
        # Ogg Opus and Matroska name no bitrate for the stream, and the file's
        # own counts the cover: add up the audio packets instead.
        try:
            res = subprocess.run(["ffprobe", "-v", "error", "-select_streams", str(audio["index"]),
                                  "-show_entries", "packet=size", "-of", "csv=p=0", path],
                                 capture_output=True, text=True, timeout=300)
            rate = sum(int(x) for x in res.stdout.split() if x.strip().isdigit()) * 8 / duration
        except (OSError, subprocess.TimeoutExpired):
            rate = 0.0
    return {"kbps": round(rate / 1000, 1), "cover": cover}


def better(qa, qb):
    """1 when a is the better file, -1 when b is, 0 for a tie."""
    ra, rb = qa["kbps"], qb["kbps"]
    if max(ra, rb) > 0 and abs(ra - rb) > BITRATE_TIE * max(ra, rb):
        return 1 if ra > rb else -1
    if qa["cover"] != qb["cover"]:
        return 1 if qa["cover"] > qb["cover"] else -1
    return 0


MATROSKA_EXTS = (".mka", ".mkv", ".webm", ".weba")


def _home(song):
    """Lower is the better file to keep when bitrate and cover tie: the song
    store's own (youtube_music/<Artist>/), then one whose format takes tags
    and covers in place - a Matroska file needs a remux for either, and the
    song covers job cannot write it at all."""
    path = song.get("path") or ""
    parts = path.split("/")
    return (0 if parts[0] == MUSIC_DIR and len(parts) == 3 else 1,
            1 if os.path.splitext(path)[1].lower() in MATROSKA_EXTS else 0)


def choose(pair, songs, qualities):
    """(kept song index, dropped song index, title, artist) for a pair that is
    one recording. qualities: {song index: quality()}."""
    i, j = pair_songs(pair)
    cmp = better(qualities[i], qualities[j]) or (
        (_home(songs[j]) > _home(songs[i])) - (_home(songs[j]) < _home(songs[i])))
    if pair["kind"] == "artist":
        keep, drop = (i, j) if cmp > 0 else (j, i)        # j has the clean title: kept on a full tie
        title = display_title(songs[j]["title"])
        for front, rest in prefixed(songs[i]["title"]):
            title = _fuller(title, display_title(rest))
        return keep, drop, title, songs[j]["artist"]
    if cmp == 0:
        cmp = 1 if (songs[i].get("added") or 0) <= (songs[j].get("added") or 0) else -1   # the older stays
    keep, drop = (i, j) if cmp > 0 else (j, i)
    artist = songs[keep]["artist"]
    # One title, two credits: the fuller one ("Geoxor & Canonblade" over "Geoxor").
    if artist_parts(songs[drop]["artist"]) > artist_parts(artist):
        artist = songs[drop]["artist"]
    title = _fuller(display_title(songs[keep]["title"]), display_title(songs[drop]["title"]))
    return keep, drop, title, artist


# ---- writing ------------------------------------------------------------------

def retag(path, title, artist):
    """Writes title and artist into the file IN PLACE - its other names are
    hard links, and a new file would leave them behind. False when the format
    takes no tags."""
    ext = os.path.splitext(path)[1].lower()
    if ext not in (".mka", ".mkv", ".webm", ".weba", ".wav"):
        try:
            import mutagen
            audio = mutagen.File(path, easy=True)
            if audio is not None:
                if audio.tags is None:
                    audio.add_tags()
                audio["title"] = [title]
                audio["artist"] = [artist]
                audio.save()
                return True
        except Exception:
            pass
        if ext == ".aac":
            return False
    return _retag_ffmpeg(path, title, artist)


def _own_tags(path):
    """(title, artist) as the file's own tags say them, the artist as the
    catalogue shows it (no " - Topic"); None for what they do not say."""
    import music_library
    info = music_library.probe(path)
    return (info.get("title") or None, music_library.clean_artist(info.get("artist") or "") or None)


def _retag_ffmpeg(path, title, artist):
    folder, name = os.path.split(path)
    stem, ext = os.path.splitext(name)
    tmp = os.path.join(folder, f".{stem}.retag{ext}")       # a dot name: no listing shows it
    try:
        res = subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-y", "-i", path, "-map", "0", "-c", "copy",
                              "-map_metadata", "0", "-metadata", f"title={title}", "-metadata", f"artist={artist}",
                              tmp], capture_output=True, timeout=900)
        if res.returncode != 0 or os.path.getsize(tmp) < 1024:
            return False
        with open(tmp, "rb") as src, open(path, "r+b") as dst:
            shutil.copyfileobj(src, dst, 1 << 20)
            dst.truncate()
        return True
    except (OSError, subprocess.TimeoutExpired):
        return False
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass


def _adopt(keep_full, names, kst, tags=None):
    """A kept file another user owns (Jellyfin's) cannot keep its date through
    an edit - only a file's owner may set its times (2026-09-28: the first
    live round retagged a jellyfin-owned .mka and died on os.utime, the song
    dated "just added") - and may refuse a new hard link
    (fs.protected_hardlinks). It is replaced by a copy of ours - retagged when
    `tags` is (title, artist) - with its date and mode, under every one of its
    names (`names`: full paths, all of them; kst: its stat). The error, or None."""
    if len(names) != kst.st_nlink:
        return "it has names outside the library"
    if not kst.st_mode & stat.S_IROTH:
        return "a copy of ours could not be read by its owner"
    folder, name = os.path.split(keep_full)
    stem, ext = os.path.splitext(name)
    tmp = os.path.join(folder, f".{stem}.adopt{ext}")
    extra = []
    try:
        shutil.copyfile(keep_full, tmp)
        if tags and not retag(tmp, *tags):
            return "its format takes no title"
        os.chmod(tmp, stat.S_IMODE(kst.st_mode))
        os.utime(tmp, ns=(kst.st_atime_ns, kst.st_mtime_ns))
        for n, path in enumerate(names):
            st = os.stat(path)
            if (st.st_dev, st.st_ino, st.st_mtime_ns) != (kst.st_dev, kst.st_ino, kst.st_mtime_ns):
                return "changed while it was copied"
            if n:
                extra.append(os.path.join(os.path.dirname(path), f".{stem}.adopt{n}{ext}"))
                os.link(tmp, extra[-1])
        # Every name is a link of the copy; now each replaces its name, the
        # first last so an error before it leaves the original whole.
        for path, link in zip(names[1:], extra):
            os.replace(link, path)
        os.replace(tmp, names[0])
        return None
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"its copy failed: {exc}"
    finally:
        for path in [tmp] + extra:
            try:
                os.remove(path)
            except OSError:
                pass


def stat_key(st):
    return "%d:%d:%d:%d" % (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns)


def pair_key(st_a, st_b):
    return "|".join(sorted((stat_key(st_a), stat_key(st_b))))


class Decisions:
    """song_merges.json: what each pair of file versions sounded like, and
    the names a merge renamed ({"root:old path": "root:new path"})."""

    def __init__(self, path):
        self.path = path
        self.lock = threading.Lock()
        self.data = {"pairs": {}, "renamed": {}}
        try:
            with open(path, encoding="utf-8") as fh:
                loaded = json.load(fh)
            if isinstance(loaded, dict):
                self.data["pairs"] = loaded.get("pairs") or {}
                self.data["renamed"] = loaded.get("renamed") or {}
        except (OSError, ValueError):
            pass

    def get(self, key):
        with self.lock:
            return self.data["pairs"].get(key)

    def put(self, key, value):
        with self.lock:
            self.data["pairs"][key] = value

    def prune(self, live):
        with self.lock:
            for k in [k for k in self.data["pairs"] if k not in live]:
                del self.data["pairs"][k]

    def renamed(self):
        with self.lock:
            return dict(self.data["renamed"])

    def rename(self, old, new):
        with self.lock:
            # A name renamed before and renamed again: the oldest ref follows.
            for k, v in list(self.data["renamed"].items()):
                if v == old:
                    self.data["renamed"][k] = new
            self.data["renamed"][old] = new

    def unrename(self, old, new):
        """`new` is `old` again (_tidy_numbered): what followed `old` to `new`
        comes back, and a ref caught holding `new` meanwhile finds it."""
        with self.lock:
            for k, v in list(self.data["renamed"].items()):
                if v == new:
                    self.data["renamed"][k] = old
            self.data["renamed"].pop(old, None)
            self.data["renamed"][new] = old

    def save(self):
        with self.lock:
            snapshot = json.dumps(self.data, ensure_ascii=False, separators=(",", ":"))
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(snapshot)
        os.replace(tmp, self.path)


def load_renamed(path):
    """{(root, old path): (root, new path)} from a decisions file, for the
    catalogue's aliases."""
    out = {}
    for old, new in Decisions(path).renamed().items():
        if ":" in old and ":" in new:
            out[tuple(old.split(":", 1))] = tuple(new.split(":", 1))
    return out


class _Locks:
    """The song store's per-artist locks (youtube_music/<Artist>.lock), which
    the extractor holds while it writes a song, for every artist folder a
    merge touches - taken in one order, so two holders never wait on each
    other."""

    def __init__(self, downloads, names):
        store = os.path.join(downloads, MUSIC_DIR)
        folders = set()
        for full in names:
            rel = os.path.relpath(full, store).replace(os.sep, "/")
            if not rel.startswith("..") and "/" in rel:
                folders.add(rel.split("/")[0])
        self.paths = [os.path.join(store, f + ".lock") for f in sorted(folders)]
        self.files = []

    def __enter__(self):
        for p in self.paths:
            fh = open(p, "a+")
            fcntl.flock(fh, fcntl.LOCK_EX)
            self.files.append(fh)
        return self

    def __exit__(self, *exc):
        for fh in reversed(self.files):
            fcntl.flock(fh, fcntl.LOCK_UN)
            fh.close()
        self.files = []


def _free_name(path, keep_full):
    """`path`, or "<stem> (2)<ext>"... when another file holds it. A name that
    already is the kept file counts as free (there is nothing to link)."""
    stem, ext = os.path.splitext(path)
    cand, n = path, 2
    while os.path.lexists(cand) and not os.path.samefile(cand, keep_full):
        cand, n = f"{stem} ({n}){ext}", n + 1
    return cand


def merge(keep, drop, title, artist, roots, downloads, batch, shared=frozenset(), expect=None):
    """Makes the names of `drop` (a catalogue song) names of `keep`, which
    gets `title` and `artist`. roots: {root id: path}; batch: the trash batch
    the dropped file goes into, in its own root; shared: {(root, rel)} names a
    share link uses, kept exactly as they are; expect: {"keep", "drop"} stat
    keys from when the pair was listened to - a file changed since is left for
    the next round.

    Returns {"retagged", "before" (the kept file's stat before), "trashed":
    (root, rel, size, path in the batch) or None, "moves": {(root, old):
    (root, new)}, "names": the dropped song's names now, "error"}."""
    out = {"retagged": False, "before": None, "trashed": None, "moves": {}, "names": [], "error": None}
    if keep["root"] not in roots:
        out["error"] = "its root is gone"
        return out
    keep_full = os.path.join(roots[keep["root"]], keep["path"])
    drop_names = [(r, p, os.path.join(roots[r], p)) for r, p in drop["names"] if r in roots]
    try:
        kst = os.stat(keep_full)
        dsts = [os.stat(f) for _r, _p, f in drop_names]
    except OSError as exc:
        out["error"] = f"gone: {exc}"
        return out
    if not dsts or len({(s.st_dev, s.st_ino) for s in dsts}) != 1:
        out["error"] = "the dropped song is no longer one file"
        return out
    if (dsts[0].st_dev, dsts[0].st_ino) == (kst.st_dev, kst.st_ino):
        out["error"] = "already one file"
        return out
    if expect and (expect.get("keep") != stat_key(kst) or expect.get("drop") != stat_key(dsts[0])):
        out["error"] = "changed since it was listened to"
        return out
    if any(s.st_dev != kst.st_dev for s in dsts):
        out["error"] = "on another disk: a hard link cannot join them"
        return out
    out["before"] = kst

    with _Locks(downloads, [keep_full] + [f for _r, _p, f in drop_names]):
        # 1. The kept file takes the clean title, keeping its own date. Its
        # OWN tags decide, not what the catalogue showed: an untagged file's
        # title came from its name, and which name the song plays from
        # changes with the merge.
        wanted = _own_tags(keep_full) != (title, artist)
        if kst.st_uid != os.geteuid() and (wanted or not os.access(keep_full, os.R_OK | os.W_OK)):
            # Another user's file: edited, or linked, as a copy of ours (_adopt).
            keep_names = [os.path.join(roots[r], p) for r, p in keep.get("names") or () if r in roots]
            err = _adopt(keep_full, keep_names or [keep_full], kst, (title, artist) if wanted else None)
            if err:
                out["error"] = err
                return out
            out["retagged"] = wanted
        elif wanted:
            if not retag(keep_full, title, artist):
                out["error"] = "its format takes no title"
                return out
            out["retagged"] = True
            try:
                os.utime(keep_full, ns=(kst.st_atime_ns, kst.st_mtime_ns))
            except OSError as exc:
                out["error"] = f"retitled, but its date could not be kept: {exc}"
                return out
        keep_ext = os.path.splitext(keep_full)[1]
        # 2. The dropped file to the trash under its first name; every name it
        # had becomes a name of the kept file.
        grave = None
        base_of = lambda r: os.path.join(roots[r], TRASH_DIR_NAME, batch)
        for n, (r, rel, full) in enumerate(drop_names):
            try:
                if n == 0:
                    grave = _free_name(os.path.join(base_of(r), rel), keep_full)
                    os.makedirs(os.path.dirname(grave), exist_ok=True)
                    os.rename(full, grave)
                    out["trashed"] = (r, rel, dsts[0].st_size,
                                      os.path.relpath(grave, base_of(r)).replace(os.sep, "/"))
                else:
                    os.unlink(full)                   # the bytes stay in the trash
            except OSError as exc:
                out["error"] = f"{rel}: {exc}"
                break
            # Picked once the dropped file has left the name: picked before, a
            # dropped file of the kept file's own extension still stood there,
            # and 62 names became "<name> (2)" (2026-09-28, _tidy_numbered).
            if (r, rel) in shared:
                target = full                         # a share link's URL names exactly this
            else:
                target = _free_name(os.path.splitext(full)[0] + keep_ext, keep_full)
            try:
                if not os.path.lexists(target):
                    os.link(keep_full, target)
            except OSError as exc:
                try:                                  # put the name back rather than lose it
                    if n == 0:
                        os.rename(grave, full)
                        out["trashed"] = None
                    else:
                        os.link(grave, full)
                except OSError:
                    pass
                out["error"] = f"{rel}: {exc}"
                break
            new_rel = os.path.relpath(target, roots[r]).replace(os.sep, "/")
            out["names"].append((r, new_rel))
            if new_rel != rel:
                out["moves"][(r, rel)] = (r, new_rel)
    return out


_NUMBERED_RE = re.compile(r"^(.*) \((\d+)\)(\.[^./]+)$")


def _tidy_numbered(decisions, roots, default_root, downloads, shared, record_song, on_merged, log):
    """The names a merge made "<name> (2)" only because the dropped file still
    stood under its own name when the new one was picked (merge(), fixed
    2026-09-28 after the first live rounds: 62 names, playlists' included):
    each goes back to its own name where that is free - the songs index, the
    favourites (on_merged) and the renamed names along. A share link's name
    stays. Returns how many."""
    tidied = 0
    index_path = os.path.join(downloads, MUSIC_DIR, ".songs.json")
    for old, new in decisions.renamed().items():
        r, _, rel_old = old.partition(":")
        r_new, _, rel_new = new.partition(":")
        m = _NUMBERED_RE.match(rel_new)
        if r != r_new or r not in roots or not m or m.group(1) + m.group(3) != rel_old or (r, rel_new) in shared:
            continue
        src, dst = os.path.join(roots[r], rel_new), os.path.join(roots[r], rel_old)
        with _Locks(downloads, [src]):
            if not os.path.isfile(src) or os.path.lexists(dst):
                continue
            try:
                os.link(src, dst)                     # never over a name that appeared meanwhile
                os.unlink(src)
            except OSError as exc:
                log(f"not tidied: {rel_new}: {exc}")
                continue
        decisions.unrename(old, new)
        tidied += 1
        if r == default_root and record_song:
            try:
                with open(index_path, encoding="utf-8") as fh:
                    index = json.load(fh) or {}
            except (OSError, ValueError):
                index = {}
            for vid, rel in index.items():
                if rel == rel_new:
                    record_song(downloads, vid, dst)
        if on_merged:
            on_merged({"retagged": False, "before": None, "trashed": None, "names": [], "error": None,
                       "moves": {(r, rel_new): (r, rel_old)}}, {"keep": [r, rel_old]})
    if tidied:
        log(f"{tidied} names given back their own name (no longer \"<name> (2)\")")
    return tidied


def best_store_name(names, default_root):
    """The songs index's path for a song: its name under youtube_music/ in the
    downloads root, <Artist>/<file> first, then the shortest; None when it has
    none there."""
    store = [p for r, p in names if r == default_root and p.split("/")[0] == MUSIC_DIR]
    if not store:
        return None
    return min(store, key=lambda p: (len(p.split("/")) != 3, len(p), p))


def write_manifest(folder, batch, items, notes):
    """The trash page's shape (items are plain paths, deleted_at dates the
    batch); the detail per file goes under notes. Rewritten after every merge,
    so a batch is described even when a round stops half way."""
    os.makedirs(folder, exist_ok=True)
    tmp = os.path.join(folder, "_manifest.json.tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump({"batch": batch, "mode": "release", "deleted_at": time.time(),
                   "deleted_by": "media-dl song merge", "items": list(items),
                   "reason": "the same song held twice: the better file stays, under the clean title",
                   "notes": list(notes)}, fh, ensure_ascii=False, indent=1)
    os.replace(tmp, os.path.join(folder, "_manifest.json"))


def new_batch():
    # The trash page's restore and empty refuse anything but <date>-<time>-<6 hex>.
    return time.strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(3)


def run(songs, roots, default_root, downloads, store_path, apply=True, shared=frozenset(), busy=None,
        record_song=None, on_merged=None, log=None):
    """One round over a catalogue's songs: every candidate pair not decided
    before is listened to, and each pair that is one recording merged (when
    `apply`). A song merged this round is not looked at again until the next
    catalogue; busy() true stops the round (a download started). record_song:
    youtube_music_extractor.record_song; on_merged(result, entry): what the
    server does around a merge (favourites, the AI index, cover cache).

    Returns {"candidates", "listened", "same", "different", "undecodable",
    "merged", "failed" (tried this round), "held" (failed before, not tried again), "stopped",
    "batch", "pairs": [entry]}."""
    decisions = Decisions(store_path)
    try:
        tidied = _tidy_numbered(decisions, roots, default_root, downloads, shared, record_song, on_merged,
                                log or (lambda msg: None)) if apply else 0
        if tidied:
            # The catalogue in hand still names the old names: the rebuild
            # the server starts for this runs the round.
            return {"candidates": 0, "listened": 0, "same": 0, "different": 0, "undecodable": 0, "merged": 0,
                    "failed": 0, "held": 0, "stopped": "names were tidied, the catalogue is built again", "batch": None,
                    "pairs": [], "tidied": tidied}
        return _round(decisions, songs, roots, default_root, downloads, apply, shared, busy,
                      record_song, on_merged, log)
    finally:
        decisions.save()        # a round that dies keeps its verdicts and the names it renamed


def _round(decisions, songs, roots, default_root, downloads, apply, shared, busy, record_song, on_merged, log):
    log = log or (lambda msg: None)
    pairs = sorted(candidates(songs), key=lambda p: p["kind"] != "artist")
    report = {"candidates": len(pairs), "listened": 0, "same": 0, "different": 0, "undecodable": 0,
              "merged": 0, "failed": 0, "held": 0, "stopped": None, "batch": None, "pairs": []}
    live, touched = set(), set()
    trashed = collections.defaultdict(lambda: ([], []))       # root -> (items, notes)
    batch = new_batch()

    def full(s):
        return os.path.join(roots[s["root"]], s["path"])

    for pair in pairs:
        if busy and busy():
            report["stopped"] = "a download started"
            break
        i, j = pair_songs(pair)
        a, b = songs[i], songs[j]
        if i in touched or j in touched or a["root"] not in roots or b["root"] not in roots:
            continue
        try:
            sa, sb = os.stat(full(a)), os.stat(full(b))
        except OSError:
            continue
        if (sa.st_dev, sa.st_ino) == (sb.st_dev, sb.st_ino):
            continue
        key = pair_key(sa, sb)
        live.add(key)
        verdict = decisions.get(key)
        if verdict is None:
            verdict = same(full(a), full(b), a.get("duration"), b.get("duration"))
            report["listened"] += 1
            verdict = dict(verdict or {"same": None}, at=round(time.time()), titles=[
                f"{a['artist']} / {a['title']}", f"{b['artist']} / {b['title']}"])
            decisions.put(key, verdict)
        if verdict.get("same") is None:
            report["undecodable"] += 1
            continue
        if not verdict["same"]:
            report["different"] += 1
            continue
        # Tried before, and nothing has changed since - the link counts neither:
        # "it has names outside the library" (a name in a trash batch) clears
        # when that batch is emptied, which no stat key sees. (One stored
        # without the counts is tried once more, and stored with them.)
        if verdict.get("failed") and verdict.get("links") == [sa.st_nlink, sb.st_nlink]:
            report["held"] += 1                       # not tried: a round that only holds is quiet
            continue
        report["same"] += 1
        q = {i: quality(full(a)), j: quality(full(b))}
        keep, drop, title, artist = choose(pair, songs, q)
        k, d = songs[keep], songs[drop]
        entry = {"kind": pair["kind"], "keep": [k["root"], k["path"]], "drop": [d["root"], d["path"]],
                 "keep_was": [k["artist"], k["title"]], "drop_was": [d["artist"], d["title"]],
                 "title": title, "artist": artist, "keep_quality": q[keep], "drop_quality": q[drop],
                 "loudness": verdict.get("loudness"), "attacks": verdict.get("attacks"),
                 "apart": verdict.get("apart")}
        report["pairs"].append(entry)
        if not apply:
            continue
        st_k, st_d = (sa, sb) if keep == i else (sb, sa)
        try:
            res = merge(k, d, title, artist, roots, downloads, batch, shared,
                        expect={"keep": stat_key(st_k), "drop": stat_key(st_d)})
        except Exception as exc:              # one pair must not end the round
            res = {"retagged": False, "before": None, "trashed": None, "moves": {}, "names": [],
                   "error": f"failed: {exc!r}"}
        entry["result"] = {"retagged": res["retagged"], "error": res["error"],
                           "moves": [[list(o), list(n)] for o, n in res["moves"].items()]}
        if res["trashed"]:
            r, rel, size, in_batch = res["trashed"]
            items, notes = trashed[r]
            items.append(in_batch)
            notes.append({"path": in_batch, "size": size,
                          "why": f"the same recording as {k['path']} (loudness {verdict.get('loudness')}, "
                                 f"attacks {verdict.get('attacks')}), now a name of it: "
                                 f"{d['artist']} - {d['title']} ({q[drop]['kbps']} kbit/s) -> "
                                 f"{artist} - {title} ({q[keep]['kbps']} kbit/s)"})
            write_manifest(os.path.join(roots[r], TRASH_DIR_NAME, batch), batch, items, notes)
            report["batch"] = batch
        if res["error"]:
            report["failed"] += 1
            log(f"not merged: {d['path']} -> {k['path']}: {res['error']}")
            if not res["trashed"]:
                if not res["error"].startswith(("gone", "changed")):
                    decisions.put(key, dict(verdict, failed=res["error"], links=[sa.st_nlink, sb.st_nlink]))
                continue
        touched.update((keep, drop))
        report["merged"] += 1
        for (r, old), (_r, new) in res["moves"].items():
            decisions.rename(f"{r}:{old}", f"{r}:{new}")
        names = [tuple(n) for n in k.get("names") or [(k["root"], k["path"])]] + res["names"]
        home = best_store_name(names, default_root)
        if record_song and home:
            for vid in {k.get("video_id"), d.get("video_id")} - {None, ""}:
                record_song(downloads, vid, os.path.join(downloads, home))
        log(f"merged: {d['artist']} - {d['title']} ({q[drop]['kbps']} kbit/s) into "
            f"{artist} - {title} ({q[keep]['kbps']} kbit/s{', retitled' if res['retagged'] else ''})")
        if on_merged:
            on_merged(res, entry)
    if not report["stopped"]:
        decisions.prune(live)
    return report
