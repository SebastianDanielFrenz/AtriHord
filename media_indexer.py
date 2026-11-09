"""Feeds the library to the vision worker and stores what comes back.

Runs on the media VM, where the files are. The GPU is on another host, so this
posts ORIGINALS over the LAN a batch at a time and keeps nothing there.

Shape of a sweep:

    walk the roots once  ->  newest first  ->  skip what needs_work() says is
    done  ->  batch of 8  ->  POST  ->  write rows  ->  repeat until the
    window closes or the work runs out

Why newest-first: a 450k backlog takes days, and the files someone actually
wants to find are the ones that arrived recently. Sorted oldest-first the
useful part of the index would appear last.

Why a fresh walk per sweep rather than a persisted queue: the queue would be
another thing to keep true as files arrive and are deleted, and the walk costs
minutes once a night against a sweep measured in hours. `needs_work()` is the
resume mechanism - it reads the DB, so stopping and restarting loses nothing
but the walk.

Videos are sampled, not watched: FRAMES_PER_VIDEO stills, decoded straight to
memory. The captions are joined and the tags unioned, so searching for what
happens in a clip finds it without storing a caption per frame.
"""
from __future__ import annotations

import argparse
import io
import json
import mimetypes
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid

import media_ai

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_NAME = "indexer_config.json"

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif"}
VIDEO_EXTS = {".mp4", ".webm", ".mkv", ".mov", ".m4v", ".avi"}
# Music, for the genre pass only (the picture passes never see it).
def inode_key(path):
    """"<device>:<inode>" for a file, or None. Two names for one file (a song
    hard-linked into a playlist folder) share it."""
    try:
        st = os.stat(path)
    except OSError:
        return None
    return f"{st.st_dev}:{st.st_ino}"


AUDIO_EXTS = {".mp3", ".m4a", ".flac", ".opus", ".ogg", ".mka", ".wav", ".aac"}

# Four stills at 15/35/60/85% of the runtime. Not the first frame: youtube and
# twitter clips routinely open on a title card or a black frame, which
# captions as "a black background" and tells you nothing about the video.
FRAME_POSITIONS = (0.15, 0.35, 0.60, 0.85)
FRAMES_PER_VIDEO = len(FRAME_POSITIONS)

# How densely a video is sampled, per source, as (seconds per frame, ceiling).
#
# Four frames for everything was set for twitter clips of eight to fifteen
# seconds. On a seven-minute cooking video that is one frame every 105 seconds:
# all four landed on the food, the cosplayer was never sampled, no face was
# found, and the file was proposed for deletion as "nobody in the picture".
#
# The two sources need different answers because their videos are different
# lengths by construction. Twitter caps an upload at 2:20, so one frame every
# five seconds is at most 28 frames and covers the clip densely - and a
# cosplayer who appears for a few seconds of a two-minute video is exactly the
# case that was being missed. A YouTube video can run an hour, where the same
# density would be hundreds of frames of a subject that barely changes, so it
# stays sparse and leans on the ceiling instead.
# Ceilings sized from a census of the library rather than guessed. Of 1069
# twitter videos only 65 run past 60 seconds and the longest is 695 seconds, so
# honouring one frame per five seconds outright costs at most 139 frames on a
# single file and a couple of thousand across the whole source. YouTube is the
# opposite shape - 3746 videos, 1765 of them over a minute, the longest 36814
# seconds - where the same density would be 7000 frames for one file, so it
# stays sparse and leans on its ceiling.
FRAME_DENSITY = {
    "twitter": (5, 144),
    "youtube": (30, 16),
    "youtube_music": (60, 6),
}
DEFAULT_FRAME_DENSITY = (10, 20)

# Never fewer than this, however short the clip - a three-second GIF still gets
# four looks, which is what "more frequently for shorter videos" means once the
# per-second rate has nothing left to divide.
MIN_FRAMES_PER_VIDEO = FRAMES_PER_VIDEO


def frame_density_for(path):
    """(seconds per frame, ceiling) for the source a file came from."""
    source = (path or "").replace("\\", "/").split("/", 1)[0].lower()
    return FRAME_DENSITY.get(source, DEFAULT_FRAME_DENSITY)


def frame_fractions(duration, path=None):
    """Evenly spaced sampling points across the middle of a video.

    The middle rather than the whole span: the first and last seconds of a clip
    are titles, fades and black.
    """
    if not duration or duration <= 0:
        return list(FRAME_POSITIONS)
    per_frame, ceiling = frame_density_for(path)
    want = int(round(duration / per_frame))
    want = max(MIN_FRAMES_PER_VIDEO, min(ceiling, want))
    if want == FRAMES_PER_VIDEO:
        return list(FRAME_POSITIONS)
    step = 0.7 / (want - 1)
    return [0.15 + i * step for i in range(want)]

# A still is at most this wide before it is sent. Originals go over the wire
# for images because detail is the point; a video frame is already a
# re-encode, and a 4K frame costs decode time and bandwidth for nothing.
FRAME_MAX_WIDTH = 1600


HEARTBEAT_NAME = "indexer_status.json"


def heartbeat(base_dir, **fields):
    """Where this sweep has got to. Written next to the index rather than in
    the app directory, and best-effort: a status file that cannot be written
    must never stop the indexing it is reporting on."""
    try:
        import media_ai
        path = os.path.join(media_ai.store_dir(base_dir), HEARTBEAT_NAME)
        tmp = path + ".part"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"at": time.time(), **fields}, fh)
        os.replace(tmp, path)
    except Exception:
        pass


