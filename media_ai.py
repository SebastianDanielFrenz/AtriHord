"""What the vision models saw in each file, and the searches that reads.

Four things per file, all optional and filled independently so a partial
index is still useful:

  caption   one sentence describing the picture      -> the "context" search
  ocr       text found inside the picture            -> the "OCR" search
  tags      danbooru-style tags, characters, series  -> character search
  faces     one row per detected face, with a 512-d embedding

SQLite with FTS5, not a JSON file like media_dimensions. That module holds two
integers per video and is read whole into a dict; this one holds a sentence,
a page of OCR text and a tag list for 450k files - tens of megabytes of text
that has to be searched by word, not by substring, and updated a batch at a
time while the server keeps serving. That is a database's job.

The FTS table is external-content: the text lives once in `media`, and the
index refers to it by rowid. Triggers keep the two in step, because an FTS5
external-content table that is written to directly and never rebuilt returns
rows that no longer exist.

Nothing here imports torch or talks to a GPU. The worker on the GPU host
produces the rows; this is only where they land and how they are read back.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time

STORE_NAME = "media_ai.sqlite3"

# Generated data does not belong in the application directory. Deploys scp
# into that directory, backups of it are meant to be small, and a multi-GB
# index sitting beside server.py is one careless sync from being copied or
# clobbered. Resolution order, most specific first:
#
#   KSF_MEDIA_AI_DIR      env, for a one-off or a test run
#   ai_data_dir           in the app config.json, which is how prod sets it
#   <base_dir>/ai_data    default, so a fresh checkout still works
DATA_DIR_ENV = "KSF_MEDIA_AI_DIR"
DATA_DIR_KEY = "ai_data_dir"
DEFAULT_DATA_SUBDIR = "ai_data"


def store_dir(base_dir: str) -> str:
    """The directory the index lives in, created if it does not exist."""
    path = os.environ.get(DATA_DIR_ENV)
    if not path:
        try:
            with open(os.path.join(base_dir, "config.json"), encoding="utf-8") as fh:
                path = (json.load(fh) or {}).get(DATA_DIR_KEY)
        except (OSError, ValueError):
            path = None
    if not path:
        path = os.path.join(base_dir, DEFAULT_DATA_SUBDIR)
    os.makedirs(path, exist_ok=True)
    return path

# Bumped when the schema changes in a way that makes stored rows wrong rather
# than merely incomplete - a re-index of everything, so it is not done lightly.
SCHEMA_VERSION = 1

# What a row was produced by, so a model upgrade can re-do only what it needs
# to rather than the whole library.
MODEL_KEYS = ("caption_model", "ocr_model", "tag_model", "face_model", "genre_model",
              "orient_model")

_local = threading.local()


def store_path(base_dir: str) -> str:
    return os.path.join(store_dir(base_dir), STORE_NAME)


def connect(base_dir: str, timeout: float = 15.0) -> sqlite3.Connection:
    """One connection per thread. Flask is threaded and a sqlite connection is
    not safe to share; a thread-local is simpler than a pool and this is not a
    high-concurrency workload."""
    conn = getattr(_local, "conn", None)
    if conn is not None and getattr(_local, "path", None) == store_path(base_dir):
        return conn
    conn = sqlite3.connect(store_path(base_dir), timeout=timeout)
    conn.row_factory = sqlite3.Row
    # WAL so the indexer can keep writing while the server reads. Without it a
    # backfill batch blocks every search for the length of its transaction.
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    _local.conn = conn
    _local.path = store_path(base_dir)
    ensure_schema(conn)
    return conn


def _add_column(conn, table, column, decl):
    """Add a column to an existing table, once.

    CREATE TABLE IF NOT EXISTS does nothing to a table that already exists, so
    a new column has to be added explicitly for stores already in the field.
    SQLite has no IF NOT EXISTS for ADD COLUMN; asking the table what it has is
    cheaper and clearer than catching the duplicate-column error.
    """
    have = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
    if column not in have:
        try:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
        except sqlite3.OperationalError as exc:
            # Another connection (the server is threaded) added it between the
            # look and the ALTER: the column is there, which is all this wants.
            # Seen on the first start with the orient columns (2026-11-08).
            if "duplicate column" not in str(exc):
                raise


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS media (
      id            INTEGER PRIMARY KEY,
      root          TEXT NOT NULL,
      path          TEXT NOT NULL,
      size          INTEGER,          -- invalidation: mtime lies on this fs
      kind          TEXT,             -- image | video
      caption       TEXT,
      ocr           TEXT,
      tags          TEXT,             -- json: {"character": [...], "series": [...], "general": [...]}
      caption_model TEXT,
      ocr_model     TEXT,
      tag_model     TEXT,
      face_model    TEXT,
      indexed_at    REAL,
      UNIQUE(root, path)
    );

    -- external content: the text is not duplicated, the index points at it.
    CREATE VIRTUAL TABLE IF NOT EXISTS media_fts USING fts5(
      caption, ocr, tags_text,
      content='media', content_rowid='id',
      tokenize='unicode61 remove_diacritics 2'
    );

    -- Triggers, not a periodic rebuild: an external-content FTS index that
    -- drifts from its table returns hits for rows that are gone, which looks
    -- like the search inventing files.
    CREATE TRIGGER IF NOT EXISTS media_ai_ins AFTER INSERT ON media BEGIN
      INSERT INTO media_fts(rowid, caption, ocr, tags_text)
      VALUES (new.id, new.caption, new.ocr, new.tags);
    END;
    CREATE TRIGGER IF NOT EXISTS media_ai_del AFTER DELETE ON media BEGIN
      INSERT INTO media_fts(media_fts, rowid, caption, ocr, tags_text)
      VALUES ('delete', old.id, old.caption, old.ocr, old.tags);
    END;
    CREATE TRIGGER IF NOT EXISTS media_ai_upd AFTER UPDATE ON media BEGIN
      INSERT INTO media_fts(media_fts, rowid, caption, ocr, tags_text)
      VALUES ('delete', old.id, old.caption, old.ocr, old.tags);
      INSERT INTO media_fts(rowid, caption, ocr, tags_text)
      VALUES (new.id, new.caption, new.ocr, new.tags);
    END;

    -- One row per detected face. Kept apart from `media` because a picture
    -- can hold several, and because clustering rewrites these without
    -- touching the caption that took a GPU second to produce.
    CREATE TABLE IF NOT EXISTS faces (
      id         INTEGER PRIMARY KEY,
      media_id   INTEGER NOT NULL REFERENCES media(id) ON DELETE CASCADE,
      bbox       TEXT,               -- json [x, y, w, h], for cropping a thumbnail
      det_score  REAL,
      embedding  BLOB,               -- float32 x 512, normalised
      cluster_id INTEGER,            -- set by clustering, rewritten freely
      quality    REAL,
      -- For a video: the timestamp of the sampled frame this face was found
      -- in, and the width the detector actually saw. bbox is in THAT frame's
      -- coordinates, so a crop has to reproduce both to land on the face.
      -- NULL for a still image, where the detector saw the original.
      frame_ts   REAL,
      frame_w    INTEGER,
      -- Degrees clockwise the frame was turned upright BEFORE it was scaled
      -- and detected (a video filmed sideways, KSF-075). NULL/0 = as decoded.
      frame_turn INTEGER
    );
    CREATE INDEX IF NOT EXISTS faces_media ON faces(media_id);
    CREATE INDEX IF NOT EXISTS faces_cluster ON faces(cluster_id);
    -- Live distinct-file counts must not fetch the 2 KB embedding row.
    CREATE INDEX IF NOT EXISTS faces_cluster_media ON faces(cluster_id, media_id);
    -- A person's faces best-first: the People grid walks this to the first
    -- face in a Safe picture (server.py safe_person_cover).
    CREATE INDEX IF NOT EXISTS faces_cluster_quality ON faces(cluster_id, quality);

    -- A cluster is a person. The name is the user's; merging two clusters
    -- rewrites cluster_id on the losing side and leaves one row here.
    CREATE TABLE IF NOT EXISTS clusters (
      id         INTEGER PRIMARY KEY,
      name       TEXT,
      merged_into INTEGER,           -- set when this cluster was folded into another
      cover_face INTEGER,
      face_count INTEGER DEFAULT 0,
      updated_at REAL
    );

    -- One row per (file, tag). The same information is in media.tags as
    -- JSON, and that is what FTS searches - but "every character in the
    -- library, with a count" is a GROUP BY, and answering it by parsing
    -- 450k JSON blobs per request is not an answer. Written alongside the
    -- JSON, not instead of it: the JSON keeps the confidences together for
    -- one file, this keeps one tag across files.
    CREATE TABLE IF NOT EXISTS media_tags (
      media_id INTEGER NOT NULL REFERENCES media(id) ON DELETE CASCADE,
      tag      TEXT NOT NULL,
      category TEXT NOT NULL,          -- character | general | rating
      p        REAL,
      PRIMARY KEY (media_id, tag)
    ) WITHOUT ROWID;
    CREATE INDEX IF NOT EXISTS media_tags_tag ON media_tags(category, tag);
    -- A character's files, surest first: the character circles walk this to
    -- the first Safe picture (server.py safe_tag_cover). Partial: 0.4M of the
    -- 21M rows are characters, and the whole table would cost ~0.7 GB.
    CREATE INDEX IF NOT EXISTS media_tags_character_best ON media_tags(tag, p)
      WHERE category = 'character';

    -- What an account is expected to post. Set by the user; media_review
    -- also measures a shape from the files themselves, and the mark here
    -- wins when both exist - they know which account is which.
    CREATE TABLE IF NOT EXISTS profiles (
      source       TEXT NOT NULL,      -- twitter
      handle       TEXT NOT NULL,      -- @name
      kind         TEXT NOT NULL,      -- cosplayer | anime | mixed
      -- Rules this account is exempt from, comma separated: sketch, meme,
      -- screenshot, promo. Some artists post rough black-and-white work on
      -- purpose; some accounts exist to post game-render memes. In both cases
      -- the picture is exactly what the rule fires on and exactly what the
      -- account is for, and no tag can tell the two apart - only a person
      -- knows, so it is recorded per account.
      --
      -- A SET rather than a column per rule: the second exemption arrived
      -- within a day of the first, so a third is likely, and a schema change
      -- per rule would be the wrong shape.
      allow_sketch INTEGER NOT NULL DEFAULT 0,   -- superseded by `exempt`
      exempt       TEXT NOT NULL DEFAULT '',
      updated_at   REAL,
      PRIMARY KEY (source, handle)
    ) WITHOUT ROWID;

    -- The review queue. A row is a PROPOSAL: nothing here has been deleted
    -- and nothing here ever will be without a person saying so.
    CREATE TABLE IF NOT EXISTS suggestions (
      media_id   INTEGER PRIMARY KEY REFERENCES media(id) ON DELETE CASCADE,
      reason     TEXT NOT NULL,
      detail     TEXT,
      score      REAL,
      state      TEXT NOT NULL DEFAULT 'pending',   -- pending | kept | deleted
      created_at REAL,
      decided_at REAL
    );
    CREATE INDEX IF NOT EXISTS suggestions_state ON suggestions(state);

    -- Which character tags name the SAME character. Danbooru gives one
    -- character several tags - a costume, an ascension, a class - and the grid
    -- showed each as its own pool, so one person appeared several times.
    -- Materialised rather than derived per request: the evidence is a
    -- co-occurrence count over every tagged file. See rebuild_tag_variants.
    CREATE TABLE IF NOT EXISTS tag_variants (
      tag       TEXT PRIMARY KEY,
      group_key TEXT NOT NULL,      -- the canonical tag of the pool
      category  TEXT NOT NULL DEFAULT 'character'
    ) WITHOUT ROWID;
    CREATE INDEX IF NOT EXISTS tag_variants_group ON tag_variants(group_key);

    -- Every judgement a person has made, in either queue. Deliberately NOT
    -- a foreign key on media: a label has to survive the file being purged,
    -- and it carries its own copy of everything a training example needs.
    CREATE TABLE IF NOT EXISTS labels (
      id           INTEGER PRIMARY KEY,
      queue        TEXT NOT NULL,       -- review | training
      root         TEXT, path TEXT NOT NULL,
      media_id     INTEGER,             -- may dangle; for joining while it lives
      profile      TEXT, profile_kind TEXT,
      reason       TEXT, score REAL,    -- what the rules said, NULL if nothing
      flagged      INTEGER NOT NULL,    -- did the rules flag it, at label time
      verdict      TEXT NOT NULL,       -- belongs | not_belongs | unsure
                                        -- (review: keep -> belongs, delete -> not_belongs)
      acted        TEXT,                -- deleted | kept | none
      tags         TEXT,                -- the tag vector as it was, JSON
      decided_at   REAL, decided_by TEXT
    );
    CREATE INDEX IF NOT EXISTS labels_path ON labels(path);
    CREATE INDEX IF NOT EXISTS labels_queue ON labels(queue);

    CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);

    -- A music track's position in genre space (Discogs-EffNet, the genre
    -- pass): 1280 numbers as float16. Its STYLES live in media_tags with
    -- category 'genre', so browsing and search treat them like any tag.
    CREATE TABLE IF NOT EXISTS audio_embeddings (
      media_id INTEGER PRIMARY KEY,
      model    TEXT NOT NULL,
      dim      INTEGER NOT NULL,
      vec      BLOB NOT NULL
    );
    """)
    _add_column(conn, 'profiles', 'allow_sketch',
                'INTEGER NOT NULL DEFAULT 0')
    _add_column(conn, 'profiles', 'exempt', "TEXT NOT NULL DEFAULT ''")
    _add_column(conn, 'media', 'genre_model', 'TEXT')
    # The orientation pass (media_indexer "orient"): its stamp, and the answer
    # as JSON - {"deg": clockwise turn that makes the picture upright, "votes":
    # frames per turn, "frames": frames looked at}. "" = looked, no frames.
    _add_column(conn, 'media', 'orient_model', 'TEXT')
    _add_column(conn, 'media', 'orient', 'TEXT')
    # The videos it found turned, and nothing else: the server reads all of
    # them for every listing's map, and finding them in the media table is a
    # scan of the whole 14 GB store (minutes on this disk, measured
    # 2026-11-08). Keyed like the server's own stores, by root and path.
    conn.execute("CREATE TABLE IF NOT EXISTS media_orient ("
                 " root TEXT NOT NULL, path TEXT NOT NULL, deg INTEGER NOT NULL,"
                 " PRIMARY KEY (root, path))")
    # Carry the older single flag into the set, once. Reading both would mean
    # two sources of truth for the same question.
    conn.execute("UPDATE profiles SET exempt='sketch'"
                 " WHERE allow_sketch=1 AND exempt=''")
    # Additive migrations: the CREATE above is a no-op on an existing table,
    # so a column added later has to be applied explicitly.
    have = {r[1] for r in conn.execute("PRAGMA table_info(faces)")}
    for column, decl in (("frame_ts", "REAL"), ("frame_w", "INTEGER"),
                           ("frame_turn", "INTEGER")):
        if column not in have:
            conn.execute(f"ALTER TABLE faces ADD COLUMN {column} {decl}")

    # Deleting a file in the UI moves it to the root's .trash, which the
    # indexer's walk skips - so without this the row lived on and the file
    # went on being counted in the character pools and the people tiles.
    # A timestamp rather than a flag, and the rows are KEPT: a restore is
    # common and a caption costs seconds of GPU time to produce again.
    have = {r[1] for r in conn.execute("PRAGMA table_info(clusters)")}
    if "hidden" not in have:
        # Set by hand from the People grid. Separate from the drawn/real
        # judgement, which is measured and can change as tags arrive - a
        # person's decision must not be overwritten by a re-measurement.
        conn.execute("ALTER TABLE clusters ADD COLUMN hidden INTEGER DEFAULT 0")

    have = {r[1] for r in conn.execute("PRAGMA table_info(media)")}
    if "trashed" not in have:
        conn.execute("ALTER TABLE media ADD COLUMN trashed REAL")
        conn.execute("CREATE INDEX IF NOT EXISTS media_trashed ON media(trashed)")
    # "<device>:<inode>": which FILE a row is about, not which name. A song
    # stored once and hard-linked into every playlist that holds it has one
    # inode and several paths, and is analysed and listed once (KSF-044).
    if "ino" not in have:
        conn.execute("ALTER TABLE media ADD COLUMN ino TEXT")
    conn.execute("CREATE INDEX IF NOT EXISTS media_ino ON media(ino)")

    conn.execute("INSERT OR IGNORE INTO meta(k, v) VALUES('schema', ?)",
                 (str(SCHEMA_VERSION),))
    conn.commit()


