"""The music library behind music.example.org (user, 2026-09-22: "a new web
page, music.example.org, served by the media-dl server, that works mostly like
YouTube Music's interface": playlists, search, re-downloading a playlist,
channel view).

Pure library code, no Flask: server.py hands it the audio files of the roots
and serves what it builds.

THE CATALOGUE. One entry per SONG, and a song is a file: the library stores a
song once and hard-links it into every playlist that holds it (vault KSF-044),
so names are grouped by (device, inode). Each song names its playback file -
the song store's own name under youtube_music/<Artist>/ when it has one - and
every folder it appears in.

  songs      title, artist, album, duration, video id, cover, genre, added
  playlists  folders holding songs: youtube_music/<Owner>/<Playlist>/ in the
             downloads root, and every music folder of the other roots (the
             Jellyfin library's). Ordered as YouTube Music has them when the
             playlist's source is known (see SourceRegistry), else by when
             each song was added - which follows the playlist, because yt-dlp
             downloads a playlist in order.
  channels   youtube_music/<Channel>/: the songs stored under that channel
             (single videos, and the songs whose home is that artist) and the
             playlists in its subfolders - "channel view (playlists and
             directly downloaded titles)".
  albums     songs grouped by album tag and artist; a "single" whose album is
             its own title is not an album.

RADIOS (user, 2026-09-22: YouTube style radios "using the same logic as the
autoplay of the 3D genre map", and autoplay after a song or list ends). Each
song carries its place on the genre map (p3, the 3D layout genre_map.py made
from the Discogs-EffNet embeddings, joined per request - see for_viewer), and
the page walks from song to the nearest one the radio has not played. What a
listener played, their radios and the autoplay switch live per account in
Listening.

Tags are read once per file version and kept in a JSON cache keyed by
device:inode:size:mtime, so a rebuild after a download reads only what is new.
mutagen reads opus/m4a/mp3/ogg/flac; Matroska (.mka) has no mutagen support and
goes through ffprobe. What a file does not say is taken from where it lives:
the title from its name (without " [videoid]"), the artist from its channel
folder.
"""
from __future__ import annotations

import functools
import hashlib
import json
import os
import re
import subprocess
import threading
import time

AUDIO_EXTS = (".opus", ".m4a", ".mka", ".mp3", ".ogg", ".oga", ".flac", ".wav", ".aac", ".weba")
VIDEO_EXTS = (".mp4", ".webm", ".mkv", ".mov", ".m4v", ".avi")
PROBE_VERSION = 2          # 2: a Matroska cover can be a plain mjpeg stream
MUSIC_DIR = "youtube_music"
NAME_ID_RE = re.compile(r"\s*\[([A-Za-z0-9_-]{11})\]")
URL_ID_RE = re.compile(r"(?:[?&]v=|youtu\.be/)([A-Za-z0-9_-]{11})")
TOPIC_RE = re.compile(r"\s*-\s*Topic$", re.I)


def is_audio(name):
    return name.lower().endswith(AUDIO_EXTS)


def is_video(name):
    return name.lower().endswith(VIDEO_EXTS)


# ---- the music video of a song ---------------------------------------------
# A song and its video are the same upload, so the video id in the file name
# says which video belongs to which song. Most songs do not carry it in their
# name, though - only in their tags - and a whole class of them carries it
# nowhere: the audio pulled out of a visualiser channel's videos sits in
# <root>/animated_music_videos/audio/<Channel>/<name>.mka beside the video at
# .../videos/<Channel>/<name>.webm, same name, no id anywhere. So the name is
# the second way in, and it is only trusted when it is long enough to mean
# something (an "Intro.mka" would find a stranger's "Intro.mp4").
NAME_MATCH_MIN = 8


def video_index(rows):
    """{"by_id": {video id: [(root, rel)]}, "by_name": {stem: [(root, rel)]}}
    from the path index rows of every video file."""
    by_id, by_name = {}, {}
    for root_id, rel in rows:
        stem = os.path.splitext(os.path.basename(rel))[0]
        m = NAME_ID_RE.search(stem)
        if m:
            by_id.setdefault(m.group(1), []).append((root_id, rel))
        if len(stem) >= NAME_MATCH_MIN:
            by_name.setdefault(stem, []).append((root_id, rel))
    return {"by_id": by_id, "by_name": by_name}


# The same video is often on disk more than once: an interpolated 4K120 copy
# beside the 4K60 original, the same upload filed under two folders or two
# roots. When every copy is as long as the others - the same cut, to within
# VIDEO_SAME_LENGTH_S - the Video pill plays the best of them: the most
# pixels, then the most frames a second (user, 2026-09-23: "If the video
# length of all options is identical, it should serve the highest resolution
# and framerate variant available"). Measured on prod: of 1120 videos held
# more than once, the copies of 1116 are within 0.1 s of each other and the
# other 4 differ by more than 2 s - different cuts, and there the plain
# original stays first. So does it while any copy is still unmeasured: "all
# identical" cannot be told then.
VIDEO_SAME_LENGTH_S = 0.5


