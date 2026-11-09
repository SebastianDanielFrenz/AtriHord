"""Intro/outro cutting (video_trim.py) on synthetic videos in a temp library, and
the staged server.py's wiring for it (config, schema, routes, download hook).

Detection, the stream-copy cut, hard links, undo, the skipped video without a
keyframe, auto-delete, reject, and a new video joining an approved signature.
Nothing under /data2 is written; everything lives in a temp dir.

  venv/bin/python video_trim_check.py /tmp/vcut_test/stage
"""
import ast
import atexit
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time

STAGE = os.path.abspath(sys.argv[1])
sys.path.insert(0, STAGE)
import video_trim                       # noqa: E402

tmp = tempfile.mkdtemp(prefix="video_trim_check_")
atexit.register(shutil.rmtree, tmp, True)
LIB = os.path.join(tmp, "lib")
os.makedirs(LIB)
ok = True


def check(label, cond, detail=""):
    global ok
    ok = ok and bool(cond)
    print(("PASS " if cond else "FAIL ") + label + (f"  ({detail})" if detail and not cond else ""), flush=True)


def sha(path):
    h = hashlib.sha1()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def probe_json(path, *args):
    out = subprocess.run(["ffprobe", "-v", "error", "-of", "json"] + list(args) + [path],
                         capture_output=True, text=True).stdout
    return json.loads(out)


def duration(path):
    return float(probe_json(path, "-show_entries", "format=duration")["format"]["duration"])


def codecs(path):
    data = probe_json(path, "-show_entries", "stream=codec_type,codec_name")
    return sorted((s["codec_type"], s["codec_name"]) for s in data["streams"])


def packet_sizes(path):
    """(pts_time, size) of every video packet - the proof of a stream copy."""
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                          "packet=size", "-of", "csv=p=0", path], capture_output=True, text=True).stdout
    return [int(x) for x in out.split()]


def is_suffix(short, full):
    return len(short) <= len(full) and full[len(full) - len(short):] == short


def is_prefix(short, full):
    return len(short) <= len(full) and full[:len(short)] == short


