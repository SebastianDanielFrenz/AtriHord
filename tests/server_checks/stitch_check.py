"""Twitter posts cut into pieces: post_stitch.py's decisions on made-up
pictures (one picture cut three ways, and the look-alikes the library holds:
phone screenshots, letterboxed pictures, two pictures of one size, a cut
through something smooth), then server.py's pass out of a STAGED server.py's
AST over a temp library - the joined picture written, the pieces trashed
with a ban (tombstones, so gallery-dl does not fetch them again), favourites
and share links moved to it, a second pass doing nothing, restored pieces
left alone, and the pass after a download.

  venv/bin/python stitch_check.py <staged server.py>
post_stitch.py is taken from beside it.
"""
import ast
import atexit
import json
import os
import shutil
import sys
import tempfile
import time

BASE = os.environ.get("ATRIHORD_DIR") or os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SERVER = os.path.abspath(sys.argv[1])
STAGE = os.path.dirname(SERVER)
sys.path.insert(0, BASE)
sys.path.insert(0, STAGE)
tmp = tempfile.mkdtemp(prefix="stitch_check_")
atexit.register(shutil.rmtree, tmp, True)
os.environ["KSF_MEDIA_AI_DIR"] = os.path.join(tmp, "ai")
os.makedirs(os.environ["KSF_MEDIA_AI_DIR"])
import numpy as np            # noqa: E402
from PIL import Image         # noqa: E402
import post_stitch as ps      # noqa: E402

assert os.path.dirname(os.path.abspath(ps.__file__)) == STAGE, ps.__file__
ok = True


def check(label, cond, detail=""):
    global ok
    ok = ok and bool(cond)
    print(("PASS " if cond else "FAIL ") + label + (f"  ({detail})" if detail and not cond else ""), flush=True)


# ---- made-up pictures ----------------------------------------------------------------------------
def picture(w, h, seed):
    """A picture that measures like the library's: smooth shading with a
    1/f^1.6 spectrum (it changes from row to row as a photo does - pure 1/f
    is pixel noise, and JPEG garbles that differently in every piece) and
    hard edges - discs and lines - where the fine detail is, as outlines and
    hair are in a drawing. Lines keep clear of the cuts the checks make
    (every twelfth of a side): a hard edge lying exactly ON a cut is the one
    thing no seam test can tell from two pictures."""
    rng = np.random.default_rng(seed)
    y, x = np.mgrid[0:h, 0:w].astype(np.float32)
    fy, fx = np.fft.fftfreq(h)[:, None], np.fft.fftfreq(w)[None, :]
    amp = 1.0 / np.maximum(np.hypot(fy, fx), 1.0 / max(w, h)) ** 1.6
    img = np.zeros((h, w, 3), np.float32)
    base = np.real(np.fft.ifft2(amp * np.exp(2j * np.pi * rng.random((h, w)))))
    for c in range(3):
        own = np.real(np.fft.ifft2(amp * np.exp(2j * np.pi * rng.random((h, w)))))
        layer = 0.8 * base + 0.2 * own
        img[..., c] = 128 + 45 * (layer - layer.mean()) / layer.std()
    for _ in range(60):
        cx, cy, r = rng.integers(0, w), rng.integers(0, h), rng.integers(8, 70)
        img[(x - cx) ** 2 + (y - cy) ** 2 < r * r] = rng.integers(0, 255, 3)

    def clear_of_cuts(pos, size):
        return min(abs(pos - k * size / 12) for k in range(13)) > 6

    for _ in range(30):
        x0 = rng.integers(0, w)
        if clear_of_cuts(x0, w):
            img[:, max(0, x0 - 2):x0 + 2] = rng.integers(0, 255, 3)
        y0 = rng.integers(0, h)
        if clear_of_cuts(y0, h):
            img[max(0, y0 - 2):y0 + 2, :] = rng.integers(0, 255, 3)
    img += rng.normal(0, 1, img.shape)
    return Image.fromarray(np.clip(img, 0, 255).astype(np.uint8))


def ramp(w, h):
    """Brightness that changes across the picture and a little down it, with
    no fine detail - a backdrop, a blurred background."""
    y, x = np.mgrid[0:h, 0:w].astype(np.float32)
    v = 60 + 140 * x / w + 0.4 * y
    return Image.fromarray(np.clip(np.stack([v, v * 0.9, v * 0.8], -1), 0, 255).astype(np.uint8))


