"""Exact-copy image de-duplication: the decisions and the file operations,
without Flask and without the trash (server.py owns those).

What it does (user, 2026-10-27):
  1. The same picture (byte for byte) under several names in DIFFERENT folders:
     one file, the other names are hard links to it. Every name stays.
  2. A FOLDER holds at most one name per picture: the oldest stays, the others
     go to the trash with a ban (server.py does that part).
  Resized or re-compressed look-alikes are out of scope.

Cost on spinning disks: the library is walked once per pass (a stat per image,
no reads); only files whose SIZE collides with another file's are looked at,
one thread, in inode order (close together on the disk): first a digest of the
first and last 64 KiB (a whole small file), and only files whose size AND quick
digest collide are read in full - same-size look-alikes are mostly settled by
two small reads. Every digest is remembered per inode (device, inode, size,
mtime) in a sqlite file, so a later pass or the check after a download never
reads an unchanged file again. The walk's result is kept in the same sqlite
file (table `files`), which is how a finished download finds the pictures of
the same size in other folders without walking anything.

Hard links share ONE inode, so they share size, mtime and content. A name that
becomes a link takes the older file's mtime (the oldest copy is the one that
stays): in a list sorted by date it moves back to when the picture first
appeared. Linking bumps the stay-file's ctime (what "recently downloaded" is
built from) - the recent list is a stored list, so nothing new shows up there.

Run by hand for a read-only report (writes only its own sqlite file):
  python image_dedup.py report <downloads dir> <db file> [--limit N]
"""
import collections
import filecmp
import hashlib
import json
import os
import re
import secrets
import sqlite3
import sys
import threading
import time

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".svg"}   # server.py MEDIA_EXTENSIONS["image"]
# Folders under the downloads root that are in scope: the downloaders whose ban
# works (gallery-dl tombstone, PixivUtil2 rows). Everything else - youtube
# thumbnails and covers, the Steam Frame folder, music_videos, the upscale
# bake-off - is neither trashed from nor linked.
CATEGORIES = ("twitter", "danbooru", "civitai", "bluesky", "patreon", "deviantart", "discord", "pixiv", "instagram")
TEMP_PREFIX = ".dedup-"
HASH_CHUNK = 1 << 20
QUICK_SPAN = 64 * 1024                # a file over twice this is first compared by its first and last QUICK_SPAN bytes
MAX_LINKS = 60000                     # ext4 allows 65000 names per inode

Entry = collections.namedtuple("Entry", "rel size mtime_ns dev ino nlink")


def accept_name(name):
    """The default 'is this a picture to consider': an image extension, not a
    hidden or partial file, not a '-poster' companion. server.py passes its own
    (kinds_of + _is_hidden_or_partial)."""
    low = name.lower()
    stem, ext = os.path.splitext(low)
    return (ext in IMAGE_EXTS and not low.startswith(".") and not low.endswith(".part")
            and not stem.endswith("-poster"))


def lower_priority():
    """Thread initializer: nice 10 and the lowest best-effort I/O class, so a
    pass over the spinning disks gives way to anyone playing from them."""
    try:
        os.nice(10)
    except OSError:
        pass
    try:                               # ioprio_set(IOPRIO_WHO_PROCESS, this thread, BE class, level 7)
        import ctypes
        import platform
        number = {"x86_64": 251, "aarch64": 30}.get(platform.machine())
        if number:
            ctypes.CDLL(None, use_errno=True).syscall(number, 1, 0, (2 << 13) | 7)
    except Exception:
        pass


# --------------------------------------------------------------- walking ----

def walk(base, categories=CATEGORIES, accept=accept_name, cancel=None, stats=None):
    """Every candidate picture under base/<category>/..., as Entry tuples (rel is
    relative to base, '/'-separated). No dot folder is entered (the trash), no
    symlink followed, no empty file (a ban tombstone) kept. One stat per file."""
    stats = stats if stats is not None else {}
    for category in categories:
        top = os.path.join(base, category)
        stack = [(top, category)]
        while stack:
            if cancel and cancel():
                return
            path, rel_dir = stack.pop()
            try:
                it = os.scandir(path)
            except OSError:
                continue
            with it:
                for de in it:
                    name = de.name
                    try:
                        if de.is_symlink():
                            continue
                        if de.is_dir(follow_symlinks=False):
                            if not name.startswith("."):
                                stack.append((de.path, rel_dir + "/" + name))
                            continue
                        if name.startswith(TEMP_PREFIX):
                            stats.setdefault("temps", []).append(rel_dir + "/" + name)
                            continue
                        if not accept(name):
                            continue
                        st = de.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    if st.st_size == 0:
                        stats["tombstones"] = stats.get("tombstones", 0) + 1
                        continue
                    yield Entry(rel_dir + "/" + name, st.st_size, st.st_mtime_ns, st.st_dev, st.st_ino, st.st_nlink)