def load_config(base_dir):
    cfg = {
        "worker": "http://10.1.1.2:8410",
        "token": "",
        "batch_size": 8,
        "tasks": "caption,ocr,tags",
        # How long to wait when the worker says it is outside its window. It
        # owns the schedule; this just stops asking for a while.
        "closed_retry_s": 600,
        "request_timeout_s": 900,
        # Skip anything larger than this. A 4 GB remux is not worth the LAN
        # round trip, and the frames of a video are sent instead of the video.
        "max_image_mb": 64,
        "roots": None,          # None = every configured root
        # Passes run in rotation, a time slice each: new downloads get every
        # pass within one rotation while the backlog of the slow passes keeps
        # shrinking. None means just `tasks`, the old single-pass behaviour.
        "pipeline": None,
        "pass_budget_s": 1800,
    }
    try:
        with open(os.path.join(base_dir, CONFIG_NAME), encoding="utf-8") as fh:
            cfg.update(json.load(fh))
    except (OSError, ValueError):
        pass
    return cfg


# ------------------------------------------------------------------ frames --

def frame_timestamps(duration, path=None):
    return [max(0.0, duration * f) for f in frame_fractions(duration, path)]


def video_frames(path, rel_path=None, turn=0):
    """Stills from a video, as JPEG bytes, without touching the disk.

    `turn` (degrees clockwise, KSF-075) is applied BEFORE the scale, so a video
    filmed sideways is seen upright by every pass. The face crop in server.py
    replays the same chain from faces.frame_turn.

    One ffmpeg call per frame with -ss BEFORE -i, which seeks by keyframe and
    is effectively instant; seeking after -i decodes from the start and turns
    a 40-minute video into a 40-minute job.
    """
    duration = probe_duration(path)
    if not duration:
        return []
    frames = []
    for frac in frame_fractions(duration, rel_path or path):
        ts = max(0.0, duration * frac)
        proc = subprocess.run(
            ["ffmpeg", "-v", "error", "-ss", f"{ts:.2f}", "-i", path,
             "-frames:v", "1",
             "-vf", turn_prefix(turn) + f"scale='min({FRAME_MAX_WIDTH},iw)':-2",
             "-f", "image2", "-vcodec", "mjpeg", "-q:v", "3", "pipe:1"],
            capture_output=True, timeout=120)
        if proc.returncode == 0 and proc.stdout:
            frames.append((ts, proc.stdout))
    return frames


def turn_prefix(turn):
    """The ffmpeg filter-chain prefix turning a frame upright ('' for none)."""
    f = media_ai.TURN_FILTERS.get(turn)
    return f + "," if f else ""


# Passes that read the picture of a video. Their stamp carries "+t<deg>" when
# the frames they saw were turned, so needs_work() re-does a video when its
# effective turn changes (the orient pass finds one later, or someone sets one
# by hand) and never otherwise.
FRAME_STAMP_KEYS = ("caption_model", "ocr_model", "tag_model", "face_model")


def turned_models(models, kind, turn):
    if kind != "video" or turn not in media_ai.ORIENT_TURNS:
        return models
    return {k: (v + f"+t{turn}" if k in FRAME_STAMP_KEYS and v else v)
            for k, v in models.items()}


def effective_turns(conn, base_dir):
    """{(root, path): deg} as the page shows it: a hand-set turn
    (rotations.json) wins over the orientation pass's; a hand-set 0 means
    'as filmed'. Two cheap reads, never a scan of the media table."""
    turns = dict(media_ai.orientations(conn))
    try:
        with open(os.path.join(base_dir, "rotations.json"), encoding="utf-8") as fh:
            hand = json.load(fh)
    except (OSError, ValueError):
        hand = {}
    if isinstance(hand, dict):
        for root, paths in hand.items():
            for path, entry in (paths or {}).items():
                deg = entry.get("deg") if isinstance(entry, dict) else None
                if deg in (0, 90, 180, 270):
                    turns[(root, path)] = deg
    return turns


def probe_duration(path):
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", path],
            capture_output=True, text=True, timeout=60)
        return float((out.stdout or "").strip())
    except (ValueError, subprocess.SubprocessError):
        return None


# ------------------------------------------------------------------ worker --

class WindowClosed(Exception):
    """The worker is outside its processing window. Not an error."""


def post_batch(cfg, items, urgent=False):
    """items: [(label, bytes)]. Returns the worker's per-file results."""
    boundary = uuid.uuid4().hex
    parts = []
    for label, blob in items:
        ctype = mimetypes.guess_type(label)[0] or "application/octet-stream"
        parts.append(
            b"--" + boundary.encode() + b"\r\n"
            + ('Content-Disposition: form-data; name="file"; filename="%s"\r\n'
               % label.replace('"', "_")).encode()
            + ("Content-Type: %s\r\n\r\n" % ctype).encode() + blob + b"\r\n")
    for key, value in (("tasks", cfg["tasks"]), ("urgent", "1" if urgent else "0")):
        parts.append(b"--" + boundary.encode() + b"\r\n"
                     + ('Content-Disposition: form-data; name="%s"\r\n\r\n%s\r\n'
                        % (key, value)).encode())
    parts.append(b"--" + boundary.encode() + b"--\r\n")

    req = urllib.request.Request(cfg["worker"].rstrip("/") + "/v1/analyze",
                                 data=b"".join(parts), method="POST")
    req.add_header("Content-Type", "multipart/form-data; boundary=" + boundary)
    if cfg.get("token"):
        req.add_header("Authorization", "Bearer " + cfg["token"])
    try:
        with urllib.request.urlopen(req, timeout=cfg["request_timeout_s"]) as r:
            return json.load(r)
    except urllib.error.HTTPError as exc:
        if exc.code == 503:
            raise WindowClosed(exc.read().decode("utf-8", "replace")[:200])
        raise


