"""Intros and outros that repeat, found and cut without a re-encode.

User, 2026-10-18: "Build a feature extremely similar video beginnings and endings
are recognized and cut. Avoid a re-encode if possible without endangering
compatibility."

How it works, in the order the user answered the follow-up questions:

  * Compared WITHIN ONE FOLDER only (a channel, a playlist): a video is
    fingerprinted at its first and its last `scan_seconds` - 4 frames a second,
    each shrunk to 9x8 grey and hashed to 64 bits (a difference hash). The
    fingerprints live in a SQLite file keyed by (device, inode, size, mtime), so
    a video is decoded once however often its folder is looked at again.
  * A SIGNATURE is a run of frames that matches across at least `min_videos`
    videos of the folder: an intro starts at t = 0 (give or take a second), an
    outro ends at the end. "Extremely similar" = the mean distance per frame
    <= `max_mean_distance` bits AND no frame further than `max_frame_distance`.
  * The small hashes only nominate candidates. Both intros and outros must
    also share audio and match detailed RGB frames in every local image area.
    This excludes similar stages/choreography with different characters.
    These checks run before a proposal is published, when adding members, and
    again on the complete section to be removed before a file is cut.
  * Detection runs in the background and NOTHING is cut before the user has
    approved that signature on the settings page ("the user approves per
    detected intro/outro before anything is cut").
  * The cut is a STREAM COPY (no re-encode, so nothing is lost and every player
    that played the file still plays it). An intro can only start where a
    keyframe is: the first keyframe at or after the intro's end, if it is within
    `max_keyframe_gap_s` - otherwise that video is skipped and says so. An outro
    needs no keyframe, the stream just ends earlier.
  * "Cut the file, keep the original, and add a section for approval to
    auto-delete originals per detected intro and outro": the original's bytes are
    copied to <root>/.cut_originals/<signature>/<path> first (a dot-folder, so
    the index and the trash purge leave it alone), and per signature the user can
    switch on "delete originals automatically", delete the kept ones now, or undo
    (write the kept bytes back).
  * "Consider running the task either after every video download or at a given
    point in time (5am)": both triggers, both settings. Default: after each
    download ON, plus a daily pass at 05:00 local over the folders that changed.

Hard links (vault KSF-048): a video may have other names (the VR share, music
videos, playlists). The cut result is therefore written INTO the original's
inode (open r+b, copy, truncate, fsync) and never renamed over it - a rename would
leave every other name on the old bytes. For the same reason a restore writes
into the inode as well.

User, 2026-10-18: "when a video is intro/outro is cut for non-new downloads,
re-do the interpolation and 3D work." A cut (and an undo, which changes the bytes
back) writes into the same inode, so size and mtime change and everything keyed
by them - the 3D cache, the vision worker's Smooth motion segments - no longer
finds the file. `on_cut(root, rel, old_version, new_version)` tells the owner of
those caches, which redoes what existed for the old version. A version is
{dev, ino, size, mtime_ns, mtime}.

Pure module: no Flask. server.py hands in the settings, the roots, "are
downloads running", a cache invalidation, a logger and the on_cut callback.
"""
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import threading
import time

VIDEO_EXTS = {".mp4", ".webm", ".mkv", ".mov", ".m4v", ".avi", ".flv", ".ts", ".mpg", ".mpeg"}
ORIGINALS_DIR = ".cut_originals"
WORK_DIR = ".work"                 # inside ORIGINALS_DIR: temp results of a cut
PREVIEW_FRAMES = 4
FRAME_BYTES = 9 * 8                # one 9x8 grey frame
INFORMATIVE_BITS = 6               # a frame with fewer set bits is flat (black, white, a fade)
MAX_ROUNDS = 8                     # signatures looked for per folder, per kind, per scan
AUDIO_RATE = 8000
VISUAL_RATE = 4
VISUAL_SIDE = 192


# Centre strip of the 24x24 tile grid where a title card prints credits
# ("created by ...", "sponsored by ... Powered by ...").
CREDIT_ROWS = slice(9, 14)
CREDIT_COLS = slice(4, 20)


def same_visual(reference, candidate, seconds, credit_text=False):
    """Require shared footage, including small foreground characters.

    Every 8x8 patch participates: a common background cannot hide a different
    character. Allow minor encoding noise and at most one frame of alignment.

    credit_text: a shared title-card animation may print different credits in
    the centre strip. Everything OUTSIDE the strip must still match as strictly
    as above, and the strip itself must match strictly in at least half of the
    frames (credits fade in and out; a different character or dance differs in
    every frame and so still fails).
    """
    import numpy as np
    need = max(1, int(seconds * VISUAL_RATE))
    if min(len(reference), len(candidate)) < max(1, need - 1):
        return False
    a = reference[:need]
    b = candidate[:need]
    strip = np.zeros((24, 24), dtype=bool)
    strip[CREDIT_ROWS, CREDIT_COLS] = True
    for shift in (0, -1, 1):
        aa, bb = (a[shift:], b) if shift >= 0 else (a, b[-shift:])
        n = min(len(aa), len(bb))
        if n < max(1, need - 1):
            continue
        diff = np.abs(aa[:n].astype(np.int16) - bb[:n].astype(np.int16))
        tiles = diff.reshape(n, 24, 8, 24, 8, 3).mean(axis=(2, 4, 5))
        frame_good = (tiles.max(axis=(1, 2)) <= 12) & (diff.mean(axis=(1, 2, 3)) <= 6)
        if float(np.mean(frame_good)) >= 0.90:
            return True
        if not credit_text:
            continue
        outside = np.where(strip, 0, tiles)
        masked = diff.copy().reshape(n, 24, 8, 24, 8, 3)
        masked[:, CREDIT_ROWS, :, CREDIT_COLS, :, :] = 0
        outside_good = ((outside.max(axis=(1, 2)) <= 12)
                        & (masked.reshape(n, VISUAL_SIDE, VISUAL_SIDE, 3).sum(axis=(1, 2, 3))
                           / (VISUAL_SIDE * VISUAL_SIDE * 3 * (1 - strip.mean())) <= 6))
        strip_good = tiles[:, strip].max(axis=1) <= 12
        if float(np.mean(outside_good)) >= 0.90 and float(np.mean(strip_good)) >= 0.50:
            return True
    return False


