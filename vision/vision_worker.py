"""Vision worker: the only thing in this estate that touches the GPU for media.

Runs on the Amarena host because that is where the RTX 5060 is. media-dl has
32 cores and no GPU, so it sends the file and gets back what the models saw.

Three passes, all measured on this card against real library images:

  caption   Florence-2-large <DETAILED_CAPTION>   ~0.18 s/img at batch 8
  ocr       Florence-2-large <OCR>                ~0.14 s/img at batch 8
  tags      WD14 eva02-large-v3 (ONNX)            ~0.07 s/img

Design notes worth keeping:

* **Originals, not thumbnails.** The caller ships the real file; quality was
  worth more than bandwidth here. Nothing is written to disk on this host -
  bytes go into memory, through the models, and out. A 450k-file backlog must
  never accumulate on the box that runs the game server. The one exception is
  frame interpolation (/v1/interp/*): a video being watched is kept in
  `interp_cache_dir`, capped at `interp_cache_gb` and evicted oldest-used
  first, because every seek streams it again from a new position.
* **Batch 8, beams 1.** Measured: batch 12 is no faster and batch 16 runs out
  of memory; beams=3 produced a byte-identical caption to beams=1 and cost
  30%. Neither is a guess.
* **Two allocators, one card.** Florence runs under torch's caching allocator
  and the tagger under onnxruntime's; neither can see the other's free list, so
  the cache is handed back between the passes. Without that the tagger fails to
  allocate while torch holds gigabytes it is not using.
* **A VRAM reserve, not a VRAM cap.** This card also runs C2ME's OpenCL
  worldgen. `reserve_mb` is left free for it, enforced through torch's own
  per-process fraction so an over-large batch fails here rather than making
  someone else's allocation fail somewhere else.
* **Models unload when idle.** They are ~2.5 GB and reload in ~5s, so holding
  them all day to save a trickle of new downloads a five-second wait is the
  wrong trade on a card a game server and worldgen also want. `idle_unload_s`
  hands the VRAM back; `/v1/unload` does it on demand.
* **Work happens in a window.** `windows` defaults to 02:30-08:00: new
  downloads do not need indexing the moment they land, and the backlog is
  better spent on hours nobody is playing. `urgent=1` gets through anyway, so
  a person asking for one file now is not made to wait until morning.
* **2D -> 3D conversion runs in the gaps** (/v1/stereo/*, the section at the end).
  A job is hours of GPU, so it is a subprocess in its own venv (stereo/ksf/
  stereo_job.py) that the manager starts only when nothing else wants the card,
  and stops (SIGTERM, at most one 20 s chunk lost) the moment something does.
"""
from __future__ import annotations

import csv
import io
import json
import os
import threading
import time

import numpy as np
from flask import Flask, jsonify, request
from PIL import Image

import genre                    # Discogs-EffNet music styles, next to this file
import grain                    # film grain: measured on the original, put back after the upscale
import upscaler                 # Real-ESRGAN, next to this file

Image.MAX_IMAGE_PIXELS = 400_000_000        # library has some very large scans

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.environ.get("KSF_VISION_CONFIG",
                             os.path.join(BASE_DIR, "vision_config.json"))

# The largest request body waitress takes: a video uploaded for 3D or for
# Smooth motion. Above it waitress answers 413 and drops the connection while
# the body is still coming, which the sender only sees as "connection reset"
# (2026-10-29: a 9.4 GiB episode against the old 8 GiB, every 30 s for a day).
# /v1/stereo/status announces it so media-dl fails a bigger file itself.
UPLOAD_MAX_BYTES = 64 * 1024 ** 3
# waitress reads the whole body before the app sees it, into a temp file once
# it is over 512 KB. The default /tmp is the 48 GB root disk, which one film
# would nearly fill: the bodies are spooled here on /data instead.
UPLOAD_SPOOL_DIR = os.path.join(BASE_DIR, "upload-spool")

FLORENCE = "microsoft/Florence-2-large"
TAGGER = "SmilingWolf/wd-eva02-large-tagger-v3"
# buffalo_l, not dlib/face_recognition: this library is cosplayers and mostly
# East Asian faces, and dlib's model is measurably worse on non-white faces.
# ArcFace R100 trained on glint360k handles both far better.
FACE_MODEL = "buffalo_l"

# Below this the detector is guessing - a blurred bystander or a face on a
# poster in the background. Those poison clustering far more than they help,
# because a dozen vague faces cluster with each other.
MIN_FACE_SCORE = 0.55
# And below this many pixels there is not enough face left to identify, however
# confident the detector is.
MIN_FACE_PX = 48

# Under this many characters, Florence's OCR output is noise rather than text -
# measured on images with no text at all, which came back as "X", "0", "B".
MIN_OCR_CHARS = 3

# 0.35 rather than the usual 0.5: measured on real library images it finds a
# character on 63% of pixiv and 59% of twitter pictures that contain a person,
# against 47%/41% at 0.5, and 0.25 adds almost nothing over it.
TAG_THRESHOLDS = {"character": 0.35, "general": 0.35, "rating": 0.5}

app = Flask(__name__)
_lock = threading.Lock()          # one batch on the GPU at a time
MODELS = {}
STATE = {"paused": False, "batches": 0, "images": 0, "seconds": 0.0,
         "started": time.time(), "last_error": None,
         "last_request": time.time(), "unloaded_at": None,
         "upscale_waiting": 0, "upscales": 0, "upscale_seconds": 0.0,
         # For the 3D manager: the last analyze/audio request (separate from
         # last_request, which interpolation and upscales also move), the last
         # URGENT one (a person clicking - the only analyze/audio activity the
         # 3D start gate waits for; the indexer's batches never delay a job, they
         # are refused instead), the last Smooth motion request, and how many
         # interpolated streams are open.
         "last_batch": time.time(), "last_urgent": 0.0, "last_interp": 0.0, "interp_streaming": 0}


def config() -> dict:
    defaults = {
        "token": "", "batch_size": 8, "reserve_mb": 2048,
        "allow_cidrs": ["127.0.0.0/8"],
        # Give the card back when nobody is using it. The models are ~2.5 GB
        # and cost 5s to reload; holding them all day so that a trickle of new
        # downloads never waits is the wrong trade when a game server and
        # worldgen want the same 8 GB.
        # VRAM is split between THREE claimants on this card and none of them
        # can see the others: torch (Florence), onnxruntime (the tagger, whose
        # arena is "non-PyTorch memory" and is never handed back), and whatever
        # else runs here - the Minecraft server itself holds ~924 MiB, and
        # C2ME worldgen will want more. So each budget is stated rather than
        # left to whoever allocates first.
        "tagger_vram_mb": 1600,
        "tagger_batch": 4,
        # Images per Florence generate call. The indexer sends FILES and a
        # video is four frames, so the request size says nothing about memory;
        # the chunk size is decided here, next to the card.
        "florence_batch": 4,
        "idle_unload_s": 900,
        # When batches are accepted at all. Empty list = any time. Windows may
        # wrap past midnight, which is the normal case here: the quiet hours
        # are the small ones.
        "windows": [{"start": "02:30", "end": "08:00"}],
        # Work asked for outside the window is refused with 503 and a reason,
        # unless the caller says it is urgent - that is how a person clicking
        # "index this now" gets through while the nightly sweep does not.
        "allow_urgent_anytime": True,
        # Live upscaling for media-dl (upscaler.py). Tiles bound the VRAM one
        # image can take; the output cap refuses an absurd request before any
        # GPU time is spent on it.
        "upscale_model_dir": os.path.join(BASE_DIR, "models"),
        "upscale_tile": 384,
        # The largest result it will make - the ceiling of media-dl's "Largest
        # result" setting (256 MP there). media-dl reads it from /v1/status
        # and plans within it, so lowering it here is safe. ~768 MB of RGB.
        "upscale_max_output_px": 256_000_000,
        # An extra model pass for an in-between factor (then Lanczos down) may
        # grow the image past the output this far, in RAM - about 768 MB.
        "upscale_max_work_px": 256_000_000,
        "upscale_quality": 92,
        # Music styles for media-dl (genre.py): the model files and its VRAM.
        # It is small (18 MB of weights), so its arena is too.
        "genre_model_dir": os.path.join(BASE_DIR, "models", "audio"),
        "genre_vram_mb": 512,
    }
    try:
        with open(CONFIG_PATH, encoding="utf-8") as fh:
            defaults.update(json.load(fh))
    except (OSError, ValueError):
        pass
    return defaults


# ------------------------------------------------------------------ window --

def _minutes(hhmm, fallback):
    try:
        h, m = str(hhmm).split(":")
        return int(h) * 60 + int(m)
    except (ValueError, AttributeError):
        return fallback


def in_window(now=None) -> bool:
    """Whether the schedule allows work right now.

    A window whose end is before its start wraps past midnight - 02:30-08:00
    does not, but 22:00-06:00 would, and getting that wrong means the worker
    silently refuses all night.
    """
    return _in_windows(config().get("windows") or [], now)


def _in_windows(windows, now=None) -> bool:
    if not windows:
        return True
    now = now or time.localtime()
    cur = now.tm_hour * 60 + now.tm_min
    for w in windows:
        start = _minutes(w.get("start"), 0)
        end = _minutes(w.get("end"), 24 * 60)
        if start <= end:
            if start <= cur < end:
                return True
        elif cur >= start or cur < end:      # wraps midnight
            return True
    return False


def next_window_text() -> str:
    windows = config().get("windows") or []
    if not windows:
        return "any time"
    return ", ".join("%s-%s" % (w.get("start"), w.get("end")) for w in windows)


# ------------------------------------------------------------- idle unload --

def unload_models(reason="idle"):
    """Everything, both models. Safe when nothing is loaded, and never called
    while a batch runs - the caller holds _lock."""
    freed = (unload_florence(reason) | unload_tagger(reason) | unload_faces(reason)
             | unload_genre(reason) | upscaler.unload(reason))
    if freed:
        MODELS.pop("torch", None)      # popped last: it frees the allocator
        STATE["unloaded_at"] = time.time()
    return bool(freed)


def idle_watcher():
    """Unloads after idle_unload_s with no work. A thread rather than a check
    inside analyze(), because the whole point is to act when nothing is
    calling."""
    while True:
        time.sleep(30)
        try:
            cfg = config()
            idle_for = time.time() - STATE["last_request"]
            if (cfg.get("idle_unload_s")
                    and (any(k in MODELS for k in ("florence", "tagger", "faces", "genre"))
                         or upscaler.loaded())
                    and idle_for > cfg["idle_unload_s"]):
                if _lock.acquire(blocking=False):
                    try:
                        unload_models("idle %.0fs" % idle_for)
                    finally:
                        _lock.release()
        except Exception as exc:                 # a watcher must never die
            print("[vision] idle watcher: %s" % exc, flush=True)


# ------------------------------------------------------------------ models --

def vram_budget(cfg, torch, share):
    """`share` of the card after the reserve. With one model resident it gets
    everything not reserved; the split only exists for allow_both."""
    total_mb = torch.cuda.get_device_properties(0).total_memory / 2**20
    return max(256.0, (total_mb - cfg["reserve_mb"]) * share), total_mb


def load_florence():
    """Loaded on first use rather than at import, so the unit comes up
    immediately and a model download does not look like a hang."""
    import torch
    from transformers import AutoModelForCausalLM, AutoProcessor

    cfg = config()
    share = 0.6 if cfg.get("allow_both") else 1.0
    budget, total_mb = vram_budget(cfg, torch, share)
    torch.cuda.set_per_process_memory_fraction(
        max(0.1, min(1.0, budget / total_mb)))

    MODELS["torch"] = torch
    MODELS["florence"] = AutoModelForCausalLM.from_pretrained(
        FLORENCE, torch_dtype=torch.float16, trust_remote_code=True).cuda().eval()
    MODELS["processor"] = AutoProcessor.from_pretrained(
        FLORENCE, trust_remote_code=True)
    print("[vision] florence loaded; %.0f MiB budget of %.0f MiB"
          % (budget, total_mb), flush=True)


