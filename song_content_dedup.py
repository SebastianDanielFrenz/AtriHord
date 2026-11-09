"""The same song held twice as two different YouTube uploads: one stays, the
other becomes a link to it (user, 2026-09-20: "one video and one song in the
results is fine; the same song twice is not").

These cannot be settled by video id - the two files ARE different uploads - so
identity is decided by the SOUND. Each candidate is decoded to 8 kHz mono, cut
into 50 ms frames, and compared at the best alignment within a second, by two
measures: the loudness envelope, and the attacks (see onsets()). A copy counts
as the same recording at 0.85 loudness OR 0.90 attacks - the second is what
catches a visualiser channel's remastered upload, whose loudness has been
changed on purpose. A different version (a remix, a slowed edit, another take)
stays far below both. Bytes cannot be compared: a re-encode changes every
sample.

Only AUDIO is ever compared with audio. A video and a song of the same name are
two different things and both stay.

Nothing is deleted: the copy that goes lands in its own root's trash with a
manifest, and a hard link to the kept song takes its place, so every path in
every library keeps playing.

  venv/bin/python song_content_dedup.py [--apply] [--report FILE] [--limit N]
"""
import argparse
import collections
import json
import os
import re
import subprocess
import sys
import time
import uuid

sys.path.insert(0, "/data2/ksf_web_dl")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import numpy as np                      # noqa: E402
import media_ai                         # noqa: E402

BASE_DIR = "/data2/ksf_web_dl"
DOWNLOADS = os.path.join(BASE_DIR, "web_files", "downloads")
MUSIC = os.path.join(DOWNLOADS, "youtube_music")
TRASH_DIR_NAME = ".trash"
RATE = 8000
FRAME = 400                             # 50 ms
SECONDS = 150                           # enough to tell two songs apart
MAX_LAG = 20                            # frames, i.e. one second either way


def norm_title(name):
    """The song's name with a video id, a trailing "| Music Visualization..."
    and punctuation taken out - what makes two files candidates at all."""
    name = re.sub(r"\[[A-Za-z0-9_-]{11}\]", "", name)
    name = re.sub(r"[│|].*$", "", name)
    return re.sub(r"[^a-z0-9]+", "", name.lower())


def envelope(path):
    """The track's loudness over time, or None if it will not decode."""
    out = subprocess.run(
        ["ffmpeg", "-v", "error", "-t", str(SECONDS), "-i", path,
         "-ac", "1", "-ar", str(RATE), "-f", "s16le", "-"],
        capture_output=True)
    if out.returncode != 0 or len(out.stdout) < RATE * 4:
        return None
    pcm = np.frombuffer(out.stdout, dtype="<i2").astype(np.float32)
    n = len(pcm) // FRAME
    env = np.abs(pcm[:n * FRAME].reshape(n, FRAME)).mean(axis=1)
    return env


def onsets(env):
    """Where the attacks are, not how loud they are: log, then each frame's
    rise over the one before. A channel that bass-boosts or re-masters a track
    moves its loudness envelope but not its attacks, and that is exactly the
    case the loudness measure alone gets wrong (measured: a remastered copy
    correlates 0.72-0.84 by loudness but 0.92-0.96 by attacks, while a
    different song stays under 0.11 in both)."""
    d = np.diff(np.log1p(env))
    return np.maximum(d, 0)


def similarity(a, b):
    """The best correlation of two envelopes within a second of shift. A
    recording and its re-encode keep the same shape; a different take or edit
    drifts apart immediately."""
    n = min(len(a), len(b))
    if n < 40:
        return 0.0
    best = -1.0
    for lag in range(-MAX_LAG, MAX_LAG + 1):
        if lag >= 0:
            x, y = a[lag:n], b[:n - lag]
        else:
            x, y = a[:n + lag], b[-lag:n]
        if len(x) < 40:
            continue
        x = x - x.mean()
        y = y - y.mean()
        d = float(np.linalg.norm(x) * np.linalg.norm(y))
        if d > 0:
            best = max(best, float(np.dot(x, y)) / d)
    return best


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