def gen(path, k, kind="mp4", intro=True, outro=True, sparse=False, crf=None, content_s=20):
    """intro (testsrc) + content (a mandelbrot zoom unique to k) + outro (smptebars)."""
    inputs, labels = [], []
    if intro:
        inputs += ["-f", "lavfi", "-i", "testsrc=s=320x240:r=25:d=3", "-f", "lavfi", "-i", "sine=f=440:d=3"]
    inputs += ["-t", str(content_s), "-f", "lavfi", "-i",
               f"mandelbrot=s=320x240:r=25:start_x={-0.74 + k * 0.013}:start_y={0.13 + k * 0.021}"
               f":start_scale={0.05 + k * 0.004}:end_scale=0.001:maxiter={200 + 17 * k}",
               "-f", "lavfi", "-i", f"sine=f={300 + 40 * k}:d={content_s}"]
    if outro:
        inputs += ["-f", "lavfi", "-i", "smptebars=s=320x240:r=25:d=3", "-f", "lavfi", "-i", "sine=f=660:d=3"]
    parts = (["intro"] if intro else []) + ["content"] + (["outro"] if outro else [])
    chain = "".join(f"[{2 * i}:v][{2 * i + 1}:a]" for i in range(len(parts)))
    fc = f"{chain}concat=n={len(parts)}:v=1:a=1[v][a]"
    if kind == "webm":
        enc = ["-c:v", "libvpx-vp9", "-b:v", "400k", "-g", "25", "-deadline", "realtime", "-cpu-used", "8",
               "-c:a", "libopus"]
    else:
        gop = "2000" if sparse else "25"
        enc = ["-c:v", "libx264", "-preset", "ultrafast", "-g", gop, "-keyint_min", gop, "-sc_threshold", "0",
               "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "64k"]
        if crf:
            enc += ["-crf", str(crf)]
    os.makedirs(os.path.dirname(path), exist_ok=True)
    res = subprocess.run(["ffmpeg", "-v", "error", "-y"] + inputs + ["-filter_complex", fc, "-map", "[v]",
                         "-map", "[a]"] + enc + [path], capture_output=True, text=True)
    assert res.returncode == 0, res.stderr
    old = time.time() - 3600           # the scan leaves files younger than a minute alone
    os.utime(path, (old, old))


def new_service(**overrides):
    cfg = dict(video_trim.DEFAULTS, **overrides)
    state = tempfile.mkdtemp(dir=tmp, prefix="state_")
    svc = video_trim.VideoTrim(state, lambda: cfg, lambda: False, lambda: [{"id": "lib", "path": LIB}],
                               lambda: invalidations.append(1), log=lambda t: print("  " + t, flush=True))
    return svc


invalidations = []

# ---- the library -------------------------------------------------------------------
t0 = time.time()
A = os.path.join(LIB, "chanA")
for k in range(1, 6):
    gen(os.path.join(A, f"v{k}.mp4"), k, crf=35 if k == 4 else None)
gen(os.path.join(A, "v6_sparse.mp4"), 6, sparse=True)
os.makedirs(os.path.join(LIB, "share"))
os.link(os.path.join(A, "v1.mp4"), os.path.join(LIB, "share", "v1_link.mp4"))
for k in range(1, 5):
    gen(os.path.join(LIB, "chanB", f"w{k}.webm"), 10 + k, kind="webm", outro=False, content_s=12)
for k in range(1, 5):
    gen(os.path.join(LIB, "chanC", f"u{k}.mp4"), 20 + k, intro=False, outro=False)
for k in range(1, 4):
    gen(os.path.join(LIB, "chanE", f"e{k}.mp4"), 30 + k, outro=False)
for k in range(1, 4):
    gen(os.path.join(LIB, "chanF", f"f{k}.mp4"), 40 + k, outro=False)
print(f"library made in {time.time() - t0:.1f}s", flush=True)

orig_hash = {os.path.join(d, n): sha(os.path.join(LIB, d, n))
             for d in ("chanA", "chanB", "chanC", "chanE", "chanF") for n in os.listdir(os.path.join(LIB, d))}
orig_packets = {os.path.join(LIB, p): packet_sizes(os.path.join(LIB, p)) for p in orig_hash
                if p.endswith("v2.mp4") or p.endswith("w1.webm")}
orig_durations = {os.path.join(LIB, p): duration(os.path.join(LIB, p)) for p in orig_hash}

# ---- detection ---------------------------------------------------------------------------------
svc = new_service()
STATE = svc.base_dir
one = os.path.join(A, "v2.mp4")
st = os.stat(one)
t1 = time.time()
fp = svc.fingerprint(one, st, "lib:chanA", svc.cfg())
cold = time.time() - t1
t1 = time.time()
svc.fingerprint(one, st, "lib:chanA", svc.cfg())
warm = time.time() - t1
print(f"fingerprint of a 26 s 320x240 video: cold {cold:.2f}s, cached {warm * 1000:.1f}ms "
      f"({len(fp['head'])} head + {len(fp['tail'])} tail frames)", flush=True)
check("fingerprint is cached by inode/size/mtime", warm < cold / 5 and cold < 20)

res = svc.scan_folder("lib", "chanA")
sigs = {s["id"]: s for s in svc.state["signatures"].values()}
kinds = sorted(s["kind"] for s in sigs.values() if s["folder"] == "chanA")
check("chanA: one intro and one outro found", kinds == ["intro", "outro"], str(kinds))
intro_a = next(s for s in sigs.values() if s["folder"] == "chanA" and s["kind"] == "intro")
outro_a = next(s for s in sigs.values() if s["folder"] == "chanA" and s["kind"] == "outro")
check("chanA intro: 6 videos (hard link counted once), about 3 s",
      len(intro_a["members"]) == 6 and 2.5 <= intro_a["length_s"] <= 3.6,
      f"{len(intro_a['members'])} {intro_a['length_s']}")
check("chanA outro: 6 videos, about 3 s", len(outro_a["members"]) == 6 and 2.5 <= outro_a["length_s"] <= 3.6,
      f"{len(outro_a['members'])} {outro_a['length_s']}")
check("the differently encoded video (crf 35) is a member too", "chanA/v4.mp4" in intro_a["members"])
check("nothing pending is cut before approval",
      all(sha(p) == h for p, h in ((os.path.join(LIB, "chanA", n), orig_hash[os.path.join("chanA", n)])
                                   for n in os.listdir(A))))
check("previews exist", all(svc.frame_path(intro_a["id"], i) for i in range(video_trim.PREVIEW_FRAMES)))
pv = svc.preview(intro_a["id"])
check("preview lists each member's range", len(pv["members"]) == 6 and all(m["start_s"] == 0 for m in pv["members"]))
check("signature status is pending", intro_a["status"] == "pending" and not intro_a["auto_delete"])

svc.scan_folder("lib", "chanB")
svc.scan_folder("lib", "chanC")
b_sigs = [s for s in svc.state["signatures"].values() if s["folder"] == "chanB"]
check("chanB (webm): one intro, 4 videos, no outro", [s["kind"] for s in b_sigs] == ["intro"]
      and len(b_sigs[0]["members"]) == 4, str([(s["kind"], len(s["members"])) for s in b_sigs]))
check("chanC (unrelated videos): nothing found",
      not [s for s in svc.state["signatures"].values() if s["folder"] == "chanC"])
count = len(svc.state["signatures"])
svc.scan_folder("lib", "chanA")
check("a second scan makes no duplicates", len(svc.state["signatures"]) == count)

# ---- cutting: chanA intro ----------------------------------------------------------------------------
v1, link = os.path.join(A, "v1.mp4"), os.path.join(LIB, "share", "v1_link.mp4")
ino_before = os.stat(v1).st_ino
svc.approve(intro_a["id"])
check("approve queued the cut", ("apply", intro_a["id"]) in svc.jobs)
svc.apply_signature(intro_a["id"])
mem = intro_a["members"]
states = {rel: m["state"] for rel, m in mem.items()}
check("5 videos cut, the one without a keyframe skipped",
      sorted(states.values()) == ["cut"] * 5 + ["skipped"], str(states))
check("the skipped one says why and is untouched",
      "no keyframe" in mem["chanA/v6_sparse.mp4"]["note"]
      and sha(os.path.join(A, "v6_sparse.mp4")) == orig_hash[os.path.join("chanA", "v6_sparse.mp4")])
v2 = os.path.join(A, "v2.mp4")
cut_d = duration(v2)
check("the cut is about 3 s shorter", 2.5 <= orig_durations[v2] - cut_d <= 5.0, f"{orig_durations[v2]:.2f} -> {cut_d:.2f}")
check("same streams and codecs (h264 + aac)", codecs(v2) == [("audio", "aac"), ("video", "h264")], str(codecs(v2)))
check("stream copy: every video packet is one of the original's, unchanged",
      is_suffix(packet_sizes(v2), orig_packets[v2]) and len(packet_sizes(v2)) < len(orig_packets[v2]))
enc = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format_tags=encoder", "-of", "csv=p=0", v2],
                     capture_output=True, text=True).stdout
