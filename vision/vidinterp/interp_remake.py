#!/usr/bin/env python3
"""Throw away the Smooth motion segments of some sources on the vision worker and make them again.

Run ON the worker host, stdlib only:
  python3 interp_remake.py keys.txt [--state remake_state.json] [--stall-s 600]

keys.txt: one interp source key (the 32 hex digits of <interp-cache>/<key>.src) per line.
For each key, in order:
  1. the variants the cache holds now (GET /v1/interp/variants/<key>) are written to the state file
     BEFORE anything is deleted - this is what makes the script resumable;
  2. every <interp-cache>/<key>/<variant>/ directory is removed (the .src upload stays);
  3. every variant is made again the way media-dl's redo does (interp_redo.py): a complete variant (it
     had an end.json) in full, a partial one the same number of leading segments - by asking the worker
     for meta.json?from=0, init.mp4 and seg_NNNNN.m4s one after the other, waiting out "still being made".
It is the worker's normal segment path, so the dedup binary (vidinterp-gpu --dedup) makes them and a
running 3D conversion steps aside as it does for a viewer. A variant that stalls (no new segment for
--stall-s) is recorded as "failed" and left for a second run (a state entry with stage != "done" is
picked up again; the variants are NOT deleted a second time).

Why: 2026-11-11, vidinterp-gpu --dedup. Segments made before it interpolated between identical
pictures of a source with held frames (animation on twos / threes) and kept its stutter.
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request

CONFIG = "/data/ksf-vision/vision_config.json"


def log(msg):
    print(time.strftime("%H:%M:%S ") + msg, flush=True)


class Worker:
    def __init__(self, base, token):
        self.base, self.token = base.rstrip("/"), token

    def get(self, path, timeout=60):
        req = urllib.request.Request(self.base + path)
        if self.token:
            req.add_header("Authorization", "Bearer " + self.token)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read()

    def wait(self, path, stall_s):
        """GET that waits out 503 'still being made' for up to stall_s. Raises HTTPError otherwise."""
        deadline = time.time() + stall_s
        while True:
            try:
                return self.get(path, timeout=60)
            except urllib.error.HTTPError as exc:
                if exc.code == 503 and time.time() < deadline:
                    time.sleep(1.0)
                    continue
                raise


def viewer_active(cache, key, quiet_s):
    """A person may be watching this video: its cache directory (or its .src) was touched within
    quiet_s, or a vidinterp-gpu is running on it. Deleting under a viewer makes the player and this
    script fight over the worker's one job (seen 2026-11-11: hung job, wrong segments)."""
    now = time.time()
    newest = 0.0
    base = os.path.join(cache, key)
    for dp, _dn, fn in os.walk(base):
        for f in fn:
            try:
                newest = max(newest, os.stat(os.path.join(dp, f)).st_mtime)
            except OSError:
                pass
    try:
        newest = max(newest, os.stat(base + ".src").st_atime)
    except OSError:
        pass
    if now - newest < quiet_s:
        return True
    return subprocess.run(["pgrep", "-f", f"vidinterp-gpu.*{key}"], capture_output=True).returncode == 0


def make_variant(w, key, variant, complete, limit, stall_s):
    """-> number of segments fetched. Raises on a stall or a refusal."""
    base = f"/v1/interp/seg/{key}/{variant}"
    total = int(json.loads(w.wait(f"{base}/meta.json?from=0", stall_s))["segments"])
    w.wait(f"{base}/init.mp4", stall_s)
    n, exact, i = total, False, 0
    while True:
        if not complete and i >= limit:
            return i
        if i >= n:
            if exact:
                return i
            try:
                raw = w.wait(f"{base}/end.json?from={i}", 60)
                n, exact = int(json.loads(raw)["last_segment"]) + 1, True
            except urllib.error.HTTPError:
                raise RuntimeError(f"no end.json after segment {i}")
            continue
        try:
            w.wait(f"{base}/seg_{i:05d}.m4s", stall_s)
        except urllib.error.HTTPError as exc:
            if exc.code == 404 and not exact:
                n = i                       # past the estimate's real end: settle it with end.json
                continue
            raise
        i += 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("keys")
    ap.add_argument("--state", default="/data/ksf-vision/interp-remake-state.json")
    ap.add_argument("--base", default="http://127.0.0.1:8410")
    ap.add_argument("--cache", default="/data/ksf-vision/interp-cache")
    ap.add_argument("--stall-s", type=float, default=300.0)
    ap.add_argument("--only-delete", action="store_true", help="delete only (no remake)")
    ap.add_argument("--quiet-s", type=float, default=600.0, help="skip a key touched this recently (a viewer)")
    ap.add_argument("--passes", type=int, default=12, help="how often to come back to skipped/failed keys")
    ap.add_argument("--pass-wait-s", type=float, default=300.0)
    a = ap.parse_args()
    token = json.load(open(CONFIG)).get("token") or ""
    w = Worker(a.base, token)
    keys = [k.strip() for k in open(a.keys) if k.strip()]
    state = json.load(open(a.state)) if os.path.isfile(a.state) else {}

    def save():
        tmp = a.state + ".tmp"
        json.dump(state, open(tmp, "w"), indent=1)
        os.replace(tmp, a.state)

    done = failed = 0
    for attempt in range(1, a.passes + 1):
        done, failed, skipped = run_pass(a, w, keys, state, save, attempt)
        if not failed and not skipped:
            break
        log(f"pass {attempt}: {done} made, {failed} failed, {skipped} skipped (viewer / busy)")
        time.sleep(a.pass_wait_s)
    log(f"finished: {sum(1 for v in state.values() if v.get('stage') == 'done')} of {len(keys)} done")
    return 0


def run_pass(a, w, keys, state, save, attempt):
    done = failed = skipped = 0
    for n, key in enumerate(keys, 1):
        st = state.get(key)
        if st and st.get("stage") == "done":
            continue
        if not os.path.isfile(os.path.join(a.cache, key + ".src")):
            state[key] = {"stage": "nosrc"}
            save()
            log(f"[{n}/{len(keys)}] {key}: no .src on the worker - skipped")
            continue
        if st is None:
            variants = json.loads(w.get(f"/v1/interp/variants/{key}"))
            st = state[key] = {"stage": "fetched", "variants": variants}
            save()
        if st["stage"] == "fetched":
            if viewer_active(a.cache, key, a.quiet_s):
                log(f"[{n}/{len(keys)}] {key}: in use (touched within {a.quiet_s:.0f}s) - later")
                skipped += 1
                continue
            for v in st["variants"]:
                shutil.rmtree(os.path.join(a.cache, key, v["variant"]), ignore_errors=True)
            st["stage"] = "deleted"
            save()
        if a.only_delete or not st["variants"]:
            continue
        ok = True
        t0 = time.time()
        for v in st["variants"]:
            if v.get("made"):
                continue
            v.pop("error", None)
            try:
                segs = make_variant(w, key, v["variant"], v["complete"], int(v["segments"]), a.stall_s)
                v["made"] = segs
            except (urllib.error.URLError, RuntimeError, ValueError, KeyError, OSError) as exc:
                v["error"] = f"{type(exc).__name__}: {exc}"
                ok = False
                log(f"[{n}/{len(keys)}] {key} {v['variant']}: FAILED {v['error']}")
                save()
                break
            save()
        if ok:
            st["stage"] = "done"
            done += 1
            log(f"[{n}/{len(keys)}] {key}: {len(st['variants'])} variants made in {time.time() - t0:.0f}s")
        else:
            failed += 1
        save()
    return done, failed, skipped


if __name__ == "__main__":
    sys.exit(main())
