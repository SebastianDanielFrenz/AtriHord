#!/usr/bin/env python3
"""P2 acceptance checks for the 3D conversion API + scheduler in vision_worker.py (media-dl 2D->3D backend).

Runs on the vision host in the WORKER venv, against a staged vision_worker.py, with a fake driver
(fake_stereo_driver.py, next to this file: same CLI/files/exit codes as stereo_job.py, no GPU) - the running
service, the GPU and the real stereo venv are never touched:

  /data/ksf-vision/.venv/bin/python stereo_worker_check.py <vision_worker.py to test> [<stereo_job.py>]

genre/grain/upscaler are imported from $VISION_DIR (default /data/ksf-vision) like vision_lock_check.py does.
The manager thread is NOT started: every check drives vw.stereo_manage_once() itself, so the gate, the start, the
preemption and the reaping are deterministic.  Prints PASS/FAIL per case, exit code 1 on any FAIL.
"""
import hashlib
import importlib.util
import io
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
WORKER = os.path.abspath(sys.argv[1])
STEREO_JOB = os.path.abspath(sys.argv[2]) if len(sys.argv) > 2 else os.path.join(HERE, "stereo_job.py")
FAKE = os.path.join(HERE, "fake_stereo_driver.py")
if "def stereo_manage_once(" not in open(WORKER, encoding="utf-8").read():
    sys.exit(f"{WORKER} has no 3D manager (stereo_manage_once) - not imported")
sys.path.insert(0, os.environ.get("VISION_DIR", "/data/ksf-vision"))

import numpy as np  # noqa: E402
from PIL import Image  # noqa: E402

spec = importlib.util.spec_from_file_location("vision_worker_under_test", WORKER)
vw = importlib.util.module_from_spec(spec)
spec.loader.exec_module(vw)

TMP = tempfile.mkdtemp(prefix="stereo_worker_check_")
WORK = os.path.join(TMP, "work")
CTL = os.path.join(TMP, "ctl.json")
ICACHE = os.path.join(TMP, "interp")
os.makedirs(ICACHE)
STUB = os.path.join(TMP, "vidinterp_stub.sh")
with open(STUB, "w") as f:
    f.write("#!/bin/sh\nsleep 0.1\nprintf abc\n")
os.chmod(STUB, 0o755)
os.environ["FAKE_DRIVER_CTL"] = CTL
os.environ["LD_LIBRARY_PATH"] = "/leak/from/the/worker"          # must NOT reach the driver
os.environ["HF_HOME"] = "/leak/hf"

CFG = {}
GPU = {"free": 6000}
UNLOADS = []


def default_cfg():
    return {"token": "", "windows": [], "allow_urgent_anytime": True, "allow_both": False, "reserve_mb": 1000,
            "stereo_work_dir": WORK, "stereo_python": sys.executable, "stereo_driver": FAKE,
            "stereo_min_free_gb": 0, "stereo_abort_free_gb": 0, "stereo_idle_s": 1, "stereo_interp_idle_s": 1,
            "stereo_retry_s": 0.05, "stereo_kill_s": 30, "stereo_home": TMP,
            "genre_model_dir": TMP, "interp_bin": STUB, "interp_dll": STUB, "interp_cache_dir": ICACHE}


vw.config = lambda: CFG
vw.gpu_free_mb = lambda: GPU["free"]
vw.unload_models = lambda reason="": UNLOADS.append((reason, vw._lock.locked()))
vw.ensure_models = lambda **k: None
vw.tag = lambda images: [{}] * len(images)
vw.genre.load = lambda *a, **k: {"provider": "fake", "styles": ["a"]}
vw.genre.analyse = lambda model, audio: (np.array([0.5]), np.zeros(4, np.float32), 1)

client = vw.app.test_client()
ok_all = True
H = {}


def check(label, cond, detail=""):
    global ok_all
    ok_all = ok_all and bool(cond)
    print(("PASS " if cond else "FAIL ") + label + (f"  ({detail})" if detail and not cond else ""), flush=True)
    return bool(cond)


# ------------------------------------------------------------------ helpers
def key_of(label):
    return hashlib.sha256(label.encode()).hexdigest()[:32]