# ----------------------------------------------------------------- writing --

def upsert(conn: sqlite3.Connection, root: str, path: str, size: int | None,
           kind: str | None, fields: dict) -> int:
    """Writes what a worker produced for one file and returns its row id.

    Only the fields present are touched: the caption pass and the face pass
    run separately and must not blank each other's work. That is why this is
    a read-modify-write of named columns rather than an INSERT OR REPLACE.
    """
    cur = conn.execute("SELECT id FROM media WHERE root=? AND path=?", (root, path))
    row = cur.fetchone()
    cols = {k: v for k, v in fields.items()
            if k in ("caption", "ocr", "tags", "ino", "orient", *MODEL_KEYS)}
    cols["indexed_at"] = time.time()
    if row is None:
        cols.update({"root": root, "path": path, "size": size, "kind": kind})
        names = ", ".join(cols)
        marks = ", ".join("?" for _ in cols)
        cur = conn.execute(f"INSERT INTO media ({names}) VALUES ({marks})",
                           tuple(cols.values()))
        return cur.lastrowid
    if size is not None:
        cols["size"] = size
    if kind is not None:
        cols["kind"] = kind
    sets = ", ".join(f"{k}=?" for k in cols)
    conn.execute(f"UPDATE media SET {sets} WHERE id=?",
                 (*cols.values(), row["id"]))
    return row["id"]