def _rank_videos(candidates, song_root, video_dims=None):
    """Every copy of a song's video, best first. Without measurements - or
    when the copies are not all the same length - the song's own root first,
    then the shortest path, which is the plain original rather than anything
    derived from it. video_dims(root, rel) -> {"width", "height", "fps",
    "duration"} or None: media-dl's dimensions store. A list, not one pick:
    the best copy may sit in a root a viewer cannot see, and that viewer is
    then given the best one they can (for_viewer)."""
    if not candidates:
        return []
    plain = sorted(candidates, key=lambda c: (c[0] != song_root, len(c[1]), c[1]))
    if len(plain) < 2 or not video_dims:
        return plain
    dims = [video_dims(r, p) or {} for r, p in plain]
    lengths = [d.get("duration") for d in dims]
    if not all(lengths) or max(lengths) - min(lengths) > VIDEO_SAME_LENGTH_S:
        return plain

    def better(i):
        d = dims[i]
        return (-(d.get("width") or 0) * (d.get("height") or 0), -(d.get("fps") or 0), i)
    return [plain[i] for i in sorted(range(len(plain)), key=better)]


def short_id(*parts):
    return hashlib.sha1("\0".join(parts).encode("utf-8")).hexdigest()[:12]


def clean_artist(name):
    name = (name or "").strip()
    return TOPIC_RE.sub("", name).strip() or name


# ---- tags -----------------------------------------------------------------

def _first(tags, *keys):
    """The first non-empty text among `keys`: easy tags are lists of strings,
    ID3 frames carry a .text list, MP4 atoms are lists."""
    for key in keys:
        try:
            value = tags.get(key)
        except Exception:
            value = None
        if not value:
            continue
        item = value[0] if isinstance(value, list) else value
        text = getattr(item, "text", item)
        if isinstance(text, list):
            text = text[0] if text else ""
        text = str(text).strip()
        if text:
            return text
    return None


def _probe_mutagen(path):
    import mutagen
    audio = mutagen.File(path)
    if audio is None:
        return None
    out = {"duration": round(float(getattr(audio.info, "length", 0) or 0), 2)}
    tags = audio.tags or {}
    easy = None
    try:
        easy = mutagen.File(path, easy=True)
    except Exception:
        easy = None
    et = (easy.tags if easy is not None and easy.tags else {}) or {}
    out["title"] = _first(et, "title") or _first(tags, "\xa9nam", "TIT2", "title")
    out["artist"] = _first(et, "artist") or _first(tags, "\xa9ART", "TPE1", "artist")
    out["album"] = _first(et, "album") or _first(tags, "\xa9alb", "TALB", "album")
    out["albumartist"] = _first(et, "albumartist") or _first(tags, "aART", "TPE2", "albumartist")
    out["date"] = _first(et, "date") or _first(tags, "\xa9day", "TDRC", "date")
    for key in ("purl", "PURL", "comment", "\xa9cmt", "WOAS", "description", "COMM::eng"):
        text = _first(tags, key)
        if text:
            m = URL_ID_RE.search(text)
            if m:
                out["video_id"] = m.group(1)
                break
    cover = False
    try:
        if "covr" in tags or "metadata_block_picture" in tags:
            cover = True
        elif any(str(k).startswith("APIC") for k in tags.keys()):
            cover = True
        elif getattr(audio, "pictures", None):
            cover = True
    except Exception:
        pass
    out["cover"] = cover
    return out


def _probe_ffprobe(path):
    try:
        res = subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                              "format=duration:format_tags:stream=codec_type,codec_name,disposition",
                              "-of", "json", path], capture_output=True, text=True, timeout=30)
        data = json.loads(res.stdout or "{}")
    except Exception:
        return {}
    fmt = data.get("format") or {}
    tags = {str(k).lower(): v for k, v in (fmt.get("tags") or {}).items()}
    out = {"duration": round(float(fmt.get("duration") or 0), 2),
           "title": tags.get("title"), "artist": tags.get("artist"), "album": tags.get("album"),
           "albumartist": tags.get("album_artist") or tags.get("albumartist"), "date": tags.get("date")}
    for key in ("purl", "comment", "description"):
        m = URL_ID_RE.search(str(tags.get(key) or ""))
        if m:
            out["video_id"] = m.group(1)
            break
    # A Matroska cover is an attachment, an attached-picture stream, or - in
    # every .mka of the Jellyfin library - a plain mjpeg video stream that is
    # not flagged as a picture at all. In an audio file, a still-image codec
    # is the cover.
    streams = data.get("streams") or []
    out["cover"] = any((s.get("disposition") or {}).get("attached_pic") for s in streams) \
        or any(s.get("codec_type") == "attachment" for s in streams) \
        or any(s.get("codec_type") == "video" and s.get("codec_name") in ("mjpeg", "png", "bmp", "webp", "gif")
               for s in streams)
    return out


