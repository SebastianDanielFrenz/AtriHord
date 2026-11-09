"""2D -> 3D conversion for media-dl: the queue, the 3D cache and the dispatcher
that talks to the vision worker's /v1/stereo/* routes (vision/vision_worker.py,
the pipeline itself is vision/stereo/stereo_job.py).

server.py wires this up (settings, routes, two daemon threads); nothing here
imports server.py or Flask - everything the module needs from the app is passed
to Service(...) as a function, which is also how the harness
(tests/server_checks/stereo3d_check.py) drives it against a fake worker.

What it does, in the order a file travels:
  request()    a file (root, path) from "Convert to 3D", the favourites or a VR viewer -> a queue
               item {lane viewing | manual | favorite}. Viewing first (newest first: someone is
               looking at it now), then manual (FIFO), then favourites (newest first).
               Last: the redo lane (3D re-made after video_trim.py cut a file), FIFO.
  step()       one dispatcher tick: poll what the worker runs, pull a finished job into the
               cache, hold a running favourite video for a manual request, or start the next
               item (cache hit -> done; else make room, PUT the source, POST the job).
  maintenance  hourly: the watched clock, orphan/stale entries, eviction to cache_max_gb.

Key = sha256(dev:ino:size:mtime_ns | profile_id)[:32]: a rename keeps the inode, a
re-encode or another setting is a new version. The queue is stereo3d_queue.json (atomic
writes); the cache is <cache_dir>/index.json plus v/<k2>/<key>/{sbs.mp4,depth.mp4,
meta.json} and p/<k2>/<key>/{sbs.jpg,depth.png,meta.json}. The cache folder is NEVER created
here and there is no fallback folder (the thin /data2 pool filled up once): a missing folder
is reported and the dispatcher stays idle.
"""
import hashlib
import json
import os
import re
import shutil
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

VIDEO_EXTS = (".mp4", ".webm", ".mkv", ".mov", ".avi", ".m4v")
# The worker's batch mode reads jpg/jpeg/png/webp/bmp; a .jfif is a JPEG and goes as .jpg.
PICTURE_EXTS = (".jpg", ".jpeg", ".jfif", ".png", ".webp", ".bmp")
LANE_MANUAL, LANE_FAVORITE, LANE_VIEWING = "manual", "favorite", "viewing"
# User, 2026-10-18: "when a video is intro/outro is cut for non-new downloads, re-do the
# interpolation and 3D work." The 3D made for the version before a cut is made again for the cut
# one, behind everything a person asked for AND behind the favourites: a batch of redos must never
# jump ahead of a viewer's or an admin's request.
LANE_REDO = "redo"
URGENT_LANES = (LANE_VIEWING, LANE_MANUAL)    # ahead of the favourites: gating treats them alike
VIEWING_MAX_PER_USER = 3         # a user's queued items in the viewing lane; a newer view demotes the oldest
RESULT_NAMES = {"video": ("sbs.mp4", "depth.mp4", "meta.json"),
                "picture": ("sbs.jpg", "depth.png", "meta.json")}
FILE_NAMES = frozenset(n for names in RESULT_NAMES.values() for n in names)
def default_cache_dir():
    """Read when needed, not at import: server.py loads atrihord.env after importing this module."""
    if os.environ.get("MEDIA_3D_CACHE"):
        return os.environ["MEDIA_3D_CACHE"]
    if os.environ.get("MEDIA_CACHE_DIR"):
        return os.path.join(os.path.expanduser(os.environ["MEDIA_CACHE_DIR"]), "3d")
    return os.path.join(os.path.expanduser("~"), ".cache", "media-dl-3d")
PICTURE_BATCH_MAX = 50
KEY_RE = re.compile(r"^[0-9a-f]{32}$")
ORPHAN_DAYS = 7
PLAY_GAP_S = 600                 # one counted play per key per ten minutes
FAILED_RETRY_S = 1800            # a failed item goes to the end of its lane for this long
BACKOFF_MIN_S, BACKOFF_MAX_S = 30, 600
DISK_LOW_BACKOFF_S = 600
UPLOAD_TIMEOUT_S = 3600
PIN_BY = "permanent folder"      # the "by" of what the permanent-folder scan queued
UPLOAD_FAILS_MAX = 3            # one item's uploads cut off in a row before that counts as a failed attempt
FULL_RETRY_S = 3600             # a favourite waits this long when the cache is full of favourites
# What a conversion is expected to add (SBS + depth + meta), to leave room before it starts.
EST_VIDEO_BYTES = 1 << 30
EST_PICTURE_BYTES = 30 << 20


def log(msg):
    print("[Info] stereo3d: " + msg, flush=True)


# ------------------------------------------------------------------ keys, profiles, files
def kind_of(path):
    ext = os.path.splitext(path or "")[1].lower()
    if ext in VIDEO_EXTS:
        return "video"
    if ext in PICTURE_EXTS:
        return "picture"
    return None


# A file that is already stereo says so in its name. Files carry no stereo metadata here (ffprobe finds none
# on the YouTube VR180 downloads) and comparing the two halves of a frame misses most of them, so only explicit
# tags count: a bare "3D" or "VR" is usually a title ("My 3D Model"), and "360" a 2D panorama.
NATIVE_3D_NAME = re.compile(r"(?<![a-z0-9])(?:vr[ _-]?180|180[ _-]?vr|3d[ _-]?180(?:vr)?|(?:full|half|h)?[ _-]?sbs|3dh|3dv)(?![a-z0-9])", re.I)


def is_native_3d(path):
    return kind_of(path) is not None and bool(NATIVE_3D_NAME.search(os.path.basename(path or "")))


def worker_ext(path):
    """The extension the worker is told: its batch mode knows no .jfif."""
    ext = os.path.splitext(path)[1].lower().lstrip(".")
    return "jpg" if ext == "jfif" else ext


def file_version(full):
    """{dev, ino, size, mtime_ns} of a file, or None when it is not there."""
    try:
        st = os.stat(full)
    except OSError:
        return None
    if not os.path.isfile(full):
        return None
    return {"dev": st.st_dev, "ino": st.st_ino, "size": st.st_size, "mtime_ns": st.st_mtime_ns}


def item_size(item):
    """The file's size when the queue item was keyed (0 for an empty file)."""
    return int((item.get("version") or {}).get("size") or 0)


def item_key(version, profile_id):
    raw = f"{version['dev']}:{version['ino']}:{version['size']}:{version['mtime_ns']}|{profile_id}"
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


def batch_key(keys):
    return hashlib.sha256("|".join(sorted(keys)).encode()).hexdigest()[:32]


def profile_for(kind, cfg, turn=0):
    """(profile sent to the worker, profile id). The profile holds only keys of stereo_job.py's
    DEFAULT_PROFILE (the worker refuses others); the id is part of the cache key, so changing a
    setting that changes the output re-converts on the next request. Pictures are never
    resolution-capped, so the cap is not part of their profile.
    turn: degrees clockwise (90/180/270) that make a video filmed on its side upright (media-dl's
    effective turn, auto-detected or hand-set). It travels as the profile key turn_cw - the driver adds
    it to the container rotation before depth estimation - and is part of the id, so a changed turn
    converts again. 0 adds nothing: every key made before the turn existed stays valid. Pictures
    ignore it (the orientation pass covers videos only)."""
    div, conv = float(cfg["divergence"]), float(cfg["convergence"])
    if kind == "video":
        side = int(cfg["max_eye_long_side"])
        mbps = [float(cfg["mbps_1080"]), float(cfg["mbps_1440"]), float(cfg["mbps_2160"])]
        profile = {"depth_model": "Any_V2_S", "method": "row_flow_v3", "divergence": div, "convergence": conv,
                   "fps": 120, "max_eye_long_side": side,
                   "bitrate_anchors": [[1920 * 1080, mbps[0]], [2560 * 1440, mbps[1]], [3840 * 2160, mbps[2]]]}
        pid = "v1:any_v2_s:rf3:d%g:c%g:f120:l%d:b%s" % (div, conv, side, "-".join("%g" % m for m in mbps))
        if turn in (90, 180, 270):
            profile["turn_cw"] = turn
            pid += ":t%d" % turn
        return profile, pid
    profile = {"picture_model": "Distill_Any_L", "method": "row_flow_v3", "divergence": div,
               "convergence": conv, "picture_jpeg_quality": 95}
    return profile, "p1:distill_l:rf3:d%g:c%g:jpg95" % (div, conv)


