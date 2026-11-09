"""What a cut (or an undo) redoes: the 3D work (stereo3d.py, lane "redo") and the Smooth
motion renditions (interp_redo.py + the server's helpers), against a temp 3D cache, a temp
"library" and a FAKE vision worker (urlopen replaced, like interp_server_check.py).

User, 2026-10-18: "when a video is intro/outro is cut for non-new downloads, re-do the
interpolation and 3D work."

  venv/bin/python trim_redo_check.py /tmp/.../server.py

stereo3d.py, interp_redo.py and video_trim.py are imported from next to the server file; the
server's functions come out of its AST. Nothing outside the temp dir is written.
"""
import ast
import io
import json
import os
import shutil
import sys
import tempfile
import time
import urllib.error

SERVER = os.path.abspath(sys.argv[1])
sys.path.insert(0, os.path.dirname(SERVER))
if os.path.isdir("/data2/ksf_web_dl"):
    sys.path.append("/data2/ksf_web_dl")            # only for the imports the AST loader tries

import interp_redo  # noqa: E402
import stereo3d  # noqa: E402
import video_trim  # noqa: E402

TMP = tempfile.mkdtemp(prefix="trim_redo_check_")
LIB = os.path.join(TMP, "lib")
CACHE = os.path.join(TMP, "cache3d")
os.makedirs(LIB)
os.makedirs(CACHE)
RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append(bool(ok))
    print(("PASS " if ok else "FAIL ") + name + (f" - {detail}" if detail and not ok else ""))


# ------------------------------------------------------------------ the server's functions
tree = ast.parse(open(SERVER, encoding="utf-8").read())
defs = {}
ns = {"__name__": "harness", "__file__": SERVER}
for node in tree.body:
    if isinstance(node, (ast.Import, ast.ImportFrom)):
        try:
            exec(compile(ast.Module(body=[node], type_ignores=[]), SERVER, "exec"), ns)
        except Exception:
            pass
    elif isinstance(node, (ast.FunctionDef, ast.ClassDef)):
        defs[node.name] = node
    elif isinstance(node, ast.Assign):
        for t in node.targets:
            if isinstance(t, ast.Name):
                defs.setdefault(t.id, node)


def load(name):
    code = compile(ast.Module(body=[defs[name]], type_ignores=[]), SERVER, "exec")
    exec(code, ns)


for n in ("_interp_worker", "_interp_worker_request", "interp_worker_status", "_interp_upload_locks",
          "_interp_upload_locks_guard", "interp_ensure_uploaded", "_interp_seg_fetch_raw",
          "_interp_seg_fetch_wait", "INTERP_DOWNLOAD_SEG_TIMEOUT_S", "INTERP_MAX_RATIO", "INTERP_MIN_RATIO",
          "INTERP_OUT_FPS", "interp_output_rate", "interp_open_rendition", "interp_segment_stream",
          "interp_key_for", "_interp_redo_variants", "_interp_redo_run", "video_trim_on_cut"):
    load(n)
ns["INTERP_DOWNLOAD_SEG_TIMEOUT_S"] = 0.3
ns["vision_worker_address"] = lambda: ("http://worker:8410", "tok")
ns["interp_settings"] = lambda: {"enabled": True, "max_height": 2160, "max_output_fps": 240}
ns["classify_media_path"] = lambda p: "video"
FPS = {"value": 25}
ns["probe_media_details"] = lambda p: {"fps": FPS["value"]}
ns["resolve_media_path"] = lambda root, rel: os.path.join(LIB, rel) if os.path.isfile(os.path.join(LIB, rel)) else None


# ------------------------------------------------------------------ the fake vision worker
class Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


TOTAL = 5                       # segments of the fake video