check("no encoder was run (container writer only)", "x264" not in enc and "Lavc" not in enc, enc)
head = open(v2, "rb").read(1 << 16)
check("mp4 keeps faststart (moov before mdat)", b"moov" in head and (b"mdat" not in head or head.find(b"moov") < head.find(b"mdat")))
after_intro = packet_sizes(v2)
check("decodes cleanly end to end",
      subprocess.run(["ffmpeg", "-v", "error", "-i", v2, "-f", "null", "-"], capture_output=True).stderr == b"")
check("hard link: same inode, same new bytes under both names",
      os.stat(v1).st_ino == ino_before == os.stat(link).st_ino and os.stat(v1).st_nlink == 2
      and sha(v1) == sha(link) != orig_hash[os.path.join("chanA", "v1.mp4")])
bak = mem["chanA/v1.mp4"].get("backup")
check("the original is kept under .cut_originals with its exact bytes",
      bak and bak.startswith(os.path.join(LIB, video_trim.ORIGINALS_DIR)) and sha(bak) == orig_hash[os.path.join("chanA", "v1.mp4")])
check("the media caches were invalidated", len(invalidations) >= 1)
listing = [s for s in svc.list_signatures() if s["id"] == intro_a["id"]][0]
check("listing shows originals kept and their size", listing["originals_kept"] == 5 and listing["originals_bytes"] > 0)