def keeper(entries):
    """Which copy stays: the one that is already the song store's, else the
    bigger file. The store is where a song lives, and its name there is the
    one the playlists and the songs index point at."""
    def score(e):
        return (e["path"].startswith("youtube_music/") and e["root"] == "downloads",
                e["size"])
    return max(entries, key=score)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--threshold", type=float, default=0.85, help="loudness match")
    ap.add_argument("--onset-threshold", type=float, default=0.90, help="attack match, for a remastered copy")
    ap.add_argument("--max-seconds-apart", type=float, default=1.0)
    ap.add_argument("--report", help="write every pair and its match to this JSON file")
    args = ap.parse_args()

    all_roots = roots()
    conn = media_ai.connect(BASE_DIR)
    rows = [r for r in conn.execute(
        "SELECT root, path FROM media WHERE kind = ? AND trashed IS NULL", ("audio",))]
    groups = collections.defaultdict(list)
    seen_inode = {}
    for r in rows:
        base = all_roots.get(r["root"])
        if not base:
            continue
        full = os.path.join(base, r["path"])
        try:
            st = os.stat(full)
        except OSError:
            continue
        if st.st_ino in seen_inode:         # already another name for this file
            continue
        seen_inode[st.st_ino] = full
        groups[norm_title(os.path.splitext(os.path.basename(r["path"]))[0])].append(
            {"root": r["root"], "path": r["path"], "full": full, "size": st.st_size,
             "ino": st.st_ino})

    candidates = [v for k, v in sorted(groups.items()) if k and len(v) > 1]
    print(f"{len(candidates)} titles are held by more than one file; listening to them")

    pairs, different, undecodable = [], [], []
    envs = {}
    for n, entries in enumerate(candidates, 1):
        if args.limit and len(pairs) >= args.limit:
            break
        keep = keeper(entries)
        for other in entries:
            if other is keep:
                continue
            for p in (keep["full"], other["full"]):
                if p not in envs:
                    envs[p] = envelope(p)
            a, b = envs[keep["full"]], envs[other["full"]]
            if a is None or b is None:
                undecodable.append(other["path"])
                continue
            apart = abs(len(a) - len(b)) * FRAME / RATE
            if apart > args.max_seconds_apart:      # not even the same length
                sim = ons = 0.0
            else:
                sim = similarity(a, b)
                ons = similarity(onsets(a), onsets(b)) if sim < args.threshold else sim
            same = sim >= args.threshold or ons >= args.onset_threshold
            (pairs if same else different).append(
                {"keep": keep, "drop": other, "sim": round(sim, 3),
                 "onsets": round(ons, 3), "apart": round(apart, 2)})
        if n % 25 == 0:
            print(f"  {n}/{len(candidates)} titles, {len(pairs)} the same song so far", flush=True)

    freed = sum(p["drop"]["size"] for p in pairs)
    print(f"\n{len(pairs)} copies are the same recording as a song already held "
          f"({freed / 1e9:.2f} GB)")
    print(f"{len(different)} are a different version and stay; {len(undecodable)} would not decode")
    for p in pairs[:5]:
        print(f"      same ({p['sim']}): {p['drop']['path']}\n"
              f"                 -> {p['keep']['path']}")
    for p in different[:5]:
        print(f"      different (loudness {p['sim']}, attacks {p['onsets']}, {p['apart']}s apart): {p['drop']['path']}")
    if args.report:
        with open(args.report, "w", encoding="utf-8") as fh:
            json.dump([{"sim": p["sim"], "apart": p["apart"], "keep": p["keep"]["path"],
                        "onsets": p["onsets"], "drop": p["drop"]["path"], "size": p["drop"]["size"]}
                       for p in sorted(pairs + different, key=lambda x: -x["sim"])],
                      fh, ensure_ascii=False, indent=1)
        print(f"every pair written to {args.report}")
    if not args.apply:
        print("\n(dry run - nothing changed)")
        return 0

    batch = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
    per_root = collections.defaultdict(list)
    done = 0
    for p in pairs:
        drop, keep = p["drop"], p["keep"]
        base = all_roots[drop["root"]]
        grave = os.path.join(base, TRASH_DIR_NAME, batch, drop["path"])
        os.makedirs(os.path.dirname(grave), exist_ok=True)
        try:
            os.rename(drop["full"], grave)
        except OSError as exc:
            print(f"  ! could not move {drop['path']}: {exc}")
            continue
        try:
            # Its own name, now the kept song - with the KEPT file's extension.
            # The first run kept the dropped copy's, and 207 of 213 links
            # ended up naming AAC or Opus ".mka" and Opus/WAV ".m4a" (see
            # fix_link_extensions.py).
            base, ext = os.path.splitext(drop["full"])[0], os.path.splitext(keep["full"])[1]
            link, n = base + ext, 2
            while os.path.lexists(link) and not os.path.samefile(link, keep["full"]):
                link, n = f"{base} ({n}){ext}", n + 1   # that name is another file's
            if not os.path.lexists(link):
                os.link(keep["full"], link)
        except OSError as exc:
            os.rename(grave, drop["full"])          # put it back rather than lose a name
            print(f"  ! could not link over {drop['path']}: {exc}")
            continue
        media_ai.set_trashed(conn, [(drop["root"], drop["path"])], True)
        per_root[drop["root"]].append({"path": drop["path"], "size": drop["size"],
                                       "why": f"the same recording as {keep['path']} "
                                              f"(match {p['sim']}), now a link to it"})
        done += 1
    conn.commit()
    for root_id, items in per_root.items():
        folder = os.path.join(all_roots[root_id], TRASH_DIR_NAME, batch)
        os.makedirs(folder, exist_ok=True)
        with open(os.path.join(folder, "_manifest.json"), "w", encoding="utf-8") as fh:
            json.dump({"batch": batch, "mode": "release", "deleted_at": time.time(),
                       "deleted_by": "media-dl song migration",
                       "items": [i["path"] for i in items],
                       "reason": "a second upload of a song already held, now a link to it",
                       "notes": items}, fh, ensure_ascii=False, indent=1)
    print(f"\n{done} copies replaced by links, {freed / 1e9:.2f} GB now in the trash (batch {batch})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