class FakeWorker:
    def __init__(self):
        self.down = False
        self.busy = False
        self.variants = {}                  # old key -> [{variant, segments, complete}]
        self.uploaded = set()
        self.requests = []                  # (key, variant, name)
        self.asked = []                     # keys asked for variants

    def urlopen(self, req, timeout=None):
        url = req.full_url
        if self.down:
            raise urllib.error.URLError("connection refused")
        path = url.split("worker:8410", 1)[1]
        if path.startswith("/v1/interp/status"):
            key = path.split("key=")[1] if "key=" in path else None
            body = {"available": True, "missing": []}
            if key:
                body["cached"] = key in self.uploaded
            return Resp(json.dumps(body).encode())
        if path.startswith("/v1/interp/source/"):
            req.data.read()
            self.uploaded.add(path.rsplit("/", 1)[1])
            return Resp(b"{}")
        if path.startswith("/v1/interp/variants/"):
            key = path.rsplit("/", 1)[1]
            self.asked.append(key)
            return Resp(json.dumps(self.variants.get(key, [])).encode())
        if path.startswith("/v1/interp/seg/"):
            _, _, _, _, key, variant, name = path.split("?")[0].split("/")
            self.requests.append((key, variant, name))
            if self.busy:
                raise urllib.error.HTTPError(url, 503, "still being made", {},
                                             io.BytesIO(b'{"error": "still being made", "pending": true}'))
            if name == "meta.json":
                body = b'{"segments": %d}' % TOTAL
            elif name == "end.json":
                body = b'{"last_segment": %d}' % (TOTAL - 1)
            elif name == "init.mp4":
                body = b"INIT"
            else:
                idx = int(name[4:9])
                if idx >= TOTAL:
                    raise urllib.error.HTTPError(url, 404, "no", {}, io.BytesIO(b'{"error": "no such segment"}'))
                body = b"SEG%05d" % idx
            r = Resp(body)
            r.headers = {"Content-Type": "application/octet-stream"}
            return r
        raise AssertionError("unexpected url " + url)

    def segs(self, key, variant):
        return sorted(n for k, v, n in self.requests if k == key and v == variant and n.startswith("seg_"))


worker = FakeWorker()
ns["urllib"].request.urlopen = worker.urlopen


# ------------------------------------------------------------------ the library and the 3D cache
def write_video(rel, data, mtime=None):
    full = os.path.join(LIB, rel)
    os.makedirs(os.path.dirname(full), exist_ok=True)
    with open(full, "wb") as fh:
        fh.write(data)
    if mtime:
        os.utime(full, (mtime, mtime))
    return full


def stat_version(full):
    return video_trim.file_version(os.stat(full))


S3D = {"enabled": True, "cache_dir": CACHE, "cache_max_gb": 150, "divergence": 2.0, "convergence": 0.5,
       "max_eye_long_side": 2160, "mbps_1080": 30, "mbps_1440": 50, "mbps_2160": 80}
FAVS = set()
svc = stereo3d.Service(os.path.join(TMP, "queue.json"), lambda: S3D,
                       lambda root, path: os.path.join(LIB, path) if os.path.isfile(os.path.join(LIB, path)) else None,
                       lambda: ("http://127.0.0.1:1", ""),
                       favourite_fn=lambda root, path: (root, path) in FAVS)
ns["stereo3d_service"] = lambda: svc
PID = stereo3d.profile_for("video", S3D)[1]


def add_3d_entry(rel, ver):
    key = stereo3d.item_key(ver, PID)
    base = os.path.join(CACHE, stereo3d.cache_rel("video", key))
    os.makedirs(base)
    for n in stereo3d.RESULT_NAMES["video"]:
        with open(os.path.join(base, n), "wb") as fh:
            fh.write(b"x")
    idx = svc.index()
    idx.put(key, {"kind": "video", "root": "downloads", "path": rel, "version": ver, "profile": PID,
                  "created": time.time(), "bytes": 3})
    idx.save()
    return key


def recut(rel, data):
    """The cut: new bytes written into the same inode, mtime moved on."""
    full = os.path.join(LIB, rel)
    old = stat_version(full)
    with open(full, "r+b") as fh:
        fh.write(data)
        fh.truncate()
    os.utime(full, (old["mtime"] + 100, old["mtime"] + 100))
    return old, stat_version(full)


# ------------------------------------------------------------------ 3D
write_video("a/one.mp4", b"o" * 5000, 1_700_000_000)
old, new = None, None
ver0 = stat_version(os.path.join(LIB, "a/one.mp4"))
k_old = add_3d_entry("a/one.mp4", ver0)
old, new = recut("a/one.mp4", b"c" * 3000)
res = svc.redo_after_cut("downloads", "a/one.mp4", old)
k_new = stereo3d.item_key(new, PID)
it = svc.queue.items.get(k_new)
check("3D: a cut file with a 3D entry is re-queued in the redo lane",
      res["status"] == "requeued" and it and it["lane"] == stereo3d.LANE_REDO and it["state"] == "queued", res)