def probe(path):
    """What the file says about itself: title, artist, album, albumartist,
    date, duration (s), video_id, cover (bool) - any of them possibly None."""
    info = None
    if not path.lower().endswith(".mka"):
        try:
            info = _probe_mutagen(path)
        except Exception:
            info = None
    if info is None:
        info = _probe_ffprobe(path)
    return {k: v for k, v in info.items() if v not in (None, "")}


class TagCache:
    """probe() results by file version, persisted. Thread-safe."""

    def __init__(self, path):
        self.path = path
        self.lock = threading.Lock()
        self.data = {}
        self.dirty = False
        try:
            with open(path, encoding="utf-8") as fh:
                loaded = json.load(fh)
            if isinstance(loaded, dict):
                self.data = loaded
        except (OSError, ValueError):
            pass

    @staticmethod
    def key(st):
        # PROBE_VERSION first: when probe() learns to read something new, every
        # file is read again instead of keeping the old answer for its version.
        return "%d:%d:%d:%d:%d" % (PROBE_VERSION, st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns)

    def get(self, full, st):
        k = self.key(st)
        with self.lock:
            hit = self.data.get(k)
        if hit is not None:
            return hit
        info = probe(full)
        with self.lock:
            self.data[k] = info
            self.dirty = True
        return info

    def prune(self, live_keys):
        with self.lock:
            gone = [k for k in self.data if k not in live_keys]
            for k in gone:
                del self.data[k]
            if gone:
                self.dirty = True

    def save(self):
        with self.lock:
            if not self.dirty:
                return
            snapshot = json.dumps(self.data, ensure_ascii=False, separators=(",", ":"))
            self.dirty = False
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(snapshot)
        os.replace(tmp, self.path)


# ---- where a playlist came from ---------------------------------------------

LIST_ID_RE = re.compile(r"[?&]list=([A-Za-z0-9_-]+)")


def playlist_url_ok(url):
    """A YouTube (Music) playlist or album link: what a sync can download."""
    url = (url or "").strip()
    return bool(re.match(r"^https://(music\.|www\.|m\.)?youtube\.com/", url)) and bool(LIST_ID_RE.search(url))


class SourceRegistry:
    """Which YouTube Music playlist each playlist folder was downloaded from,
    and in which order it held its songs. Written when a playlist is
    downloaded (server.maybe_spawn_youtube_music_extraction), and when someone
    links an older playlist to its URL on the page. Keyed by "root:folder"."""

    def __init__(self, path):
        self.path = path
        self.lock = threading.Lock()
        self.data = None

    def _load(self):
        if self.data is None:
            try:
                with open(self.path, encoding="utf-8") as fh:
                    loaded = json.load(fh)
                self.data = loaded if isinstance(loaded, dict) else {}
            except (OSError, ValueError):
                self.data = {}
        return self.data

    def all(self):
        with self.lock:
            return {k: dict(v) for k, v in self._load().items()}

    def get(self, key):
        with self.lock:
            v = self._load().get(key)
            return dict(v) if v else None

    def put(self, key, **fields):
        with self.lock:
            entry = dict(self._load().get(key) or {})
            entry.update({k: v for k, v in fields.items() if v is not None})
            self._load()[key] = entry
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(self.data, fh, ensure_ascii=False, indent=1)
            os.replace(tmp, self.path)
            return dict(entry)


# ---- your own playlists -------------------------------------------------------

class _JsonStore:
    """A per-account JSON file, read once and rewritten whole on each change."""

    def __init__(self, path):
        self.path = path
        self.lock = threading.Lock()
        self.data = None

    def _load(self):
        if self.data is None:
            try:
                with open(self.path, encoding="utf-8") as fh:
                    loaded = json.load(fh)
                self.data = loaded if isinstance(loaded, dict) else {}
            except (OSError, ValueError):
                self.data = {}
        return self.data

    def _save(self):
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(self.data, fh, ensure_ascii=False, indent=1)
        os.replace(tmp, self.path)


