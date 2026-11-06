"""What song_merge would do to the library, without doing any of it: builds
the music catalogue from the files (as server.py does from its path index),
listens to every candidate pair and writes each pair's verdict and the file
that would stay. Reads the library only - the tag cache is a copy and the
decisions go to a temp file.

  venv/bin/python song_merge_report.py OUT.json [--store song_merges.json]
"""
import argparse
import json
import os
import shutil
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import music_library          # noqa: E402
import song_merge             # noqa: E402

BASE_DIR = os.environ.get("ATRIHORD_DIR") or os.path.dirname(os.path.abspath(__file__))
DOWNLOADS = os.path.join(BASE_DIR, "web_files", "downloads")


def roots():
    out = {"downloads": DOWNLOADS}
    try:
        with open(os.path.join(BASE_DIR, "roots.json"), encoding="utf-8") as fh:
            for r in json.load(fh):
                if r.get("id") and r.get("path"):
                    out[r["id"]] = r["path"]
    except (OSError, ValueError):
        pass
    return out


def names_of(all_roots):
    out = []
    for rid, base in all_roots.items():
        for d, dirs, files in os.walk(base):
            dirs[:] = [x for x in dirs if not x.startswith(".")]
            for f in files:
                if f.startswith(".") or not music_library.is_audio(f):
                    continue
                full = os.path.join(d, f)
                try:
                    st = os.stat(full)
                except OSError:
                    continue
                if st.st_size == 0:
                    continue                    # a tombstone
                out.append((rid, os.path.relpath(full, base).replace(os.sep, "/"), full, st))
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("out")
    ap.add_argument("--store", help="decisions to start from (copied, never written)")
    args = ap.parse_args()
    tmp = tempfile.mkdtemp(prefix="song_merge_report_")
    try:
        all_roots = roots()
        t0 = time.time()
        names = names_of(all_roots)
        tags_copy = os.path.join(tmp, "music_tags.json")
        if os.path.exists(os.path.join(BASE_DIR, "music_tags.json")):
            shutil.copy(os.path.join(BASE_DIR, "music_tags.json"), tags_copy)
        songs_index = {}
        try:
            with open(os.path.join(DOWNLOADS, "youtube_music", ".songs.json"), encoding="utf-8") as fh:
                songs_index = json.load(fh)
        except (OSError, ValueError):
            pass
        catalog = music_library.build(names, music_library.TagCache(tags_copy), "downloads",
                                      songs_index=songs_index)
        songs = catalog["songs"]
        print(f"{len(names)} names, {len(songs)} songs in {time.time() - t0:.0f}s", flush=True)
        store = os.path.join(tmp, "song_merges.json")
        if args.store and os.path.exists(args.store):
            shutil.copy(args.store, store)
        report = song_merge.run(songs, all_roots, "downloads", DOWNLOADS, store, apply=False,
                                log=lambda m: print(m, flush=True))
        decided = json.load(open(store, encoding="utf-8"))["pairs"]
        # Every pair listened to, same or not, with its numbers.
        report["verdicts"] = sorted(decided.values(), key=lambda v: -(v.get("loudness") or 0))
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump(report, fh, ensure_ascii=False, indent=1)
        print(f"{report['candidates']} candidates: {report['same']} the same recording, "
              f"{report['different']} different, {report['undecodable']} undecodable "
              f"({time.time() - t0:.0f}s) -> {args.out}")
    finally:
        shutil.rmtree(tmp, True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