check("3D: the old cache entry and its files are deleted",
      svc.index().get(k_old) is None and not os.path.exists(os.path.join(CACHE, stereo3d.cache_rel("video", k_old))))
check("3D: status_for shows the redo lane", svc.status_for("downloads", "a/one.mp4").get("lane") == "redo")
check("3D: redo counts", svc.redo_counts()["queued"] == 1 and svc.redo_counts()["requeued_total"] == 1)

# no 3D at all: a brand-new download cut right away
write_video("a/fresh.mp4", b"f" * 4000, 1_700_000_000)
old, new = recut("a/fresh.mp4", b"c" * 1000)
n_before = len(svc.queue.items)
res = svc.redo_after_cut("downloads", "a/fresh.mp4", old)
check("3D: a file with no 3D gets nothing", res["status"] == "none" and len(svc.queue.items) == n_before, res)

# a favourite goes to the favourites lane, with the old fav_at
write_video("a/fav.mp4", b"v" * 4000, 1_700_000_000)
ver = stat_version(os.path.join(LIB, "a/fav.mp4"))
FAVS.add(("downloads", "a/fav.mp4"))
add_3d_entry("a/fav.mp4", ver)
old, new = recut("a/fav.mp4", b"c" * 1500)
res = svc.redo_after_cut("downloads", "a/fav.mp4", old)
it = svc.queue.items.get(stereo3d.item_key(new, PID))
check("3D: a favourite uses the favourite lane", res["status"] == "requeued" and it and it["lane"] == "favorite", res)

# a queued old item (failed) with no cache entry: re-queued, old item dropped
write_video("a/failed.mp4", b"x" * 4000, 1_700_000_000)
ver = stat_version(os.path.join(LIB, "a/failed.mp4"))
svc.request("downloads", "a/failed.mp4", stereo3d.LANE_MANUAL, "bob")
k_f_old = stereo3d.item_key(ver, PID)
svc.queue.update(k_f_old, state="failed", error="boom")
old, new = recut("a/failed.mp4", b"c" * 2000)
res = svc.redo_after_cut("downloads", "a/failed.mp4", old)
check("3D: an old failed queue item is replaced by one for the cut file",
      res["status"] == "requeued" and k_f_old not in svc.queue.items
      and stereo3d.item_key(new, PID) in svc.queue.items, res)

# the redo lane is the lowest
write_video("a/man.mp4", b"m" * 100, 1_700_000_000)
svc.request("downloads", "a/man.mp4", stereo3d.LANE_MANUAL, "carol")
order = [i["lane"] for i in svc.queue.ordered()]
check("3D: order is manual, then favourite, then redo",
      order.index("manual") < order.index("favorite") < order.index("redo") and order[-1] == "redo", order)

# an undo is the same situation
write_video("a/undo.mp4", b"u" * 3000, 1_700_000_000)
ver = stat_version(os.path.join(LIB, "a/undo.mp4"))
add_3d_entry("a/undo.mp4", ver)
old, new = recut("a/undo.mp4", b"c" * 1000)
svc.redo_after_cut("downloads", "a/undo.mp4", old)
old2, new2 = recut("a/undo.mp4", b"u" * 3000)             # the bytes written back
res = svc.redo_after_cut("downloads", "a/undo.mp4", old2)
check("3D: after an undo the version of the cut is dropped and the restored one queued",
      res["status"] == "requeued" and stereo3d.item_key(old2, PID) not in svc.queue.items
      and stereo3d.item_key(new2, PID) in svc.queue.items, res)

# ------------------------------------------------------------------ Smooth motion
clock = [1000.0]
busy = [False]
redo = interp_redo.InterpRedo(os.path.join(TMP, "interp_redo.json"), lambda: busy[0], ns["_interp_redo_variants"],
                              lambda r, p, spec, stop: ns["_interp_redo_run"](r, p, spec, stop),
                              log=lambda t: None, clock=lambda: clock[0])
ns["_interp_redo"] = redo
FULL = {"variant": "m2_h2160_r20", "segments": TOTAL, "complete": True}
PART = {"variant": "f120_h2160_r20", "segments": 2, "complete": False}
OTHER_H = {"variant": "m2_h1080_r20", "segments": TOTAL, "complete": True}

