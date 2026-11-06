"""A date index of every image and video in the library, for the Timeline.

One row per file: (root, path, ts, kind, size). ts is the file's mtime in whole
seconds - the CONTENT's own date (downloaders stamp the source's timestamp onto
it, phone uploads keep the capture time), which is what a timeline wants. When
a file landed on the disk is a different question, answered by /api/media/recent.

Why a table at all: the media index the search uses holds paths only, and a
stat of ~300k files per request is out of the question. A background scan fills
this once, and re-scans every few hours; a month is then one indexed query.

The scan is a plain walk with os.scandir, throttled so it never competes with
a download or a playback for the disk. It writes in batches and removes the
rows of files it no longer sees, per root, only after that root's walk finished
(a half-finished walk must never delete anything).

Standalone on purpose (no import of server.py): the server hands in what only
it knows - the roots, "is this name hidden/partial", "what type is this" - as
callables, so this file can be tested alone.
"""
import os
import sqlite3
import threading
import time

DB_NAME = "media_dates.sqlite"
RESCAN_SECONDS = 6 * 3600
THROTTLE_EVERY = 400          # files between short sleeps
THROTTLE_SLEEP = 0.02
BATCH = 2000

_lock = threading.Lock()
_state = {"state": "idle", "root": "", "files": 0, "started": 0.0, "finished": 0.0, "error": ""}
_wake = threading.Event()


