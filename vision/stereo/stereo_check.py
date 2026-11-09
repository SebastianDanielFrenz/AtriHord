#!/usr/bin/env python3
"""P1 acceptance checks for stereo_job.py (run on the worker, stereo venv).

    run_stereo.sh exec /data/ksf-vision/stereo/.venv/bin/python ksf/stereo_check.py all
    stereo_check.py run NAME...      run the job for the named test inputs (skips finished work dirs)
    stereo_check.py verify NAME...   check finished work dirs only
    stereo_check.py resume           SIGTERM after chunk 2 is done, re-run, compare with the uninterrupted run
    stereo_check.py caps             clip d at long-side caps 1440 / 2160 / 2560
    stereo_check.py summary          VRAM peaks and GPU-minutes per output minute from the job.json files
Prints PASS/FAIL per check; exit code 1 if any FAIL.
"""
import glob
import json
import os
import re
import signal
import subprocess
import sys
import time

sys.path.insert(0, "/data/ksf-vision/stereo/ksf")
import stereo_job as SJ  # noqa: E402

ROOT = "/data/ksf-vision/stereo"
SRC = ROOT + "/bench/src"
WORK = "/data/ksf-vision/stereo-work"
EVID = ROOT + "/p1_evidence"
KSF = ROOT + "/ksf"
PY = ROOT + "/.venv/bin/python"

VIDEOS = {  # name: file
    "a_anime": "a_anime_gate.mkv",
    "b_live": "b_live_trains1080.mkv",
    "c_phone": "c_phone_portrait.mkv",
    "d_4k": "d_live_trains4k.mkv",
    "e_mmd120": "p1_mmd120.mkv",
    "f_hdr": "p1_hdr60.mkv",
}
PICTURES = {"pic1": "p1_anime_illust.jpg", "pic2": "p3_cos_eula.jpg"}
CAPS = [1440, 2160, 2560]

RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append((name, ok))
    print(("PASS " if ok else "FAIL ") + name + (": " + str(detail) if detail != "" else ""), flush=True)
    return ok


def sh(cmd):
    return subprocess.run(cmd, capture_output=True, text=True)


def stream_info(path, sel="v:0"):
    r = sh([SJ.FFPROBE, "-v", "error", "-select_streams", sel, "-show_entries",
            "stream=codec_name,profile,width,height,pix_fmt,r_frame_rate,avg_frame_rate,duration,bit_rate,color_range"
            ":format=duration,size", "-of", "json", path])
    j = json.loads(r.stdout)
    s = (j.get("streams") or [{}])[0]
    s["fmt_duration"] = float(j["format"]["duration"])
    s["fmt_size"] = int(j["format"]["size"])
    return s


def extract_frame(path, idx, out, gray16=False):
    cmd = [SJ.FFMPEG, "-v", "error", "-y", "-i", path, "-vf", f"select=eq(n\\,{idx})", "-fps_mode", "passthrough", "-frames:v", "1"]
    if gray16:
        cmd += ["-pix_fmt", "gray16le", "-f", "rawvideo", out]
    else:
        cmd += [out]
    return sh(cmd).returncode == 0 and os.path.exists(out)


def edge_corr(a, b):
    import numpy as np

    def grad(x):
        gy, gx = np.gradient(x.astype("float64"))
        return np.hypot(gx, gy).ravel()
    ga, gb = grad(a), grad(b)
    if ga.std() < 1e-9 or gb.std() < 1e-9:
        return 0.0
    return float(np.corrcoef(ga, gb)[0, 1])


