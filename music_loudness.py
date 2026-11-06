"""How loud each file of the music page is, so every song and video plays at
the same loudness (vault KSF-058).

User, 2026-09-27: "videos generally play much louder than songs. All media
files should play at equal volume on the music page." Measured on the library:
songs range from -4 to -18 LUFS. A song and its music video are usually the
same upload at the same level, but some songs are separate .m4a copies (AAC,
96 kHz) that were made about 10 dB quieter than their videos (Tenebrax - Code
Red: video -4.4, song -15.8 LUFS), so switching Song | Video jumped in volume.

What is measured is the EBU R128 integrated loudness and the true peak
(ffmpeg's ebur128 filter) of the file's first audio stream, once per file
version: the key is the file's (device, inode, size, mtime), like
music_library.TagCache, so a song's hard links in playlists are measured once
and a replaced file again. Nothing in the files changes: the page applies a
gain per file.

THE TARGET (user: "We should target the highest adjusted felt volume that
does not cause cut-off"): one loudness for everything, as high as it can be.
A file may be turned up only until its true peak reaches 0 dBTP, so each file
has a ceiling (its loudness minus its peak); the target is the highest level
that REACH of the library's files can get to without passing that ceiling.
The few very dynamic files below it play at their own ceiling - as loud as
they go without clipping, a little under the rest. Turning a file up needs
Web Audio on the page (element.volume stops at 1); without it the page only
turns files down.
"""
import json
import math
import os
import re
import subprocess
import threading
import time

LOUDNESS_VERSION = 1       # part of every key: a change to measure() re-measures everything
# ebur128 reports this floor for a file with no sound at all: no loudness to match.
SILENT_LUFS = -69.0
MEASURE_TIMEOUT_S = 900    # a 2-hour mix decodes in about a minute here
SAVE_EVERY = 50            # results between writes of the cache file
_I_RE = re.compile(r"I:\s+(-?[\d.]+|-inf) LUFS")
_PEAK_RE = re.compile(r"Peak:\s+(-?[\d.]+|-inf) dBFS")


def measure(path, ffmpeg="ffmpeg"):
    """[integrated loudness (LUFS), true peak (dBTP)] of the file's first
    audio stream; the loudness is None when the file has no audio, is silent
    or does not decode. The TRUE peak (4x oversampled), not the sample peak:
    the browser resamples (a 96 kHz file to the device's 48 kHz) and the
    peaks between samples are what would clip once a quiet file is turned
    up. Low priority: this runs beside downloads."""
    cmd = ["nice", "-n", "15", "ionice", "-c", "3",
           ffmpeg, "-nostdin", "-nostats", "-hide_banner", "-i", path,
           "-map", "0:a:0", "-vn", "-sn", "-dn",
           "-af", "ebur128=framelog=verbose:peak=true", "-f", "null", "-"]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, errors="replace", timeout=MEASURE_TIMEOUT_S)
    except (OSError, subprocess.TimeoutExpired):
        return [None, None]
    summary = p.stderr[p.stderr.rfind("Summary:"):] if "Summary:" in p.stderr else ""
    i = _I_RE.search(summary)
    peak = _PEAK_RE.search(summary)
    lufs = float(i.group(1)) if i and i.group(1) != "-inf" else None
    if lufs is not None and lufs <= SILENT_LUFS:
        lufs = None
    pk = float(peak.group(1)) if peak and peak.group(1) != "-inf" else None
    return [None if lufs is None else round(lufs, 1), None if pk is None else round(pk, 1)]