def screenshot(seed):
    """A phone screenshot: white, a coloured bar on top and at the bottom,
    and pictures inset from the edges."""
    im = Image.new("RGB", (540, 1170), (255, 255, 255))
    arr = np.asarray(im).copy()
    arr[:90] = (40, 120, 220)
    arr[-80:] = (245, 245, 245)
    arr[150:152] = (200, 200, 200)
    im = Image.fromarray(arr)
    im.paste(picture(480, 420, seed), (30, 200))
    im.paste(picture(480, 420, seed + 1), (30, 660))
    return im


def letterbox(seed):
    im = Image.new("RGB", (800, 600), (255, 255, 255))
    arr = np.asarray(im).copy()
    arr[:60] = 0
    arr[-60:] = 0
    im = Image.fromarray(arr)
    im.paste(picture(500, 420, seed), (150, 90))
    return im


def save_pieces(folder, tid, pieces, ext="jpg"):
    os.makedirs(folder, exist_ok=True)
    names = []
    for n, im in enumerate(pieces, 1):
        name = f"{tid}_{n}.{ext}"
        im.save(os.path.join(folder, name), quality=90) if ext == "jpg" else im.save(os.path.join(folder, name))
        os.utime(os.path.join(folder, name), (1_700_000_000 + n, 1_700_000_000 + n))
        names.append(name)
    return names