def scan_folder(base, rel_dir, accept=accept_name):
    """Entries of the pictures directly in one folder."""
    out = []
    try:
        with os.scandir(os.path.join(base, *rel_dir.split("/"))) as it:
            for de in it:
                try:
                    if de.is_symlink() or de.is_dir(follow_symlinks=False):
                        continue
                    if de.name.startswith(TEMP_PREFIX) or not accept(de.name):
                        continue
                    st = de.stat(follow_symlinks=False)
                except OSError:
                    continue
                if st.st_size:
                    out.append(Entry(rel_dir + "/" + de.name, st.st_size, st.st_mtime_ns, st.st_dev, st.st_ino,
                                     st.st_nlink))
    except OSError:
        pass
    return out


# ------------------------------------------------------------- the store ----

class Store:
    """sqlite: `files` (the last walk: rel, size, mtime, inode), `hashes` (sha256
    per inode, valid while size and mtime still match), `exempt` (names restored
    from a dedup batch on purpose - never trashed again)."""

    def __init__(self, path):
        self.path = path
        self.lock = threading.RLock()
        self.conn = sqlite3.connect(path, check_same_thread=False, timeout=60)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS files(rel TEXT PRIMARY KEY, folder TEXT, size INTEGER, mtime_ns INTEGER,
                                             dev INTEGER, ino INTEGER, nlink INTEGER) WITHOUT ROWID;
            CREATE INDEX IF NOT EXISTS files_size ON files(size);
            CREATE INDEX IF NOT EXISTS files_folder ON files(folder);
            CREATE TABLE IF NOT EXISTS hashes(dev INTEGER, ino INTEGER, size INTEGER, mtime_ns INTEGER, sha TEXT,
                                              PRIMARY KEY(dev, ino)) WITHOUT ROWID;
            CREATE TABLE IF NOT EXISTS exempt(rel TEXT PRIMARY KEY) WITHOUT ROWID;
        """)
        if "quick" not in {r[1] for r in self.conn.execute("PRAGMA table_info(hashes)")}:
            self.conn.execute("ALTER TABLE hashes ADD COLUMN quick TEXT")
        self.conn.commit()

    def close(self):
        with self.lock:
            self.conn.close()

    # files
    def replace_files(self, entries):
        with self.lock:
            self.conn.execute("DELETE FROM files")
            self.conn.executemany("INSERT OR REPLACE INTO files VALUES(?,?,?,?,?,?,?)",
                                  ((e.rel, e.rel.rsplit("/", 1)[0], *e[1:]) for e in entries))
            self.conn.commit()

    def replace_folder(self, rel_dir, entries):
        with self.lock:
            self.conn.execute("DELETE FROM files WHERE folder = ?", (rel_dir,))
            self.conn.executemany("INSERT OR REPLACE INTO files VALUES(?,?,?,?,?,?,?)",
                                  ((e.rel, rel_dir, *e[1:]) for e in entries))
            self.conn.commit()

    def with_size(self, size):
        with self.lock:
            return [Entry(*r) for r in self.conn.execute(
                "SELECT rel, size, mtime_ns, dev, ino, nlink FROM files WHERE size = ?", (size,))]

    def file_count(self):
        with self.lock:
            return self.conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]

    # hashes: (sha, quick) of an inode, each None until read; valid while size and mtime still match
    def get_hash(self, e):
        with self.lock:
            row = self.conn.execute("SELECT size, mtime_ns, sha, quick FROM hashes WHERE dev=? AND ino=?",
                                    (e.dev, e.ino)).fetchone()
        return (row[2], row[3]) if row and row[0] == e.size and row[1] == e.mtime_ns else (None, None)

    def put_hash(self, e, sha=None, quick=None):
        old_sha, old_quick = self.get_hash(e)
        with self.lock:
            self.conn.execute("INSERT OR REPLACE INTO hashes(dev, ino, size, mtime_ns, sha, quick) VALUES(?,?,?,?,?,?)",
                              (e.dev, e.ino, e.size, e.mtime_ns, sha or old_sha, quick or old_quick))
            self.conn.commit()

    def prune_hashes(self, live):
        """Forget the hashes of inodes no walk saw (a complete pass only)."""
        with self.lock:
            gone = [(d, i) for d, i in self.conn.execute("SELECT dev, ino FROM hashes") if (d, i) not in live]
            self.conn.executemany("DELETE FROM hashes WHERE dev=? AND ino=?", gone)
            self.conn.commit()
        return len(gone)

    # exempt
    def exempt_add(self, rels):
        with self.lock:
            self.conn.executemany("INSERT OR IGNORE INTO exempt VALUES(?)", [(r,) for r in rels])
            self.conn.commit()

    def exempt_all(self):
        with self.lock:
            return {r[0] for r in self.conn.execute("SELECT rel FROM exempt")}


def sha256_of(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            block = fh.read(HASH_CHUNK)
            if not block:
                break
            h.update(block)
    return h.hexdigest()


def quick_of(path, size):
    """Digest of the first and last QUICK_SPAN bytes; for a file of at most twice
    that, the digest of the whole file (so it is the full hash too).
    Returns (quick, sha or None)."""
    if size <= 2 * QUICK_SPAN:
        sha = sha256_of(path)
        return sha, sha
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        h.update(fh.read(QUICK_SPAN))
        fh.seek(-QUICK_SPAN, os.SEEK_END)
        h.update(fh.read(QUICK_SPAN))
    return "q" + h.hexdigest(), None


class Hasher:
    """Digests of an entry. quick(e) / full(e): from the store when this inode was
    read before and has not changed, else read (one thread - parallel reads thrash
    a spinning disk) and stored. Counts what it read: `quick_files` for the
    two-small-reads digests, `files` and `bytes` for whole files."""

    def __init__(self, store, base, cancel=None):
        self.store, self.base, self.cancel = store, base, cancel
        self.files = self.bytes = self.quick_files = self.cached = self.errors = 0

    def _path(self, e):
        return os.path.join(self.base, *e.rel.split("/"))

    def quick(self, e):
        sha, quick = self.store.get_hash(e) if self.store else (None, None)
        if quick:
            self.cached += 1
            return quick
        if sha and e.size <= 2 * QUICK_SPAN:
            self.cached += 1
            return sha
        if self.cancel and self.cancel():
            return None
        try:
            quick, sha = quick_of(self._path(e), e.size)
        except OSError:
            self.errors += 1
            return None
        if sha:
            self.files += 1
            self.bytes += e.size
        else:
            self.quick_files += 1
        if self.store:
            self.store.put_hash(e, sha, quick)
        return quick

    def full(self, e):
        sha, _quick = self.store.get_hash(e) if self.store else (None, None)
        if sha:
            return sha
        if self.cancel and self.cancel():
            return None
        try:
            sha = sha256_of(self._path(e))
        except OSError:
            self.errors += 1
            return None
        self.files += 1
        self.bytes += e.size
        if self.store:
            self.store.put_hash(e, sha, None)
        return sha


# ------------------------------------------------------------- deciding ----

_POST = re.compile(r"^(\d+)")


def post_token(rel):
    """The post a name belongs to: the digits it starts with (a tweet id, a
    pixiv id). 123_1.jpg and 123_2.jpg are two pictures of one post; the same
    picture twice IN one post is left alone (it may be pieces of one cut
    picture, or a post that meant it)."""
    m = _POST.match(rel.rsplit("/", 1)[-1])
    return m.group(1) if m else None


def keep_key(e):
    """Which of several names in ONE folder stays: the oldest post (the lowest
    leading id - tweets and pixiv ids grow with time), else the oldest mtime,
    else the name."""
    tok = post_token(e.rel)
    return (0, int(tok)) if tok else (1, 0), e.mtime_ns, e.rel


def canon_key(e):
    """Which inode all the names end up sharing: the oldest file (mtime), more
    names already on it first, then the name."""
    return e.mtime_ns, -e.nlink, e.rel


def build_plan(entries, hasher, exempt=frozenset(), progress=None, cancel=None, max_examples=40):
    """Groups of identical pictures and what to do with each.

    entries: Entry tuples (the whole library, or the few a download touched).
    hasher: a Hasher (quick(entry) / full(entry)). Only entries whose size another
    inode shares get a quick digest, and only those whose quick digest another
    inode shares are read in full; names on one inode need no reading at all.
    Returns {"groups": [...], "stats": {...}}. Reads no more than hashing."""
    t0 = time.time()
    stats = collections.Counter()
    clusters = collections.defaultdict(list)             # (dev, ino) -> names
    for e in entries:
        clusters[(e.dev, e.ino)].append(e)
        stats["names"] += 1
    stats["clusters"] = len(clusters)
    by_size = collections.defaultdict(list)
    for key, names in clusters.items():
        by_size[names[0].size].append(key)
    candidates = [key for keys in by_size.values() if len(keys) > 1 for key in keys]
    stats["size_collisions"] = len(candidates)
    stats["size_collision_bytes"] = sum(clusters[k][0].size for k in candidates)
    # In inode order: names created together sit together on the disk.
    quick_of_key = {}
    for n, key in enumerate(sorted(candidates)):
        if cancel and cancel():
            break
        q = hasher.quick(clusters[key][0])
        if q:
            quick_of_key[key] = (clusters[key][0].size, q)
        if progress and n % 500 == 0:
            progress("quick digests", stats)
    by_quick = collections.defaultdict(list)
    for key, qk in quick_of_key.items():
        by_quick[qk].append(key)
    sha_of = {}
    needed = sorted(k for keys in by_quick.values() if len(keys) > 1 for k in keys)
    stats["quick_collisions"] = len(needed)
    for n, key in enumerate(needed):
        if cancel and cancel():
            break
        sha = hasher.full(clusters[key][0])
        if sha:
            sha_of[key] = sha
        if progress and n % 500 == 0:
            progress("full digests", stats)
    content = collections.defaultdict(list)               # content key -> cluster keys
    for key, names in clusters.items():
        content[sha_of.get(key) or ("inode", key)].append(key)

    groups = []
    for ckey, keys in content.items():
        names = [e for k in keys for e in clusters[k]]
        if len(names) < 2:
            continue
        by_folder = collections.defaultdict(list)
        for e in names:
            by_folder[e.rel.rsplit("/", 1)[0]].append(e)
        if len(keys) < 2 and all(len(v) < 2 for v in by_folder.values()):
            continue                                       # one inode, one name per folder: nothing to do
        keep, trash, skipped = [], [], []
        for folder, es in by_folder.items():
            es.sort(key=keep_key)
            kept = [es[0]]
            for e in es[1:]:
                if e.rel in exempt:
                    skipped.append((e.rel, "restored"))
                elif any(post_token(e.rel) and post_token(e.rel) == post_token(k.rel) for k in kept):
                    skipped.append((e.rel, "same post"))
                else:
                    trash.append((e, kept[0]))
                    continue
                kept.append(e)
            keep.extend(kept)
        canon = min(keep, key=canon_key)
        cc = (canon.dev, canon.ino)
        links = [e for e in keep if (e.dev, e.ino) != cc]
        trash_links = [e for e, _ in trash if (e.dev, e.ino) != cc]
        if not links and not trash:
            for _rel, why in skipped:
                stats["left_" + why.replace(" ", "_")] += 1
            continue
        distinct = len(keys)
        groups.append({
            "sha": ckey if isinstance(ckey, str) else None,
            "size": canon.size,
            "canon": canon,
            "links": links,                                # surviving names to point at the canon
            "trash": trash,                                # [(entry, keeper entry)] - linked first, then trashed
            "trash_links": trash_links,
            "skipped": skipped,
            "saved": (distinct - 1) * canon.size,
        })
        stats["groups"] += 1
        stats["trash_names"] += len(trash)
        stats["link_names"] += len(links)
        stats["trash_link_names"] += len(trash_links)
        stats["saved_bytes"] += (distinct - 1) * canon.size
        stats["inodes_freed"] += distinct - 1
        for _rel, why in skipped:
            stats["left_" + why.replace(" ", "_")] += 1
    groups.sort(key=lambda g: (-g["saved"], g["canon"].rel))
    stats["plan_seconds"] = round(time.time() - t0, 1)
    examples = []
    for g in groups[:max_examples]:
        examples.append({"size": g["size"], "keep": g["canon"].rel, "saved": g["saved"],
                         "link": [e.rel for e in g["links"]][:6],
                         "trash": [[e.rel, k.rel] for e, k in g["trash"]][:6],
                         "left": g["skipped"][:4]})
    return {"groups": groups, "stats": dict(stats), "examples": examples}


# ------------------------------------------------------------- acting ------

class Refused(Exception):
    pass


def relink(base, canon, e):
    """Makes name e a hard link to canon's file. Refuses (Refused) unless both
    are still what the plan saw (size, inode, mtime), on one device, and the
    two files really are byte-identical - the data under e's old inode is gone
    once its last name is replaced, so nothing is taken on the hash alone.
    Atomic: a link under a temporary name, then os.replace."""
    cpath = os.path.join(base, *canon.rel.split("/"))
    path = os.path.join(base, *e.rel.split("/"))
    try:
        cst, st = os.lstat(cpath), os.lstat(path)
    except OSError as exc:
        raise Refused(f"gone: {exc}")
    for ent, s in ((canon, cst), (e, st)):
        if (s.st_size, s.st_ino, s.st_dev, s.st_mtime_ns) != (ent.size, ent.ino, ent.dev, ent.mtime_ns):
            raise Refused(f"changed since the plan: {ent.rel}")
    if (cst.st_dev, cst.st_ino) == (st.st_dev, st.st_ino):
        return False                                       # already one file
    if cst.st_dev != st.st_dev:
        raise Refused("different devices")
    if cst.st_nlink >= MAX_LINKS:
        raise Refused("too many links on the file")
    if not filecmp.cmp(cpath, path, shallow=False):
        raise Refused("contents differ")
    tmp = os.path.join(os.path.dirname(path), TEMP_PREFIX + secrets.token_hex(4))
    try:
        os.link(cpath, tmp)
        os.replace(tmp, path)
    except OSError as exc:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise Refused(f"link failed: {exc}")
    return True


def sweep_temps(base, rels):
    """Temporary link names a stopped pass left behind. Each is only one more
    name of a file that has others - removing it loses nothing. Call it while
    no apply is running."""
    n = 0
    for rel in rels:
        try:
            os.unlink(os.path.join(base, *rel.split("/")))
            n += 1
        except OSError:
            pass
    return n


def set_ai_inodes(conn, root, pairs):
    """The AI index rows of names that became links now name the shared file:
    pairs = [(rel, 'dev:ino')]. Best effort; returns rows touched."""
    n = 0
    for rel, ino in pairs:
        n += conn.execute("UPDATE media SET ino = ? WHERE root = ? AND path = ?", (ino, root, rel)).rowcount
    conn.commit()
    return n


# ----------------------------------------------------------- the report ----

def report(base, db_path, limit=0, log=print):
    """Read-only dry run for a shell: walk, hash size collisions, plan; nothing
    but the sqlite file at db_path is written."""
    lower_priority()
    store = Store(db_path)
    walk_stats = {}
    t0 = time.time()
    entries = []
    for e in walk(base, stats=walk_stats):
        entries.append(e)
        if limit and len(entries) >= limit:
            break
        if len(entries) % 50000 == 0:
            log(f"walked {len(entries)} ({time.time() - t0:.0f}s)")
    walk_s = time.time() - t0
    store.replace_files(entries)
    hasher = Hasher(store, base)
    t1 = time.time()
    seen = [0]

    def progress(phase, _stats):
        if hasher.files - seen[0] >= 2000:
            seen[0] = hasher.files
            log(f"{phase}: read {hasher.files} files, {hasher.bytes / 1e9:.1f} GB ({time.time() - t1:.0f}s)")

    plan = build_plan(entries, hasher, store.exempt_all(), progress=progress)
    out = dict(plan["stats"], walk_seconds=round(walk_s, 1), hash_seconds=round(time.time() - t1, 1),
               hashed_files=hasher.files, hashed_bytes=hasher.bytes, quick_files=hasher.quick_files,
               hash_cached=hasher.cached, hash_errors=hasher.errors, tombstones=walk_stats.get("tombstones", 0),
               stale_temps=len(walk_stats.get("temps", [])), images=len(entries),
               image_bytes=sum(e.size for e in entries))
    out["examples"] = plan["examples"]
    return out


if __name__ == "__main__":
    if len(sys.argv) >= 4 and sys.argv[1] == "report":
        lim = int(sys.argv[sys.argv.index("--limit") + 1]) if "--limit" in sys.argv else 0
        print(json.dumps(report(sys.argv[2], sys.argv[3], lim, log=lambda m: print(m, file=sys.stderr, flush=True)),
                         indent=1, ensure_ascii=False))
    else:
        print(__doc__)