class LoudnessCache:
    """measure() results by file version, persisted. Thread-safe."""

    def __init__(self, path):
        self.path = path
        self.lock = threading.Lock()
        self.data = {}
        self.dirty = False
        try:
            with open(path, encoding="utf-8") as fh:
                loaded = json.load(fh)
            if isinstance(loaded, dict):
                self.data = loaded
        except (OSError, ValueError):
            pass

    @staticmethod
    def key(st):
        return "%d:%d:%d:%d:%d" % (LOUDNESS_VERSION, st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns)

    def get(self, st):
        """The stored [lufs, peak], or None when this version was never measured."""
        with self.lock:
            return self.data.get(self.key(st))

    def put(self, st, value):
        with self.lock:
            self.data[self.key(st)] = value
            self.dirty = True

    def prune(self, live_keys):
        with self.lock:
            gone = [k for k in self.data if k not in live_keys]
            for k in gone:
                del self.data[k]
            if gone:
                self.dirty = True

    def save(self):
        with self.lock:
            if not self.dirty:
                return
            blob = json.dumps(self.data, separators=(",", ":"))
            self.dirty = False
        tmp = self.path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                fh.write(blob)
            os.replace(tmp, self.path)
        except OSError:
            with self.lock:
                self.dirty = True


class Measurer:
    """Measures what the cache is missing, a few files at a time, in the
    background. want() takes [(full path, stat)]; on_batch() is called after
    every `batch` results and when the queue runs dry, so the catalogue can
    pick the new numbers up without waiting for the whole library."""

    def __init__(self, cache, workers=2, batch=200, on_batch=None, measure_fn=None):
        self.cache = cache
        self.workers = workers
        self.batch = batch
        self.on_batch = on_batch
        self.measure_fn = measure_fn or measure
        self.lock = threading.Lock()
        self.queue = []
        self.queued = set()          # cache keys waiting or being measured
        # Workers still taking from the queue. Counted under the lock, not by
        # is_alive(): one on its way out (saving) takes nothing more, and work
        # queued at that moment must get a worker of its own.
        self.running = 0
        self.done = 0
        self.since_batch = 0
        self.since_save = 0
        self.busy = 0

    def want(self, items):
        """Queue every (full, st) whose version is not measured yet; returns how many were added."""
        added = 0
        with self.lock:
            for full, st in items:
                k = self.cache.key(st)
                if k in self.queued or self.cache.get(st) is not None:
                    continue
                self.queued.add(k)
                self.queue.append((full, st, k))
                added += 1
            while self.queue and self.running < self.workers:
                self.running += 1
                threading.Thread(target=self._run, daemon=True, name="music-loudness").start()
        return added

    def status(self):
        with self.lock:
            return {"queued": len(self.queue), "measuring": self.busy, "measured": self.done}

    def _run(self):
        while True:
            with self.lock:
                if not self.queue:
                    self.running -= 1
                    last = self.busy == 0
                    break
                full, st, k = self.queue.pop(0)
                self.busy += 1
            value = self.measure_fn(full)
            self.cache.put(st, value)
            with self.lock:
                self.busy -= 1
                self.queued.discard(k)
                self.done += 1
                self.since_batch += 1
                self.since_save += 1
                save = self.since_save >= SAVE_EVERY
                if save:
                    self.since_save = 0
                report = self.since_batch >= self.batch
                if report:
                    self.since_batch = 0
            if save:
                self.cache.save()
            if report and self.on_batch:
                self.on_batch()
        # The queue ran dry: the last worker out writes what is left and reports it.
        if last:
            self.cache.save()
            with self.lock:
                report, self.since_batch = self.since_batch > 0, 0
            if report and self.on_batch:
                self.on_batch()


PEAK_CEILING_DBTP = 0.0    # no file is turned up past this: above it the output clips
REACH = 0.95               # the share of files that must be able to reach the target


def ceiling(value):
    """The loudest a file can be played without its true peak passing the
    ceiling: [lufs, peak] -> LUFS, or None when it has no loudness."""
    lufs, peak = value[0], value[1] if len(value) > 1 else None
    if lufs is None:
        return None
    return lufs + (PEAK_CEILING_DBTP - peak) if peak is not None else lufs