write_video("b/clip.mp4", b"o" * 6000, 1_700_000_000)
full = os.path.join(LIB, "b/clip.mp4")
old, new = recut("b/clip.mp4", b"c" * 2500)
old_key = ns["interp_key_for"]("downloads", "b/clip.mp4", old["size"], old["mtime"])
new_key = ns["interp_key_for"]("downloads", "b/clip.mp4", new["size"], new["mtime"])
worker.variants[old_key] = [FULL, PART, OTHER_H]
ns["video_trim_on_cut"]("downloads", "b/clip.mp4", old, new)
check("interp: the worker was asked about the OLD key", worker.asked == [old_key], worker.asked)
job = redo.jobs.get("downloads\nb/clip.mp4")
check("interp: a redo job holds the variants", job and len(job["variants"]) == 3, job)

busy[0] = True
ran = redo.step()
check("interp: redo waits while downloads are busy", ran is False and not worker.requests)
busy[0] = False
ran = redo.step()
check("interp: the complete variant is generated in full (every segment)",
      ran and worker.segs(new_key, FULL["variant"]) == ["seg_%05d.m4s" % i for i in range(TOTAL)],
      worker.segs(new_key, FULL["variant"]))
check("interp: the partial variant gets the same number of leading segments",
      worker.segs(new_key, PART["variant"]) == ["seg_00000.m4s", "seg_00001.m4s"], worker.segs(new_key, PART["variant"]))
check("interp: a variant of another max height is skipped",
      not any(v == OTHER_H["variant"] for _, v, _ in worker.requests))
check("interp: the source was uploaded under the NEW key only", worker.uploaded == {new_key}, worker.uploaded)
check("interp: the job is gone when done", not redo.jobs and redo.finished == 1)

# nothing existed (a new download cut right away)
write_video("b/new.mp4", b"o" * 6000, 1_700_000_000)
old, new = recut("b/new.mp4", b"c" * 2500)
worker.requests.clear()
ns["video_trim_on_cut"]("downloads", "b/new.mp4", old, new)
check("interp: no variants, no job", not redo.jobs and not worker.requests)

# the worker is away at the cut: asked again later, with back-off
write_video("b/away.mp4", b"o" * 6000, 1_700_000_000)
old, new = recut("b/away.mp4", b"c" * 2500)
old_key = ns["interp_key_for"]("downloads", "b/away.mp4", old["size"], old["mtime"])
new_key = ns["interp_key_for"]("downloads", "b/away.mp4", new["size"], new["mtime"])
worker.variants[old_key] = [FULL]
worker.down = True
ns["video_trim_on_cut"]("downloads", "b/away.mp4", old, new)
job = redo.jobs.get("downloads\nb/away.mp4")
check("interp: worker unreachable at the cut -> the old key is kept", job and job["old_keys"] == [old_key], job)
redo.step()
job = redo.jobs["downloads\nb/away.mp4"]
check("interp: unreachable worker -> back-off, job kept",
      job["attempts"] == 1 and job["state"] == "queued" and job["not_before"] > clock[0], job)
worker.down = False
check("interp: nothing runs before the back-off is over", redo.step() is False)
clock[0] += 120
worker.requests.clear()
redo.step()
check("interp: after the back-off the lookup and the redo go through",
      "b/away.mp4" not in "".join(redo.jobs) and len(worker.segs(new_key, FULL["variant"])) == TOTAL)

# the worker answers 503 busy: back off, no loss
write_video("b/busy.mp4", b"o" * 6000, 1_700_000_000)
old, new = recut("b/busy.mp4", b"c" * 2500)
old_key = ns["interp_key_for"]("downloads", "b/busy.mp4", old["size"], old["mtime"])
worker.variants[old_key] = [FULL]
ns["video_trim_on_cut"]("downloads", "b/busy.mp4", old, new)
worker.busy = True
redo.step()
job = redo.jobs["downloads\nb/busy.mp4"]
check("interp: 503 busy from the worker -> back-off", job["attempts"] == 1 and job["state"] == "queued", job)
worker.busy = False
clock[0] += 120
redo.step()
check("interp: ... and done on the next try", "downloads\nb/busy.mp4" not in redo.jobs)

