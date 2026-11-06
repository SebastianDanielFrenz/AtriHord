"""Probes every video and picture in the library once and records its dimensions.

Deliberately gentle: the library sits on a spinning disk that a download job
is also writing to, so this runs a small worker pool at low priority and
saves in batches. It is resumable - anything already recorded with a matching
file size is skipped - so it can be stopped and restarted freely.

  venv/bin/python backfill_dimensions.py [--kind all|videos|images] [--workers N]
                                         [--limit N] [--dry-run]

Run it with the venv's interpreter: the system python3 on the media VM has no
PIL, and without it every picture silently measures as nothing.

New downloads are measured the next time this runs; nothing measures them at
download time. A full run (no --limit) also removes the entries of files that
are gone or have become 0-byte ban tombstones.
"""
import argparse
import json
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

import media_dimensions as md   # noqa: E402

# Every save rewrites the WHOLE store under a lock, so with pictures in it
# (~460k entries, tens of MB) saving every 400 would rewrite it over a
# thousand times. Save by count or by time, whichever comes first, so a slow
# video pass still persists regularly.
SAVE_EVERY = 5000
SAVE_AFTER_S = 120

# A root where much of what was measured has "disappeared" was not emptied:
# its disk is not mounted, and an empty mount point walks cleanly. Past this
# share of the root's entries (and this floor) nothing under it is pruned.
PRUNE_MAX_SHARE = 0.05
PRUNE_MIN_CEILING = 1000


def roots():
    """The media roots, read the way the server reads them."""
    try:
        with open(os.path.join(BASE_DIR, "roots.json"), encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        data = []
    out = [(r["id"], r["path"]) for r in data if r.get("id") and r.get("path")]
    if not any(rid == "downloads" for rid, _ in out):
        out.insert(0, ("downloads", os.path.join(BASE_DIR, "web_files", "downloads")))
    return out


def wanted(name, kind):
    return ((kind in ("all", "videos") and md.is_video_name(name))
            or (kind in ("all", "images") and md.is_image_name(name)))


def walk_media(root_id, base, kind="all"):
    for dirpath, dirnames, filenames in os.walk(base, followlinks=True):
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        rel_dir = os.path.relpath(dirpath, base).replace(os.sep, "/")
        if rel_dir == ".":
            rel_dir = ""
        for name in filenames:
            if name.startswith(".") or not wanted(name, kind):
                continue
            rel = "/".join(p for p in (rel_dir, name) if p)
            yield rel, os.path.join(dirpath, name)


def gone_entries(known, seen, walked_roots, kind):
    """Keys of measured files the walk no longer found - deleted, or moved to
    a .trash folder, which the walk skips. Decided per root, and refused for a
    root where too much is missing to be deletions."""
    gone = []
    for root_id in walked_roots:
        prefix = root_id + "|"
        mine = [k for k in known if k.startswith(prefix) and wanted(k[len(prefix):], kind)]
        missing = [k for k in mine if k not in seen]
        if len(missing) > max(PRUNE_MIN_CEILING, len(mine) * PRUNE_MAX_SHARE):
            print(f"NOT pruning {len(missing)} of {len(mine)} entries under {root_id}: "
                  f"too many to be deletions - is its disk mounted?", flush=True)
            continue
        gone.extend(missing)
    return gone


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--kind", choices=("all", "videos", "images"), default="all")
    ap.add_argument("--workers", type=int, default=3)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--dry-run", action="store_true",
                    help="walk, report what would be probed and pruned, write nothing")
    args = ap.parse_args()

    # ffprobe reads a file header: cheap on CPU, a seek on disk. Being nice
    # matters because a download is usually writing to the same spindle.
    try:
        os.nice(10)
    except OSError:
        pass

    known = md.load(BASE_DIR)
    print(f"already known: {len(known)}", flush=True)

    todo = []
    seen = set()
    walked_roots = []
    for root_id, base in roots():
        if not os.path.isdir(base):
            print(f"skipping missing root {root_id} ({base})", flush=True)
            continue
        walked_roots.append(root_id)
        for rel, full in walk_media(root_id, base, args.kind):
            key = md.key_for(root_id, rel)
            seen.add(key)
            prev = known.get(key)
            if prev and len(prev) > 2:
                try:
                    unchanged = os.path.getsize(full) == prev[2]
                except OSError:
                    continue
                # Unchanged since it was probed - unless it is a video probed
                # before frame rates and durations were recorded.
                if unchanged and not md.needs_video_fields(rel, prev):
                    continue
            todo.append((key, full))
            if args.limit and len(todo) >= args.limit:
                break
        if args.limit and len(todo) >= args.limit:
            break

    # Only a walk that went all the way knows what is missing.
    gone = [] if args.limit else gone_entries(known, seen, walked_roots, args.kind)

    total = len(todo)
    print(f"to probe: {total}; measured files no longer on disk: {len(gone)}", flush=True)
    if args.dry_run:
        tombstones = 0
        for key, full in todo:
            try:
                tombstones += key in known and os.path.getsize(full) == 0
            except OSError:
                pass
        print(f"dry run: would prune {len(gone)} gone and {tombstones} tombstoned entries",
              flush=True)
        return 0

    pending = {}
    stale = []
    lock = threading.Lock()
    done = [0]
    started = time.time()
    last_save = [time.time()]

    def work(item):
        key, full = item
        entry = md.entry_for(full)
        tombstone = False
        if entry is None and key in known:
            try:
                tombstone = os.path.getsize(full) == 0
            except OSError:
                pass
        with lock:
            done[0] += 1
            if entry:
                pending[key] = entry
            elif tombstone:
                stale.append(key)
            n = done[0]
            flush = len(pending) >= SAVE_EVERY or (
                pending and time.time() - last_save[0] > SAVE_AFTER_S)
            batch = dict(pending) if flush else None
            if flush:
                pending.clear()
                last_save[0] = time.time()
        if batch:
            md.merge(BASE_DIR, batch)
        if n % 500 == 0 or n == total:
            rate = n / max(1e-6, time.time() - started)
            left = (total - n) / rate if rate else 0
            print(f"  {n}/{total}  {rate:.1f}/s  ~{left/60:.0f} min left", flush=True)

    if total:
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            list(pool.map(work, todo))

    if pending:
        md.merge(BASE_DIR, pending)
    removed = md.prune(BASE_DIR, set(stale) | set(gone))
    stored = len(md.load(BASE_DIR))
    print(f"done in {(time.time()-started)/60:.1f} min; removed {removed} stale entries "
          f"({len(stale)} tombstones, {len(gone)} gone); {stored} entries stored", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