# ----------------------------------------------------------------- walking --

# Set from the config; see iter_candidates.
PRIORITISE_FACES = False


def iter_candidates(roots, conn, models, max_bytes, turns=None):
    """(root_id, rel_path, abs_path, size, kind) for everything still to do,
    newest first.

    The whole list is materialised on purpose: it has to be sorted by mtime,
    and 450k tuples is tens of megabytes against a sweep that runs for hours.
    """
    found = []
    # The genre pass looks at music and nothing else; every other pass at
    # pictures and videos.
    audio_pass = "genre_model" in models
    # The orientation pass is about videos only: a photo carries its turn in
    # its EXIF, which browsers already honour.
    orient_pass = "orient_model" in models
    for root_id, base in roots:
        if not os.path.isdir(base):
            continue
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = [d for d in dirnames if not d.startswith(".")]
            for name in filenames:
                if name.startswith("."):
                    continue
                ext = os.path.splitext(name)[1].lower()
                if audio_pass:
                    kind = "audio" if ext in AUDIO_EXTS else None
                elif orient_pass:
                    kind = "video" if ext in VIDEO_EXTS else None
                else:
                    kind = ("image" if ext in IMAGE_EXTS
                            else "video" if ext in VIDEO_EXTS else None)
                if kind is None:
                    continue
                abs_path = os.path.join(dirpath, name)
                try:
                    st = os.stat(abs_path)
                except OSError:
                    continue
                # An empty file has nothing to look at, and in this library it
                # is almost always a ban tombstone at a trashed file's path.
                # That row keeps the REAL size, so needs_work saw a "changed"
                # file for ever - 1,925 were retried every ten seconds - and
                # recording one would overwrite the trashed row's size with 0.
                if st.st_size == 0:
                    continue
                if kind == "image" and st.st_size > max_bytes:
                    continue
                rel = os.path.relpath(abs_path, base).replace(os.sep, "/")
                # A song stored once and hard-linked into the playlists that
                # hold it has several names and one inode: whichever name was
                # indexed first carries the work, the others are not candidates.
                if st.st_nlink > 1 and media_ai.indexed_elsewhere(
                        conn, f"{st.st_dev}:{st.st_ino}", root_id, rel):
                    continue
                if not media_ai.needs_work(
                        conn, root_id, rel, st.st_size,
                        turned_models(models, kind, (turns or {}).get((root_id, rel), 0))):
                    continue
                found.append((st.st_mtime, root_id, rel, abs_path, st.st_size, kind))
    found.sort(reverse=True)            # newest first

    # Faces ran far ahead of tags, which left the People grid unable to tell a
    # real person from a drawing - that judgement reads tags, and most files
    # with a face had none. So when asked, put the files that ALREADY have a
    # face at the front: they are exactly the ones a pass makes useful today,
    # and they are a few thousand rather than half a million.
    if PRIORITISE_FACES:
        with_faces = {(r["root"], r["path"]) for r in conn.execute(
            "SELECT DISTINCT m.root AS root, m.path AS path"
            " FROM faces f JOIN media m ON m.id = f.media_id")}
        found.sort(key=lambda t: (0 if (t[1], t[2]) in with_faces else 1, -t[0]))

    return [(r, rel, p, size, kind) for _mt, r, rel, p, size, kind in found]


# ----------------------------------------------------------------- storing --

def merge_video(results, timestamps=None, turn=0):
    """One record from several frames. Captions are joined because each
    describes a different moment; OCR is joined for the same reason; tags are
    unioned keeping the highest confidence seen for each."""
    captions, ocr, tags = [], [], {}
    for r in results:
        cap = (r.get("caption") or "").strip()
        if cap and cap not in captions:
            captions.append(cap)
        text = (r.get("ocr") or "").strip()
        if text and text not in ocr:
            ocr.append(text)
        for group, entries in (r.get("tags") or {}).items():
            bucket = tags.setdefault(group, {})
            for e in entries:
                if e["p"] > bucket.get(e["tag"], 0):
                    bucket[e["tag"]] = e["p"]
    # Faces from every sampled frame are kept together: the same person turns
    # up in several frames, and clustering wants all of those examples.
    faces = []
    for i, r in enumerate(results):
        ts = (timestamps or [None] * len(results))[i] if timestamps else None
        for f in (r.get("faces") or []):
            # Stamped here, where the frame is still known. By the time these
            # reach the database the frames are gone and a bbox alone cannot
            # be turned back into a crop.
            faces.append({**f, "frame_ts": ts, "frame_w": FRAME_MAX_WIDTH,
                          "frame_turn": turn or None})
    return (" ".join(captions),
            "\n".join(ocr),
            {g: [{"tag": t, "p": p} for t, p in sorted(v.items(), key=lambda x: -x[1])]
             for g, v in tags.items()},
            faces)


def decode_faces(faces):
    """base64 embeddings back to bytes for the BLOB column. The worker encodes
    them because 512 JSON floats would roughly triple the response for data
    nothing in between ever reads a component of."""
    import base64
    out = []
    for f in faces or []:
        blob = f.get("embedding_b64")
        if not blob:
            continue
        try:
            emb = base64.b64decode(blob)
        except Exception:
            continue
        if len(emb) != 512 * 4:
            continue
        out.append({"bbox": f.get("bbox"), "det_score": f.get("det_score"),
                    "quality": f.get("quality"), "embedding": emb,
                    "frame_ts": f.get("frame_ts"), "frame_w": f.get("frame_w"),
                    "frame_turn": f.get("frame_turn")})
    return out


