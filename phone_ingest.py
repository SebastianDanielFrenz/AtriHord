"""Receive the phone's photo and video backup: the app's replacement for
Immich's phone sync.

Files land in <dest>/<phone folder>/<original name>, the same shape the Immich
export has (KSF-055), so a new Camera photo sits next to the old ones. The
transfer is resumable because phones drop off Wi-Fi in the middle of a
gigabyte of video, and every route answers to the same credentials as
/api/media/ingest (admin session or X-API-Key; 401 without, 403 for guests).

    POST /api/phone/have    {"files": [{"folder", "name", "size"}, ...]}
                            -> {"have": [true, false, ...]}
    POST /api/phone/begin   {"folder", "name", "size", "sha256", "mtime"}
                            -> {"status": "duplicate"}
                             | {"status": "ready", "id", "offset"}
    PUT  /api/phone/chunk/<id>?offset=N        raw bytes, Content-Length set
                            -> {"offset": N'}    409 {"offset": real} if N is stale
    POST /api/phone/finish/<id>
                            -> 201 {"status": "stored", "name"}

Invariant that makes resuming trivial: the part file only ever grows by a whole
chunk that arrived complete. A chunk whose body is cut short is dropped without
touching the file, so the offset the server reports is always one the client
can continue from.
"""
import hashlib
import json
import os
import re
import shutil
import sqlite3
import threading
import time
import unicodedata

from flask import jsonify, request

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".heic", ".heif",
                    ".avif", ".bmp", ".tif", ".tiff", ".dng"}
VIDEO_EXTENSIONS = {".mp4", ".m4v", ".mov", ".3gp", ".3g2", ".webm", ".mkv",
                    ".avi", ".mts", ".m2ts"}
# An allowlist, not a courtesy: this is a directory a web server can see.
ALLOWED_EXTENSIONS = IMAGE_EXTENSIONS | VIDEO_EXTENSIONS

FOLDER_FALLBACK = "Unsorted"
MAX_BYTES = 32 * 1024 ** 3
MAX_CHUNK = 32 * 1024 ** 2
FREE_RESERVE = 20 * 1024 ** 3        # never fill the volume with a backup
PART_TTL = 7 * 24 * 3600
INCOMING = ".incoming"               # dot-names are skipped by every walker
INVALIDATE_AFTER = 20                # seconds: one cache reset per burst, not per file
HASH_DB = "phone_uploads.sqlite3"

_CTRL = re.compile(r"[\x00-\x1f\x7f/\\]")
_CLASH = re.compile(r" \((\d+)\)$")
_SID = re.compile(r"^[0-9a-f]{32}$")
_SHA = re.compile(r"^[0-9a-f]{64}$")


def clean(raw, fallback, limit=200):
    """One path component. Unicode and '@' survive (the exported folders are
    full of both); separators and control characters do not, and a leading dot
    goes because the walkers hide dot-names and '..' is not a name."""
    s = unicodedata.normalize("NFC", raw if isinstance(raw, str) else "")
    s = _CTRL.sub("_", s).strip().lstrip(".").strip()
    if not s:
        return fallback
    if len(s.encode("utf-8")) > limit:
        stem, ext = os.path.splitext(s)
        ext = ext[:16]
        while stem and len((stem + ext).encode("utf-8")) > limit:
            stem = stem[:-1]
        s = (stem + ext) or fallback
    return s


def base_key(name):
    """Name without a trailing ' (2)' clash counter and with a lower-case
    extension: the Immich export added the counter when two assets shared a
    name, and so does finish() below, so they compare equal here."""
    stem, ext = os.path.splitext(name)
    return _CLASH.sub("", stem) + ext.lower()


class FolderIndex:
    """(base name, size) of every file in a folder, rebuilt when the folder's
    own mtime moves. 'Do you already have these 200 files' is then one
    directory listing, not 200 stats."""

    LIMIT = 512

    def __init__(self):
        self._lock = threading.Lock()
        self._cache = {}

    def _keys(self, path):
        try:
            mtime = os.stat(path).st_mtime_ns
        except OSError:
            return frozenset()
        with self._lock:
            hit = self._cache.get(path)
        if hit and hit[0] == mtime:
            return hit[1]
        keys = set()
        try:
            with os.scandir(path) as it:
                for e in it:
                    if e.name.startswith(".") or not e.is_file(follow_symlinks=False):
                        continue
                    try:
                        keys.add((base_key(e.name), e.stat(follow_symlinks=False).st_size))
                    except OSError:
                        pass
        except OSError:
            return frozenset()
        keys = frozenset(keys)
        with self._lock:
            if len(self._cache) >= self.LIMIT:
                self._cache.clear()
            self._cache[path] = (mtime, keys)
        return keys

    def has(self, folder_path, name, size):
        return (base_key(name), size) in self._keys(folder_path)