class UserPlaylists(_JsonStore):
    """Playlists made on the music page, per account: {user: {pid: {name,
    created, updated, songs: [ref]}}}. A ref is "v:<video id>" when the song
    has one - it survives the song being renamed or moved - else
    "p:<root>:<path>"."""

    MAX_PLAYLISTS = 500
    MAX_SONGS = 5000

    def of(self, user):
        with self.lock:
            return json.loads(json.dumps(self._load().get(user) or {}))

    def create(self, user, name):
        name = (name or "").strip()[:120] or "New playlist"
        with self.lock:
            mine = self._load().setdefault(user, {})
            if len(mine) >= self.MAX_PLAYLISTS:
                raise ValueError("too many playlists")
            pid = "u" + hashlib.sha1(("%s\0%s\0%f" % (user, name, time.time())).encode()).hexdigest()[:11]
            now = time.time()
            mine[pid] = {"name": name, "created": now, "updated": now, "songs": []}
            self._save()
            return pid, dict(mine[pid])

    def update(self, user, pid, name=None, songs=None, add=None, remove=None):
        with self.lock:
            pl = (self._load().get(user) or {}).get(pid)
            if pl is None:
                return None
            if name is not None:
                pl["name"] = name.strip()[:120] or pl["name"]
            if songs is not None:
                pl["songs"] = [s for s in songs if isinstance(s, str)][:self.MAX_SONGS]
            if add:
                have = set(pl["songs"])
                for s in add:
                    if isinstance(s, str) and s not in have and len(pl["songs"]) < self.MAX_SONGS:
                        pl["songs"].append(s)
                        have.add(s)
            if remove:
                drop = set(remove)
                pl["songs"] = [s for s in pl["songs"] if s not in drop]
            pl["updated"] = time.time()
            self._save()
            return dict(pl)

    def delete(self, user, pid):
        with self.lock:
            mine = self._load().get(user) or {}
            if pid not in mine:
                return False
            del mine[pid]
            if not mine:
                self._load().pop(user, None)
            self._save()
            return True


RADIO_KINDS = ("song", "artist", "genre", "playlist", "album", "auto")
PLAY_SOURCES = ("user", "list", "radio")


class Listening(_JsonStore):
    """What each account listened to, for its radios and its history: {user:
    {"events": [[ref, src, t, ctx], ...], "radios": [{id, kind, key, title,
    played: [ref], started, t}], "autoplay": bool}}.

    src says how a song came to play: "user" - picked by hand (a click on
    that song, or the first song of a list started by hand), "list" - the
    list went on to it, "radio" - a radio or autoplay chose it. A radio's id
    is "<kind>:<key>" (the key: a song ref, an artist, a style, a playlist or
    album id), so starting the same radio again continues past what it has
    already played instead of repeating it.

    ctx says WHERE a song was played from, so the history can say "from Night
    drive" and not only name the song: a short code the page writes and reads
    back ("pl:<id>", "al:<id>", "ar:<artist>", "ge:<style>", "lib", "search",
    "radio:<radio id>"). It is the LAST field because the plays recorded
    before there was a history are three long and stay that way.

    Events are kept far past what the radios need - a history is worth months,
    not hours - so summary(), which travels with every library load, hands out
    only the newest SUMMARY_EVENTS of them; history() pages through the rest."""

    MAX_EVENTS = 5000
    SUMMARY_EVENTS = 300
    MAX_RADIOS = 30
    # A radio may never play the same song twice (user, 2026-09-23), and this
    # list is the only memory of what it has played: a cap that bites is a
    # radio that forgets and repeats itself. Above the whole library (3,413
    # songs), so it cannot bite today; it stays a cap because a list that
    # grows forever would be rewritten with every play. Measured: the biggest
    # radio in the store has 54 plays, the whole file 41 KB.
    MAX_PLAYED = 5000

    def _mine(self, user):
        mine = self._load().setdefault(user, {})
        mine.setdefault("events", [])
        mine.setdefault("radios", [])
        mine.setdefault("autoplay", True)
        return mine

    def summary(self, user):
        """What the page loads with the library: the events, the radios
        without their played lists (those come with radio()), the switch."""
        with self.lock:
            mine = self._load().get(user) or {}
            radios = [{k: v for k, v in r.items() if k != "played"}
                      | {"count": len(r.get("played", [])), "last": (r.get("played") or [None])[-1]}
                      for r in mine.get("radios", [])]
            return {"events": [list(e) for e in mine.get("events", [])[-self.SUMMARY_EVENTS:]],
                    "radios": radios, "autoplay": bool(mine.get("autoplay", True)),
                    "target_lufs": mine.get("target_lufs")}

    def radio(self, user, rid):
        with self.lock:
            for r in (self._load().get(user) or {}).get("radios", []):
                if r.get("id") == rid:
                    return json.loads(json.dumps(r))
        return None

    def history(self, user, before=None, limit=100):
        """A page of the account's plays, newest first - what the history view
        lists. `before`: only plays older than that time, which is how the
        view asks for the page after the one it has."""
        with self.lock:
            events = (self._load().get(user) or {}).get("events", [])
            rows = [list(e) for e in reversed(events) if not before or e[2] < before]
        return {"events": rows[:limit], "more": len(rows) > limit}

    def record(self, user, ref, src, radio=None, now=None, ctx=None):
        """One song started. `radio`: {id, kind, key, title} when a radio or
        autoplay played it - that radio moves to the front of the recent ones
        and remembers the song as played. `ctx`: where it was played from."""
        now = time.time() if now is None else now
        with self.lock:
            mine = self._mine(user)
            events = mine["events"]
            events.append([ref, src, round(now, 1)] + ([ctx] if ctx else []))
            del events[:-self.MAX_EVENTS]
            if radio:
                radios = mine["radios"]
                entry = next((r for r in radios if r.get("id") == radio["id"]), None)
                if entry is None:
                    entry = {"id": radio["id"], "kind": radio["kind"], "key": radio["key"],
                             "title": radio["title"], "played": [], "started": round(now, 1)}
                else:
                    radios.remove(entry)
                    entry["title"] = radio["title"] or entry.get("title")
                radios.insert(0, entry)
                del radios[self.MAX_RADIOS:]
                if ref in entry["played"]:
                    entry["played"].remove(ref)
                entry["played"].append(ref)
                del entry["played"][:-self.MAX_PLAYED]
                entry["t"] = round(now, 1)
            self._save()

    def set_autoplay(self, user, on):
        with self.lock:
            self._mine(user)["autoplay"] = bool(on)
            self._save()

    def set_target_lufs(self, user, value):
        """The loudness this viewer's music page plays everything at; None:
        the library's own (music_loudness.choose_target)."""
        with self.lock:
            mine = self._mine(user)
            if value is None:
                mine.pop("target_lufs", None)
            else:
                mine["target_lufs"] = float(value)
            self._save()


