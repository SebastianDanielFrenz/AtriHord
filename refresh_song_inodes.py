"""One row per song file in the AI index, after files have been linked or
moved around outside the app.

The index carries an `ino` column ("<device>:<inode>") so a song's other names
are not taken for new files. A migration that links or moves files leaves that
column stale, and a row can survive for a path that is now just another name
for a file the index already has - which would be a second analysis, a second
search hit and a second dot on the genre map.

This re-reads the inode of every audio row, and where two live rows are the
same file it keeps the one in the song store (or the first) and forgets the
other. Only the index is touched; no file is moved, and a forgotten path is
indexed again from scratch if it ever becomes a file of its own.

  venv/bin/python refresh_song_inodes.py [--apply]
"""
import argparse
import collections
import json
import os
import sys

sys.path.insert(0, "/data2/ksf_web_dl")
import media_ai                          # noqa: E402

BASE_DIR = "/data2/ksf_web_dl"
DOWNLOADS = os.path.join(BASE_DIR, "web_files", "downloads")


def roots():
    out = {"downloads": DOWNLOADS}
    try:
        with open(os.path.join(BASE_DIR, "roots.json"), encoding="utf-8") as fh:
            for r in json.load(fh):
                if r.get("id") and r.get("path"):
                    out[r["id"]] = r["path"]
    except (OSError, ValueError):
        pass
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()
    all_roots = roots()
    conn = media_ai.connect(BASE_DIR)
    rows = conn.execute(
        "SELECT id, root, path, ino, trashed FROM media WHERE kind = ?", ("audio",)).fetchall()
    stale, gone = [], []
    by_ino = collections.defaultdict(list)
    for r in rows:
        base = all_roots.get(r["root"])
        if not base:
            continue
        try:
            st = os.stat(os.path.join(base, r["path"]))
        except OSError:
            gone.append(r)
            continue
        key = f"{st.st_dev}:{st.st_ino}"
        if r["ino"] != key:
            stale.append((r["id"], key, r["path"]))
        if not r["trashed"]:
            by_ino[key].append(r)
    extra = []
    for key, rs in by_ino.items():
        if len(rs) < 2:
            continue
        # The song store's own name is the one to keep: it is what the songs
        # index and every playlist link point at.
        # The store's own name first, and among those the shortest path: a
        # song's home is youtube_music/<Artist>/<Title>, not a playlist or an
        # old "<Artist> - Topic" folder.
        rs = sorted(rs, key=lambda r: (not (r["root"] == "downloads"
                                            and r["path"].startswith("youtube_music/")),
                                       len(r["path"]), r["path"]))
        extra.extend(rs[1:])
    print(f"{len(rows)} audio rows: {len(stale)} carry a stale inode, "
          f"{len(extra)} are a second row for a file the index already has, "
          f"{len(gone)} have no file")
    for r in extra[:10]:
        print(f"      second row: {r['root']} {r['path']}")
    if not args.apply:
        print("\n(dry run - nothing changed)")
        return 0
    for row_id, key, _path in stale:
        conn.execute("UPDATE media SET ino = ? WHERE id = ?", (key, row_id))
    conn.commit()
    for r in extra:
        # PRAGMA foreign_keys is 0 in this store, so ON DELETE CASCADE never
        # fires: what hangs off the row has to go by hand, or an orphaned
        # embedding keeps the song on the genre map after its row is gone.
        conn.execute("DELETE FROM media_tags WHERE media_id = ?", (r["id"],))
        conn.execute("DELETE FROM audio_embeddings WHERE media_id = ?", (r["id"],))
        media_ai.forget(conn, [(r["root"], r["path"])])
    conn.commit()
    print(f"{len(stale)} inodes refreshed, {len(extra)} duplicate rows forgotten")
    return 0


if __name__ == "__main__":
    sys.exit(main())