def put(key, size=1000, **q):
    body = (bytes(range(256)) * (size // 256 + 1))[:size]
    return client.put(f"/v1/stereo/source/{key}", data=body, query_string=q, headers=H)


def post(key, kind="video", **extra):
    return client.post("/v1/stereo/jobs", json={"key": key, "kind": kind, **extra}, headers=H)


def job(key):
    r = client.get(f"/v1/stereo/jobs/{key}", headers=H)
    return r.get_json() if r.status_code == 200 else {"state": "http%d" % r.status_code}


def status():
    return client.get("/v1/stereo/status", headers=H).get_json()


def state(key):
    return job(key)["state"]


def set_ctl(default=None, **per_key):
    d = {"default": default or {}}
    d.update(per_key)
    with open(CTL, "w") as f:
        json.dump(d, f)


def tick(cond, timeout=25.0, step=0.1):
    end = time.time() + timeout
    while time.time() < end:
        vw.stereo_manage_once()
        if cond():
            return True
        time.sleep(step)
    vw.stereo_manage_once()
    return bool(cond())


def open_gate():
    vw.STATE["paused"] = False
    vw.STATE["last_batch"] = time.time() - 1000
    vw.STATE["last_urgent"] = time.time() - 1000
    vw._stereo["vram_stuck_until"] = 0.0
    vw.STATE["last_interp"] = 0.0
    vw.STATE["interp_streaming"] = 0
    vw.STATE["upscale_waiting"] = 0
    vw._seg["want"].clear()
    GPU["free"] = 6000


def reset():
    act = vw._stereo["active"]
    if act:
        vw._stereo_kill(act)
        vw._stereo_wait(act, 5)
        vw.stereo_reap()
    CFG.clear()
    CFG.update(default_cfg())
    for k in list(vw.stereo_jobs()):
        client.delete(f"/v1/stereo/jobs/{k}", headers=H)
    open_gate()
    UNLOADS.clear()
    set_ctl()
    vw._stereo["housekept"] = time.time()


def new_job(label, kind="video", size=1000, ext="mkv", profile=None, **q):
    key = key_of(label + str(time.time()))
    r = put(key, size, ext=ext, **q)
    assert r.status_code == 200, r.get_data(as_text=True)
    r = post(key, kind, profile=profile, label=label)
    assert r.status_code == 201, r.get_data(as_text=True)
    return key


def wdir(key):
    return os.path.join(WORK, key)


def flog(key):
    try:
        return [ln.split(" ", 1)[1].strip() for ln in open(wdir(key) + "/fake_log.txt")]
    except OSError:
        return []


def running_chunk(key):
    p = (job(key).get("progress") or {})
    return p.get("chunk") or 0


def run_to_chunk(key, n, timeout=25.0):
    return tick(lambda: running_chunk(key) >= n, timeout)


def alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def analyze(urgent=True):
    buf = io.BytesIO()
    Image.new("RGB", (8, 8), (10, 20, 30)).save(buf, "PNG")
    return client.post("/v1/analyze", data={"file": (io.BytesIO(buf.getvalue()), "c.png"), "tasks": "tags",
                                            "urgent": "1" if urgent else ""}, content_type="multipart/form-data")


def audio(urgent=True):
    return client.post("/v1/audio" + ("?urgent=1" if urgent else ""), data=b"\0" * (2 * vw.genre.SAMPLE_RATE * 6),
                       content_type="application/octet-stream")


def ndrivers():
    r = subprocess.run(["pgrep", "-fc", "fake_stereo_driver.py"], capture_output=True, text=True)
    return int((r.stdout or "0").strip() or 0)


# ------------------------------------------------------------------ the cases
def case_upload():
    reset()
    k = key_of("upload")
    r = put(k, 5000, ext="mkv")
    check("upload: PUT stores the body, answers its length", r.status_code == 200 and r.get_json()["bytes"] == 5000
          and os.path.getsize(wdir(k) + "/src.mkv") == 5000, r.get_data(as_text=True))
    check("upload: the job is 'uploaded' with its size", job(k)["state"] == "uploaded" and job(k)["source_bytes"] == 5000)
    put(k, 700, ext="mp4")
    check("upload: a second PUT replaces it (new extension, old file gone)",
          os.path.exists(wdir(k) + "/src.mp4") and not os.path.exists(wdir(k) + "/src.mkv")
          and job(k)["source_bytes"] == 700, os.listdir(wdir(k)))
    k2 = key_of("short")
    r = client.put(f"/v1/stereo/source/{k2}", data=b"x" * 50, headers=H, environ_overrides={"CONTENT_LENGTH": "100"})
    check("upload: a body shorter than its Content-Length is refused (400), nothing is left behind",
          r.status_code == 400 and k2 not in vw.stereo_jobs()
          and not [n for n in (os.listdir(wdir(k2)) if os.path.isdir(wdir(k2)) else []) if n.endswith(".part")],
          (r.status_code, r.get_data(as_text=True)[:100]))
    # The shared helper itself: a stream that ends early reports the numbers.
    with vw.app.test_request_context(method="PUT", data=b"y" * 30):
        got, err = vw._receive_upload(os.path.join(TMP, "short.bin"), 40)
    check("upload: _receive_upload reports 'short upload (30 of 40 bytes)' and leaves no file",
          got is None and err == "short upload (30 of 40 bytes)" and not os.path.exists(os.path.join(TMP, "short.bin"))
          and not [n for n in os.listdir(TMP) if n.endswith(".part")], (got, err))
    with vw.app.test_request_context(method="PUT", data=b"z" * 40):
        got, err = vw._receive_upload(os.path.join(TMP, "full.bin"), 40)
    check("upload: _receive_upload of a complete body renames it into place",
          got == 40 and err is None and os.path.getsize(os.path.join(TMP, "full.bin")) == 40)
    r = client.put(f"/v1/stereo/source/{k2}", data=b"", headers=H)
    check("upload: an empty/unannounced body is refused (411)", r.status_code in (400, 411), r.status_code)
    CFG["stereo_min_free_gb"] = 10 ** 7
    k3 = key_of("full-disk")
    r = put(k3, 1000, ext="mkv")
    check("upload: 507 when free space would drop below stereo_min_free_gb, no job created",
          r.status_code == 507 and k3 not in vw.stereo_jobs() and "disk_free_gb" in r.get_json(), r.status_code)
    CFG["stereo_min_free_gb"] = 0
    check("upload: bad key 400", client.put("/v1/stereo/source/zz", data=b"x", headers=H).status_code == 400)
    check("upload: bad ext 400", put(key_of("e"), 10, ext="a/b").status_code == 400)
    r = post(k, "video")
    check("upload: after the job was submitted a PUT is refused (409)", r.status_code == 201
          and put(k, 10, ext="mp4").status_code == 409)


def case_submit_validation():
    reset()
    k = key_of("val")
    check("submit: source not uploaded -> 404 with upload:true", post(k).status_code == 404 and post(k).get_json()["upload"])
    put(k, 100, ext="mkv")
    check("submit: bad kind 400", post(k, "audio").status_code == 400)
    check("submit: unknown profile key 400", post(k, profile={"evil": 1}).status_code == 400)
    check("submit: bad profile string 400", post(k, profile={"method": "a b; rm"}).status_code == 400)
    check("submit: bad profile type 400", post(k, profile={"fps": {"x": 1}}).status_code == 400)
    check("submit: nothing was queued by the refusals", state(k) == "uploaded")
    # turn_cw (media-dl's turn of a video filmed on its side) is a profile key and reaches the job
    kt = key_of("turnkey")
    put(kt, 100, ext="mkv")
    rt = post(kt, profile={"turn_cw": 90}, label="turn")
    check("submit: turn_cw is an accepted profile key and is kept in the job",
          rt.status_code == 201 and vw.stereo_jobs()[kt]["profile"].get("turn_cw") == 90, rt.get_data(as_text=True)[:200])
    client.delete(f"/v1/stereo/jobs/{kt}", headers=H)        # keep the queue position checks below as they were
    # the driver adds it to the container rotation (pure function, no GPU)
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import stereo_job as SJ
    pr0 = {"w": 3840, "h": 2160, "fps": 30.0, "duration": 10, "video_duration": 10, "rotation_cw": 0, "hdr": False, "audio": []}
    pt = SJ.merge_profile('{"turn_cw": 90}')
    plan = SJ.make_plan(pr0, pt)
    check("driver: turn_cw 90 on an unflagged 3840x2160 file gives rotation 90 and a portrait eye",
          plan["rotation_cw"] == 90 and plan["eye_h"] > plan["eye_w"], plan)
    check("driver: the turn adds to a container rotation (90 + 90 = 180, landscape again)",
          SJ.make_plan(dict(pr0, rotation_cw=90), pt)["rotation_cw"] == 180)
    check("driver: no turn leaves the plan as before", SJ.make_plan(pr0, SJ.merge_profile(None))["rotation_cw"] == 0)
    try:
        SJ.make_plan(pr0, SJ.merge_profile('{"turn_cw": 45}'))
        bad = False
    except RuntimeError:
        bad = True
    check("driver: a turn that is not a quarter turn is refused", bad)
    ia = SJ.build_iw3_args.__code__.co_varnames       # the turn rides the existing rotation path of build_iw3_args
    check("driver: turn_cw is in DEFAULT_PROFILE (0)", SJ.DEFAULT_PROFILE.get("turn_cw") == 0)
    r = post(k, profile={"divergence": 2.5, "max_eye_long_side": 1920}, label="x")
    check("submit: 201 with state waiting_gpu and queue position 1", r.status_code == 201
          and r.get_json()["state"] == "waiting_gpu" and r.get_json()["queue_position"] == 1, r.get_data(as_text=True))
    r2 = post(k, profile={"divergence": 9})
    check("submit: idempotent - the same job comes back (200), its first profile kept",
          r2.status_code == 200 and r2.get_json()["state"] == "waiting_gpu"
          and vw.stereo_jobs()[k]["profile"]["divergence"] == 2.5)
    check("submit: chunk_s defaults from stereo_chunk_s", vw.stereo_jobs()[k]["profile"]["chunk_s"] == 20)


def case_lifecycle():
    reset()
    os.environ["LD_LIBRARY_PATH"] = "/leak/from/the/worker"
    set_ctl({"chunks": 4, "chunk_s": 0.3})
    k = new_job("life", profile={"divergence": 2.5})
    check("lifecycle: waiting_gpu before the manager runs", state(k) == "waiting_gpu")
    check("lifecycle: it starts and shows converting with progress", tick(lambda: state(k) == "converting"))
    p = job(k)["progress"]
    check("lifecycle: progress carries phase/chunk/chunks/frames/eta", p["phase"] == "converting"
          and p["chunks"] == 4 and "frames_total" in p and "recent_fps" in p and "eta_s" in p, p)
    check("lifecycle: the models were unloaded once, under the GPU lock",
          UNLOADS == [("3D conversion", True)], UNLOADS)
    check("lifecycle: finishes done with results", tick(lambda: state(k) == "done"))
    j = job(k)
    names = {r["name"]: r["bytes"] for r in j["results"]}
    check("lifecycle: results sbs.mp4/depth.mp4/meta.json with sizes and the driver's meta",
          set(names) == {"sbs.mp4", "depth.mp4", "meta.json"} and names["sbs.mp4"] == 3000 and j["meta"]["frames"] == 400,
          j.get("results"))
    a = json.load(open(wdir(k) + "/fake_args.json"))
    check("lifecycle: the driver got the CLI of stereo_job.py (run --work --src --kind --profile)",
          a["argv"][1] == "run" and a["argv"][a["argv"].index("--kind") + 1] == "video"
          and a["argv"][a["argv"].index("--work") + 1] == wdir(k)
          and a["argv"][a["argv"].index("--src") + 1] == wdir(k) + "/src.mkv"
          and a["profile"]["divergence"] == 2.5 and a["profile"]["chunk_s"] == 20 and a["src_exists"], a)
    check("lifecycle: the worker's CUDA/HF environment is scrubbed from the driver",
          a["env"] == {"LD_LIBRARY_PATH": None, "HF_HOME": None, "INSIGHTFACE_HOME": None, "KSF_VISION_PORT": None}, a["env"])
    check("lifecycle: the source was dropped after done", not os.path.exists(wdir(k) + "/src.mkv"))
    r = client.get(f"/v1/stereo/jobs/{k}/result/sbs.mp4", headers=H)
    check("result: full GET 200 with the whole file", r.status_code == 200 and len(r.data) == 3000
          and r.headers["Accept-Ranges"] == "bytes", r.status_code)
    r = client.get(f"/v1/stereo/jobs/{k}/result/sbs.mp4", headers={**H, "Range": "bytes=4-7"})
    check("result: Range bytes=4-7 -> 206, Content-Range bytes 4-7/3000, the ftyp box",
          r.status_code == 206 and r.headers["Content-Range"] == "bytes 4-7/3000" and r.data == b"ftyp",
          (r.status_code, r.headers.get("Content-Range")))
    r = client.get(f"/v1/stereo/jobs/{k}/result/sbs.mp4", headers={**H, "Range": "bytes=2500-"})
    check("result: open-ended Range (a resumed pull) -> 206 with the tail", r.status_code == 206 and len(r.data) == 500
          and r.headers["Content-Range"] == "bytes 2500-2999/3000")
    check("result: unknown name 404, traversal 404",
          client.get(f"/v1/stereo/jobs/{k}/result/nope.mp4", headers=H).status_code == 404
          and client.get(f"/v1/stereo/jobs/{k}/result/../mgr.json", headers=H).status_code == 404
          and client.get(f"/v1/stereo/jobs/{k}/result/%2e%2e%2fmgr.json", headers=H).status_code == 404)
    check("result: stamps fetched_at", vw.stereo_jobs()[k].get("fetched_at"))
    r = client.delete(f"/v1/stereo/jobs/{k}", headers=H)
    check("delete: removes the job and its directory (200)", r.status_code == 200 and r.get_json()["deleted"]
          and not os.path.exists(wdir(k)) and state(k) == "http404")
    check("delete: a second DELETE is idempotent (200, deleted false)",
          client.delete(f"/v1/stereo/jobs/{k}", headers=H).get_json()["deleted"] is False)
    k2 = new_job("notdone")
    check("result: before done -> 409", client.get(f"/v1/stereo/jobs/{k2}/result/sbs.mp4", headers=H).status_code == 409)


def case_one_active():
    reset()
    set_ctl({"chunks": 3, "chunk_s": 0.25})
    ks = [new_job("one%d" % i) for i in range(3)]
    seen_max, procs_max = 0, 0
    end = time.time() + 40
    while time.time() < end and not all(state(k) == "done" for k in ks):
        vw.stereo_manage_once()
        busy = [k for k in ks if state(k) in ("probing", "converting", "interpolating", "joining")]
        seen_max = max(seen_max, len(busy))
        procs_max = max(procs_max, ndrivers())
        time.sleep(0.05)
    check("one active job: three jobs all finish", all(state(k) == "done" for k in ks), [state(k) for k in ks])
    check("one active job: never more than one running (states) nor one driver process",
          seen_max == 1 and procs_max <= 1, (seen_max, procs_max))
    check("one active job: they ran in submission order",
          [json.load(open(wdir(k) + "/mgr.json"))["started_at"] for k in ks] == sorted(
              json.load(open(wdir(k) + "/mgr.json"))["started_at"] for k in ks))


def case_gates():
    reset()
    set_ctl({"chunks": 3, "chunk_s": 0.2})
    k = new_job("gate", profile={"max_eye_long_side": 1920})

    def blocked(label, setup, teardown, fragment):
        setup()
        for _ in range(8):
            vw.stereo_manage_once()
            time.sleep(0.05)
        s = status()
        good = (state(k) == "waiting_gpu" and not s["gate"]["open"] and fragment in s["gate"]["reason"]
                and vw._stereo["active"] is None)
        check("gate: %s keeps the job waiting (status says why)" % label, good, (state(k), s["gate"]))
        teardown()

    def hhmm(minutes):
        t = time.localtime(time.time() + minutes * 60)
        return "%02d:%02d" % (t.tm_hour, t.tm_min)

    blocked("a paused worker", lambda: vw.STATE.update(paused=True), lambda: vw.STATE.update(paused=False), "paused")
    blocked("a closed 3D window",
            lambda: CFG.update(stereo_windows=[{"start": hhmm(120), "end": hhmm(130)}]),
            lambda: CFG.pop("stereo_windows"), "window")
    blocked("an URGENT analyze/audio request a moment ago", lambda: vw.STATE.update(last_urgent=time.time()),
            lambda: vw.STATE.update(last_urgent=time.time() - 1000), "analyze/audio")
    blocked("an upscale waiting", lambda: vw.STATE.update(upscale_waiting=1), lambda: vw.STATE.update(upscale_waiting=0),
            "upscale")
    blocked("a Smooth motion viewer (segment want)",
            lambda: vw._seg["want"].update({("k", "v"): {"cursor": 0, "touched": time.time()}}),
            lambda: vw._seg["want"].clear(), "smooth motion viewer")
    blocked("an open Smooth motion stream", lambda: vw.STATE.update(interp_streaming=1),
            lambda: vw.STATE.update(interp_streaming=0), "stream open")
    blocked("Smooth motion used a moment ago", lambda: vw.STATE.update(last_interp=time.time()),
            lambda: vw.STATE.update(last_interp=0.0), "smooth motion used")
    blocked("too little free VRAM (needs estimate + reserve)", lambda: GPU.update(free=1000 + 1800 - 1),
            lambda: (GPU.update(free=6000), vw._stereo.update(vram_stuck_until=0.0)), "vram")
    blocked("too little free disk", lambda: CFG.update(stereo_min_free_gb=10 ** 7),
            lambda: CFG.update(stereo_min_free_gb=0), "disk")
    # The VRAM estimate follows the job: 1920 px eyes need 1800 + reserve, a picture 3500 + reserve.
    GPU["free"] = 1000 + 1800
    ok_video, _ = vw.stereo_gate(vw.stereo_jobs()[k])
    kp = key_of("pic-gate")
    put(kp, 100, ext="jpg")
    post(kp, "picture")
    ok_pic, why = vw.stereo_gate(vw.stereo_jobs()[kp])
    check("gate: the VRAM estimate is per job (1080p-class video fits where a picture does not)",
          ok_video and not ok_pic and "vram" in why, (ok_video, ok_pic, why))
    client.delete(f"/v1/stereo/jobs/{kp}", headers=H)
    GPU["free"] = 6000
    check("gate: with everything open the same job starts and finishes",
          tick(lambda: state(k) == "converting") and tick(lambda: state(k) == "done"))
    check("gate: the start unloaded the worker's models once, under the GPU lock", UNLOADS[-1:] == [("3D conversion", True)], UNLOADS[-3:])


def case_analyze_audio_503():
    reset()
    set_ctl({"chunks": 20, "chunk_s": 0.5})
    check("indexer: with no job running a non-urgent analyze goes through", analyze(False).status_code == 200)
    check("indexer: ... and so does a non-urgent audio request", audio(False).status_code == 200)
    vw.MODELS.pop("genre", None)
    open_gate()
    k = new_job("busy")
    check("indexer: job running", tick(lambda: state(k) == "converting"))
    before = vw.STATE["last_batch"]
    r = analyze(False)
    check("indexer: non-urgent analyze while a job runs -> 503 '3D conversion running', retry_after_s 600",
          r.status_code == 503 and r.get_json() == {"error": "3D conversion running", "retry_after_s": 600},
          (r.status_code, r.get_json()))
    r = audio(False)
    check("indexer: non-urgent audio likewise 503", r.status_code == 503 and r.get_json()["retry_after_s"] == 600)
    check("indexer: a refused request does not count as activity (the job stays running, gate clock untouched)",
          vw.STATE["last_batch"] == before and state(k) == "converting" and vw._stereo["active"] is not None)


def case_preempt():
    for what, call in (("analyze", lambda: analyze(True)), ("audio", lambda: audio(True))):
        reset()
        set_ctl({"chunks": 8, "chunk_s": 0.4})
        k = new_job("pre-" + what)
        check("preempt/%s: running at chunk 2" % what, run_to_chunk(k, 2))
        t0 = time.time()
        r = call()
        took = time.time() - t0
        j = job(k)
        check("preempt/%s: an urgent request is answered (200) and the job is back to waiting_gpu" % what,
              r.status_code == 200 and j["state"] == "waiting_gpu" and j["preempts"] == 1
              and j["last_preempt"]["reason"] == "urgent " + what and vw._stereo["active"] is None,
              (r.status_code, j))
        check("preempt/%s: within the 5 s allowance" % what, took < 5.5, took)
        log = flog(k)
        check("preempt/%s: the driver was stopped mid-chunk (SIGTERM honoured, exit 75 path)" % what,
              any(x.startswith("term at") for x in log), log)
        s = status()
        check("preempt/%s: the request just made keeps the gate closed (idle needed)" % what,
              not s["gate"]["open"] and "analyze/audio" in s["gate"]["reason"], s["gate"])
        check("preempt/%s: after the idle time the job resumes" % what, tick(lambda: state(k) in ("converting", "joining")))
        check("preempt/%s: ... and finishes with every chunk done once" % what, tick(lambda: state(k) == "done", 40))
        done = [x for x in flog(k) if x.startswith("done chunk")]
        starts = [x for x in flog(k) if x.startswith("start chunk")]
        check("preempt/%s: at most one chunk of work was lost (8 done once each, %d starts)" % (what, len(starts)),
              sorted(done) == sorted("done chunk %d" % i for i in range(1, 9)) and 8 <= len(starts) <= 9, (len(done), len(starts)))
        check("preempt/%s: resuming did not count as a failure" % what, not vw.stereo_jobs()[k].get("fail_counts"))
    # An analyze that is NOT urgent never preempts.
    reset()
    set_ctl({"chunks": 6, "chunk_s": 0.3})
    k = new_job("nonurgent")
    run_to_chunk(k, 1)
    analyze(False)
    check("preempt: a non-urgent analyze does not stop the job", state(k) == "converting")


def case_smooth_motion():
    reset()
    set_ctl({"chunks": 10, "chunk_s": 0.4})
    ikey = key_of("interp-src")
    open(os.path.join(ICACHE, ikey + ".src"), "wb").write(b"x" * 100)
    k = new_job("smooth-seg")
    check("smooth: running at chunk 2", run_to_chunk(k, 2))
    t0 = time.time()
    r = client.get(f"/v1/interp/seg/{ikey}/f120_h1080_r20/end.json", headers=H)
    j = job(k)
    check("smooth: a segment request that must wait stops the job (waiting_gpu, reason, <5.5 s)",
          j["state"] == "waiting_gpu" and j["last_preempt"]["reason"] == "smooth motion segment"
          and time.time() - t0 < 5.5 and r.status_code == 404, (j.get("state"), j.get("last_preempt"), r.status_code))
    for _ in range(10):
        vw.stereo_manage_once()
        time.sleep(0.05)
    s = status()
    check("smooth: while the viewer is present the job stays down (gate: smooth motion viewer)",
          state(k) == "waiting_gpu" and "smooth motion" in s["gate"]["reason"], s["gate"])
    vw._seg["want"].clear()
    vw.STATE["last_interp"] = 0.0
    check("smooth: once the viewer is gone and the idle time passed it resumes and finishes",
          tick(lambda: state(k) == "done", 40))
    # The live stream route
    reset()
    set_ctl({"chunks": 10, "chunk_s": 0.4})
    k = new_job("smooth-stream")
    check("smooth: running again at chunk 2", run_to_chunk(k, 2))
    r = client.get(f"/v1/interp/stream/{ikey}?fps=120&start=0", headers=H)
    body = r.data
    j = job(k)
    check("smooth: opening an interpolated stream stops the job",
          r.status_code == 200 and body == b"abc" and j["state"] == "waiting_gpu"
          and j["last_preempt"]["reason"] == "smooth motion stream", (r.status_code, body, j.get("state")))
    check("smooth: the stream bookkeeping is balanced afterwards (none open, last_interp set)",
          vw.STATE["interp_streaming"] == 0 and time.time() - vw.STATE["last_interp"] < 5)
    check("smooth: ... and the gate waits stereo_interp_idle_s after it",
          "smooth motion used" in status()["gate"]["reason"] or "viewer" in status()["gate"]["reason"])
    vw.STATE["last_interp"] = 0.0
    vw._seg["want"].clear()
    check("smooth: then it resumes and finishes", tick(lambda: state(k) == "done", 40))


def case_upscale_alongside():
    reset()
    set_ctl({"chunks": 6, "chunk_s": 0.3})
    k = new_job("upscale")
    run_to_chunk(k, 1)
    vw.STATE["upscale_waiting"] = 1
    for _ in range(10):
        vw.stereo_manage_once()
        time.sleep(0.05)
    check("upscale: an upscale waiting does not stop a running job", state(k) == "converting" and vw._stereo["active"])
    vw.STATE["upscale_waiting"] = 0


def case_kill_and_pause_and_disk():
    reset()
    set_ctl({"chunks": 10, "chunk_s": 5, "ignore_term": True})
    CFG["stereo_kill_s"] = 1.0
    k = new_job("stubborn")
    run_to_chunk(k, 1)
    t0 = time.time()
    r = analyze(True)
    took = time.time() - t0
    check("kill: a driver that ignores SIGTERM does not hold an urgent request past 5 s",
          r.status_code == 200 and 4.5 < took < 8, took)
    check("kill: ... the manager SIGKILLs it after stereo_kill_s and the job waits again",
          tick(lambda: state(k) == "waiting_gpu", 10) and vw._stereo["active"] is None and ndrivers() == 0)
    # pause stops a running job (it means "give the card back")
    reset()
    set_ctl({"chunks": 10, "chunk_s": 0.4})
    k = new_job("paused")
    run_to_chunk(k, 2)
    vw.STATE["paused"] = True
    check("pause: /v1/pause while a job runs stops it (waiting_gpu, reason paused)",
          tick(lambda: state(k) == "waiting_gpu", 10) and job(k)["last_preempt"]["reason"] == "paused")
    vw.STATE["paused"] = False
    check("pause: unpaused it resumes", tick(lambda: state(k) in ("converting", "joining", "done"), 10))
    # disk emergency
    reset()
    set_ctl({"chunks": 10, "chunk_s": 0.4})
    k = new_job("diskfull")
    run_to_chunk(k, 1)
    CFG["stereo_abort_free_gb"] = 10 ** 7
    check("disk: a running job is stopped and failed when free space falls under stereo_abort_free_gb",
          tick(lambda: state(k) == "failed", 10) and "disk almost full" in (job(k)["error"] or ""), job(k))


def case_failures():
    reset()
    set_ctl({"chunks": 6, "chunk_s": 0.1, "fail_at": {"2": 2, "4": 2}})
    k = new_job("flaky")
    check("failure: a driver that fails twice at chunk 2 and twice at chunk 4 still finishes (restarted each time)",
          tick(lambda: state(k) == "done", 40), (state(k), job(k).get("error")))
    m = vw.stereo_jobs()[k]
    check("failure: counted per phase+chunk, never three of one (2:2, 4:2; 5 starts)",
          m["fail_counts"] == {"converting:2": 2, "converting:4": 2} and m["attempts"] == 5, (m["fail_counts"], m["attempts"]))
    reset()
    set_ctl({"chunks": 6, "chunk_s": 0.1, "fail_at": {"3": 99}})
    k = new_job("broken")
    check("failure: the same chunk failing 3 times fails the job", tick(lambda: state(k) == "failed", 40))
    m = vw.stereo_jobs()[k]
    check("failure: with the driver's error text and exactly 3 starts",
          m["fail_counts"] == {"converting:3": 3} and m["attempts"] == 3 and "fake failure at chunk 3" in m["error"],
          (m["fail_counts"], m["attempts"], m.get("error")))
    for _ in range(10):
        vw.stereo_manage_once()
        time.sleep(0.05)
    check("failure: a failed job is not started again", job(k)["attempts"] == 3 and vw._stereo["active"] is None)
    check("failure: the source is kept for a retry", os.path.exists(wdir(k) + "/src.mkv"))
    set_ctl({"chunks": 6, "chunk_s": 0.1})
    r = post(k)
    check("failure: POST again retries a failed job (waiting_gpu, counts cleared) and it then finishes",
          r.status_code == 200 and r.get_json()["state"] == "waiting_gpu" and tick(lambda: state(k) == "done", 30))
    check("failure: the retry resumed from the finished chunks (chunk 1-2 done once)",
          [x for x in flog(k) if x.startswith("done chunk 1")] == ["done chunk 1"])
    reset()
    set_ctl({"chunks": 3, "chunk_s": 0.1, "no_outputs": True})
    k = new_job("nooutputs")
    check("failure: exit 0 without the outputs is a failure too", tick(lambda: state(k) == "failed", 40)
          and "missing" in job(k)["error"], job(k))
    reset()
    k = key_of("nodriver")
    CFG["stereo_driver"] = os.path.join(TMP, "does-not-exist.py")
    put(k, 100, ext="mkv")
    post(k)
    check("failure: a driver that cannot run fails the job after 3 starts (not a crash loop)",
          tick(lambda: state(k) == "failed", 40), job(k))


def case_hold_cancel():
    reset()
    set_ctl({"chunks": 8, "chunk_s": 0.4})
    a = new_job("hold-a")
    run_to_chunk(a, 3)
    r = client.post(f"/v1/stereo/jobs/{a}/hold", headers=H)
    check("hold: stops the driver, keeps the directory and the finished chunks (200, held)",
          r.status_code == 200 and r.get_json()["state"] == "held" and vw._stereo["active"] is None
          and os.path.exists(wdir(a) + "/src.mkv") and json.load(open(wdir(a) + "/fake_state.json"))["done"] >= 2, r.get_data(as_text=True))
    set_ctl({"chunks": 2, "chunk_s": 0.2})
    b = new_job("hold-b")
    c = new_job("hold-c")
    r = client.post(f"/v1/stereo/jobs/{c}/hold", headers=H)
    check("hold: at most one job is held (a second hold -> 409)", r.status_code == 409 and r.get_json()["held"] == a, r.get_data(as_text=True))
    check("hold: the held job is not scheduled while others run; the others finish",
          tick(lambda: state(b) == "done" and state(c) == "done", 40) and state(a) == "held")
    set_ctl({"chunks": 8, "chunk_s": 0.2})
    r = post(a)
    check("hold: POST jobs resumes a held job (waiting_gpu)", r.status_code == 200 and r.get_json()["state"] == "waiting_gpu")
    check("hold: ... which finishes, every chunk done exactly once", tick(lambda: state(a) == "done", 40)
          and sorted(x for x in flog(a) if x.startswith("done chunk")) == sorted("done chunk %d" % i for i in range(1, 9)))
    # resume route + a queued job can be parked
    reset()
    set_ctl({"chunks": 3, "chunk_s": 0.2})
    p = new_job("park")
    r = client.post(f"/v1/stereo/jobs/{p}/hold", headers=H)
    check("hold: a queued job is parked without a driver", r.status_code == 200 and state(p) == "held")
    vw.stereo_manage_once()
    check("hold: a parked job is not started", state(p) == "held" and vw._stereo["active"] is None)
    r = client.post(f"/v1/stereo/jobs/{p}/resume", headers=H)
    check("hold: POST /resume queues it again and it finishes", r.status_code == 200 and tick(lambda: state(p) == "done", 20))
    check("hold: a done job cannot be held (409)", client.post(f"/v1/stereo/jobs/{p}/hold", headers=H).status_code == 409)
    # cancel
    reset()
    set_ctl({"chunks": 10, "chunk_s": 0.5})
    k = new_job("cancel")
    run_to_chunk(k, 2)
    pid = vw._stereo["active"]["proc"].pid
    r = client.delete(f"/v1/stereo/jobs/{k}", headers=H)
    check("cancel: DELETE stops the driver and removes everything", r.status_code == 200 and r.get_json()["deleted"]
          and not alive(pid) and not os.path.exists(wdir(k)) and vw._stereo["active"] is None and state(k) == "http404")
    check("cancel: the manager does not trip over the vanished job", tick(lambda: True) and vw._stereo["active"] is None)
    k = new_job("cancel-queued")
    check("cancel: a queued job is removed too", client.delete(f"/v1/stereo/jobs/{k}", headers=H).get_json()["deleted"]
          and not os.path.exists(wdir(k)))


def case_restart():
    reset()
    set_ctl({"chunks": 8, "chunk_s": 0.4})
    k = new_job("restart")
    up = key_of("restart-up")
    put(up, 300, ext="mkv")
    open(wdir(up) + "/src.mkv.99.part", "wb").write(b"junk")
    run_to_chunk(k, 3)
    act = vw._stereo["active"]
    os.killpg(act["proc"].pid, signal.SIGKILL)           # what systemd's cgroup kill does with the service
    act["proc"].wait()
    vw._stereo["active"] = None                          # ... and the new process knows nothing
    vw._stereo["jobs"], vw._stereo["dir"] = {}, None
    jobs = vw.stereo_jobs()
    check("restart: a job that was running comes back as waiting_gpu", jobs[k]["state"] == "waiting_gpu"
          and jobs[k]["restarts"] == 1 and state(k) == "waiting_gpu", jobs[k]["state"])
    check("restart: an upload-only job stays uploaded, half-written parts are gone",
          jobs[up]["state"] == "uploaded" and not os.path.exists(wdir(up) + "/src.mkv.99.part"))
    check("restart: it resumes from its finished chunks and finishes with the right totals",
          tick(lambda: state(k) == "done", 40) and json.load(open(wdir(k) + "/meta.json"))["frames"] == 800
          and sorted(set(x for x in flog(k) if x.startswith("done chunk"))) == sorted("done chunk %d" % i for i in range(1, 9)))
    check("restart: the kill was not counted as a failure", not vw.stereo_jobs()[k].get("fail_counts"))


def case_pictures():
    reset()
    set_ctl({"chunks": 1, "chunk_s": 0.15})
    k = key_of("pic1")
    put(k, 400, ext="jpg")
    r = post(k, "picture")
    check("picture: single file job queued", r.status_code == 201)
    check("picture: finishes with sbs.jpg/depth.png/meta.json", tick(lambda: state(k) == "done", 20)
          and {x["name"] for x in job(k)["results"]} == {"sbs.jpg", "depth.png", "meta.json"})
    r = client.get(f"/v1/stereo/jobs/{k}/result/sbs.jpg", headers=H)
    check("picture: result served as image/jpeg", r.status_code == 200 and r.mimetype == "image/jpeg" and r.data[:2] == b"\xff\xd8")
    # batch
    reset()
    set_ctl({"chunks": 1, "chunk_s": 0.1})
    k = key_of("pic-batch")
    for n in ("a.jpg", "b.png", "c.webp"):
        r = put(k, 300, name=n)
        assert r.status_code == 200, r.get_data(as_text=True)
    check("picture batch: the files are listed on the job", job(k)["files"] == 3 and job(k)["source_bytes"] == 900, job(k))
    check("picture batch: a name that is not a picture file 400", put(k, 10, name="../x.jpg").status_code == 400
          and put(k, 10, name="x.mp4").status_code == 400)
    check("picture batch: the same stem with another extension 409", put(k, 10, name="a.png").status_code == 409)
    check("picture batch: mixing one file and a batch 409", put(k, 10, ext="jpg").status_code == 409)
    check("picture batch: a batch is not a video job", post(k, "video").status_code == 400)
    check("picture batch: queued", post(k, "picture").status_code == 201)
    check("picture batch: finishes", tick(lambda: state(k) == "done", 30))
    names = {x["name"] for x in job(k)["results"]}
    check("picture batch: results per stem a/ b/ c/ with sbs.jpg depth.png meta.json",
          names == {s + "/" + n for s in "abc" for n in ("sbs.jpg", "depth.png", "meta.json")}, names)
    a = json.load(open(wdir(k) + "/fake_args.json"))
    r = client.get(f"/v1/stereo/jobs/{k}/result/b/depth.png", headers=H)
    check("picture batch: the driver got the src DIRECTORY; <stem>/<name> downloads work",
          a["src_is_dir"] and a["argv"][a["argv"].index("--src") + 1] == wdir(k) + "/src"
          and r.status_code == 200 and r.mimetype == "image/png", (a["src_is_dir"], r.status_code))
    check("picture batch: the sources were dropped, the outputs kept", not os.path.exists(wdir(k) + "/src")
          and os.path.exists(wdir(k) + "/out/a/sbs.jpg"))


def case_status_and_auth():
    reset()
    set_ctl({"chunks": 10, "chunk_s": 0.4})
    s = status()
    check("status: shape (available/missing/running/held/waiting/disk_free_gb/work_gb/nunif_commit/gate)",
          all(x in s for x in ("available", "missing", "running", "held", "waiting", "disk_free_gb", "work_gb",
                               "nunif_commit", "gate", "counts", "min_free_gb")) and s["running"] is None, list(s))
    check("status: available true when the pieces exist", s["available"] is True and s["missing"] == [], s["missing"])
    check("status: announces the largest upload, above the old 8 GiB (2026-10-29: a 9.4 GiB episode)",
          s.get("max_upload_bytes") == vw.UPLOAD_MAX_BYTES and vw.UPLOAD_MAX_BYTES > 8 * 1024 ** 3, s.get("max_upload_bytes"))
    main = open(WORKER, encoding="utf-8").read().split('if __name__ == "__main__":')[-1]
    check("main: waitress takes UPLOAD_MAX_BYTES and spools bodies under the app dir on /data, set before serve()",
          "max_request_body_size=UPLOAD_MAX_BYTES" in main and "tempfile.tempdir = UPLOAD_SPOOL_DIR" in main
          and main.index("tempfile.tempdir = ") < main.index("serve(app") and "8 * 1024" not in main
          and os.path.dirname(vw.UPLOAD_SPOOL_DIR) == vw.BASE_DIR, vw.UPLOAD_SPOOL_DIR)
    CFG["stereo_python"] = os.path.join(TMP, "missing-python")
    check("status: names what is missing", "stereo venv python" in status()["missing"] and not status()["available"])
    CFG["stereo_python"] = sys.executable
    a = new_job("status-a")
    b = new_job("status-b")
    tick(lambda: state(a) == "converting")
    s = status()
    check("status: running job with progress, the other waiting at position 1",
          s["running"]["key"] == a and s["running"]["progress"]["chunks"] == 10
          and [w["key"] for w in s["waiting"]] == [b] and s["waiting"][0]["queue_position"] == 1, s["running"])
    client.post(f"/v1/stereo/jobs/{b}/hold", headers=H)
    check("status: the held job is listed under held", [h["key"] for h in status()["held"]] == [b])
    check("status: work_gb counts the work dir", status()["work_gb"] >= 0 and isinstance(status()["disk_free_gb"], float))
    # auth
    CFG["token"] = "sekrit"
    codes = [client.get("/v1/stereo/status").status_code, client.put(f"/v1/stereo/source/{a}", data=b"x").status_code,
             client.post("/v1/stereo/jobs", json={}).status_code, client.get(f"/v1/stereo/jobs/{a}").status_code,
             client.get(f"/v1/stereo/jobs/{a}/result/sbs.mp4").status_code, client.delete(f"/v1/stereo/jobs/{a}").status_code,
             client.post(f"/v1/stereo/jobs/{a}/hold").status_code, client.post(f"/v1/stereo/jobs/{a}/resume").status_code]
    check("auth: every /v1/stereo route answers 401 without the token", codes == [401] * 8, codes)
    CFG["token"] = ""
    CFG["token"] = "sekrit"
    good = {"Authorization": "Bearer sekrit"}
    check("auth: ... and works with it", client.get("/v1/stereo/status", headers=good).status_code == 200)
    CFG["token"] = ""
    check("auth: an unknown key is 404, a malformed one 400",
          client.get("/v1/stereo/jobs/" + "a" * 32).status_code == 404 and client.get("/v1/stereo/jobs/zz").status_code == 400)


def case_housekeeping():
    reset()
    h = 3600
    set_ctl({"chunks": 1, "chunk_s": 0.05})
    now = time.time()

    def finish(label):
        k = new_job(label)
        assert tick(lambda: state(k) == "done", 20)
        return k

    old_done, fresh_done, fetched_done = finish("old-done"), finish("fresh-done"), finish("fetched-done")
    jobs = vw.stereo_jobs()
    jobs[old_done]["done_at"] = now - 100 * h
    jobs[fetched_done]["done_at"] = now - 100 * h
    jobs[fetched_done]["fetched_at"] = now - 1 * h
    set_ctl({"chunks": 3, "chunk_s": 0.05, "fail_at": {"1": 99}})
    old_failed, fresh_failed = new_job("old-failed"), new_job("fresh-failed")
    assert tick(lambda: state(old_failed) == "failed" and state(fresh_failed) == "failed", 40)
    jobs[old_failed]["failed_at"] = now - 25 * h
    up_old, up_new = key_of("up-old"), key_of("up-new")
    put(up_old, 100, ext="mkv")
    put(up_new, 100, ext="mkv")
    jobs[up_old]["updated"] = now - 25 * h
    set_ctl({"chunks": 3, "chunk_s": 0.05})
    held = new_job("held-old")
    client.post(f"/v1/stereo/jobs/{held}/hold", headers=H)
    jobs[held]["held_at"] = now - 8 * 24 * h
    orphan_old, orphan_new = key_of("orph-old"), key_of("orph-new")
    for o in (orphan_old, orphan_new):
        os.makedirs(wdir(o))
        open(wdir(o) + "/junk", "w").write("x")
    os.utime(wdir(orphan_old), (now - 2 * 24 * h, now - 2 * 24 * h))
    p1 = os.path.join(WORK, "a_anime")                       # a hand-run P1 dir: not ours, never touched
    os.makedirs(p1)
    os.utime(p1, (now - 30 * 24 * h, now - 30 * 24 * h))
    vw.stereo_housekeeping(vw.stereo_config(), force=True)
    gone = lambda k: k not in vw.stereo_jobs() and not os.path.exists(wdir(k))   # noqa: E731
    check("ttl: a done job nobody fetched for 72 h is removed (dir and entry)", gone(old_done))
    check("ttl: a fresh done job stays", not gone(fresh_done) and state(fresh_done) == "done")
    check("ttl: a result fetched 1 h ago keeps the job (the TTL runs from the last fetch)", not gone(fetched_done))
    check("ttl: a job failed 25 h ago is removed, a fresh failure stays", gone(old_failed) and not gone(fresh_failed))
    check("ttl: an upload nobody submitted for 25 h is removed, a fresh one stays", gone(up_old) and not gone(up_new))
    check("ttl: a job held for 8 days is removed", gone(held))
    check("ttl: a key dir without mgr.json older than a day is removed, a young one stays",
          not os.path.exists(wdir(orphan_old)) and os.path.exists(wdir(orphan_new)))
    check("ttl: a directory that is not a job (the P1 work dirs) is never touched", os.path.isdir(p1))
    shutil.rmtree(p1)


def case_driver_retry():
    """stereo_job.py (B): an encoder that refuses to open is retried in-process, 3 tries, 5/15/45 s backoff."""
    spec2 = importlib.util.spec_from_file_location("stereo_job_under_test", STEREO_JOB)
    sj = importlib.util.module_from_spec(spec2)
    spec2.loader.exec_module(sj)

    class ExternalError(Exception):          # same class name as av.error.ExternalError
        pass

    def ext(args_form=False):
        if args_form:      # what PyAV really raises: (errno, message, filename)
            return ExternalError(542398533, "Generic error in an external library", "avcodec_open2(hevc_nvenc)")
        return ExternalError("[Errno 542398533] Generic error in an external library: 'avcodec_open2(hevc_nvenc)'")

    check("driver retry: back-off schedule is 5/15/45 s", tuple(sj.ENCODER_RETRY_BACKOFF_S) == (5, 15, 45), sj.ENCODER_RETRY_BACKOFF_S)
    check("driver retry: avcodec_open2 ExternalError recognised (also as a __cause__)",
          sj.is_encoder_open_failure(ext()) and sj.is_encoder_open_failure(ext(True)))
    wrapped = RuntimeError("chunk failed")
    wrapped.__cause__ = ext()
    check("driver retry: ... through a wrapper", sj.is_encoder_open_failure(wrapped))
    check("driver retry: other errors are not (ExternalError of another call, RuntimeError)",
          not sj.is_encoder_open_failure(ExternalError("avcodec_send_frame")) and not sj.is_encoder_open_failure(RuntimeError("x")))

    cd = os.path.join(TMP, "chunks")
    os.makedirs(cd, exist_ok=True)
    sleeps = []
    sj.interruptible_sleep = lambda s: sleeps.append(s)

    class Hooks:
        def __init__(self):
            self.writer, self.begins, self.ends = object(), 0, 0

        def begin_chunk(self, p):
            self.begins += 1

        def end_chunk(self):
            self.ends += 1
            return 123

    class U:
        calls = 0
        fail = 0
        exc = None
        entry = []

        @staticmethod
        def process_video_full(inp, out, args, dm, sm):
            U.calls += 1
            U.entry.append([f for f in os.listdir(cd) if f.endswith(".mp4")])
            for f in ("_tmp_sbs_00001.mp4", "_error_sbs_00001.mp4", "sbs_00001.mp4", "_dpart_00001.mp4"):
                open(os.path.join(cd, f), "w").write("partial")
            if U.calls <= U.fail:
                raise (U.exc or ext())

    def run(fail, exc=None):
        U.calls, U.fail, U.exc, U.entry = 0, fail, exc, []
        for f in os.listdir(cd):
            os.remove(os.path.join(cd, f))
        del sleeps[:]
        h = Hooks()
        out = sj.convert_chunk_with_retry(U, h, "in", cd + "/sbs_00001.mp4", cd + "/_dpart_00001.mp4",
                                          cd + "/_tmp_sbs_00001.mp4", cd, "00001", None, None, None)
        return out, h

    out, h = run(0)
    check("driver retry: no failure -> one call, no sleep, frames returned", out == 123 and U.calls == 1 and not sleeps)
    out, h = run(2)
    check("driver retry: two open failures then success -> 3 calls, sleeps 5 then 15, result returned",
          out == 123 and U.calls == 3 and sleeps == [5, 15], (U.calls, sleeps))
    check("driver retry: a fresh depth writer per try, and nothing of the failed try is left when the next one starts",
          h.begins == 3 and U.entry == [[], [], []], (h.begins, U.entry))
    try:
        run(3)
        raised = False
    except ExternalError:
        raised = True
    check("driver retry: three failures -> the error propagates (the manager then restarts the driver)",
          raised and U.calls == 3 and sleeps == [5, 15], (raised, U.calls, sleeps))
    left = [f for f in os.listdir(cd) if f in ("_tmp_sbs_00001.mp4", "_error_sbs_00001.mp4", "sbs_00001.mp4", "_dpart_00001.mp4")]
    check("driver retry: the failed chunk leaves no partial files behind", not left, left)
    try:
        run(1, RuntimeError("something else"))
        other = False
    except RuntimeError:
        other = True
    check("driver retry: any other error is not retried", other and U.calls == 1 and not sleeps)
    sj.STOP.set()
    try:
        run(1)
        stopped = False
    except ExternalError:
        stopped = True
    sj.STOP.clear()
    check("driver retry: SIGTERM (STOP) during a failure is not retried", stopped and U.calls == 1)
    # The real back-off must stop on SIGTERM
    real = importlib.util.module_from_spec(spec2)
    spec2.loader.exec_module(real)
    real.STOP.set()
    try:
        real.interruptible_sleep(30)
        interrupted = False
    except real.Interrupted:
        interrupted = True
    check("driver retry: interruptible_sleep raises Interrupted at once when STOP is set", interrupted)
    src = open(STEREO_JOB, encoding="utf-8").read()
    check("driver: pictures stay uncapped (no --max-output-height on the picture path)",
          'if upright_max_h:' in src and 'argv += ["--max-output-height"' in src and 'else:\n        argv += ["--format", "jpeg"]' in src)


def case_existing_routes():
    reset()
    # The interp upload still behaves the same after the _receive_upload refactor.
    ikey = key_of("interp-up")
    r = client.put(f"/v1/interp/source/{ikey}", data=b"v" * 1234, headers=H)
    check("refactor: /v1/interp/source still stores the body and answers its length",
          r.status_code == 200 and r.get_json() == {"key": ikey, "bytes": 1234}
          and os.path.getsize(os.path.join(ICACHE, ikey + ".src")) == 1234, r.get_data(as_text=True))
    r = client.get(f"/v1/interp/status?key={ikey}", headers=H)
    check("refactor: /v1/interp/status sees it cached", r.get_json()["cached"] is True)
    check("refactor: bad interp key still 400", client.put("/v1/interp/source/zz", data=b"x", headers=H).status_code == 400)


def _png():
    buf = io.BytesIO()
    Image.new("RGB", (8, 8), (10, 20, 30)).save(buf, "PNG")
    return buf.getvalue()


def case_indexer_yields():
    """P2b: GPU priority upscale > Smooth motion > 3D > indexing. A waiting job must not be starved by the
    indexer's non-urgent batches."""
    import threading

    def hhmm(minutes):
        t = time.localtime(time.time() + minutes * 60)
        return "%02d:%02d" % (t.tm_hour, t.tm_min)

    def post_batch(box):
        box["r"] = vw.app.test_client().post(
            "/v1/analyze", data={"file": (io.BytesIO(_png()), "c.png"), "tasks": "tags", "urgent": ""},
            content_type="multipart/form-data")

    # 1. a waiting job + a stream of non-urgent batches: the batches get 503 and the job starts
    reset()
    set_ctl({"chunks": 4, "chunk_s": 0.2})
    k = new_job("starved")
    before = vw.STATE["last_batch"]
    codes, started, deadline = [], False, time.time() + 25
    while time.time() < deadline and not started:
        r = analyze(False)
        codes.append((r.status_code, (r.get_json() or {}).get("error"), (r.get_json() or {}).get("retry_after_s")))
        r = audio(False)
        codes.append((r.status_code, (r.get_json() or {}).get("error"), (r.get_json() or {}).get("retry_after_s")))
        vw.stereo_manage_once()
        started = state(k) in ("converting", "joining", "done")
        time.sleep(0.1)
    check("yield: a stream of non-urgent analyze/audio batches does not keep a waiting job from starting", started, state(k))
    check("yield: ... the batches before the start got 503 '3D conversion waiting to start', retry_after_s 600",
          codes[:2] == [(503, "3D conversion waiting to start", 600)] * 2, codes[:3])
    check("yield: refused batches did not move the gate clock (last_batch)", vw.STATE["last_batch"] == before)
    check("yield: the job finishes", tick(lambda: state(k) == "done", 30))
    check("yield: with the job done the indexer is served again", analyze(False).status_code == 200 and audio(False).status_code == 200)
    vw.MODELS.pop("genre", None)

    # 2. things the indexer cannot change: it is served normally while the job waits
    reset()
    set_ctl({"chunks": 4, "chunk_s": 0.2})
    k = new_job("blocked")

    def served(label, setup, teardown):
        setup()
        r1, r2 = analyze(False), audio(False)
        s = status()
        check("yield: %s -> the job waits and the indexer is NOT refused" % label,
              r1.status_code == 200 and r2.status_code == 200 and state(k) == "waiting_gpu" and not s["gate"]["open"],
              (r1.status_code, r2.status_code, s["gate"]))
        teardown()

    served("outside the 3D window", lambda: CFG.update(stereo_windows=[{"start": hhmm(120), "end": hhmm(130)}]),
           lambda: CFG.pop("stereo_windows"))
    served("disk low", lambda: CFG.update(stereo_min_free_gb=10 ** 7), lambda: CFG.update(stereo_min_free_gb=0))
    served("a Smooth motion viewer", lambda: vw._seg["want"].update({("k", "v"): {"cursor": 0, "touched": time.time()}}),
           lambda: vw._seg["want"].clear())
    served("an urgent request a moment ago", lambda: vw.STATE.update(last_urgent=time.time()),
           lambda: vw.STATE.update(last_urgent=time.time() - 1000))
    vw.STATE["paused"] = True
    r = analyze(False)
    check("yield: a paused worker keeps its own 503 'paused' (not the 3D one)", r.status_code == 503 and r.get_json()["error"] == "paused")
    vw.STATE["paused"] = False
    vw.MODELS.pop("genre", None)
    check("yield: back to open, the same waiting job refuses the indexer",
          analyze(False).status_code == 503 and status()["gate"]["open"])
    r = client.post(f"/v1/stereo/jobs/{k}/hold", headers=H)
    check("yield: a held job does not make the indexer yield", analyze(False).status_code == 200,
          (r.status_code, r.get_data(as_text=True)[:80]))
    vw.MODELS.pop("genre", None)

    # 3. VRAM short even after unloading the worker's models: the indexer is served, the job retries later
    reset()
    set_ctl({"chunks": 4, "chunk_s": 0.2})
    k = new_job("vramstuck")
    GPU["free"] = 1000 + 1800 - 1
    check("yield: VRAM is not part of the yield test before the manager has tried (the indexer yields)",
          analyze(False).status_code == 503)
    for _ in range(3):
        vw.stereo_manage_once()
    check("yield: the manager unloaded, found VRAM still short and the job still waits",
          state(k) == "waiting_gpu" and UNLOADS and "vram" in vw._stereo["gate"] and vw._stereo["vram_stuck_until"] > time.time(),
          (state(k), UNLOADS, vw._stereo["gate"]))
    n = len(UNLOADS)
    vw.stereo_manage_once()
    check("yield: ... and the indexer is served meanwhile; no unload thrash", analyze(False).status_code == 200 and len(UNLOADS) == n)
    vw.MODELS.pop("genre", None)
    GPU["free"] = 6000
    vw._stereo["vram_stuck_until"] = 0.0
    check("yield: once the card has room (and the retry time passed) the job starts", tick(lambda: state(k) in ("converting", "done")))

    # 4. an urgent request still stops a running job; batches arriving while the gate is closed by it are served
    reset()
    set_ctl({"chunks": 8, "chunk_s": 0.4})
    k = new_job("urgent")
    check("yield: running", run_to_chunk(k, 2))
    check("yield: non-urgent batch while running -> 503 running", analyze(False).get_json()["error"] == "3D conversion running")
    r = analyze(True)
    check("yield: urgent analyze still preempts (200, job back to waiting_gpu)", r.status_code == 200 and state(k) == "waiting_gpu")
    check("yield: right after it the gate is closed by the urgent request, so a non-urgent batch is served",
          analyze(False).status_code == 200 and "urgent" in status()["gate"]["reason"])
    vw.MODELS.pop("genre", None)
    check("yield: after stereo_idle_s the job resumes and finishes",
          tick(lambda: state(k) == "converting", 10) and tick(lambda: state(k) == "done", 40))

    # 5. a batch in flight finishes before the job starts
    reset()
    set_ctl({"chunks": 3, "chunk_s": 0.2})
    inside, release = threading.Event(), threading.Event()
    real_tag = vw.tag

    def slow_tag(images):
        inside.set()
        release.wait(20)
        return [{}] * len(images)

    vw.tag = slow_tag
    result = {}
    t = threading.Thread(target=post_batch, args=(result,))
    k = None
    try:
        t.start()
        check("inflight: a non-urgent batch is on the card", inside.wait(10))
        k = new_job("after-batch")
        for _ in range(10):
            vw.stereo_manage_once()
            time.sleep(0.05)
        check("inflight: the open gate does not start the job under it ('gpu busy')",
              state(k) == "waiting_gpu" and vw._stereo["active"] is None and vw._stereo["gate"] == "gpu busy",
              (state(k), vw._stereo["gate"]))
        check("inflight: a NEW batch meanwhile is refused", analyze(False).status_code == 503)
        release.set()
        t.join(10)
        check("inflight: the batch completed (200)", result.get("r") is not None and result["r"].status_code == 200)
        check("inflight: then the job starts", tick(lambda: state(k) in ("converting", "joining", "done"), 10))
    finally:
        release.set()
        vw.tag = real_tag
    if k:
        tick(lambda: state(k) == "done", 20)
    vw.MODELS.pop("genre", None)

    # 6. a batch that queued on the GPU lock while the manager started a job must not run beside it
    reset()
    held_lock = vw._lock.acquire(timeout=5)
    result = {}
    real_running = vw.stereo_running
    t = threading.Thread(target=post_batch, args=(result,))
    try:
        t.start()
        time.sleep(0.4)                          # it passed the yield check and now waits for _lock
        vw.stereo_running = lambda: True         # ... the manager started a job meanwhile
    finally:
        vw._lock.release()
        t.join(10)
        vw.stereo_running = real_running
    check("lockrace: a batch that waited for the GPU lock and finds a job running is refused 503",
          held_lock and result.get("r") is not None and result["r"].status_code == 503
          and result["r"].get_json()["error"] == "3D conversion running",
          result.get("r") and result["r"].get_data(as_text=True)[:80])


CASES = [case_upload, case_submit_validation, case_lifecycle, case_one_active, case_gates, case_analyze_audio_503,
         case_preempt, case_smooth_motion, case_upscale_alongside, case_kill_and_pause_and_disk, case_failures,
         case_hold_cancel, case_restart, case_pictures, case_status_and_auth, case_housekeeping, case_driver_retry,
         case_existing_routes, case_indexer_yields]

if __name__ == "__main__":
    only = [a for a in sys.argv[3:]]
    try:
        for fn in CASES:
            if only and fn.__name__.replace("case_", "") not in only:
                continue
            print("--- " + fn.__name__, flush=True)
            fn()
    finally:
        reset()
        vw.MODELS.pop("genre", None)
        shutil.rmtree(TMP, ignore_errors=True)
    print("ALL PASS" if ok_all else "SOME CHECKS FAILED")
    sys.exit(0 if ok_all else 1)