# ---- building the catalogue ------------------------------------------------

def _home_rank(root_id, rel, default_root):
    """Lower is a better playback name: the song store's artist folder first,
    then anything in the downloads root, then the shortest path."""
    parts = rel.split("/")
    in_store = root_id == default_root and parts[0] == MUSIC_DIR
    return (0 if in_store and len(parts) == 3 else 1 if in_store else 2 if root_id == default_root else 3,
            len(rel), rel)


def build(names, tags, default_root, songs_index=None, registry=None, genres=None, root_names=None,
          videos=None, video_dims=None, renamed=None):
    """The catalogue from `names`: [(root_id, rel, full, stat)] of every audio
    file. songs_index: {video id: downloads-relative path} (the song store's
    own index); registry: SourceRegistry.all(); genres: {(root, rel): style};
    root_names: {root id: display name}; renamed: {(root, old): (root, new)},
    the names song_merge.py gave another extension. Returns plain dicts, see
    the module notes."""
    songs_index = songs_index or {}
    videos = videos or {"by_id": {}, "by_name": {}}
    registry = registry or {}
    genres = genres or {}
    root_names = root_names or {}
    id_by_path = {("%s:%s" % (default_root, rel)): vid for vid, rel in songs_index.items()}

    by_file = {}
    for root_id, rel, full, st in names:
        by_file.setdefault((st.st_dev, st.st_ino), []).append((root_id, rel, full, st))

    songs = []
    folder_members = {}            # (root, folder) -> [song index]
    live_keys = set()
    for key, group in by_file.items():
        group.sort(key=lambda g: _home_rank(g[0], g[1], default_root))
        root_id, rel, full, st = group[0]
        live_keys.add(TagCache.key(st))
        info = tags.get(full, st)
        stem = os.path.splitext(os.path.basename(rel))[0]
        parts = rel.split("/")
        vid = info.get("video_id")
        if not vid:
            m = NAME_ID_RE.search(stem)
            vid = m.group(1) if m else None
        if not vid:
            for r, p, _f, _s in group:
                vid = id_by_path.get("%s:%s" % (r, p))
                if vid:
                    break
        channel_folder = parts[1] if root_id == default_root and parts[0] == MUSIC_DIR and len(parts) >= 3 else None
        # What the file does not say, from where it lives and what it is called:
        # its channel folder in the song store, else "Artist - Title" in its
        # name - which is how most untagged files (the Jellyfin library's
        # music-video audio) are named.
        bare = NAME_ID_RE.sub("", stem).strip()
        tag_artist = clean_artist(info.get("artist") or "")
        if tag_artist:
            artist, title = tag_artist, info.get("title") or bare
        elif " - " in bare:
            # The name says who, even inside a channel folder - a visualiser
            # channel (MHM) is not the artist of "MVDNES - Like That".
            name_artist, name_title = bare.split(" - ", 1)
            artist, title = name_artist.strip(), info.get("title") or name_title
        else:
            artist = (channel_folder if channel_folder and len(parts) == 3 else None) or "Unknown artist"
            title = info.get("title") or bare
        title = (title or "").strip() or stem
        genre = None
        for r, p, _f, _s in group:
            genre = genres.get((r, p))
            if genre:
                break
        # Its music video, if the library holds one: by the video id first,
        # by the file's own name second. Every copy, best first.
        video = _rank_videos(videos["by_id"].get(vid or ""), root_id, video_dims)
        if not video:
            for r, p, _f, _s in group:
                video = _rank_videos(videos["by_name"].get(os.path.splitext(os.path.basename(p))[0]),
                                     root_id, video_dims)
                if video:
                    break
        idx = len(songs)
        songs.append({
            "video": list(video[0]) if video else None,
            "videos": [list(v) for v in video],
            "title": title, "artist": artist, "album": info.get("album") or None,
            "albumartist": clean_artist(info.get("albumartist") or "") or None,
            "duration": info.get("duration") or 0, "video_id": vid, "cover": bool(info.get("cover")),
            "root": root_id, "path": rel, "added": st.st_mtime, "genre": genre,
            "names": [(r, p) for r, p, _f, _s in group],
        })
        for r, p, _f, _s in group:
            folder = p.rsplit("/", 1)[0] if "/" in p else ""
            folder_members.setdefault((r, folder), []).append(idx)
    tags.prune(live_keys)

    # Other refs a song answers to (song_merge.py): a merged song's dropped
    # video id, which the songs index points at the kept file, and a name
    # that took the kept file's extension. User playlists, history and radios
    # hold refs, and a merge must not empty them.
    by_name = {}
    for idx, s in enumerate(songs):
        for r, p in s["names"]:
            by_name[(r, p)] = idx
    owned = {song_ref(s) for s in songs}
    extra = [("v:" + vid, (default_root, rel)) for vid, rel in songs_index.items()]
    extra += [("p:%s:%s" % tuple(old), tuple(new)) for old, new in (renamed or {}).items()
              if tuple(old) not in by_name]
    for ref, name in extra:
        idx = by_name.get(tuple(name))
        if idx is not None and ref not in owned:
            songs[idx].setdefault("aliases", []).append(ref)
            owned.add(ref)

    # Channels: youtube_music/<Channel>/ in the downloads root.
    channels = {}
    for (r, folder), members in folder_members.items():
        parts = folder.split("/") if folder else []
        if r == default_root and len(parts) == 2 and parts[0] == MUSIC_DIR:
            ch = channels.setdefault(parts[1], {"name": parts[1], "songs": [], "playlists": []})
            ch["songs"] = members
    # Playlists: every other folder holding songs.
    playlists = []
    for (r, folder), members in folder_members.items():
        parts = folder.split("/") if folder else []
        if not parts or (r == default_root and parts[0] == MUSIC_DIR and len(parts) <= 2):
            continue                                  # the store root, or a channel's own folder
        key = "%s:%s" % (r, folder)
        src = registry.get(key) or {}
        order = src.get("tracks") or []
        members = sorted(set(members), key=lambda i: songs[i]["added"])
        if order:
            pos = {v: n for n, v in enumerate(order)}
            members.sort(key=lambda i: (pos.get(songs[i]["video_id"], len(pos)), songs[i]["added"]))
        in_store = r == default_root and parts[0] == MUSIC_DIR and len(parts) == 3
        # A downloaded playlist is its owner's; any other music folder is its
        # library's, with the path above it to tell same-named folders apart.
        owner = parts[1] if in_store else root_names.get(r, r)
        pid = "f" + short_id(key)
        playlists.append({
            "id": pid, "title": src.get("title") or parts[-1], "owner": owner,
            "channel": parts[1] if in_store else None, "root": r, "folder": folder,
            "source": "youtube" if in_store else "library",
            "library": None if in_store else root_names.get(r, r),
            "where": None if in_store else " / ".join(parts[:-1]),
            "url": src.get("url"), "synced": src.get("synced_at"), "songs": members,
        })
        if in_store:
            channels.setdefault(parts[1], {"name": parts[1], "songs": [], "playlists": []})["playlists"].append(pid)
    playlists.sort(key=lambda p: (p["source"] != "youtube", p["owner"].lower(), p["title"].lower()))

    # Albums: by album tag and artist; a single named after its own title is not one.
    albums = {}
    for i, s in enumerate(songs):
        album = s["album"]
        if not album:
            continue
        who = s["albumartist"] or s["artist"]
        albums.setdefault((album.lower(), who.lower()), {"title": album, "artist": who, "songs": []})["songs"].append(i)
    album_list = []
    for (a, w), al in albums.items():
        if len(al["songs"]) < 2:
            continue                  # a single: its album is itself, and a shelf of those is noise
        al["songs"].sort(key=lambda i: (songs[i]["added"], songs[i]["title"].lower()))
        al["id"] = "a" + short_id(a, w)
        album_list.append(al)
    album_list.sort(key=lambda al: (al["artist"].lower(), al["title"].lower()))
    return {"songs": songs, "playlists": playlists,
            "channels": sorted(channels.values(), key=lambda c: c["name"].lower()),
            "albums": album_list, "built": time.time()}