def _connect(base_dir):
    conn = sqlite3.connect(os.path.join(base_dir, DB_NAME), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS dates ("
        " root TEXT NOT NULL, path TEXT NOT NULL, ts INTEGER NOT NULL,"
        " kind TEXT NOT NULL, size INTEGER, seen INTEGER NOT NULL,"
        " PRIMARY KEY (root, path))")
    conn.execute("CREATE INDEX IF NOT EXISTS dates_ts ON dates (ts)")
    conn.execute("CREATE INDEX IF NOT EXISTS dates_seen ON dates (root, seen)")
    return conn


def status():
    with _lock:
        return dict(_state)


def request_rescan():
    _wake.set()


def _set(**kw):
    with _lock:
        _state.update(kw)


def scan_root(conn, root_id, base, scan_id, hidden_name, kind_of, poster_names):
    """Walk one root; upsert every image/video. Returns the file count."""
    count = 0
    batch = []
    stack = [("", base)]
    while stack:
        rel_dir, full_dir = stack.pop()
        try:
            it = list(os.scandir(full_dir))
        except OSError:
            continue
        names = [e.name for e in it if e.is_file(follow_symlinks=False)]
        posters = poster_names(names)
        for e in it:
            name = e.name
            if name.startswith("."):
                continue                       # the trash, partial downloads
            try:
                if e.is_dir(follow_symlinks=False):
                    stack.append(("/".join(p for p in (rel_dir, name) if p), e.path))
                    continue
                if not e.is_file(follow_symlinks=False):
                    continue
            except OSError:
                continue
            if hidden_name(name) or name in posters:
                continue
            kind = kind_of(rel_dir, name, root_id)
            if kind not in ("image", "video"):
                continue
            try:
                st = e.stat(follow_symlinks=False)
            except OSError:
                continue
            if st.st_size == 0:
                continue                       # a ban tombstone
            rel = "/".join(p for p in (rel_dir, name) if p)
            batch.append((root_id, rel, int(st.st_mtime), kind, st.st_size, scan_id))
            count += 1
            if len(batch) >= BATCH:
                _flush(conn, batch)
                batch = []
            if count % THROTTLE_EVERY == 0:
                time.sleep(THROTTLE_SLEEP)
                _set(files=_state["files"] + THROTTLE_EVERY)
    if batch:
        _flush(conn, batch)
    return count


def _flush(conn, batch):
    conn.executemany(
        "INSERT INTO dates (root, path, ts, kind, size, seen) VALUES (?,?,?,?,?,?)"
        " ON CONFLICT(root, path) DO UPDATE SET ts=excluded.ts, kind=excluded.kind,"
        " size=excluded.size, seen=excluded.seen", batch)
    conn.commit()


def scan_once(base_dir, roots, hidden_name, kind_of, poster_names):
    conn = _connect(base_dir)
    try:
        scan_id = int(time.time())
        _set(state="scanning", files=0, started=time.time(), error="")
        for r in roots():
            base = r["path"]
            if not os.path.isdir(base):
                continue            # an unmounted root: keep its rows
            _set(root=r["id"])
            scan_root(conn, r["id"], base, scan_id, hidden_name, kind_of, poster_names)
            conn.execute("DELETE FROM dates WHERE root = ? AND seen < ?", (r["id"], scan_id))
            conn.commit()
        _set(state="idle", root="", finished=time.time())
    finally:
        conn.close()


def scan_loop(base_dir, roots, hidden_name, kind_of, poster_names):
    """Daemon thread body: scan, then sleep until the next interval or a wake."""
    time.sleep(60)                         # let the server finish its own start-up first
    while True:
        try:
            scan_once(base_dir, roots, hidden_name, kind_of, poster_names)
        except Exception as exc:           # noqa: BLE001 - never let the thread die
            _set(state="error", error=str(exc)[:200])
        _wake.wait(RESCAN_SECONDS)
        _wake.clear()


def buckets(base_dir, allowed_roots, kinds, granularity="month"):
    """[{key, count}] newest first. key is YYYY-MM (month) or YYYY (year)."""
    fmt = "%Y" if granularity == "year" else "%Y-%m"
    conn = _connect(base_dir)
    try:
        ph_r = ",".join("?" * len(allowed_roots)) or "''"
        ph_k = ",".join("?" * len(kinds)) or "''"
        rows = conn.execute(
            f"SELECT strftime('{fmt}', ts, 'unixepoch') AS k, COUNT(*) AS n FROM dates"
            f" WHERE root IN ({ph_r}) AND kind IN ({ph_k}) GROUP BY k ORDER BY k DESC",
            (*allowed_roots, *kinds)).fetchall()
        return [{"key": r["k"], "count": r["n"]} for r in rows]
    finally:
        conn.close()


def bucket_range(key):
    """(from_ts, to_ts) UTC seconds for 'YYYY' or 'YYYY-MM', or None."""
    import calendar
    try:
        if len(key) == 4:
            y = int(key)
            return calendar.timegm((y, 1, 1, 0, 0, 0)), calendar.timegm((y + 1, 1, 1, 0, 0, 0))
        y, m = int(key[:4]), int(key[5:7])
        if key[4] != "-" or not 1 <= m <= 12:
            return None
        ny, nm = (y + 1, 1) if m == 12 else (y, m + 1)
        return calendar.timegm((y, m, 1, 0, 0, 0)), calendar.timegm((ny, nm, 1, 0, 0, 0))
    except (ValueError, IndexError):
        return None


def rows_in_range(base_dir, allowed_roots, kinds, start, end, offset=0, limit=None):
    """Rows with start <= ts < end, newest first (ties by path for a stable order)."""
    conn = _connect(base_dir)
    try:
        ph_r = ",".join("?" * len(allowed_roots)) or "''"
        ph_k = ",".join("?" * len(kinds)) or "''"
        sql = (f"SELECT root, path, ts, kind, size FROM dates WHERE root IN ({ph_r})"
               f" AND kind IN ({ph_k}) AND ts >= ? AND ts < ? ORDER BY ts DESC, path")
        args = [*allowed_roots, *kinds, start, end]
        if limit is not None:
            sql += " LIMIT ? OFFSET ?"
            args += [limit, offset]
        return [dict(r) for r in conn.execute(sql, args).fetchall()]
    finally:
        conn.close()


def forget(base_dir, pairs):
    """Drop rows whose file turned out to be gone."""
    if not pairs:
        return
    conn = _connect(base_dir)
    try:
        conn.executemany("DELETE FROM dates WHERE root = ? AND path = ?", pairs)
        conn.commit()
    finally:
        conn.close()