def gain_db(value, target):
    """The gain that brings a file to `target`, but never so far up that its
    true peak passes the ceiling; None when it has no loudness. Rounded DOWN
    to 0.1 dB, so the rounding can never be what makes it clip."""
    lufs = value[0]
    if lufs is None:
        return None
    g = target - lufs
    top = ceiling(value)
    if top is not None:
        g = min(g, top - lufs)
    return math.floor(g * 10 + 1e-9) / 10


def choose_target(values, reach=REACH):
    """The highest loudness that `reach` of the files can be brought to
    without clipping: the (1 - reach) quantile of their ceilings, rounded
    down to 0.5 dB so a growing library does not move every volume a hair
    with each download. None when nothing is measured yet."""
    tops = sorted(c for c in (ceiling(v) for v in values) if c is not None)
    if not tops:
        return None
    t = tops[min(len(tops) - 1, int(math.floor((1 - reach) * len(tops))))]
    return math.floor(t * 2) / 2


def level(value):
    """[lufs, ceiling] of a file, for a page that plays at a loudness of the
    viewer's own choosing (user, 2026-11-03: "Add a value for target LUFS"):
    its gain there is min(target, ceiling) - lufs. The loudness is rounded UP
    and the ceiling DOWN to 0.1 dB, so the rounding can never make it clip.
    None when it has no loudness."""
    top = ceiling(value)
    if top is None:
        return None
    return [math.ceil(value[0] * 10 - 1e-9) / 10, math.floor(top * 10 + 1e-9) / 10]


def plan(measured, reach=REACH):
    """{target, gains: {root: {path: dB}}, levels: {root: {path: [lufs,
    ceiling]}}, unknown: dB} for {(root, path): [lufs, peak]} - what the page
    needs. `unknown` is the gain for a file not measured yet: the median of
    the others, but never up (an unmeasured file may peak anywhere)."""
    target = choose_target(measured.values(), reach)
    if target is None:
        return {"target": None, "gains": {}, "levels": {}, "unknown": 0.0}
    gains = {}
    levels = {}
    known = []
    for (root, path), value in measured.items():
        g = gain_db(value, target)
        if g is None:
            continue
        gains.setdefault(root, {})[path] = g
        levels.setdefault(root, {})[path] = level(value)
        known.append(g)
    known.sort()
    unknown = min(0.0, known[len(known) // 2]) if known else 0.0
    return {"target": target, "gains": gains, "levels": levels, "unknown": unknown}


def _fill(cache_path, tops, workers):
    """Measure every audio file, and every video in a folder named "videos" or
    "music_videos", under the given folders into the cache file - the first
    fill of the library, done before the server ever reads the file."""
    from music_library import is_audio, is_video
    cache = LoudnessCache(cache_path)
    items = []
    for top in tops:
        for d, dirs, files in os.walk(top, followlinks=True):
            dirs[:] = [x for x in dirs if not x.startswith((".", "$"))]
            in_videos = any(p in ("videos", "music_videos") for p in d.split(os.sep))
            for f in files:
                if f.startswith(".") or not (is_audio(f) or (in_videos and is_video(f))):
                    continue
                full = os.path.join(d, f)
                try:
                    items.append((full, os.stat(full)))
                except OSError:
                    pass
    done = threading.Event()
    m = Measurer(cache, workers=workers, batch=500, on_batch=lambda: print(
        f"{time.strftime('%H:%M:%S')} {m.status()}", flush=True))
    print(f"{len(items)} files, {m.want(items)} to measure", flush=True)
    while True:
        s = m.status()
        if not s["queued"] and not s["measuring"]:
            break
        done.wait(5)
    cache.save()
    print(f"done: {len(cache.data)} entries", flush=True)


if __name__ == "__main__":
    import sys
    if len(sys.argv) >= 4 and sys.argv[1] == "fill":
        _fill(sys.argv[2], sys.argv[4:], int(sys.argv[3]))
    else:
        sys.exit("usage: music_loudness.py fill <cache.json> <workers> <folder>...")