def song_ref(song):
    """How a user playlist refers to a song: its video id when it has one."""
    return ("v:" + song["video_id"]) if song.get("video_id") else "p:%s:%s" % (song["root"], song["path"])


def position_of(song, positions):
    """The song's place on the genre map, [x, y, z], or 0 when it has none
    yet. The map knows files by path, and may know a song by any of its
    names (a playlist's link as well as its home under the artist)."""
    for r, p in [(song["root"], song["path"])] + [tuple(n) for n in song.get("names", [])]:
        xyz = positions.get((r, p))
        if xyz:
            return [round(float(v), 4) for v in xyz]
    return 0


# ---- the title the music page shows -----------------------------------------

# Characters that draw as an emoji picture by default (Unicode
# Emoji_Presentation, whole pictograph blocks where they are). Text symbols
# stay - "!PVNDEMIK ✖ Galiv", "TRANCE ◉", "♪", "™" - unless a U+FE0F turns them
# into the emoji picture.
_EMOJI_CHARS = ("⌚⌛⏩-⏬⏰⏳◽◾☔☕♈-♓♿⚓"
                "⚡⚪⚫⚽⚾⛄⛅⛎⛔⛪⛲⛳⛵⛺⛽"
                "✅✊✋✨❌❎❓-❕❗➕-➗➰➿"
                "⬛⬜⭐⭕"
                "\U0001f004\U0001f0cf\U0001f18e\U0001f191-\U0001f19a\U0001f1e6-\U0001f1ff\U0001f201\U0001f21a"
                "\U0001f22f\U0001f232-\U0001f236\U0001f238-\U0001f23a\U0001f250\U0001f251"
                "\U0001f300-\U0001f64f\U0001f680-\U0001f6ff\U0001f7e0-\U0001f7ff\U0001f900-\U0001f9ff"
                "\U0001fa70-\U0001faff")