def load_tagger():
    cfg = config()
    import onnxruntime as ort
    from huggingface_hub import hf_hub_download
    # kSameAsRequested rather than the default kNextPowerOfTwo: the default
    # doubles the arena on every growth and overshoots badly for a workload
    # whose batch shape never changes.
    sess = ort.InferenceSession(
        hf_hub_download(TAGGER, "model.onnx"),
        providers=[("CUDAExecutionProvider", {
            "gpu_mem_limit": cfg["tagger_vram_mb"] * 1024 * 1024,
            "arena_extend_strategy": "kSameAsRequested",
        }), "CPUExecutionProvider"])
    if sess.get_providers()[0] != "CUDAExecutionProvider":
        # It does NOT raise when the CUDA provider fails to load - it warns and
        # silently runs on CPU, which is 20x slower and looks like a slow disk.
        print("[vision] WARNING: tagger fell back to CPU; check LD_LIBRARY_PATH "
              "points at torch's bundled nvidia libs", flush=True)
    names, cats = [], []
    with open(hf_hub_download(TAGGER, "selected_tags.csv"),
              newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            names.append(row["name"])
            cats.append(int(row["category"]))
    MODELS["tagger"] = sess
    MODELS["tag_names"] = np.array(names)
    MODELS["tag_cats"] = np.array(cats)
    MODELS["tag_input"] = sess.get_inputs()[0]
    print("[vision] tagger loaded; %d MiB arena limit" % cfg["tagger_vram_mb"],
          flush=True)


def load_faces():
    cfg = config()
    os.environ.setdefault("INSIGHTFACE_HOME", os.path.join(BASE_DIR, "insightface"))
    from insightface.app import FaceAnalysis
    app = FaceAnalysis(name=FACE_MODEL,
                       providers=[("CUDAExecutionProvider", {
                           "gpu_mem_limit": cfg["tagger_vram_mb"] * 1024 * 1024,
                           "arena_extend_strategy": "kSameAsRequested",
                       }), "CPUExecutionProvider"])
    app.prepare(ctx_id=0, det_size=(640, 640))
    used = app.models["detection"].session.get_providers()[0]
    if used != "CUDAExecutionProvider":
        # Worth shouting about: installing `insightface` pulls in the CPU
        # onnxruntime, which OVERWRITES onnxruntime-gpu and silently drops
        # every model in this process to CPU. It is a warning, not an error.
        print("[vision] WARNING: face models on %s - reinstall onnxruntime-gpu"
              % used, flush=True)
    MODELS["faces"] = app
    print("[vision] face models loaded (%s)" % used, flush=True)


def unload_faces(reason=""):
    if "faces" not in MODELS:
        return False
    MODELS.pop("faces", None)
    import gc
    gc.collect()
    print("[vision] face models unloaded (%s)" % reason, flush=True)
    return True


def unload_genre(reason=""):
    if "genre" not in MODELS:
        return False
    MODELS.pop("genre", None)
    import gc
    gc.collect()
    print("[vision] genre model unloaded (%s)" % reason, flush=True)
    return True


def unload_florence(reason=""):
    if "florence" not in MODELS:
        return False
    torch = MODELS.get("torch")
    MODELS.pop("florence", None)
    MODELS.pop("processor", None)
    if torch is not None:
        import gc
        gc.collect()
        torch.cuda.empty_cache()
    print("[vision] florence unloaded (%s)" % reason, flush=True)
    return True


def unload_tagger(reason=""):
    if "tagger" not in MODELS:
        return False
    for key in ("tagger", "tag_names", "tag_cats", "tag_input"):
        MODELS.pop(key, None)
    import gc
    gc.collect()
    # onnxruntime releases its arena when the session is collected, which is
    # the only reason evicting it is possible at all.
    print("[vision] tagger unloaded (%s)" % reason, flush=True)
    return True


def ensure_models(need_florence=True, need_tagger=True, need_faces=False):
    """Loads what this batch needs, and evicts what it does not.

    ONE MODEL AT A TIME by default. The card is 8 GB and the claimants do not
    fit together: Florence peaks at ~4.0 GB, the tagger arena wants ~1.7 GB and
    never returns it, the Minecraft server holds ~0.9 GB, and the CUDA context
    is another ~0.5 GB. Holding both turned every second batch into an
    out-of-memory - sometimes torch failing, sometimes onnxruntime, depending
    on which allocator asked first, which is why it looked like a different
    bug each time.

    So a sweep runs one KIND of pass: caption+ocr over the library, then tags
    over the library. Switching per batch would thrash, and avoiding that is
    the caller's job, not something for this function to hide.

    `allow_both` in the config turns this off for a card with the room.

    The CALLER HOLDS _lock, and keeps holding it until its passes are done.
    This used to take the lock itself and let go before the batch ran, so a
    second batch could evict in that gap what the first had just loaded: a
    music cover (caption, tags, faces) loaded Florence, the indexer's next
    tags-only batch unloaded it a second later, and the cover's pass failed
    with KeyError 'processor' (vault KSF-059).
    """
    cfg = config()
    if cfg.get("allow_both"):
        need_florence = need_tagger = True
    # Evict whatever this batch does not need before loading what it does.
    if not need_florence and "florence" in MODELS:
        unload_florence("not needed by this pass")
    if not need_tagger and "tagger" in MODELS:
        unload_tagger("not needed by this pass")
    if not need_faces and "faces" in MODELS:
        unload_faces("not needed by this pass")
    if need_florence and "florence" not in MODELS:
        load_florence()
    if need_tagger and "tagger" not in MODELS:
        load_tagger()
    if need_faces and "faces" not in MODELS:
        load_faces()


# ------------------------------------------------------------------ passes --

def _florence_chunk(images, task):
    torch = MODELS["torch"]
    proc, model = MODELS["processor"], MODELS["florence"]
    inputs = proc(text=[task] * len(images), images=images,
                  return_tensors="pt").to("cuda", torch.float16)
    with torch.inference_mode():
        ids = model.generate(input_ids=inputs["input_ids"],
                             pixel_values=inputs["pixel_values"],
                             max_new_tokens=256, num_beams=1, do_sample=False)
    return [t.strip() for t in proc.batch_decode(ids, skip_special_tokens=True)]


def florence(images, task):
    """Captions or OCR for any number of images, in chunks that fit the card.

    Eight videos used to arrive as 32 frames in ONE generate call and ran the
    card out of memory (5.6 GiB allocated of 6.45 allowed) on the first
    library-wide caption pass. A chunk that still does not fit is retried in
    halves, down to a single image, before it is reported as a failure.
    """
    torch = MODELS["torch"]
    step = max(1, int(config().get("florence_batch", 4)))

    def run(chunk):
        try:
            return _florence_chunk(chunk, task)
        except torch.cuda.OutOfMemoryError:
            torch.cuda.empty_cache()
            if len(chunk) == 1:
                raise
            half = len(chunk) // 2
            return run(chunk[:half]) + run(chunk[half:])

    out = []
    for i in range(0, len(images), step):
        out.extend(run(images[i:i + step]))
    return out


def tagger_prep(img):
    """WD14 wants a square, white-padded, BGR, 0-255 float image. Every one of
    those four is a silent wrong-answer if you skip it rather than an error."""
    size = MODELS["tag_input"].shape[1]
    img = img.convert("RGBA")
    flat = Image.new("RGBA", img.size, (255, 255, 255))
    flat.alpha_composite(img)
    img = flat.convert("RGB")
    side = max(img.size)
    square = Image.new("RGB", (side, side), (255, 255, 255))
    square.paste(img, ((side - img.size[0]) // 2, (side - img.size[1]) // 2))
    return np.asarray(square.resize((size, size), Image.BICUBIC),
                      dtype=np.float32)[:, :, ::-1]


def tag(images):
    sess, inp = MODELS["tagger"], MODELS["tag_input"]
    names, cats = MODELS["tag_names"], MODELS["tag_cats"]
    # Sub-batched: one attention matmul over 8 images at 448x448 asks for
    # 537 MB in a single buffer, and it is that single allocation - not the
    # total - that fails. Throughput is unaffected; this model is not
    # batch-limited.
    step = max(1, int(config().get("tagger_batch", 4)))
    chunks = []
    for i in range(0, len(images), step):
        batch = np.stack([tagger_prep(im) for im in images[i:i + step]])
        chunks.append(sess.run(None, {inp.name: batch})[0])
    probs = np.concatenate(chunks) if chunks else np.zeros((0, len(names)))
    out = []
    for p in probs:
        def pick(cat, thr):
            idx = np.where((cats == cat) & (p > thr))[0]
            idx = idx[np.argsort(-p[idx])]
            return [{"tag": str(names[i]), "p": round(float(p[i]), 3)} for i in idx]
        out.append({"character": pick(4, TAG_THRESHOLDS["character"]),
                    "general": pick(0, TAG_THRESHOLDS["general"]),
                    "rating": pick(9, TAG_THRESHOLDS["rating"])})
    return out


def detect_faces(images):
    """Per image: the faces worth keeping, with a normalised 512-d embedding.

    Embeddings go over the wire base64-encoded rather than as 512 JSON floats:
    the array is the bulk of the response and JSON numbers would roughly
    triple it for no gain, since nothing between here and the database reads
    an individual component.
    """
    import base64
    import numpy as np
    app = MODELS["faces"]
    out = []
    for img in images:
        # insightface wants BGR, and PIL hands us RGB. Getting this backwards
        # does not fail - it quietly identifies people slightly worse.
        arr = np.asarray(img)[:, :, ::-1]
        found = []
        for f in app.get(arr):
            x1, y1, x2, y2 = [float(v) for v in f.bbox]
            w, h = x2 - x1, y2 - y1
            if float(f.det_score) < MIN_FACE_SCORE:
                continue
            if min(w, h) < MIN_FACE_PX:
                continue
            emb = f.normed_embedding.astype("float32")
            found.append({
                "bbox": [round(x1, 1), round(y1, 1), round(w, 1), round(h, 1)],
                "det_score": round(float(f.det_score), 4),
                # Sharpness stands in for quality: a crisp small face is worth
                # more to clustering than a large blurred one.
                "quality": round(float(min(w, h)) * float(f.det_score), 2),
                "embedding_b64": base64.b64encode(emb.tobytes()).decode("ascii"),
            })
        out.append(found)
    return out


# The orientation pass (media_indexer "orient"): is a video's picture lying on
# its side? Some channels upload phone footage as a landscape file whose
# content is turned 90 degrees, with no rotation flag a player could read.
#
# Each frame is searched for faces as stored and turned 90, 180 and 270
# degrees clockwise. The detector alone does not decide it - SCRFD finds the
# same sideways face in every turn at 0.6-0.8 - but its five landmarks do: a
# face counts for a turn only when, in that turn, its eyes sit above its mouth.
# The answer is left to the indexer, which sees all of a video's frames; this
# only reports what each turn showed.
ORIENT_TURNS = ((0, None), (90, Image.Transpose.ROTATE_270),
                (180, Image.Transpose.ROTATE_180), (270, Image.Transpose.ROTATE_90))
ORIENT_MIN_SCORE = 0.5
ORIENT_MIN_PX = 40


def orient_faces(images):
    """Per image: {"0": [[score, short side px, up], ...], "90": ..., ...} -
    the faces found with the picture turned that many degrees clockwise. `up`
    is where the face's top points in that turn, in degrees: 0 straight up, 90
    right, -90 left; None when the detector gave no landmarks."""
    import math
    det = MODELS["faces"].det_model
    out = []
    for img in images:
        rec = {}
        for deg, turn in ORIENT_TURNS:
            arr = np.asarray(img if turn is None else img.transpose(turn))[:, :, ::-1]
            bboxes, kpss = det.detect(arr, max_num=0, metric="default")
            faces = []
            for i, b in enumerate(bboxes):
                x1, y1, x2, y2, score = (float(v) for v in b)
                px = min(x2 - x1, y2 - y1)
                if score < ORIENT_MIN_SCORE or px < ORIENT_MIN_PX:
                    continue
                up = None
                if kpss is not None:
                    k = kpss[i]
                    # Eyes minus mouth; image y grows downwards.
                    vx = (k[0][0] + k[1][0] - k[3][0] - k[4][0]) / 2
                    vy = (k[0][1] + k[1][1] - k[3][1] - k[4][1]) / 2
                    up = round(math.degrees(math.atan2(vx, -vy)), 1)
                faces.append([round(score, 3), round(px), up])
            rec[str(deg)] = faces
        out.append(rec)
    return out


def clean_ocr(text):
    stripped = "".join(ch for ch in (text or "") if ch.strip())
    return text.strip() if len(stripped) >= MIN_OCR_CHARS else ""


# ------------------------------------------------------------------ routes --

def authorised(req):
    cfg = config()
    if not cfg.get("token"):
        return True                       # unset token means lab-only, open
    got = (req.headers.get("authorization") or "").removeprefix("Bearer ").strip()
    import hmac
    return hmac.compare_digest(got.encode(), str(cfg["token"]).encode())


@app.route("/v1/status")
def status():
    cfg = config()
    body = {"paused": STATE["paused"], "batches": STATE["batches"],
            "images": STATE["images"],
            "loaded": [k for k in ("florence", "tagger", "faces", "genre") if k in MODELS]
                      + ["upscale:" + n for n in upscaler.loaded()],
            "upscale_models": sorted(upscaler.MODEL_SPECS),
            "upscales": STATE["upscales"], "upscale_waiting": STATE["upscale_waiting"],
            "upscale_max_output_px": int(cfg.get("upscale_max_output_px") or 0),
            "upscale_max_work_px": int(cfg.get("upscale_max_work_px") or 0),
            "batch_size": cfg["batch_size"], "reserve_mb": cfg["reserve_mb"],
            "last_error": STATE["last_error"],
            "in_window": in_window(), "windows": next_window_text(),
            "idle_unload_s": cfg.get("idle_unload_s"),
            "idle_for_s": round(time.time() - STATE["last_request"]),
            "unloaded_at": STATE["unloaded_at"]}
    if STATE["images"]:
        body["seconds_per_image"] = round(STATE["seconds"] / STATE["images"], 4)
    if "torch" in MODELS:
        t = MODELS["torch"]
        body["vram_allocated_mb"] = round(t.cuda.memory_allocated() / 2**20)
        body["vram_peak_mb"] = round(t.cuda.max_memory_allocated() / 2**20)
    return jsonify(body)


@app.route("/v1/pause", methods=["POST"])
def pause():
    """So worldgen can have the card back without stopping this service and
    losing the loaded models."""
    if not authorised(request):
        return jsonify({"error": "unauthorised"}), 401
    STATE["paused"] = bool((request.json or {}).get("paused", True))
    return jsonify({"paused": STATE["paused"]})


@app.route("/v1/unload", methods=["POST"])
def unload():
    """Hand the VRAM back now - for when worldgen wants the card and nobody
    feels like waiting out the idle timer."""
    if not authorised(request):
        return jsonify({"error": "unauthorised"}), 401
    with _lock:
        freed = unload_models("asked")
    return jsonify({"unloaded": freed})


@app.route("/v1/upscale", methods=["POST"])
def upscale_route():
    """One image in, the same picture `factor` times larger out.

    Interactive - someone in media-dl is looking at this image - so the
    processing window does not apply and it goes ahead of the indexer's next
    batch (see analyze). It still runs under the one GPU lock and inside the
    same VRAM reserve as every other model here.
    Form: file, model (a key of upscaler.MODEL_SPECS: anime, general or a photo
    model), factor (any whole number from 2), or
    width + height (an exact size in the picture's proportions, any scale above
    1.05 - media-dl sends that when a megapixel limit binds), quality (optional),
    keep_larger + max_px + max_scale (optional: a pass that overshoots is handed
    back unshrunk, within max_px pixels and max_scale times the picture),
    grain + grain_hint (optional, "1": measure the original's film grain and put
    back what the model took - see grain.py; grain_hint says the tagger already
    called it film_grain). The answer's X-Upscale-Grain header says what was done.
    The output has to fit upscale_max_output_px either way.
    """
    if not authorised(request):
        return jsonify({"error": "unauthorised"}), 401
    cfg = config()
    model = (request.form.get("model") or "general").strip()
    if model not in upscaler.MODEL_SPECS:
        return jsonify({"error": "unknown model %s" % model}), 400
    try:
        factor = int(request.form.get("factor") or 2)
    except ValueError:
        factor = 0
    target = None
    if request.form.get("width") or request.form.get("height"):
        try:
            target = (int(request.form.get("width") or 0), int(request.form.get("height") or 0))
        except ValueError:
            target = (0, 0)
        if min(target) < 1:
            return jsonify({"error": "width and height must be whole numbers above 0"}), 400
    elif factor < 2:
        return jsonify({"error": "factor must be a whole number of at least 2"}), 400
    upload = request.files.get("file")
    if not upload:
        return jsonify({"error": "no file"}), 400
    try:
        img = Image.open(io.BytesIO(upload.read()))
        img.load()
    except Exception as exc:
        return jsonify({"error": "unreadable image: %s" % str(exc)[:200]}), 422
    if target:
        # An exact size must keep the picture's proportions (2 px of floor
        # rounding allowed) and be larger than it.
        tw, th = target
        s = max(tw / img.width, th / img.height)
        if abs(tw - img.width * s) > 2 or abs(th - img.height * s) > 2:
            return jsonify({"error": "%dx%d does not keep the proportions of %dx%d"
                                     % (tw, th, img.width, img.height)}), 400
        if s < 1.05:
            return jsonify({"error": "%dx%d is not larger than %dx%d" % (tw, th, img.width, img.height)}), 400
        factor = max(2, int(s) + (0 if s == int(s) else 1))
        out_px, asked = tw * th, "%dx%d" % (tw, th)
    else:
        out_px, asked = img.width * img.height * factor * factor, "%dx" % factor
    limit = int(cfg.get("upscale_max_output_px") or 0)
    if out_px > limit:
        # Said with the numbers, and the limit as a field, so media-dl can plan
        # within it instead of passing on a bare refusal.
        return jsonify({"error": "%s of %dx%d would be %.1f MP, over this worker's limit of %.0f MP"
                                 % (asked, img.width, img.height, out_px / 1e6, limit / 1e6),
                        "max_output_px": limit, "output_px": out_px}), 413
    try:
        quality = max(50, min(100, int(request.form.get("quality") or cfg.get("upscale_quality") or 92)))
    except ValueError:
        quality = 92
    keep_larger = (request.form.get("keep_larger") or "").strip().lower() in ("1", "true", "yes")
    try:
        max_px = int(request.form.get("max_px") or 0) or None
    except ValueError:
        max_px = None
    if limit:
        max_px = min(max_px or limit, limit)          # never past this worker's own ceiling
    try:
        max_scale = float(request.form.get("max_scale") or 0) or None
    except ValueError:
        max_scale = None
    want_grain = (request.form.get("grain") or "").strip() == "1"
    grain_hint = (request.form.get("grain_hint") or "").strip() == "1"

    STATE["upscale_waiting"] += 1
    queued = True
    try:
        with _lock:
            STATE["upscale_waiting"] -= 1
            queued = False
            import torch
            if "torch" not in MODELS:
                budget, total_mb = vram_budget(cfg, torch, 1.0)
                torch.cuda.set_per_process_memory_fraction(max(0.1, min(1.0, budget / total_mb)))
                MODELS["torch"] = torch
            try:
                out, seconds = upscaler.upscale(torch, img, model, factor, cfg["upscale_model_dir"],
                                                tile=int(cfg.get("upscale_tile") or 384),
                                                max_work_px=int(cfg.get("upscale_max_work_px") or 256_000_000),
                                                target=target, keep_larger=keep_larger,
                                                max_px=max_px, max_scale=max_scale)
            finally:
                torch.cuda.empty_cache()
    except Exception as exc:
        STATE["last_error"] = ("upscale: %s" % exc)[:300]
        return jsonify({"error": str(exc)[:300]}), 500
    finally:
        if queued:
            STATE["upscale_waiting"] -= 1

    # Film grain, outside the GPU lock: it is numpy on the CPU (about 1 s for a
    # 4320x4320 result), and the indexer's next batch should not wait for it.
    # It can only add to an upscale, never cost one: any failure is reported
    # and the plain result goes out.
    grain_info = None
    if want_grain:
        try:
            src_rgb = np.asarray(img.convert("RGB"))
            profile = grain.analyse(src_rgb, hinted=grain_hint)
            alpha = out.getchannel("A") if out.mode == "RGBA" else None
            rgb, grain_info = grain.restore(np.asarray(out.convert("RGB")), profile, source_rgb=src_rgb)
            if grain_info.get("added"):
                out = Image.fromarray(rgb)
                if alpha is not None:
                    out.putalpha(alpha)
        except Exception as exc:
            grain_info = {"added": False, "why": ("the grain step failed: %s" % exc)[:200]}
            STATE["last_error"] = ("grain: %s" % exc)[:300]

    # WebP stops at 16383 px a side; past that JPEG (or PNG with alpha).
    buf = io.BytesIO()
    if max(out.size) <= 16383:
        out.save(buf, "WEBP", quality=quality, method=4)
        mimetype = "image/webp"
    elif out.mode == "RGBA":
        out.save(buf, "PNG")
        mimetype = "image/png"
    else:
        out.save(buf, "JPEG", quality=min(quality + 3, 97))
        mimetype = "image/jpeg"
    STATE["upscales"] += 1
    STATE["upscale_seconds"] += seconds
    STATE["last_request"] = time.time()
    resp = app.response_class(buf.getvalue(), mimetype=mimetype)
    resp.headers["X-Upscale-Model"] = model
    resp.headers["X-Upscale-Seconds"] = "%.2f" % seconds
    resp.headers["X-Upscale-Size"] = "%dx%d" % out.size
    if grain_info is not None:
        resp.headers["X-Upscale-Grain"] = json.dumps(grain_info, ensure_ascii=True, separators=(",", ":"))
    return resp


@app.route("/v1/analyze", methods=["POST"])
def analyze():
    """One batch. Files come as multipart; `tasks` picks the passes.

    The whole batch is held in memory and dropped when the response is
    written - nothing about this backlog is allowed to land on this host's
    disk.
    """
    if not authorised(request):
        return jsonify({"error": "unauthorised"}), 401
    if STATE["paused"]:
        return jsonify({"error": "paused"}), 503

    cfg = config()
    urgent = (request.form.get("urgent") or "").strip() in ("1", "true")
    if not in_window() and not (urgent and cfg.get("allow_urgent_anytime")):
        # Refused, not queued: the indexer owns the backlog and knows how to
        # come back. A worker that queued would hold the files in memory for
        # hours.
        return jsonify({"error": "outside processing window",
                        "windows": next_window_text(),
                        "retry_after_s": 600}), 503
    # A 3D conversion owns the card for hours. The indexer's sweep waits (it
    # backs off on any 503); a person asking for one file now stops the job.
    if urgent:
        stereo_preempt("urgent analyze")
    else:
        why = stereo_yield_reason()
        if why:
            return jsonify({"error": why, "retry_after_s": 600}), 503

    STATE["last_request"] = STATE["last_batch"] = time.time()
    if urgent:
        STATE["last_urgent"] = STATE["last_batch"]
    tasks = set((request.form.get("tasks") or "caption,ocr,tags").split(","))
    files = request.files.getlist("file")
    if not files:
        return jsonify({"error": "no files"}), 400

    names, images, failed = [], [], []
    for f in files:
        try:
            img = Image.open(io.BytesIO(f.read()))
            img.load()
            images.append(img.convert("RGB"))
            names.append(f.filename)
        except Exception as exc:
            # A file the decoder refuses is a result, not an outage: it is
            # recorded so the indexer marks it done rather than retrying it
            # every pass forever.
            failed.append({"name": f.filename, "error": str(exc)[:200]})

    results = [{"name": n} for n in names]
    if images:
        # Someone waiting for an upscale is looking at a picture right now:
        # this batch starts only once no upscale is queued for the GPU.
        waited = time.time()
        while STATE["upscale_waiting"] and time.time() - waited < 120:
            time.sleep(0.05)
        try:
            with _lock:
                # The 3D manager starts a job under this same lock: a batch that
                # queued here while it did must not run beside the job.
                if not urgent and stereo_running():
                    return jsonify({"error": "3D conversion running", "retry_after_s": 600}), 503
                # Loaded under the same hold as the passes that use them.
                ensure_models(need_florence=bool({"caption", "ocr"} & tasks),
                              need_tagger="tags" in tasks,
                              need_faces=bool({"faces", "orient"} & tasks))
                started = time.time()
                if "caption" in tasks:
                    for r, c in zip(results, florence(images, "<DETAILED_CAPTION>")):
                        r["caption"] = c
                if "ocr" in tasks:
                    for r, t in zip(results, florence(images, "<OCR>")):
                        r["ocr"] = clean_ocr(t)
                if "tags" in tasks:
                    # torch and onnxruntime keep SEPARATE allocators on the
                    # same card. Torch's caching allocator hangs on to the
                    # blocks Florence just freed, so ONNX asks the driver for
                    # its 537 MB workspace and is refused while torch is
                    # sitting on gigabytes it is not using - which surfaces as
                    # a bare "Failed to allocate memory" from a FusedMatMul,
                    # not as anything resembling an out-of-memory in torch.
                    # Handing the cache back before the tagger runs is what
                    # makes two frameworks share one 8 GB card.
                    if "torch" in MODELS:
                        MODELS["torch"].cuda.empty_cache()
                    for r, t in zip(results, tag(images)):
                        r["tags"] = t
                if "faces" in tasks:
                    for r, f in zip(results, detect_faces(images)):
                        r["faces"] = f
                if "orient" in tasks:
                    for r, o in zip(results, orient_faces(images)):
                        r["orient"] = o
        except Exception as exc:
            STATE["last_error"] = str(exc)[:300]
            return jsonify({"error": str(exc)[:300]}), 500
        elapsed = time.time() - started
        STATE["last_request"] = STATE["last_batch"] = time.time()
        if urgent:
            STATE["last_urgent"] = STATE["last_batch"]
        STATE["batches"] += 1
        STATE["images"] += len(images)
        STATE["seconds"] += elapsed
        STATE["last_error"] = None

    return jsonify({"results": results, "failed": failed,
                    "models": {"caption": FLORENCE, "ocr": FLORENCE,
                               "tags": TAGGER, "faces": FACE_MODEL, "orient": FACE_MODEL}})


@app.route("/v1/audio", methods=["POST"])
def audio_route():
    """One track in, its music styles and embedding out (genre.py).

    Body: the track as 16 kHz mono signed 16-bit little-endian PCM - media-dl
    decodes it with ffmpeg, which is also how a 30-minute mix is cut down to a
    few windows before anything crosses the network. Query: urgent=1 to pass
    the processing window, like /v1/analyze. Answers the 400 style scores
    (only those of 0.01 or more, highest first) and the embedding as base64
    float16.
    """
    import base64

    if not authorised(request):
        return jsonify({"error": "unauthorised"}), 401
    if STATE["paused"]:
        return jsonify({"error": "paused"}), 503
    cfg = config()
    urgent = (request.args.get("urgent") or "").strip() in ("1", "true")
    if not in_window() and not (urgent and cfg.get("allow_urgent_anytime")):
        return jsonify({"error": "outside processing window", "windows": next_window_text(),
                        "retry_after_s": 600}), 503
    if urgent:
        stereo_preempt("urgent audio")
    else:
        why = stereo_yield_reason()
        if why:
            return jsonify({"error": why, "retry_after_s": 600}), 503
    body = request.get_data(cache=False)
    if len(body) < 2 * genre.SAMPLE_RATE * 3:
        return jsonify({"error": "under 3 s of audio"}), 422
    audio = np.frombuffer(body[:len(body) // 2 * 2], dtype="<i2").astype(np.float32) / 32768.0
    STATE["last_request"] = STATE["last_batch"] = time.time()
    if urgent:
        STATE["last_urgent"] = STATE["last_batch"]
    # Someone waiting for an upscale is looking at a picture right now.
    waited = time.time()
    while STATE["upscale_waiting"] and time.time() - waited < 120:
        time.sleep(0.05)
    started = time.time()
    try:
        with _lock:
            if not urgent and stereo_running():          # see analyze(): the manager starts jobs under this lock
                return jsonify({"error": "3D conversion running", "retry_after_s": 600}), 503
            if "genre" not in MODELS:
                if "torch" in MODELS:
                    MODELS["torch"].cuda.empty_cache()     # onnxruntime asks the driver itself
                MODELS["genre"] = genre.load(cfg["genre_model_dir"], cfg.get("genre_vram_mb", 512))
                print("[vision] genre model loaded (%s)" % MODELS["genre"]["provider"], flush=True)
            result = genre.analyse(MODELS["genre"], audio)
    except Exception as exc:
        STATE["last_error"] = ("audio: %s" % exc)[:300]
        return jsonify({"error": str(exc)[:300]}), 500
    if result is None:
        return jsonify({"error": "too short to analyse"}), 422
    scores, embedding, n = result
    styles = MODELS["genre"]["styles"]
    order = np.argsort(-scores)
    STATE["last_request"] = STATE["last_batch"] = time.time()
    if urgent:
        STATE["last_urgent"] = STATE["last_batch"]
    return jsonify({
        "model": genre.MODEL_NAME,
        "styles": [[styles[i], round(float(scores[i]), 4)] for i in order if scores[i] >= 0.01],
        "embedding": base64.b64encode(embedding.astype("<f2").tobytes()).decode("ascii"),
        "dim": int(embedding.shape[0]),
        "patches": n,
        "seconds": round(time.time() - started, 2),
        "provider": MODELS["genre"]["provider"],
    })


# ------------------------------------------------------ frame interpolation --
# media-dl's player can ask for a video at a higher frame rate. The frames are
# generated by Lossless Scaling frame generation (LSFG) through vidinterp-gpu
# (vidinterp/ next to this file, installed by vidinterp/install.sh), which
# decodes, interpolates and encodes on the GPU and writes fragmented MP4 to
# stdout. This host cannot reach media-dl (the firewall only lets media-dl in on
# this port), so media-dl uploads the video once, keyed by what it is, and then
# asks for streams from any start position.

import re
import shutil
import subprocess

INTERP_KEY = re.compile(r"^[0-9a-f]{16,64}$")
_interp_streams = threading.BoundedSemaphore(1)   # replaced once config is read
_interp_streams_n = 0
_interp_upload_lock = threading.Lock()
INTERP_OUT_FPS = (10.0, 480.0)     # the output rates ?fps= / f<rate> may ask for


def interp_config() -> dict:
    cfg = config()
    home = cfg.get("interp_home") or os.path.join(BASE_DIR, "vidinterp")
    return {
        "bin": cfg.get("interp_bin") or os.path.join(home, "bin", "vidinterp-gpu"),
        "dll": cfg.get("interp_dll") or os.path.join(home, "lsfg-vk.dll"),
        "cache_dir": cfg.get("interp_cache_dir") or os.path.join(BASE_DIR, "interp-cache"),
        # Sources and the interpolated segments of each (a 4K120 video at 20 Mbit/s
        # is ~150 MB a minute); /data has ~450 GB free.
        "cache_gb": float(cfg.get("interp_cache_gb") or 100),
        # NVENC is the limit on this card: one 4K60 x2 stream takes ~55% of it.
        "max_streams": int(cfg.get("interp_max_streams") or 1),
        "max_height": int(cfg.get("interp_max_height") or 2160),
    }


def interp_streams():
    """The semaphore limiting concurrent streams, rebuilt if the limit changed."""
    global _interp_streams, _interp_streams_n
    n = interp_config()["max_streams"]
    if n != _interp_streams_n:
        _interp_streams, _interp_streams_n = threading.BoundedSemaphore(n), n
    return _interp_streams


def interp_available(icfg):
    missing = [what for what, path in (("vidinterp-gpu", icfg["bin"]), ("lsfg-vk.dll", icfg["dll"]))
               if not os.path.isfile(path)]
    return (not missing), missing


def interp_source_path(icfg, key):
    return os.path.join(icfg["cache_dir"], key + ".src")


def interp_evict(icfg, keep_bytes, keep_key=None):
    """Oldest-used first until the cache plus `keep_bytes` fits: uploaded sources
    (<key>.src) and interpolated segments (<key>/), never those of `keep_key`."""
    limit = icfg["cache_gb"] * 1024 ** 3
    entries = []
    try:
        names = os.listdir(icfg["cache_dir"])
    except OSError:
        return
    for n in names:
        path = os.path.join(icfg["cache_dir"], n)
        try:
            if n.endswith(".src") and os.path.isfile(path):
                st = os.stat(path)
                entries.append((st.st_mtime, st.st_size, path, n[:-4]))
            elif INTERP_KEY.match(n) and os.path.isdir(path):
                size = sum(os.path.getsize(os.path.join(r, f)) for r, _, fs in os.walk(path) for f in fs)
                entries.append((os.stat(path).st_mtime, size, path, n))
        except OSError:
            pass
    total = sum(e[1] for e in entries) + keep_bytes
    for _, size, path, key in sorted(entries):
        if total <= limit:
            break
        if key == keep_key:
            continue
        try:
            if os.path.isdir(path):
                shutil.rmtree(path)
            else:
                os.remove(path)
            total -= size
        except OSError:
            pass


@app.route("/v1/interp/status")
def interp_status():
    """Whether interpolation can run here, and whether a source is cached."""
    if not authorised(request):
        return jsonify({"error": "unauthorised"}), 401
    icfg = interp_config()
    ok, missing = interp_available(icfg)
    body = {"available": ok, "missing": missing, "max_streams": icfg["max_streams"],
            "max_height": icfg["max_height"]}
    key = request.args.get("key", "")
    if INTERP_KEY.match(key):
        body["cached"] = os.path.isfile(interp_source_path(icfg, key))
    return jsonify(body)


def _receive_upload(target, size):
    """Streams the request body to `target` through a .part file that is
    renamed into place only when it is complete. `size` is the announced
    Content-Length (0 = not announced, nothing to compare). Returns
    (bytes written, None), or (None, the error text) for a short body; a
    failed write leaves no .part behind. Shared by the interpolation and the
    3D conversion uploads."""
    part = f"{target}.{threading.get_ident()}.part"
    got = 0
    try:
        with open(part, "wb") as fh:
            while True:
                chunk = request.stream.read(1 << 20)
                if not chunk:
                    break
                fh.write(chunk)
                got += len(chunk)
    except BaseException:
        try:
            os.remove(part)
        except OSError:
            pass
        raise
    if size and got != size:
        os.remove(part)
        return None, f"short upload ({got} of {size} bytes)"
    os.replace(part, target)
    return got, None


@app.route("/v1/interp/source/<key>", methods=["PUT"])
def interp_upload(key):
    """The raw video file as the request body. Stored under its key; a second
    upload of the same key just replaces it."""
    if not authorised(request):
        return jsonify({"error": "unauthorised"}), 401
    if not INTERP_KEY.match(key):
        return jsonify({"error": "bad key"}), 400
    icfg = interp_config()
    os.makedirs(icfg["cache_dir"], exist_ok=True)
    size = int(request.content_length or 0)
    with _interp_upload_lock:
        interp_evict(icfg, size)
    got, error = _receive_upload(interp_source_path(icfg, key), size)
    if error:
        return jsonify({"error": error}), 400
    return jsonify({"key": key, "bytes": got})


@app.route("/v1/interp/stream/<key>")
def interp_stream(key):
    """Fragmented MP4 of the cached source from ?start= seconds on, at ?fps=
    frames a second (or ?mult= times its frame rate), capped at ?mbps= Mbit/s
    (0 = no cap). Runs as long as the client reads; closing the connection
    stops the encoder."""
    if not authorised(request):
        return jsonify({"error": "unauthorised"}), 401
    if not INTERP_KEY.match(key):
        return jsonify({"error": "bad key"}), 400
    icfg = interp_config()
    ok, missing = interp_available(icfg)
    if not ok:
        return jsonify({"error": "interpolation is not installed here: missing " + ", ".join(missing)}), 503
    src = interp_source_path(icfg, key)
    if not os.path.isfile(src):
        return jsonify({"error": "source not uploaded", "upload": True}), 404
    try:
        start = max(0.0, float(request.args.get("start", 0)))
        mult = int(request.args.get("mult", 2))
        out_fps = float(request.args.get("fps") or 0)
        mbps = max(0.0, float(request.args.get("mbps", 0)))
        max_h = int(request.args.get("max_height") or 0)
        loop = request.args.get("loop") == "1"
    except ValueError:
        return jsonify({"error": "bad parameters"}), 400
    if out_fps:
        if not INTERP_OUT_FPS[0] <= out_fps <= INTERP_OUT_FPS[1]:
            return jsonify({"error": "fps must be %g-%g" % INTERP_OUT_FPS}), 400
        rate = ["--fps", f"{out_fps:.3f}".rstrip("0").rstrip(".")]
    elif 2 <= mult <= 8:
        rate = ["-m", str(mult)]
    else:
        return jsonify({"error": "mult must be 2-8"}), 400
    max_h = min(max_h, icfg["max_height"]) if max_h > 0 else icfg["max_height"]

    streams = interp_streams()
    # Smooth motion is someone watching right now: a running 3D conversion
    # steps aside (it resumes once nobody has watched for a while).
    stereo_note_interp("smooth motion stream")
    # A seek closes the old stream and opens the next at once: give the old one
    # a moment to shut down before calling the GPU busy.
    if not streams.acquire(timeout=8):
        return jsonify({"error": "the GPU is busy with another interpolated stream", "busy": True}), 503
    STATE["interp_streaming"] += 1
    os.utime(src)                                   # recently used, for eviction
    cmd = [icfg["bin"], "-i", src, "-o", "-", *rate, "-s", f"{start:.3f}",
           "--max-height", str(max_h), "--maxrate", f"{mbps:g}", "-d", icfg["dll"]]
    if loop:
        # Endless: the player loops videos, and a new stream at the end cost a
        # second of black while the encoder started again.
        cmd.append("--loop")
    log_path = os.path.join(icfg["cache_dir"], f"stream-{key[:12]}.log")
    log = open(log_path, "w")
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=log, bufsize=0)
    except OSError as exc:
        log.close()
        STATE["interp_streaming"] -= 1
        streams.release()
        return jsonify({"error": f"could not start vidinterp-gpu: {exc}"}), 500
    STATE["last_request"] = time.time()

    def body():
        try:
            while True:
                chunk = proc.stdout.read(1 << 16)
                if not chunk:
                    break
                yield chunk
        finally:
            # Also reached when the client goes away (waitress closes the iterator).
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
            log.close()
            if proc.returncode not in (0, -15, -9):
                try:
                    with open(log_path, encoding="utf-8", errors="replace") as fh:
                        STATE["last_error"] = "interpolation: " + fh.read()[-300:]
                except OSError:
                    pass
            STATE["last_interp"] = time.time()
            STATE["interp_streaming"] -= 1
            streams.release()

    from flask import Response
    return Response(body(), mimetype="video/mp4",
                    headers={"Cache-Control": "no-store", "X-Interp-Rate": " ".join(rate)})


# ------------------------------------------------ interpolated segment cache --
# The player's Smooth motion plays segments instead of one live stream: 2-second
# CMAF segments of the interpolated video (vidinterp-gpu --segment-dir), kept on
# disk under <cache>/<key>/<variant>/. Anything made once is served from disk -
# looping, seeking back, and the next visit start at once. One job at a time
# makes segments for the most recent viewer: from where they are watching, and
# while they keep watching, on through the whole file. Every segment starts with
# a keyframe at its own place on the timeline, so segments from different runs
# (a seek, another day) play as one video.

SEG_NAME = re.compile(r"^(init\.mp4|meta\.json|end\.json|seg_(\d{5})\.m4s)$")
# m<k>: k times the video's rate; f<rate>: that output rate exactly (the screen's,
# e.g. f120 for a 25 fps video = x4.8, so every frame is shown equally long).
SEG_VARIANT = re.compile(r"^(?:m(\d)|f(\d{2,3}(?:\.\d{1,3})?))_h(\d{3,4})_r(\d{1,4}(?:\.\d+)?)$")
SEG_ACTIVE_S = 30          # a viewer counts as present this long after their last request
SEG_WAIT_S = 25            # how long a request for a segment in the making waits
_seg = {"lock": threading.Lock(), "want": {}, "job": None, "wake": threading.Event()}


def seg_variant_ok(variant):
    m = SEG_VARIANT.match(variant)
    return bool(m) and (not m.group(2) or INTERP_OUT_FPS[0] <= float(m.group(2)) <= INTERP_OUT_FPS[1])


def seg_dir(icfg, key, variant):
    return os.path.join(icfg["cache_dir"], key, variant)


def seg_have(d):
    """Indices of the finished segments on disk (temp files do not count)."""
    try:
        return {int(m.group(2)) for n in os.listdir(d) if (m := SEG_NAME.match(n)) and m.group(2)}
    except OSError:
        return set()


def seg_total(d):
    """How many segments the video has: exact once a run reached the end, else
    the estimate the first run wrote; None before any run."""
    for name, field, add in (("end.json", "last_segment", 1), ("meta.json", "segments", 0)):
        try:
            with open(os.path.join(d, name), encoding="utf-8") as fh:
                return int(json.load(fh)[field]) + add
        except (OSError, ValueError, KeyError, TypeError):
            pass
    return None


def seg_plan(d, cursor):
    """(start, end) of the next run - the first missing segment from the cursor
    on, else from the beginning, up to the next finished one (end -1 = to the
    end of the file) - or None when every segment is there."""
    have = seg_have(d)
    total = seg_total(d)
    if total is None:
        todo = [cursor] if cursor not in have else [cursor + 1]
    else:
        todo = ([s for s in range(cursor, total) if s not in have]
                or [s for s in range(0, min(cursor, total)) if s not in have])
    if not todo:
        return None
    start = todo[0]
    later = [s for s in have if s > start]
    end = min(later) if later else -1
    return start, (-1 if total is not None and end >= total else end)


def seg_stop(job):
    proc = job["proc"]
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
    job["log"].close()
    # A killed run leaves the segment it was writing as a temp file.
    for n in os.listdir(job["dir"]):
        if ".tmp" in n:
            try:
                os.remove(os.path.join(job["dir"], n))
            except OSError:
                pass


def seg_manage_once():
    icfg = interp_config()
    now = time.time()
    with _seg["lock"]:
        _seg["want"] = {k: w for k, w in _seg["want"].items() if now - w["touched"] < SEG_ACTIVE_S}
        target = max(_seg["want"].items(), key=lambda kv: kv[1]["touched"], default=None)
        job = _seg["job"]
    if job and job["proc"].poll() is not None:            # the run has finished
        rc = job["proc"].returncode
        seg_stop(job)
        if rc not in (0, -15, -9):
            try:
                with open(os.path.join(job["dir"], "job.log"), encoding="utf-8", errors="replace") as fh:
                    STATE["last_error"] = "interpolation: " + fh.read()[-300:]
            except OSError:
                pass
        job = _seg["job"] = None
    if target is None:                                     # nobody watching any more
        if job:
            seg_stop(job)
            _seg["job"] = None
        return
    (key, variant), want = target
    d = seg_dir(icfg, key, variant)
    plan = seg_plan(d, want["cursor"])
    if job:
        if job["key"] == key and job["variant"] == variant and plan is not None:
            # Keep a run that is about to make what is needed next.
            s = plan[0]
            done = [x for x in seg_have(d) if x >= job["start"]]
            progress = max(done) if done else job["start"] - 1
            if job["start"] <= s and (job["end"] < 0 or s < job["end"]) and s - progress <= 3:
                return
        seg_stop(job)
        job = _seg["job"] = None
    if plan is None:
        return
    src = interp_source_path(icfg, key)
    m = SEG_VARIANT.match(variant)
    if not os.path.isfile(src) or not m:
        return
    rate = ["-m", m.group(1)] if m.group(1) else ["--fps", m.group(2)]
    max_h, mbps = int(m.group(3)), float(m.group(4))
    interp_evict(icfg, 0, keep_key=key)
    os.makedirs(d, exist_ok=True)
    start, end = plan
    cmd = [icfg["bin"], "-i", src, "--segment-dir", d, *rate, "--max-height", str(max_h),
           "--maxrate", f"{mbps:g}", "-d", icfg["dll"], "--start-segment", str(start)]
    if end >= 0:
        cmd += ["--end-segment", str(end)]
    log = open(os.path.join(d, "job.log"), "a", encoding="utf-8")
    log.write(f"--- {time.strftime('%Y-%m-%d %H:%M:%S')} segments {start}..{end if end >= 0 else 'end'}\n")
    log.flush()
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=log)
    for path in (src, os.path.join(icfg["cache_dir"], key)):
        try:
            os.utime(path)                                 # recently used, for eviction
        except OSError:
            pass
    STATE["last_request"] = time.time()
    _seg["job"] = {"key": key, "variant": variant, "dir": d, "start": start, "end": end,
                   "proc": proc, "log": log}


def seg_manager():
    while True:
        _seg["wake"].wait(1.0)
        _seg["wake"].clear()
        try:
            seg_manage_once()
        except Exception as exc:                           # the manager must not die
            STATE["last_error"] = f"interpolation segments: {exc}"[:300]


@app.route("/v1/interp/cached/<key>/<variant>/<name>")
def interp_cached_segment(key, variant, name):
    """Read existing segments without uploading, scheduling, or waking GPU work."""
    if not authorised(request):
        return jsonify({"error": "unauthorised"}), 401
    if not INTERP_KEY.fullmatch(key) or not seg_variant_ok(variant) or not SEG_NAME.fullmatch(name):
        return jsonify({"error": "bad request"}), 400
    folder = seg_dir(interp_config(), key, variant)
    path = os.path.join(folder, name)
    if not os.path.isfile(path):
        return jsonify({"error": "not cached"}), 404
    from flask import send_file
    response = send_file(path, mimetype="application/json" if name.endswith(".json") else "video/mp4", conditional=True)
    response.headers["Cache-Control"] = "private, no-store"
    return response


def seg_variants(icfg, key):
    """The renditions the segment cache holds for one source key:
    [{variant, segments, complete}], sorted by name. `segments` = finished
    segment files, `complete` = end.json exists (a run reached the end of the
    file). Read only."""
    base = os.path.join(icfg["cache_dir"], key)
    try:
        names = sorted(os.listdir(base))
    except OSError:
        return []
    out = []
    for name in names:
        d = os.path.join(base, name)
        if not seg_variant_ok(name) or not os.path.isdir(d):
            continue
        out.append({"variant": name, "segments": len(seg_have(d)),
                    "complete": os.path.isfile(os.path.join(d, "end.json"))})
    return out


@app.route("/v1/interp/variants/<key>")
def interp_variants(key):
    """What the segment cache holds for `key`, so media-dl can redo it after a
    video was cut (user, 2026-10-18: "re-do the interpolation and 3D work").
    Read only: schedules nothing, wakes nothing."""
    if not authorised(request):
        return jsonify({"error": "unauthorised"}), 401
    if not INTERP_KEY.fullmatch(key):
        return jsonify({"error": "bad request"}), 400
    return jsonify(seg_variants(interp_config(), key))


@app.route("/v1/interp/seg/<key>/<variant>/<name>")
def interp_segment(key, variant, name):
    """One file of the segment cache: init.mp4, seg_NNNNN.m4s, meta.json or
    end.json. What is on disk comes at once; what is being made is waited for
    (up to SEG_WAIT_S, then 503 + Retry-After). ?from= says where the viewer is
    when asking for init/meta."""
    if not authorised(request):
        return jsonify({"error": "unauthorised"}), 401
    m = SEG_NAME.match(name)
    if not INTERP_KEY.match(key) or not seg_variant_ok(variant) or not m:
        return jsonify({"error": "bad request"}), 400
    icfg = interp_config()
    ok, missing = interp_available(icfg)
    if not ok:
        return jsonify({"error": "interpolation is not installed here: missing " + ", ".join(missing)}), 503
    if not os.path.isfile(interp_source_path(icfg, key)):
        return jsonify({"error": "source not uploaded", "upload": True}), 404
    try:
        index = int(m.group(2)) if m.group(2) else max(0, int(request.args.get("from") or 0))
    except ValueError:
        return jsonify({"error": "bad from"}), 400
    d = seg_dir(icfg, key, variant)
    path = os.path.join(d, name)
    # Where the viewer is, set ONCE per request. A request still waiting after
    # the player moved on (a seek aborts the fetch, but the proxied request goes
    # on) must not pull the job back: re-setting the cursor while waiting made
    # two requests restart the job against each other five times a second.
    # A file already on disk moves nothing - only a request that has to wait says
    # where the job should be: the player's prefetch of the loop's start near the
    # end must not drag the job away from the segments being watched.
    with _seg["lock"]:
        w = _seg["want"].get((key, variant))
        if os.path.isfile(path) and w:
            w["touched"] = time.time()
            seq = None
        else:
            _seg["seq"] = seq = _seg.get("seq", 0) + 1
            _seg["want"][(key, variant)] = {"cursor": index, "touched": time.time(), "seq": seq}
    _seg["wake"].set()
    if seq is not None:
        # A viewer who needs segments made: a running 3D conversion steps aside.
        stereo_note_interp("smooth motion segment")
    deadline = time.time() + SEG_WAIT_S
    while not os.path.isfile(path):
        total = seg_total(d)
        if name == "end.json" or (m.group(2) and total is not None and index >= total):
            return jsonify({"error": "no such segment"}), 404
        if time.time() > deadline:
            return jsonify({"error": "still being made", "pending": True}), 503, {"Retry-After": "1"}
        with _seg["lock"]:                      # still watching: keep the viewer present
            w = _seg["want"].get((key, variant))
            if w and w.get("seq") == seq:
                w["touched"] = time.time()
        time.sleep(0.1)
    mime = "application/json" if name.endswith(".json") else "video/mp4"
    from flask import send_file
    resp = send_file(path, mimetype=mime, conditional=True)
    resp.headers["Cache-Control"] = "no-store" if name.endswith(".json") else "private, max-age=86400"
    return resp


# --------------------------------------------------- 2D -> 3D conversion --
# media-dl asks for a video or picture to be made stereoscopic (side-by-side
# L|R plus a depth map). It is offline work - about 4.5 GPU-hours per hour of
# 1080p video - so it runs in the gaps: stereo/ksf/stereo_job.py in its own venv
# (nunif/iw3, a different torch from this one), started and stopped by
# stereo_manager below. This host cannot reach media-dl, so media-dl pushes the
# source (PUT), says what to do (POST), polls (GET) and pulls the results.
#
# GPU priority (owner, 2026-10-05):  upscale > interpolation (Smooth motion) > 3D > indexing
#   upscale (a picture someone looks at)       -> runs alongside, no preemption; a
#                                  job does not START while one is waiting
#   Smooth motion viewer                       -> the job is stopped at once
#                                  (SIGTERM; the driver keeps every finished 20 s
#                                  chunk, so at most one chunk is lost); no start
#                                  while a stream/viewer is active or just was
#   urgent analyze/audio (a person clicking)   -> same: stops a running job; the
#                                  start gate waits stereo_idle_s after the last one
#   3D job                                     -> see the rules below
#   indexer batches (non-urgent analyze/audio) -> lowest: they never delay a job
# A job only STARTS when none of the above has been active for a while. Indexer
# batches do NOT count as activity (they arrive continuously and would starve a job
# forever - observed live); instead stereo_yield_reason() answers them 503
# {"error", "retry_after_s": 600} iff
#   (a) a job is RUNNING, or
#   (b) an eligible job is WAITING (waiting_gpu, not_before passed; held/failed/
#       cancelled/done never count) AND the start gate, ignoring VRAM, is open -
#       i.e. nothing but this worker's own batches stands between the job and the
#       card - AND the manager has not found VRAM short even after unloading the
#       worker's models (then yielding would stop indexing for nothing; it retries
#       every STEREO_VRAM_RETRY_S).
# While the job is blocked by something the indexer cannot affect (paused worker,
# outside the 3D window, disk low, upscale/Smooth motion busy) batches are served
# normally. A batch already in flight finishes first: the manager only starts a job
# with a NON-blocking take of _lock, and a batch that queued on _lock re-checks
# stereo_running() once it has it.
# Holding _lock for the whole job was rejected: every interactive upscale would
# wait hours. The manager takes it once, to unload the worker's own models.
#
# Files: <stereo_work_dir>/<key>/ holds the driver's own files (job.json,
# progress.json, chunks/...) plus mgr.json, which is this manager's state.
# Directories without a mgr.json (the hand-run P1 jobs) are never touched.

import signal

STEREO_KINDS = ("video", "picture")
# Keys of stereo_job.py's DEFAULT_PROFILE: media-dl may set these and nothing else.
STEREO_PROFILE_KEYS = {"depth_model", "picture_model", "method", "divergence", "convergence", "fps",
                       "max_eye_long_side", "chunk_s", "cq", "nvenc_preset", "bitrate_anchors",
                       "interp_quality", "depth_cq", "depth_maxrate_mbps", "depth_scale",
                       "picture_jpeg_quality", "turn_cw"}
STEREO_WORD = re.compile(r"^[A-Za-z0-9_.-]{1,40}$")
STEREO_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,120}$")
STEREO_EXT = re.compile(r"^[A-Za-z0-9]{1,5}$")
STEREO_PICTURE_EXT = (".jpg", ".jpeg", ".png", ".webp", ".bmp")     # what the driver's batch mode reads
STEREO_VRAM_RETRY_S = 60           # a start attempt found VRAM short after unloading: no new attempt, indexer served, this long
STEREO_MAX_SAME_FAILS = 3          # the same phase+chunk failing this often fails the job
# The driver's progress.json phase -> the state a client sees while it runs.
STEREO_PHASE_STATE = {"starting": "probing", "probing": "probing", "interpolating": "interpolating",
                      "splitting": "converting", "converting": "converting", "joining": "joining",
                      "done": "joining"}
# The driver runs in ANOTHER venv: nothing of this service's environment may leak
# into it (the unit sets LD_LIBRARY_PATH to this venv's CUDA libs and HF_HOME to
# this service's model cache, which would override the driver's own defaults).
STEREO_SCRUB_ENV = ("LD_LIBRARY_PATH", "HF_HOME", "INSIGHTFACE_HOME", "KSF_VISION_PORT", "KSF_VISION_CONFIG",
                    "VIRTUAL_ENV", "PYTHONPATH", "PYTHONHOME")
_stereo = {"lock": threading.RLock(), "jobs": {}, "dir": None, "active": None, "wake": threading.Event(),
           "housekept": 0.0, "gate": "starting", "work_gb": (0.0, 0.0), "gate_cache": (0.0, None)}


def _num(cfg, key, default):
    v = cfg.get(key)
    return float(default if v is None else v)


def stereo_config() -> dict:
    cfg = config()
    home = cfg.get("stereo_home") or os.path.join(BASE_DIR, "stereo")
    windows = cfg.get("stereo_windows")
    return {
        "home": home,
        "python": cfg.get("stereo_python") or os.path.join(home, ".venv", "bin", "python"),
        "driver": cfg.get("stereo_driver") or os.path.join(home, "ksf", "stereo_job.py"),
        "work_dir": cfg.get("stereo_work_dir") or os.path.join(BASE_DIR, "stereo-work"),
        # None = the same windows as analyze; [] = any time.
        "windows": (cfg.get("windows") or []) if windows is None else windows,
        # /data also holds the interp cache and the models: a job never starts, and an
        # upload is refused, below this much free space. A running job is stopped and
        # failed below abort_free_gb (a 4K 120 fps intermediate can be ~90 GB).
        "min_free_gb": _num(cfg, "stereo_min_free_gb", 60),
        "abort_free_gb": _num(cfg, "stereo_abort_free_gb", 10),
        "idle_s": _num(cfg, "stereo_idle_s", 120),           # since the last analyze/audio request
        "interp_idle_s": _num(cfg, "stereo_interp_idle_s", 60),   # since the last Smooth motion request
        "chunk_s": int(_num(cfg, "stereo_chunk_s", 20)),
        "result_ttl_h": _num(cfg, "stereo_result_ttl_h", 72),
        "failed_ttl_h": _num(cfg, "stereo_failed_ttl_h", 24),
        "held_ttl_h": _num(cfg, "stereo_held_ttl_h", 168),
        "kill_s": _num(cfg, "stereo_kill_s", 30),             # SIGTERM -> SIGKILL
        "retry_s": _num(cfg, "stereo_retry_s", 15),           # driver restart delay after a failure, times the failure count
        # VRAM the driver may need on top of what is free: measured in P1 (iw3 +1.2 GB at 1080p,
        # ~3.1 GB at 4K, Distill_Any_L 2.7 GB) plus margin. Free VRAM must also leave reserve_mb.
        "vram_mb_hd": int(_num(cfg, "stereo_vram_mb_hd", 1800)),
        "vram_mb_uhd": int(_num(cfg, "stereo_vram_mb_uhd", 3800)),
        "vram_mb_picture": int(_num(cfg, "stereo_vram_mb_picture", 3500)),
        "reserve_mb": int(_num(cfg, "reserve_mb", 2048)),
    }


def _read_json(path, default=None):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def _stereo_dir(key):
    return os.path.join(stereo_config()["work_dir"], key)


def _stereo_save(job):
    """mgr.json, atomically. The caller holds _stereo["lock"]."""
    job["updated"] = time.time()
    path = os.path.join(_stereo_dir(job["key"]), "mgr.json")
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(job, fh, indent=1)
    os.replace(tmp, path)


def _free_gb(path):
    try:
        return shutil.disk_usage(path).free / 1024 ** 3
    except OSError:
        return 0.0


def _dir_bytes(path):
    total = 0
    for root, _, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total


def stereo_recover(work):
    """Reads every managed job from disk. A job that was running when the worker
    stopped goes back to waiting_gpu (systemd's cgroup kill took its driver down
    with the service; the driver resumes from its finished chunks), and the
    half-written uploads are dropped."""
    os.makedirs(work, exist_ok=True)
    jobs = {}
    for name in os.listdir(work):
        d = os.path.join(work, name)
        job = _read_json(os.path.join(d, "mgr.json"))
        if not (INTERP_KEY.match(name) and os.path.isdir(d) and isinstance(job, dict)) or job.get("key") != name:
            continue
        for root, _, files in os.walk(d):
            for f in files:
                if f.endswith(".part"):
                    try:
                        os.remove(os.path.join(root, f))
                    except OSError:
                        pass
        if job.get("state") == "running":
            job["state"] = "waiting_gpu"
            job["restarts"] = job.get("restarts", 0) + 1
        jobs[name] = job
    return jobs


def stereo_jobs():
    """key -> job. Loaded from the work dir on first use (and again if the
    configured dir changed)."""
    work = stereo_config()["work_dir"]
    with _stereo["lock"]:
        if _stereo["dir"] != work:
            _stereo["jobs"] = stereo_recover(work)
            _stereo["dir"] = work
        return _stereo["jobs"]


def stereo_running() -> bool:
    act = _stereo["active"]
    return bool(act and act["proc"].poll() is None)


def stereo_yield_reason():
    """Why a non-urgent (indexer) analyze/audio request must be refused now, else None. Rule: see the
    priority block above the 3D section."""
    if stereo_running():
        return "3D conversion running"
    now = time.time()
    if now < _stereo.get("vram_stuck_until", 0.0):
        return None
    jobs = stereo_jobs()
    with _stereo["lock"]:
        waiting = sorted((j for j in jobs.values()
                          if j.get("state") == "waiting_gpu" and j.get("not_before", 0) <= now),
                         key=lambda j: j.get("queued_at", 0))
    if not waiting:
        return None
    ok, _why = stereo_gate(waiting[0], vram=False)
    return "3D conversion waiting to start" if ok else None


def stereo_vram_need_mb(job, cfg):
    if job is not None and job.get("kind") == "picture":
        return cfg["vram_mb_picture"]
    side = int(((job or {}).get("profile") or {}).get("max_eye_long_side") or 2160)
    return cfg["vram_mb_hd"] if side <= 1920 else cfg["vram_mb_uhd"]


def gpu_free_mb():
    """Free VRAM of the card in MiB (nvidia-smi: other processes' use counts), None if it cannot be read."""
    try:
        r = subprocess.run(["nvidia-smi", "--query-gpu=memory.free", "--format=csv,noheader,nounits"],
                           capture_output=True, text=True, timeout=5)
        return int(r.stdout.strip().split("\n")[0])
    except (OSError, ValueError, IndexError, subprocess.SubprocessError):
        return None


def stereo_smooth_busy(cfg, now):
    """Why Smooth motion has the card (a viewer, an open stream, or one a moment ago), else None."""
    with _seg["lock"]:
        viewer = any(now - w["touched"] < SEG_ACTIVE_S for w in _seg["want"].values())
        running = _seg["job"] is not None
    if viewer or running:
        return "smooth motion viewer"
    if STATE["interp_streaming"] > 0:
        return "smooth motion stream open"
    if now - STATE["last_interp"] < cfg["interp_idle_s"]:
        return "smooth motion used %ds ago" % (now - STATE["last_interp"])
    return None


def stereo_gate(job=None, vram=True):
    """(open, reason): may a waiting job start now?"""
    cfg = stereo_config()
    now = time.time()
    if STATE["paused"]:
        return False, "worker paused"
    if not _in_windows(cfg["windows"]):
        return False, "outside the 3D window"
    idle = now - STATE["last_urgent"]       # indexer batches do not count: they are refused while a job waits
    if idle < cfg["idle_s"]:
        return False, "urgent analyze/audio %ds ago (needs %ds idle)" % (idle, cfg["idle_s"])
    if STATE["upscale_waiting"]:
        return False, "upscale waiting"
    busy = stereo_smooth_busy(cfg, now)
    if busy:
        return False, busy
    free = _free_gb(cfg["work_dir"])
    if free < cfg["min_free_gb"]:
        return False, "disk: %.0f GB free, needs %.0f" % (free, cfg["min_free_gb"])
    if vram:
        need = stereo_vram_need_mb(job, cfg) + cfg["reserve_mb"]
        got = gpu_free_mb()
        if got is None:
            return False, "nvidia-smi failed"
        if got < need:
            return False, "vram: %d MiB free, needs %d (incl. %d reserve)" % (got, need, cfg["reserve_mb"])
    return True, "open"


# --- running a job --------------------------------------------------------

def _stereo_term(act):
    if not act.get("term_at"):
        act["term_at"] = time.time()
        try:
            act["proc"].send_signal(signal.SIGTERM)
        except OSError:
            pass


def _stereo_kill(act):
    try:
        os.killpg(act["proc"].pid, signal.SIGKILL)      # the driver's own session: vidinterp-gpu goes too
    except OSError:
        try:
            act["proc"].kill()
        except OSError:
            pass


def stereo_start(job, cfg):
    """Popen the driver for `job` and make it the active one. The caller holds
    _stereo["lock"] and has checked the gate."""
    wd = _stereo_dir(job["key"])
    try:
        os.remove(os.path.join(wd, "progress.json"))    # a stale one would mislead the failure counter
    except OSError:
        pass
    cmd = [cfg["python"], cfg["driver"], "run", "--work", wd, "--src", os.path.join(wd, job["source"]),
           "--kind", job["kind"], "--profile", json.dumps(job["profile"])]
    out = open(os.path.join(wd, "driver.out"), "ab")
    out.write(("--- %s attempt %d\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), job.get("attempts", 0) + 1)).encode())
    out.flush()
    env = {k: v for k, v in os.environ.items() if k not in STEREO_SCRUB_ENV}
    try:
        proc = subprocess.Popen(cmd, stdout=out, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, cwd=wd,
                                env=env, start_new_session=True)
    except OSError as exc:
        out.close()
        job["state"], job["error"] = "failed", "could not start the driver: %s" % exc
        job["failed_at"] = time.time()
        _stereo_save(job)
        return
    job["state"] = "running"
    job["attempts"] = job.get("attempts", 0) + 1
    job["started_at"] = time.time()
    _stereo_save(job)
    _stereo["active"] = {"key": job["key"], "proc": proc, "out": out, "started": time.time()}
    print("[vision] 3D job %s started (pid %d, attempt %d)" % (job["key"][:12], proc.pid, job["attempts"]), flush=True)


def stereo_collect(job):
    """(results, None) when the driver's outputs are all there, else (None, what is missing)."""
    wd = _stereo_dir(job["key"])
    if job["kind"] == "video":
        rel = ["sbs.mp4", "depth.mp4", "meta.json"]
        names = [(r, r) for r in rel]
    elif job.get("files"):
        names = []
        for f in job["files"]:
            stem = os.path.splitext(f)[0]
            names += [(stem + "/" + n, "out/" + stem + "/" + n) for n in ("sbs.jpg", "depth.png", "meta.json")]
    else:
        names = [(n, n) for n in ("sbs.jpg", "depth.png", "meta.json")]
    results = []
    for name, rel in names:
        try:
            size = os.path.getsize(os.path.join(wd, rel))
        except OSError:
            size = 0
        if size <= 0:
            return None, "output %s is missing" % name
        results.append({"name": name, "bytes": size})
    return results, None


def stereo_drop_inputs(job):
    """What a finished job no longer needs: the source and the working files (the
    chunks are normally gone already)."""
    wd = _stereo_dir(job["key"])
    for n in os.listdir(wd):
        p = os.path.join(wd, n)
        if n == "src" or n == "chunks" or n.startswith("src.") or n in ("audio.mka",) or n.startswith("interp"):
            try:
                shutil.rmtree(p) if os.path.isdir(p) else os.remove(p)
            except OSError:
                pass


def _stereo_failure_text(job, wd, rc):
    err = (_read_json(os.path.join(wd, "job.json"), {}) or {}).get("error")
    if err:
        return str(err)[-400:]
    try:
        with open(os.path.join(wd, "driver.out"), encoding="utf-8", errors="replace") as fh:
            tail = fh.read()[-300:].strip()
    except OSError:
        tail = ""
    return ("driver exited with code %s: %s" % (rc, tail))[-400:]


def stereo_settle(job, act, rc):
    """What an exited driver means for its job. Called with _stereo["lock"] held."""
    wd = _stereo_dir(job["key"])
    now = time.time()
    if act.get("delete"):
        return
    if rc == 0:
        results, why = stereo_collect(job)
        if results is not None:
            stereo_drop_inputs(job)
            job.update(state="done", done_at=now, results=results, error=None)
            _stereo_save(job)
            print("[vision] 3D job %s done" % job["key"][:12], flush=True)
            return
        rc = "0 (%s)" % why             # exited cleanly without its outputs: a failure like any other
    if act.get("abort"):
        job.update(state="failed", failed_at=now, error=act["abort"])
    elif act.get("hold"):
        job.update(state="held", held_at=now)
    elif act.get("preempt"):
        job.update(state="waiting_gpu", preempts=job.get("preempts", 0) + 1,
                   last_preempt={"reason": act["preempt"], "at": now})
    else:
        # Not stopped by us: the driver (or the card) failed. The same phase+chunk failing
        # three times is a bad input or a real bug, not bad luck - any other failure restarts it.
        prog = _read_json(os.path.join(wd, "progress.json"), {}) or {}
        sig = "%s:%s" % (prog.get("phase") or "starting", prog.get("chunk") or 0)
        counts = job.setdefault("fail_counts", {})
        counts[sig] = counts.get(sig, 0) + 1
        job["error"] = _stereo_failure_text(job, wd, rc)
        STATE["last_error"] = ("3D conversion: %s" % job["error"])[:300]
        if counts[sig] >= STEREO_MAX_SAME_FAILS:
            job.update(state="failed", failed_at=now)
            print("[vision] 3D job %s failed at %s: %s" % (job["key"][:12], sig, job["error"][:120]), flush=True)
        else:
            job.update(state="waiting_gpu", not_before=now + stereo_config()["retry_s"] * counts[sig])
    _stereo_save(job)


def stereo_reap():
    """Settles the active job if its driver has exited. Idempotent; the manager
    calls it every tick and so does whoever waited for the exit."""
    with _stereo["lock"]:
        act = _stereo["active"]
        if not act or act["proc"].poll() is None:
            return False
        _stereo["active"] = None
        try:
            act["out"].close()
        except OSError:
            pass
        job = stereo_jobs().get(act["key"])
        if job is not None:
            stereo_settle(job, act, act["proc"].returncode)
        return True


def _stereo_wait(act, seconds):
    end = time.time() + seconds
    while act["proc"].poll() is None and time.time() < end:
        time.sleep(0.05)
    return act["proc"].poll() is not None


def stereo_preempt(reason, wait=5.0):
    """Stops the running job for someone who needs the card now: SIGTERM, then up
    to `wait` seconds for the driver to leave (it drops the chunk in flight and
    exits 75). The job goes back to waiting_gpu; the gate decides when it
    resumes. Does nothing (False) when no job runs. A driver that ignores the
    SIGTERM is killed by the manager after stereo_kill_s."""
    with _stereo["lock"]:
        act = _stereo["active"]
        if not act or act["proc"].poll() is not None:
            stereo_reap()
            return False
        act.setdefault("preempt", reason)
        _stereo_term(act)
    _stereo_wait(act, wait)
    stereo_reap()
    return True


def stereo_note_interp(reason):
    """A Smooth motion request: remember it (the gate waits a while after it) and stop the job."""
    STATE["last_interp"] = time.time()
    stereo_preempt(reason)


# --- housekeeping -----------------------------------------------------------

def stereo_housekeeping(cfg, force=False):
    now = time.time()
    if not force and now - _stereo["housekept"] < 600:
        return
    _stereo["housekept"] = now
    jobs = stereo_jobs()
    work = cfg["work_dir"]
    h = 3600.0
    with _stereo["lock"]:
        for key, job in list(jobs.items()):
            st = job.get("state")
            age = None
            if st == "done":
                age = now - max(job.get("done_at") or 0, job.get("fetched_at") or 0)
                limit = cfg["result_ttl_h"]
            elif st == "failed":
                age, limit = now - (job.get("failed_at") or job.get("updated") or now), cfg["failed_ttl_h"]
            elif st == "uploaded":
                age, limit = now - (job.get("updated") or now), cfg["failed_ttl_h"]
            elif st == "held":
                age, limit = now - (job.get("held_at") or now), cfg["held_ttl_h"]
            if age is not None and age > limit * h:
                shutil.rmtree(os.path.join(work, key), ignore_errors=True)
                jobs.pop(key, None)
                print("[vision] 3D job %s removed (%s for %.0f h)" % (key[:12], st, age / h), flush=True)
    # A key directory without mgr.json is a crashed first upload: gone after a day.
    try:
        names = os.listdir(work)
    except OSError:
        names = []
    for name in names:
        d = os.path.join(work, name)
        if INTERP_KEY.match(name) and name not in jobs and os.path.isdir(d) \
                and not os.path.exists(os.path.join(d, "mgr.json")):
            try:
                if now - os.stat(d).st_mtime > 24 * h:
                    shutil.rmtree(d, ignore_errors=True)
            except OSError:
                pass


def stereo_manage_once():
    cfg = stereo_config()
    stereo_jobs()
    stereo_reap()
    now = time.time()
    with _stereo["lock"]:
        act = _stereo["active"]
    if act:
        if act["proc"].poll() is None:
            if STATE["paused"] and not act.get("preempt"):
                stereo_preempt("paused")                # /v1/pause means "give the card back"
            elif _free_gb(cfg["work_dir"]) < cfg["abort_free_gb"] and not act.get("abort"):
                act["abort"] = "disk almost full (under %.0f GB free): job stopped" % cfg["abort_free_gb"]
                _stereo_term(act)
            if act.get("term_at") and now - act["term_at"] > cfg["kill_s"]:
                _stereo_kill(act)
        return
    stereo_housekeeping(cfg)
    jobs = stereo_jobs()
    waiting = sorted((j for j in jobs.values() if j.get("state") == "waiting_gpu" and j.get("not_before", 0) <= now),
                     key=lambda j: j.get("queued_at", 0))
    if not waiting:
        _stereo["gate"] = "nothing waiting"
        return
    job = waiting[0]
    ok, why = stereo_gate(job, vram=False)
    _stereo["gate"] = why
    if not ok:
        return
    if now < _stereo.get("vram_stuck_until", 0.0):
        _stereo["gate"] = "vram short after unloading, retrying in %ds" % (_stereo["vram_stuck_until"] - now)
        return
    if not _lock.acquire(blocking=False):                # a batch or an upscale is on the card
        _stereo["gate"] = "gpu busy"
        return
    try:
        # The worker's own models go first: they are the VRAM the driver would otherwise be refused.
        unload_models("3D conversion")
        ok, why = stereo_gate(job)
        _stereo["gate"] = why
        if not ok:
            if why.startswith("vram"):
                # Not the worker's own models (they are gone): something else holds the card. Serve the
                # indexer meanwhile instead of refusing it for a job that cannot start.
                _stereo["vram_stuck_until"] = time.time() + STEREO_VRAM_RETRY_S
            return
        _stereo["vram_stuck_until"] = 0.0
        with _stereo["lock"]:
            if _stereo["active"] is None and job.get("state") == "waiting_gpu":
                stereo_start(job, cfg)
    finally:
        _lock.release()


def stereo_manager():
    while True:
        _stereo["wake"].wait(2.0)
        _stereo["wake"].clear()
        try:
            stereo_manage_once()
        except Exception as exc:                           # the manager must not die
            STATE["last_error"] = f"3D manager: {exc}"[:300]
            print("[vision] 3D manager: %s" % exc, flush=True)


# --- routes -------------------------------------------------------------------

def stereo_available(cfg):
    icfg = interp_config()
    missing = [what for what, path in (("stereo venv python", cfg["python"]), ("stereo_job.py", cfg["driver"]),
                                       ("vidinterp-gpu", icfg["bin"]), ("lsfg-vk.dll", icfg["dll"]))
               if not os.path.isfile(path)]
    return (not missing), missing


def stereo_view(job, detail=True):
    """A job as the API shows it. Called with _stereo["lock"] held."""
    key = job["key"]
    v = {"key": key, "kind": job.get("kind"), "label": job.get("label"), "state": job["state"],
         "created": job.get("created"), "queued_at": job.get("queued_at"), "updated": job.get("updated"),
         "attempts": job.get("attempts", 0), "preempts": job.get("preempts", 0),
         "error": job.get("error"), "source_bytes": job.get("source_bytes", 0)}
    if job.get("files"):
        v["files"] = len(job["files"])
    act = _stereo["active"]
    if act and act["key"] == key:
        prog = _read_json(os.path.join(_stereo_dir(key), "progress.json"), {}) or {}
        if act.get("hold"):
            v["state"] = "holding"
        else:
            v["state"] = STEREO_PHASE_STATE.get(prog.get("phase"), "probing") if job.get("kind") == "video" \
                else "converting"
        v["progress"] = {k: prog.get(k) for k in ("phase", "chunk", "chunks", "frames_done", "frames_total",
                                                  "recent_fps", "eta_s", "updated") if k in prog}
        v["running_s"] = round(time.time() - act["started"])
    if job["state"] == "waiting_gpu":
        order = sorted((j for j in _stereo["jobs"].values() if j.get("state") == "waiting_gpu"),
                       key=lambda j: j.get("queued_at", 0))
        v["queue_position"] = [j["key"] for j in order].index(key) + 1
        if job.get("last_preempt"):
            v["last_preempt"] = job["last_preempt"]
        if job.get("not_before", 0) > time.time():
            v["retry_in_s"] = round(job["not_before"] - time.time())
    if detail and job["state"] == "done":
        v["results"] = job.get("results", [])
        v["meta"] = _read_json(os.path.join(_stereo_dir(key), "meta.json" if job["kind"] == "video" or not job.get("files")
                                            else "out/%s/meta.json" % os.path.splitext(job["files"][0])[0]))
    return v


def _stereo_key(key):
    return bool(INTERP_KEY.match(key))


@app.route("/v1/stereo/status")
def stereo_status():
    """Whether 3D conversion can run here, what runs, what waits, and why nothing starts."""
    if not authorised(request):
        return jsonify({"error": "unauthorised"}), 401
    cfg = stereo_config()
    ok, missing = stereo_available(cfg)
    jobs = stereo_jobs()
    with _stereo["lock"]:
        views = {k: stereo_view(j, detail=False) for k, j in jobs.items()}
        act = _stereo["active"]
        running = views.get(act["key"]) if act else None
        waiting = sorted((v for v in views.values() if v["state"] == "waiting_gpu"), key=lambda v: v["queue_position"])
        held = [v for v in views.values() if v["state"] in ("held", "holding")]
        first = next((jobs[v["key"]] for v in waiting), None)
        counts = {}
        for v in views.values():
            counts[v["state"]] = counts.get(v["state"], 0) + 1
    now = time.time()
    if now - _stereo["work_gb"][0] > 30:
        _stereo["work_gb"] = (now, _dir_bytes(cfg["work_dir"]) / 1024 ** 3)
    commit = None
    try:
        with open(os.path.join(cfg["home"], "nunif", "COMMIT_PINNED"), encoding="utf-8") as fh:
            commit = fh.read().split()[0]
    except (OSError, IndexError):
        pass
    gate_ok, gate_why = stereo_gate(first, vram=True)
    return jsonify({"available": ok, "missing": missing, "running": running, "held": held, "waiting": waiting,
                    "counts": counts, "disk_free_gb": round(_free_gb(cfg["work_dir"]), 1),
                    "work_gb": round(_stereo["work_gb"][1], 2), "min_free_gb": cfg["min_free_gb"],
                    "nunif_commit": commit, "paused": STATE["paused"], "max_upload_bytes": UPLOAD_MAX_BYTES,
                    "gate": {"open": gate_ok, "reason": gate_why, "manager": _stereo["gate"]}})


@app.route("/v1/stereo/source/<key>", methods=["PUT"])
def stereo_upload(key):
    """The source as the request body (Content-Length required). One video or
    picture per key: src.<ext> (?ext=, default bin). For a batch of pictures
    (one job, one model load) PUT each file with ?name=<file name> instead; they
    are kept under src/ and every name must be a picture with its own stem. 507
    when /data would drop below stereo_min_free_gb; 409 once the job was submitted."""
    if not authorised(request):
        return jsonify({"error": "unauthorised"}), 401
    if not _stereo_key(key):
        return jsonify({"error": "bad key"}), 400
    cfg = stereo_config()
    size = int(request.content_length or 0)
    if size <= 0:
        return jsonify({"error": "Content-Length required"}), 411
    name = request.args.get("name") or ""
    ext = (request.args.get("ext") or "bin").lower().lstrip(".")
    if name:
        if not STEREO_NAME.match(name) or not name.lower().endswith(STEREO_PICTURE_EXT):
            return jsonify({"error": "name must be a picture file name (%s)" % " ".join(STEREO_PICTURE_EXT)}), 400
    elif not STEREO_EXT.match(ext):
        return jsonify({"error": "bad ext"}), 400
    work = cfg["work_dir"]
    os.makedirs(work, exist_ok=True)
    free = _free_gb(work)
    if free - size / 1024 ** 3 < cfg["min_free_gb"]:
        return jsonify({"error": "not enough free space: %.1f GB free, %.2f GB upload, %.0f GB must stay free"
                                 % (free, size / 1024 ** 3, cfg["min_free_gb"]),
                        "disk_free_gb": round(free, 1)}), 507
    jobs = stereo_jobs()
    wd = os.path.join(work, key)
    sdir = os.path.join(wd, "src")
    with _stereo["lock"]:
        job = jobs.get(key)
        if job is not None and job["state"] != "uploaded":
            return jsonify({"error": "the job was already submitted (%s)" % job["state"], "state": job["state"]}), 409
        have_dir = os.path.isdir(sdir) and bool(os.listdir(sdir))
        have_file = any(n.startswith("src.") and not n.endswith(".part") for n in os.listdir(wd)) \
            if os.path.isdir(wd) else False
        if name and have_file or (not name and have_dir):
            return jsonify({"error": "this key already holds %s" % ("one file" if have_file else "a batch")}), 409
        if name:
            stem = os.path.splitext(name)[0]
            clash = [f for f in (os.listdir(sdir) if have_dir else []) if os.path.splitext(f)[0] == stem and f != name]
            if clash:
                return jsonify({"error": "%s has the same name before the extension as %s" % (name, clash[0])}), 409
        new = job is None
        os.makedirs(sdir if name else wd, exist_ok=True)
    if name:
        target = os.path.join(sdir, name)
    else:
        target = os.path.join(wd, "src." + ext)
    try:
        got, error = _receive_upload(target, size)
    except BaseException:
        if new:
            shutil.rmtree(wd, ignore_errors=True)
        raise
    if error:
        if new:
            shutil.rmtree(wd, ignore_errors=True)
        return jsonify({"error": error}), 400
    if not name:
        for n in os.listdir(wd):
            if n.startswith("src.") and not n.endswith(".part") and n != "src." + ext:
                os.remove(os.path.join(wd, n))        # a re-upload with another extension
    with _stereo["lock"]:
        job = jobs.get(key)
        if job is None:
            job = jobs[key] = {"key": key, "state": "uploaded", "created": time.time()}
        if name:
            files = sorted(f for f in os.listdir(sdir) if not f.endswith(".part"))
            job.update(source="src", files=files,
                       source_bytes=sum(os.path.getsize(os.path.join(sdir, f)) for f in files))
        else:
            job.update(source="src." + ext, files=None, source_bytes=got)
        _stereo_save(job)
    return jsonify({"key": key, "bytes": got, "state": "uploaded", "name": name or None})


def _stereo_profile(raw, kind, cfg):
    """(profile, None) or (None, error): only the driver's own keys, plausible types."""
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        return None, "profile must be an object"
    out = {}
    for k, v in raw.items():
        if k not in STEREO_PROFILE_KEYS:
            return None, "unknown profile key %s" % k
        if isinstance(v, str):
            if not STEREO_WORD.match(v):
                return None, "bad profile value for %s" % k
        elif isinstance(v, bool) or not isinstance(v, (int, float, list)):
            return None, "bad profile value for %s" % k
        elif isinstance(v, list) and not all(isinstance(r, list) and len(r) == 2 and all(
                isinstance(x, (int, float)) and not isinstance(x, bool) for x in r) for r in v):
            return None, "bad profile value for %s" % k
        out[k] = v
    out.setdefault("chunk_s", cfg["chunk_s"])
    return out, None


@app.route("/v1/stereo/jobs", methods=["POST"])
def stereo_submit():
    """Body {key, kind: video|picture, profile?, label?}: queues the uploaded
    source. Idempotent: an existing job comes back as it is (200). Exceptions that
    do something: a held job is resumed and a failed one retried (both back to
    waiting_gpu, fail counts cleared)."""
    if not authorised(request):
        return jsonify({"error": "unauthorised"}), 401
    body = request.get_json(silent=True) or {}
    key, kind = str(body.get("key") or ""), body.get("kind")
    if not _stereo_key(key):
        return jsonify({"error": "bad key"}), 400
    if kind not in STEREO_KINDS:
        return jsonify({"error": "kind must be video or picture"}), 400
    cfg = stereo_config()
    jobs = stereo_jobs()
    with _stereo["lock"]:
        job = jobs.get(key)
        if job is None:
            return jsonify({"error": "source not uploaded", "upload": True}), 404
        if job["state"] == "uploaded":
            profile, error = _stereo_profile(body.get("profile"), kind, cfg)
            if error:
                return jsonify({"error": error}), 400
            if kind == "video" and job.get("files"):
                return jsonify({"error": "a batch of files is for pictures"}), 400
            job.update(kind=kind, profile=profile, label=str(body.get("label") or "")[:200],
                       state="waiting_gpu", queued_at=time.time(), fail_counts={}, error=None)
            _stereo_save(job)
            _stereo["wake"].set()
            return jsonify(stereo_view(job)), 201
        if job["state"] in ("held", "failed") and os.path.exists(os.path.join(_stereo_dir(key), job["source"])):
            job.update(state="waiting_gpu", queued_at=time.time(), fail_counts={}, error=None, not_before=0)
            _stereo_save(job)
            _stereo["wake"].set()
        return jsonify(stereo_view(job)), 200


@app.route("/v1/stereo/jobs/<key>")
def stereo_job_get(key):
    """State, progress, error and (when done) the result names with their sizes."""
    if not authorised(request):
        return jsonify({"error": "unauthorised"}), 401
    if not _stereo_key(key):
        return jsonify({"error": "bad key"}), 400
    jobs = stereo_jobs()
    with _stereo["lock"]:
        job = jobs.get(key)
        if job is None:
            return jsonify({"error": "no such job"}), 404
        return jsonify(stereo_view(job))


@app.route("/v1/stereo/jobs/<key>/result/<path:name>")
def stereo_result(key, name):
    """One result file (names as listed by the job: sbs.mp4, depth.mp4, meta.json;
    pictures sbs.jpg, depth.png, meta.json - <stem>/... for a batch). Range
    requests work (206), so a pull can resume."""
    if not authorised(request):
        return jsonify({"error": "unauthorised"}), 401
    if not _stereo_key(key):
        return jsonify({"error": "bad key"}), 400
    jobs = stereo_jobs()
    with _stereo["lock"]:
        job = jobs.get(key)
        if job is None:
            return jsonify({"error": "no such job"}), 404
        if job["state"] != "done":
            return jsonify({"error": "job is %s, not done" % job["state"], "state": job["state"]}), 409
        if name not in [r["name"] for r in job.get("results", [])]:
            return jsonify({"error": "no such result"}), 404
        job["fetched_at"] = time.time()
        path = os.path.join(_stereo_dir(key), ("out/" + name) if job.get("files") else name)
    mime = {"mp4": "video/mp4", "jpg": "image/jpeg", "png": "image/png", "json": "application/json"}.get(
        name.rsplit(".", 1)[-1], "application/octet-stream")
    from flask import send_file
    resp = send_file(path, mimetype=mime, conditional=True, max_age=0)
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.route("/v1/stereo/jobs/<key>/hold", methods=["POST"])
def stereo_hold(key):
    """Stops the job's driver but keeps everything (finished chunks included), to
    make room for another job; POST /jobs again (or /resume) continues it. One
    held job at most. A queued job is simply parked."""
    if not authorised(request):
        return jsonify({"error": "unauthorised"}), 401
    if not _stereo_key(key):
        return jsonify({"error": "bad key"}), 400
    jobs = stereo_jobs()
    with _stereo["lock"]:
        job = jobs.get(key)
        if job is None:
            return jsonify({"error": "no such job"}), 404
        if job["state"] == "held":
            return jsonify(stereo_view(job))
        other = [k for k, j in jobs.items() if j.get("state") == "held" and k != key]
        act = _stereo["active"]
        is_active = bool(act and act["key"] == key and act["proc"].poll() is None)
        if job["state"] not in ("waiting_gpu", "running") or (job["state"] == "running" and not is_active):
            return jsonify({"error": "a %s job cannot be held" % job["state"], "state": job["state"]}), 409
        if other:
            return jsonify({"error": "another job is already held", "held": other[0]}), 409
        if not is_active:
            job.update(state="held", held_at=time.time())
            _stereo_save(job)
            return jsonify(stereo_view(job))
        act["hold"] = True
        _stereo_term(act)
    _stereo_wait(act, 15)
    stereo_reap()
    with _stereo["lock"]:
        return jsonify(stereo_view(job)), (200 if job["state"] == "held" else 202)


@app.route("/v1/stereo/jobs/<key>/resume", methods=["POST"])
def stereo_resume(key):
    if not authorised(request):
        return jsonify({"error": "unauthorised"}), 401
    if not _stereo_key(key):
        return jsonify({"error": "bad key"}), 400
    jobs = stereo_jobs()
    with _stereo["lock"]:
        job = jobs.get(key)
        if job is None:
            return jsonify({"error": "no such job"}), 404
        if job["state"] != "held":
            return jsonify({"error": "job is %s, not held" % job["state"], "state": job["state"]}), 409
        job.update(state="waiting_gpu", queued_at=time.time(), not_before=0)
        _stereo_save(job)
        _stereo["wake"].set()
        return jsonify(stereo_view(job))


@app.route("/v1/stereo/jobs/<key>", methods=["DELETE"])
def stereo_delete(key):
    """Stops the driver if it runs and removes the job with all its files.
    Idempotent: an unknown key answers 200 with deleted false."""
    if not authorised(request):
        return jsonify({"error": "unauthorised"}), 401
    if not _stereo_key(key):
        return jsonify({"error": "bad key"}), 400
    jobs = stereo_jobs()
    with _stereo["lock"]:
        act = _stereo["active"]
        mine = bool(act and act["key"] == key)
        if mine:
            act["delete"] = True
            _stereo_term(act)
    if mine:
        if not _stereo_wait(act, 10):
            _stereo_kill(act)
            _stereo_wait(act, 5)
        stereo_reap()
    with _stereo["lock"]:
        existed = jobs.pop(key, None) is not None
        shutil.rmtree(_stereo_dir(key), ignore_errors=True)
    return jsonify({"key": key, "deleted": existed})


if __name__ == "__main__":
    import tempfile
    from waitress import serve
    os.makedirs(UPLOAD_SPOOL_DIR, exist_ok=True)
    tempfile.tempdir = UPLOAD_SPOOL_DIR   # waitress's spool files (unlinked at once: nothing is left behind)
    stereo_jobs()                       # reads the jobs back: running ones wait for their turn again
    threading.Thread(target=idle_watcher, daemon=True).start()
    threading.Thread(target=seg_manager, daemon=True).start()
    threading.Thread(target=stereo_manager, daemon=True).start()
    port = int(os.environ.get("KSF_VISION_PORT", "8410"))
    print(f"[vision] listening on :{port}", flush=True)
    # waitress rather than Flask's dev server: this takes 100 MB uploads from
    # another host all day and the dev server is explicitly not for that.
    # 6 threads: an interpolated stream holds one for as long as someone watches,
    # and uploads/analyses must not queue behind it.
    host = os.environ.get("KSF_VISION_HOST") or config().get("host") or "127.0.0.1"
    if host not in ("127.0.0.1", "::1", "localhost") and not config().get("token"):
        raise SystemExit("The worker accepts files and runs models for anyone who reaches it: set a \"token\" in its "
                         "config (and the same one in the server's settings) before KSF_VISION_HOST exposes it.")
    serve(app, host=host, port=port, threads=6,
          max_request_body_size=UPLOAD_MAX_BYTES)