def alignment(name, sbs, depth, frames, fps):
    """Depth frame k belongs to SBS frame k: gradient correlation with the left eye must beat other indices."""
    import numpy as np
    from PIL import Image
    os.makedirs(EVID, exist_ok=True)
    idxs = [int(frames * 0.15), int(frames * 0.5), int(frames * 0.85)]
    other = {i: (i + int(frames * 0.33)) % (frames - 2) for i in idxs}
    wins = 0
    detail = []
    for i in idxs:
        sb, dp = f"{EVID}/{name}_sbs_{i}.png", f"{EVID}/{name}_depth_{i}.png"
        ob = f"{EVID}/{name}_depthother_{other[i]}.png"
        if not (extract_frame(sbs, i, sb) and extract_frame(depth, i, dp) and extract_frame(depth, other[i], ob)):
            check(f"{name}: extract frame {i}", False)
            continue
        L = Image.open(sb).convert("L")
        L = L.crop((0, 0, L.width // 2, L.height))
        D, O = Image.open(dp).convert("L"), Image.open(ob).convert("L")
        Ls = np.asarray(L.resize(D.size, Image.BILINEAR))
        same, diff = edge_corr(Ls, np.asarray(D)), edge_corr(Ls, np.asarray(O))
        wins += same > diff and same > 0.05
        detail.append(f"idx {i}: corr same {same:.3f} vs frame {other[i]} {diff:.3f}")
        # composite for looking at: left eye | its depth | depth of a distant frame
        W = 640
        parts = [im.resize((W, int(W * im.height / im.width))) for im in (L, D.convert("RGB"), O.convert("RGB"))]
        comp = Image.new("RGB", (W * 3, max(p.height for p in parts)))
        for k, p in enumerate(parts):
            comp.paste(p.convert("RGB"), (k * W, 0))
        comp.save(f"{EVID}/{name}_align_{i}.jpg", quality=88)
    print(f"INFO  {name}: edge-correlation (weak for smooth depth, informational): wins {wins}/3; " + "; ".join(detail))
    lag_test(name, sbs, depth, frames)


def gray_series(path, start, n, sbs):
    import numpy as np
    crop = "crop=iw/2:ih:0:0," if sbs else ""
    r = subprocess.run([SJ.FFMPEG, "-v", "error", "-i", path, "-vf",
                        "select=between(n\\,%d\\,%d),%sscale=64:64" % (start, start + n - 1, crop),
                        "-fps_mode", "passthrough", "-pix_fmt", "gray", "-f", "rawvideo", "-"], capture_output=True)
    a = np.frombuffer(r.stdout, dtype="u1")
    k = len(a) // (64 * 64)
    return a[:k * 64 * 64].reshape(k, 64, 64).astype("float64")


def lag_test(name, sbs, depth, frames):
    """Frame-exact alignment: the per-frame motion energy of the left eye and of the depth video must correlate best at lag 0."""
    import numpy as np
    n = 300
    start = max(0, min(int(frames * 0.4), frames - n - 2))
    L, D = gray_series(sbs, start, n, True), gray_series(depth, start, n, False)
    m = min(len(L), len(D))
    if m < 100:
        return check(f"{name}: depth/SBS frame lag test", False, f"decoded only {len(L)}/{len(D)} frames")
    dl = np.abs(np.diff(L[:m], axis=0)).mean(axis=(1, 2))
    dd = np.abs(np.diff(D[:m], axis=0)).mean(axis=(1, 2))
    if dl.std() < 1e-3 or dd.std() < 1e-3:
        return check(f"{name}: depth/SBS frame lag test", False, f"inconclusive: motion std L {dl.std():.4f} D {dd.std():.4f}")
    cors = {}
    for lag in range(-4, 5):
        a = dl[max(0, lag):len(dl) + min(0, lag)]
        b = dd[max(0, -lag):len(dd) + min(0, -lag)]
        cors[lag] = float(np.corrcoef(a, b)[0, 1])
    best = max(cors, key=cors.get)
    check(f"{name}: depth frame k belongs to SBS frame k (motion-energy correlation peaks at lag 0)", best == 0 and cors[0] > 0.3,
          f"frames {start}..{start + m - 1}; corr by lag " + " ".join(f"{k}:{v:.2f}" for k, v in cors.items()))


def depth_not_constant(name, depth, frames):
    os.makedirs(EVID, exist_ok=True)
    import numpy as np
    stds = []
    for i in (int(frames * 0.2), int(frames * 0.6)):
        out = f"{EVID}/{name}_depthraw_{i}.raw"
        if extract_frame(depth, i, out, gray16=True):
            a = np.fromfile(out, dtype="<u2")
            stds.append(float(a.std()))
            os.remove(out)
    check(f"{name}: depth video not constant", len(stds) == 2 and min(stds) > 500, f"16-bit std {stds}")


def audio_duration(path):
    r = sh([SJ.FFPROBE, "-v", "error", "-select_streams", "a:0", "-show_entries", "packet=pts_time,duration_time", "-of", "csv=p=0", path])
    last = [ln for ln in r.stdout.strip().splitlines() if ln][-1].split(",")
    return float(last[0]) + float(last[1] or 0)


def verify_video(name, src_file):
    w = f"{WORK}/{name}"
    meta = SJ.read_json(w + "/meta.json")
    job = SJ.read_json(w + "/job.json", {})
    if not meta:
        return check(f"{name}: finished", False, "no meta.json")
    plan, pr, prof = job["plan"], job["probe"], job["profile"]
    sbs, depth = w + "/sbs.mp4", w + "/depth.mp4"
    check(f"{name}: job state done", job.get("state") == "done", job.get("state"))
    v, d = stream_info(sbs), stream_info(depth)
    ew, eh = v["width"] // 2, v["height"]
    cap = prof["max_eye_long_side"]
    check(f"{name}: SBS width = 2x eye, eye matches plan (+-2 px)",
          v["width"] % 2 == 0 and abs(ew - plan["eye_w"]) <= 2 and abs(eh - plan["eye_h"]) <= 2,
          f"sbs {v['width']}x{v['height']}, plan eye {plan['eye_w']}x{plan['eye_h']}")
    check(f"{name}: eye long side <= cap {cap} (+2)", max(ew, eh) <= cap + 2, f"{max(ew, eh)}")
    want_rfr = "120/1" if plan["out_fps"] == 120.0 else None
    if want_rfr:
        check(f"{name}: r_frame_rate 120/1", v["r_frame_rate"] == "120/1", v["r_frame_rate"])
    else:
        check(f"{name}: r_frame_rate = source ({pr['fps']:.3f})", abs(SJ.frac(v["r_frame_rate"]) - pr["fps"]) < 0.05, v["r_frame_rate"])
    frames = SJ.count_frames(sbs)
    dur = v["fmt_duration"]
    check(f"{name}: frames = round(duration*fps) +-2", abs(frames - round(dur * plan["out_fps"])) <= 2, f"{frames} vs {round(dur * plan['out_fps'])}")
    vd = pr.get("video_duration", pr["duration"])
    check(f"{name}: duration within 0.3 s of the source's video duration", abs(dur - vd) <= 0.3, f"{dur:.3f} vs {vd:.3f}")
    if pr["audio"]:
        a = stream_info(sbs, "a:0")
        ad = float(a.get("duration") or dur)
        asrc = audio_duration(f"{SRC}/{src_file}")
        check(f"{name}: audio present, duration within 0.3 s of the source's audio", a.get("codec_name") is not None and abs(ad - asrc) <= 0.3,
              f"{a.get('codec_name')} {ad:.2f}s vs source audio {asrc:.2f}s (video {dur:.2f}s)")
    br = v["fmt_size"] * 8 / dur / 1e6
    check(f"{name}: avg bitrate <= cap*1.15", br <= plan["bitrate_cap_mbps"] * 1.15, f"{br:.1f} Mbit/s (file incl. audio) vs cap {plan['bitrate_cap_mbps']}")
    dframes = SJ.count_frames(depth)
    check(f"{name}: depth frames = SBS frames", dframes == frames, f"{dframes} vs {frames}")
    check(f"{name}: depth is 10-bit HEVC Main 10", d["pix_fmt"] == "yuv420p10le" and d["codec_name"] == "hevc" and "10" in d.get("profile", ""),
          f"{d['codec_name']} {d.get('profile')} {d['pix_fmt']} {d['width']}x{d['height']}")
    check(f"{name}: depth fps = SBS fps", d["r_frame_rate"] == v["r_frame_rate"], f"{d['r_frame_rate']} vs {v['r_frame_rate']}")
    depth_not_constant(name, depth, frames)
    alignment(name, sbs, depth, frames, plan["out_fps"])
    if plan["rotation_cw"] in (90, 270):
        check(f"{name}: rotated source gives portrait eye", eh > ew, f"{ew}x{eh} (rotation_cw {plan['rotation_cw']}, {pr['rotation_source']})")
        left = f"{EVID}/{name}_upright_left_eye.png"
        sh([SJ.FFMPEG, "-v", "error", "-y", "-i", sbs, "-vf", f"select=eq(n\\,{int(frames * 0.3)}),crop=iw/2:ih:0:0", "-fps_mode", "passthrough", "-frames:v", "1", left])
        print(f"INFO  {name}: saved upright check frame {left}")
    if name.startswith(("e_", "mmd")) and pr["fps"] >= 119:
        check(f"{name}: >=119 fps source not interpolated", plan["interpolate"] is False and meta["interpolated"] is False
              and "interp" not in (job.get("seconds") or {}), f"interpolate={plan['interpolate']} seconds={job.get('seconds')}")
    if pr["hdr"]:
        check(f"{name}: HDR source tonemapped, SDR output tagged bt709", v.get("pix_fmt") == "yuv420p", f"{v.get('pix_fmt')}")
    check(f"{name}: nunif commit == pinned", SJ.COMMIT_PINNED == open(SJ.NUNIF + "/COMMIT_PINNED").read().split()[0])
    return True


def verify_picture(name, src_file):
    from PIL import Image
    w = f"{WORK}/{name}"
    meta = SJ.read_json(w + "/meta.json")
    if not meta:
        return check(f"{name}: finished", False)
    src = Image.open(f"{SRC}/{src_file}")
    from PIL import ImageOps
    src = ImageOps.exif_transpose(src)
    sbs = Image.open(w + "/sbs.jpg")
    dep = Image.open(w + "/depth.png")
    check(f"{name}: sbs.jpg width = 2x source", sbs.width == 2 * src.width and sbs.height == src.height, f"{sbs.size} vs source {src.size}")
    check(f"{name}: depth.png 16-bit (I;16) at source size", dep.mode in ("I;16", "I") and dep.size == src.size, f"{dep.mode} {dep.size}")
    import numpy as np
    a = np.asarray(dep)
    check(f"{name}: depth.png not constant, uses the 16-bit range", float(a.std()) > 1000 and int(a.max()) > 256, f"std {a.std():.0f} max {a.max()}")
    L = sbs.crop((0, 0, sbs.width // 2, sbs.height)).convert("L").resize((dep.width // 4, dep.height // 4))
    D = dep.point(lambda p: p / 256).convert("L") if dep.mode == "I" else Image.fromarray((a >> 8).astype("uint8"))
    D = D.resize(L.size)
    corr = edge_corr(np.asarray(L), np.asarray(D))
    check(f"{name}: depth edges correlate with the left eye", corr > 0.05, f"{corr:.3f}")
    os.makedirs(EVID, exist_ok=True)
    comp = Image.new("RGB", (L.width * 2, L.height))
    comp.paste(L.convert("RGB"), (0, 0))
    comp.paste(D.convert("RGB"), (L.width, 0))
    comp.save(f"{EVID}/{name}_pic_align.jpg", quality=88)
    return True


def job_cmd(name, src, kind, profile=None):
    cmd = [PY, f"{KSF}/stereo_job.py", "run", "--work", f"{WORK}/{name}", "--src", src, "--kind", kind]
    if profile:
        cmd += ["--profile", json.dumps(profile)]
    return cmd


def run_job(name, src, kind, profile=None):
    if os.path.exists(f"{WORK}/{name}/meta.json") and kind == "video":
        print(f"skip {name}: finished")
        return 0
    t0 = time.time()
    rc = subprocess.call(job_cmd(name, src, kind, profile))
    print(f"job {name} rc={rc} in {time.time() - t0:.0f}s", flush=True)
    return rc


def test_resume():
    """SIGTERM after chunk 2 is done; re-run; totals equal the uninterrupted run of the same clip."""
    base = SJ.read_json(f"{WORK}/b_live/meta.json")
    if not base:
        return check("resume: needs finished b_live as the uninterrupted reference", False)
    name = "resume_b"
    wd = f"{WORK}/{name}"
    if os.path.exists(wd):
        subprocess.call(["rm", "-rf", wd])
    p = subprocess.Popen(job_cmd(name, f"{SRC}/{VIDEOS['b_live']}", "video"), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    t0 = time.time()
    while time.time() - t0 < 900:
        c = SJ.read_json(wd + "/chunks.json")
        if c and sum(1 for x in c["chunks"] if x.get("done")) >= 2:
            break
        time.sleep(0.5)
    n_done_at_kill = sum(1 for x in (SJ.read_json(wd + "/chunks.json") or {"chunks": []})["chunks"] if x.get("done"))
    p.send_signal(signal.SIGTERM)
    rc1 = p.wait(timeout=60)
    c = SJ.read_json(wd + "/chunks.json")
    done_after = sum(1 for x in c["chunks"] if x.get("done"))
    check("resume: SIGTERM exits 75 within 60 s, >= 2 chunks done, not finished", rc1 == 75 and done_after >= 2 and not os.path.exists(wd + "/meta.json"),
          f"rc {rc1}, chunks done {done_after}/{len(c['chunks'])}, state {SJ.read_json(wd + '/job.json')['state']}")
    leftovers = [f for f in os.listdir(wd + "/chunks") if f.startswith(("_tmp", "_dpart"))]
    check("resume: no partial files left behind", not leftovers, leftovers)
    t1 = time.time()
    rc2 = subprocess.call(job_cmd(name, f"{SRC}/{VIDEOS['b_live']}", "video"), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    m = SJ.read_json(wd + "/meta.json")
    check("resume: second run finishes (rc 0)", rc2 == 0 and m is not None, f"rc {rc2}, {time.time() - t1:.0f}s")
    if m:
        check("resume: frame count equals the uninterrupted run", m["frames"] == base["frames"], f"{m['frames']} vs {base['frames']}")
        check("resume: duration equals the uninterrupted run (+-0.01 s)", abs(m["duration"] - base["duration"]) <= 0.01, f"{m['duration']:.3f} vs {base['duration']:.3f}")
        check("resume: depth frames equal SBS frames", m["depth_frames"] == m["frames"], f"{m['depth_frames']} vs {m['frames']}")
        v = stream_info(f"{wd}/sbs.mp4")
        check("resume: output size equal to the uninterrupted run (+-1 %)", abs(v["fmt_size"] - base["sbs_bytes"]) <= 0.01 * base["sbs_bytes"] + 1,
              f"{v['fmt_size']} vs {base['sbs_bytes']} (bytes differ slightly only if encoder state differs)")


def summary(names):
    print("\nname | eye | fps | out min | gpu s (interp/convert/load) | GPU-min per output-min | sbs Mbit/s (cap) | depth MB | vram interp | vram iw3")
    for n in names:
        m = SJ.read_json(f"{WORK}/{n}/meta.json")
        if not m or "seconds" not in m:
            continue
        s, v = m["seconds"], m.get("vram_mb") or {}
        print(f"{n} | {m['eye_w']}x{m['eye_h']} | {m['fps']:.2f} | {m['duration'] / 60:.2f} | "
              f"{s.get('interp', 0):.0f}/{s.get('convert', 0):.0f}/{s.get('model_load', 0):.0f} | {m['gpu_min_per_output_min']:.2f}"
              f" | {m['sbs_mbps']} ({m['bitrate_cap_mbps']}) | {m['depth_bytes'] / 1e6:.1f} | "
              f"{(v.get('interp') or {}).get('proc_peak_mb')} (card {(v.get('interp') or {}).get('card_peak_mb')}) | "
              f"{(v.get('convert') or {}).get('proc_peak_mb')} (card {(v.get('convert') or {}).get('card_peak_mb')}, base {(v.get('convert') or {}).get('card_base_mb')})")


def main():
    a = sys.argv[1:] or ["all"]
    cmd, names = a[0], a[1:]
    if cmd in ("run", "verify", "all") and not names:
        names = list(VIDEOS) + list(PICTURES)
    if cmd in ("run", "all"):
        for n in names:
            if n in VIDEOS:
                run_job(n, f"{SRC}/{VIDEOS[n]}", "video")
            elif n in PICTURES:
                os.makedirs(f"{WORK}/{n}", exist_ok=True)
                run_job(n, f"{SRC}/{PICTURES[n]}", "picture")
    if cmd == "caps":
        names = []
        for cap in CAPS:
            n = "d_4k" if cap == 2160 else f"d_cap{cap}"      # d_4k IS the default-profile (2160) run
            if cap != 2160:
                run_job(n, f"{SRC}/{VIDEOS['d_4k']}", "video", {"max_eye_long_side": cap})
            names.append(n)
        for n in names:
            verify_video(n, VIDEOS["d_4k"])
        summary(names)
    if cmd in ("verify", "all"):
        for n in names:
            print(f"\n=== {n}")
            if n in VIDEOS:
                verify_video(n, VIDEOS[n])
            elif n in PICTURES:
                verify_picture(n, PICTURES[n])
    if cmd in ("resume", "all"):
        print("\n=== resume")
        test_resume()
    if cmd in ("summary", "all"):
        summary(names or list(VIDEOS))
    bad = [n for n, ok in RESULTS if not ok]
    print(f"\n{len(RESULTS) - len(bad)} PASS, {len(bad)} FAIL")
    for n in bad:
        print("  FAIL", n)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