# ---- an outro on top of the cut intro ---------------------------------------------------------------
svc.approve(outro_a["id"])
svc.apply_signature(outro_a["id"])
both = duration(v2)
check("outro cut: shorter again, still h264 + aac", orig_durations[v2] - both >= 5.0 and codecs(v2) == [("audio", "aac"), ("video", "h264")],
      f"{both:.2f}")
check("outro cut is a prefix of the intro-cut packets", is_prefix(packet_sizes(v2), after_intro)
      and len(packet_sizes(v2)) < len(after_intro))
check("both cuts leave the sparse video's outro cut and intro skipped",
      outro_a["members"]["chanA/v6_sparse.mp4"]["state"] == "cut")

# ---- undo, in the right order ----------------------------------------------------------------------------
res = svc.undo_signature(intro_a["id"])
check("undoing the intro first is refused while the outro cut sits on top (v1-v5)",
      res["restored"] == 0 and "changed after the cut" in mem["chanA/v2.mp4"]["note"], str(res))
svc.undo_signature(outro_a["id"])
res = svc.undo_signature(intro_a["id"])
check("undo outro then intro restores 5 + 5", res["restored"] == 5, str(res))
check("undo restores the original bytes exactly (hash), also under the hard link name",
      all(sha(os.path.join(A, n)) == orig_hash[os.path.join("chanA", n)] for n in os.listdir(A) if n.endswith(".mp4"))
      and sha(link) == orig_hash[os.path.join("chanA", "v1.mp4")])
check("the kept copies are gone after the restore",
      not os.path.exists(os.path.join(LIB, video_trim.ORIGINALS_DIR, intro_a["id"], "chanA", "v1.mp4")))
svc.approve(intro_a["id"])
svc.apply_signature(intro_a["id"])
check("an undone video is not cut again", all(m["state"] in ("undone", "skipped") for m in mem.values())
      and sha(v1) == orig_hash[os.path.join("chanA", "v1.mp4")])

# ---- webm, delete originals now ---------------------------------------------------------------------------
b = b_sigs[0]
w1 = os.path.join(LIB, "chanB", "w1.webm")
svc.approve(b["id"])
svc.apply_signature(b["id"])
check("webm: all 4 cut by stream copy (vp9 + opus)", [m["state"] for m in b["members"].values()] == ["cut"] * 4
      and codecs(w1) == [("audio", "opus"), ("video", "vp9")], str({r: (m["state"], m["note"]) for r, m in b["members"].items()}))
check("webm: video packets are a suffix of the original's", is_suffix(packet_sizes(w1), orig_packets[w1]))
res = svc.delete_originals(b["id"])
check("delete originals now removes the kept copies", res["deleted"] == 4
      and not os.path.exists(os.path.join(LIB, video_trim.ORIGINALS_DIR, b["id"]))
      and svc.undo_signature(b["id"])["restored"] == 0)
check("the cut stays in place after the originals went", abs(duration(w1) - (orig_durations[w1] - 3)) < 1.6)

# ---- auto-delete, rejected, a new video ----------------------------------------------------------------------
svc.scan_folder("lib", "chanE")
svc.scan_folder("lib", "chanF")
e = next(s for s in svc.state["signatures"].values() if s["folder"] == "chanE")
f = next(s for s in svc.state["signatures"].values() if s["folder"] == "chanF")
svc.set_auto_delete(e["id"], True)
svc.approve(e["id"])
svc.apply_signature(e["id"])
check("auto-delete: cut and no original kept", [m["state"] for m in e["members"].values()] == ["cut"] * 3
      and not any(m.get("orig_kept") for m in e["members"].values())
      and not os.path.exists(os.path.join(LIB, video_trim.ORIGINALS_DIR, e["id"])))
svc.reject(f["id"])
svc.apply_signature(f["id"])
check("a rejected signature cuts nothing", all(sha(os.path.join(LIB, "chanF", n)) == orig_hash[os.path.join("chanF", n)]
                                               for n in os.listdir(os.path.join(LIB, "chanF"))))
gen(os.path.join(LIB, "chanE", "e4.mp4"), 34, outro=False)
before = len(svc.state["signatures"])
svc.scan_folder("lib", "chanE")
e4 = e["members"].get("chanE/e4.mp4")
check("a new video joins the approved signature and is cut by the scan",
      e4 and e4["state"] == "cut" and len(svc.state["signatures"]) == before
      and duration(os.path.join(LIB, "chanE", "e4.mp4")) < 20.5, str(e4))
