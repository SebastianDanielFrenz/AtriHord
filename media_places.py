"""Where the pictures were taken, for the Map.

One row per image the scan has looked at: (root, path, size, lat, lon, ts). lat/lon are NULL for a
picture without a GPS position - the row is kept so the next scan does not open that file again.
A row is looked at again only when the file's size changed.

The candidates come from the Timeline's date index (media_dates.sqlite: every image of every root, with
its size and content date), so this file walks nothing. It reads only the EXIF GPS block of
JPEG/TIFF/WebP files through Pillow (no pixel data), slowly: a short sleep every few files, so it
never competes with a download or a playback. Downloaded art carries no GPS; phone photos do.

Standalone like media_dates.py: the server hands in the roots, this file keeps its own database.
"""
import os
import sqlite3
import threading
import time

from PIL import Image

DB_NAME = "media_places.sqlite"
DATES_DB = "media_dates.sqlite"
RESCAN_SECONDS = 6 * 3600
EXTENSIONS = (".jpg", ".jpeg", ".jpe", ".tif", ".tiff", ".webp")
THROTTLE_EVERY = 25
THROTTLE_SLEEP = 0.03
BATCH = 500

_lock = threading.Lock()
_state = {"state": "idle", "checked": 0, "total": 0, "with_gps": 0, "started": 0.0, "finished": 0.0, "error": ""}
_wake = threading.Event()


def _connect(base_dir):
    conn = sqlite3.connect(os.path.join(base_dir, DB_NAME), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS places ("
        " root TEXT NOT NULL, path TEXT NOT NULL, size INTEGER, lat REAL, lon REAL, ts INTEGER,"
        " PRIMARY KEY (root, path))")
    conn.execute("CREATE INDEX IF NOT EXISTS places_gps ON places (lat) WHERE lat IS NOT NULL")
    return conn


def status():
    with _lock:
        return dict(_state)


def request_rescan():
    _wake.set()


def _set(**kw):
    with _lock:
        _state.update(kw)


def _ratio(v):
    try:
        return float(v)
    except Exception:  # noqa: BLE001 - IFDRational, tuple pairs, or garbage
        try:
            return float(v[0]) / float(v[1])
        except Exception:  # noqa: BLE001
            return None


def _degrees(dms, ref):
    parts = [_ratio(x) for x in dms]
    if len(parts) != 3 or any(p is None for p in parts):
        return None
    deg = parts[0] + parts[1] / 60.0 + parts[2] / 3600.0
    return -deg if str(ref).upper() in ("S", "W") else deg


def read_gps(full_path):
    """(lat, lon) from the file's EXIF GPS block, or None. Never raises."""
    try:
        with Image.open(full_path) as im:
            gps = im.getexif().get_ifd(0x8825)
    except Exception:  # noqa: BLE001 - unreadable, truncated, not an image
        return None
    if not gps:
        return None
    try:
        lat = _degrees(gps[2], gps.get(1, "N"))
        lon = _degrees(gps[4], gps.get(3, "E"))
    except Exception:  # noqa: BLE001
        return None
    if lat is None or lon is None or abs(lat) > 90 or abs(lon) > 180 or (lat == 0 and lon == 0):
        return None
    return lat, lon


def scan_once(base_dir, roots):
    """roots() -> [{"id", "path"}]. Looks at every candidate not yet checked at its current size."""
    dates_path = os.path.join(base_dir, DATES_DB)
    if not os.path.exists(dates_path):
        return
    base_of = {r["id"]: r["path"] for r in roots()}
    conn = _connect(base_dir)
    src = sqlite3.connect(f"file:{dates_path}?mode=ro", uri=True, timeout=30)
    try:
        _set(state="scanning", checked=0, started=time.time(), error="")
        have = {(r["root"], r["path"]): r["size"] for r in conn.execute("SELECT root, path, size FROM places")}
        todo = []
        alive = set()
        for root, path, ts, size in src.execute("SELECT root, path, ts, size FROM dates WHERE kind = 'image'"):
            if not path.lower().endswith(EXTENSIONS):
                continue
            alive.add((root, path))
            if have.get((root, path), -1) != size:
                todo.append((root, path, ts, size))
        _set(total=len(todo))
        batch, n = [], 0
        for root, path, ts, size in todo:
            base = base_of.get(root)
            if not base:
                continue
            pos = read_gps(os.path.join(base, *path.split("/")))
            batch.append((root, path, size, pos[0] if pos else None, pos[1] if pos else None, ts))
            n += 1
            if len(batch) >= BATCH:
                _flush(conn, batch)
                batch = []
            if n % THROTTLE_EVERY == 0:
                time.sleep(THROTTLE_SLEEP)
                _set(checked=n)
        if batch:
            _flush(conn, batch)
        gone = [k for k in have if k not in alive and k[0] in base_of]
        for i in range(0, len(gone), 500):
            conn.executemany("DELETE FROM places WHERE root = ? AND path = ?", gone[i:i + 500])
        conn.commit()
        with_gps = conn.execute("SELECT COUNT(*) FROM places WHERE lat IS NOT NULL").fetchone()[0]
        _set(state="idle", checked=n, with_gps=with_gps, finished=time.time())
    finally:
        src.close()
        conn.close()


def _flush(conn, batch):
    conn.executemany(
        "INSERT INTO places (root, path, size, lat, lon, ts) VALUES (?,?,?,?,?,?)"
        " ON CONFLICT(root, path) DO UPDATE SET size=excluded.size, lat=excluded.lat,"
        " lon=excluded.lon, ts=excluded.ts", batch)
    conn.commit()


def scan_loop(base_dir, roots):
    """Daemon thread body: wait for the date index to have been built, scan, sleep, repeat."""
    time.sleep(300)
    while True:
        try:
            scan_once(base_dir, roots)
        except Exception as exc:  # noqa: BLE001 - never let the thread die
            _set(state="error", error=str(exc)[:200])
        _wake.wait(RESCAN_SECONDS)
        _wake.clear()


def points(base_dir, allowed_roots, limit=50000):
    """Every picture with a position in the allowed roots, newest first: dict rows."""
    if not allowed_roots or not os.path.exists(os.path.join(base_dir, DB_NAME)):
        return []
    conn = _connect(base_dir)
    try:
        marks = ",".join("?" * len(allowed_roots))
        rows = conn.execute(
            f"SELECT root, path, size, lat, lon, ts FROM places WHERE lat IS NOT NULL AND root IN ({marks})"
            " ORDER BY ts DESC LIMIT ?", (*allowed_roots, limit)).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def forget(base_dir, pairs):
    if not pairs:
        return
    conn = _connect(base_dir)
    try:
        conn.executemany("DELETE FROM places WHERE root = ? AND path = ?", list(pairs))
        conn.commit()
    finally:
        conn.close()