def tags_text(tags):
    """What the FTS index actually searches. Underscores become spaces so
    `hatsune_miku` is found by typing `miku`, and the series suffix in
    `cu_chulainn_alter_(fate)` becomes a searchable word rather than
    punctuation FTS would choke on."""
    words = []
    for group in ("character", "general", "rating"):
        for e in (tags or {}).get(group, []):
            words.append(e["tag"].replace("_", " ").replace("(", " ").replace(")", " "))
    return " ".join(words)


# Which stamp each task leaves. needs_work() compares these, so stamping a
# model for a pass that did not run marks the file done for work it never had.
TASK_MODELS = {
    "caption": ("caption_model", "florence2-large"),
    "ocr": ("ocr_model", "florence2-large"),
    "tags": ("tag_model", "wd-eva02-large-v3"),
    "faces": ("face_model", "insightface-buffalo_l"),
    "genre": ("genre_model", "discogs-effnet-bsdynamic-1"),
    # Bump the suffix when orient_decide's rules change: every video is
    # looked at again (a few hours of frame grabs, minutes of GPU).
    "orient": ("orient_model", "insightface-det10g-turns-1"),
}


def models_for(tasks):
    wanted = {t.strip() for t in str(tasks).split(",") if t.strip()}
    return {key: value for task, (key, value) in TASK_MODELS.items()
            if task in wanted}


def store(conn, root_id, rel, size, kind, payload, models, ino=None):
    caption, ocr, tags, faces = payload
    has_tags = any(tags.get(g) for g in ("character", "general", "rating"))
    fields = {**models, "ino": ino}
    # EMPTY STRING means the pass ran and found nothing; NULL means it never
    # ran. Storing None for both made the model stamp the only record of
    # whether a pass happened - so when the stamps were wrong there was no way
    # to tell, which is exactly the hole a stale deploy fell through.
    if "caption_model" in models:
        fields["caption"] = caption or ""
    if "ocr_model" in models:
        fields["ocr"] = ocr or ""
    if "tag_model" in models:
        fields["tags"] = (json.dumps({**tags, "_text": tags_text(tags)},
                                     ensure_ascii=False) if has_tags else "")
    media_id = media_ai.upsert(conn, root_id, rel, size, kind, fields)
    if "tag_model" in models:
        media_ai.replace_tags(conn, media_id, tags if has_tags else {})
    if "face_model" in models:
        media_ai.replace_faces(conn, media_id, decode_faces(faces))


# -------------------------------------------------------------------- main --

# A grid that silently ages out is worse than an empty one. The People page
# spent an evening showing 1102 faces of 14836 because clustering was a manual
# step nobody re-ran, so it now runs itself once the face count has moved
# enough to matter. Not after every batch: clustering is a whole-library
# operation, and doing it per batch would spend more time regrouping than
# detecting.
RECLUSTER_GROWTH = 1.20        # 20 percent more faces than the last run
RECLUSTER_MIN_FACES = 200      # below this the grid is not worth the work
# While the face pass is still working through its backlog, regrouping on
# every 20 percent would run about twenty times, each over more faces than the
# last. Mid-backlog it waits at least this long between runs; once the pass has
# caught up it regroups on growth alone, as before.
RECLUSTER_BACKLOG_INTERVAL_S = 24 * 3600   # a regroup over a large backlog can take hours


def maybe_recluster(conn, base_dir, cfg, faces_backlog=False):
    """Re-cluster if the face count has grown materially since the last run.

    Deliberately triggered by GROWTH rather than by a timer: a library that is
    not gaining faces gains nothing from regrouping, and one that just took a
    thousand new faces should not wait for a clock.
    """
    if not cfg.get("auto_recluster", True):
        return
    try:
        total = conn.execute(
            "SELECT COUNT(*) AS n FROM faces WHERE embedding IS NOT NULL").fetchone()["n"]
        row = conn.execute("SELECT v FROM meta WHERE k = 'faces_at_last_cluster'").fetchone()
        last = int(row["v"]) if row else 0
    except Exception as exc:
        print(f"[indexer] recluster check failed: {exc}", flush=True)
        return

    if total < RECLUSTER_MIN_FACES or total < last * RECLUSTER_GROWTH:
        return
    if faces_backlog:
        row = conn.execute("SELECT v FROM meta WHERE k = 'faces_last_cluster_at'").fetchone()
        if row and time.time() - float(row["v"]) < RECLUSTER_BACKLOG_INTERVAL_S:
            return
    try:
        import media_faces
        print(f"[indexer] re-clustering: {total} faces, {last} at the last run",
              flush=True)
        result = media_faces.recluster(base_dir)
        conn.execute("INSERT INTO meta(k, v) VALUES('faces_at_last_cluster', ?)"
                     " ON CONFLICT(k) DO UPDATE SET v = excluded.v", (str(total),))
        conn.execute("INSERT INTO meta(k, v) VALUES('faces_last_cluster_at', ?)"
                     " ON CONFLICT(k) DO UPDATE SET v = excluded.v", (str(time.time()),))
        conn.commit()
        print(f"[indexer] re-clustered: {result}", flush=True)
    except Exception as exc:
        # A failed regroup must never stop the indexing that feeds it.
        print(f"[indexer] recluster failed: {exc}", flush=True)


# Same idea as the recluster hook, and much cheaper: whether two character
# tags name one person is decided by how often they land on the same picture,
# so the answer changes as tagging proceeds. A newly tagged character whose
# costume tags have not been grouped yet shows up in the grid several times,
# which is the bug this exists to stop coming back.
VARIANT_REGROUP_GROWTH = 1.05   # 5 percent more character tag rows


