"""genre_map.py (next to this file, or the directory given) over a TEMP AI
store: synthetic "genres" as clusters in 1280-d, stored the way the genre
pass stores them. Checks the layout keeps clusters apart, a rebuild keeps the
map's shape, trashed tracks stay off it, and the landmark path past
EXACT_MAX places the rest sensibly.
  python genre_map_check.py <staged server.py, or its directory>
genre_map.py and media_ai.py are taken from beside it (run_all.sh copies the
production ones there when they are not part of the change).
"""
import atexit
import os
import shutil
import sys
import tempfile
import time

HERE = os.path.abspath(sys.argv[1] if len(sys.argv) > 1 else os.path.dirname(__file__))
if os.path.isfile(HERE):
    HERE = os.path.dirname(HERE)
sys.path.insert(0, HERE)
tmp = tempfile.mkdtemp(prefix="genre_map_")
atexit.register(shutil.rmtree, tmp, True)
os.environ["KSF_MEDIA_AI_DIR"] = os.path.join(tmp, "ai")
os.makedirs(os.environ["KSF_MEDIA_AI_DIR"])
import numpy as np   # noqa: E402
import media_ai      # noqa: E402
import genre_map     # noqa: E402

assert os.path.dirname(os.path.abspath(genre_map.__file__)) == HERE
ok = True


def check(label, cond, detail=""):
    global ok
    ok = ok and bool(cond)
    print(("PASS " if cond else "FAIL ") + label + (f"  ({detail})" if detail and not cond else ""), flush=True)


rng = np.random.default_rng(3)
GENRES = [("Electronic", "Techno"), ("Electronic", "House"), ("Rock", "Punk"), ("Rock", "Metal"),
          ("Classical", "Baroque"), ("Hip Hop", "Trap"), ("Stage & Screen", "Soundtrack"), ("Jazz", "Bop")]
centres = rng.normal(0, 1, (len(GENRES), 1280)).astype(np.float32)


def add_tracks(conn, start, count, trashed=()):
    labels = {}
    for i in range(start, start + count):
        g = i % len(GENRES)
        vec = (centres[g] + rng.normal(0, 0.55, 1280)).astype(np.float16)
        path = f"music/{GENRES[g][1].lower()}/track{i:05d}.opus"
        mid = media_ai.upsert(conn, "downloads", path, 1000 + i, "audio", {"genre_model": "m", "tags": ""})
        parent, style = GENRES[g]
        # A second style from another parent at a lower weight, so "most
        # weight wins" is exercised: the parent is still the cluster's.
        other = GENRES[(g + 3) % len(GENRES)]
        media_ai.replace_genres(conn, mid, [(f"{parent}---{style}", 0.6), (f"{other[0]}---{other[1]}", 0.2)])
        media_ai.store_embedding(conn, mid, "m", vec.astype("<f2").tobytes(), 1280)
        labels[mid] = g
    conn.commit()
    if trashed:
        media_ai.set_trashed(conn, trashed, True)
        conn.commit()
    return labels


def purity(tracks, labels, key, k=10):
    """Share of each track's k nearest map neighbours in its own genre."""
    P = np.array([t[key] for t in tracks])
    lab = np.array([labels[t["id"]] for t in tracks])
    D = ((P[:, None, :] - P[None, :, :]) ** 2).sum(-1)
    np.fill_diagonal(D, np.inf)
    nn = np.argpartition(D, k, axis=1)[:, :k]
    return float((lab[nn] == lab[:, None]).mean())


conn = media_ai.connect(tmp)
labels = add_tracks(conn, 0, 1200)
t0 = time.time()
first = genre_map.build(tmp)
took = time.time() - t0
print(f"  (first build of 1200: {took:.1f} s; {first['method']})")
check("every track with an embedding is on the map", first["count"] == 1200, first["count"])
check("2-D: tracks sit among their own genre (10-NN purity > 0.95)", purity(first["tracks"], labels, "p2") > 0.95,
      purity(first["tracks"], labels, "p2"))
check("3-D: the same", purity(first["tracks"], labels, "p3") > 0.95, purity(first["tracks"], labels, "p3"))
p2 = np.array([t["p2"] for t in first["tracks"]])
check("normalised into about [-1, 1]", np.abs(p2).max() < 1.6 and np.percentile(np.abs(p2), 99) <= 1.0,
      np.abs(p2).max())
