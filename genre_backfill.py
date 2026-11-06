"""One genre pass over all music, then genre joins the indexer's rotation.
Run as its own unit so the daemon's 30-minute slices do not stretch the first
pass over a day:
  systemd-run --user --unit ksf-genre-backfill --collect <app dir>/venv/bin/python <app dir>/genre_backfill.py
"""
import json
import os
import sys
import time

BASE = os.environ.get("ATRIHORD_DIR") or os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)
os.chdir(BASE)
import media_indexer  # noqa: E402

cfg = media_indexer.load_config(BASE)
roots = media_indexer.resolve_roots(BASE, cfg.get("roots"))
t0 = time.time()
while True:
    try:
        done, failed, total = media_indexer.sweep(dict(cfg, tasks="genre"), BASE, roots, verbose=True)
        print(f"[genre-backfill] {done} analysed, {failed} unusable of {total} in {time.time() - t0:.0f}s", flush=True)
        break
    except media_indexer.WindowClosed as exc:
        print(f"[genre-backfill] worker window closed ({exc}); retrying in 10 min", flush=True)
        time.sleep(600)
    except Exception as exc:
        print(f"[genre-backfill] sweep failed: {exc}; retrying in 60 s", flush=True)
        time.sleep(60)

# From now on the daemon keeps new music analysed. Read-modify-write of the
# config the daemon re-reads each rotation.
path = os.path.join(BASE, media_indexer.CONFIG_NAME)
with open(path, encoding="utf-8") as fh:
    current = json.load(fh)
pipeline = current.get("pipeline") or [current.get("tasks") or "tags"]
if "genre" not in pipeline:
    pipeline.append("genre")
    current["pipeline"] = pipeline
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(current, fh, indent=2)
    os.replace(tmp, path)
    print(f"[genre-backfill] genre added to the rotation: {pipeline}", flush=True)