def maybe_regroup_variants(conn):
    """Re-derive the character variant groups once tagging has moved on.

    Runs on GROWTH rather than a timer, and at a much lower threshold than
    reclustering because it is a second of work over 57k rows rather than
    minutes over every face in the library.
    """
    try:
        total = conn.execute(
            "SELECT COUNT(*) AS n FROM media_tags"
            " WHERE category = 'character'").fetchone()["n"]
        row = conn.execute("SELECT v FROM meta WHERE k = 'tags_at_last_regroup'").fetchone()
        last = int(row["v"]) if row else 0
    except Exception as exc:
        print(f"[indexer] variant regroup check failed: {exc}", flush=True)
        return

    if total and total < last * VARIANT_REGROUP_GROWTH:
        return
    try:
        n = media_ai.rebuild_tag_variants(conn)
        conn.execute("INSERT INTO meta(k, v) VALUES('tags_at_last_regroup', ?)"
                     " ON CONFLICT(k) DO UPDATE SET v = excluded.v", (str(total),))
        conn.commit()
        print(f"[indexer] character variants regrouped: {n} tags in groups"
              f" ({total} character tag rows)", flush=True)
    except Exception as exc:
        # As with reclustering: a failed regroup must never stop the indexing.
        print(f"[indexer] variant regroup failed: {exc}", flush=True)


def sweep(cfg, base_dir, roots, limit=None, urgent=False, verbose=True, budget_s=None):
    conn = media_ai.connect(base_dir)
    models = models_for(cfg["tasks"])
    max_bytes = cfg["max_image_mb"] * 1024 * 1024

    t0 = time.time()
    heartbeat(base_dir, state="walking", task=cfg["tasks"])
    global PRIORITISE_FACES
    PRIORITISE_FACES = bool(cfg.get("prioritise_faces"))
    turns = effective_turns(conn, base_dir)
    todo = iter_candidates(roots, conn, models, max_bytes, turns)
    if verbose:
        print(f"[indexer] {len(todo)} files to do "
              f"(walk took {time.time() - t0:.0f}s)", flush=True)
    if limit:
        todo = todo[:limit]

    done = failed = 0
    batch = []
    stopped_early = False
    for root_id, rel, abs_path, size, kind in todo:
        batch.append((root_id, rel, abs_path, size, kind))
        if len(batch) >= cfg["batch_size"]:
            d, f = flush(conn, cfg, batch, models, urgent, verbose,
                         effective_turns(conn, base_dir))
            done += d
            failed += f
            batch = []
            heartbeat(base_dir, state="indexing", task=cfg["tasks"],
                      done=done, failed=failed, outstanding=len(todo),
                      started=t0)
            if budget_s and time.time() - t0 >= budget_s:
                # This pass's slice is up; the rotation comes back to it.
                stopped_early = True
                break
    if batch and not stopped_early:
        d, f = flush(conn, cfg, batch, models, urgent, verbose,
                     effective_turns(conn, base_dir))
        done += d
        failed += f
    conn.commit()
    maybe_regroup_variants(conn)
    # Only a face pass that got through everything knows the backlog is gone.
    maybe_recluster(conn, base_dir, cfg,
                    faces_backlog=stopped_early or "face_model" not in models)
    heartbeat(base_dir, state="sweep-finished", task=cfg["tasks"],
              done=done, failed=failed, outstanding=len(todo), started=t0)
    return done, failed, len(todo)


def flush(conn, cfg, batch, models, urgent, verbose, turns=None):
    """One batch. Images go as themselves; each video is sent as its frames,
    which is why the mapping back is by label rather than by position."""
    if "genre_model" in models:
        return flush_audio(conn, cfg, batch, models, urgent, verbose)
    if "orient_model" in models:
        return flush_orient(conn, cfg, batch, models, urgent, verbose)
    payloads, mapping = [], {}
    frame_meta = {}          # label -> timestamp, for video faces
    turns = turns or {}
    turn_of = {(r, rel): turns.get((r, rel), 0) if kind == "video" else 0
               for r, rel, _p, _s, kind in batch}

    # Reading the files and pulling video frames happens in parallel. It was
    # the bottleneck, not the GPU: four ffmpeg seeks per video on the spinning
    # disk are 2-3 s, so a batch of eight videos spent ~20 s here one after
    # another and the face pass ran at 0.4 files a second.
    from concurrent.futures import ThreadPoolExecutor

    def prepare(entry):
        root_id, rel, abs_path, size, kind = entry
        try:
            if kind == "image":
                with open(abs_path, "rb") as fh:
                    return entry, [(None, fh.read())], None
            return entry, video_frames(abs_path, rel, turn_of.get((root_id, rel), 0)), None
        except (OSError, subprocess.SubprocessError) as exc:
            # A timed-out ffmpeg used to escape as an exception and fail the
            # whole sweep; now the file just produces nothing and is recorded.
            return entry, [], exc

    with ThreadPoolExecutor(max_workers=max(1, int(cfg.get("prep_workers", 6)))) as pool:
        prepared = list(pool.map(prepare, batch))
    for (root_id, rel, abs_path, size, kind), blobs, exc in prepared:
        if exc is not None:
            if verbose:
                print(f"[indexer] unreadable {rel}: {exc}", flush=True)
            continue
        for n, (ts, blob) in enumerate(blobs):
            if kind == "image":
                label = f"i{len(payloads)}|{os.path.basename(rel)}"
            else:
                label = f"v{len(payloads)}_{n}|{os.path.basename(rel)}"
                frame_meta[label] = ts
            payloads.append((label, blob))
            mapping.setdefault((root_id, rel, size, kind), []).append(label)

    # A file that produced nothing to send - a video ffmpeg could not take a
    # frame from, a file that could not be read - is recorded as seen with no
    # content, exactly like one the worker's decoder refuses below. Returning
    # without recording it brought the same files back on every sweep.
    sent = set(mapping)
    # Which file each name is, for the index: one inode, one song or picture.
    abs_by = {(root_id, rel): abs_path for root_id, rel, abs_path, _s, _k in batch}
    unsent = [(root_id, rel, size, kind) for root_id, rel, _p, size, kind in batch
              if (root_id, rel, size, kind) not in sent]
    for root_id, rel, size, kind in unsent:
        media_ai.upsert(conn, root_id, rel, size, kind,
                        {**turned_models(models, kind, turn_of.get((root_id, rel), 0)),
                         "ino": inode_key(abs_by.get((root_id, rel)))})

    if not payloads:
        conn.commit()
        return 0, len(unsent)

    reply = post_batch(cfg, payloads, urgent=urgent)
    by_label = {r["name"]: r for r in reply.get("results", [])}
    for f in reply.get("failed", []):
        by_label[f["name"]] = {"failed": True}

    done = failed = 0
    for (root_id, rel, size, kind), labels in mapping.items():
        results = [by_label[l] for l in labels if l in by_label]
        usable = [r for r in results if not r.get("failed")]
        if not usable:
            # Recorded as seen with no content, so a file the decoder refuses
            # is not retried on every sweep for ever.
            media_ai.upsert(conn, root_id, rel, size, kind,
                            {**turned_models(models, kind, turn_of.get((root_id, rel), 0)),
                             "ino": inode_key(abs_by.get((root_id, rel)))})
            failed += 1
            continue
        if kind == "video":
            payload = merge_video(usable, [frame_meta.get(l) for l in labels
                                           if l in by_label],
                                  turn_of.get((root_id, rel), 0))
        else:
            r = usable[0]
            payload = (r.get("caption") or "", r.get("ocr") or "",
                       r.get("tags") or {}, r.get("faces") or [])
        store(conn, root_id, rel, size, kind, payload,
              turned_models(models, kind, turn_of.get((root_id, rel), 0)),
              inode_key(abs_by.get((root_id, rel))))
        done += 1
    conn.commit()
    return done, failed + len(unsent)