def indexed_elsewhere(conn: sqlite3.Connection, ino: str, root: str, path: str) -> bool:
    """Is another name for this same file already in the index? A song
    hard-linked into playlist folders is one song: the first name seen carries
    its caption, tags and genre, and the others are skipped rather than
    analysed again and listed again beside it."""
    if not ino:
        return False
    row = conn.execute(
        "SELECT 1 FROM media WHERE ino = ? AND NOT (root = ? AND path = ?) LIMIT 1",
        (ino, root, path)).fetchone()
    return row is not None


def replace_tags(conn: sqlite3.Connection, media_id: int, tags: dict) -> None:
    """This file's tags, replaced wholesale. Re-tagging must not accumulate,
    and a tag that dropped below the threshold on a re-run has to disappear
    rather than linger as a character the file no longer shows."""
    conn.execute("DELETE FROM media_tags WHERE media_id=?", (media_id,))
    seen = set()
    for category in ("character", "general", "rating"):
        for entry in (tags or {}).get(category) or []:
            tag = entry.get("tag")
            if not tag or tag in seen:
                continue
            seen.add(tag)
            conn.execute(
                "INSERT INTO media_tags (media_id, tag, category, p)"
                " VALUES (?, ?, ?, ?)",
                (media_id, tag, category, entry.get("p")))


