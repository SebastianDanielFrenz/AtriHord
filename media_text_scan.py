"""Reading every caption, OCR text and tag list from start to end - the search
the word index cannot do.

User, 2026-11-10: "read the entire DB ... keep only one or multiple small
chunks in memory for processing during the read and then unload it". The FTS
index (media_ai.media_fts) finds whole words and word beginnings in a fraction
of a second, but never the middle of a word: "0513" does not find
"img_20240513" in a screenshot's OCR text. This module keeps a compact copy of
just the searchable text - no faces, no embeddings - in one file, and a search
reads that file in fixed-size chunks, keeping one chunk in memory at a time.

The file is a cache, never the truth:
  - rows indexed after it was built (id above its max_id) are read live from
    the database at search time, so a new picture is found at once;
  - every hit is checked against its live row before it is returned, so a file
    that was trashed or re-captioned since never comes back as a false hit;
  - it is rebuilt in the background once a day, or sooner when many new rows
    have piled up. A missing file just means the fallback finds nothing yet.

File format, UTF-8, one line per file:
    #ksf-text-scan 1 <max_id> <built_at> <rows>
    <id>\t<root>\t<path>\t<caption>\t<ocr>\t<tags>
caption/ocr/tags are lowercased, with tab/newline/CR turned into spaces; tags
are the indexer's own "_text" form (underscores and brackets as spaces).
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time

import media_ai

FILE_NAME = "text_scan.v1.txt"
HEADER = "#ksf-text-scan 1"
DIR_ENV = "KSF_TEXT_SCAN_DIR"
DIR_KEY = "text_scan_dir"           # config.json: where the file lives (an SSD)
CHUNK_BYTES = 32 * 1024 * 1024      # read size; at most ~2 of these in memory
MAX_AGE_S = 24 * 3600               # rebuilt at least once a day
MAX_LIVE_ROWS = 20_000              # ... or once this many rows are newer than it
VERIFY_BATCH = 500

# Which of the three text fields each search mode looks at - the same columns
# media_ai.SEARCH_COLUMNS gives the word index.
FIELD_INDEX = {"caption": 0, "ocr": 1, "tags_text": 2}

_build_lock = threading.Lock()
_state = {"building": False, "last_error": None, "last_build_s": None}


# ------------------------------------------------------------------ paths --

def scan_dir(base_dir: str) -> str:
    path = os.environ.get(DIR_ENV)
    if not path:
        try:
            with open(os.path.join(base_dir, "config.json"), encoding="utf-8") as fh:
                path = (json.load(fh) or {}).get(DIR_KEY)
        except (OSError, ValueError):
            path = None
    if not path:
        path = media_ai.store_dir(base_dir)
    os.makedirs(path, exist_ok=True)
    return path


def scan_path(base_dir: str) -> str:
    return os.path.join(scan_dir(base_dir), FILE_NAME)


# ------------------------------------------------------------------ text --

def _clean(text) -> str:
    if not text:
        return ""
    return str(text).lower().replace("\t", " ").replace("\n", " ").replace("\r", " ")


def tags_text_of(tags_json) -> str:
    """The tag words of a media.tags value: the indexer's own "_text" when it
    stored one, else rebuilt the way media_indexer.tags_text does."""
    if not tags_json:
        return ""
    try:
        tags = json.loads(tags_json)
    except ValueError:
        return str(tags_json)
    if not isinstance(tags, dict):
        return ""
    if isinstance(tags.get("_text"), str):
        return tags["_text"]
    words = []
    for group in ("character", "general", "rating"):
        for e in tags.get(group) or []:
            tag = e.get("tag") if isinstance(e, dict) else e
            if tag:
                words.append(str(tag).replace("_", " ").replace("(", " ").replace(")", " "))
    return " ".join(words)


def row_fields(caption, ocr, tags_json):
    return (_clean(caption), _clean(ocr), _clean(tags_text_of(tags_json)))


def terms_of(query: str):
    return [t for t in query.lower().replace('"', " ").split() if t]


def fields_match(fields, terms, wanted) -> bool:
    """Like the word index: every term inside ONE of the wanted fields."""
    return any(all(t in fields[i] for t in terms) for i in wanted)


def _wanted(mode: str):
    cols = media_ai.SEARCH_COLUMNS.get(mode) or ()
    return tuple(FIELD_INDEX[c] for c in cols if c in FIELD_INDEX)


# ----------------------------------------------------------------- build --

def _ro_connect(base_dir: str) -> sqlite3.Connection:
    uri = "file:" + media_ai.store_path(base_dir) + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=30)
    return conn


def build(base_dir: str) -> dict:
    """Writes the file afresh: one sequential pass over the media table, the
    output streamed to a temporary file and moved into place at the end."""
    started = time.time()
    final = scan_path(base_dir)
    tmp = f"{final}.tmp-{os.getpid()}"
    conn = _ro_connect(base_dir)
    rows = 0
    max_id = 0
    try:
        with open(tmp, "w", encoding="utf-8", newline="\n", buffering=4 * 1024 * 1024) as out:
            out.write(HEADER + " " + " " * 60 + "\n")   # rewritten with the numbers below
            cur = conn.execute(
                "SELECT id, root, path, caption, ocr, tags FROM media"
                " WHERE trashed IS NULL ORDER BY id")
            while True:
                batch = cur.fetchmany(5000)
                if not batch:
                    break
                lines = []
                for mid, root, path, caption, ocr, tags in batch:
                    max_id = max(max_id, mid)
                    cap, o, tg = row_fields(caption, ocr, tags)
                    if not (cap or o or tg):
                        continue
                    lines.append(f"{mid}\t{root}\t{path}\t{cap}\t{o}\t{tg}\n")
                rows += len(lines)
                out.writelines(lines)
            # Rows can be trashed but never hold a larger id than the newest
            # live one, so the highest id of the table is the right boundary.
            top = conn.execute("SELECT MAX(id) FROM media").fetchone()[0] or 0
            max_id = max(max_id, top)
        header = f"{HEADER} {max_id} {int(started)} {rows}"
        with open(tmp, "r+", encoding="utf-8") as fh:
            fh.write(header.ljust(len(HEADER) + 61))
        os.replace(tmp, final)
    finally:
        conn.close()
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass
    took = time.time() - started
    return {"rows": rows, "max_id": max_id, "seconds": round(took, 1),
            "bytes": os.path.getsize(final)}


def read_header(path: str):
    """(max_id, built_at, rows) or None when there is no usable file."""
    try:
        with open(path, encoding="utf-8") as fh:
            parts = fh.readline().split()
    except OSError:
        return None
    if len(parts) < 5 or " ".join(parts[:2]) != HEADER:
        return None
    try:
        return int(parts[2]), int(parts[3]), int(parts[4])
    except ValueError:
        return None


def _build_in_background(base_dir: str):
    try:
        info = build(base_dir)
        _state["last_build_s"] = info["seconds"]
        _state["last_error"] = None
        print(f"[text-scan] rebuilt: {info}", flush=True)
    except Exception as exc:          # a failed rebuild keeps the old file
        _state["last_error"] = str(exc)
        print(f"[text-scan] rebuild failed: {exc}", flush=True)
    finally:
        _state["building"] = False


def ensure_fresh(base_dir: str, conn: sqlite3.Connection | None = None) -> bool:
    """Starts a background rebuild when the file is missing, a day old, or
    behind by many rows. Never blocks; True when one was started."""
    head = read_header(scan_path(base_dir))
    stale = head is None or time.time() - head[1] > MAX_AGE_S
    if not stale and conn is not None:
        try:
            newer = conn.execute("SELECT COUNT(*) FROM media WHERE id > ?", (head[0],)).fetchone()[0]
            stale = newer > MAX_LIVE_ROWS
        except sqlite3.Error:
            pass
    if not stale:
        return False
    with _build_lock:
        if _state["building"]:
            return False
        _state["building"] = True
    threading.Thread(target=_build_in_background, args=(base_dir,), daemon=True,
                     name="text-scan-build").start()
    return True


def status(base_dir: str) -> dict:
    head = read_header(scan_path(base_dir))
    return {"ready": head is not None,
            "max_id": head[0] if head else None,
            "built_at": head[1] if head else None,
            "rows": head[2] if head else None,
            "building": _state["building"],
            "last_build_s": _state["last_build_s"],
            "last_error": _state["last_error"]}


# ---------------------------------------------------------------- search --

def _scan_file(path: str, terms, wanted, limit: int):
    """(id, root, path) of every line matching, reading CHUNK_BYTES at a time.
    Searches for the longest term with bytes.find and parses only the lines it
    lands in, so a rare term costs little more than the read itself."""
    needle = max(terms, key=len).encode("utf-8")
    hits = []
    carry = b""
    with open(path, "rb", buffering=0) as fh:
        fh.readline()                                # the header
        while len(hits) < limit:
            block = fh.read(CHUNK_BYTES)
            if not block:
                data, carry = carry, b""
            else:
                data = carry + block
                cut = data.rfind(b"\n") + 1
                if cut == 0:                         # one line longer than a chunk
                    carry = data
                    continue
                data, carry = data[:cut], data[cut:]
            pos = data.find(needle)
            while pos != -1 and len(hits) < limit:
                start = data.rfind(b"\n", 0, pos) + 1
                end = data.find(b"\n", pos)
                if end == -1:
                    end = len(data)
                parts = data[start:end].decode("utf-8", "replace").split("\t")
                if len(parts) == 6:
                    fields = (parts[3], parts[4], parts[5])
                    if fields_match(fields, terms, wanted):
                        try:
                            hits.append((int(parts[0]), parts[1], parts[2]))
                        except ValueError:
                            pass
                pos = data.find(needle, end)
            del data
            if not block:
                break
    return hits


def _verify(conn: sqlite3.Connection, hits, terms, wanted):
    """Only the hits whose LIVE row still matches and is not trashed."""
    ok = []
    for i in range(0, len(hits), VERIFY_BATCH):
        part = hits[i:i + VERIFY_BATCH]
        holes = ",".join("?" * len(part))
        live = {r[0]: r for r in conn.execute(
            "SELECT id, root, path, caption, ocr, tags, trashed FROM media"
            f" WHERE id IN ({holes})", [h[0] for h in part])}
        for mid, _root, _path in part:
            r = live.get(mid)
            if r is None or r[6] is not None:
                continue
            if fields_match(row_fields(r[3], r[4], r[5]), terms, wanted):
                ok.append((r[1], r[2]))
    return ok


def _live_rows(conn: sqlite3.Connection, after_id: int, terms, wanted, limit: int):
    """Rows newer than the file, read straight from the table (a rowid range)."""
    out = []
    for mid, root, path, caption, ocr, tags in conn.execute(
            "SELECT id, root, path, caption, ocr, tags FROM media"
            " WHERE id > ? AND trashed IS NULL ORDER BY id", (after_id,)):
        if fields_match(row_fields(caption, ocr, tags), terms, wanted):
            out.append((root, path))
            if len(out) >= limit:
                break
    return out


def search(base_dir: str, conn: sqlite3.Connection, text: str, mode: str,
           limit: int = 5000):
    """(root, path) of every file whose text contains each word ANYWHERE, also
    inside other words. Slow (one read of the whole file), so the caller only
    asks when the word index came back short."""
    terms = terms_of(text)
    wanted = _wanted(mode)
    if not terms or not wanted:
        return []
    path = scan_path(base_dir)
    head = read_header(path)
    ensure_fresh(base_dir, conn)
    found = []
    if head is not None:
        found = _verify(conn, _scan_file(path, terms, wanted, limit), terms, wanted)
    after = head[0] if head is not None else 0
    if head is not None:    # without a file the live pass would be the whole table
        found += _live_rows(conn, after, terms, wanted, max(0, limit - len(found)))
    return found[:limit]


if __name__ == "__main__":
    import sys
    base = os.path.dirname(os.path.abspath(__file__))
    if len(sys.argv) >= 2 and sys.argv[1] == "build":
        print(build(base))
    elif len(sys.argv) >= 4 and sys.argv[1] == "search":
        t = time.time()
        res = search(base, _ro_connect(base), sys.argv[3], sys.argv[2])
        print(f"{len(res)} hits in {time.time() - t:.2f} s")
        for r in res[:10]:
            print("  ", r)
    else:
        print("usage: media_text_scan.py build | search <mode> <words>")
