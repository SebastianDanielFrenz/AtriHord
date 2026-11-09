"""vision_worker (vault KSF-059): a batch's models are loaded and used under ONE
hold of the GPU lock, so a second batch can never evict them in between.

Runs on the vision host with its venv, the models faked (no GPU work):
  .venv/bin/python vision_lock_check.py <vision_worker.py to test> [expect-race]

Two threads post to /v1/analyze at once, like a music cover (caption, tags,
faces) and the indexer's tags-only sweep. The fake passes read MODELS exactly
as the real ones do, so a pass whose model was evicted fails the way
production did: HTTP 500 with KeyError 'processor'. With expect-race the check
passes only if that failure DOES show up - proof the harness reproduces the
bug on the old file.
"""
import importlib.util
import io
import os
import sys
import threading
import time

from PIL import Image

WORKER = os.path.abspath(sys.argv[1])
EXPECT_RACE = len(sys.argv) > 2 and sys.argv[2] == "expect-race"
# It imports the file it is given and runs its module code: never anything but
# a vision worker (run_all.sh once handed it the staged server.py).
if "def ensure_models(" not in open(WORKER, encoding="utf-8").read():
    sys.exit(f"{WORKER} is not a vision worker (no ensure_models) - not imported")
# genre, grain and upscaler are imported by the worker; they live next to the
# installed copy, which is what the test file sits beside or is pointed at.
sys.path.insert(0, os.environ.get("VISION_DIR", "/data/ksf-vision"))
spec = importlib.util.spec_from_file_location("vision_worker_under_test", WORKER)
vw = importlib.util.module_from_spec(spec)
spec.loader.exec_module(vw)

ok = True


def check(label, cond, detail=""):
    global ok
    ok = ok and bool(cond)
    print(("PASS " if cond else "FAIL ") + label + (f"  ({detail})" if detail and not cond else ""), flush=True)


loads = {"florence": 0, "tagger": 0, "faces": 0}


def fake_load(name, keys):
    def load():
        time.sleep(0.03)              # a load takes a while, holding the lock
        for k in keys:
            vw.MODELS[k] = object()
        loads[name] += 1
    return load


vw.load_florence = fake_load("florence", ("florence", "processor"))
vw.load_tagger = fake_load("tagger", ("tagger", "tag_names", "tag_cats", "tag_input"))
vw.load_faces = fake_load("faces", ("faces",))


def fake_florence(images, task):
    vw.MODELS["processor"], vw.MODELS["florence"]      # what _florence_chunk reads first
    time.sleep(0.005)
    return ["a caption"] * len(images)


def fake_tag(images):
    vw.MODELS["tagger"]
    time.sleep(0.005)
    return [{}] * len(images)


def fake_faces(images):
    vw.MODELS["faces"]
    return [[]] * len(images)


vw.florence, vw.tag, vw.detect_faces = fake_florence, fake_tag, fake_faces
vw.authorised = lambda req: True
vw.in_window = lambda: True
vw.config = lambda: {"allow_urgent_anytime": True, "allow_both": False}

buf = io.BytesIO()
Image.new("RGB", (8, 8), (200, 30, 30)).save(buf, "PNG")
PNG = buf.getvalue()
client = vw.app.test_client()
results = {"cover": [], "index": []}
res_lock = threading.Lock()


def post(kind, tasks, n):
    for _ in range(n):
        r = client.post("/v1/analyze", data={"file": (io.BytesIO(PNG), "c.png"), "tasks": tasks, "urgent": "1"},
                        content_type="multipart/form-data")
        body = r.get_json(silent=True) or {}
        with res_lock:
            results[kind].append((r.status_code, body.get("error")))


def upscales_queued(stop):
    """The page asks for the cover's upscale while it is being analysed: an
    upscale waiting for the GPU makes a batch sleep before it runs its passes.
    In the old worker that sleep came AFTER loading its models - the gap the
    other batch evicted them in."""
    while not stop.is_set():
        vw.STATE["upscale_waiting"] = 1
        time.sleep(0.03)
        vw.STATE["upscale_waiting"] = 0
        time.sleep(0.03)


N = 60
stop = threading.Event()
toggler = threading.Thread(target=upscales_queued, args=(stop,), daemon=True)
toggler.start()
threads = [threading.Thread(target=post, args=("cover", "caption,tags,faces", N)),
           threading.Thread(target=post, args=("index", "tags", N))]
for t in threads:
    t.start()
for t in threads:
    t.join(120)
stop.set()
toggler.join()
vw.STATE["upscale_waiting"] = 0

errors = [e for kind in results for code, e in results[kind] if code != 200]
done = sum(len(v) for v in results.values())
print(f"{done} batches, {len(errors)} failed, loads {loads}, errors seen {sorted(set(map(str, errors)))[:3]}",
      flush=True)
check("every batch answered", done == 2 * N, done)
if EXPECT_RACE:
    check("the OLD worker loses the race: a cover fails with KeyError 'processor'",
          any(e == "'processor'" for e in errors), errors[:3])
else:
    check("no batch fails: nothing is evicted between loading and the pass", not errors, errors[:5])
    check("... and the two kinds really did alternate (Florence loaded again and again)", loads["florence"] > 5, loads)
    # One model at a time still holds: a tags-only batch evicts Florence and the faces.
    r = client.post("/v1/analyze", data={"file": (io.BytesIO(PNG), "c.png"), "tasks": "tags", "urgent": "1"},
                    content_type="multipart/form-data")
    check("a tags-only batch still evicts Florence and the face models afterwards",
          r.status_code == 200 and "florence" not in vw.MODELS and "processor" not in vw.MODELS
          and "faces" not in vw.MODELS and "tagger" in vw.MODELS, sorted(vw.MODELS))
    check("the lock is free again", vw._lock.acquire(blocking=False) and (vw._lock.release() or True))
print("ALL PASS" if ok else "SOME CHECKS FAILED")
sys.exit(0 if ok else 1)