def replace_genres(conn: sqlite3.Connection, media_id: int, styles) -> None:
    """A track's music styles, replaced wholesale: [(label, p)], the Discogs
    label as the tag ("Electronic---House"). Only the genre rows are touched -
    replace_tags owns the others."""
    conn.execute("DELETE FROM media_tags WHERE media_id=? AND category='genre'", (media_id,))
    conn.executemany("INSERT OR IGNORE INTO media_tags (media_id, tag, category, p) VALUES (?, ?, 'genre', ?)",
                     [(media_id, label, p) for label, p in styles])


def store_embedding(conn: sqlite3.Connection, media_id: int, model: str, vec: bytes, dim: int) -> None:
    conn.execute("INSERT INTO audio_embeddings (media_id, model, dim, vec) VALUES (?, ?, ?, ?)"
                 " ON CONFLICT(media_id) DO UPDATE SET model=excluded.model, dim=excluded.dim, vec=excluded.vec",
                 (media_id, model, dim, vec))


def audio_embeddings(conn: sqlite3.Connection):
    """[(media_id, root, path, float32 vector)] for every track outside the
    trash with an embedding."""
    import numpy as np
    out = []
    for mid, root, path, dim, vec in conn.execute(
            "SELECT e.media_id, m.root, m.path, e.dim, e.vec FROM audio_embeddings e"
            " JOIN media m ON m.id = e.media_id WHERE m.trashed IS NULL"):
        v = np.frombuffer(vec, dtype="<f2").astype(np.float32)
        if v.shape[0] == dim:
            out.append((mid, root, path, v))
    return out


def replace_faces(conn: sqlite3.Connection, media_id: int, faces: list) -> None:
    """A file's faces, replaced wholesale. Re-running detection on a file must
    not double its faces, and there is no stable identity to match them by."""
    conn.execute("DELETE FROM faces WHERE media_id=?", (media_id,))
    for f in faces:
        conn.execute(
            "INSERT INTO faces (media_id, bbox, det_score, embedding, quality,"
            " frame_ts, frame_w, frame_turn) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (media_id, json.dumps(f.get("bbox")), f.get("det_score"),
             f.get("embedding"), f.get("quality"),
             f.get("frame_ts"), f.get("frame_w"), f.get("frame_turn") or None))


def needs_work(conn: sqlite3.Connection, root: str, path: str, size: int,
               models: dict) -> bool:
    """Whether this file still has to be looked at.

    True when it is unknown, when its size changed (the file was replaced),
    or when any pass was produced by a model that is no longer the current
    one - so a model upgrade re-does its own pass and nothing else.
    """
    # Every stamp in MODEL_KEYS: a fixed list here left the genre pass
    # crashing on its second sweep, once its own stamp was the one asked for.
    row = conn.execute(
        "SELECT size, " + ", ".join(MODEL_KEYS) +
        " FROM media WHERE root=? AND path=?", (root, path)).fetchone()
    if row is None or row["size"] != size:
        return True
    return any(row[key] != want for key, want in models.items() if want)


# ----------------------------------------------------------------- reading --

# Which columns each search mode looks at. "all" is deliberately not the
# union of every column plus the filename: the filename search runs against a
# different index entirely (the path index in server.py), so the caller
# combines them rather than this doing it badly.
SEARCH_COLUMNS = {
    # Captions AND tags. The caption pass has reached a small part of the
    # library and the tagger nearly all of it: within one character's 5,255
    # files "beach" is in 6 captions and in 161 tag lists (2026-09-14).
    "context": ("caption", "tags_text"),
    "ocr": ("ocr",),
    "tags": ("tags_text",),
    "ai": ("caption", "ocr", "tags_text"),
}


def _fts_query(text: str, columns: tuple) -> str:
    """A user's words as an FTS5 MATCH expression, restricted to columns.

    Every term is quoted: FTS5 reads bare punctuation as operators, so a
    search for "girl (cosplay)" is a syntax error rather than a search.
    """
    terms = [t for t in text.replace('"', " ").split() if t]
    if not terms:
        return ""
    body = " AND ".join(f'"{t}"*' for t in terms)
    return " OR ".join(f"{{{col}}} : ({body})" for col in columns)


def search(conn: sqlite3.Connection, text: str, mode: str, limit: int = 5000):
    """(root, path) for every file whose AI text matches, best first.

    Returns paths rather than entries: the caller owns permissions, paging and
    what a file looks like, and this must not learn any of that.
    """
    columns = SEARCH_COLUMNS.get(mode)
    if not columns:
        return []
    expr = _fts_query(text, columns)
    if not expr:
        return []
    try:
        rows = conn.execute(
            "SELECT m.root, m.path FROM media_fts f"
            " JOIN media m ON m.id = f.rowid"
            " WHERE media_fts MATCH ? AND m.trashed IS NULL"
            " ORDER BY rank LIMIT ?",
            (expr, limit)).fetchall()
    except sqlite3.OperationalError:
        # A query FTS5 cannot parse is a bad search, not a broken server.
        return []
    return [(r["root"], r["path"]) for r in rows]


