"""Pick a tagger comparison set from the AI index and copy the pictures out.

Two sets, because a new tagger has to do two things:
  known   - pictures whose character the CURRENT tagger names confidently,
            several per character: a new tagger must not lose these.
  unknown - solo pictures the current tagger names nobody on: what a new
            tagger should add (newer characters).

Writes <out>/img/<media id>.<ext> and <out>/tagsel.json, the input of
eval_run.py / eval_report.py. Explicit pictures are left out so the set can be
looked at during the review. Run it where the index and the library are:

  python -I vision/tagger/make_eval_set.py <media-dl base dir> <out dir>
         [--chars 13] [--per 5] [--unknown 70] [--seed 1]

Runbook: vision/tagger/TAGGER_UPGRADE.md.
"""
import argparse
import json
import os
import random
import shutil
import sqlite3
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))   # repo root: media_ai, media_indexer

import media_ai          # noqa: E402
import media_indexer     # noqa: E402

KNOWN_MIN_P = 0.8        # "confidently": the current tagger's own score


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("base_dir")
    ap.add_argument("out_dir")
    ap.add_argument("--chars", type=int, default=13)
    ap.add_argument("--per", type=int, default=5)
    ap.add_argument("--unknown", type=int, default=70)
    ap.add_argument("--seed", type=int, default=1)
    args = ap.parse_args()
    rnd = random.Random(args.seed)

    cfg = media_indexer.load_config(args.base_dir)
    roots = dict(media_indexer.resolve_roots(args.base_dir, cfg.get("roots")))
    db = os.path.join(media_ai.store_dir(args.base_dir), "media_ai.sqlite3")
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row

    def tags_of(row):
        try:
            return json.loads(row["tags"] or "{}") or {}
        except ValueError:
            return {}

    def explicit(t):
        return any(e["tag"] in ("explicit", "questionable") and e["p"] >= 0.5
                   for e in t.get("rating", []))

    def path_of(row):
        base = roots.get(row["root"])
        return os.path.join(base, row["path"]) if base else None

    picked = []
    top = conn.execute(
        "SELECT t.tag, COUNT(*) n FROM media_tags t JOIN media m ON m.id = t.media_id"
        " WHERE t.category = 'character' AND m.kind = 'image' AND m.trashed IS NULL"
        " GROUP BY t.tag ORDER BY n DESC LIMIT ?", (args.chars * 3,)).fetchall()
    for tag in rnd.sample([r["tag"] for r in top], min(args.chars, len(top))):
        rows = conn.execute(
            "SELECT m.* FROM media_tags t JOIN media m ON m.id = t.media_id"
            " WHERE t.tag = ? AND t.category = 'character' AND m.kind = 'image'"
            " AND m.trashed IS NULL ORDER BY RANDOM() LIMIT 200", (tag,)).fetchall()
        got = 0
        for r in rows:
            t = tags_of(r)
            chars = t.get("character", [])
            if (len(chars) == 1 and chars[0]["p"] >= KNOWN_MIN_P and not explicit(t)
                    and path_of(r) and os.path.exists(path_of(r))):
                picked.append(("known", tag, r, t))
                got += 1
                if got >= args.per:
                    break

    rows = conn.execute(
        "SELECT m.* FROM media_tags t JOIN media m ON m.id = t.media_id"
        " WHERE t.tag = 'solo' AND m.kind = 'image' AND m.trashed IS NULL"
        " AND NOT EXISTS (SELECT 1 FROM media_tags c WHERE c.media_id = m.id"
        "                 AND c.category = 'character')"
        " ORDER BY RANDOM() LIMIT ?", (args.unknown * 5,)).fetchall()
    got = 0
    for r in rows:
        t = tags_of(r)
        if not explicit(t) and path_of(r) and os.path.exists(path_of(r)):
            picked.append(("unknown", None, r, t))
            got += 1
            if got >= args.unknown:
                break

    img_dir = os.path.join(args.out_dir, "img")
    os.makedirs(img_dir, exist_ok=True)
    sel = []
    for set_name, expect, r, t in picked:
        src = path_of(r)
        dst = os.path.join(img_dir, f"{r['id']}{os.path.splitext(src)[1].lower()}")
        shutil.copyfile(src, dst)
        sel.append({"set": set_name, "expect": expect, "id": r["id"], "abs": src,
                    "rating": [e["tag"] for e in t.get("rating", []) if e["p"] >= 0.5],
                    "wd_chars": [e["tag"] for e in t.get("character", [])],
                    "tag_model": r["tag_model"]})
    with open(os.path.join(args.out_dir, "tagsel.json"), "w", encoding="utf-8") as fh:
        json.dump(sel, fh, ensure_ascii=False, indent=1)
    print(f"{sum(s['set'] == 'known' for s in sel)} known, "
          f"{sum(s['set'] == 'unknown' for s in sel)} unknown -> {args.out_dir}")


if __name__ == "__main__":
    main()
