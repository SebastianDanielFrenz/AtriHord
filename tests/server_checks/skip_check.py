"""flush_audio of the STAGED media_indexer.py with the worker stubbed: a 5xx on
one track skips only that track; five in a row stop the batch, keeping what
was done; 401 and outages still stop at once. Temp AI store only.
  venv/bin/python skip_check.py <staged server.py>
media_indexer.py and media_ai.py are taken from beside it (run_all.sh copies
the production ones there when they are not part of the change).
"""
import io
import atexit
import os
import shutil
import sys
import tempfile
import urllib.error

STAGE = os.path.dirname(os.path.abspath(sys.argv[1]))
sys.path.insert(0, STAGE)
tmp = tempfile.mkdtemp(prefix="genre_skip_")
atexit.register(shutil.rmtree, tmp, True)
os.environ["KSF_MEDIA_AI_DIR"] = os.path.join(tmp, "ai")
os.makedirs(os.environ["KSF_MEDIA_AI_DIR"])
import media_ai        # noqa: E402
import media_indexer   # noqa: E402

assert os.path.dirname(os.path.abspath(media_indexer.__file__)) == STAGE
ok = True


def check(label, cond, detail=""):
    global ok
    ok = ok and bool(cond)
    print(("PASS " if cond else "FAIL ") + label + (f"  ({detail})" if detail and not cond else ""), flush=True)


REPLY = {"model": "m", "styles": [["Electronic---Techno", 0.8]], "embedding": "", "dim": 0}
answers = {}


def fake_post(cfg, pcm, urgent=False):
    a = answers[pcm.decode()]
    if isinstance(a, Exception):
        raise a
    return a


def err(code):
    return urllib.error.HTTPError("http://w/v1/audio", code, "x", {}, io.BytesIO(b"{}"))


media_indexer.decode_audio = lambda path: os.path.basename(path).encode()
media_indexer.post_audio = fake_post
stored = []
media_indexer.store_genre = lambda conn, root_id, rel, size, reply, models, ino=None: stored.append(rel)
conn = media_ai.connect(tmp)
models = media_indexer.models_for("genre")
cfg = {"prep_workers": 2}


def batch(names):
    return [("r", "music/" + n, "/x/" + n, 100, "audio") for n in names]


def recorded():
    return sorted(r[0] for r in conn.execute("SELECT path FROM media"))


# 1. One track the worker fails on, among good ones.
answers.update({"a": REPLY, "bad": err(500), "b": REPLY, "c": None})
stored.clear()
done, failed = media_indexer.flush_audio(conn, cfg, batch(["a", "bad", "b", "c"]), models, False, False)
check("a 500 on one track: the tracks after it still get done", stored == ["music/a", "music/b"], stored)
check("... counted as done 2, nothing found 1", (done, failed) == (2, 1), (done, failed))
check("... and the failed track is not recorded, so a later sweep retries it",
      "music/bad" not in recorded() and "music/c" in recorded(), recorded())

# 2. Five 500s in a row: the worker is broken - stop, keep what was done.
names = ["ok1"] + [f"e{i}" for i in range(5)] + ["ok2"]
answers.update({n: err(500) for n in names if n.startswith("e")})
answers.update({"ok1": None, "ok2": REPLY})
stored.clear()
try:
    media_indexer.flush_audio(conn, cfg, batch(names), models, False, False)
    raised = None
except urllib.error.HTTPError as exc:
    raised = exc.code
check("five 500s in a row stop the batch, like an outage", raised == 500, raised)
check("... the track after them is not tried", "music/ok2" not in stored, stored)
conn2 = media_ai.connect(tmp)
check("... what came before is committed", "music/ok1" in [r[0] for r in conn2.execute("SELECT path FROM media")])

# 3. Errors apart from each other never add up to five.
names = []
for i in range(6):
    names += [f"x{i}", f"g{i}"]
    answers[f"x{i}"] = err(502)
    answers[f"g{i}"] = REPLY
stored.clear()
done, failed = media_indexer.flush_audio(conn, cfg, batch(names), models, False, False)
check("six 5xx with good tracks between them: the batch goes on", done == 6 and len(stored) == 6, (done, stored))

# 4. Not the track's fault: 401 stops at once; a closed window too.
for exc, label in ((err(401), "401 (token)"), (media_indexer.WindowClosed("closed"), "a closed window"),
                   (urllib.error.URLError("refused"), "a worker that is down")):
    answers["t"] = exc
    try:
        media_indexer.flush_audio(conn, cfg, batch(["t"]), models, False, False)
        stopped = False
    except Exception as e:
        stopped = type(e) is type(exc)
    check(f"{label} still stops the batch at once", stopped)

print("ALL PASS" if ok else "SOME CHECKS FAILED")
sys.exit(0 if ok else 1)
