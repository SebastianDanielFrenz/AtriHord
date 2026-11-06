"""Smooth motion made again for a video that was cut.

User, 2026-10-18: "when a video is intro/outro is cut for non-new downloads, re-do
the interpolation and 3D work." (The 3D half lives in stereo3d.py, LANE_REDO.)

The vision worker keeps interpolated segments per source key
sha256(root, path, size, int(mtime)) and variant (rate, max height, bitrate).
video_trim.py cuts INTO the original's inode, so size and mtime change, the key
changes, and everything made for the old version is orphaned (the worker evicts
it on its own). This module remembers what existed for the old key and makes the
same renditions for the new one:

  * a COMPLETE variant (the worker has its end.json) -> every segment is
    generated again;
  * a PARTIAL variant -> the same number of leading segments.

The jobs live in interp_redo.json (atomic writes), at most one per (root, path):
a newer cut of the same file merges into the waiting job instead of adding
another. One background thread runs one job at a time and only while no download
is running; it backs off when the worker cannot be reached or answers busy (the
GPU is shared with 3D, upscaling and the indexer). The first look at the worker's
variants happens in the cut's own callback; if the worker cannot be reached then,
the old key is kept in the job and asked again later.

Pure module: no Flask, no server.py. It is handed the functions that talk to the
worker (variants_fn, run_fn), so the harness can fake them.
"""
import json
import os
import re
import threading
import time

BACKOFF_MIN_S = 60
BACKOFF_MAX_S = 1800
MAX_ATTEMPTS = 8
BUSY_WAIT_S = 30
IDLE_WAIT_S = 20

# m<k> or f<rate>, then the max height and the bitrate: the worker's SEG_VARIANT.
VARIANT_RE = re.compile(r"^(m\d+|f\d+(?:\.\d+)?)_h(\d+)_r(\d+(?:\.\d+)?)$")


class Backoff(Exception):
    """The worker cannot be reached, or is busy: try this job again later."""


def parse_variant(variant):
    """'f120_h2160_r20' -> {"prefix": "f120", "max_height": 2160, "mbps": 20.0, "args": {"o": "120"}},
    or None. `args` is what interp_output_rate() reads: m=<k> for a multiplier, o=<rate> for an
    exact rate."""
    m = VARIANT_RE.match(variant or "")
    if not m:
        return None
    prefix = m.group(1)
    args = {"m": prefix[1:]} if prefix[0] == "m" else {"o": prefix[1:]}
    return {"prefix": prefix, "max_height": int(m.group(2)), "mbps": float(m.group(3)), "args": args}


def merge_variants(have, more):
    """Two variant lists -> one: per variant name the bigger job (complete beats partial, then more
    segments)."""
    out = {}
    for v in list(have or []) + list(more or []):
        name = v.get("variant")
        if not name:
            continue
        rank = (bool(v.get("complete")), int(v.get("segments") or 0))
        cur = out.get(name)
        if cur is None or rank > (bool(cur.get("complete")), int(cur.get("segments") or 0)):
            out[name] = {"variant": name, "segments": rank[1], "complete": rank[0]}
    return list(out.values())