# ------------------------------------------------------------- orientation --

# Some channels (Morio CH, Matsurou Ch.) upload phone footage as landscape
# files whose picture lies on its side, with no rotation flag for a player to
# read. The worker searches each frame for faces turned four ways and reports
# which turns showed an UPRIGHT face (vision_worker.orient_faces); this decides
# the video from all its frames. Measured 2026-11-08 on 31 sideways and 81
# upright videos: sideways ones voted 90 in nearly every frame, upright ones
# never voted 90 or 270 - only a stray 180 at 0.53-0.66 in single frames.
ORIENT_POSITIONS = (0.15, 0.32, 0.5, 0.68, 0.85)
ORIENT_FRAME_WIDTH = 960       # the detector looks at 640 px; more is only disk time
ORIENT_UPRIGHT_DEG = 30        # landmarks within this of straight up count as upright
ORIENT_VOTE_MIN = 0.6          # an upright face this sure makes the frame vote
ORIENT_MIN_VOTES = 2           # a turn needs at least this many frames...
ORIENT_MAJORITY = 2            # ...and this many times the votes of every other turn together


def orient_frames(path):
    """A few small stills from across a video, as JPEG bytes. Taken by ffmpeg,
    which applies a rotation flag the way the browser does - so what is
    decided here is the turn on top of what the page already shows."""
    duration = probe_duration(path)
    if not duration:
        return []
    frames = []
    for frac in ORIENT_POSITIONS:
        ts = max(0.0, duration * frac)
        proc = subprocess.run(
            ["ffmpeg", "-v", "error", "-ss", f"{ts:.2f}", "-i", path,
             "-frames:v", "1", "-vf", f"scale='min({ORIENT_FRAME_WIDTH},iw)':-2",
             "-f", "image2", "-vcodec", "mjpeg", "-q:v", "3", "pipe:1"],
            capture_output=True, timeout=120)
        if proc.returncode == 0 and proc.stdout:
            frames.append((ts, proc.stdout))
    return frames


def orient_vote(rec):
    """The turn one frame votes for - the one holding its surest upright
    face - or None when no face there is upright and sure enough."""
    best, best_score = None, 0.0
    for turn, faces in (rec or {}).items():
        for score, _px, up in faces or []:
            if (up is not None and abs(up) <= ORIENT_UPRIGHT_DEG
                    and score >= ORIENT_VOTE_MIN and score > best_score):
                best, best_score = int(turn), score
    return best


def orient_decide(recs):
    """{"deg", "votes", "frames"} for a video from its frames' reports. Not
    turned unless one turn clearly wins: a wrong turn is worse than none, and
    the page has a button for the ones this leaves sideways."""
    votes = {0: 0, 90: 0, 180: 0, 270: 0}
    for rec in recs:
        turn = orient_vote(rec)
        if turn in votes:
            votes[turn] += 1
    turn = max(votes, key=lambda t: (votes[t], t == 0))
    others = sum(votes.values()) - votes[turn]
    deg = turn if (turn and votes[turn] >= ORIENT_MIN_VOTES
                   and votes[turn] >= ORIENT_MAJORITY * others) else 0
    return {"deg": deg, "votes": {str(t): n for t, n in votes.items()}, "frames": len(recs)}


