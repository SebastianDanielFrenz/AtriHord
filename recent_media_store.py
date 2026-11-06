"""Cross-process-safe persistence for the recently-downloaded media list
(a bounded, ordered list of paths relative to DOWNLOADS_DIR - most recent
first). Used by both server.py (the main pipeline, in threads) and
youtube_music_extractor.py (an independent subprocess), so both write
through the same fcntl-locked file instead of a Python-level Lock that
would only ever synchronize within a single process.
"""
import fcntl
import json
import os

DEFAULT_MAX_RECENT_MEDIA_ITEMS = 3000

# Kept as a module constant because callers imported it directly for years.
# It is the DEFAULT only - ask max_recent_media_items(base_dir) for the value
# actually in force.
MAX_RECENT_MEDIA_ITEMS = DEFAULT_MAX_RECENT_MEDIA_ITEMS

_MAX_ITEMS_ENV = "KSF_MAX_RECENT_MEDIA_ITEMS"


def max_recent_media_items(base_dir):
    """How many entries the recent list keeps.

    Order of precedence: the KSF_MAX_RECENT_MEDIA_ITEMS environment variable,
    then "max_recent_media_items" in config.json, then the default.

    This module is imported by an independent subprocess as well as the
    server, so it reads the config file itself rather than being handed a
    value - there is no shared process state to pass it through.

    The file is re-read on every call, deliberately. An mtime cache was tried
    and removed: this filesystem returns the SAME st_mtime_ns for consecutive
    writes (measured - three writes in a row, one timestamp), so a cache keyed
    on it silently serves a stale limit after an edit. The read is a couple of
    hundred bytes from page cache, and this is called about once per finished
    task, so there is nothing to save.

    Raising this costs memory and a longer list to trim, not I/O: the entries
    are built lazily a page at a time (see get_recent_media_entries). It also
    makes the 24h download-volume seed accurate over a wider window."""
    raw = os.environ.get(_MAX_ITEMS_ENV)
    if raw is None:
        try:
            with open(os.path.join(base_dir, "config.json"), "r", encoding="utf-8") as f:
                raw = json.load(f).get("max_recent_media_items")
        except Exception:
            raw = None
    return _coerce_max_items(raw)


def _coerce_max_items(raw):
    """A bad value must not silently disable the cap and let the list grow
    without bound, so anything unusable falls back to the default."""
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_MAX_RECENT_MEDIA_ITEMS
    return value if value > 0 else DEFAULT_MAX_RECENT_MEDIA_ITEMS


def _paths_file(base_dir):
    return os.path.join(base_dir, "recent_media.json")


def recent_media_list_exists(base_dir):
    """False on a fresh install, where the caller may want to seed the list
    from the existing library rather than start from empty."""
    return os.path.exists(_paths_file(base_dir))


def read_recent_media_paths(base_dir):
    path_file = _paths_file(base_dir)
    if not os.path.exists(path_file):
        return []
    lock_file = open(path_file + ".lock", "a+")
    try:
        fcntl.flock(lock_file, fcntl.LOCK_SH)
        try:
            with open(path_file, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return []
    finally:
        fcntl.flock(lock_file, fcntl.LOCK_UN)
        lock_file.close()


def write_recent_media_paths(base_dir, paths):
    path_file = _paths_file(base_dir)
    lock_file = open(path_file + ".lock", "a+")
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX)
        try:
            temp_file = path_file + ".tmp"
            with open(temp_file, "w", encoding="utf-8") as f:
                json.dump(paths, f, indent=2, ensure_ascii=False)
            os.replace(temp_file, path_file)
        finally:
            fcntl.flock(lock_file, fcntl.LOCK_UN)
    finally:
        lock_file.close()


DEFAULT_ROOT_ID = "downloads"


def normalize_entry(item):
    """Canonical form is {"root": ..., "path": ...}. A bare string is a
    pre-roots entry, which by definition lived in the downloads root."""
    if isinstance(item, str):
        return {"root": DEFAULT_ROOT_ID, "path": item}
    return {
        "root": item.get("root") or DEFAULT_ROOT_ID,
        "path": item.get("path") or "",
    }


def _entry_key(item):
    e = normalize_entry(item)
    return (e["root"], e["path"])


def record_recent_media(base_dir, rel_paths):
    """Prepends newly-produced files (most recent first among themselves) to
    the recent-media list, dedups against what's already there, and caps at
    the configured limit (see max_recent_media_items). Accepts either
    {"root","path"} dicts or bare
    path strings (treated as the downloads root). The whole
    read-modify-write happens under one exclusive lock so a concurrent
    writer (e.g. the youtube_music extractor subprocess) can't race and
    clobber this update."""
    if not rel_paths:
        return

    path_file = _paths_file(base_dir)
    lock_file = open(path_file + ".lock", "a+")
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX)
        try:
            existing = []
            if os.path.exists(path_file):
                try:
                    with open(path_file, "r", encoding="utf-8") as f:
                        existing = json.load(f)
                except Exception:
                    existing = []

            existing = [normalize_entry(e) for e in existing]
            existing_keys = {_entry_key(e) for e in existing}
            seen = set()
            new_ordered = []
            for p in rel_paths:
                key = _entry_key(p)
                if key not in existing_keys and key not in seen:
                    new_ordered.append(normalize_entry(p))
                    seen.add(key)

            combined = (new_ordered + existing)[:max_recent_media_items(base_dir)]

            temp_file = path_file + ".tmp"
            with open(temp_file, "w", encoding="utf-8") as f:
                json.dump(combined, f, indent=2, ensure_ascii=False)
            os.replace(temp_file, path_file)
        finally:
            fcntl.flock(lock_file, fcntl.LOCK_UN)
    finally:
        lock_file.close()