def search_ids(conn: sqlite3.Connection, text: str, mode: str, within) -> set:
    """Of the media ids in `within`, those whose AI text matches - how words
    narrow a character, a person or the favourites.

    The matching rowids are read once and filtered here rather than joined
    against the subject in SQL: FTS5 seeks by rowid by re-running the MATCH,
    and the join and IN shapes did not finish in five minutes on a common word
    ("hair", 2026-09-14), while this reads that doclist in 0.3 s. Nothing is cut
    at a limit either - a cap before narrowing would drop the subject's files.
    """
    columns = SEARCH_COLUMNS.get(mode)
    expr = _fts_query(text, columns) if columns else ""
    if not expr or not within:
        return set()
    try:
        return {r[0] for r in conn.execute(
            "SELECT rowid FROM media_fts WHERE media_fts MATCH ?", (expr,)) if r[0] in within}
    except sqlite3.OperationalError:
        return set()


def ids_for_paths(conn: sqlite3.Connection, pairs) -> dict:
    """{media id: (root, path)} for the (root, path) pairs the index knows."""
    out = {}
    for root, path in pairs:
        row = conn.execute("SELECT id FROM media WHERE root=? AND path=?", (root, path)).fetchone()
        if row is not None:
            out[row[0]] = (root, path)
    return out


def tag_counts(conn: sqlite3.Connection, category="character",
               query=None, limit=500, min_count=1):
    """(tag, count) for a category, most-used first.

    `query` is a substring filter, which is what makes type-ahead possible:
    the vocabulary is fixed (~10k danbooru character names), so completing
    against it gives spellings that will actually match something.
    """
    sql = ("SELECT t.tag AS tag, COUNT(*) AS n FROM media_tags t"
           " JOIN media m ON m.id = t.media_id"
           " WHERE t.category=? AND m.trashed IS NULL")
    args = [category]
    if query:
        # Both sides normalised to spaces. Danbooru writes `houshou_marine`
        # but a person types either form, and matching the raw column meant
        # typing the underscore spelling found nothing - the one spelling a
        # user is most likely to paste back from a result.
        sql += " AND REPLACE(tag, '_', ' ') LIKE ?"
        args.append("%" + query.replace("%", "").replace("_", " ").lower() + "%")
    sql += " GROUP BY tag HAVING n >= ? ORDER BY n DESC, tag LIMIT ?"
    args += [min_count, limit]
    return [(r["tag"], r["n"]) for r in conn.execute(sql, args)]


# How much two tags of the same character have to overlap to be called one
# character. Measured, not guessed: over the 292 pairs that share a base name
# the overlaps are bimodal - 184 sit at 0.60 or above (mostly exactly 1.00,
# the tagger emitting both tags on the same picture) and 74 sit at exactly
# 0.00. Anywhere in the empty middle gives the same answer; 0.35 is the
# middle of the gap. See rebuild_tag_variants.
VARIANT_OVERLAP = 0.35


def _variant_base(tag: str) -> str:
    """The name with every parenthesised qualifier stripped."""
    cut = tag.find("_(")
    return tag if cut < 0 else tag[:cut]


def rebuild_tag_variants(conn: sqlite3.Connection, category="character",
                         threshold=VARIANT_OVERLAP):
    """Work out which tags name the same character, and store the result.

    THE PROBLEM. Danbooru does not give a character one tag. It gives her one
    per costume, per ascension art and per class - artoria_pendragon_(fate),
    artoria_pendragon_(lancer)_(fate), artoria_pendragon_(swimsuit_ruler)_(fate)
    and three more. Grouping the grid by raw tag therefore showed the same
    person six times, which is what a user reported seeing.

    WHY NOT PARSE THE NAME. The obvious rule - strip the brackets, group by
    what is left - is wrong on real data, and wrong in the direction that
    merges two different people into one pool: asuna_(sao) and
    asuna_(blue_archive) are different characters, as are albedo_(overlord)
    and albedo_(genshin_impact), commander_(azur_lane) and the Girls Frontline
    commander, and every Azur Lane ship-girl who shares a warship name with a
    KanColle one. Nor can the series be read off the last bracket:
    hoshimachi_suisei_(1st_costume) would be filed under the series "1st
    costume". The tagger emits no copyright tags, so there is no second field
    to consult either.

    WHAT THE DATA SAYS INSTEAD. Two tags of one character are emitted TOGETHER
    on the same picture - the tagger says both "artoria pendragon" and "lancer
    artoria" about one image - while two different people who happen to share
    a name never are. Overlap, as a share of the smaller tag, is bimodal: 184
    of 292 same-base pairs sit at 0.60+, 74 at exactly 0.00, and the band
    between is nearly empty.

    WHY COMPONENTS AND NOT PAIRS. Two costumes of one character are mutually
    exclusive within a picture, so they overlap 0.00 with each other - all six
    Artoria variants do. They are linked through the plain tag, which does
    co-occur with each of them. Taking connected components picks that up;
    pairwise merging would leave the outfits stranded. Sharing a base name
    remains a precondition of every edge, so a component can never wander off
    into a different character who merely appears in the same pictures.

    Only tags that end up in a group of two or more are stored, so a bare
    COALESCE(group_key, tag) is the pool key for every tag in the library.
    """
    members: dict[str, set[int]] = {}
    for row in conn.execute(
            "SELECT t.tag AS tag, t.media_id AS mid FROM media_tags t"
            " JOIN media m ON m.id = t.media_id"
            " WHERE t.category = ? AND m.trashed IS NULL", (category,)):
        members.setdefault(row["tag"], set()).add(row["mid"])

    by_base: dict[str, list[str]] = {}
    for tag in members:
        by_base.setdefault(_variant_base(tag), []).append(tag)

    parent = {tag: tag for tag in members}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for tags in by_base.values():
        if len(tags) < 2:
            continue
        for a_i in range(len(tags)):
            for b_i in range(a_i + 1, len(tags)):
                a, b = tags[a_i], tags[b_i]
                first, second = members[a], members[b]
                smaller = min(len(first), len(second))
                if smaller and len(first & second) / smaller >= threshold:
                    ra, rb = find(a), find(b)
                    if ra != rb:
                        parent[ra] = rb

    groups: dict[str, list[str]] = {}
    for tag in members:
        groups.setdefault(find(tag), []).append(tag)

    rows = []
    for tags in groups.values():
        if len(tags) < 2:
            continue
        # The canonical tag is the least qualified one - artoria_pendragon_(fate)
        # over artoria_pendragon_(lancer)_(fate), hoshimachi_suisei over
        # hoshimachi_suisei_(1st_costume). That is both the name a person would
        # use and the only choice that leaves the trailing bracket meaning the
        # series, which is what the label is built from.
        key = min(tags, key=lambda t: (t.count("("), -len(members[t]), t))
        rows.extend((t, key, category) for t in tags)

    with conn:
        conn.execute("DELETE FROM tag_variants WHERE category = ?", (category,))
        conn.executemany(
            "INSERT OR REPLACE INTO tag_variants(tag, group_key, category)"
            " VALUES (?, ?, ?)", rows)
    return len(rows)


