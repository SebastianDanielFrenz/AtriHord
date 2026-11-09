#!/usr/bin/env python3
"""2D -> stereo 3D worker pipeline (media-dl 3D backend, phase P1).

    stereo_job.py run --work DIR --src FILE --kind video|picture [--profile JSON|FILE] [--keep-chunks]
    stereo_job.py probe FILE

Run it with the stereo venv (/data/ksf-vision/stereo/.venv/bin/python).  Re-running `run`
on the same --work DIR resumes: finished phases and finished 20 s chunks are kept.
SIGTERM stops cleanly (the chunk in flight is discarded, exit code 75 = "interrupted, resume me").

Video: probe -> plan -> vidinterp-gpu to exactly 120 fps (skipped for >= 119 fps sources)
       -> stream-copy split into chunks + audio.mka -> ONE iw3 process, Any_V2_S loaded once,
       iw3.utils.process_video_full per chunk with runtime patches (nunif is never edited):
         a. make_video_codec_option: capped hevc_nvenc
         b. apply_divergence wrapper: the same depth tensor goes into a 10-bit HEVC depth video
         c. progress.json via the tqdm shim
         d. SIGTERM -> iw3's stop_event
       -> concat + audio + faststart -> sbs.mp4, depth.mp4, meta.json.
Pictures: Distill_Any_L loaded once, process_image per file -> sbs.jpg (q95) + depth.png (16 bit).
"""
import argparse
import glob
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from fractions import Fraction

ROOT = "/data/ksf-vision/stereo"
NUNIF = ROOT + "/nunif"
PY = ROOT + "/.venv/bin/python"
FFBIN = "/data/ksf-vision/ffmpeg/ffmpeg-master-latest-linux64-gpl/bin"
FFMPEG, FFPROBE = FFBIN + "/ffmpeg", FFBIN + "/ffprobe"
VIDINTERP = "/data/ksf-vision/vidinterp/bin/vidinterp-gpu"
LSFG_DLL = "/data/ksf-vision/vidinterp/lsfg-vk.dll"
COMMIT_PINNED = "d23721f1b5f0a4c92c3ee1be013180bf298730c5"

os.environ.setdefault("HF_HOME", ROOT + "/cache/hf")
os.environ.setdefault("TORCH_HOME", ROOT + "/cache/torch")
os.environ.setdefault("PYTHONUNBUFFERED", "1")

# Signatures of every iw3/nunif function we call or patch, at COMMIT_PINNED.
EXPECTED_SIGNATURES = {
    ("iw3.utils", "apply_divergence"): "(depth, im, args, side_model, reset_pts=None)",
    ("iw3.utils", "make_video_codec_option"): "(args, input_path=None)",
    ("iw3.utils", "process_video_full"): "(input_filename, output_path, args, depth_model, side_model)",
    ("iw3.utils", "process_image"): "(x, args, depth_model, side_model, skip_autocrop=None, autocrop_uncrop=False)",
    ("iw3.utils", "set_state_args"): "(args, stop_event=None, tqdm_fn=None, depth_model=None, suspend_event=None)",
    ("iw3.utils", "calc_auto_warp_steps"): "(method, divergence, synthetic_view)",
    ("iw3.stereo_model_factory", "create_stereo_model"):
        "(method, divergence, device_id, use_weak_convergence_model=False, inpaint_model=None)",
    ("nunif.utils.video", "process_video"):
        "(input_path, output_path, frame_callback, config_callback=<function default_config_callback>, "
        "title=None, vf='', stop_event=None, suspend_event=None, tqdm_fn=None, start_time=None, "
        "end_time=None, test_callback=None, device='cpu')",
}

DEFAULT_PROFILE = {
    "depth_model": "Any_V2_S",
    "picture_model": "Distill_Any_L",
    "method": "row_flow_v3",
    "divergence": 2.0,
    "convergence": 0.5,
    "fps": 120,
    "max_eye_long_side": 2160,
    "chunk_s": 20,
    "cq": 22,                      # hevc_nvenc vbr constant-quality target, capped by the bitrate below
    "nvenc_preset": "p5",
    # Bitrate cap of the whole SBS stream by EYE pixel count (piecewise linear between the anchors,
    # proportional below the first, flat above the last).  Anchors = 30/50/80 Mbit/s for 1080p/1440p/2160p eyes.
    "bitrate_anchors": [[1920 * 1080, 30.0], [2560 * 1440, 50.0], [3840 * 2160, 80.0]],
    "interp_quality": 18,
    "depth_cq": 16,
    "depth_maxrate_mbps": 15.0,
    "depth_scale": 0.5,            # depth video size = eye size * this
    "picture_jpeg_quality": 95,
    # Degrees clockwise (0/90/180/270) that make a video FILMED ON ITS SIDE upright, decided by media-dl
    # (no container flag says so).  Added to the container rotation before depth estimation.  Videos only.
    "turn_cw": 0,
}
RC_INTERRUPTED = 75


class Interrupted(Exception):
    pass


STOP = threading.Event()
CHILD = [None]


def _on_term(signum, frame):
    STOP.set()
    p = CHILD[0]
    if p is not None and p.poll() is None:
        try:
            p.terminate()
        except OSError:
            pass


# ------------------------------------------------------------------ small helpers
LOGF = [None]


def log(*a):
    line = time.strftime("%H:%M:%S ") + " ".join(str(x) for x in a)
    print(line, flush=True)
    if LOGF[0]:
        LOGF[0].write(line + "\n")
        LOGF[0].flush()