def flush_orient(conn, cfg, batch, models, urgent, verbose):
    """One batch of the orientation pass: each video's frames to the worker,
    its answer to the index."""
    from concurrent.futures import ThreadPoolExecutor

    def prepare(entry):
        try:
            return entry, orient_frames(entry[2]), None
        except (OSError, subprocess.SubprocessError) as exc:
            return entry, [], exc

    with ThreadPoolExecutor(max_workers=max(1, int(cfg.get("prep_workers", 6)))) as pool:
        prepared = list(pool.map(prepare, batch))
    payloads, mapping = [], {}
    for entry, blobs, exc in prepared:
        if exc is not None and verbose:
            print(f"[indexer] unreadable {entry[1]}: {exc}", flush=True)
        for _ts, blob in blobs:
            label = f"o{len(payloads)}|{os.path.basename(entry[1])}"
            payloads.append((label, blob))
            mapping.setdefault(entry, []).append(label)

    by_label = {}
    if payloads:
        reply = post_batch(cfg, payloads, urgent=urgent)
        # A worker from before this pass answers every frame without an
        # "orient" key. Recording that would stamp the whole library as looked
        # at and never come back to it, so the sweep fails and is retried.
        if "orient" not in (reply.get("models") or {}):
            raise RuntimeError("the vision worker has no orient pass yet")
        by_label = {r["name"]: r for r in reply.get("results", [])}

    done = failed = 0
    for entry in batch:
        root_id, rel, abs_path, size, kind = entry
        recs = [by_label[l]["orient"] for l in mapping.get(entry, [])
                if isinstance(by_label.get(l, {}).get("orient"), dict)]
        fields = {**models, "ino": inode_key(abs_path)}
        deg = 0
        if recs:
            decided = orient_decide(recs)
            deg = decided["deg"]
            fields["orient"] = json.dumps(decided, separators=(",", ":"))
            done += 1
        else:
            # Looked at, nothing to look at: not retried on every sweep.
            fields["orient"] = ""
            failed += 1
        media_ai.upsert(conn, root_id, rel, size, kind, fields)
        media_ai.set_orient(conn, root_id, rel, deg)
    conn.commit()
    return done, failed


# ------------------------------------------------------------------- music --

# A track is sent whole up to this long; longer ones (hour-long mixes, full
# albums in one file) as GENRE_WINDOWS windows of GENRE_WINDOW_S seconds, so
# no request carries more than about six minutes of audio.
GENRE_WHOLE_S = 360
GENRE_WINDOW_S = 120
GENRE_WINDOWS = (0.2, 0.5, 0.8)
GENRE_SAMPLE_RATE = 16000
# What is stored of the 400 style scores: the top style always, and any other
# of at least GENRE_MIN_P, at most GENRE_MAX_STYLES of them.
GENRE_MIN_P = 0.1
GENRE_MAX_STYLES = 8
# Worker errors (HTTP 5xx) on this many tracks in a row are the worker, not the
# tracks: the sweep stops and is retried like any outage.
AUDIO_ERRORS_IN_A_ROW = 5


def decode_audio(path):
    """The track as 16 kHz mono s16le PCM bytes, windowed when long; b"" when
    ffmpeg gets nothing out of it."""
    duration = probe_duration(path) or 0
    spans = ([(None, None)] if duration <= GENRE_WHOLE_S
             else [(max(0.0, duration * f - GENRE_WINDOW_S / 2), GENRE_WINDOW_S) for f in GENRE_WINDOWS])
    pcm = []
    for start, length in spans:
        cmd = ["ffmpeg", "-nostdin", "-v", "error"]
        if start is not None:
            cmd += ["-ss", f"{start:.2f}", "-t", f"{length:.2f}"]
        cmd += ["-i", path, "-vn", "-ac", "1", "-ar", str(GENRE_SAMPLE_RATE), "-f", "s16le", "pipe:1"]
        proc = subprocess.run(cmd, capture_output=True, timeout=300)
        if proc.returncode == 0:
            pcm.append(proc.stdout)
    return b"".join(pcm)


def post_audio(cfg, pcm, urgent=False):
    """The worker's answer for one track, or None when it cannot use it."""
    url = cfg["worker"].rstrip("/") + "/v1/audio" + ("?urgent=1" if urgent else "")
    req = urllib.request.Request(url, data=pcm, method="POST")
    req.add_header("Content-Type", "application/octet-stream")
    if cfg.get("token"):
        req.add_header("Authorization", "Bearer " + cfg["token"])
    try:
        with urllib.request.urlopen(req, timeout=cfg["request_timeout_s"]) as r:
            return json.load(r)
    except urllib.error.HTTPError as exc:
        if exc.code == 503:
            raise WindowClosed(exc.read().decode("utf-8", "replace")[:200])
        if exc.code == 422:                 # too short to analyse: a result, not an outage
            return None
        raise


def genre_words(label):
    """'Folk, World, & Country---Folk' -> 'folk world country folk', for the
    word search."""
    return " ".join(w for w in label.replace("---", " ").replace(",", " ").replace("&", " ")
                    .replace("/", " ").replace("-", " ").lower().split())


def store_genre(conn, root_id, rel, size, reply, models, ino=None):
    """Styles as genre tags (the media row's tags JSON too, which is what the
    word search reads) and the embedding for the genre map."""
    import base64
    styles = [(label, float(p)) for label, p in reply.get("styles") or []]
    kept = [s for i, s in enumerate(styles) if i == 0 or s[1] >= GENRE_MIN_P][:GENRE_MAX_STYLES]
    tags = {"genre": [{"tag": label, "p": round(p, 4)} for label, p in kept],
            "_text": " ".join(genre_words(label) for label, _ in kept)}
    fields = {**models, "tags": json.dumps(tags, ensure_ascii=False) if kept else "", "ino": ino}
    media_id = media_ai.upsert(conn, root_id, rel, size, "audio", fields)
    media_ai.replace_genres(conn, media_id, kept)
    if reply.get("embedding"):
        media_ai.store_embedding(conn, media_id, reply.get("model") or models.get("genre_model"),
                                 base64.b64decode(reply["embedding"]), int(reply.get("dim") or 0))