_ONE_EMOJI = "(?:[0-9#*]️?⃣|[%s]|[^\\w\\s]️)" % _EMOJI_CHARS   # keycap first: "#️⃣"
# One emoji with what belongs to it: skin tones (inside the blocks above),
# variation selectors, flag tags, and the ZWJ-joined rest of a family/sequence.
EMOJI_RE = re.compile("%s(?:[︎️\U000e0020-\U000e007f]|‍%s)*" % (_ONE_EMOJI, _ONE_EMOJI))
# A visualiser channel's label after the song: "Like That | Music
# Visualization🖤🎶💎", "Slip and slide (Bershka Mix) Music Visualization".
MUSIC_VIS_RE = re.compile(r"\s*[|｜│]?\s*music(?:al)?\s+(?:video\s+)?visuali[sz](?:ation|er)\b", re.I)
# UNFINISH's: "Here Goes Nothing - UNFINISH EDM Music Visualizer", "Inner Tyrant
# - UNFINISH / EDM Animate Visualizer", "I Just Wanna Love U - EDM Music Video
# Visualizer". `who` must be the song's own artist (shown_title checks).
VISUALIZER_TAIL_RE = re.compile(r"\s+[-–—|｜]\s+(?:(?P<who>[^-–—|｜]+?)\s*[/|｜]?\s+)??(?:EDM\s+)?"
                                r"(?:music\s+video|music|animated?|video)\s+visuali[sz]er\s*$", re.I)
# The song's own artist again after it: "Maniac Candy - UNFINISH" by UNFINISH.
ARTIST_TAIL_RE = re.compile(r"\s+[-–—]\s+([^-–—|｜]+?)\s*$")
# The tail of a waifu2x/VFI transcode's file name ("..._waifu2x_3840x2160_mp4_W2xEX_VFI_mp4").
TRANSCODE_TAIL_RE = re.compile(r"(?:_waifu2x|_W2xEX)[A-Za-z0-9_]*$", re.I)
_EMPTY_BRACKETS_RE = re.compile(r"\s*(?:\(\s*\)|\[\s*\]|【\s*】|「\s*」)")
_LOOSE_END_RE = re.compile(r"(?:\s+[|｜│/~–—-])+$")
_LOOSE_START_RE = re.compile(r"^(?:[|｜│/~–—-]\s+)+")


def _same_name(a, b):
    fa = re.sub(r"\W", "", (a or "").casefold())
    return bool(fa) and fa == re.sub(r"\W", "", (b or "").casefold())


@functools.lru_cache(maxsize=32768)
def shown_title(title, artist=None):
    """A song's title as the music page (and its share pages) show it: without
    a visualiser channel's "Music Visualization"/"... Visualizer" (with the
    song's own artist named inside it), emoji, a transcode's file name tail,
    and the song's own artist repeated at the end. Display only - the
    catalogue, the files and media-dl keep the full name. The full title when
    nothing would be left of it."""
    raw = (title or "").strip()
    t = EMOJI_RE.sub(" ", raw)
    t = TRANSCODE_TAIL_RE.sub("", t.rstrip())
    m = VISUALIZER_TAIL_RE.search(t)
    if m and (m.group("who") is None or _same_name(m.group("who"), artist)):
        t = t[:m.start()]
    t = MUSIC_VIS_RE.sub(" ", t).rstrip()
    m = ARTIST_TAIL_RE.search(t)
    if m and artist and _same_name(m.group(1), artist):
        t = t[:m.start()]
    if t == raw:
        return raw              # nothing removed: the title's own spacing stays
    t = _EMPTY_BRACKETS_RE.sub("", t)
    t = re.sub(r" {2,}", " ", t).strip()
    t = _LOOSE_START_RE.sub("", _LOOSE_END_RE.sub("", t)).strip()
    return t or raw