def cache_rel(kind, key):
    return "%s/%s/%s" % ("v" if kind == "video" else "p", key[:2], key)


def download_name(entry, name):
    """A file name SKYBOX reads the layout from: <title>_Full_SBS.mp4 for the side by side."""
    title = re.sub(r'[\\/*?:"<>|\x00-\x1f]', "", os.path.splitext(os.path.basename(entry.get("path") or "3d"))[0]).strip() or "3d"
    ext = os.path.splitext(name)[1]
    if name.startswith("sbs."):
        return title + "_Full_SBS" + ext
    if name.startswith("depth."):
        return title + "_depth" + ext
    return title + "_" + name


def eviction_order(entries, is_favourite, now, include_favourites=True, is_pinned=None):
    """Keys in the order they go when the cache is over its cap: orphans (the source has been
    gone or changed for ORPHAN_DAYS - least recently noticed first), then non-favourites,
    then favourites (left out when include_favourites is false), each group least recently
    WATCHED first. Pinned entries (a permanent folder) never go, unless orphaned."""
    orphans, plain, favs = [], [], []
    for key, e in entries.items():
        missing = e.get("missing_since")
        if missing and now - float(missing) >= ORPHAN_DAYS * 86400:
            orphans.append((float(missing), key))
        elif is_pinned is not None and is_pinned(e.get("root"), e.get("path")):
            continue
        elif is_favourite(e.get("root"), e.get("path")):
            favs.append((float(e.get("last_watched") or 0), float(e.get("created") or 0), key))
        else:
            plain.append((float(e.get("last_watched") or 0), float(e.get("created") or 0), key))
    return ([k for _, k in sorted(orphans)] + [t[-1] for t in sorted(plain)]
            + ([t[-1] for t in sorted(favs)] if include_favourites else []))


def _atomic_write_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, separators=(",", ":"))
    os.replace(tmp, path)


def _read_json(path, default):
    try:
        with open(path, encoding="utf-8") as fh:
            value = json.load(fh)
        return default if value is None else value
    except (OSError, ValueError):
        return default


# ------------------------------------------------------------------ queue
class Queue:
    """stereo3d_queue.json: {"items": [...], "evicted": {"root\\npath": {at, cap_gb}}}.
    Done items leave it (the cache index is their record); failed ones stay until retried
    or removed. After a restart an item that was being uploaded is queued again and one that was
    being downloaded is "submitted" again (the worker still holds the finished job)."""

    def __init__(self, path, clock=time.time):
        self.path, self.clock = path, clock
        self.lock = threading.RLock()
        self.items = {}
        self.evicted = {}
        self.seq = 0
        data = _read_json(path, {})
        for it in data.get("items") or []:
            if not isinstance(it, dict) or not KEY_RE.match(str(it.get("key") or "")):
                continue
            if it.get("state") == "uploading":
                it["state"] = "queued"
            elif it.get("state") == "downloading":
                it["state"] = "submitted"
            it.setdefault("attempts", 0)
            self.items[it["key"]] = it
            self.seq = max(self.seq, int(it.get("seq") or 0))
            if not isinstance(it.get("viewers"), dict):
                it.pop("viewers", None)
            else:
                for v in it["viewers"].values():
                    self.seq = max(self.seq, int(v or 0))
        self.evicted = data.get("evicted") if isinstance(data.get("evicted"), dict) else {}

    def save(self):
        with self.lock:
            _atomic_write_json(self.path, {"version": 1, "items": list(self.items.values()), "evicted": self.evicted})

    def next_seq(self):
        self.seq += 1
        return self.seq

    @staticmethod
    def order(item):
        retried = 1 if item.get("attempts") else 0
        if item.get("lane") == LANE_VIEWING:
            return (0, retried, -Queue.view_seq(item))
        if item.get("lane") == LANE_MANUAL:
            return (1, retried, item.get("seq") or 0)
        if item.get("lane") == LANE_REDO:
            return (3, retried, item.get("seq") or 0)
        return (2, retried, -float(item.get("fav_at") or item.get("at") or 0))

    @staticmethod
    def view_seq(item):
        """When somebody last looked at the item (the newest viewer's sequence number)."""
        v = item.get("viewers")
        return max([int(x or 0) for x in v.values()] or [0]) if isinstance(v, dict) else 0

    def ordered(self, states=("queued",)):
        with self.lock:
            return sorted((i for i in self.items.values() if i.get("state") in states), key=self.order)

    def candidates(self, now):
        return [i for i in self.ordered() if float(i.get("not_before") or 0) <= now]

    def position(self, key):
        """1 = next in line (counting items waiting for a retry time too)."""
        for n, it in enumerate(self.ordered(), 1):
            if it["key"] == key:
                return n
        return None

    def update(self, key, save=True, **fields):
        with self.lock:
            it = self.items.get(key)
            if it is None:
                return None
            it.update(fields)
            if save:
                self.save()
            return it

    def remove(self, keys, save=True):
        with self.lock:
            for k in keys:
                self.items.pop(k, None)
            if save:
                self.save()

    def group(self, worker_key):
        with self.lock:
            return [i for i in self.items.values() if i.get("worker_key") == worker_key
                    and i.get("state") in ("submitted", "downloading", "held", "uploading")]

    def active_groups(self):
        """{worker_key: [items]} of everything the worker has (running, held)."""
        out = {}
        with self.lock:
            for i in self.items.values():
                if i.get("state") in ("submitted", "downloading", "held") and i.get("worker_key"):
                    out.setdefault(i["worker_key"], []).append(i)
        return out

    def evicted_blocked(self, root, path, cap_gb):
        rec = self.evicted.get("%s\n%s" % (root, path))
        return bool(rec) and float(cap_gb) <= float(rec.get("cap_gb") or 0)

    def record_evicted(self, root, path, cap_gb):
        with self.lock:
            self.evicted["%s\n%s" % (root, path)] = {"at": self.clock(), "cap_gb": float(cap_gb)}

    def clear_evicted(self, root, path):
        with self.lock:
            return self.evicted.pop("%s\n%s" % (root, path), None) is not None


# ------------------------------------------------------------------ cache index
class CacheIndex:
    """<cache>/index.json: key -> entry. Entry: kind, root, path, version, profile, created, bytes,
    sbs_bytes, depth_bytes, eye_w, eye_h, fps, duration, frames, source_fps, interpolated,
    gpu_seconds, last_watched, watch_count, sbs_atime, missing_since."""

    def __init__(self, cache_dir):
        self.dir = cache_dir
        self.path = os.path.join(cache_dir, "index.json")
        self.lock = threading.RLock()
        self.entries = _read_json(self.path, {})
        if not isinstance(self.entries, dict):
            self.entries = {}
        self.dirty = False

    def get(self, key):
        with self.lock:
            return self.entries.get(key)

    def put(self, key, entry):
        with self.lock:
            self.entries[key] = entry
            self.dirty = True

    def remove(self, key):
        with self.lock:
            self.dirty = self.entries.pop(key, None) is not None or self.dirty

    def total_bytes(self):
        with self.lock:
            return sum(int(e.get("bytes") or 0) for e in self.entries.values())

    def save(self):
        with self.lock:
            _atomic_write_json(self.path, self.entries)
            self.dirty = False

    def flush(self):
        with self.lock:
            if self.dirty:
                self.save()

    def files_ok(self, key):
        e = self.get(key)
        if not e:
            return False
        base = os.path.join(self.dir, cache_rel(e["kind"], key))
        return all(os.path.isfile(os.path.join(base, n)) for n in RESULT_NAMES[e["kind"]])


# ------------------------------------------------------------------ worker client
class WorkerDown(Exception):
    """The worker cannot be reached (network, not-available): back off and try again."""


class WorkerRefused(Exception):
    def __init__(self, code, message):
        super().__init__("%s: %s" % (code, message))
        self.code, self.message = code, message