svc.scan_folder("lib", "chanB")
check("after the cuts a rescan finds nothing new", len(svc.state["signatures"]) == before)

# ---- state survives a restart; recovery of an interrupted write -----------------------------------------------------
svc2 = video_trim.VideoTrim(STATE, lambda: video_trim.DEFAULTS, lambda: False, lambda: [{"id": "lib", "path": LIB}],
                            lambda: None, log=lambda t: None)
check("the state file is reloaded", set(svc2.state["signatures"]) == set(svc.state["signatures"]))
sig2 = svc2.state["signatures"][intro_a["id"]]
m2 = sig2["members"]["chanA/v3.mp4"]
bad = os.path.join(A, "v3.mp4")
svc2._cut_one(sig2, "chanA/v3.mp4", m2, svc2.cfg())            # real cut, original kept (auto-delete is off here)
m2.update(state="committing")                                  # as if the process died after the backup
with open(bad, "r+b") as fh:
    fh.truncate(1000)
svc2.recover()
check("recovery puts the original back into a half-written file", sha(bad) == orig_hash[os.path.join("chanA", "v3.mp4")]
      and m2["state"] == "error")

# ---- daily trigger and the download hook -----------------------------------------------------------------------------
svc3 = new_service(daily_time="05:00")
noon = time.mktime(time.strptime("2026-10-18 12:00", "%Y-%m-%d %H:%M"))
early = time.mktime(time.strptime("2026-10-18 04:30", "%Y-%m-%d %H:%M"))
cfg = svc3.cfg()
check("daily pass is due after 05:00, not before, once a day",
      svc3._daily_due(cfg, noon) and not svc3._daily_due(cfg, early)
      and not (svc3.state.update(daily_day="2026-10-18") or svc3._daily_due(cfg, noon)))
svc3.state["daily_day"] = None
svc3.queue_dirty_folders()
scans = sorted(j[2] for j in svc3.jobs if j[0] == "scan")
check("the daily walk queues folders with enough videos, skips dot-folders",
      scans == ["chanA", "chanB", "chanC", "chanE", "chanF"], str(scans))
svc3.jobs.clear()
check("after_download queues the folders of new videos only",
      svc3.enqueue_paths([{"root": "lib", "path": "chanA/new.mp4"}, {"root": "lib", "path": "chanA/x.jpg"}]) == 1
      and svc3.jobs == [("scan", "lib", "chanA")])
svc3.jobs.clear()
off = new_service(after_download=False)
check("after_download off queues nothing", off.enqueue_paths([{"root": "lib", "path": "chanA/new.mp4"}]) == 0)
check("disabled queues nothing", new_service(enabled=False).enqueue_paths([{"root": "lib", "path": "chanA/n.mp4"}]) == 0)
os.makedirs(os.path.join(tmp, "busy_state"))
busy = video_trim.VideoTrim(os.path.join(tmp, "busy_state"), lambda: video_trim.DEFAULTS, lambda: True,
                            lambda: [{"id": "lib", "path": LIB}], lambda: None, log=lambda t: None)
check("a scan stands back while downloads run", busy.scan_folder("lib", "chanC") == {"deferred": True})

# ---- the staged server.py ------------------------------------------------------------------------------------------------
src = open(os.path.join(STAGE, "server.py"), encoding="utf-8").read()
tree = ast.parse(src)
check("server: import, DEFAULT_CONFIG block, config merge, hook, start",
      "import video_trim\n" in src and '"video_trim": dict(video_trim.DEFAULTS)' in src
      and re.search(r'"video_trim",?\s*(?:"[a-z_]+"\s*)?\)\s*and isinstance', src) and "_video_trim.enqueue_paths(new_paths)" in src
      and "\n_video_trim.start()\n" in src)
routes = re.findall(r'@app\.route\("(/api/media/trim[^"]*)", methods=\["(\w+)"\]\)\n@require_admin', src)
check("server: every trim route is admin-only",
      sorted(r[0] for r in routes) == sorted([
          "/api/media/trim", "/api/media/trim/<sid>/preview", "/api/media/trim/<sid>/frame/<int:index>",
          "/api/media/trim/<sid>/approve", "/api/media/trim/<sid>/reject", "/api/media/trim/<sid>/undo",
          "/api/media/trim/<sid>/delete-originals", "/api/media/trim/<sid>/auto-delete", "/api/media/trim/run"]),
      str(routes))
