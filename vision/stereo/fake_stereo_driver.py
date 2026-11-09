#!/usr/bin/env python3
"""Stand-in for stereo_job.py in stereo_worker_check.py: the same CLI, work-dir files, progress.json format and
exit codes (0 done, 1 failed, 75 SIGTERM -> resume me), but no GPU and no media - every chunk is a sleep.

Behaviour comes from the JSON file named by $FAKE_DRIVER_CTL: {"default": {...}, "<work dir name>": {...}} with
  chunks (6), chunk_s (0.25), fail_at {"<chunk, 1-based>": times it fails in total}, ignore_term, no_outputs,
  out_bytes (3000), start_delay (0).
Resumes like the real driver: finished chunks are kept in fake_state.json.  fake_log.txt records every chunk it
starts/finishes (the check counts lost work from it), fake_args.json the argv and the environment it was given.
"""
import argparse
import json
import os
import signal
import sys
import time

STOP = False


def on_term(signum, frame):
    global STOP
    STOP = True


def wjson(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f)
    os.replace(tmp, path)


def rjson(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--work", required=True)
    r.add_argument("--src", required=True)
    r.add_argument("--kind", required=True, choices=["video", "picture"])
    r.add_argument("--profile")
    r.add_argument("--keep-chunks", action="store_true")
    a = ap.parse_args()
    work = os.path.abspath(a.work)
    key = os.path.basename(work)
    allctl = rjson(os.environ.get("FAKE_DRIVER_CTL", ""), {})
    ctl = dict(allctl.get("default", {}))
    ctl.update(allctl.get(key, {}))
    signal.signal(signal.SIGTERM, signal.SIG_IGN if ctl.get("ignore_term") else on_term)

    log = open(work + "/fake_log.txt", "a")

    def note(msg):
        log.write(f"{time.time():.3f} {msg}\n")
        log.flush()

    wjson(work + "/fake_args.json", {"argv": sys.argv, "src_exists": os.path.exists(a.src),
                                     "src_is_dir": os.path.isdir(a.src), "cwd": os.getcwd(),
                                     "profile": json.loads(a.profile) if a.profile else None,
                                     "env": {k: os.environ.get(k) for k in
                                             ("LD_LIBRARY_PATH", "HF_HOME", "INSIGHTFACE_HOME", "KSF_VISION_PORT")}})
    state = rjson(work + "/fake_state.json", {"done": 0, "fails": {}})
    chunk_s = float(ctl.get("chunk_s", 0.25))
    out_bytes = int(ctl.get("out_bytes", 3000))
    t_start = time.time()

    def progress(phase, **kw):
        d = {"phase": phase, "updated": int(time.time())}
        d.update(kw)
        wjson(work + "/progress.json", d)

    def wait(secs):
        end = time.time() + secs
        while time.time() < end:
            if STOP:
                return False
            time.sleep(0.02)
        return not STOP

    def interrupted(where):
        note(f"term at {where}")
        wjson(work + "/job.json", {"state": "interrupted"})
        sys.exit(75)

    def blob(n):
        return b"\x00\x00\x00\x20ftypisom" + bytes((i * 7) % 251 for i in range(max(0, n - 12)))

    wjson(work + "/job.json", {"state": "running", "kind": a.kind})
    note("start")
    if a.kind == "video":
        n = int(ctl.get("chunks", 6))
        progress("starting", frames_total=n * 100)
        if not wait(float(ctl.get("start_delay", 0))):
            interrupted("start")
        for i in range(state["done"], n):
            progress("converting", chunk=i + 1, chunks=n, frames_done=i * 100, frames_total=n * 100,
                     recent_fps=100.0 / max(chunk_s, 0.01), eta_s=int((n - i) * chunk_s))
            note(f"start chunk {i + 1}")
            if not wait(chunk_s):
                interrupted(f"chunk {i + 1}")
            want = int(ctl.get("fail_at", {}).get(str(i + 1), 0))
            if state["fails"].get(str(i + 1), 0) < want:
                state["fails"][str(i + 1)] = state["fails"].get(str(i + 1), 0) + 1
                wjson(work + "/fake_state.json", state)
                note(f"fail chunk {i + 1}")
                wjson(work + "/job.json", {"state": "failed", "error": f"fake failure at chunk {i + 1}"})
                return 1
            state["done"] = i + 1
            wjson(work + "/fake_state.json", state)
            note(f"done chunk {i + 1}")
        progress("joining", chunk=n, chunks=n, frames_done=n * 100, frames_total=n * 100)
        if not ctl.get("no_outputs"):
            for name in ("sbs.mp4", "depth.mp4"):
                with open(work + "/" + name, "wb") as f:
                    f.write(blob(out_bytes))
            wjson(work + "/meta.json", {"kind": "video", "eye_w": 1920, "eye_h": 1080, "fps": 120.0,
                                        "duration": n * 20.0, "frames": n * 100, "chunks": n})
        progress("done", frames_done=n * 100, frames_total=n * 100)
    else:
        if os.path.isdir(a.src):
            files = sorted(os.listdir(a.src))
            outs = [(f, work + "/out/" + os.path.splitext(f)[0]) for f in files]
        else:
            outs = [(os.path.basename(a.src), work)]
        for f, d in outs:
            os.makedirs(d, exist_ok=True)
            note(f"start picture {f}")
            if not wait(chunk_s):
                interrupted(f"picture {f}")
            if os.path.exists(d + "/meta.json"):
                continue
            if not ctl.get("no_outputs"):
                with open(d + "/sbs.jpg", "wb") as fh:
                    fh.write(b"\xff\xd8\xff\xe0" + bytes(out_bytes))
                with open(d + "/depth.png", "wb") as fh:
                    fh.write(b"\x89PNG\r\n\x1a\n" + bytes(out_bytes))
                wjson(d + "/meta.json", {"kind": "picture", "src": f, "eye_w": 100, "eye_h": 100})
            note(f"done picture {f}")
    wjson(work + "/job.json", {"state": "done"})
    note(f"exit 0 after {time.time() - t_start:.2f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