class Worker:
    def __init__(self, address_fn):
        self.address_fn = address_fn

    def _request(self, path, method="GET", data=None, headers=None):
        base, token = self.address_fn()
        req = urllib.request.Request(base.rstrip("/") + path, data=data, method=method)
        if token:
            req.add_header("Authorization", "Bearer " + token)
        for k, v in (headers or {}).items():
            req.add_header(k, v)
        return req

    def call(self, path, method="GET", body=None, timeout=10):
        """(http status, parsed JSON) - an HTTP error answer comes back as data, a network failure raises WorkerDown."""
        data = json.dumps(body).encode() if body is not None else (b"" if method == "POST" else None)
        headers = {"Content-Type": "application/json"} if body is not None else {}
        try:
            with urllib.request.urlopen(self._request(path, method, data, headers), timeout=timeout) as r:
                raw = r.read()
                return r.status, (json.loads(raw) if raw else {})
        except urllib.error.HTTPError as exc:
            try:
                parsed = json.loads(exc.read() or b"{}")
            except ValueError:
                parsed = {}
            return exc.code, parsed
        except (OSError, ValueError) as exc:
            raise WorkerDown("the vision worker could not be reached: %s" % exc)

    def status(self):
        code, body = self.call("/v1/stereo/status")
        if code != 200:
            raise WorkerDown("worker answered %s" % code)
        if not body.get("available"):
            raise WorkerDown("3D conversion is not installed on the worker: missing %s" % ", ".join(body.get("missing") or ["?"]))
        return body

    def get_job(self, key):
        return self.call("/v1/stereo/jobs/" + key)

    def put_source(self, key, full, ext=None, name=None):
        """Streams the file as the request body. 507 (worker disk low) and other refusals raise
        WorkerRefused; 409 (already holds it) is fine."""
        size = os.path.getsize(full)
        qs = urllib.parse.urlencode({"name": name} if name else {"ext": ext or "bin"})
        try:
            with open(full, "rb") as fh:
                req = self._request("/v1/stereo/source/%s?%s" % (key, qs), "PUT", fh,
                                    {"Content-Length": str(size), "Content-Type": "application/octet-stream"})
                with urllib.request.urlopen(req, timeout=UPLOAD_TIMEOUT_S) as r:
                    r.read()
        except urllib.error.HTTPError as exc:
            try:
                msg = json.loads(exc.read() or b"{}").get("error") or ""
            except ValueError:
                msg = ""
            if exc.code == 409:
                return
            raise WorkerRefused(exc.code, msg or "refused the upload")
        except OSError as exc:
            if isinstance(exc, FileNotFoundError):
                raise WorkerRefused(404, "source file vanished")
            raise WorkerDown("upload interrupted: %s" % exc)

    def fetch(self, key, name, dest, expected):
        """One result file into dest, resuming a partial dest with a Range request; the size must
        match what the job listed or it raises (the partial stays for the next try)."""
        have = os.path.getsize(dest) if os.path.exists(dest) else 0
        if have > expected:
            os.remove(dest)
            have = 0
        if have < expected:
            path = "/v1/stereo/jobs/%s/result/%s" % (key, urllib.parse.quote(name, safe="/"))
            try:
                with urllib.request.urlopen(self._request(path, headers={"Range": "bytes=%d-" % have} if have else {}),
                                            timeout=120) as r:
                    if have and r.status != 206:
                        have = 0
                    with open(dest, "ab" if have else "wb") as out:
                        shutil.copyfileobj(r, out, 1 << 20)
            except urllib.error.HTTPError as exc:
                raise WorkerRefused(exc.code, "result %s: HTTP error" % name)
            except OSError as exc:
                raise WorkerDown("download interrupted: %s" % exc)
        got = os.path.getsize(dest)
        if got != expected:
            raise WorkerDown("result %s is %d bytes, expected %d" % (name, got, expected))