ns = {"re": re}
for node in tree.body:
    if isinstance(node, ast.FunctionDef) and node.name == "_coerce_setting":
        exec(compile(ast.Module([node], []), "server", "exec"), ns)
coerce = ns["_coerce_setting"]
time_field = {"type": "time"}
def raises(fn):
    try:
        fn()
    except ValueError:
        return True
    return False


check("settings: a time field takes HH:MM or empty and refuses the rest",
      coerce(time_field, "05:00") == "05:00" and coerce(time_field, "") == ""
      and all(raises(lambda v=v: coerce(time_field, v)) for v in ("5:00", "24:00", "05:60", 5, None)))
schema_keys = set(re.findall(r'"key": "(video_trim\.[a-z_]+)"', src))
check("settings: schema keys are all defaults", schema_keys <= {"video_trim." + k for k in video_trim.DEFAULTS}
      and "video_trim.daily_time" in schema_keys, str(schema_keys))

# ---- the routes' bodies, from the staged server.py's AST, on a stub app ------------------------------------------------
from flask import Flask, jsonify, request, send_file     # noqa: E402
stub = Flask("trim_check")
rns = {"app": stub, "require_admin": lambda fn: fn, "jsonify": jsonify, "request": request,
       "send_file": send_file, "_video_trim": svc,
       # the redo status the list route reports (2026-10-18): stubs, the real ones are checked by trim_redo_check.py
       "_interp_redo": type("R", (), {"status": staticmethod(lambda: {"queued": 0})})(),
       "stereo3d_service": lambda: type("S", (), {"redo_counts": staticmethod(lambda: {"queued": 0})})()}
for node in tree.body:
    if isinstance(node, ast.FunctionDef) and (node.name.startswith("video_trim_") or node.name == "_video_trim_action")             and node.name not in ("video_trim_settings", "video_trim_roots"):
        exec(compile(ast.Module([node], []), "server", "exec"), rns)
client = stub.test_client()
sid = intro_a["id"]
listing = client.get("/api/media/trim").get_json()
check("route: list has the signatures with originals and errors fields",
      sid in {x["id"] for x in listing["signatures"]} and "originals_bytes" in listing["signatures"][0]
      and "last_pass" in listing["status"] and listing["redo"]["interp"] == {"queued": 0})
check("route: preview JSON and a JPEG frame",
      client.get(f"/api/media/trim/{sid}/preview").get_json()["frames"] == [0, 1, 2, 3]
      and client.get(f"/api/media/trim/{sid}/frame/0").data[:3] == bytes([255, 216, 255])
      and client.get(f"/api/media/trim/{sid}/frame/9").status_code == 404
      and client.get("/api/media/trim/nope/preview").status_code == 404)
check("route: approve, reject, auto-delete (bool only), undo, delete-originals, run",
      client.post(f"/api/media/trim/{sid}/auto-delete", json={"value": "yes"}).status_code == 400
      and client.post(f"/api/media/trim/{sid}/auto-delete", json={"value": True}).get_json()["ok"]
      and svc.state["signatures"][sid]["auto_delete"] is True
      and client.post(f"/api/media/trim/{sid}/reject").get_json()["ok"] and svc.state["signatures"][sid]["status"] == "rejected"
      and client.post(f"/api/media/trim/{sid}/approve").get_json()["ok"] and svc.state["signatures"][sid]["status"] == "approved"
      and client.post(f"/api/media/trim/{sid}/undo").get_json()["ok"] and ("undo", sid) in svc.jobs
      and client.post(f"/api/media/trim/{sid}/delete-originals").get_json()["ok"] and ("delete", sid) in svc.jobs
      and client.post("/api/media/trim/nope/approve").status_code == 404
      and client.post("/api/media/trim/run", json={}).get_json()["started"])
for thread in threading.enumerate():
    if thread.name == "video-trim-walk":
        thread.join(30)

print("ALL PASS" if ok else "SOME FAILED", flush=True)
sys.exit(0 if ok else 1)