def flush_audio(conn, cfg, batch, models, urgent, verbose):
    """One batch of music: decoded in parallel (the disk is the slow part),
    then one request per track."""
    from concurrent.futures import ThreadPoolExecutor

    def prepare(entry):
        try:
            return entry, decode_audio(entry[2]), None
        except (OSError, subprocess.SubprocessError) as exc:
            return entry, b"", exc

    with ThreadPoolExecutor(max_workers=max(1, int(cfg.get("prep_workers", 6)))) as pool:
        prepared = list(pool.map(prepare, batch))
    done = failed = errors = 0
    for (root_id, rel, abs_path, size, kind), pcm, exc in prepared:
        try:
            reply = post_audio(cfg, pcm, urgent) if pcm else None
        except urllib.error.HTTPError as err:
            # The worker failed on THIS track: skipped for now and not recorded,
            # so a later sweep tries it again - but it no longer holds up every
            # track behind it, sweep after sweep (KSF-042).
            if err.code < 500:
                raise
            errors += 1
            if errors >= AUDIO_ERRORS_IN_A_ROW:
                conn.commit()           # keep what this batch got done
                raise
            if verbose:
                print(f"[indexer] worker error on {rel}: HTTP {err.code}, skipped for now", flush=True)
            continue
        errors = 0
        if not reply:
            # Recorded as seen with nothing found, so it is not retried every sweep.
            if verbose and exc is not None:
                print(f"[indexer] undecodable {rel}: {exc}", flush=True)
            media_ai.upsert(conn, root_id, rel, size, "audio",
                            {**models, "tags": "", "ino": inode_key(abs_path)})
            failed += 1
            continue
        store_genre(conn, root_id, rel, size, reply, models, inode_key(abs_path))
        done += 1
    conn.commit()
    return done, failed


DEFAULT_ROOT_ID = "downloads"


def resolve_roots(base_dir, wanted):
    """(root_id, absolute path) for every media root, built-in first.

    roots.json holds only the ADMIN-ADDED roots; the main library is the
    built-in `downloads` root and is not in that file. Reading only the file
    indexes the extra library and silently skips the 450k files that are the
    entire point - which is exactly what the first version did.
    """
    roots = [(DEFAULT_ROOT_ID, os.path.join(base_dir, "web_files", "downloads"))]
    try:
        with open(os.path.join(base_dir, "roots.json"), encoding="utf-8") as fh:
            for r in json.load(fh):
                if r.get("id") and r.get("path") and r["id"] != DEFAULT_ROOT_ID:
                    roots.append((r["id"], r["path"]))
    except (OSError, ValueError):
        pass
    if wanted:
        roots = [r for r in roots if r[0] in wanted]
    return roots


def main():
    ap = argparse.ArgumentParser(description="Index the library with the vision worker")
    ap.add_argument("--once", action="store_true",
                    help="one sweep and exit, instead of running as a daemon")
    ap.add_argument("--limit", type=int, help="stop after this many files")
    ap.add_argument("--urgent", action="store_true",
                    help="bypass the worker's processing window")
    args = ap.parse_args()

    cfg = load_config(BASE_DIR)
    roots = resolve_roots(BASE_DIR, cfg.get("roots"))
    if not roots:
        print("[indexer] no roots configured", flush=True)
        return 1
    print(f"[indexer] roots: {', '.join(r for r, _ in roots)}", flush=True)

    while True:
        # Re-read every rotation, so a changed pipeline or slice length - or a
        # library added or removed in the Libraries card - applies without
        # restarting the daemon.
        cfg = load_config(BASE_DIR)
        new_roots = resolve_roots(BASE_DIR, cfg.get("roots"))
        if new_roots != roots:
            roots = new_roots
            print(f"[indexer] roots: {', '.join(r for r, _ in roots)}", flush=True)
        pipeline = [str(t).strip() for t in (cfg.get("pipeline") or [cfg["tasks"]])
                    if str(t).strip()]
        # One pass for its slice, then the next. The worker holds one model at
        # a time, so slices keep the swaps to a few an hour; and every pass
        # walks newest first, so a new download gets each pass within one
        # rotation instead of waiting behind a backlog of several days.
        budget = cfg.get("pass_budget_s") if len(pipeline) > 1 else None
        try:
            busy = False
            for task in pipeline:
                done, failed, total = sweep(dict(cfg, tasks=task), BASE_DIR, roots,
                                            limit=args.limit, urgent=args.urgent,
                                            budget_s=budget)
                print(f"[indexer] {task}: {done} indexed, {failed} unusable, "
                      f"{total} were outstanding", flush=True)
                busy = busy or total > 0
            if args.once:
                return 0
            if not busy:
                # Said before sleeping: the server reads a heartbeat older than
                # three minutes as a dead indexer, and this sleep is ten.
                heartbeat(BASE_DIR, state="idle", task=" \u2192 ".join(pipeline),
                          until=time.time() + cfg["closed_retry_s"])
                time.sleep(cfg["closed_retry_s"])
        except WindowClosed as exc:
            heartbeat(BASE_DIR, state="waiting-for-window", task=cfg["tasks"],
                      detail=str(exc)[:200],
                      until=time.time() + cfg["closed_retry_s"])
            if args.once:
                print(f"[indexer] worker window closed: {exc}", flush=True)
                return 0
            time.sleep(cfg["closed_retry_s"])
        except Exception as exc:                  # a sweep must not kill the daemon
            print(f"[indexer] sweep failed: {exc}", flush=True)
            if args.once:
                return 1
            time.sleep(60)


if __name__ == "__main__":
    sys.exit(main())