# ------------------------------------------------------------------ service
class Service:
    """settings_fn() -> the stereo3d settings; resolve_fn(root, path) -> absolute path or None;
    worker_fn() -> (base url, token); favourite_fn(root, path) -> an admin has it favourited;
    favourite_added / favourite_removed / backfill are what the favourites automation calls (the
    server hands backfill() the admins' favourites as (favourited at, root, path) rows);
    forbidden_dirs: folders the cache must never be in (the app dir, /data2)."""

    def __init__(self, queue_path, settings_fn, resolve_fn, worker_fn, favourite_fn=None,
                 forbidden_dirs=(), clock=time.time, pinned_fn=None, turn_fn=None):
        self.last_backfill = None
        self.last_pin_scan = None
        # pinned_fn(root, path) -> the file is in a permanent folder: converted like a favourite
        # and never evicted (favourite_fn should say True for it too).
        self.pinned_fn = pinned_fn
        # turn_fn(root, path) -> degrees clockwise the file is filmed on its side (0 = upright)
        self.turn_fn = turn_fn
        self.settings_fn, self.resolve_fn = settings_fn, resolve_fn
        self.favourite_fn = favourite_fn or (lambda root, path: False)
        self.forbidden = [os.path.realpath(d) for d in forbidden_dirs]
        self.clock = clock
        self.queue = Queue(queue_path, clock)
        self.worker = Worker(worker_fn)
        self.lock = threading.RLock()
        self._index = None
        self._played = {}
        self.problem = None            # why nothing happens, for the settings card
        self.worker_error = None
        self.down_until = 0.0
        self.fail_count = 0
        self.last_status = None
        self.last_step = None
        self._maintained = 0.0
        self.redo_requeued = 0         # since start: 3D entries/items re-queued after a cut
        self.redo_last = None
        self._repair_empty_batches()

    def _repair_empty_batches(self):
        """Empty files never belong in the queue (see _start_next); before 2026-10-29 one of them
        in a picture batch failed the whole batch with the worker's 411 for its empty upload. At
        start, empty items leave the queue and pictures failed by that 411 are queued again. With
        no empty upload left nothing is refused with 411 any more, so once run this finds nothing."""
        q = self.queue
        with q.lock:
            empty = [k for k, i in q.items.items() if (i.get("version") or {}).get("size") == 0]
            redo = [i for i in q.items.values() if i.get("state") == "failed" and item_size(i)
                    and str(i.get("error") or "").startswith("the worker refused: 411")]
            for i in redo:
                i.update(state="queued", attempts=0, error=None, not_before=0, worker_key=None,
                         worker_state=None, progress=None)
            if empty or redo:
                q.remove(empty)
        if empty or redo:
            log("start: %d empty file(s) dropped from the queue, %d picture(s) failed with them queued again"
                % (len(empty), len(redo)))

    # ---- settings, cache folder
    def cfg(self):
        c = self.settings_fn()
        return c

    def cache_dir(self):
        """(folder, None) or (None, why). Never creates the folder."""
        wanted = os.path.abspath((self.cfg().get("cache_dir") or "").strip() or default_cache_dir())
        real = os.path.realpath(wanted)
        for bad in self.forbidden:
            if real == bad or real.startswith(bad + os.sep):
                return None, "cache folder %s is inside %s: the 3D cache must live on the media disk" % (wanted, bad)
        if not os.path.isdir(wanted):
            return None, ("cache folder %s does not exist - create it: create it with the service account as owner (mode 755): %s"
                          % (wanted, wanted))
        if not os.access(wanted, os.W_OK | os.X_OK):
            return None, "cache folder %s is not writable" % wanted
        return wanted, None

    def index(self):
        """The cache index, or None while the cache folder is unusable."""
        cache, problem = self.cache_dir()
        self.problem = problem
        if not cache:
            return None
        with self.lock:
            if self._index is None or self._index.dir != cache:
                if self._index is not None:
                    self._index.flush()
                self._index = CacheIndex(cache)
            return self._index

    # ---- requests
    def _turn(self, kind, root, path):
        """The turn the conversion applies: videos only, 0 when unknown or on any error."""
        if kind != "video" or self.turn_fn is None:
            return 0
        try:
            t = int(self.turn_fn(root, path) or 0)
        except Exception:
            return 0
        return t if t in (90, 180, 270) else 0

    def _profile(self, kind, root, path, cfg=None):
        return profile_for(kind, cfg or self.cfg(), self._turn(kind, root, path))

    def _validate(self, item):
        """Re-stat the item's file: ('ok', item) | ('gone', None) | ('rekeyed', item)."""
        full = self.resolve_fn(item["root"], item["path"])
        ver = file_version(full) if full else None
        if ver is None:
            return "gone", None
        _, pid = self._profile(item["kind"], item["root"], item["path"])
        key = item_key(ver, pid)
        if key == item["key"]:
            return "ok", item
        q = self.queue
        with q.lock:
            old = q.items.pop(item["key"], None)
            if old is None:
                return "gone", None
            if key in q.items:
                q.save()
                return "rekeyed", q.items[key]
            old.update(key=key, version=ver, profile_id=pid, worker_key=None, worker_state=None,
                       progress=None, state="queued", error=None)
            q.items[key] = old
            q.save()
            return "rekeyed", old

    def request(self, root, path, lane=LANE_MANUAL, by=None, fav_at=None, save=True):
        """-> {"status": queued | duplicate | promoted | retry | cached | skipped, "key", "reason"}."""
        cfg = self.cfg()
        out = {"root": root, "path": path}
        if not cfg.get("enabled", True):
            return dict(out, status="skipped", reason="3D conversion is turned off")
        kind = kind_of(path)
        if kind is None:
            return dict(out, status="skipped", reason="only videos and pictures can be made 3D")
        full = self.resolve_fn(root, path)
        ver = file_version(full) if full else None
        if ver is None:
            return dict(out, status="skipped", reason="file not found")
        if not ver.get("size"):
            return dict(out, status="skipped", reason="the file is empty")
        profile, pid = self._profile(kind, root, path, cfg)
        key = item_key(ver, pid)
        out["key"] = key
        q = self.queue
        viewing = lane == LANE_VIEWING
        manual = lane == LANE_MANUAL
        urgent = manual or viewing            # both are somebody asking now: an evicted-for-space record does not block
        cap = float(cfg.get("cache_max_gb") or 0)
        idx = self.index()
        if idx is not None and idx.files_ok(key):
            e = idx.get(key)
            if (e.get("root"), e.get("path")) != (root, path):      # renamed or moved: same inode
                e.update(root=root, path=path)
                idx.dirty = True
            if urgent:
                q.clear_evicted(root, path)
                if save:
                    q.save()
            return dict(out, status="cached")
        if not urgent and lane != LANE_REDO and q.evicted_blocked(root, path, cap):
            return dict(out, status="skipped", reason="evicted for space; waits for a bigger cache or a manual request")
        with q.lock:
            existing = q.items.get(key)
            if existing is not None:
                if urgent:
                    q.clear_evicted(root, path)
                if viewing:
                    result = self._view_existing(existing, by)
                    self._enforce_view_bound(by)
                elif manual and existing.get("lane") in (LANE_FAVORITE, LANE_REDO):
                    existing.update(lane=LANE_MANUAL, seq=q.next_seq(), by=by)
                    result = "promoted"
                elif manual and existing.get("state") == "failed":
                    existing.pop("viewers", None)
                    existing.update(lane=LANE_MANUAL, state="queued", attempts=0, error=None, not_before=0,
                                    worker_key=None, worker_state=None, seq=q.next_seq())
                    result = "retry"
                else:
                    result = "duplicate"          # incl. a manual request for an item in the viewing lane
                if save:
                    q.save()
                return dict(out, status=result)
            if urgent:
                q.clear_evicted(root, path)
            now = self.clock()
            q.items[key] = {"key": key, "root": root, "path": path, "ext": worker_ext(path), "kind": kind,
                            "lane": lane, "seq": q.next_seq() if (manual or lane == LANE_REDO) else 0, "by": by, "at": now,
                            "fav_at": fav_at or now, "version": ver, "profile_id": pid, "attempts": 0,
                            "state": "queued", "error": None, "worker_key": None, "worker_state": None,
                            "progress": None, "not_before": 0}
            if viewing:
                q.items[key]["viewers"] = {str(by): q.next_seq()}
                self._enforce_view_bound(by)
            if save:
                q.save()
        return dict(out, status="queued")

    # ---- viewing lane (the VR app opened it: someone is looking at it right now)
    def _view_existing(self, it, user):
        """An item already in the queue is viewed by `user` (queue lock held). A waiting item of
        another lane moves up to the viewing lane; one in it just gets this viewer's newest stamp;
        a failed one (waits for an admin) and one the worker has stay as they are."""
        if it.get("state") != "queued":
            return "duplicate"
        in_lane = it.get("lane") == LANE_VIEWING and isinstance(it.get("viewers"), dict)
        viewers = it["viewers"] if in_lane else {}
        viewers[str(user)] = self.queue.next_seq()
        it.update(lane=LANE_VIEWING, viewers=viewers)
        return "duplicate" if in_lane else "promoted"

    def _demote(self, it, user=None):
        """`user` stops viewing an item of the viewing lane. With no viewer left (and the item not
        started) it goes to the END of the manual lane: converted later, never cancelled."""
        viewers = it.get("viewers") if isinstance(it.get("viewers"), dict) else {}
        if user is not None:
            viewers.pop(str(user), None)
        if viewers or it.get("lane") != LANE_VIEWING:
            it["viewers"] = viewers
            return False
        it.pop("viewers", None)
        it.update(lane=LANE_MANUAL, seq=self.queue.next_seq())
        return True

    def _enforce_view_bound(self, user):
        """At most viewing_max_per_user (default VIEWING_MAX_PER_USER) queued viewing items per user
        (queue lock held): the user's oldest view is demoted - a lost leave (headset taken off)."""
        limit = max(1, int(self.cfg().get("viewing_max_per_user") or VIEWING_MAX_PER_USER))
        mine = sorted((i for i in self.queue.items.values()
                       if i.get("lane") == LANE_VIEWING and i.get("state") == "queued"
                       and isinstance(i.get("viewers"), dict) and str(user) in i["viewers"]),
                      key=lambda i: int(i["viewers"][str(user)] or 0))
        for it in mine[:max(0, len(mine) - limit)]:
            self._demote(it, user)

    def view(self, root, path, user):
        """The VR app opened (root, path) for `user`: a picture or video without a 3D version is
        queued in the viewing lane. -> request()'s answer."""
        return self.request(root, path, LANE_VIEWING, user)

    def leave(self, root, path, user):
        """`user` left the item: still waiting in the viewing lane -> manual lane, at the end (a
        running job and every other lane are untouched). -> {"status": demoted | kept | none}."""
        kind = kind_of(path)
        full = self.resolve_fn(root, path) if kind else None
        ver = file_version(full) if full else None
        if ver is None:
            return {"status": "none"}
        _, pid = self._profile(kind, root, path)
        q = self.queue
        with q.lock:
            it = q.items.get(item_key(ver, pid))
            if it is None or it.get("lane") != LANE_VIEWING or it.get("state") != "queued":
                return {"status": "none"}
            demoted = self._demote(it, user)
            q.save()
        return {"status": "demoted" if demoted else "kept"}

    def request_many(self, items, lane=LANE_MANUAL, by=None):
        """Selection order is kept: manual items get increasing sequence numbers."""
        results = [self.request(i["root"], i["path"], lane, by, save=False) for i in items]
        self.queue.save()
        idx = self._index
        if idx is not None:
            idx.flush()
        return results

    # ---- redo after a cut (video_trim.py rewrote the file: size and mtime changed)
    def redo_after_cut(self, root, path, old_version):
        """The file (root, path) was cut (or the cut undone): its bytes changed in place, so the
        key of the 3D made for it - dev:ino:size:mtime_ns - changed too. If 3D existed for the OLD
        version (a finished cache entry, or a queue item in any state), the file is queued again as
        it is now and the old entry / item is dropped. Nothing existed: nothing is queued (a fresh
        download that is cut right away has nothing to redo).
        Lane: a favourite keeps the favourites lane (with the favourite's old fav_at); something a
        person was still waiting on (manual / viewing) goes to the manual lane; everything else to
        LANE_REDO, the lowest. -> {"status": requeued | none | skipped, "key", "old_key", "lane"}."""
        cfg = self.cfg()
        kind = kind_of(path)
        if kind is None or not cfg.get("enabled", True):
            return {"status": "none"}
        full = self.resolve_fn(root, path)
        new_ver = file_version(full) if full else None
        if new_ver is None:
            return {"status": "none"}
        _, pid = self._profile(kind, root, path, cfg)
        old_key = item_key(old_version, pid)
        if old_key == item_key(new_ver, pid):
            return {"status": "none"}
        idx = self.index()
        q = self.queue
        had_entry = idx is not None and idx.files_ok(old_key)
        with q.lock:
            old_item = dict(q.items[old_key]) if old_key in q.items else None
        if not had_entry and old_item is None:
            return {"status": "none", "old_key": old_key}
        by = (old_item or {}).get("by")
        fav_at = (old_item or {}).get("fav_at")
        if self.favourite_fn(root, path):
            lane = LANE_FAVORITE
        elif old_item is not None and old_item.get("lane") in URGENT_LANES:
            lane, fav_at = LANE_MANUAL, None
        else:
            lane, fav_at = LANE_REDO, None
        res = self.request(root, path, lane, by or "cut", fav_at=fav_at)
        if res.get("status") == "skipped":
            return dict(res, old_key=old_key)         # the old entry stays: nothing replaces it
        if old_item is not None:
            self.cancel(old_key)
        if had_entry and idx is not None:
            with idx.lock:
                self._delete_entry(idx, old_key)
                idx.save()
        self.redo_requeued += 1
        self.redo_last = {"root": root, "path": path, "at": self.clock(), "lane": lane}
        log("re-queued %s/%s after a cut (%s)" % (root, path, lane))
        return {"status": "requeued", "key": res.get("key"), "old_key": old_key, "lane": lane,
                "request": res.get("status")}

    def redo_counts(self):
        """For the trim settings line: items in the redo lane by state, and the total since start."""
        counts = {"queued": 0, "running": 0, "failed": 0}
        with self.queue.lock:
            for it in self.queue.items.values():
                if it.get("lane") != LANE_REDO:
                    continue
                st = it.get("state")
                counts["failed" if st == "failed" else "queued" if st == "queued" else "running"] += 1
        return dict(counts, requeued_total=self.redo_requeued)

    # ---- favourites automation
    def favourite_added(self, root, path, by=None, fav_at=None):
        """An admin favourited a file: queue it in the favourites lane (behind every manual
        request; newest favourite first). Same answer as request(); "skipped" when automatic
        preparation is off. Idempotent: a file already queued, cached or manual stays as it is
        (a manual item is never demoted), one evicted for space stays out (see request())."""
        if not self.cfg().get("favorites_auto", True):
            return {"root": root, "path": path, "status": "skipped",
                    "reason": "favourites are not prepared automatically"}
        return self.request(root, path, LANE_FAVORITE, by, fav_at=fav_at)

    def favourite_removed(self, root, path):
        """The favourite is gone. Unless another admin still has the file favourited: a favourites-lane
        item that is still waiting (queued, or failed) leaves the queue; a job the worker already has
        (uploading, running, downloading, held) is left alone, and a manual item is never touched. The
        evicted-for-space record goes too (the favourite it belonged to is gone). Eviction protection
        needs no work: the cache asks favourite_fn each time; the cached entry stays.
        -> {"status": kept | dequeued | none, "dequeued": n}"""
        if self.favourite_fn(root, path):
            return {"status": "kept", "dequeued": 0}
        q = self.queue
        with q.lock:
            keys = [k for k, i in q.items.items()
                    if i.get("root") == root and i.get("path") == path and i.get("lane") == LANE_FAVORITE
                    and i.get("state") in ("queued", "failed")]
            q.remove(keys, save=False)
            cleared = q.clear_evicted(root, path)
            if keys or cleared:
                q.save()
        return {"status": "dequeued" if keys else "none", "dequeued": len(keys)}

    def _unpinned(self, item):
        """A favourites-lane item the permanent-folder scan queued whose file is in no permanent
        folder any more (the folder was taken off the list) and that no admin has favourited.
        Asks the settings, not the disk: a folder that cannot be read for a while drops nothing."""
        root, path = item.get("root"), item.get("path")
        return (item.get("by") == PIN_BY and item.get("lane") == LANE_FAVORITE
                and not (self.pinned_fn and self.pinned_fn(root, path)) and not self.favourite_fn(root, path))

    def drop_unpinned(self):
        """User, 2026-10-29: took the Series folder off the list, yet its 50 episodes stayed queued
        and kept being uploaded and converted - the scan only ever added. Now what a permanent
        folder queued and the worker has not started (queued, or failed) leaves the queue when the
        file is in no permanent folder any more; a job the worker already has is left alone, as for
        a favourite that is removed (favourite_removed). -> how many items left the queue."""
        q = self.queue
        with q.lock:
            waiting = [dict(i) for i in q.items.values() if i.get("state") in ("queued", "failed")
                       and i.get("by") == PIN_BY]
        stale = [i["key"] for i in waiting if self._unpinned(i)]      # outside the queue lock
        if not stale:
            return 0
        with q.lock:
            keys = [k for k in stale if (q.items.get(k) or {}).get("state") in ("queued", "failed")]
            q.remove(keys)
        log("%d waiting item(s) no longer in a permanent folder left the queue" % len(keys))
        return len(keys)

    def pin_scan(self, files):
        """files: (mtime, root, path) of everything in the permanent folders. Queues what is not
        converted yet in the favourites lane, newest file first; idempotent like backfill(). What
        the folders queued earlier and is in none of them now leaves the queue (drop_unpinned)."""
        cfg = self.cfg()
        out = {"at": self.clock(), "files": 0, "queued": 0, "cached": 0, "duplicate": 0,
               "promoted": 0, "retry": 0, "skipped": 0, "dropped": 0}
        if not cfg.get("enabled", True):
            out["status"] = "off"
            self.last_pin_scan = out
            return out
        rows = sorted(((float(ts or 0), root, path) for ts, root, path in files if kind_of(path)),
                      key=lambda r: (-r[0], r[1], r[2]))
        out["files"] = len(rows)
        for ts, root, path in rows:
            r = self.request(root, path, LANE_FAVORITE, PIN_BY, fav_at=ts, save=False)
            out[r["status"]] = out.get(r["status"], 0) + 1
        self.queue.save()
        out["dropped"] = self.drop_unpinned()
        if self._index is not None:
            self._index.flush()
        out["status"] = "ok"
        self.last_pin_scan = out
        return out

    def backfill(self, favourites):
        """favourites: (favourited at, root, path) rows of the ADMINS' favourites. Queues those newer
        than favorites_backfill_days in the favourites lane; the lane orders by favourited-at, so
        it is newest first whatever the order of the rows. Idempotent: whatever is queued, cached,
        failed, manual or evicted for space is left as it is, so a second run adds nothing. 0 days,
        favorites_auto off or 3D off = nothing. -> counts per request() status plus "evicted"
        (skipped because the cache evicted it for space and has not grown since)."""
        cfg = self.cfg()
        days = float(cfg.get("favorites_backfill_days") or 0)
        out = {"at": self.clock(), "days": days, "window": 0, "queued": 0, "cached": 0, "duplicate": 0,
               "promoted": 0, "retry": 0, "skipped": 0, "evicted": 0}
        if days <= 0 or not cfg.get("favorites_auto", True) or not cfg.get("enabled", True):
            out["status"] = "off"
            self.last_backfill = out
            return out
        since = out["at"] - days * 86400
        newest = {}
        for ts, root, path in favourites:
            if kind_of(path) is None or float(ts or 0) < since:
                continue
            key = (root, path)
            newest[key] = max(float(ts or 0), newest.get(key, 0.0))
        out["window"] = len(newest)
        for (root, path), ts in sorted(newest.items(), key=lambda kv: (-kv[1], kv[0])):
            r = self.request(root, path, LANE_FAVORITE, "backfill", fav_at=ts, save=False)
            out[r["status"]] = out.get(r["status"], 0) + 1
            if r["status"] == "skipped" and "evicted" in (r.get("reason") or ""):
                out["evicted"] += 1
        self.queue.save()
        if self._index is not None:
            self._index.flush()
        out["status"] = "ok"
        self.last_backfill = out
        return out

    def cancel(self, key):
        """Drops an item from the queue; a job the worker runs for it is deleted unless other
        items (a picture batch) still need it. True when there was such an item."""
        q = self.queue
        with q.lock:
            item = q.items.get(key)
            if item is None:
                return False
            wk = item.get("worker_key")
            q.remove([key])
            rest = q.group(wk) if wk else []
        if wk and not rest:
            try:
                self.worker.call("/v1/stereo/jobs/" + wk, "DELETE")
            except WorkerDown:
                pass
        return True

    def clear_queue(self):
        n = 0
        for key in list(self.queue.items):
            n += bool(self.cancel(key))
        return n

    def retry(self, key=None):
        """Failed items -> queued again (one key, or every failed item)."""
        n = 0
        with self.queue.lock:
            for it in self.queue.items.values():
                if it.get("state") == "failed" and (key is None or it["key"] == key):
                    it.update(state="queued", attempts=0, error=None, not_before=0, worker_key=None,
                              worker_state=None, progress=None)
                    n += 1
            self.queue.save()
        self.down_until = 0.0
        return n

    # ---- the dispatcher
    def busy(self):
        return any(i.get("state") != "failed" for i in self.queue.items.values())

    def step(self):
        """One tick; returns a short word for what happened (tests and logs)."""
        now = self.clock()
        cfg = self.cfg()
        if not cfg.get("enabled", True):
            return self._done("off")
        idx = self.index()
        if idx is None:
            return self._done("cache folder: " + str(self.problem))
        if not any(i.get("state") != "failed" for i in self.queue.items.values()):
            return self._done("idle")
        if now < self.down_until:
            return self._done("backoff")
        try:
            self.last_status = self.worker.status()
            self.fail_count, self.worker_error = 0, None
            return self._done(self._step_worker(cfg, idx, now))
        except (WorkerDown, WorkerRefused) as exc:
            self.fail_count += 1
            self.worker_error = str(exc)
            self.down_until = now + min(BACKOFF_MAX_S, BACKOFF_MIN_S * 2 ** (self.fail_count - 1))
            for it in self.queue.items.values():
                if it.get("state") == "uploading":
                    it.update(state="queued", worker_key=None)
            self.queue.save()
            log("worker: %s (retry in %ds)" % (exc, self.down_until - now))
            return self._done("worker down")

    def _done(self, what):
        self.last_step = what
        return what

    def _step_worker(self, cfg, idx, now):
        q = self.queue
        did = "polled"
        running = None
        for wk, items in list(q.active_groups().items()):
            state = self._poll(wk, items, cfg, idx, now)
            if state in ("collected", "failed", "lost"):
                did = state
            elif state not in ("held",):
                running = (wk, items)
        if running is not None:
            self._maybe_hold(running, now)
            return did
        return self._start_next(cfg, idx, now, did)

    # -- polling
    def _poll(self, wk, items, cfg, idx, now):
        code, job = self.worker.get_job(wk)
        q = self.queue
        keys = [i["key"] for i in items]
        if code == 404:
            log("worker lost job %s: back to the queue" % wk[:8])
            for k in keys:
                q.update(k, save=False, state="queued", worker_key=None, worker_state=None, progress=None)
            q.save()
            return "lost"
        if code != 200:
            raise WorkerDown("worker answered %s for a job" % code)
        state = job.get("state")
        for k in keys:
            q.update(k, save=False, worker_state=state, progress=job.get("progress"),
                     worker_position=job.get("queue_position"))
        if state == "done":
            self._collect(wk, items, job, cfg, idx)
            return "collected"
        if state == "failed":
            self._fail(keys, job.get("error") or "the worker's job failed", cfg, now=self.clock())
            self.worker.call("/v1/stereo/jobs/" + wk, "DELETE")
            return "failed"
        if state == "cancelled":
            for k in keys:
                q.update(k, save=False, state="queued", worker_key=None, worker_state=None, progress=None)
            q.save()
            return "lost"
        if state == "held":
            for k in keys:
                q.update(k, save=False, state="held")
            q.save()
            return "held"
        for k in keys:
            q.update(k, save=False, state="submitted")
        return "running"

    def _maybe_hold(self, running, now):
        """A manual or viewing request goes ahead of a favourite VIDEO the worker is converting."""
        wk, items = running
        if len(items) != 1 or items[0].get("kind") != "video" or items[0].get("lane") not in (LANE_FAVORITE, LANE_REDO):
            return
        if not any(i.get("lane") in URGENT_LANES for i in self.queue.candidates(now)):
            return
        if any(i.get("state") == "held" for i in self.queue.items.values()):
            return
        code, body = self.worker.call("/v1/stereo/jobs/%s/hold" % wk, "POST", timeout=30)
        if code in (200, 202) and body.get("state") in ("held", "holding"):
            if body.get("state") == "held":
                self.queue.update(items[0]["key"], state="held", worker_state="held")
            log("held favourite %s for a manual/viewing request" % wk[:8])

    # -- collecting a finished job
    def _collect(self, wk, items, job, cfg, idx):
        q = self.queue
        cache = idx.dir
        listed = {r["name"]: int(r.get("bytes") or 0) for r in job.get("results") or []}
        part = os.path.join(cache, ".partial", wk)
        os.makedirs(part, exist_ok=True)
        for it in items:
            q.update(it["key"], save=False, state="downloading")
        done, failed = [], []
        for it in items:
            if q.items.get(it["key"]) is None:
                continue                                  # cancelled meanwhile: its result is not pulled
            kind, key = it["kind"], it["key"]
            names = RESULT_NAMES[kind]
            prefix = "" if kind == "video" else key + "/"
            if any(prefix + n not in listed for n in names):
                failed.append(key)
                continue
            stage = os.path.join(part, key)
            os.makedirs(stage, exist_ok=True)
            for n in names:
                self.worker.fetch(wk, prefix + n, os.path.join(stage, n), listed[prefix + n])
            meta = _read_json(os.path.join(stage, "meta.json"), {})
            target = os.path.join(cache, cache_rel(kind, key))
            os.makedirs(os.path.dirname(target), exist_ok=True)
            if os.path.isdir(target):
                shutil.rmtree(target)
            os.replace(stage, target)
            sizes = {n: os.path.getsize(os.path.join(target, n)) for n in names}
            try:
                atime = os.stat(os.path.join(target, names[0])).st_atime
            except OSError:
                atime = 0
            now = self.clock()
            idx.put(key, {
                "kind": kind, "root": it["root"], "path": it["path"], "version": it["version"],
                "profile": it["profile_id"], "created": now, "bytes": sum(sizes.values()),
                "sbs_bytes": sizes[names[0]], "depth_bytes": sizes[names[1]],
                "eye_w": meta.get("eye_w"), "eye_h": meta.get("eye_h"), "fps": meta.get("fps"),
                "duration": meta.get("duration"), "frames": meta.get("frames"),
                "source_fps": meta.get("source_fps"), "interpolated": meta.get("interpolated"),
                "gpu_seconds": meta.get("gpu_seconds"), "last_watched": now, "watch_count": 0,
                "sbs_atime": atime, "missing_since": None})
            done.append(key)
        idx.save()
        if failed:
            self._fail(failed, "the worker's result lacks files for this picture", cfg, now=self.clock())
        q.remove(done)
        self.worker.call("/v1/stereo/jobs/" + wk, "DELETE")
        shutil.rmtree(part, ignore_errors=True)
        log("job %s done: %d cached%s" % (wk[:8], len(done), ", %d failed" % len(failed) if failed else ""))
        self.evict(cfg, idx)

    def _upload_failed(self, keys, exc, cfg, now):
        """An upload broke off part way. The worker answered /v1/stereo/status just before, so when
        the same item's upload breaks UPLOAD_FAILS_MAX times in a row it is this file, not the
        network: it then counts as a failed attempt (to the back of its lane for FAILED_RETRY_S,
        failed after max_attempts) and the queue moves on - before, it stayed first in line and
        blocked every item behind it (2026-10-29: a day of "connection reset" for one episode).
        -> True when it was given up on; False: the caller backs off as for a worker that is down."""
        q = self.queue
        with q.lock:
            fails = 0
            for k in keys:
                it = q.items.get(k)
                if it is not None:
                    it["upload_fails"] = int(it.get("upload_fails") or 0) + 1
                    fails = max(fails, it["upload_fails"])
            if fails < UPLOAD_FAILS_MAX:
                q.save()
                return False
            for k in keys:
                if k in q.items:
                    q.items[k]["upload_fails"] = 0
        self._fail(keys, "the upload to the vision worker broke off %d times in a row: %s" % (fails, exc), cfg, now)
        log("gave up uploading %s after %d tries: %s" % (keys[0][:8], fails, exc))
        return True

    def _upload_limit(self):
        """The largest file the worker takes (max_upload_bytes in its /v1/stereo/status); 0 when it
        does not say (an older worker), and then nothing is refused here."""
        try:
            return int((self.last_status or {}).get("max_upload_bytes") or 0)
        except (TypeError, ValueError):
            return 0

    def _fail(self, keys, error, cfg, now):
        q = self.queue
        limit = int(cfg.get("max_attempts") or 2)
        with q.lock:
            for k in keys:
                it = q.items.get(k)
                if it is None:
                    continue
                it["attempts"] = int(it.get("attempts") or 0) + 1
                it.update(error=str(error)[:300], worker_key=None, worker_state=None, progress=None)
                if it["attempts"] >= limit:
                    it["state"] = "failed"
                else:
                    it.update(state="queued", not_before=now + FAILED_RETRY_S)
            q.save()

    # -- starting the next job
    def _start_next(self, cfg, idx, now, did):
        q = self.queue
        manual = [i for i in q.candidates(now) if i.get("lane") in URGENT_LANES]
        held = [(wk, g) for wk, g in q.active_groups().items() if all(i.get("state") == "held" for i in g)]
        if not manual and held:
            wk, g = held[0]
            code, body = self.worker.call("/v1/stereo/jobs/%s/resume" % wk, "POST")
            if code == 200:
                for i in g:
                    q.update(i["key"], save=False, state="submitted", worker_state="waiting_gpu")
                q.save()
                return "resumed"
            if code == 409:                 # not held any more: the next poll sorts it out
                for i in g:
                    q.update(i["key"], save=False, state="submitted")
                q.save()
                return did
        for _ in range(len(q.items) + 1):
            cands = q.candidates(now)
            if not cands:
                return did
            first = cands[0]
            verdict, item = self._validate(first)
            if verdict == "gone":
                q.remove([first["key"]])
                log("%s/%s vanished: dropped" % (first["root"], first["path"]))
                continue
            if verdict == "rekeyed":
                continue                    # the file changed under the queue: look again with the new key
            if self._unpinned(item):
                # Its folder came off the list (also the last one, when the server stops scanning).
                q.remove([item["key"]])
                log("%s/%s is in no permanent folder any more: dropped" % (item["root"], item["path"]))
                continue
            if idx.files_ok(item["key"]):
                idx.get(item["key"]).update(root=item["root"], path=item["path"])
                idx.dirty = True
                q.remove([item["key"]])
                continue
            size = item_size(item)
            if not size:
                # An empty file (what a deleted download leaves behind): nothing to convert, and the
                # worker refuses its upload with 411, which in a picture batch failed every picture
                # with it (2026-10-29: 28 empty favourites took 28 real ones down). It leaves the queue.
                q.remove([item["key"]])
                log("%s/%s is empty: dropped" % (item["root"], item["path"]))
                continue
            limit = self._upload_limit()
            if limit and size > limit:
                # Above it the worker's server answers 413 and drops the connection while the file
                # is still being sent, which media-dl only ever sees as "connection reset". Retrying
                # cannot help; Retry on the card tries again (after the worker's limit was raised).
                q.update(item["key"], state="failed", worker_key=None, error=(
                    "too big for the vision worker: %.1f GiB, it takes at most %.1f GiB" % (size / 2 ** 30, limit / 2 ** 30)))
                log("%s/%s is too big for the worker (%.1f GiB): failed" % (item["root"], item["path"], size / 2 ** 30))
                continue
            group = [item]
            if item["kind"] == "picture":
                for other in cands[1:]:
                    if len(group) >= PICTURE_BATCH_MAX:
                        break
                    if other["kind"] == "picture" and other["lane"] == item["lane"]:
                        v2, o2 = self._validate(other)
                        if v2 == "ok" and not idx.files_ok(o2["key"]) and item_size(o2) and not (
                                limit and item_size(o2) > limit) and not self._unpinned(o2):
                            group.append(o2)
            # Make room first. A manual request may push out anything (favourites last); a favourite
            # never pushes out another favourite, so a full cache of favourites does not churn.
            cap = float(cfg.get("cache_max_gb") or 0) * 1024 ** 3
            if item.get("lane") in (LANE_FAVORITE, LANE_REDO):
                est = EST_VIDEO_BYTES if item["kind"] == "video" else EST_PICTURE_BYTES * len(group)
                self.evict(cfg, idx, est, allow_favourites=False)
                if idx.total_bytes() + est > cap:
                    for g in group:
                        q.update(g["key"], save=False, not_before=now + FULL_RETRY_S,
                                 error="the 3D cache is full (of favourites)")
                    q.save()
                    continue
            else:
                self.evict(cfg, idx)
            return self._submit(group, cfg, now)
        return did

    def _submit(self, group, cfg, now):
        q = self.queue
        kind = group[0]["kind"]
        wk = group[0]["key"] if kind == "video" else batch_key([i["key"] for i in group])
        keys = [i["key"] for i in group]
        profile, _ = self._profile(kind, group[0]["root"], group[0]["path"], cfg)
        label = ("%s/%s" % (group[0]["root"], group[0]["path"]) + (" +%d" % (len(group) - 1) if len(group) > 1 else ""))[:200]
        try:
            code, job = self.worker.get_job(wk)
            if code == 200 and job.get("state") == "uploaded" and kind == "picture":
                # A batch may have been cut off part way (the job lists no file names): upload it all again.
                self.worker.call("/v1/stereo/jobs/" + wk, "DELETE")
                code = 404
            if code == 200 and job.get("state") not in (None, "uploaded"):
                if job["state"] in ("failed", "held"):
                    self.worker.call("/v1/stereo/jobs", "POST", {"key": wk, "kind": kind, "profile": profile, "label": label})
            else:
                if code != 200:
                    for i in group:
                        q.update(i["key"], save=False, state="uploading", worker_key=wk, error=None)
                    q.save()
                    try:
                        for i in group:
                            full = self.resolve_fn(i["root"], i["path"])
                            if kind == "video":
                                self.worker.put_source(wk, full, ext=i["ext"])
                            else:
                                self.worker.put_source(wk, full, name="%s.%s" % (i["key"], i["ext"]))
                    except WorkerDown as exc:
                        if self._upload_failed(keys, exc, cfg, now):
                            return "upload failed"
                        raise
                    if not q.group(wk):
                        self.worker.call("/v1/stereo/jobs/" + wk, "DELETE")
                        return "cancelled"
                code, body = self.worker.call("/v1/stereo/jobs", "POST", {"key": wk, "kind": kind, "profile": profile, "label": label})
                if code not in (200, 201):
                    raise WorkerRefused(code, body.get("error") or "job refused")
        except WorkerRefused as exc:
            if exc.code == 507:
                self.down_until = now + DISK_LOW_BACKOFF_S
                self.worker_error = "worker disk is low: %s" % exc.message
                for k in keys:
                    q.update(k, save=False, state="queued", worker_key=None, error=self.worker_error)
                q.save()
                return "worker disk low"
            self._fail(keys, "the worker refused: %s" % exc, cfg, now)
            return "refused"
        for k in keys:
            q.update(k, save=False, state="submitted", worker_key=wk, worker_state="waiting_gpu", error=None,
                     upload_fails=0)
        q.save()
        if not q.group(wk):
            # Every item was cancelled while the upload ran: do not leave a job nobody tracks.
            self.worker.call("/v1/stereo/jobs/" + wk, "DELETE")
            return "cancelled"
        return "submitted"

    # ---- cache housekeeping
    def evict(self, cfg, idx, extra_bytes=0, allow_favourites=True):
        """Deletes cache entries until the cache (plus extra_bytes for what is about to come) fits
        cache_max_gb; returns the evicted keys."""
        cap = float(cfg.get("cache_max_gb") or 0) * 1024 ** 3
        evicted = []
        with idx.lock:
            if idx.total_bytes() + extra_bytes <= cap:
                return evicted
            order = eviction_order(idx.entries, self.favourite_fn, self.clock(), allow_favourites,
                                   self.pinned_fn)
            for key in order:
                if idx.total_bytes() + extra_bytes <= cap:
                    break
                e = idx.get(key)
                if self.favourite_fn(e.get("root"), e.get("path")):
                    self.queue.record_evicted(e.get("root"), e.get("path"), cfg.get("cache_max_gb"))
                self._delete_entry(idx, key)
                evicted.append(key)
            if evicted:
                idx.save()
                self.queue.save()
        if evicted:
            log("evicted %d entries to fit %s GB" % (len(evicted), cfg.get("cache_max_gb")))
        return evicted

    def _delete_entry(self, idx, key):
        e = idx.get(key)
        if e and KEY_RE.match(key):
            shutil.rmtree(os.path.join(idx.dir, cache_rel(e["kind"], key)), ignore_errors=True)
        idx.remove(key)

    def clear_cache(self):
        idx = self.index()
        if idx is None:
            return 0
        with idx.lock:
            n = len(idx.entries)
            for key in list(idx.entries):
                self._delete_entry(idx, key)
            idx.save()
        shutil.rmtree(os.path.join(idx.dir, ".partial"), ignore_errors=True)
        return n

    def maintenance(self, force=False):
        """Hourly: orphan marks, the watched clock, entries whose files went, stray folders, eviction."""
        now = self.clock()
        idx = self.index()
        if idx is None:
            return
        cfg = self.cfg()
        with idx.lock:
            for key, e in list(idx.entries.items()):
                if not idx.files_ok(key):
                    idx.remove(key)
                    continue
                full = self.resolve_fn(e.get("root"), e.get("path"))
                ver = file_version(full) if full else None
                alive = ver is not None and all(ver.get(k) == (e.get("version") or {}).get(k) for k in ("ino", "size", "mtime_ns"))
                if alive and e.get("missing_since"):
                    e["missing_since"] = None
                    idx.dirty = True
                elif not alive and not e.get("missing_since"):
                    e["missing_since"] = now
                    idx.dirty = True
                try:
                    atime = os.stat(os.path.join(idx.dir, cache_rel(e["kind"], key), RESULT_NAMES[e["kind"]][0])).st_atime
                except OSError:
                    continue
                if atime > float(e.get("sbs_atime") or 0) + 1:
                    e["sbs_atime"] = atime
                    if atime > float(e.get("last_watched") or 0):
                        e["last_watched"] = atime
                        e["watch_count"] = int(e.get("watch_count") or 0) + 1
                    idx.dirty = True
        self.evict(cfg, idx)
        self._tidy(idx, now)
        idx.flush()
        self.queue.save()

    def _tidy(self, idx, now):
        """Folders no entry owns (a crash between the rename and the index) after a day; .partial
        folders of no running job after three days."""
        known = set(idx.entries)
        for top in ("v", "p"):
            root = os.path.join(idx.dir, top)
            for sub in _listdir(root):
                for name in _listdir(os.path.join(root, sub)):
                    path = os.path.join(root, sub, name)
                    if name not in known and _age(path, now) > 86400:
                        shutil.rmtree(path, ignore_errors=True)
        live = set(self.queue.active_groups())
        part = os.path.join(idx.dir, ".partial")
        for name in _listdir(part):
            if name not in live and _age(os.path.join(part, name), now) > 3 * 86400:
                shutil.rmtree(os.path.join(part, name), ignore_errors=True)

    def flush(self):
        """Saves the watched clock and other index changes (every few minutes)."""
        if self._index is not None:
            self._index.flush()

    # ---- views
    def note_play(self, key, range_header=None):
        """A play start served by media-dl (no Range, or Range from 0): the watched clock."""
        if range_header and not re.match(r"^bytes=0-", range_header.strip()):
            return False
        now = self.clock()
        if now - self._played.get(key, 0) < PLAY_GAP_S:
            return False
        idx = self._index
        e = idx.get(key) if idx else None
        if not e:
            return False
        self._played[key] = now
        e["last_watched"] = now
        e["watch_count"] = int(e.get("watch_count") or 0) + 1
        idx.dirty = True
        return True

    def entry_files(self, key):
        """(folder, entry) of a cached key whose files are all there, else (None, None)."""
        if not KEY_RE.match(key or ""):
            return None, None
        idx = self.index()
        if idx is None or not idx.files_ok(key):
            return None, None
        e = idx.get(key)
        return os.path.join(idx.dir, cache_rel(e["kind"], key)), e

    def entry_view(self, key, e, favourite=False):
        names = RESULT_NAMES[e["kind"]]
        base = "/api/media/3d/file?key=%s&name=" % key
        return {"key": key, "root": e.get("root"), "path": e.get("path"),
                "title": os.path.splitext(os.path.basename(e.get("path") or ""))[0], "kind": e["kind"],
                "sbs_url": base + names[0], "depth_url": base + names[1], "eye_w": e.get("eye_w"),
                "eye_h": e.get("eye_h"), "fps": e.get("fps"), "duration": e.get("duration"),
                "bytes": e.get("bytes"), "created": e.get("created"), "last_watched": e.get("last_watched"),
                "source_missing": bool(e.get("missing_since")), "favourite": bool(favourite)}

    def list_entries(self, visible_fn, favourite_for=None):
        idx = self.index()
        if idx is None:
            return []
        with idx.lock:
            rows = [(k, dict(e)) for k, e in idx.entries.items() if idx.files_ok(k) and visible_fn(e.get("root"))]
        rows.sort(key=lambda r: -float(r[1].get("created") or 0))
        return [self.entry_view(k, e, bool(favourite_for and favourite_for(e.get("root"), e.get("path")))) for k, e in rows]

    def ready_paths(self):
        """{(root, path)} of every finished conversion, for the 3D filter. From the index alone: the files_ok of
        list_entries would stat thousands of files on every page of a list."""
        idx = self.index()
        if idx is None:
            return set()
        with idx.lock:
            return {(e.get("root"), e.get("path")) for e in idx.entries.values()}

    def status_for(self, root, path):
        kind = kind_of(path)
        if kind is None:
            return {"state": "unsupported"}
        full = self.resolve_fn(root, path)
        ver = file_version(full) if full else None
        if ver is None:
            return {"state": "none", "reason": "file not found"}
        _, pid = self._profile(kind, root, path)
        key = item_key(ver, pid)
        idx = self.index()
        if idx is not None and idx.files_ok(key):
            return dict(self.entry_view(key, idx.get(key)), state="done")
        it = self.queue.items.get(key)
        if it is None:
            return {"state": "none", "key": key}
        st = it.get("state")
        out = {"key": key, "lane": it.get("lane"), "attempts": it.get("attempts", 0)}
        if st == "queued":
            pos = self.queue.position(key)
            return dict(out, state="queued", position=pos, ahead=(pos or 1) - 1,
                        retry_in_s=max(0, round(float(it.get("not_before") or 0) - self.clock())) or None,
                        error=it.get("error"))
        if st == "failed":
            return dict(out, state="failed", error=it.get("error"))
        if st == "held":
            return dict(out, state="held", phase="held")
        prog = it.get("progress") or {}
        return dict(out, state="converting", phase=it.get("worker_state") or st, progress=prog,
                    eta_s=prog.get("eta_s"))

    def overview(self):
        """For the settings card: queue, worker, cache."""
        idx = self.index()
        cfg = self.cfg()
        items = []
        for it in self.queue.ordered(("submitted", "downloading", "uploading", "held")) + self.queue.ordered() + \
                self.queue.ordered(("failed",)):
            items.append({k: it.get(k) for k in ("key", "root", "path", "kind", "lane", "state", "attempts", "error",
                                                 "worker_state", "progress", "not_before", "at")})
        cap = float(cfg.get("cache_max_gb") or 0)
        cache = {"dir": self.cache_dir()[0], "problem": self.problem, "max_gb": cap,
                 "entries": len(idx.entries) if idx else 0,
                 "gb": round(idx.total_bytes() / 1024 ** 3, 2) if idx else 0.0}
        return {"enabled": bool(cfg.get("enabled", True)), "items": items, "cache": cache,
                "worker": {"error": self.worker_error, "status": self.last_status,
                           "retry_in_s": max(0, round(self.down_until - self.clock()))},
                "last_step": self.last_step, "backfill": self.last_backfill}


def _listdir(path):
    try:
        return os.listdir(path)
    except OSError:
        return []


def _age(path, now):
    try:
        return now - os.path.getmtime(path)
    except OSError:
        return 0