def variant_map(conn: sqlite3.Connection, category="character"):
    """{tag: group_key} for tags that share a character with another tag."""
    return {r["tag"]: r["group_key"] for r in conn.execute(
        "SELECT tag, group_key FROM tag_variants WHERE category = ?", (category,))}


def _pool_group_expr(category):
    """SQL for the pool key. Only characters are grouped: the general
    vocabulary has no variant spellings to merge, and its 3.2M rows would pay
    for a join that could never match."""
    return "COALESCE(v.group_key, t.tag)" if category == "character" else "t.tag"


def _pool_count_expr(category):
    """How many files a pool holds.

    DISTINCT only where pools are groups: a picture carrying both
    "artoria pendragon" and "lancer artoria" is one picture, and counting rows
    claimed 1,048 where the truth was 887. Everywhere else (media_id, tag) is
    the primary key, so the two counts are equal by construction and the
    cheaper one is used - DISTINCT over the 3.2M general rows cost 5.4s.
    """
    return ("COUNT(DISTINCT t.media_id)" if category == "character"
            else "COUNT(*)")


def _pool_from(category):
    join = (" LEFT JOIN tag_variants v ON v.tag = t.tag AND v.category = 'character'"
            if category == "character" else "")
    return (" FROM media_tags t JOIN media m INDEXED BY media_trashed ON m.id = t.media_id" + join
            + " WHERE t.category = ? AND m.trashed IS NULL")


def _pool_query_clause(category, group, query, args):
    """Restrict a pool query to pools matching the type-ahead text."""
    like = "%" + query.replace("%", "").replace("_", " ").lower() + "%"
    if category != "character":
        args.append(like)
        return " AND REPLACE(t.tag, '_', ' ') LIKE ?"
    # A hit on ANY member has to bring back the whole pool, counts and all -
    # typing "suisei" must not silently drop her other costumes from the total.
    args.extend([category, like])
    return (" AND " + group + " IN (SELECT COALESCE(v2.group_key, t2.tag)"
            " FROM media_tags t2"
            " LEFT JOIN tag_variants v2 ON v2.tag = t2.tag"
            " AND v2.category = 'character'"
            " WHERE t2.category = ? AND REPLACE(t2.tag, '_', ' ') LIKE ?)")


def tag_pools(conn: sqlite3.Connection, category="character", query=None,
              limit=300, min_count=1, keys=None):
    """One dict per POOL, biggest first: tag, count, cover, variants.

    A pool is usually one tag. For characters it is the GROUP of tags that
    name the same character (rebuild_tag_variants), so a character with six
    costume tags is one tile with six variants rather than six tiles. The
    count is DISTINCT files, not the sum of the members: a picture carrying
    both "artoria pendragon" and "lancer artoria" is one picture, and summing
    claimed 1,048 where the truth was 887.

    Count pools using the compact trash-state index, without reading large
    media rows. Count member tags separately, then seek each tag's highest
    confidence live cover. Reading media paths for every matching row merely
    to find MAX(p) caused gigabytes of unnecessary I/O on the real library.

    The cover is still the file the tagger was most confident about, so a pool
    shows its clearest example.
    """
    group = _pool_group_expr(category)
    sql = ("SELECT " + group + " AS g, " + _pool_count_expr(category) + " AS n"
           + _pool_from(category))
    args = [category]
    if query:
        sql += _pool_query_clause(category, group, query, args)
    if keys:
        # Named pools only. A link to a character has to be reopenable
        # without the grid it was clicked in ever having been built.
        sql += " AND " + group + " IN (" + ",".join("?" * len(keys)) + ")"
        args += list(keys)
    sql += " GROUP BY g HAVING n >= ? ORDER BY n DESC, g LIMIT ?"
    args += [min_count, limit]
    counts = [(r["g"], r["n"]) for r in conn.execute(sql, args)]
    if not counts:
        return []

    # Every tag whose rows belong to a pool on this page: the pool keys
    # themselves plus, for a grouped pool, its other spellings.
    keys = [g for g, _ in counts]
    siblings: dict[str, list[str]] = {g: [g] for g in keys}
    if category == "character":
        wanted = set(keys)
        for tag, key in variant_map(conn).items():
            if key in wanted and tag != key:
                siblings[key].append(tag)

    tags = [t for group_tags in siblings.values() for t in group_tags]
    covers, member_counts = {}, {}
    # Chunked: a host SQLite may cap parameters at 999 and the caller is
    # allowed to ask for more pools than that.
    for i in range(0, len(tags), 400):
        chunk = tags[i:i + 400]
        holes = ",".join("?" * len(chunk))
        for r in conn.execute(
                "SELECT t.tag AS tag, COUNT(*) AS n FROM media_tags t"
                " JOIN media m INDEXED BY media_trashed ON m.id=t.media_id"
                " WHERE t.category=? AND m.trashed IS NULL AND t.tag IN ("+holes+") GROUP BY t.tag", [category]+chunk):
            member_counts[r["tag"]] = r["n"]
        for tag in chunk:
            r = conn.execute(
                "SELECT t.p AS best,m.root,m.path FROM media_tags t JOIN media m ON m.id=t.media_id"
                " WHERE t.category=? AND t.tag=? AND m.trashed IS NULL ORDER BY t.p DESC LIMIT 1", (category,tag)).fetchone()
            if r: covers[tag]=(r["best"] or 0,r["root"],r["path"])

    pools = []
    for key, n in counts:
        group_tags = sorted(siblings[key],
                            key=lambda t: (-member_counts.get(t, 0), t))
        # Each member carries its highest-confidence live picture; the pool
        # shows whichever member the tagger was surest about.
        best = max((covers[t] for t in group_tags if t in covers),
                   key=lambda c: c[0], default=(0, None, None))
        pools.append({
            "tag": key,
            "count": n,
            "cover": (best[1], best[2]),
            "variants": [{"tag": t, "count": member_counts.get(t, 0),
                          "cover": (covers[t][1], covers[t][2]) if t in covers
                          else (None, None)}
                         for t in group_tags] if len(group_tags) > 1 else [],
        })
    return pools