def write_json(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f, indent=1)
    os.replace(tmp, path)


def read_json(path, default=None):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def run_cmd(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def even(x):
    return int(round(x / 2.0)) * 2


def frac(s):
    try:
        a, b = str(s).split("/")
        return float(a) / float(b) if float(b) else 0.0
    except Exception:
        return 0.0


def count_frames(path):
    r = run_cmd([FFPROBE, "-v", "error", "-select_streams", "v:0", "-count_packets",
                 "-show_entries", "stream=nb_read_packets", "-of", "csv=p=0", path])
    try:
        return int(r.stdout.strip().split("\n")[0].strip(","))
    except ValueError:
        raise RuntimeError(f"cannot count frames of {path}: {r.stderr[-200:]}")


def probe_duration(path):
    r = run_cmd([FFPROBE, "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", path])
    try:
        return float(r.stdout.strip())
    except ValueError:
        return 0.0


def merge_profile(spec):
    p = json.loads(json.dumps(DEFAULT_PROFILE))
    if spec:
        extra = json.loads(spec) if spec.strip().startswith("{") else read_json(spec, {})
        p.update(extra)
    return p


def profile_id(p):
    return hashlib.sha1(json.dumps(p, sort_keys=True).encode()).hexdigest()[:12]


def bitrate_cap_mbps(profile, eye_w, eye_h):
    px = eye_w * eye_h
    anchors = sorted((float(a), float(b)) for a, b in profile["bitrate_anchors"])
    if px <= anchors[0][0]:
        return max(6.0, anchors[0][1] * px / anchors[0][0])
    for (p0, b0), (p1, b1) in zip(anchors, anchors[1:]):
        if px <= p1:
            return b0 + (b1 - b0) * (px - p0) / (p1 - p0)
    return anchors[-1][1]


# ------------------------------------------------------------------ probe
def probe(path):
    r = run_cmd([FFPROBE, "-v", "error", "-show_streams", "-show_format", "-of", "json", path])
    if r.returncode != 0:
        raise RuntimeError("ffprobe failed: " + r.stderr[-300:])
    j = json.loads(r.stdout)
    vs = [s for s in j["streams"] if s["codec_type"] == "video" and s.get("disposition", {}).get("attached_pic") != 1]
    if not vs:
        raise RuntimeError("no video stream")
    v = vs[0]
    fmt = j["format"]
    avg, rfr = frac(v.get("avg_frame_rate")), frac(v.get("r_frame_rate"))
    fps = avg or rfr
    tags = {k.lower(): val for k, val in (v.get("tags") or {}).items()}
    rot_src, rotation_cw = "none", 0
    for sd in v.get("side_data_list", []) or []:
        if "rotation" in sd:
            rotation_cw = int(round(-float(sd["rotation"]))) % 360   # ffmpeg: rotation is counter-clockwise
            rot_src = "display_matrix(rotation=%s)" % sd["rotation"]
    if rot_src == "none" and "rotate" in tags:
        try:
            rotation_cw = int(float(tags["rotate"])) % 360             # legacy tag: degrees clockwise
            rot_src = "tag rotate=%s" % tags["rotate"]
        except ValueError:
            pass
    trc = v.get("color_transfer")
    dur = float(fmt.get("duration") or v.get("duration") or 0)
    # The container duration can be shorter than the video (cut with -t on a keyframe boundary: a_anime has 60.0 s
    # container, 60.4 s of video).  Count the video packets (no decoding) and use that for every expectation.
    vframes = count_frames(path)
    vdur = vframes / fps if fps else dur
    audio = [{"codec": s.get("codec_name"), "channels": s.get("channels"), "rate": s.get("sample_rate")}
             for s in j["streams"] if s["codec_type"] == "audio"]
    return {
        "w": int(v["width"]), "h": int(v["height"]), "fps": fps, "avg_fps": avg, "r_fps": rfr,
        "vfr": bool(avg and rfr and abs(avg - rfr) / rfr > 0.01),
        "duration": dur, "video_frames": vframes, "video_duration": vdur, "frames_est": vframes,
        "codec": v.get("codec_name"), "pix_fmt": v.get("pix_fmt"),
        "color_transfer": trc, "color_primaries": v.get("color_primaries"),
        "hdr": trc in ("smpte2084", "arib-std-b67"),
        "rotation_cw": rotation_cw, "rotation_source": rot_src,
        "audio": audio, "format_bytes": int(fmt.get("size") or 0),
    }


def make_plan(pr, profile):
    turn = profile.get("turn_cw") or 0
    if turn not in (0, 90, 180, 270):
        raise RuntimeError("turn_cw must be 0, 90, 180 or 270, not %r" % (turn,))
    cw = (pr["rotation_cw"] + turn) % 360                    # container flag + media-dl's turn
    sw, sh = pr["w"], pr["h"]
    dw, dh = (sh, sw) if cw in (90, 270) else (sw, sh)       # upright size
    cap = int(profile["max_eye_long_side"])
    scale = min(1.0, cap / float(max(dw, dh)))
    if scale < 1.0:
        # same formulas as vidinterp-gpu's --max-height (stored orientation) and iw3's --max-output-height (upright)
        stored_h = even(sh * scale)
        stored_w = ((sw * stored_h // sh) + 1) // 2 * 2
        while max(stored_w, stored_h) > cap and stored_h > 2:   # vidinterp rounds the width up to even: 2162 > 2160
            stored_h -= 2
            stored_w = ((sw * stored_h // sh) + 1) // 2 * 2
        eye_w, eye_h = (stored_h, stored_w) if cw in (90, 270) else (stored_w, stored_h)
    else:
        stored_h = sh
        eye_w, eye_h = dw - dw % 2, dh - dh % 2
    fps = pr["fps"]
    target = float(profile["fps"])
    interpolate = fps < target - 1.0
    out_fps = target if (interpolate or fps > target + 1.0) else fps
    cap_mbps = bitrate_cap_mbps(profile, eye_w, eye_h)
    return {
        "eye_w": eye_w, "eye_h": eye_h, "scale": scale, "stored_max_height": stored_h if scale < 1.0 else 0,
        "rotation_cw": cw, "interpolate": interpolate, "out_fps": out_fps,
        "expected_frames": int(round(pr.get("video_duration", pr["duration"]) * out_fps)),
        "bitrate_cap_mbps": round(cap_mbps, 2), "hdr": pr["hdr"],
        "audio": bool(pr["audio"]),
    }


# ------------------------------------------------------------------ GPU sampling
class GpuSampler(threading.Thread):
    """Samples nvidia-smi: peak VRAM of the given pid set and of the whole card."""

    def __init__(self, pids_fn):
        super().__init__(daemon=True)
        self.pids_fn = pids_fn
        self.peak_proc = 0
        self.peak_total = 0
        self.base_total = None
        self._halt = threading.Event()

    def run(self):
        while not self._halt.is_set():
            try:
                t = run_cmd(["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"])
                tot = int(t.stdout.strip().split("\n")[0])
                if self.base_total is None:
                    self.base_total = tot
                self.peak_total = max(self.peak_total, tot)
                a = run_cmd(["nvidia-smi", "--query-compute-apps=pid,used_memory", "--format=csv,noheader,nounits"])
                pids = set(self.pids_fn())
                s = 0
                for ln in a.stdout.strip().split("\n"):
                    if "," in ln:
                        p, m = ln.split(",")
                        if int(p) in pids:
                            s += int(m)
                self.peak_proc = max(self.peak_proc, s)
            except Exception:
                pass
            self._halt.wait(0.7)

    def finish(self):
        self._halt.set()
        self.join(timeout=3)
        return {"proc_peak_mb": self.peak_proc, "card_peak_mb": self.peak_total, "card_base_mb": self.base_total}


def child_tree_pids(pid):
    out = [pid]
    try:
        r = run_cmd(["pgrep", "-P", str(pid)])
        for ln in r.stdout.split():
            out += child_tree_pids(int(ln))
    except Exception:
        pass
    return out


# ------------------------------------------------------------------ job state
class Job:
    def __init__(self, work):
        self.work = work
        self.path = work + "/job.json"
        self.d = read_json(self.path, {})

    def save(self):
        write_json(self.path, self.d)

    def __getitem__(self, k):
        return self.d[k]

    def get(self, k, default=None):
        return self.d.get(k, default)

    def set(self, **kw):
        self.d.update(kw)
        self.save()

    def add_seconds(self, key, secs):
        self.d.setdefault("seconds", {})
        self.d["seconds"][key] = round(self.d["seconds"].get(key, 0.0) + secs, 3)
        self.save()

    def note_vram(self, phase, info):
        v = self.d.setdefault("vram", {})
        old = v.get(phase, {})
        v[phase] = {k: max(old.get(k) or 0, info.get(k) or 0) for k in ("proc_peak_mb", "card_peak_mb")}
        v[phase]["card_base_mb"] = info.get("card_base_mb")
        self.save()


class Progress:
    def __init__(self, work, total_frames):
        self.path = work + "/progress.json"
        self.total = total_frames
        self.t0 = time.time()
        self.last_write = 0
        self.hist = []
        self.state = {"phase": "starting"}

    def update(self, force=False, **kw):
        self.state.update(kw)
        now = time.time()
        if not force and now - self.last_write < 2.0:
            return
        self.last_write = now
        fd = self.state.get("frames_done")
        if fd is not None:
            self.hist.append((now, fd))
            self.hist = [h for h in self.hist if now - h[0] <= 30]
            if len(self.hist) >= 2 and self.hist[-1][0] > self.hist[0][0]:
                fps = (self.hist[-1][1] - self.hist[0][1]) / (self.hist[-1][0] - self.hist[0][0])
                self.state["recent_fps"] = round(fps, 2)
                if fps > 0 and self.total:
                    self.state["eta_s"] = int(max(0, self.total - fd) / fps)
        self.state["frames_total"] = self.total
        self.state["updated"] = int(now)
        write_json(self.path, self.state)


# ------------------------------------------------------------------ video phases
def run_logged(cmd, tag, progress_re=None, on_line=None):
    """Runs a child, mirrors stderr to the job log, honours SIGTERM (kills child, raises Interrupted)."""
    p = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, bufsize=1)
    CHILD[0] = p
    tail = []
    last_log = 0
    for line in p.stderr:
        line = line.rstrip()
        tail = (tail + [line])[-20:]
        if on_line:
            on_line(line)
        if time.time() - last_log > 30 or "done" in line or "rror" in line:
            log(f"[{tag}] {line}")
            last_log = time.time()
    rc = p.wait()
    CHILD[0] = None
    if STOP.is_set():
        raise Interrupted()
    if rc != 0:
        raise RuntimeError(f"{tag} failed rc={rc}: " + " | ".join(tail[-5:]))


def phase_interp(job, prog, src, plan, profile):
    w = job.work
    final, part, done = w + "/interp.mkv", w + "/interp.part.mkv", w + "/interp.done"
    if os.path.exists(done) and os.path.exists(final):
        return final
    for f in (final, part):
        if os.path.exists(f):
            os.remove(f)
    cmd = [VIDINTERP, "-i", src, "-o", part, "--fps", str(int(profile["fps"])), "--encoder", "hevc_nvenc",
           "--quality", str(profile["interp_quality"]), "-d", LSFG_DLL]
    if plan["stored_max_height"]:
        cmd += ["--max-height", str(plan["stored_max_height"])]
    log("interpolate:", " ".join(cmd))
    prog.update(force=True, phase="interpolating", frames_done=0)
    sampler = GpuSampler(lambda: [CHILD[0].pid] + child_tree_pids(CHILD[0].pid) if CHILD[0] else [])
    sampler.start()
    t0 = time.time()
    rx = re.compile(r"in (\d+), out (\d+)")

    def on_line(line):
        m = rx.search(line)
        if m:
            prog.update(frames_done=int(m.group(2)))
    try:
        run_logged(cmd, "vidinterp", on_line=on_line)
    finally:
        job.note_vram("interp", sampler.finish())
        job.add_seconds("interp", time.time() - t0)
        if STOP.is_set() and os.path.exists(part):
            os.remove(part)
    os.replace(part, final)
    open(done, "w").close()
    return final


def phase_split(job, prog, split_src, plan, profile, src):
    w = job.work
    cj = w + "/chunks.json"
    if os.path.exists(cj):
        return read_json(cj)
    cdir = w + "/chunks"
    shutil.rmtree(cdir, ignore_errors=True)
    os.makedirs(cdir)
    prog.update(force=True, phase="splitting")
    cmd = [FFMPEG, "-v", "error", "-y", "-i", split_src, "-map", "0:v:0", "-c", "copy", "-f", "segment",
           "-segment_time", str(profile["chunk_s"]), "-reset_timestamps", "1", "-segment_format", "matroska",
           cdir + "/in_%05d.mkv"]
    run_logged(cmd, "split")
    files = sorted(glob.glob(cdir + "/in_*.mkv"))
    chunks = [{"name": os.path.basename(f)[3:-4], "frames": count_frames(f), "duration": probe_duration(f)} for f in files]
    if not chunks:
        raise RuntimeError("split produced no chunks")
    total = sum(c["frames"] for c in chunks)
    exp = plan["expected_frames"]
    log(f"split: {len(chunks)} chunks, {total} frames (expected ~{exp})")
    if abs(total - exp) > max(3, plan["out_fps"] * 0.1):
        raise RuntimeError(f"split frame count {total} far from expected {exp}")
    if job.get("probe")["audio"] and not os.path.exists(w + "/audio.mka"):
        r = run_cmd([FFMPEG, "-v", "error", "-y", "-i", src, "-map", "0:a:0", "-vn", "-c", "copy",
                     w + "/audio.mka.part.mka"])
        if r.returncode != 0:
            raise RuntimeError("audio extract failed: " + r.stderr[-300:])
        os.replace(w + "/audio.mka.part.mka", w + "/audio.mka")
    write_json(cj, {"chunks": chunks, "total_frames": total})
    if split_src.endswith("interp.mkv"):
        os.remove(split_src)        # frame counts add up, the chunks are the only copy now
    return read_json(cj)


def check_signatures():
    import importlib
    import inspect
    bad = []
    for (mod, fn), want in EXPECTED_SIGNATURES.items():
        m = importlib.import_module(mod)
        got = re.sub(r" at 0x[0-9a-f]+", "", str(inspect.signature(getattr(m, fn))))
        if got != want:
            bad.append(f"{mod}.{fn}: expected {want} got {got}")
    pin = open(NUNIF + "/COMMIT_PINNED").read().split()[0]
    if pin != COMMIT_PINNED:
        bad.append(f"nunif COMMIT_PINNED {pin} != {COMMIT_PINNED}")
    if bad:
        raise RuntimeError("iw3 patch points differ from the pinned commit - refusing to run:\n  " + "\n  ".join(bad))


class DepthVideoWriter:
    """10-bit HEVC (p010le, limited range) gray depth video written by PyAV hevc_nvenc."""

    def __init__(self, path, fps, w, h, cq, maxrate_mbps):
        import av
        self.av = av
        self.w, self.h = w, h
        self.c = av.open(path, "w", options={"movflags": "+faststart"})
        self.s = self.c.add_stream("hevc_nvenc", Fraction(fps).limit_denominator(1001))
        self.s.width, self.s.height, self.s.pix_fmt = w, h, "p010le"
        self.s.options = {"rc": "vbr", "cq": str(cq), "preset": "p5", "profile": "main10",
                          "maxrate": f"{int(maxrate_mbps * 1e6)}", "bufsize": f"{int(maxrate_mbps * 2e6)}"}
        self.s.codec_context.color_range = 1      # mpeg/tv
        self.n = 0

    def write(self, depth):
        """depth: float tensor [B,1,H,W] in 0..1 on the GPU."""
        import numpy as np
        import torch
        import torch.nn.functional as F
        d = depth.float().clamp(0, 1)
        if d.shape[-2:] != (self.h, self.w):
            d = F.interpolate(d, size=(self.h, self.w), mode="bilinear", align_corners=False, antialias=True)
        y = (torch.round(d * 876.0 + 64.0).to(torch.int32) << 6).to(torch.int32).squeeze(1).cpu().numpy().astype(np.uint16)
        for i in range(y.shape[0]):
            f = self.av.VideoFrame(self.w, self.h, "p010le")
            p0 = f.planes[0]
            a = np.zeros((self.h, p0.line_size // 2), np.uint16)
            a[:, :self.w] = y[i]
            p0.update(a.tobytes())
            p1 = f.planes[1]
            b = np.full((self.h // 2, p1.line_size // 2), 512 << 6, np.uint16)
            p1.update(b.tobytes())
            f.pts = self.n
            self.n += 1
            for pk in self.s.encode(f):
                self.c.mux(pk)

    def close(self):
        for pk in self.s.encode(None):
            self.c.mux(pk)
        self.c.close()


class Hooks:
    """Runtime patches on iw3.utils.  One instance per process."""

    def __init__(self, profile, plan):
        self.profile, self.plan = profile, plan
        self.mode = "video"
        self.in_process_image = 0
        self.writer = None
        self.depth_path = None
        self.depth_frames = 0
        self.last_depth = None
        self.depth_shape_seen = None

    def install(self):
        import iw3.utils as U
        orig_div, orig_pi, orig_codec = U.apply_divergence, U.process_image, U.make_video_codec_option
        me = self

        def make_video_codec_option(args, input_path=None):
            if args.video_codec == "hevc_nvenc":
                cap = me.plan["bitrate_cap_mbps"]
                opts = {"rc": "vbr", "cq": str(me.profile["cq"]), "preset": me.profile["nvenc_preset"],
                        "tune": "hq", "maxrate": str(int(cap * 1e6)), "bufsize": str(int(cap * 2e6)), "b": "0"}
                if args.gpu and args.gpu[0] >= 0:
                    opts["gpu"] = str(args.gpu[0])
                return opts
            return orig_codec(args, input_path)

        def process_image(*a, **k):
            me.in_process_image += 1
            try:
                return orig_pi(*a, **k)
            finally:
                me.in_process_image -= 1

        def apply_divergence(depth, im, args, side_model, reset_pts=None):
            if me.mode == "picture":
                me.last_depth = depth.detach()
            elif me.in_process_image == 0 and depth.ndim == 4 and not STOP.is_set():
                me.depth_shape_seen = tuple(depth.shape)
                if me.writer is None:
                    sc = float(me.profile["depth_scale"])
                    h, w = im.shape[-2:]       # the frame (eye) size; the depth tensor itself is at model resolution
                    me.writer = DepthVideoWriter(me.depth_path, me.plan["out_fps"], even(w * sc), even(h * sc),
                                                 me.profile["depth_cq"], me.profile["depth_maxrate_mbps"])
                me.writer.write(depth)
                me.depth_frames += depth.shape[0]
            return orig_div(depth, im, args, side_model, reset_pts)

        U.make_video_codec_option = make_video_codec_option
        U.process_image = process_image
        U.apply_divergence = apply_divergence

    def begin_chunk(self, depth_path):
        self.depth_path, self.writer, self.depth_frames = depth_path, None, 0

    def end_chunk(self):
        if self.writer is not None:
            self.writer.close()
            self.writer = None
        return self.depth_frames


class TqdmShim:
    cur = None

    def __init__(self, *a, total=None, desc=None, **k):
        self.n = 0
        TqdmShim.cur = self

    def update(self, n=1):
        self.n += n
        if TqdmShim.on_update:
            TqdmShim.on_update(self.n)

    def close(self):
        pass

    on_update = None


def build_iw3_args(profile, plan, model, for_video, upright_max_h):
    import iw3.utils as U
    argv = ["-i", "in.mkv", "-o", "out.mp4", "-y", "--depth-model", model, "--method", profile["method"],
            "-d", str(profile["divergence"]), "-c", str(profile["convergence"]), "--max-fps", str(int(profile["fps"])),
            "--crf", str(profile["cq"]), "--preset", profile["nvenc_preset"], "--max-workers", "0"]
    if for_video:
        argv += ["--video-codec", "hevc_nvenc", "--colorspace", "bt709", "--video-format", "mp4"]
        cw = plan["rotation_cw"]
        if cw == 90:
            argv += ["--rotate-right"]
        elif cw == 270:
            argv += ["--rotate-left"]
        elif cw == 180:
            argv += ["--vf", "hflip,vflip"]
        if upright_max_h:
            argv += ["--max-output-height", str(upright_max_h)]
    else:
        argv += ["--format", "jpeg"]
    return U.create_parser().parse_args(argv)


def setup_models(args, stop_event):
    import iw3.utils as U
    from iw3 import models  # noqa: F401  (registers the models, like iw3.cli)
    from iw3.mapper import resolve_mapper_name
    from iw3.stereo_model_factory import create_stereo_model
    U.set_state_args(args, stop_event=stop_event, tqdm_fn=TqdmShim)
    args.warp_steps = U.calc_auto_warp_steps(method=args.method, divergence=args.divergence,
                                             synthetic_view=args.synthetic_view)
    depth_model = args.state["depth_model"]
    depth_model.load(gpu=args.gpu, resolution=args.resolution, limit_resolution=args.limit_resolution)
    args.mapper = resolve_mapper_name(mapper=args.mapper, foreground_scale=args.foreground_scale,
                                      metric_depth=depth_model.is_metric(), mapper_type=args.mapper_type)
    side_model = create_stereo_model(args.method, divergence=args.divergence, device_id=args.gpu[0],
                                     inpaint_model=args.inpaint_model)
    return depth_model, side_model


ENCODER_RETRY_BACKOFF_S = (5, 15, 45)     # 3 tries: wait 5 s after the first failure, 15 s after the second


def is_encoder_open_failure(exc):
    """av.error.ExternalError from avcodec_open2 (NVENC refusing to open: transient, seen once on a real
    run after chunk 10 of 14).  Looks through the whole exception chain."""
    seen = 0
    while exc is not None and seen < 8:
        if type(exc).__name__ == "ExternalError" and "avcodec_open2" in str(exc):
            return True
        exc = exc.__cause__ or exc.__context__
        seen += 1
    return False


def interruptible_sleep(secs):
    end = time.time() + secs
    while time.time() < end:
        if STOP.is_set():
            raise Interrupted()
        time.sleep(min(1.0, max(0.0, end - time.time())))


def convert_chunk_with_retry(U, hooks, inp, sbs, dep_part, tmp, cdir, nm, args, depth_model, side_model):
    """iw3.process_video_full for one chunk, retried in-process (up to 3 tries) when the encoder cannot be
    opened.  Any other error propagates at once (the manager restarts the driver and counts it per chunk)."""
    tries = len(ENCODER_RETRY_BACKOFF_S)
    for attempt in range(1, tries + 1):
        hooks.begin_chunk(dep_part)
        try:
            U.process_video_full(inp, sbs, args, depth_model, side_model)
            return hooks.end_chunk()
        except Exception as e:
            if not is_encoder_open_failure(e) or STOP.is_set():
                raise
            log(f"chunk {nm}: encoder open failed (try {attempt}/{tries}): {e!r}")
            try:
                hooks.end_chunk()
            except Exception:
                hooks.writer = None
            # nunif parks a failed _tmp_ output as _error_<name> next to it
            for f in (tmp, dep_part, os.path.join(cdir, f"_error_sbs_{nm}.mp4"), sbs):
                if os.path.exists(f):
                    try:
                        os.remove(f)
                    except OSError:
                        pass
            if attempt == tries:
                raise
            wait = ENCODER_RETRY_BACKOFF_S[attempt - 1]
            log(f"chunk {nm}: retrying in {wait}s")
            interruptible_sleep(wait)


def phase_convert(job, prog, profile, plan, chunks):
    import iw3.utils as U
    import torch
    check_signatures()
    w = job.work
    cdir = w + "/chunks"
    pending = [c for c in chunks["chunks"] if not c.get("done")]
    if not pending:
        return
    total = chunks["total_frames"]
    done_before = sum(c.get("out_frames", c["frames"]) for c in chunks["chunks"] if c.get("done"))
    sampler = GpuSampler(lambda: [os.getpid()])
    sampler.start()
    t_load = time.time()
    upright_max_h = plan["eye_h"] if plan["scale"] < 1.0 else 0
    args = build_iw3_args(profile, plan, profile["depth_model"], True, upright_max_h)
    hooks = Hooks(profile, plan)
    hooks.install()
    depth_model, side_model = setup_models(args, STOP)
    job.add_seconds("model_load", time.time() - t_load)
    log(f"depth model {profile['depth_model']} loaded in {time.time() - t_load:.1f}s; "
        f"{len(pending)} chunks pending of {len(chunks['chunks'])}")
    state = {"in_chunk": 0, "done_frames": done_before}

    def on_upd(n):
        state["in_chunk"] = n
        prog.update(frames_done=state["done_frames"] + n)
    TqdmShim.on_update = on_upd
    idx_of = {c["name"]: i for i, c in enumerate(chunks["chunks"])}
    try:
        for c in pending:
            if STOP.is_set():
                raise Interrupted()
            nm = c["name"]
            inp, sbs, dep = f"{cdir}/in_{nm}.mkv", f"{cdir}/sbs_{nm}.mp4", f"{cdir}/depth_{nm}.mp4"
            dep_part = f"{cdir}/_dpart_{nm}.mp4"
            tmp = f"{cdir}/_tmp_sbs_{nm}.mp4"
            for f in (sbs, dep, dep_part, tmp):
                if os.path.exists(f):
                    os.remove(f)
            state["in_chunk"] = 0
            prog.update(force=True, phase="converting", chunk=idx_of[nm] + 1, chunks=len(chunks["chunks"]),
                        frames_done=state["done_frames"])
            t0 = time.time()
            dframes = convert_chunk_with_retry(U, hooks, inp, sbs, dep_part, tmp, cdir, nm, args, depth_model, side_model)
            if STOP.is_set():
                for f in (tmp, dep_part):
                    if os.path.exists(f):
                        os.remove(f)
                raise Interrupted()
            sbs_frames = count_frames(sbs)
            dep_frames = count_frames(dep_part)
            # iw3 conforms every chunk to constant fps, so a source with timestamp holes (or > 120 fps) legitimately
            # yields another frame count than the packets; the contract is SBS = depth, and SBS = the input packet
            # count (exact for CFR chunks, e.g. the interpolated ones) or what the chunk's duration asks for (+-1;
            # the mkv container duration of a stream-copied chunk is itself only good to ~2 frames).
            if "duration" not in c:       # chunks split by an older run of this script
                c["duration"] = probe_duration(inp)
            want = int(round(c["duration"] * plan["out_fps"]))
            if not (sbs_frames == dep_frames == dframes) or not (sbs_frames == c["frames"] or abs(sbs_frames - want) <= 1):
                raise RuntimeError(f"chunk {nm}: frames input {c['frames']} (duration-implied {want}) sbs {sbs_frames} "
                                   f"depth {dep_frames} (written {dframes}) differ")
            c["out_frames"] = sbs_frames
            os.replace(dep_part, dep)
            job.add_seconds("convert", time.time() - t0)
            c["done"] = True
            c["wall_s"] = round(time.time() - t0, 2)
            write_json(w + "/chunks.json", chunks)
            os.remove(inp)
            state["done_frames"] += sbs_frames
            torch.cuda.synchronize()
            log(f"chunk {nm} done: {c['frames']} frames in {c['wall_s']}s ({c['frames'] / c['wall_s']:.1f} fps)"
                f" depth shape {hooks.depth_shape_seen}")
    finally:
        job.note_vram("convert", sampler.finish())
        TqdmShim.on_update = None
        job.set(torch_max_alloc_mb=int(torch.cuda.max_memory_allocated() / 2**20))


def phase_join(job, prog, plan, chunks, profile):
    w = job.work
    prog.update(force=True, phase="joining")
    names = [c["name"] for c in chunks["chunks"]]
    out_sbs, out_depth = w + "/sbs.mp4", w + "/depth.mp4"

    def concat(kind, out, extra_in=None, audio_args=None):
        lst = w + f"/{kind}_list.txt"
        with open(lst, "w") as f:
            for n in names:
                f.write(f"file 'chunks/{kind}_{n}.mp4'\n")
        cmd = [FFMPEG, "-v", "error", "-y", "-f", "concat", "-safe", "0", "-i", lst]
        if extra_in:
            cmd += ["-i", extra_in]
        cmd += ["-map", "0:v:0"]
        if extra_in:
            cmd += ["-map", "1:a:0"] + audio_args
        cmd += ["-c:v", "copy", "-movflags", "+faststart", out + ".part.mp4"]
        r = run_cmd(cmd)
        if r.returncode != 0:
            raise RuntimeError(f"concat {kind} failed: " + r.stderr[-300:])
        os.replace(out + ".part.mp4", out)
        os.remove(lst)

    acodec = ""
    ach = 2
    audio_args = []
    if os.path.exists(w + "/audio.mka"):
        a = job.get("probe")["audio"][0]
        acodec, ach = a["codec"], a.get("channels") or 2
        if acodec in ("aac", "mp3", "ac3", "eac3"):
            audio_args = ["-c:a", "copy"]
        else:    # opus/vorbis/dts/flac/...: AAC plays everywhere (SKYBOX / Steam Frame / browsers)
            audio_args = ["-c:a", "aac", "-b:a", "192k" if ach <= 2 else "384k"]
    concat("sbs", out_sbs, w + "/audio.mka" if audio_args else None, audio_args)
    concat("depth", out_depth)
    p = probe(out_sbs)
    frames = count_frames(out_sbs)
    dframes = count_frames(out_depth)
    dur = p["duration"]
    meta = {
        "kind": "video", "profile_id": profile_id(profile), "profile": profile,
        "eye_w": p["w"] // 2, "eye_h": p["h"], "fps": p["fps"], "duration": dur, "frames": frames,
        "depth_frames": dframes, "depth_w": probe(out_depth)["w"], "depth_h": probe(out_depth)["h"],
        "source_fps": job.get("probe")["fps"], "interpolated": plan["interpolate"],
        "rotation_cw": plan["rotation_cw"], "hdr_tonemapped_to_sdr": plan["hdr"],
        "audio_codec_in": acodec or None, "audio_codec_out": (probe_audio_codec(out_sbs) if audio_args else None),
        "sbs_bytes": os.path.getsize(out_sbs), "depth_bytes": os.path.getsize(out_depth),
        "sbs_mbps": round(os.path.getsize(out_sbs) * 8 / dur / 1e6, 2) if dur else None,
        "bitrate_cap_mbps": plan["bitrate_cap_mbps"],
        "gpu_seconds": round(sum(job.get("seconds", {}).values()), 1), "seconds": job.get("seconds"),
        "gpu_min_per_output_min": round(sum(job.get("seconds", {}).values()) / dur, 2) if dur else None,
        "vram_mb": job.get("vram"), "nunif_commit": COMMIT_PINNED,
    }
    write_json(w + "/meta.json", meta)
    return meta


def probe_audio_codec(path):
    r = run_cmd([FFPROBE, "-v", "error", "-select_streams", "a:0", "-show_entries", "stream=codec_name",
                 "-of", "csv=p=0", path])
    return r.stdout.strip() or None


def run_video(work, src, profile, keep_chunks):
    job = Job(work)
    pid_ = profile_id(profile)
    if job.d and (job.d.get("profile_id") != pid_ or job.d.get("src") != src):
        raise RuntimeError("work dir holds a different job (src/profile differ); use an empty dir")
    if not job.d:
        job.d = {"kind": "video", "src": src, "profile": profile, "profile_id": pid_, "created": int(time.time())}
    if "probe" not in job.d:
        job.d["probe"] = probe(src)
    pr = job["probe"]
    plan = make_plan(pr, profile)
    job.set(plan=plan, state="running")
    log("probe:", json.dumps(pr))
    log("plan:", json.dumps(plan))
    if os.path.exists(work + "/meta.json") and os.path.exists(work + "/sbs.mp4"):
        log("already complete")
        job.set(state="done")
        return read_json(work + "/meta.json")
    prog = Progress(work, plan["expected_frames"])
    split_src = src
    if plan["interpolate"] and not os.path.exists(work + "/chunks.json"):
        split_src = phase_interp(job, prog, src, plan, profile)
    chunks = phase_split(job, prog, split_src, plan, profile, src)
    phase_convert(job, prog, profile, plan, chunks)
    chunks = read_json(work + "/chunks.json")
    meta = phase_join(job, prog, plan, chunks, profile)
    if not keep_chunks:
        shutil.rmtree(work + "/chunks", ignore_errors=True)
    prog.update(force=True, phase="done", frames_done=meta["frames"])
    job.set(state="done")
    log("done:", json.dumps({k: meta[k] for k in ("frames", "duration", "sbs_mbps", "gpu_min_per_output_min")}))
    return meta


# ------------------------------------------------------------------ pictures
def run_pictures(work, src, profile):
    import numpy as np
    import torch
    import torch.nn.functional as F
    from PIL import Image
    from torchvision.transforms import functional as TF
    import iw3.utils as U
    from nunif.utils.pil_io import load_image_simple
    check_signatures()
    if os.path.isdir(src):
        files = sorted(f for f in glob.glob(src + "/*") if f.lower().endswith((".jpg", ".jpeg", ".png", ".webp", ".bmp")))
        single = False
    else:
        files, single = [src], True
    job = Job(work)
    job.d.setdefault("kind", "picture")
    job.d["profile"], job.d["profile_id"], job.d["src"] = profile, profile_id(profile), src
    plan = {"bitrate_cap_mbps": 0, "rotation_cw": 0}
    sampler = GpuSampler(lambda: [os.getpid()])
    sampler.start()
    t_load = time.time()
    args = build_iw3_args(profile, plan, profile["picture_model"], False, 0)
    hooks = Hooks(profile, plan)
    hooks.mode = "picture"
    hooks.install()
    depth_model, side_model = setup_models(args, STOP)
    job.add_seconds("model_load", time.time() - t_load)
    log(f"{profile['picture_model']} loaded in {time.time() - t_load:.1f}s")
    depth_model.disable_ema()
    if side_model is not None and hasattr(side_model, "set_mode"):
        side_model.set_mode("image")
        side_model.reset()
    results = []
    for f in files:
        if STOP.is_set():
            raise Interrupted()
        outdir = work if single else os.path.join(work, "out", os.path.splitext(os.path.basename(f))[0])
        os.makedirs(outdir, exist_ok=True)
        if os.path.exists(outdir + "/meta.json"):
            results.append(read_json(outdir + "/meta.json"))
            continue
        t0 = time.time()
        im, _ = load_image_simple(f, color="rgb", exif_transpose=True)
        sw, sh = im.size
        x = TF.to_tensor(im).to(args.state["device"])
        with torch.inference_mode():
            sbs = U.process_image(x, args, depth_model, side_model)
        pil = U.to_pil_image(sbs)
        pil.save(outdir + "/sbs.jpg", format="JPEG", quality=int(profile["picture_jpeg_quality"]), subsampling="4:2:0")
        d = hooks.last_depth
        d = d.float().reshape(1, 1, *d.shape[-2:])
        if d.shape[-2:] != (sh, sw):
            d = F.interpolate(d, size=(sh, sw), mode="bilinear", align_corners=False)
        arr = (d.clamp(0, 1)[0, 0] * 65535.0).round().to(torch.int32).cpu().numpy().astype(np.uint16)
        Image.fromarray(arr).save(outdir + "/depth.png")
        wall = time.time() - t0
        meta = {"kind": "picture", "src": f, "eye_w": pil.width // 2, "eye_h": pil.height, "depth_w": sw, "depth_h": sh,
                "sbs_bytes": os.path.getsize(outdir + "/sbs.jpg"), "depth_bytes": os.path.getsize(outdir + "/depth.png"),
                "wall_s": round(wall, 2), "model": profile["picture_model"], "profile_id": profile_id(profile)}
        write_json(outdir + "/meta.json", meta)
        results.append(meta)
        job.add_seconds("convert", wall)
        log(f"picture {os.path.basename(f)}: {meta['eye_w']}x{meta['eye_h']} per eye, {wall:.1f}s")
    job.note_vram("pictures", sampler.finish())
    job.set(state="done", results=len(results), torch_max_alloc_mb=int(torch.cuda.max_memory_allocated() / 2**20))
    return results


# ------------------------------------------------------------------ main
def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--work", required=True)
    r.add_argument("--src", required=True)
    r.add_argument("--kind", required=True, choices=["video", "picture"])
    r.add_argument("--profile")
    r.add_argument("--keep-chunks", action="store_true")
    pq = sub.add_parser("probe")
    pq.add_argument("file")
    a = ap.parse_args()
    if a.cmd == "probe":
        print(json.dumps(probe(os.path.abspath(a.file)), indent=1))
        return 0
    work, src = os.path.abspath(a.work), os.path.abspath(a.src)
    os.makedirs(work, exist_ok=True)
    LOGF[0] = open(work + "/job.log", "a")
    signal.signal(signal.SIGTERM, _on_term)
    signal.signal(signal.SIGINT, _on_term)
    profile = merge_profile(a.profile)
    os.chdir(NUNIF)
    sys.path.insert(0, NUNIF)
    try:
        if a.kind == "video":
            run_video(work, src, profile, a.keep_chunks)
        else:
            run_pictures(work, src, profile)
    except Interrupted:
        log("interrupted (SIGTERM); resume by re-running with the same --work")
        Job(work).set(state="interrupted")
        return RC_INTERRUPTED
    except Exception as e:
        import traceback
        log("FAILED:", repr(e))
        log(traceback.format_exc())
        Job(work).set(state="failed", error=str(e)[-500:])
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