def same_audio(reference, candidate, seconds):
    """Codec/gain tolerant waveform comparison of every audio track.

    Permit <=40ms alignment error and <=10% differing quarter-second windows.
    Substantial changes, missing tracks, and incomplete samples fail closed.
    """
    import numpy as np
    if len(reference) != len(candidate):
        return False
    need = int(seconds * AUDIO_RATE)
    if need < AUDIO_RATE // 2:
        return False
    for ref, other in zip(reference, candidate):
        if min(len(ref), len(other)) < need - AUDIO_RATE // 20:
            return False
        n = min(need, len(ref), len(other))
        a = np.asarray(ref[:n], dtype=np.float64)
        b = np.asarray(other[:n], dtype=np.float64)
        if not np.isfinite(a).all() or not np.isfinite(b).all():
            return False
        # Global alignment handles encoder delay. Gain normalization is per
        # window, so volume changes alone do not look like different content.
        fft_size = 1 << (2 * n - 1).bit_length()
        corr = np.fft.irfft(np.fft.rfft(a, fft_size) *
                            np.conj(np.fft.rfft(b, fft_size)), fft_size)
        shifts = np.arange(-AUDIO_RATE // 25, AUDIO_RATE // 25 + 1)
        lag = int(shifts[np.argmax(corr[shifts % fft_size])])
        if lag > 0:
            a, b = a[lag:], b[:-lag]
        elif lag < 0:
            a, b = a[:lag], b[-lag:]
        width = AUDIO_RATE // 4
        windows = min(len(a), len(b)) // width
        if not windows:
            return False
        a = a[:windows * width].reshape(windows, width)
        b = b[:windows * width].reshape(windows, width)
        aa, bb = np.mean(a*a, axis=1), np.mean(b*b, axis=1)
        silent_a, silent_b = aa < 1e-8, bb < 1e-8
        similarity = np.mean(a*b, axis=1) / np.maximum(np.sqrt(aa*bb), 1e-20)
        good = (silent_a & silent_b) | (~silent_a & ~silent_b & (similarity >= 0.80))
        # Silence must not dilute a short but substantially different soundtrack.
        active = ~(silent_a & silent_b)
        if active.any() and float(np.mean(good[active])) < 0.90:
            return False
    return True

DEFAULTS = {
    "enabled": True,
    "folders": ["downloads:youtube"],
    # Right after a video download: queue its folder, and apply approved
    # signatures to the new video.
    "after_download": True,
    # Local time of the daily pass over the folders that changed since the last
    # one. "" = no daily pass.
    "daily_time": "05:00",
    "scan_seconds": 120,           # how much of each end is fingerprinted
    "fps": 4,                      # fingerprint frames per second
    "min_videos": 3,               # videos that must share a segment
    "min_seconds": 2.0,            # shortest segment
    "max_seconds": 90.0,           # longest segment
    "max_mean_distance": 4.0,      # bits (of 64) per frame, on average
    "max_frame_distance": 10,      # bits (of 64) in any single frame
    "max_start_offset_s": 1.0,     # an intro may begin this late, an outro end this early
    "max_keyframe_gap_s": 1.5,     # the keyframe may lie this far past the boundary
    "min_free_gb": 5,              # kept free on the disk while copying
}


def file_version(st):
    """The identity the caches key a file by, from an os.stat result."""
    return {"dev": st.st_dev, "ino": st.st_ino, "size": st.st_size,
            "mtime_ns": st.st_mtime_ns, "mtime": st.st_mtime}


def merged_settings(raw):
    cfg = dict(DEFAULTS)
    cfg.update(raw or {})
    return cfg


# ---- fingerprints -------------------------------------------------------------

def dhash(px, margin=2):
    """64-bit difference hash of a 9x8 grey frame: each pixel against its right
    neighbour. The margin keeps compression noise in flat areas from flipping
    bits (a flat frame hashes to 0)."""
    value = 0
    for row in range(8):
        base = row * 9
        for col in range(8):
            value = (value << 1) | (1 if px[base + col] > px[base + col + 1] + margin else 0)
    return value


def hashes_from_raw(raw):
    return [dhash(raw[i:i + FRAME_BYTES]) for i in range(0, len(raw) - FRAME_BYTES + 1, FRAME_BYTES)]


def pack(hashes):
    return b"".join(h.to_bytes(8, "big") for h in hashes)


def unpack(blob):
    return [int.from_bytes(blob[i:i + 8], "big") for i in range(0, len(blob), 8)]


def informative_share(hashes):
    if not hashes:
        return 0.0
    return sum(1 for h in hashes if h.bit_count() >= INFORMATIVE_BITS) / len(hashes)


# ---- matching -----------------------------------------------------------------

def _run_len(a, ia, b, ib, max_frame, max_mean, limit):
    """Frames that match from a[ia:] against b[ib:]: every one within
    max_frame bits, the mean within max_mean (the tail is trimmed until it is)."""
    n = min(len(a) - ia, len(b) - ib, limit)
    dist = []
    total = 0
    for k in range(n):
        d = (a[ia + k] ^ b[ib + k]).bit_count()
        if d > max_frame:
            break
        dist.append(d)
        total += d
    length = len(dist)
    while length and total / length > max_mean:
        length -= 1
        total -= dist[length]
    return length


def align(a, b, max_off, max_frame, max_mean, limit):
    """The longest matching run between two anchored sequences, both starting
    within max_off frames of their anchor: (length, start in a, start in b)."""
    best = (0, 0, 0)
    for shift in range(-max_off, max_off + 1):
        for ia in range(max_off + 1):
            ib = ia + shift
            if ib < 0 or ib > max_off or ia >= len(a) or ib >= len(b):
                continue
            if (a[ia] ^ b[ib]).bit_count() > max_frame:
                continue
            length = _run_len(a, ia, b, ib, max_frame, max_mean, limit)
            if length > best[0]:
                best = (length, ia, ib)
    return best


def match_signature(sig_frames, hashes, max_off, max_frame, max_mean):
    """Where a signature's frames start in a video's anchored sequence, or None:
    the whole signature has to match, beginning within max_off frames."""
    need = len(sig_frames)
    for ib in range(max_off + 1):
        if ib + need > len(hashes):
            break
        if (sig_frames[0] ^ hashes[ib]).bit_count() > max_frame:
            continue
        if _run_len(sig_frames, 0, hashes, ib, max_frame, max_mean, need) >= need:
            return ib
    return None


def discover(items, min_videos, min_frames, max_frames, max_off, max_frame, max_mean):
    """Signatures among anchored sequences {id: [hash, ...]}.

    Returns [(frames, {id: start})]: the shared frames and, for each video that
    holds them, where they start. Greedy: the signature with the most videos
    first, its videos leave the pool, repeat."""
    remaining = dict(items)
    found = []
    for _round in range(MAX_ROUNDS):
        if len(remaining) < min_videos:
            break
        ids = sorted(remaining)
        candidates = []
        for ref in ids:
            buckets = {}
            for other in ids:
                if other == ref:
                    continue
                length, ia, _ib = align(remaining[ref], remaining[other], max_off, max_frame, max_mean, max_frames)
                # A shared prefix is only an intro/outro if it actually ends.
                # Reaching the cap or the end of either sample is not evidence
                # of a boundary (especially for copies of the same video).
                if (length >= min_frames and length < max_frames
                        and ia + length < len(remaining[ref])
                        and _ib + length < len(remaining[other])):
                    buckets.setdefault(ia, []).append(length)
            for start, lengths in buckets.items():
                if len(lengths) < min_videos - 1:
                    continue
                lengths.sort(reverse=True)
                shared = lengths[min_videos - 2]
                count = sum(1 for n in lengths if n >= shared)
                candidates.append((count, shared, ref, start))
        candidates.sort(key=lambda c: (-c[0], -c[1], c[2], c[3]))
        chosen = None
        for count, shared, ref, start in candidates:
            frames = remaining[ref][start:start + shared]
            if informative_share(frames) < 0.6:
                continue
            members = {}
            for vid in ids:
                at = match_signature(frames, remaining[vid], max_off, max_frame, max_mean)
                if at is not None:
                    members[vid] = at
            if len(members) >= min_videos:
                chosen = (frames, members)
                break
        if not chosen:
            break
        found.append(chosen)
        for vid in chosen[1]:
            remaining.pop(vid, None)
    return found


# ---- the store of fingerprints ---------------------------------------------------

class FingerprintStore:
    def __init__(self, path):
        self.path = path
        with self._conn() as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS fp (key TEXT PRIMARY KEY, folder TEXT, duration REAL,"
                         " head BLOB, tail BLOB, tail_t0 REAL, seen REAL)")

    def _conn(self):
        conn = sqlite3.connect(self.path, timeout=30)
        return conn

    def get(self, key):
        conn = self._conn()
        try:
            row = conn.execute("SELECT duration, head, tail, tail_t0 FROM fp WHERE key = ?", (key,)).fetchone()
        finally:
            conn.close()
        if not row:
            return None
        return {"duration": row[0], "head": unpack(row[1]), "tail": unpack(row[2]), "tail_t0": row[3]}

    def put(self, key, folder, duration, head, tail, tail_t0):
        conn = self._conn()
        try:
            with conn:
                conn.execute("INSERT OR REPLACE INTO fp VALUES (?,?,?,?,?,?,?)",
                             (key, folder, duration, pack(head), pack(tail), tail_t0, time.time()))
        finally:
            conn.close()

    def prune(self, folder, keep_keys):
        """Forget the folder's rows for files that changed or are gone."""
        conn = self._conn()
        try:
            with conn:
                rows = conn.execute("SELECT key FROM fp WHERE folder = ?", (folder,)).fetchall()
                for (key,) in rows:
                    if key not in keep_keys:
                        conn.execute("DELETE FROM fp WHERE key = ?", (key,))
        finally:
            conn.close()


# ---- the service --------------------------------------------------------------------

class VideoTrim:
    def __init__(self, base_dir, cfg_fn, busy_fn, roots_fn, invalidate_fn, log=print,
                 state_name="video_trim.json", on_cut=None):
        self.base_dir = base_dir
        self.cfg = lambda: merged_settings(cfg_fn())
        self.busy = busy_fn
        self.roots = roots_fn
        self.invalidate = invalidate_fn
        self.log = log
        self.on_cut = on_cut
        self.state_file = os.path.join(base_dir, state_name)
        shared = os.environ.get("MEDIA_CACHE_DIR", "").strip()
        self.cache_dir = (os.path.join(os.path.expanduser(shared), "video_trim") if shared
                          else os.path.join(base_dir, "video_trim_cache"))
        os.makedirs(self.cache_dir, exist_ok=True)
        self.store = FingerprintStore(os.path.join(base_dir, "video_trim_fp.sqlite"))
        self.lock = threading.RLock()
        self.jobs = []                    # [("scan", root, dir) | ("apply", id) | ("undo", id) | ("delete", id)]
        self.wake = threading.Event()
        self.current = None
        self.nice = [c for c in (["nice", "-n", "19"], ["ionice", "-c3"]) if shutil.which(c[0])]
        self.nice = [part for c in self.nice for part in c]
        self.state = {"signatures": {}, "folders": {}, "daily_day": None, "last_pass": None}
        try:
            with open(self.state_file, encoding="utf-8") as fh:
                self.state.update(json.load(fh))
        except (OSError, ValueError):
            pass
        # Old pending proposals were generated without duplicate/boundary
        # checks and outside the newly configurable scope. Keep an archive,
        # preserve user decisions and originals, and require fresh detection.
        if self.state.get("detector_version", 1) < 4:
            archive = self.state_file + ".before-detector-v4-" + str(time.time_ns())
            with open(archive, "w", encoding="utf-8") as fh:
                json.dump(self.state, fh, ensure_ascii=False)
            kept = {}
            for sid, sig in self.state["signatures"].items():
                if sig["status"] != "pending" or any(m.get("state") in ("cut", "committing") or m.get("orig_kept")
                       for m in sig["members"].values()):
                    kept[sid] = sig
                else:
                    audio = os.path.join(self.cache_dir, sid, "intro_audio_v1.npz")
                    if os.path.isfile(audio):
                        os.unlink(audio)
            self.state["signatures"] = kept
            self.state.update(folders={}, daily_day=None, last_pass=None, detector_version=4)
            self._save()
        self.recover()
        if self.state.get("detector_version", 4) < 5:
            affected = {(sig["root"], sig["folder"]) for sig in self.state["signatures"].values()
                        if any(not self._claimed(sig, m) for m in sig["members"].values())}
            for root_id, folder in affected:
                self.state["folders"].pop(f"{root_id}:{folder}", None)
                if self.in_scope(root_id, folder):
                    self._queue(("scan", root_id, folder))
            self.state["detector_version"] = 5
            self._save()

    def _notify_cut(self, root, rel, old_st, new_st):
        """Hands the old and the new version of a file whose bytes just changed to
        on_cut. Runs on the worker thread; whatever it raises is logged and never
        fails the cut (the file is already committed)."""
        if not self.on_cut:
            return
        try:
            self.on_cut(root, rel, file_version(old_st), file_version(new_st))
        except Exception as exc:
            self.log(f"[trim] on_cut {root}:{rel}: {type(exc).__name__}: {exc}")

    # ---- state ----
    def _save(self):
        with self.lock:
            tmp = self.state_file + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(self.state, fh, ensure_ascii=False)
            os.replace(tmp, self.state_file)

    def _root_path(self, root_id):
        for root in self.roots():
            if root["id"] == root_id:
                return os.path.abspath(root["path"])
        return None

    def _full(self, root_id, rel):
        base = self._root_path(root_id)
        if not base:
            return None
        full = os.path.abspath(os.path.join(base, rel))
        return full if full.startswith(base + os.sep) else None

    def recover(self):
        """A restart in the middle of writing a cut into an inode: put the kept
        original back, so a half-written video never stays."""
        changed = False
        for sig in self.state["signatures"].values():
            for rel, m in sig["members"].items():
                if m.get("state") != "committing":
                    continue
                full = self._full(sig["root"], rel)
                bak = m.get("backup")
                if full and bak and os.path.exists(bak):
                    try:
                        self._copy_into(bak, full)
                        os.utime(full, None)
                        m.update(state="error", note="interrupted by a restart - the original was put back")
                    except OSError as exc:
                        m.update(state="error", note=f"interrupted by a restart and not restored: {exc}")
                else:
                    m.update(state="error", note="interrupted by a restart; no kept original to restore from")
                changed = True
        if changed:
            self._save()

    # ---- external commands ----
    def _run(self, cmd, timeout=3600, heavy=True):
        prefix = self.nice if heavy else []
        return subprocess.run(prefix + cmd, capture_output=True, timeout=timeout)

    def probe(self, path):
        """{duration, streams: [(type, codec)]} or None."""
        res = self._run(["ffprobe", "-v", "error", "-show_entries", "format=duration:stream=codec_type,codec_name",
                         "-of", "json", path], timeout=120, heavy=False)
        if res.returncode != 0:
            return None
        try:
            data = json.loads(res.stdout)
            duration = float((data.get("format") or {}).get("duration"))
        except (ValueError, TypeError):
            return None
        streams = [(s.get("codec_type"), s.get("codec_name")) for s in data.get("streams") or []]
        return {"duration": duration, "streams": streams}

    def _frames(self, path, cfg, seek_args):
        fps = cfg["fps"]
        res = self._run(["ffmpeg", "-nostdin", "-v", "error", "-threads", "2"] + seek_args + [
            "-i", path, "-an", "-sn", "-vf", f"fps={fps},scale=9:8:flags=area,format=gray",
            "-f", "rawvideo", "-"], timeout=900)
        if res.returncode != 0 and not res.stdout:
            return None
        return hashes_from_raw(res.stdout)

    def fingerprint(self, path, st, folder_key, cfg):
        key = f"{st.st_dev}:{st.st_ino}:{st.st_size}:{st.st_mtime_ns}:{cfg['scan_seconds']}:{cfg['fps']}"
        got = self.store.get(key)
        if got:
            got["key"] = key
            return got
        info = self.probe(path)
        if not info or info["duration"] < 1:
            return None
        scan = cfg["scan_seconds"]
        duration = info["duration"]
        head = self._frames(path, cfg, ["-t", str(scan)])
        t0 = max(0.0, duration - scan)
        tail = self._frames(path, cfg, ["-ss", f"{t0:.3f}"]) if t0 > 0 else list(head or [])
        if not head or not tail:
            return None
        self.store.put(key, folder_key, duration, head, tail, t0)
        return {"key": key, "duration": duration, "head": head, "tail": tail, "tail_t0": t0}

    # ---- looking at a folder ----
    def _list_videos(self, root_path, rel_dir):
        base = os.path.join(root_path, rel_dir) if rel_dir else root_path
        out = []
        try:
            names = sorted(os.listdir(base))
        except OSError:
            return out
        for name in names:
            if name.startswith(".") or name.endswith((".part", ".ytdl", ".temp")):
                continue
            if os.path.splitext(name)[1].lower() not in VIDEO_EXTS:
                continue
            full = os.path.join(base, name)
            try:
                st = os.stat(full)
            except OSError:
                continue
            if not os.path.isfile(full) or st.st_size == 0 or time.time() - st.st_mtime < 60:
                continue
            out.append((name, full, st))
        return out

    def _sig_id(self, root_id, rel_dir, kind, frames):
        blob = f"{root_id}|{rel_dir}|{kind}|".encode() + pack(frames)
        return hashlib.sha1(blob).hexdigest()[:16]

    def in_scope(self, root_id, rel_dir):
        rel = rel_dir.replace("\\", "/").strip("/")
        for entry in self.cfg().get("folders", []):
            root, sep, folder = entry.partition(":")
            folder = folder.replace("\\", "/").strip("/")
            if sep and root == root_id and ".." not in folder.split("/"):
                if not folder or rel == folder or rel.startswith(folder + "/"):
                    return True
        return False

    def scan_folder(self, root_id, rel_dir):
        """Fingerprint a folder's videos, attach them to known signatures, look
        for new ones and apply the approved. Returns a summary dict."""
        cfg = self.cfg()
        if not self.in_scope(root_id, rel_dir):
            return {"skipped": "folder outside configured scope"}
        root_path = self._root_path(root_id)
        if not root_path:
            return {"error": "unknown root"}
        folder_key = f"{root_id}:{rel_dir}"
        try:
            dir_mtime = os.stat(os.path.join(root_path, rel_dir)).st_mtime_ns
        except OSError:
            return {"error": "folder is gone"}
        videos = self._list_videos(root_path, rel_dir)
        self.current = folder_key
        try:
            return self._scan(cfg, root_id, rel_dir, folder_key, root_path, videos, dir_mtime)
        finally:
            self.current = None

    def _scan(self, cfg, root_id, rel_dir, folder_key, root_path, videos, dir_mtime):
        fps = cfg["fps"]
        max_off = int(round(cfg["max_start_offset_s"] * fps))
        max_frame, max_mean = cfg["max_frame_distance"], cfg["max_mean_distance"]
        min_frames = max(2, int(round(cfg["min_seconds"] * fps)))
        max_frames = max(min_frames, int(round(cfg["max_seconds"] * fps)))
        sigs = [s for s in self.state["signatures"].values() if s["root"] == root_id and s["folder"] == rel_dir]
        known = {kind: {rel for s in sigs if s["kind"] == kind
                        for rel, m in s["members"].items() if self._claimed(s, m)}
                 for kind in ("intro", "outro")}
        known_ino = {kind: {(m.get("dev"), m.get("ino")) for s in sigs if s["kind"] == kind
                            for m in s["members"].values() if self._claimed(s, m)}
                     for kind in ("intro", "outro")}
        # One name per inode: another name of the same file is cut with it.
        seen_ino, entries = set(), []
        for name, full, st in videos:
            if (st.st_dev, st.st_ino) in seen_ino:
                continue
            seen_ino.add((st.st_dev, st.st_ino))
            entries.append((name, full, st))
        fps_by_rel, keys = {}, set()
        for name, full, st in entries:
            if self.busy():
                return {"deferred": True}
            rel = (rel_dir + "/" + name) if rel_dir else name
            fp = self.fingerprint(full, st, folder_key, cfg)
            if fp:
                fps_by_rel[rel] = (fp, st)
                keys.add(fp["key"])
        self.store.prune(folder_key, keys)

        def anchored(fp, kind):
            return fp["head"] if kind == "intro" else list(reversed(fp["tail"]))

        def boundary(fp, kind, start, length):
            if kind == "intro":
                return (start + length) / fps
            n = len(fp["tail"])
            t_start = fp["tail_t0"] + (n - start - length) / fps
            return fp["duration"] - t_start

        # Separate copies/re-encodes must not vote as distinct source videos.
        unique = {}
        for rel, (fp, st) in fps_by_rel.items():
            duplicate = False
            for other, _ in unique.values():
                if abs(fp["duration"] - other["duration"]) > 1.0:
                    continue
                if all(len(fp[end]) == len(other[end]) and
                       _run_len(fp[end], 0, other[end], 0, max_frame, max_mean,
                                len(fp[end])) == len(fp[end])
                       for end in ("head", "tail")):
                    duplicate = True
                    break
            if not duplicate:
                unique[rel] = (fp, st)
        fps_by_rel = unique
        added = 0
        new_sigs = 0
        for kind in ("intro", "outro"):
            # 1. videos that belong to a signature the folder already has
            for rel, (fp, st) in fps_by_rel.items():
                if rel in known[kind] or (st.st_dev, st.st_ino) in known_ino[kind]:
                    continue
                for sig in [s for s in sigs if s["kind"] == kind]:
                    frames = unpack(bytes.fromhex(sig["frames"]))
                    at = match_signature(frames, anchored(fp, kind), max_off, max_frame, max_mean)
                    if at is not None and self._plausible(fp, kind, boundary(fp, kind, at, len(frames))):
                        check_seconds = boundary(fp, kind, at, len(frames))
                        audio_ok, _ = self._audio_guard(sig, self._full(root_id, rel), check_seconds, cfg)
                        visual_ok, _ = self._visual_guard(sig, self._full(root_id, rel), check_seconds, cfg)
                        if not audio_ok or not visual_ok:
                            continue
                        self._add_member(sig, rel, fp, st, boundary(fp, kind, at, len(frames)))
                        known[kind].add(rel)
                        known_ino[kind].add((st.st_dev, st.st_ino))
                        added += 1
                        break
            # 2. new signatures among the rest
            pool = {rel: anchored(fp, kind) for rel, (fp, st) in fps_by_rel.items()
                    if rel not in known[kind] and (st.st_dev, st.st_ino) not in known_ino[kind]
                    and fp["duration"] >= max(10.0, 2 * cfg["min_seconds"] + 4)}
            if len(pool) < cfg["min_videos"]:
                continue
            for frames, members in discover(pool, cfg["min_videos"], min_frames, max_frames, max_off,
                                            max_frame, max_mean):
                good = {}
                for rel, at in members.items():
                    fp, st = fps_by_rel[rel]
                    b = boundary(fp, kind, at, len(frames))
                    if self._plausible(fp, kind, b):
                        good[rel] = (fp, st, b)
                if len(good) < cfg["min_videos"]:
                    continue
                sid = self._sig_id(root_id, rel_dir, kind, frames)
                candidate = {"id": sid, "root": root_id, "folder": rel_dir, "kind": kind,
                             "frames": pack(frames).hex(), "length_s": round(len(frames) / fps, 2),
                             "status": "pending", "auto_delete": False, "created": time.time(), "members": {}}
                for rel, (fp, st, b) in good.items():
                    self._add_member(candidate, rel, fp, st, b)
                # One coarse hash can represent several genuine intros. Do not
                # let one accepted/rejected subgroup consume the other variants.
                for sig in self._verified_variants(candidate, cfg):
                    sid = sig["id"]
                    with self.lock:
                        if sid in self.state["signatures"]:
                            continue
                        self.state["signatures"][sid] = sig
                        sigs.append(sig)
                        for rel, m in sig["members"].items():
                            known[kind].add(rel)
                            known_ino[kind].add((m["dev"], m["ino"]))
                        self._save()
                    self._make_previews(sig, sorted(sig["members"])[0])
                    new_sigs += 1
                    self.log(f"[trim] {folder_key}: new {kind} of {sig['length_s']}s in {len(sig['members'])} videos")
        with self.lock:
            self.state["folders"][folder_key] = {"dir_mtime_ns": dir_mtime, "scanned": time.time(),
                                                 "videos": len(entries)}
            self._save()
        applied = 0
        for sig in [s for s in sigs if s["status"] == "approved"]:
            applied += self.apply_signature(sig["id"]).get("cut", 0)
        return {"videos": len(entries), "added": added, "new": new_sigs, "cut": applied}

    @staticmethod
    def _claimed(sig, member):
        if sig["status"] == "rejected":
            return True
        # A failed match to one old reference must not suppress discovery of a
        # different real intro. Keyframe skips and completed cuts stay claimed.
        mismatch = member.get("state") == "skipped" and any(
            text in member.get("note", "") for text in ("audio", "video details", "shared footage"))
        return not mismatch

    def _verified_variants(self, candidate, cfg):
        import numpy as np
        seconds = max(m["boundary_s"] for m in candidate["members"].values())
        if candidate["kind"] == "intro":
            seconds += cfg["max_keyframe_gap_s"]
        samples = {}
        for rel, member in sorted(candidate["members"].items()):
            if self.busy():
                return
            full = self._full(candidate["root"], rel)
            try:
                st = os.stat(full)
                if (st.st_size, st.st_mtime_ns) != (member["size"], member["mtime_ns"]):
                    continue
                samples[rel] = (self._segment_audio(candidate, full, seconds),
                                self._segment_visual(candidate, full, seconds))
            except (OSError, ValueError, subprocess.TimeoutExpired):
                continue
        pairs = {}
        def matches(a, b):
            key = tuple(sorted((a, b)))
            if key not in pairs:
                end = min(candidate["members"][a]["boundary_s"], candidate["members"][b]["boundary_s"])
                aa, av = samples[a]; ba, bv = samples[b]
                pairs[key] = same_visual(av, bv, end, candidate["kind"] == "intro") and same_audio(aa, ba, end)
            return pairs[key]
        remaining = set(samples)
        while len(remaining) >= cfg["min_videos"]:
            groups = [(ref, [other for other in sorted(remaining) if matches(ref, other)])
                      for ref in sorted(remaining)]
            ref, group = max(groups, key=lambda item: len(item[1]))
            # Independent copies may not supply multiple votes.
            identities = {(candidate["members"][rel]["dev"], candidate["members"][rel]["ino"]) for rel in group}
            if len(identities) < cfg["min_videos"]:
                break
            sig = dict(candidate)
            sig["id"] = hashlib.sha1((candidate["id"] + "|variant|" + ref).encode()).hexdigest()[:16]
            sig["members"] = {rel: candidate["members"][rel] for rel in group}
            sig.update(audio_verified=True, visual_verified=True)
            audio, visual = samples[ref]
            # Credits that differ inside the title card: remember, so the cut-time
            # guard applies the same (bounded) tolerance and the UI can say so.
            sig["credit_text"] = not all(
                same_visual(visual, samples[rel][1], min(candidate["members"][ref]["boundary_s"],
                                                         candidate["members"][rel]["boundary_s"]))
                for rel in group)
            out = os.path.join(self.cache_dir, sig["id"])
            os.makedirs(out, exist_ok=True)
            for name, data in [(sig["kind"] + "_audio_v1.npz", {f"track_{i:04}": a for i, a in enumerate(audio)}),
                               (sig["kind"] + "_visual_v1.npz", {"frames": visual})]:
                path = os.path.join(out, name)
                with open(path + ".tmp", "wb") as fh:
                    np.savez_compressed(fh, **data)
                os.replace(path + ".tmp", path)
            yield sig
            remaining.difference_update(group)

    def _plausible(self, fp, kind, boundary_s):
        """A segment must be a small part of the video, and leave something."""
        return 0 < boundary_s <= 0.45 * fp["duration"]

    def _add_member(self, sig, rel, fp, st, boundary_s):
        sig["members"][rel] = {"state": "pending", "boundary_s": round(boundary_s, 3),
                               "duration": round(fp["duration"], 3), "dev": st.st_dev, "ino": st.st_ino,
                               "size": st.st_size, "mtime_ns": st.st_mtime_ns, "note": ""}

    def _make_previews(self, sig, ref_rel):
        full = self._full(sig["root"], ref_rel)
        m = sig["members"][ref_rel]
        out = os.path.join(self.cache_dir, sig["id"])
        os.makedirs(out, exist_ok=True)
        b, d = m["boundary_s"], m["duration"]
        for i in range(PREVIEW_FRAMES):
            frac = (i + 0.5) / PREVIEW_FRAMES
            t = b * frac if sig["kind"] == "intro" else min(d - 0.2, d - b + b * frac)
            self._run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-ss", f"{max(0.0, t):.3f}", "-i", full,
                       "-frames:v", "1", "-vf", "scale=320:-2", "-q:v", "5",
                       os.path.join(out, f"{i}.jpg")], timeout=120, heavy=False)

    # ---- cutting ----
    def _segment_audio(self, sig, full, seconds):
        if sig["kind"] == "intro":
            return self._decode_audio(full, seconds)
        info = self.probe(full)
        if not info:
            raise ValueError("cannot inspect outro audio")
        audio = self._decode_audio(full, seconds, max(0.0, info["duration"] - seconds))
        # Anchor at the end so differing video lengths still align correctly.
        return [track[::-1].copy() for track in audio]

    def _segment_visual(self, sig, full, seconds):
        import numpy as np
        info = self.probe(full)
        if not info:
            raise ValueError("cannot inspect video")
        start = 0.0 if sig["kind"] == "intro" else max(0.0, info["duration"] - seconds)
        res = self._run(["ffmpeg", "-nostdin", "-v", "error", "-threads", "2",
                         "-ss", f"{start:.6f}", "-i", full, "-t", f"{seconds:.6f}",
                         "-map", "0:v:0", "-vf", f"fps={VISUAL_RATE},scale={VISUAL_SIDE}:{VISUAL_SIDE}",
                         "-pix_fmt", "rgb24", "-f", "rawvideo", "-"], timeout=300)
        size = VISUAL_SIDE * VISUAL_SIDE * 3
        if res.returncode or res.stderr.strip() or not res.stdout or len(res.stdout) % size:
            raise ValueError("cannot decode detailed video frames")
        frames = np.frombuffer(res.stdout, dtype=np.uint8).reshape(-1, VISUAL_SIDE, VISUAL_SIDE, 3).copy()
        return frames if sig["kind"] == "intro" else frames[::-1].copy()

    def _visual_reference(self, sig, cfg):
        import numpy as np
        path = os.path.join(self.cache_dir, sig["id"], sig["kind"] + "_visual_v1.npz")
        if os.path.isfile(path):
            with np.load(path, allow_pickle=False) as stored:
                return stored["frames"].copy()
        seconds = max(m["boundary_s"] for m in sig["members"].values())
        if sig["kind"] == "intro":
            seconds += cfg["max_keyframe_gap_s"]
        samples = []
        for rel, member in sorted(sig["members"].items()):
            if member["state"] not in ("pending", "queued", "skipped"):
                continue
            try:
                full = self._full(sig["root"], rel)
                st = os.stat(full)
                if (st.st_size, st.st_mtime_ns) != (member["size"], member["mtime_ns"]):
                    continue
                frames = self._segment_visual(sig, full, seconds)
                samples.append((frames, member["boundary_s"], (st.st_dev, st.st_ino)))
            except (OSError, ValueError, subprocess.TimeoutExpired):
                continue
        for frames, boundary, _ in samples:
            supporters = {identity for other, end, identity in samples
                          if same_visual(frames, other, min(boundary, end), sig.get("credit_text", False))}
            if len(supporters) < cfg["min_videos"]:
                continue
            os.makedirs(os.path.dirname(path), exist_ok=True)
            tmp = path + ".tmp"
            with open(tmp, "wb") as fh:
                np.savez_compressed(fh, frames=frames)
            os.replace(tmp, path)
            return frames
        raise ValueError("detailed video frames differ; no shared footage")

    def _visual_guard(self, sig, full, seconds, cfg):
        try:
            reference = self._visual_reference(sig, cfg)
            if not same_visual(reference, self._segment_visual(sig, full, seconds), seconds,
                               sig.get("credit_text", False)):
                return False, "video details differ from the shared footage; not cut"
            return True, ""
        except Exception as exc:
            return False, f"shared footage could not be verified; not cut ({str(exc)[:160]})"

    def _validate_proposal(self, sig, cfg):
        """Only offer members with verified shared footage and audio."""
        try:
            reference = self._audio_reference(sig, cfg)
            visual_reference = self._visual_reference(sig, cfg)
            good = {}
            for rel, member in sig["members"].items():
                try:
                    audio = self._segment_audio(sig, self._full(sig["root"], rel), member["boundary_s"])
                    visual = self._segment_visual(sig, self._full(sig["root"], rel), member["boundary_s"])
                    if (same_audio(reference, audio, member["boundary_s"])
                            and same_visual(visual_reference, visual, member["boundary_s"],
                                            sig.get("credit_text", False))):
                        good[rel] = member
                except (OSError, ValueError, subprocess.TimeoutExpired):
                    continue
            if len(good) < cfg["min_videos"]:
                return False
            sig["members"] = good
            sig["audio_verified"] = True
            sig["visual_verified"] = True
            return True
        except Exception as exc:
            self.log(f"[trim] {sig['root']}:{sig['folder']}: trim proposal omitted: {str(exc)[:160]}")
            return False

    def _decode_audio(self, path, seconds, start=0.0):
        import numpy as np
        info = self.probe(path)
        if not info:
            raise ValueError("cannot inspect audio")
        tracks = []
        for index in range(sum(kind == "audio" for kind, _ in info["streams"])):
            res = self._run(["ffmpeg", "-nostdin", "-v", "error", "-threads", "2",
                             "-ss", f"{start:.6f}", "-i", path, "-t", f"{seconds:.6f}", "-map", f"0:a:{index}",
                             "-vn", "-ac", "1", "-ar", str(AUDIO_RATE),
                             "-f", "f32le", "-"], timeout=180)
            if res.returncode or res.stderr.strip() or not res.stdout:
                raise ValueError("cannot decode intro audio")
            tracks.append(np.frombuffer(res.stdout, dtype="<f4").copy())
        return tracks

    def _audio_reference(self, sig, cfg):
        """Persist an uncut audio reference supported by min_videos members.

        It survives cutting/deleting originals and service restarts. No audio
        consensus means no cuts; visual detection alone cannot authorize it.
        """
        import numpy as np
        path = os.path.join(self.cache_dir, sig["id"], sig["kind"] + "_audio_v1.npz")
        if os.path.isfile(path):
            with np.load(path, allow_pickle=False) as stored:
                return [stored[name].copy() for name in sorted(stored.files)]
        seconds = max(m["boundary_s"] for m in sig["members"].values()) + (cfg["max_keyframe_gap_s"] if sig["kind"] == "intro" else 0)
        samples = []
        for rel, member in sorted(sig["members"].items()):
            if member["state"] not in ("pending", "queued", "skipped"):
                continue
            full = self._full(sig["root"], rel)
            try:
                st = os.stat(full)
                if (st.st_size, st.st_mtime_ns) != (member["size"], member["mtime_ns"]):
                    continue
                audio = self._segment_audio(sig, full, seconds)
                samples.append((audio, member["boundary_s"], (st.st_dev, st.st_ino)))
            except (OSError, ValueError, subprocess.TimeoutExpired):
                continue
        minimum = cfg["min_videos"]
        for audio, boundary, inode in samples:
            supporters = {identity for other, end, identity in samples
                          if same_audio(audio, other, min(boundary, end))}
            if len(supporters) < minimum:
                continue
            os.makedirs(os.path.dirname(path), exist_ok=True)
            tmp = path + ".tmp"
            with open(tmp, "wb") as fh:
                np.savez_compressed(fh, **{f"track_{i:04}": data for i, data in enumerate(audio)})
            os.replace(tmp, path)
            return audio
        raise ValueError(f"trim audio does not match across {minimum} distinct videos")

    def _audio_guard(self, sig, full, seconds, cfg):
        try:
            reference = self._audio_reference(sig, cfg)
            if not same_audio(reference, self._segment_audio(sig, full, seconds), seconds):
                return False, "audio substantially differs from the shared segment; not cut"
            return True, ""
        except Exception as exc:
            return False, f"audio could not be verified; not cut ({str(exc)[:160]})"

    def keyframe_at_or_after(self, path, boundary, gap):
        """The first video keyframe time in [boundary, boundary + gap], or None."""
        lo = max(0.0, boundary - 1.0)
        res = self._run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                         "packet=pts_time,flags", "-of", "csv=p=0",
                         "-read_intervals", f"{lo:.3f}%{boundary + gap + 1.0:.3f}", path], timeout=300, heavy=False)
        if res.returncode != 0:
            return None
        best = None
        for line in res.stdout.decode("utf-8", "replace").splitlines():
            parts = line.strip().split(",")
            if len(parts) < 2 or "K" not in parts[1]:
                continue
            try:
                t = float(parts[0])
            except ValueError:
                continue
            if boundary <= t <= boundary + gap and (best is None or t < best):
                best = t
        return best

    @staticmethod
    def _copy_into(src, dst):
        """Bytes of src into the inode behind dst (KSF-048: never a rename)."""
        with open(src, "rb") as s, open(dst, "r+b") as d:
            shutil.copyfileobj(s, d, 1 << 20)
            d.flush()
            d.truncate()
            os.fsync(d.fileno())

    @staticmethod
    def _same_bytes(a, b):
        if os.path.getsize(a) != os.path.getsize(b):
            return False
        with open(a, "rb") as fa, open(b, "rb") as fb:
            while True:
                ca, cb = fa.read(1 << 20), fb.read(1 << 20)
                if ca != cb:
                    return False
                if not ca:
                    return True

    def _decodes(self, path, seek_args):
        res = self._run(["ffmpeg", "-nostdin", "-v", "error", "-threads", "2"] + seek_args +
                        ["-i", path, "-f", "null", "-"], timeout=600)
        return res.returncode == 0 and not res.stderr.strip(), res.stderr.decode("utf-8", "replace").strip()[:200]

    def _cut_one(self, sig, rel, m, cfg):
        """Cuts one member. Returns (state, note). Never touches the file unless
        the result has been verified."""
        full = self._full(sig["root"], rel)
        if not full or not os.path.isfile(full):
            return "error", "the file is gone"
        st = os.stat(full)
        info = self.probe(full)
        if not info:
            return "error", "ffprobe could not read the file"
        d = info["duration"]
        if sig["kind"] == "intro":
            kf = self.keyframe_at_or_after(full, m["boundary_s"], cfg["max_keyframe_gap_s"])
            if kf is None:
                return "skipped", (f"no keyframe near the boundary ({m['boundary_s']:.2f}s, "
                                   f"within {cfg['max_keyframe_gap_s']}s) - not re-encoded")
            # An OUTPUT-side -ss: with -c copy, an input-side one seeks to the seek
            # point before the position (a second too much in webm), this one drops
            # packets until the first keyframe at or after it. A millisecond below
            # the keyframe keeps that keyframe itself.
            seek = ["-ss", f"{max(0.0, kf - 0.001):.6f}"]
            expected = d - kf
            tail = []
        else:
            end_t = d - m["boundary_s"]
            if end_t < 1.0:
                return "error", "the outro would leave less than a second"
            seek, expected, tail = [], end_t, ["-t", f"{end_t:.3f}"]
        removed_seconds = kf if sig["kind"] == "intro" else m["boundary_s"]
        for guard in (self._audio_guard, self._visual_guard):
            ok, note = guard(sig, full, removed_seconds, cfg)
            if not ok:
                return "skipped", note
        if expected < 0.55 * d:
            return "skipped", "the cut would remove more than 45% of the video"
        ext = os.path.splitext(full)[1].lower()
        root_path = self._root_path(sig["root"])
        work = os.path.join(root_path, ORIGINALS_DIR, WORK_DIR)
        os.makedirs(work, exist_ok=True)
        free = shutil.disk_usage(root_path).free
        if free < 2 * st.st_size + cfg["min_free_gb"] * 1e9:
            return "queued", f"waiting for disk space ({free / 1e9:.1f} GB free)"
        tmp = os.path.join(work, f"{sig['id']}_{hashlib.sha1(rel.encode()).hexdigest()[:10]}{ext}")
        faststart = ["-movflags", "+faststart"] if ext in (".mp4", ".m4v", ".mov") else []
        try:
            fallback = False
            last = []
            for maps in (["-map", "0"], ["-map", "0:v?", "-map", "0:a?", "-map", "0:s?"]):
                res = self._run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-i", full] + seek + maps + [
                    "-c", "copy", "-map_metadata", "0", "-map_chapters", "0",
                    "-avoid_negative_ts", "make_zero"] + tail + faststart + [tmp], timeout=3600)
                if res.returncode == 0 and os.path.exists(tmp):
                    break
                fallback = True
                last = res.stderr.decode("utf-8", "replace").strip().splitlines()
            else:
                return "error", "ffmpeg failed: " + (last[-1][:160] if last else "unknown")
            out = self.probe(tmp)
            if not out:
                return "error", "the result is not readable"
            if abs(out["duration"] - expected) > 0.5:
                return "error", f"the result is {out['duration']:.1f}s, expected {expected:.1f}s"
            # The fallback may only drop attachments/data streams (fonts, timecode):
            # a cut that loses a subtitle track is refused, not committed.
            kinds = ("video", "audio", "subtitle")
            before = sorted(s for s in info["streams"] if s[0] in kinds)
            after = sorted(s for s in out["streams"] if s[0] in kinds)
            if before != after:
                return "error", "the result's streams differ from the original's"
            ok, msg = self._decodes(tmp, ["-t", "5"])
            if ok:
                ok, msg = self._decodes(tmp, ["-sseof", "-5"])
            if not ok:
                return "error", "the result does not decode cleanly: " + msg
            return self._commit(sig, rel, m, full, st, tmp, cfg)
        finally:
            try:
                os.remove(tmp)
            except OSError:
                pass

    def _commit(self, sig, rel, m, full, st, tmp, cfg):
        root_path = self._root_path(sig["root"])
        bak_dir = os.path.join(root_path, ORIGINALS_DIR, sig["id"], os.path.dirname(rel))
        os.makedirs(bak_dir, exist_ok=True)
        bak = os.path.join(bak_dir, os.path.basename(rel))
        shutil.copyfile(full, bak)
        if os.path.getsize(bak) != st.st_size:
            os.remove(bak)
            return "error", "the copy of the original is incomplete"
        with self.lock:
            m.update(state="committing", backup=bak, orig_size=st.st_size)
            self._save()
        try:
            self._copy_into(tmp, full)
            if not self._same_bytes(tmp, full):
                raise OSError("read-back differs")
            os.utime(full, None)        # caches keyed by size + mtime refresh
        except Exception as exc:
            try:
                self._copy_into(bak, full)
                os.utime(full, None)
                note = f"writing failed ({exc}); the original was put back"
            except OSError as exc2:
                note = f"writing failed ({exc}) and the restore failed ({exc2}); the original is in {bak}"
            m["backup"] = bak
            return "error", note
        new = os.stat(full)
        m.update(post_size=new.st_size, post_mtime_ns=new.st_mtime_ns, cut_at=time.time(),
                 orig_kept=True)
        if sig.get("auto_delete"):
            self._drop_backup(m)
        self._notify_cut(sig["root"], rel, st, new)
        return "cut", ""

    def _after_cut(self, sig, rel, m):
        """The file now has new bytes: another signature waiting on the same
        inode (an outro after an intro) must not mistake that for a foreign
        change. Its boundary is measured from its own end, so it still holds."""
        with self.lock:
            for other in self.state["signatures"].values():
                for o_rel, o in other["members"].items():
                    if o is m or o.get("ino") != m.get("ino") or o.get("dev") != m.get("dev"):
                        continue
                    if o["state"] in ("pending", "queued"):
                        o["size"], o["mtime_ns"] = m["post_size"], m["post_mtime_ns"]
            self._save()

    def _after_undo(self, m, st):
        """The file is back at the state the NEXT-older cut on it left: that
        cut's undo check has to expect this stat now."""
        with self.lock:
            older = [o for sg in self.state["signatures"].values() for o in sg["members"].values()
                     if o is not m and o.get("ino") == m.get("ino") and o.get("dev") == m.get("dev")
                     and o["state"] == "cut" and o.get("orig_kept") and o.get("cut_at", 0) < m.get("cut_at", 0)]
            if older:
                latest = max(older, key=lambda o: o["cut_at"])
                latest["post_size"], latest["post_mtime_ns"] = st.st_size, st.st_mtime_ns

    def _drop_backup(self, m):
        bak = m.get("backup")
        if bak:
            try:
                os.remove(bak)
            except OSError:
                pass
            folder = os.path.dirname(bak)
            for _ in range(8):
                try:
                    os.rmdir(folder)
                except OSError:
                    break
                if os.path.basename(folder) == ORIGINALS_DIR:
                    break
                folder = os.path.dirname(folder)
        m["orig_kept"] = False

    def apply_signature(self, sid):
        """Cut every member of an approved signature that is still waiting."""
        cfg = self.cfg()
        sig = self.state["signatures"].get(sid)
        if not sig or sig["status"] != "approved" or not self.in_scope(sig["root"], sig["folder"]):
            return {"cut": 0}
        cut = 0
        for rel, m in sorted(sig["members"].items()):
            if m["state"] not in ("pending", "queued"):
                continue
            if self.busy():
                break
            full = self._full(sig["root"], rel)
            try:
                st = os.stat(full)
            except (OSError, TypeError):
                m.update(state="error", note="the file is gone")
                continue
            if (st.st_size, st.st_mtime_ns) != (m["size"], m["mtime_ns"]):
                m.update(state="error", note="the file changed since it was fingerprinted - scan again")
                self.log(f"[trim] {sig['id']} {rel}: {m['note']}")
                continue
            twin = [r for r, o in sig["members"].items()
                    if r != rel and o.get("ino") == m.get("ino") and o.get("dev") == m.get("dev") and o["state"] == "cut"]
            if twin:
                m.update(state="cut", note=f"same file as {twin[0]}")
                continue
            self.current = rel
            state, note = self._cut_one(sig, rel, m, cfg)
            self.current = None
            with self.lock:
                m.update(state=state, note=note)
                if state in ("skipped", "error"):
                    m["size"], m["mtime_ns"] = st.st_size, st.st_mtime_ns
                self._save()
            if state == "cut":
                cut += 1
                self._after_cut(sig, rel, m)
            self.log(f"[trim] {sig['id']} {rel}: {state} {note}")
        if cut:
            self.invalidate()
        return {"cut": cut}

    # ---- the user's decisions ----
    def approve(self, sid):
        with self.lock:
            sig = self.state["signatures"].get(sid)
            if not sig:
                return False
            sig["status"] = "approved"
            self._save()
        self._queue(("apply", sid))
        return True

    def reject(self, sid):
        with self.lock:
            sig = self.state["signatures"].get(sid)
            if not sig:
                return False
            sig["status"] = "rejected"
            self._save()
        return True

    def set_auto_delete(self, sid, value):
        with self.lock:
            sig = self.state["signatures"].get(sid)
            if not sig:
                return False
            sig["auto_delete"] = bool(value)
            self._save()
        return True

    def undo_signature(self, sid):
        """Writes every kept original back into its inode."""
        sig = self.state["signatures"].get(sid)
        if not sig:
            return {"restored": 0}
        restored = 0
        for rel, m in sig["members"].items():
            if m["state"] != "cut" or not m.get("orig_kept"):
                continue
            full = self._full(sig["root"], rel)
            try:
                st = os.stat(full)
            except (OSError, TypeError):
                m["note"] = "the file is gone"
                continue
            if (st.st_size, st.st_mtime_ns) != (m.get("post_size"), m.get("post_mtime_ns")):
                m["note"] = "the file changed after the cut (another cut?) - undo that one first"
                continue
            try:
                self._copy_into(m["backup"], full)
                os.utime(full, None)
            except OSError as exc:
                m["note"] = f"restore failed: {exc}"
                continue
            self._drop_backup(m)
            m.update(state="undone", note="restored")
            restored += 1
            back = os.stat(full)
            self._after_undo(m, back)
            self._notify_cut(sig["root"], rel, st, back)
        with self.lock:
            if restored:
                sig["status"] = "pending"
            self._save()
        if restored:
            self.invalidate()
        return {"restored": restored}

    def delete_originals(self, sid):
        sig = self.state["signatures"].get(sid)
        if not sig:
            return {"deleted": 0}
        deleted = 0
        for m in sig["members"].values():
            if m["state"] == "cut" and m.get("orig_kept"):
                self._drop_backup(m)
                deleted += 1
        with self.lock:
            self._save()
        return {"deleted": deleted}

    def queue_job(self, job):
        with self.lock:
            if job not in self.jobs:
                self.jobs.append(job)
        self.wake.set()
        return True

    _queue = queue_job

    # ---- triggers ----
    def enqueue_paths(self, items):
        """After a download: [{root, path}] of new files; their video folders
        are looked at."""
        cfg = self.cfg()
        if not (cfg["enabled"] and cfg["after_download"]):
            return 0
        folders = set()
        for it in items:
            rel = it.get("path") or ""
            if (os.path.splitext(rel)[1].lower() in VIDEO_EXTS
                    and self.in_scope(it.get("root") or "downloads", os.path.dirname(rel))):
                folders.add((it.get("root") or "downloads", os.path.dirname(rel)))
        for root_id, rel_dir in sorted(folders):
            self._queue(("scan", root_id, rel_dir))
        return len(folders)

    def queue_dirty_folders(self, everything=False):
        """The daily pass: every folder with enough videos (or an approved
        signature) that changed since it was last looked at."""
        cfg = self.cfg()
        approved = {(s["root"], s["folder"]) for s in self.state["signatures"].values() if s["status"] == "approved"}
        queued = 0
        for root in self.roots():
            scopes = [item.partition(":")[2].replace("\\", "/").strip("/")
                      for item in cfg.get("folders", [])
                      if item.partition(":")[0] == root["id"] and ":" in item]
            if not scopes:
                continue
            base = os.path.abspath(root["path"])
            for dirpath, dirnames, filenames in os.walk(base):
                dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
                relative = os.path.relpath(dirpath, base).replace(os.sep, "/")
                relative = "" if relative == "." else relative
                dirnames[:] = [d for d in dirnames
                               if any(not scope or (relative + "/" + d).strip("/") == scope
                                      or scope.startswith((relative + "/" + d).strip("/") + "/")
                                      or (relative + "/" + d).strip("/").startswith(scope + "/")
                                      for scope in scopes)]
                videos = sum(1 for n in filenames
                             if not n.startswith(".") and os.path.splitext(n)[1].lower() in VIDEO_EXTS)
                rel_dir = os.path.relpath(dirpath, base).replace(os.sep, "/")
                rel_dir = "" if rel_dir == "." else rel_dir
                if not self.in_scope(root["id"], rel_dir):
                    continue
                if videos < cfg["min_videos"] and (root["id"], rel_dir) not in approved:
                    continue
                known = self.state["folders"].get(f"{root['id']}:{rel_dir}")
                try:
                    mtime = os.stat(dirpath).st_mtime_ns
                except OSError:
                    continue
                if everything or not known or known.get("dir_mtime_ns") != mtime:
                    self._queue(("scan", root["id"], rel_dir))
                    queued += 1
        with self.lock:
            self.state["last_pass"] = time.time()
            self._save()
        return queued

    def run_now(self, everything=False):
        """Starts the daily pass at once, in the background."""
        thread = threading.Thread(target=self.queue_dirty_folders, args=(everything,), daemon=True,
                                  name="video-trim-walk")
        thread.start()
        return thread

    def _daily_due(self, cfg, now):
        text = (cfg.get("daily_time") or "").strip()
        try:
            hh, mm = (int(p) for p in text.split(":"))
        except ValueError:
            return False
        today = time.strftime("%Y-%m-%d", time.localtime(now))
        local = time.localtime(now)
        return (local.tm_hour, local.tm_min) >= (hh, mm) and self.state.get("daily_day") != today

    # ---- the loop ----
    def run_forever(self):
        self.log("[trim] worker started")
        while True:
            try:
                cfg = self.cfg()
                if not cfg["enabled"]:
                    time.sleep(60)
                    continue
                with self.lock:
                    # Restores and deletions are the user's own request and do
                    # not wait for the downloads.
                    job = next((j for j in self.jobs if j[0] in ("undo", "delete")), None)
                    if job is None and not self.busy():
                        job = self.jobs[0] if self.jobs else None
                    if job:
                        self.jobs.remove(job)
                if job is None:
                    if not self.busy() and self._daily_due(cfg, time.time()):
                        with self.lock:
                            self.state["daily_day"] = time.strftime("%Y-%m-%d")
                            self._save()
                        self.queue_dirty_folders()
                        continue
                    self.wake.wait(60 if not self.busy() else 120)
                    self.wake.clear()
                    continue
                if job[0] == "scan":
                    res = self.scan_folder(job[1], job[2])
                    if res.get("deferred"):
                        self._queue(job)
                        time.sleep(60)
                        continue
                    if res.get("cut"):
                        self.invalidate()
                elif job[0] == "apply":
                    self.apply_signature(job[1])
                elif job[0] == "undo":
                    self.undo_signature(job[1])
                elif job[0] == "delete":
                    self.delete_originals(job[1])
            except Exception as exc:
                self.log(f"[trim] worker error: {type(exc).__name__}: {exc}")
                time.sleep(30)

    def start(self):
        thread = threading.Thread(target=self.run_forever, name="video-trim", daemon=True)
        thread.start()
        return thread

    # ---- what the page shows ----
    def list_signatures(self):
        out = []
        with self.lock:
            sigs = list(self.state["signatures"].values())
        for sig in sigs:
            members = sig["members"]
            counts = {}
            for m in members.values():
                counts[m["state"]] = counts.get(m["state"], 0) + 1
            kept = [m for m in members.values() if m["state"] == "cut" and m.get("orig_kept")]
            problems = [{"path": rel, "state": m["state"], "note": m.get("note", "")}
                        for rel, m in members.items() if m["state"] in ("error", "skipped") and m.get("note")]
            out.append({
                "id": sig["id"], "root": sig["root"], "folder": sig["folder"], "kind": sig["kind"],
                "length_s": sig["length_s"], "status": sig["status"], "auto_delete": sig["auto_delete"],
                "videos": len(members), "counts": counts, "created": sig["created"],
                "originals_kept": len(kept), "originals_bytes": sum(m.get("orig_size", 0) for m in kept),
                "errors": problems[:8], "preview_frames": PREVIEW_FRAMES,
            })
        out.sort(key=lambda s: ({"pending": 0, "approved": 1, "rejected": 2}.get(s["status"], 3), -s["created"]))
        return out

    def preview(self, sid):
        sig = self.state["signatures"].get(sid)
        if not sig:
            return None
        members = []
        for rel, m in sorted(sig["members"].items()):
            if sig["kind"] == "intro":
                start, end = 0.0, m["boundary_s"]
            else:
                start, end = m["duration"] - m["boundary_s"], m["duration"]
            members.append({"path": rel, "state": m["state"], "start_s": round(start, 2), "end_s": round(end, 2)})
        have = [i for i in range(PREVIEW_FRAMES)
                if os.path.exists(os.path.join(self.cache_dir, sid, f"{i}.jpg"))]
        return {"id": sid, "kind": sig["kind"], "length_s": sig["length_s"], "frames": have, "members": members}

    def frame_path(self, sid, index):
        if sid not in self.state["signatures"] or not 0 <= index < PREVIEW_FRAMES:
            return None
        path = os.path.join(self.cache_dir, sid, f"{index}.jpg")
        return path if os.path.exists(path) else None

    def status(self):
        cfg = self.cfg()
        with self.lock:
            return {"enabled": cfg["enabled"], "after_download": cfg["after_download"],
                    "daily_time": cfg["daily_time"], "queued": len(self.jobs), "current": self.current,
                    "last_pass": self.state.get("last_pass"), "folders_scanned": len(self.state["folders"])}