def tag_pool_total(conn: sqlite3.Connection, category="character", query=None,
                   min_count=1):
    """How many pools there are, ignoring any page size.

    Separate from tag_pools because a caller that asks for 500 rows must not
    have to infer the total from how many it got back - that is exactly the
    mistake the characters page was making.
    """
    group = _pool_group_expr(category)
    sql = "SELECT COUNT(*) AS n FROM (SELECT " + group + " AS g" + _pool_from(category)
    args = [category]
    if query:
        sql += _pool_query_clause(category, group, query, args)
    sql += " GROUP BY g HAVING " + _pool_count_expr(category) + " >= ?)"
    args.append(min_count)
    return conn.execute(sql, args).fetchone()["n"]


def files_for_tag(conn: sqlite3.Connection, tag, limit=5000):
    """(root, path) for everything carrying one tag, best confidence first -
    so a pool's cover picture is the clearest example of the character rather
    than whichever file sorted first.

    `tag` may also be SEVERAL tags, which is how a character with a tag per
    costume is browsed as one pool. A file carrying two of them is returned
    once, ranked by the better of the two confidences.
    """
    tags = [tag] if isinstance(tag, str) else list(tag)
    if not tags:
        return []
    holes = ",".join("?" * len(tags))
    rows = conn.execute(
        "SELECT m.root AS root, m.path AS path, MAX(t.p) AS p"
        " FROM media_tags t JOIN media m ON m.id = t.media_id"
        " WHERE t.tag IN (" + holes + ") AND m.trashed IS NULL"
        " GROUP BY m.root, m.path ORDER BY p DESC LIMIT ?",
        tags + [limit]).fetchall()
    return [(r["root"], r["path"]) for r in rows]


# ---- content level -------------------------------------------------------------
# How sexual a file is, on four levels: 0 general, 1 sensitive, 2 nudity or
# otherwise questionable, 3 sex. WD14's rating is danbooru's scale. It holds up
# on drawings - 96-99% of the files it tags nude or nipples are rated
# questionable or explicit - but is soft on photographs, where 27% of the photos
# it tags "nude" were rated general or sensitive (measured 2026-09-14). So a
# confident nudity or sex tag lifts a file as well. "censored" is not one of
# them: 990 files rated general or sensitive carried it with no other marker,
# so on its own it does not show nudity.
RATING_LEVELS = {"general": 0, "sensitive": 1, "questionable": 2, "explicit": 3}
NUDITY_TAGS = ("nude", "completely_nude", "nipples", "areolae", "areola_slip", "nipple_slip",
               "topless", "bottomless", "breasts_out", "pubic_hair", "anus")
SEX_TAGS = ("pussy", "penis", "testicles", "erection", "sex", "vaginal", "anal", "fellatio",
            "paizuri", "handjob", "masturbation", "clitoris", "spread_pussy", "cum_in_pussy")
CONTENT_MARKER_MIN_P = 0.5


def _marker_level(tag):
    return 3 if tag in SEX_TAGS else 2


def content_levels(conn: sqlite3.Connection) -> dict:
    """{(root, path): level} for every file outside the trash with evidence:
    its highest rating tag, lifted by confident nudity (2) or sex (3) tags. A
    file with neither - no rating over 0.5, or not tagged yet - is absent,
    which is what "not rated" means to the content filter.

    Levels are gathered per media id first and joined to paths once, on a
    cursor without the Row factory: ~0.7 M tag rows would otherwise become as
    many Row objects, and every root name a string of its own.
    """
    cur = conn.cursor()
    cur.row_factory = None
    by_id = {}
    for media_id, tag in cur.execute(
            "SELECT media_id, tag FROM media_tags WHERE category = 'rating'"):
        level = RATING_LEVELS.get(tag)
        if level is not None and level > by_id.get(media_id, -1):
            by_id[media_id] = level
    markers = NUDITY_TAGS + SEX_TAGS
    for media_id, tag in cur.execute(
            "SELECT media_id, tag FROM media_tags WHERE category = 'general'"
            " AND tag IN (" + ",".join("?" * len(markers)) + ") AND p >= ?",
            markers + (CONTENT_MARKER_MIN_P,)):
        level = _marker_level(tag)
        if level > by_id.get(media_id, -1):
            by_id[media_id] = level
    roots, levels = {}, {}
    for media_id, root, path in cur.execute(
            "SELECT id, root, path FROM media WHERE trashed IS NULL"):
        level = by_id.get(media_id)
        if level is not None:
            levels[(roots.setdefault(root, root), path)] = level
    return levels


def content_evidence(conn: sqlite3.Connection, root: str, path: str):
    """(level or None, evidence) for one file: the rating tags and the
    nudity/sex markers that decide its level, as {"tag", "kind", "p", "level"}
    dicts, strongest level first."""
    row = conn.execute("SELECT id FROM media WHERE root = ? AND path = ?",
                       (root, path)).fetchone()
    if not row:
        return None, []
    markers = NUDITY_TAGS + SEX_TAGS
    rows = conn.execute(
        "SELECT tag, category, p FROM media_tags WHERE media_id = ? AND (category = 'rating'"
        " OR (category = 'general' AND tag IN (" + ",".join("?" * len(markers)) + ") AND p >= ?))",
        (row[0],) + markers + (CONTENT_MARKER_MIN_P,)).fetchall()
    evidence = []
    for tag, category, p in rows:
        level = RATING_LEVELS.get(tag) if category == "rating" else _marker_level(tag)
        if level is not None:
            evidence.append({"tag": tag, "kind": category, "p": p, "level": level})
    evidence.sort(key=lambda e: (-e["level"], -(e["p"] or 0)))
    return (evidence[0]["level"] if evidence else None), evidence