def cut(im, layout, n=2):
    w, h = im.size
    if layout == "v":
        step = h // n
        return [im.crop((0, i * step, w, (i + 1) * step)) for i in range(n)]
    if layout == "h":
        step = w // n
        return [im.crop((i * step, 0, (i + 1) * step, h)) for i in range(n)]
    return [im.crop((0, 0, w // 2, h // 2)), im.crop((w // 2, 0, w, h // 2)),
            im.crop((0, h // 2, w // 2, h)), im.crop((w // 2, h // 2, w, h))]


def plan_of(folder, names):
    return ps.plan([ps.load_edges(os.path.join(folder, n)) for n in names])


work = os.path.join(tmp, "work")
cases = [
    ("stacked in two", cut(picture(900, 1400, 1), "v"), [("v", [0, 1])]),
    ("side by side", cut(picture(1600, 900, 2), "h"), [("h", [0, 1])]),
    ("a 2x2 grid", cut(picture(1400, 1400, 3), "grid"), [("grid", [0, 1, 2, 3])]),
    ("a strip of four", cut(picture(800, 2400, 4), "v", 4), [("v", [0, 1, 2, 3])]),
    ("two pictures of one size, stacked", [picture(900, 700, 5), picture(900, 700, 6)], []),
    ("two pictures of one size, side by side", [picture(800, 900, 7), picture(800, 900, 8)], []),
    ("two phone screenshots of one app", [screenshot(9), screenshot(19)], []),
    ("two letterboxed pictures", [letterbox(29), letterbox(39)], []),
    ("one picture, and a third that is not", cut(picture(900, 1400, 10), "v") + [picture(900, 700, 11)],
     [("v", [0, 1])]),
    ("a cut through something smooth, alone", cut(ramp(900, 1200), "v"), []),
]
for i, (label, pieces, want) in enumerate(cases):
    names = save_pieces(work, str(100 + i), pieces)
    got = plan_of(work, names)
    check(f"{label}: {want or 'nothing joins'}", got == want, got)

# A strip whose second cut runs through a smooth band: accepted beside a strong cut.
strip = picture(900, 2100, 13)
band = np.asarray(strip).copy()
band[1300:1500] = np.asarray(ramp(900, 200))
pieces = cut(Image.fromarray(band), "v", 3)
names = save_pieces(work, "120", pieces)
seams = [ps.verdict(ps.seam(ps.load_edges(os.path.join(work, names[k]))[2]["bottom"],
                            ps.load_edges(os.path.join(work, names[k + 1]))[2]["top"])) for k in (0, 1)]
check("a smooth cut is soft, the other strong", seams == ["strong", "soft"], seams)
check("... and the whole strip joins", plan_of(work, names) == [("v", [0, 1, 2])], plan_of(work, names))

# The joined picture: the original again (JPEG pieces - within JPEG's error).
orig = picture(1400, 1400, 3)
names = save_pieces(work, "130", cut(orig, "grid"), ext="png")
joined = ps.join([os.path.join(work, n) for n in names], "grid")
check("a PNG grid joins back to the original exactly",
      joined.size == orig.size and np.array_equal(np.asarray(joined), np.asarray(orig)))

# posts_in and stitch_post
posts = ps.posts_in(work)
check("posts_in: every post with two or more pictures", "100" in posts and len(posts["103"]) == 4, sorted(posts))
open(os.path.join(work, "140_1.jpg"), "wb").close()
picture(50, 50, 1).save(os.path.join(work, "140_2.jpg"))
picture(50, 50, 1).save(os.path.join(work, "141_1.jpg"))
posts = ps.posts_in(work)
check("... empty files (tombstones) and single pictures do not count",
      "140" not in posts and "141" not in posts, sorted(posts))
res = ps.stitch_post(work, "100", posts["100"])
part = os.path.join(work, ps.part_name("100_1-2.jpg"))
check("stitch_post writes the joined picture for <id>_1-2.jpg, the size of both pieces",
      res and res[0]["out"] == "100_1-2.jpg" and Image.open(part).size == (900, 1400), res)
check("... under a hidden name until the pieces are gone",
      os.path.basename(part).startswith(".") and not os.path.exists(os.path.join(work, "100_1-2.jpg")))
check("... dated as the post", int(os.stat(part).st_mtime) == 1_700_000_002, os.stat(part).st_mtime)
check("... and not a piece's name", not ps.PIECE_RE.match("100_1-2.jpg"))
os.replace(part, os.path.join(work, "100_1-2.jpg"))
again = ps.stitch_post(work, "100", posts["100"])
check("a joined picture that is already there is left alone", again and again[0].get("existing"), again)
check("nothing joins: nothing written", ps.stitch_post(work, "104", posts["104"]) == []
      and not [f for f in os.listdir(work) if f.startswith("104_1-")])
check("an unreadable piece: an error, not an exception",
      isinstance(ps.stitch_post(work, "999", [(1, "nope.jpg"), (2, "nope2.jpg")]), dict))

# ---- the server's pass ---------------------------------------------------------------------------
tree = ast.parse(open(SERVER, encoding="utf-8").read())
defs = {}
ns = {"__name__": "harness", "__file__": os.path.join(BASE, "server.py")}
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


def resolve(thunk):
    for _ in range(400):
        try:
            return thunk()
        except NameError as exc:
            missing = getattr(exc, "name", None) or str(exc).split("'")[1]
            if missing in ns or missing not in defs:
                raise
            load(missing)
    raise RuntimeError("names did not resolve")


def load(name):
    code = compile(ast.Module(body=[defs[name]], type_ignores=[]), SERVER, "exec")
    resolve(lambda: exec(code, ns))


def _names(code):
    out = set(code.co_names)
    for const in code.co_consts:
        if hasattr(const, "co_names"):
            out |= _names(const)
    return out


def preload(name, seen=None):
    """Every server.py name a function uses, first - a re-run after a
    NameError would repeat moves into the trash."""
    seen = set() if seen is None else seen
    if name in seen or name not in defs:
        return
    seen.add(name)
    code = compile(ast.Module(body=[defs[name]], type_ignores=[]), SERVER, "exec")
    for used in _names(code):
        if used not in ns:
            preload(used, seen)
    if name not in ns:
        load(name)


def F(name):
    preload(name)
    return ns[name]


lib = os.path.join(tmp, "lib")
acct = os.path.join(lib, "twitter", "@bob")
R = "downloads"
ADMIN = {"username": "alice", "role": "admin"}
recent = []
ns.update(BASE_DIR=tmp, DOWNLOADS_DIR=lib, DEFAULT_ROOT_ID=R, CONFIG={"twitter_stitch": {"enabled": True}},
          get_root=lambda root_id: {"id": R, "path": lib, "name": "Downloads"} if root_id == R else None,
          get_all_roots=lambda: [{"id": R, "path": lib, "name": "Downloads"}],
          download_category_of=lambda root_id, rel: rel.split("/")[0],
          invalidate_media_caches=lambda: None,
          recent_media_store=type("S", (), {"record_recent_media": staticmethod(
              lambda base, items: recent.extend(i["path"] for i in items))}))
for name in ("app", "TRASH_DIR_NAME", "DELETE_BAN", "DELETE_RELEASE", "GALLERY_DL_CATEGORIES",
             "EMPTY_FILES_STORE", "_empty_files", "_empty_files_lock", "_empty_files_loaded", "_save_empty_files",
             "note_empty_files", "FAVORITES_FILE", "_favorites_lock", "_favorites", "SHARES_FILE", "shares_lock",
             "STITCH_CHUNK", "STITCH_WORKERS", "_stitch_lock", "_stitch"):
    if name not in ns and name in defs:
        load(name)
ns["EMPTY_FILES_STORE"] = os.path.join(tmp, "empty_files.json")
ns["FAVORITES_FILE"] = os.path.join(tmp, "favorites.json")
ns["SHARES_FILE"] = os.path.join(tmp, "shares.json")
for name in ("run_stitch", "stitch_after_download", "set_favorite", "user_favorites",
             "_read_shares_unlocked", "trash_resolved"):
    preload(name)

pieces = save_pieces(acct, "200", cut(picture(1000, 1600, 21), "v"))
save_pieces(acct, "201", [picture(700, 700, 22), picture(700, 700, 23)])           # two pictures
save_pieces(acct, "202", cut(picture(1400, 1400, 24), "grid"))
json.dump({"alice": {R: {"twitter/@bob/200_2.jpg": 1.0}}, "carol": {R: {"twitter/@bob/201_1.jpg": 2.0}}},
          open(ns["FAVORITES_FILE"], "w"))
json.dump([{"id": "s1", "kind": "pool", "items": [
              {"kind": "file", "root": R, "path": "twitter/@bob/200_1.jpg"},
              {"kind": "file", "root": R, "path": "twitter/@bob/200_2.jpg"},
              {"kind": "file", "root": R, "path": "twitter/@bob/201_1.jpg"}]},
           {"id": "s2", "kind": "file", "root": R, "path": "twitter/@bob/202_3.jpg"}],
          open(ns["SHARES_FILE"], "w"))

posts = [(acct, tid, items) for tid, items in sorted(ps.posts_in(acct).items())]
resolve(lambda: ns["run_stitch"]("library", lambda: posts, "auto: stitch (library)", False))
st = ns["_stitch"]
check("the library pass: two posts joined, six pieces trashed, nothing failed",
      st["joined"] == 2 and st["pieces"] == 6 and st["failed"] == 0 and not st["running"] and not st["error"], st)
check("... the joined pictures are there",
      os.path.isfile(os.path.join(acct, "200_1-2.jpg")) and os.path.isfile(os.path.join(acct, "202_1-4.jpg")),
      sorted(os.listdir(acct)))
check("... every piece a 0-byte tombstone (gallery-dl will not fetch it again)",
      all(os.path.getsize(os.path.join(acct, f"{t}_{n}.jpg")) == 0 for t, k in (("200", 2), ("202", 4))
          for n in range(1, k + 1)))
trash = os.path.join(lib, ".trash", st["batches"][0], "twitter", "@bob") if st["batches"] else ""
check("... and the pieces in ONE trash batch, restorable",
      len(st["batches"]) == 1 and sorted(os.listdir(trash)) ==
      sorted([f"200_{n}.jpg" for n in (1, 2)] + [f"202_{n}.jpg" for n in range(1, 5)]),
      (st["batches"], os.listdir(trash) if trash and os.path.isdir(trash) else None))
manifest = json.load(open(os.path.join(lib, ".trash", st["batches"][0], "_manifest.json")))
check("... named for the pass, as a ban", manifest["deleted_by"] == "auto: stitch (library)"
      and manifest["mode"] == "ban", manifest)
check("the post of two separate pictures is untouched",
      all(os.path.getsize(os.path.join(acct, f"201_{n}.jpg")) > 0 for n in (1, 2)))
fav = json.load(open(ns["FAVORITES_FILE"]))
check("a favourite piece: the joined picture is a favourite too",
      "twitter/@bob/200_1-2.jpg" in fav["alice"][R] and "twitter/@bob/200_1-2.jpg" not in fav["carol"].get(R, {}),
      fav)
shares = {s["id"]: s for s in json.load(open(ns["SHARES_FILE"]))}
check("a share of two pieces lists the joined picture once, the other file kept",
      [i["path"] for i in shares["s1"]["items"]] == ["twitter/@bob/200_1-2.jpg", "twitter/@bob/201_1.jpg"],
      shares["s1"]["items"])
check("a share of one piece shows the joined picture", shares["s2"]["path"] == "twitter/@bob/202_1-4.jpg",
      shares["s2"])
check("the library pass fills no Recently downloaded strip", recent == [], recent)

posts = [(acct, tid, items) for tid, items in sorted(ps.posts_in(acct).items())]
resolve(lambda: ns["run_stitch"]("library", lambda: posts, "auto: stitch (library)", False))
check("a second pass: nothing more to do", st["joined"] == 0 and st["pieces"] == 0 and st["batches"] == [],
      st)

# Restored pieces beside their joined picture: left alone.
for n in (1, 2):
    os.remove(os.path.join(acct, f"200_{n}.jpg"))
    shutil.copy(os.path.join(trash, f"200_{n}.jpg"), os.path.join(acct, f"200_{n}.jpg"))
posts = [(acct, tid, items) for tid, items in sorted(ps.posts_in(acct).items())]
resolve(lambda: ns["run_stitch"]("library", lambda: posts, "auto: stitch (library)", False))
check("restored pieces are not trashed again",
      st["pieces"] == 0 and os.path.getsize(os.path.join(acct, "200_1.jpg")) > 0, st)

# After a download: only the posts it brought, in the background, into the strip.
save_pieces(acct, "300", cut(picture(1600, 800, 31), "h"))
save_pieces(acct, "301", cut(picture(1600, 800, 32), "h"))
new = [{"root": R, "path": "twitter/@bob/300_1.jpg"}, {"root": R, "path": "twitter/@bob/300_2.jpg"},
       {"root": R, "path": "twitter/other.mp4"}]
resolve(lambda: ns["stitch_after_download"](new))
for _ in range(200):
    time.sleep(0.05)
    if not st["running"] and st["scope"] == "download" and st["finished"]:
        break
check("after a download: its post joined, in the strip",
      st["scope"] == "download" and st["joined"] == 1 and recent == ["twitter/@bob/300_1-2.jpg"], (st, recent))
check("... a post it did not bring is left for the library pass",
      not os.path.exists(os.path.join(acct, "301_1-2.jpg")))
last = ns["_stitch_last"]
check("... and the library pass's result is still there beside it",
      last.get("library", {}).get("scope") == "library" and last.get("download", {}).get("joined") == 1,
      {k: (v.get("scope"), v.get("joined")) for k, v in last.items()})
check("... and the trash batch is named for the automatic pass",
      json.load(open(os.path.join(lib, ".trash", st["batches"][0], "_manifest.json")))["deleted_by"]
      == "auto: stitch", st["batches"])

# A stop between writing the joined picture and trashing the pieces: only a
# hidden file, and the next pass finishes the post.
save_pieces(acct, "400", cut(picture(1000, 1600, 41), "v"))
ps.stitch_post(acct, "400", ps.posts_in(acct)["400"])
visible = [f for f in os.listdir(acct) if f.startswith("400_") and not f.startswith(".")]
check("a stop before the trash: the pieces as they were, the joined picture hidden",
      sorted(visible) == ["400_1.jpg", "400_2.jpg"] and os.path.exists(os.path.join(acct, ps.part_name("400_1-2.jpg"))),
      sorted(f for f in os.listdir(acct) if "400_" in f))
posts = [(acct, "400", ps.posts_in(acct)["400"])]
resolve(lambda: ns["run_stitch"]("library", lambda: posts, "auto: stitch (library)", False))
check("... the next pass finishes it",
      st["joined"] == 1 and os.path.isfile(os.path.join(acct, "400_1-2.jpg"))
      and not os.path.exists(os.path.join(acct, ps.part_name("400_1-2.jpg")))
      and os.path.getsize(os.path.join(acct, "400_1.jpg")) == 0, st)

# Pieces that cannot be moved: the post stays exactly as it was.
save_pieces(acct, "500", cut(picture(1000, 1600, 51), "v"))
real_move = ns["move_to_trash"]


def refuse(root_id, rel_path, full_path, batch):
    if "/500_" in rel_path:
        raise OSError("read-only")
    return real_move(root_id, rel_path, full_path, batch)


ns["move_to_trash"] = refuse
posts = [(acct, "500", ps.posts_in(acct)["500"])]
resolve(lambda: ns["run_stitch"]("library", lambda: posts, "auto: stitch (library)", False))
ns["move_to_trash"] = real_move
check("pieces that cannot be moved: no joined picture, hidden or not, the pieces kept",
      st["joined"] == 0 and st["failed"] == 2
      and sorted(f for f in os.listdir(acct) if "500_" in f) == ["500_1.jpg", "500_2.jpg"],
      (st, sorted(f for f in os.listdir(acct) if "500_" in f)))

# Trash batches hold STITCH_BATCH joined pictures, not one per round.
for t, seed in (("600", 61), ("601", 62), ("602", 63)):
    save_pieces(acct, t, cut(picture(1600, 800, seed), "h"))
posts = [(acct, t, ps.posts_in(acct)[t]) for t in ("600", "601", "602")]
ns["STITCH_BATCH"], ns["STITCH_CHUNK"] = 2, 1
resolve(lambda: ns["run_stitch"]("library", lambda: posts, "auto: stitch (library)", False))
check("three joins, two to a batch: two trash batches", st["joined"] == 3 and len(st["batches"]) == 2, st)

print("ALL PASS" if ok else "SOME CHECKS FAILED")
sys.exit(0 if ok else 1)