class Hashes:
    """Content hashes of everything this module stored. Deliberately remembers
    a hash after the file is deleted: a photo removed from the library on
    purpose must not come back because the phone was reinstalled."""

    def __init__(self, path):
        self.path = path
        self._lock = threading.Lock()
        with self._lock:
            db = self._conn()
            try:
                with db:
                    db.execute("CREATE TABLE IF NOT EXISTS uploads ("
                               "sha256 TEXT PRIMARY KEY, rel TEXT NOT NULL, "
                               "size INTEGER NOT NULL, stored INTEGER NOT NULL)")
            finally:
                db.close()

    def _conn(self):
        return sqlite3.connect(self.path, timeout=15)

    def known(self, sha):
        with self._lock:
            db = self._conn()
            try:
                return db.execute("SELECT 1 FROM uploads WHERE sha256=?", (sha,)).fetchone() is not None
            finally:
                db.close()

    def add(self, sha, rel, size):
        with self._lock:
            db = self._conn()
            try:
                with db:
                    db.execute("INSERT OR REPLACE INTO uploads VALUES (?,?,?,?)",
                               (sha, rel, size, int(time.time())))
            finally:
                db.close()


class Debounce:
    def __init__(self, fn, delay):
        self._fn, self._delay = fn, delay
        self._lock = threading.Lock()
        self._timer = None

    def poke(self):
        with self._lock:
            if self._timer is not None:
                return
            self._timer = threading.Timer(self._delay, self._fire)
            self._timer.daemon = True
            self._timer.start()

    def _fire(self):
        with self._lock:
            self._timer = None
        try:
            self._fn()
        except Exception:
            pass


class PhoneIngest:
    def __init__(self, dest_dir, state_dir, invalidate_caches, invalidate_after=INVALIDATE_AFTER):
        self.root = os.path.abspath(dest_dir)
        self.incoming = os.path.join(self.root, INCOMING)
        self.folders = FolderIndex()
        self.hashes = Hashes(os.path.join(state_dir, HASH_DB))
        self.invalidate = Debounce(invalidate_caches, invalidate_after)
        self._locks = {}
        self._locks_guard = threading.Lock()
        self._name_lock = threading.Lock()

    def lock_for(self, sid):
        with self._locks_guard:
            return self._locks.setdefault(sid, threading.Lock())

    def part(self, sid):
        return os.path.join(self.incoming, sid + ".part")

    def meta(self, sid):
        return os.path.join(self.incoming, sid + ".json")

    def read_meta(self, sid):
        try:
            with open(self.meta(sid), encoding="utf-8") as fh:
                return json.load(fh)
        except (OSError, ValueError):
            return None

    def drop(self, sid):
        for p in (self.part(sid), self.meta(sid)):
            try:
                os.unlink(p)
            except OSError:
                pass

    def sweep(self):
        cutoff = time.time() - PART_TTL
        try:
            for e in os.scandir(self.incoming):
                try:
                    if e.stat().st_mtime < cutoff:
                        os.unlink(e.path)
                except OSError:
                    pass
        except OSError:
            pass

    def unique(self, dest, name):
        stem, ext = os.path.splitext(name)
        candidate, n = name, 1
        while os.path.exists(os.path.join(dest, candidate)):
            n += 1
            candidate = clean(f"{stem} ({n}){ext}", name)
        return candidate