# a newer cut merges into the waiting job; the file lists survive a restart
redo.schedule("downloads", "b/m.mp4", "k1", [FULL])
redo.schedule("downloads", "b/m.mp4", "k2", [PART, {"variant": FULL["variant"], "segments": 3, "complete": False}])
job = redo.jobs["downloads\nb/m.mp4"]
check("interp: one job per file; a newer cut merges (complete beats partial)",
      len([j for j in redo.jobs.values() if j["path"] == "b/m.mp4"]) == 1
      and {v["variant"]: v["complete"] for v in job["variants"]} == {FULL["variant"]: True, PART["variant"]: False}, job)
again = interp_redo.InterpRedo(redo.path, lambda: False, None, None, log=lambda t: None)
check("interp: jobs persist in interp_redo.json", "downloads\nb/m.mp4" in again.jobs)

# variant strings -> request args
p1, p2 = interp_redo.parse_variant("m4_h2160_r0"), interp_redo.parse_variant("f119.88_h1080_r12.5")
check("interp: variant strings parse back to request args",
      p1["args"] == {"m": "4"} and p2["args"] == {"o": "119.88"} and p2["mbps"] == 12.5 and p2["max_height"] == 1080
      and interp_redo.parse_variant("bogus") is None)

# ------------------------------------------------------------------ the hook in video_trim.py (cut and undo)
calls = []
roots = [{"id": "downloads", "path": LIB}]
vt = video_trim.VideoTrim(TMP, lambda: {}, lambda: False, lambda: roots, lambda: None, log=lambda t: None,
                          state_name="trim_state.json",
                          on_cut=lambda root, rel, o, n: (calls.append((root, rel, o, n)),
                                                          ns["video_trim_on_cut"](root, rel, o, n)))
rel = "c/real.mp4"
full = write_video(rel, b"O" * 8000, 1_700_000_000)
ver = stat_version(full)
add_3d_entry(rel, ver)
worker.variants[ns["interp_key_for"]("downloads", rel, ver["size"], ver["mtime"])] = [FULL]
cut_src = os.path.join(TMP, "cut_bytes.mp4")
with open(cut_src, "wb") as fh:
    fh.write(b"C" * 5000)
sig = {"id": "sig1", "root": "downloads", "folder": "c", "kind": "intro", "length_s": 5, "status": "approved",
       "auto_delete": False, "created": time.time(), "members": {}}
m = {"state": "queued"}
sig["members"][rel] = m
vt.state["signatures"]["sig1"] = sig
state, note = vt._commit(sig, rel, m, full, os.stat(full), cut_src, video_trim.merged_settings({}))
check("trim hook: a commit calls on_cut with the old and the new version",
      state == "cut" and len(calls) == 1 and calls[0][2]["size"] == 8000 and calls[0][3]["size"] == 5000
      and calls[0][2]["ino"] == calls[0][3]["ino"], (state, note, calls))
check("trim hook: 3D and Smooth motion were redone for the cut",
      stereo3d.item_key(calls[0][3], PID) in svc.queue.items and "downloads\nc/real.mp4" in redo.jobs)
m["state"] = "cut"
n_calls = len(calls)
vt.undo_signature("sig1")
check("trim hook: an undo calls on_cut too (cut version -> restored version)",
      len(calls) == n_calls + 1 and calls[-1][2]["size"] == 5000 and calls[-1][3]["size"] == 8000, calls[-1:])
check("trim hook: ... and re-queues 3D for the restored bytes",
      stereo3d.item_key(calls[-1][3], PID) in svc.queue.items
      and stereo3d.item_key(calls[-1][2], PID) not in svc.queue.items)

# an exception in the hook never fails the cut
boom = video_trim.VideoTrim(TMP, lambda: {}, lambda: False, lambda: roots, lambda: None, log=lambda t: None,
                            state_name="trim_state2.json", on_cut=lambda *a: 1 / 0)
full2 = write_video("c/boom.mp4", b"O" * 8000, 1_700_000_000)
sig2 = {"id": "sig2", "root": "downloads", "folder": "c", "kind": "intro", "length_s": 5, "status": "approved",
        "auto_delete": False, "created": time.time(), "members": {}}
state, note = boom._commit(sig2, "c/boom.mp4", {"state": "queued"}, full2, os.stat(full2), cut_src,
                           video_trim.merged_settings({}))
check("trim hook: an exception in on_cut is swallowed, the cut stands", state == "cut" and os.path.getsize(full2) == 5000)

shutil.rmtree(TMP, ignore_errors=True)
print("\n%d/%d passed" % (sum(RESULTS), len(RESULTS)))
sys.exit(0 if all(RESULTS) else 1)