class InterpRedo:
    """variants_fn(old_key) -> [{variant, segments, complete}] (raises Backoff when the worker is
    not there); run_fn(root, path, spec, should_stop) -> "done" | "skip" | "gone" | "stopped"
    (raises Backoff); busy_fn() -> downloads are running."""

    def __init__(self, path, busy_fn, variants_fn, run_fn, log=print, clock=time.time):
        self.path = path
        self.busy, self.variants_fn, self.run_fn = busy_fn, variants_fn, run_fn
        self.log, self.clock = log, clock
        self.lock = threading.RLock()
        self.wake = threading.Event()
        self.jobs = {}                  # "root\npath" -> job
        self.finished = 0               # since start
        self.current = None
        try:
            with open(path, encoding="utf-8") as fh:
                for job in (json.load(fh) or {}).get("jobs") or []:
                    if isinstance(job, dict) and job.get("root") is not None and job.get("path"):
                        if job.get("state") == "running":
                            job["state"] = "queued"
                        job.setdefault("variants", [])
                        job.setdefault("old_keys", [])
                        self.jobs[self._key(job["root"], job["path"])] = job
        except (OSError, ValueError):
            pass

    @staticmethod
    def _key(root, path):
        return "%s\n%s" % (root, path)

    def save(self):
        with self.lock:
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump({"version": 1, "jobs": list(self.jobs.values())}, fh, ensure_ascii=False)
            os.replace(tmp, self.path)

    # ---- what the cut callback does
    def schedule(self, root, path, old_key, variants=None):
        """A cut changed (root, path). `variants` = what the worker held for the old key; None = it
        could not be asked, so old_key is kept and asked again later. An empty list = it held nothing
        (a brand-new download): nothing is added. -> the job, or None when there is nothing to redo."""
        with self.lock:
            key = self._key(root, path)
            job = self.jobs.get(key)
            if variants is not None and not variants and job is None:
                return None
            if job is None:
                job = {"root": root, "path": path, "variants": [], "old_keys": [], "state": "queued",
                       "attempts": 0, "not_before": 0, "error": None, "at": self.clock()}
                self.jobs[key] = job
            if variants is None:
                if old_key not in job["old_keys"]:
                    job["old_keys"].append(old_key)
            else:
                job["variants"] = merge_variants(job["variants"], variants)
            job["rev"] = int(job.get("rev") or 0) + 1       # a running pass sees it was replaced
            if job["state"] == "failed":
                job.update(state="queued", attempts=0, not_before=0, error=None)
            self.save()
        self.wake.set()
        return job

    # ---- the worker thread
    def _next(self, now):
        with self.lock:
            ready = [j for j in self.jobs.values()
                     if j["state"] == "queued" and float(j.get("not_before") or 0) <= now]
            return min(ready, key=lambda j: j.get("at") or 0) if ready else None

    def _backoff(self, job, why):
        with self.lock:
            job["attempts"] = int(job.get("attempts") or 0) + 1
            job["error"] = str(why)[:200]
            if job["attempts"] >= MAX_ATTEMPTS:
                job["state"] = "failed"
                self.log("[interp-redo] %s/%s: given up after %d tries: %s"
                         % (job["root"], job["path"], job["attempts"], why))
            else:
                job["state"] = "queued"
                job["not_before"] = self.clock() + min(BACKOFF_MAX_S, BACKOFF_MIN_S * 2 ** (job["attempts"] - 1))
            self.save()

    def run_job(self, job):
        """One pass over a job. Returns when it is finished, replaced, stopped for a download, or
        backed off."""
        root, path = job["root"], job["path"]
        key = self._key(root, path)
        rev = job.get("rev")
        with self.lock:
            job["state"] = "running"
            self.current = "%s/%s" % (root, path)
            self.save()

        def should_stop():
            return self.jobs.get(key) is not job or job.get("rev") != rev or bool(self.busy())

        try:
            for old_key in list(job["old_keys"]):
                found = self.variants_fn(old_key)
                with self.lock:
                    job["variants"] = merge_variants(job["variants"], found)
                    if old_key in job["old_keys"]:
                        job["old_keys"].remove(old_key)
                    self.save()
            for spec in list(job["variants"]):
                if should_stop():
                    break
                result = self.run_fn(root, path, spec, should_stop)
                if result == "gone":
                    with self.lock:
                        self.jobs.pop(key, None)
                        self.save()
                    return
                if result == "stopped":
                    break
                with self.lock:
                    if spec in job["variants"]:
                        job["variants"].remove(spec)
                    self.save()
                self.log("[interp-redo] %s/%s: %s %s" % (root, path, spec["variant"], result))
        except Backoff as exc:
            self._backoff(job, exc)
            return
        except Exception as exc:
            self._backoff(job, "%s: %s" % (type(exc).__name__, exc))
            return
        finally:
            self.current = None
        with self.lock:
            if self.jobs.get(key) is not job:
                return                      # replaced: the newer job runs on its own
            if job.get("rev") != rev:
                job["state"] = "queued"     # a newer cut merged in while this ran
            elif not job["variants"] and not job["old_keys"]:
                self.jobs.pop(key, None)
                self.finished += 1
            else:
                job.update(state="queued", not_before=self.clock() + BUSY_WAIT_S)
            self.save()

    def step(self):
        """One loop turn (also what the harness calls). -> True when a job ran."""
        if self.busy():
            return False
        job = self._next(self.clock())
        if job is None:
            return False
        self.run_job(job)
        return True

    def run_forever(self):
        while True:
            try:
                if self.busy():
                    time.sleep(BUSY_WAIT_S)
                    continue
                if not self.step():
                    self.wake.wait(IDLE_WAIT_S)
                    self.wake.clear()
            except Exception as exc:
                self.log("[interp-redo] worker error: %s: %s" % (type(exc).__name__, exc))
                time.sleep(30)

    def start(self):
        thread = threading.Thread(target=self.run_forever, name="interp-redo", daemon=True)
        thread.start()
        return thread

    def status(self):
        with self.lock:
            jobs = list(self.jobs.values())
            counts = {"queued": 0, "running": 0, "failed": 0}
            for j in jobs:
                counts[j["state"]] = counts.get(j["state"], 0) + 1
            return dict(counts, finished=self.finished, current=self.current,
                        jobs=[{"root": j["root"], "path": j["path"], "state": j["state"],
                               "variants": len(j["variants"]), "lookups": len(j["old_keys"]),
                               "attempts": j.get("attempts", 0), "error": j.get("error")}
                              for j in jobs[:20]])