def register(app, get_user, is_admin, dest_dir, state_dir, invalidate_caches,
             invalidate_after=INVALIDATE_AFTER):
    ing = (PhoneIngest(dest_dir, state_dir, invalidate_caches, invalidate_after)
           if dest_dir else None)

    def gate():
        """None when the caller may proceed, else the response to send."""
        user = get_user()
        if not user:
            return jsonify({"error": "Unauthorized"}), 401
        if not is_admin(user):
            return jsonify({"error": "Forbidden"}), 403
        if ing is None or not os.path.isdir(ing.root):
            return jsonify({"error": "phone upload is not configured"}), 503
        return None

    @app.route("/api/phone/have", methods=["POST"])
    def phone_have():
        denied = gate()
        if denied:
            return denied
        files = (request.get_json(silent=True) or {}).get("files")
        if not isinstance(files, list) or len(files) > 1000:
            return jsonify({"error": "files: a list of at most 1000"}), 400
        out = []
        for f in files:
            f = f if isinstance(f, dict) else {}
            folder = clean(f.get("folder"), FOLDER_FALLBACK, 120)
            name = clean(f.get("name"), "")
            size = f.get("size")
            out.append(bool(name) and isinstance(size, int)
                       and ing.folders.has(os.path.join(ing.root, folder), name, size))
        return jsonify({"have": out})

    @app.route("/api/phone/begin", methods=["POST"])
    def phone_begin():
        denied = gate()
        if denied:
            return denied
        b = request.get_json(silent=True) or {}
        folder = clean(b.get("folder"), FOLDER_FALLBACK, 120)
        name = clean(b.get("name"), "")
        size, sha, mtime = b.get("size"), str(b.get("sha256") or "").lower(), b.get("mtime") or 0
        if not name or not isinstance(size, int) or isinstance(size, bool) or size <= 0:
            return jsonify({"error": "bad-name-or-size"}), 400
        if os.path.splitext(name)[1].lower() not in ALLOWED_EXTENSIONS:
            return jsonify({"error": "bad-type"}), 400
        if not _SHA.match(sha):
            return jsonify({"error": "bad-sha256"}), 400
        if size > MAX_BYTES:
            return jsonify({"error": "too-large"}), 413
        if ing.folders.has(os.path.join(ing.root, folder), name, size) or ing.hashes.known(sha):
            return jsonify({"status": "duplicate"})
        if shutil.disk_usage(ing.root).free - size < FREE_RESERVE:
            return jsonify({"error": "no-space"}), 507
        os.makedirs(ing.incoming, exist_ok=True)
        ing.sweep()
        sid = hashlib.sha256(f"{folder}\0{name}\0{size}\0{sha}".encode("utf-8")).hexdigest()[:32]
        with ing.lock_for(sid):
            part = ing.part(sid)
            if not os.path.exists(part):
                open(part, "wb").close()
            with open(ing.meta(sid), "w", encoding="utf-8") as fh:
                json.dump({"folder": folder, "name": name, "size": size, "sha256": sha,
                           "mtime": mtime if isinstance(mtime, int) else 0}, fh)
            have = os.path.getsize(part)
            if have > size:                      # cannot happen unless something else wrote here
                os.truncate(part, 0)
                have = 0
        return jsonify({"status": "ready", "id": sid, "offset": have})

    @app.route("/api/phone/chunk/<sid>", methods=["PUT"])
    def phone_chunk(sid):
        denied = gate()
        if denied:
            return denied
        meta = ing.read_meta(sid) if _SID.match(sid) else None
        if meta is None or not os.path.exists(ing.part(sid)):
            return jsonify({"error": "unknown-upload"}), 404
        try:
            offset = int(request.args.get("offset", ""))
        except ValueError:
            return jsonify({"error": "offset"}), 400
        length = request.content_length
        if length is None or length <= 0:
            return jsonify({"error": "content-length"}), 411
        if length > MAX_CHUNK:
            return jsonify({"error": "chunk-too-large"}), 413
        # The body is read BEFORE the lock is taken. A phone that falls off the
        # network leaves this thread waiting on a half-open connection for as
        # long as the TCP stack likes; if it held the upload's lock meanwhile,
        # the client's retry (begin, then this route again) would queue behind
        # a request nobody is sending to any more.
        body = bytearray()
        try:
            while len(body) < length:
                piece = request.stream.read(min(1 << 20, length - len(body)))
                if not piece:
                    break
                body += piece
        except Exception:
            # Werkzeug's ClientDisconnected, or a reset socket, depending on the
            # server in front of it: either way the chunk is short.
            pass
        with ing.lock_for(sid):
            part = ing.part(sid)
            try:
                have = os.path.getsize(part)
            except OSError:
                return jsonify({"error": "unknown-upload"}), 404
            if len(body) < length:               # the client went away mid-chunk: keep none of it
                return jsonify({"error": "short-body", "offset": have}), 400
            if offset != have:
                return jsonify({"error": "offset", "offset": have}), 409
            if have + length > meta["size"]:
                return jsonify({"error": "overrun", "offset": have}), 400
            with open(part, "ab") as fh:
                fh.write(body)
        return jsonify({"offset": have + length})

    @app.route("/api/phone/finish/<sid>", methods=["POST"])
    def phone_finish(sid):
        denied = gate()
        if denied:
            return denied
        meta = ing.read_meta(sid) if _SID.match(sid) else None
        if meta is None or not os.path.exists(ing.part(sid)):
            return jsonify({"error": "unknown-upload"}), 404
        with ing.lock_for(sid):
            part = ing.part(sid)
            have = os.path.getsize(part)
            if have != meta["size"]:
                return jsonify({"error": "incomplete", "offset": have}), 409
            h = hashlib.sha256()
            with open(part, "rb") as fh:
                for block in iter(lambda: fh.read(1 << 20), b""):
                    h.update(block)
            if h.hexdigest() != meta["sha256"]:
                ing.drop(sid)
                return jsonify({"error": "checksum"}), 422
            dest = os.path.join(ing.root, meta["folder"])
            os.makedirs(dest, exist_ok=True)
            with ing._name_lock:
                name = ing.unique(dest, meta["name"])
                final = os.path.join(dest, name)
                os.replace(part, final)
            stamp = meta.get("mtime") or 0
            if 0 < stamp / 1000.0 <= time.time() + 86400:
                os.utime(final, (stamp / 1000.0, stamp / 1000.0))
            ing.hashes.add(meta["sha256"], meta["folder"] + "/" + name, meta["size"])
            try:
                os.unlink(ing.meta(sid))
            except OSError:
                pass
        ing.invalidate.poke()
        return jsonify({"status": "stored", "name": name}), 201

    return ing