def for_viewer(catalog, visible_roots, user_playlists=None, can_sync=False, positions=None, listening=None):
    """The catalogue as one viewer may see it, in the compact shape the page
    loads: songs as rows, and every list of songs as row numbers into them.
    Songs in roots the viewer cannot see are left out, and so are the
    playlists and channels left with nothing. positions: {(root, path): [x,
    y, z]} from the genre map; listening: Listening.summary() of the viewer."""
    songs = catalog["songs"]
    positions = positions or {}
    # The gain in dB that plays each file at the common loudness (server.py
    # _music_loudness_plan, music_loudness.py); None: not measured yet.
    loud = catalog.get("loudness") or {}
    gains = loud.get("gains") or {}
    levels = loud.get("levels") or {}

    def gain_of(root, path):
        return (gains.get(root) or {}).get(path)

    def level_of(root, path):
        return (levels.get(root) or {}).get(path)
    # Songs deleted since this catalogue was built (server.py music_song_delete):
    # their files are in the trash, and the next build leaves them out anyway.
    deleted = catalog.get("deleted") or ()
    keep = [i for i, s in enumerate(songs) if s["root"] in visible_roots and (s["root"], s["path"]) not in deleted]
    new_index = {old: new for new, old in enumerate(keep)}
    rows = []
    by_ref = {}
    for old in keep:
        s = songs[old]
        # The best copy this viewer may see (a catalogue from before the
        # copies were kept has only the one).
        video = next((v for v in (s.get("videos") or [s.get("video")])
                      if v and v[0] in visible_roots), None)
        rows.append([shown_title(s["title"], s["artist"]), s["artist"],
                     shown_title(s["album"], s["artist"]) if s["album"] else "",
                     round(s["duration"] or 0),
                     s["root"], s["path"], s["video_id"] or "", s["genre"] or "", int(s["added"]),
                     1 if s["cover"] else 0, position_of(s, positions), video,
                     gain_of(s["root"], s["path"]), gain_of(video[0], video[1]) if video else None,
                     level_of(s["root"], s["path"]), level_of(video[0], video[1]) if video else None])
        by_ref[song_ref(s)] = new_index[old]
        by_ref["p:%s:%s" % (s["root"], s["path"])] = new_index[old]
        for r, p in s["names"]:
            by_ref.setdefault("p:%s:%s" % (r, p), new_index[old])
    # A merged song's other refs (see build): the page resolves saved queues,
    # history and radios with them; its own playlists are resolved here.
    aliases = {}
    for old in keep:
        for ref in songs[old].get("aliases") or ():
            if ref not in by_ref:
                by_ref[ref] = aliases[ref] = new_index[old]

    def remap(members):
        return [new_index[i] for i in members if i in new_index]

    playlists = []
    for p in catalog["playlists"]:
        if p["root"] not in visible_roots:
            continue
        members = remap(p["songs"])
        if members:
            playlists.append({k: v for k, v in p.items() if k != "songs"} | {"songs": members})
    channels = []
    for c in catalog["channels"]:
        members = remap(c["songs"])
        pls = [pid for pid in c["playlists"] if any(pl["id"] == pid for pl in playlists)]
        if members or pls:
            channels.append({"name": c["name"], "songs": members, "playlists": pls})
    albums = []
    for a in catalog["albums"]:
        members = remap(a["songs"])
        if len(members) >= 2:
            albums.append({"id": a["id"], "title": shown_title(a["title"], a["artist"]), "artist": a["artist"],
                           "songs": members})
    mine = []
    for pid, pl in sorted((user_playlists or {}).items(), key=lambda kv: -kv[1].get("updated", 0)):
        members = [by_ref[r] for r in pl.get("songs", []) if r in by_ref]
        mine.append({"id": pid, "title": pl["name"], "songs": members, "refs": pl.get("songs", []),
                     "updated": pl.get("updated"), "source": "user"})
    return {"fields": ["title", "artist", "album", "duration", "root", "path", "video_id", "genre", "added", "cover",
                       "p3", "video", "gain", "video_gain", "level", "video_level"],
            "songs": rows, "playlists": playlists, "channels": channels, "albums": albums,
            "aliases": aliases,
            "mine": mine, "can_sync": bool(can_sync), "built": catalog.get("built"),
            "loudness": {"target": loud.get("target"), "unknown": loud.get("unknown", 0.0)},
            "me": listening or {"events": [], "radios": [], "autoplay": True}}
