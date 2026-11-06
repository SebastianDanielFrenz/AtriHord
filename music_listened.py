"""How long each account listened to each song, for charts (vault KSF-052).

User, 2026-09-24: "keep track of the listened minutes for each song for each
user, so that you can later build charts out of it. The video associated with
an audio track counts as the song."

What counts is what actually PLAYED: the pages add up how far the playback
position moved while playing (a pause or a seek adds nothing) and send it about
once a minute, under the song's ref ("v:<video id>" or "p:<root>:<path>", the
listening store's own). The server files it under the account, the song and
the UTC hour it arrived in - hours rather than days so a chart can be drawn in
any time zone and by time of day. A song's music video is filed under the song
(server.py resolves a played file to its song).

`songs` keeps what the song was called when it was last heard, so a chart of
last year still has names for songs since renamed, moved or deleted.

SQLite, not a JSON store like the other music stores: this one grows with
every minute listened, is added to every minute while anything plays, and is
read by sums and groupings - one row upserted instead of a whole file
rewritten, and charts are one GROUP BY.
"""
import sqlite3
import threading
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS listened (
    user TEXT NOT NULL,
    ref TEXT NOT NULL,
    hour INTEGER NOT NULL,          -- UTC hours since the epoch: int(t // 3600)
    seconds REAL NOT NULL,
    PRIMARY KEY (user, ref, hour)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS listened_user_hour ON listened (user, hour);
CREATE TABLE IF NOT EXISTS songs (
    ref TEXT PRIMARY KEY,
    title TEXT, artist TEXT, album TEXT, genre TEXT, duration REAL,
    root TEXT, path TEXT,
    seen REAL                       -- when these names were last written
) WITHOUT ROWID;
"""

# The most one report may add to one song. The pages send every minute or so;
# a page that could not reach the server for a while sends what piled up, and
# anything past this is not a listening session.
MAX_ITEM_SECONDS = 6 * 3600


class Listened:
    def __init__(self, path):
        self.path = path
        self.lock = threading.Lock()
        self._conn = None

    def _db(self):
        if self._conn is None:
            conn = sqlite3.connect(self.path, timeout=10, check_same_thread=False)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(SCHEMA)
            self._conn = conn
        return self._conn

    def add(self, user, items, now=None):
        """items: [(ref, seconds, song or None)]. `song`, when known, is the
        catalogue entry whose names are kept for the charts."""
        now = time.time() if now is None else now
        hour = int(now // 3600)
        with self.lock:
            db = self._db()
            with db:
                for ref, seconds, song in items:
                    seconds = min(float(seconds), MAX_ITEM_SECONDS)
                    if not seconds > 0:
                        continue
                    db.execute(
                        "INSERT INTO listened (user, ref, hour, seconds) VALUES (?, ?, ?, ?)"
                        " ON CONFLICT (user, ref, hour) DO UPDATE SET seconds = seconds + excluded.seconds",
                        (user, ref, hour, round(seconds, 1)))
                    if song:
                        db.execute(
                            "INSERT INTO songs (ref, title, artist, album, genre, duration, root, path, seen)"
                            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
                            " ON CONFLICT (ref) DO UPDATE SET title = excluded.title, artist = excluded.artist,"
                            " album = excluded.album, genre = excluded.genre, duration = excluded.duration,"
                            " root = excluded.root, path = excluded.path, seen = excluded.seen",
                            (ref, song.get("title"), song.get("artist"), song.get("album") or None,
                             song.get("genre") or None, song.get("duration") or None,
                             song.get("root"), song.get("path"), round(now, 1)))

    def summary(self, user, since=None, limit=50):
        """The account's listening since `since` (epoch seconds, None = all):
        the total, the songs by time listened, and the time per UTC day."""
        since_hour = int(since // 3600) if since else 0
        with self.lock:
            db = self._db()
            total = db.execute("SELECT COALESCE(SUM(seconds), 0) FROM listened WHERE user = ? AND hour >= ?",
                               (user, since_hour)).fetchone()[0]
            songs = db.execute(
                "SELECT l.ref, SUM(l.seconds) AS s, s.title, s.artist, s.album"
                " FROM listened l LEFT JOIN songs s ON s.ref = l.ref"
                " WHERE l.user = ? AND l.hour >= ? GROUP BY l.ref ORDER BY s DESC LIMIT ?",
                (user, since_hour, int(limit))).fetchall()
            days = db.execute(
                "SELECT hour / 24 AS d, SUM(seconds) FROM listened WHERE user = ? AND hour >= ?"
                " GROUP BY d ORDER BY d", (user, since_hour)).fetchall()
        return {
            "seconds": round(total, 1),
            "songs": [{"ref": r, "seconds": round(s, 1), "title": t, "artist": a, "album": al}
                      for r, s, t, a, al in songs],
            "days": [[int(d) * 86400, round(s, 1)] for d, s in days],
        }