by_id = {t["id"]: t for t in first["tracks"]}
t = by_id[next(iter(labels))]
check("a track carries its parent genre and styles, strongest first",
      first["parents"][t["g"]] == "Electronic" and first["styles"][t["s"][0][0]] == "Electronic---Techno"
      and t["s"][0][1] == 0.6 and len(t["s"]) == 2, t)
check("a first build starts cold", "previous map" not in first["method"]["2d"], first["method"])
genre_map.write_map(tmp, first)
check("written where the server reads it, and read back", genre_map.load_map(tmp)["count"] == 1200)

# A rebuild after the library grew by 10% and lost two tracks to the trash.
gone = [("downloads", f"music/{GENRES[0][1].lower()}/track00000.opus"),
        ("downloads", f"music/{GENRES[1][1].lower()}/track00001.opus")]
labels.update(add_tracks(conn, 1200, 120, trashed=gone))
t0 = time.time()
second = genre_map.build(tmp, genre_map.load_map(tmp))
print(f"  (warm rebuild of 1318: {time.time() - t0:.1f} s; {second['method']})")
check("the rebuild starts from the previous map", "previous map" in second["method"]["2d"]
      and "previous map" in second["method"]["3d"], second["method"])
check("trashed tracks are left off", second["count"] == 1318 and not {t["path"] for t in second["tracks"]}
      & {p for _r, p in gone}, second["count"])
check("new tracks land among their own genre", purity(second["tracks"], labels, "p2") > 0.95,
      purity(second["tracks"], labels, "p2"))
a = np.array([by_id[t["id"]]["p2"] for t in second["tracks"] if t["id"] in by_id])
b = np.array([t["p2"] for t in second["tracks"] if t["id"] in by_id])
moved = float(np.median(np.sqrt(((a - b) ** 2).sum(1))))
check("tracks already on the map stay about where they were (median shift < 0.15 of the map's radius)",
      moved < 0.15, moved)
# Genre centres keep their relative placement: same angle to each other.
def centre_angles(tracks, key):
    C = np.array([np.mean([t[key] for t in tracks if labels[t["id"]] == g], 0) for g in range(len(GENRES))])
    return np.arctan2(C[:, 1] - C[:, 1].mean(), C[:, 0] - C[:, 0].mean())
d = np.angle(np.exp(1j * (centre_angles(second["tracks"], "p2") - centre_angles(first["tracks"], "p2"))))
check("... and the map did not rotate or mirror (genre directions within 25 degrees)",
      np.abs(np.degrees(d)).max() < 25, str(np.degrees(d).round(1).tolist()))

# Past EXACT_MAX: a sample is laid out, the rest placed beside their neighbours.
genre_map.EXACT_MAX = 400
third = genre_map.build(tmp)           # cold, so the sample path runs on its own
check("past EXACT_MAX: a sample is laid out and the rest placed", "on 400 of 1318" in third["method"]["2d"],
      third["method"])
check("... and the placed tracks sit among their own genre too", purity(third["tracks"], labels, "p2") > 0.9,
      purity(third["tracks"], labels, "p2"))

# Degenerate libraries.
genre_map.EXACT_MAX = 6000
for n in (0, 1, 3, 12):
    sub = tempfile.mkdtemp(prefix="genre_small_", dir=tmp)
    os.environ["KSF_MEDIA_AI_DIR"] = os.path.join(sub, "ai")
    os.makedirs(os.environ["KSF_MEDIA_AI_DIR"])
    c = media_ai.connect(sub)
    add_tracks(c, 0, n)
    try:
        m = genre_map.build(sub)
        good = m["count"] == n and all(len(t["p2"]) == 2 and len(t["p3"]) == 3 and np.all(np.isfinite(t["p2"] + t["p3"]))
                                       for t in m["tracks"])
    except Exception as exc:
        good, m = False, exc
    check(f"a library of {n} track(s) builds a finite map", good, m if not good else "")

print("ALL PASS" if ok else "SOME CHECKS FAILED")
sys.exit(0 if ok else 1)