ORIENT_TURNS = (90, 180, 270)
# ffmpeg filter that turns a decoded frame upright, per clockwise degrees.
TURN_FILTERS = {90: "transpose=1", 180: "hflip,vflip", 270: "transpose=2"}


def set_orient(conn: sqlite3.Connection, root: str, path: str, deg: int) -> None:
    """Records the orientation pass's turn for one video in media_orient: a
    row while it is turned, none while it is upright."""
    if deg in ORIENT_TURNS:
        conn.execute("INSERT OR REPLACE INTO media_orient (root, path, deg) VALUES (?, ?, ?)",
                     (root, path, deg))
    else:
        conn.execute("DELETE FROM media_orient WHERE root = ? AND path = ?", (root, path))


def orientations(conn: sqlite3.Connection) -> dict:
    """{(root, path): degrees clockwise} for every video in the library whose
    picture the orientation pass found lying on its side or upside down.
    Only the turned ones: everything else is upright, or not looked at yet.
    A few hundred rows of media_orient, each checked against the trash by the
    media table's (root, path) index - never a scan of the media table.
    CROSS JOIN pins that order: with a plain JOIN and no table statistics the
    planner may start from media via media_trashed ("trashed IS NULL" looks
    selective to it) and walk every live row of the library."""
    out = {}
    for root, path, deg in conn.execute(
            "SELECT o.root, o.path, o.deg FROM media_orient o"
            " CROSS JOIN media m ON m.root = o.root AND m.path = o.path"
            " WHERE m.trashed IS NULL"):
        if deg in ORIENT_TURNS:
            out[(root, path)] = deg
    return out


def orientation(conn: sqlite3.Connection, root: str, path: str):
    """What the orientation pass decided for one file, for the info panel:
    {"deg", "votes", "frames"}, {} when it looked and had nothing to look at,
    None when it has not looked yet."""
    row = conn.execute("SELECT orient_model, orient FROM media WHERE root = ? AND path = ?",
                       (root, path)).fetchone()
    if not row or row[0] is None:
        return None
    try:
        rec = json.loads(row[1]) if row[1] else {}
    except ValueError:
        return {}
    return rec if isinstance(rec, dict) else {}


def set_trashed(conn: sqlite3.Connection, items, trashed=True):
    """Mark files as trashed, or bring them back. `items` is (root, path).

    The rows are kept either way. Purging is what deletes them - see
    forget(). Returns how many rows it actually touched, which is how the
    caller can tell that a restore under a different name missed.
    """
    stamp = time.time() if trashed else None
    n = 0
    for root, path in items:
        n += conn.execute("UPDATE media SET trashed = ? WHERE root = ? AND path = ?",
                          (stamp, root, path)).rowcount
    conn.commit()
    return n


def forget(conn: sqlite3.Connection, items):
    """Drop everything known about these files - the purge case, where the
    bytes are gone for good. Tags and faces follow via ON DELETE CASCADE."""
    n = 0
    for root, path in items:
        n += conn.execute("DELETE FROM media WHERE root = ? AND path = ?",
                          (root, path)).rowcount
        # Keyed by path, not by row: no cascade reaches it.
        conn.execute("DELETE FROM media_orient WHERE root = ? AND path = ?", (root, path))
    conn.commit()
    return n


def coverage(conn: sqlite3.Connection) -> dict:
    """How much of the library has been looked at, per pass. The filter panel
    says this out loud: a half-built index looks exactly like a search that
    loses results at random."""
    row = conn.execute(
        "SELECT COUNT(*) AS n,"
        # != '' as well as NOT NULL: an empty string records a pass that
        # ran and found nothing, which is done but is not a result.
        " SUM(caption IS NOT NULL AND caption != '') AS captioned,"
        " SUM(ocr IS NOT NULL AND ocr != '') AS ocred,"
        " SUM(tags IS NOT NULL AND tags != '') AS tagged"
        # Music tracks (the genre pass) are counted apart: the picture and
        # video passes never touch them, so in these totals they would keep
        # every bar short of its end.
        " FROM media WHERE kind IS NOT 'audio'").fetchone()
    faces = conn.execute("SELECT COUNT(*) AS n FROM faces").fetchone()
    people = conn.execute(
        "SELECT COUNT(*) AS n FROM clusters WHERE merged_into IS NULL").fetchone()
    chars = conn.execute(
        "SELECT COUNT(DISTINCT tag) AS n FROM media_tags"
        " WHERE category='character'").fetchone()
    # For progress, what counts is whether a pass RAN on a file (its model
    # stamp), over files that are still in the library. The result counts
    # above include trashed files and leave out a pass that found nothing, so
    # a bar built on them never reaches the end - 508,517 of 532,997 "tagged"
    # with tagging long finished.
    live = conn.execute(
        "SELECT COUNT(*) AS n,"
        " SUM(caption_model IS NOT NULL) AS caption_done,"
        " SUM(ocr_model IS NOT NULL) AS ocr_done,"
        " SUM(tag_model IS NOT NULL) AS tags_done,"
        " SUM(face_model IS NOT NULL) AS faces_done"
        " FROM media WHERE trashed IS NULL AND kind IS NOT 'audio'").fetchone()
    music = conn.execute(
        "SELECT COUNT(*) AS n, SUM(genre_model IS NOT NULL) AS done"
        " FROM media WHERE trashed IS NULL AND kind = 'audio'").fetchone()
    styles = conn.execute(
        "SELECT COUNT(DISTINCT tag) AS n FROM media_tags WHERE category='genre'").fetchone()
    return {"files": row["n"] or 0, "captioned": row["captioned"] or 0,
            "ocr": row["ocred"] or 0, "tagged": row["tagged"] or 0,
            "characters": chars["n"] or 0,
            "faces": faces["n"] or 0, "people": people["n"] or 0,
            "indexable": live["n"] or 0,
            "caption_done": live["caption_done"] or 0,
            "ocr_done": live["ocr_done"] or 0,
            "tags_done": live["tags_done"] or 0,
            "faces_done": live["faces_done"] or 0,
            "music": music["n"] or 0, "genre_done": music["done"] or 0,
            "genre_styles": styles["n"] or 0}
