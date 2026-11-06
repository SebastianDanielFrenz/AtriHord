"""Twitter posts whose pictures are one picture cut into pieces.

User, 2026-09-23: "What about the algorithm that is supposed to find twitter
posts where images fit together seamlessly and stitch them together?" - then:
replace the pieces, for new downloads and the library already there.

Artists and photographers cut a tall or wide picture over a post's 2-4 slots
(Twitter would crop it otherwise): stacked top to bottom, side by side, or a
2x2 grid read like a page. gallery-dl saves each slot as
@<author>/<tweet id>_<n>.<ext>.

Two pieces belong together when the edge where they meet is no bigger a step
than the steps between neighbouring rows INSIDE each piece, and the two edge
rows show the same structure - down to the small changes along the seam, not
just the overall brightness (a blurred background correlates on that alone).
An edge with no detail proves nothing: two phone screenshots, two letterboxed
pictures, two white margins all "continue" into each other. Measured on the
library (2026-09-23): the screenshot and letterbox pairs have an inner step of
~0 or fine detail under 0.35; genuine cuts through a photo or a drawing have
both, and agree about as well across the cut as rows inside a piece do.

This module measures, decides and joins. What becomes of the pieces (trash,
a tombstone so gallery-dl does not fetch them again) is server.py's.
"""
import os
import re

import numpy as np
from PIL import Image

Image.MAX_IMAGE_PIXELS = 400_000_000

PIECE_RE = re.compile(r"^(\d+)_(\d+)\.(jpe?g|png|webp)$", re.IGNORECASE)
EDGE = 4          # rows (or columns) read at each edge

# A seam the pictures themselves vouch for.
STRONG = {"detail": 8.0, "inner": 0.5, "fine": 0.5, "ratio": 2.0, "corr": 0.9,
          "gcorr": 0.45, "gdrop": 0.3}
# A seam through something smooth (a blurred background, skin, a white
# backdrop with a figure in it): nothing contradicts it, but it proves little.
# Accepted only inside a layout that has a strong seam as well.
SOFT = {"detail": 8.0, "inner": 0.15, "ratio": 1.3, "corr": 0.98}


def load_edges(path):
    """(width, height, {"top", "bottom", "left", "right"}) - each edge as EDGE
    rows of RGB float32, ordered from the outside in, columns as rows.

    Only the four strips become arrays: the whole picture as float32 was
    ~140 MB for a 12-megapixel photo, and made the library pass 3 posts/s."""
    with Image.open(path) as im:
        if im.mode != "RGB":
            im = im.convert("RGB")
        w, h = im.size
        k = min(EDGE, h, w)

        def strip(box):
            return np.asarray(im.crop(box), dtype=np.float32)

        return w, h, {
            "top": strip((0, 0, w, k)),                                        # row 0 first
            "bottom": strip((0, h - k, w, h))[::-1],                           # last row first
            "left": strip((0, 0, k, h)).transpose(1, 0, 2),                    # column 0 first
            "right": strip((w - k, 0, w, h))[:, ::-1].transpose(1, 0, 2),      # last column first
        }


def _step(x, y):
    return float(np.abs(x - y).mean())


def _lum(row):
    return row @ np.array([0.299, 0.587, 0.114], dtype=np.float32)


def _corr(x, y):
    x = x - x.mean()
    y = y - y.mean()
    d = float(np.sqrt((x * x).sum() * (y * y).sum()))
    return float((x * y).sum() / d) if d > 0 else 0.0


def seam(first, second):
    """How well an edge of one piece continues into the facing edge of the
    next. `first` and `second` are the facing edges, outside row first (A's
    bottom and B's top, or A's right and B's left), the same length.

    step:   the step across the seam
    inner:  the median step between neighbouring rows inside the two pieces
    ratio:  step / inner - about 1 for one picture, large for two
    corr:   correlation of the two edge rows' brightness
    gcorr:  the same for their small-scale changes along the seam
    gbase:  gcorr between the first piece's two outermost rows - what "one
            picture" looks like in this very picture
    detail: the smaller brightness spread of the two edge rows
    fine:   the smaller mean small-scale change along the two edge rows

    All on brightness: a JPEG keeps colour at half resolution (4:2:0, as
    Twitter's are), so a hard edge one row past the cut bleeds colour into
    the edge row - a step of 25 where the brightness steps 4 (measured)."""
    first, second = _lum(first), _lum(second)
    la, lb = first[0], second[0]
    inner = [_step(first[i], first[i + 1]) for i in range(len(first) - 1)]
    inner += [_step(second[i], second[i + 1]) for i in range(len(second) - 1)]
    inner_step = float(np.median(inner)) if inner else 0.0
    step = _step(la, lb)
    ga, gb = np.diff(la), np.diff(lb)
    base = _corr(np.diff(first[0]), np.diff(first[1])) if len(first) > 1 else 0.0
    return {
        "step": round(step, 2),
        "inner": round(inner_step, 2),
        "ratio": round(step / max(inner_step, 1.0), 2),
        "corr": round(_corr(la, lb), 3),
        "gcorr": round(_corr(ga, gb), 3),
        "gbase": round(base, 3),
        "detail": round(float(min(la.std(), lb.std())), 1),
        "fine": round(float(min(np.abs(ga).mean(), np.abs(gb).mean())), 2),
    }


def verdict(m):
    """"strong", "soft" or None for one seam's measurements."""
    s = STRONG
    if (m["detail"] >= s["detail"] and m["inner"] >= s["inner"] and m["fine"] >= s["fine"]
            and m["ratio"] <= s["ratio"] and m["corr"] >= s["corr"]
            and m["gcorr"] >= s["gcorr"] and m["gcorr"] >= m["gbase"] - s["gdrop"]):
        return "strong"
    s = SOFT
    if (m["detail"] >= s["detail"] and m["inner"] >= s["inner"]
            and m["ratio"] <= s["ratio"] and m["corr"] >= s["corr"]):
        return "soft"
    return None


def plan(pieces):
    """Which pieces of one post make one picture, and how.

    pieces: [(width, height, edges)] in the post's order. Returns
    [(layout, [index, ...]), ...] - layout "grid" (four pieces, 1 2 / 3 4),
    "v" (stacked in order) or "h" (side by side in order) - for every group
    that joins; [] when nothing does. A group always holds a strong seam; its
    other seams may be soft. A 2x2 grid is tried first, then runs of
    neighbours in one direction; when both directions find runs, the one with
    more strong seams wins (then the one covering more pieces)."""
    n = len(pieces)
    cache = {}

    def link(i, j, d):
        key = (i, j, d)
        if key not in cache:
            (wa, ha, ea), (wb, hb, eb) = pieces[i], pieces[j]
            if d == "v":
                m = seam(ea["bottom"], eb["top"]) if wa == wb else None
            else:
                m = seam(ea["right"], eb["left"]) if ha == hb else None
            cache[key] = verdict(m) if m else None
        return cache[key]

    if n == 4:
        seams = [link(0, 1, "h"), link(2, 3, "h"), link(0, 2, "v"), link(1, 3, "v")]
        if all(seams) and seams.count("strong") >= 2:
            return [("grid", [0, 1, 2, 3])]

    best, best_key = [], (0, 0)
    for d in ("v", "h"):
        links = [link(i, i + 1, d) for i in range(n - 1)]
        groups, start = [], 0
        for i in range(n):
            if i == n - 1 or not links[i]:
                run = links[start:i]
                if i > start and "strong" in run:
                    groups.append((d, list(range(start, i + 1))))
                start = i + 1
        key = (sum(links[g[1][0]:g[1][-1]].count("strong") for g in groups),
               sum(len(g[1]) for g in groups))
        if key > best_key:
            best, best_key = groups, key
    return best


def join(paths, layout):
    """The pieces as one picture (see plan for the layouts)."""
    ims = []
    for p in paths:
        with Image.open(p) as im:
            alpha = im.mode in ("RGBA", "LA") or (im.mode == "P" and "transparency" in im.info)
            ims.append(im.convert("RGBA" if alpha else "RGB"))
    mode = "RGBA" if any(im.mode == "RGBA" for im in ims) else "RGB"
    ims = [im if im.mode == mode else im.convert(mode) for im in ims]
    grid = {"v": lambda: [[im] for im in ims],
            "h": lambda: [ims],
            "grid": lambda: [ims[:2], ims[2:]]}[layout]()
    width = max(sum(im.width for im in row) for row in grid)
    height = sum(max(im.height for im in row) for row in grid)
    out = Image.new(mode, (width, height), (255, 255, 255, 0) if mode == "RGBA" else (255, 255, 255))
    y = 0
    for row in grid:
        x = 0
        for im in row:
            out.paste(im, (x, y))
            x += im.width
        y += max(im.height for im in row)
    return out


def posts_in(folder):
    """{tweet id: [(n, file name), ...] in order} for the pieces in one author
    folder - posts with two or more pictures only. Empty files are the
    tombstones of pieces already deleted (or joined) and do not count."""
    by = {}
    try:
        names = os.listdir(folder)
    except OSError:
        return {}
    for name in names:
        m = PIECE_RE.match(name)
        if not m:
            continue
        try:
            if os.path.getsize(os.path.join(folder, name)) == 0:
                continue
        except OSError:
            continue
        by.setdefault(m.group(1), []).append((int(m.group(2)), name))
    return {tid: sorted(items) for tid, items in by.items() if len(items) > 1}


def joined_name(tweet_id, first, last, ext):
    """<tweet id>_<first>-<last>.<ext>: the post's id as gallery-dl writes it,
    so everything reading the id from a name still finds it - and not a
    piece's name, so nothing ever takes it for one."""
    return f"{tweet_id}_{first}-{last}.{ext}"


def part_name(out):
    """The hidden name a joined picture waits under (listings skip dot-files)
    until its pieces are in the trash - so the pieces and the joined picture
    are never both in the library, even when the server stops in between."""
    return "." + out + ".part"


def stitch_post(folder, tweet_id, items):
    """Measure one post, join what belongs together and write it under
    part_name(). Runs in a worker thread. items: [(n, file name)] as
    posts_in gives them.

    Returns [{"out": name, "pieces": [name, ...], "layout": ...}] for the
    pictures written, [] when nothing joins, or {"error": ...}. The caller
    trashes the pieces, then renames the part to "out". A joined picture
    that is already there (restored pieces, a second run) is left alone and
    reported with "existing": True, so the caller does not trash the pieces
    again."""
    try:
        paths = [os.path.join(folder, name) for _n, name in items]
        groups = plan([load_edges(p) for p in paths])
        done = []
        for layout, idx in groups:
            names = [items[i][1] for i in idx]
            nums = [items[i][0] for i in idx]
            exts = {os.path.splitext(n)[1].lower() for n in names}
            ext = "jpg" if exts <= {".jpg", ".jpeg"} else "png"
            out = joined_name(tweet_id, nums[0], nums[-1], ext)
            target = os.path.join(folder, out)
            if os.path.exists(target):
                done.append({"out": out, "pieces": names, "layout": layout, "existing": True})
                continue
            picture = join([paths[i] for i in idx], layout)
            tmp = os.path.join(folder, part_name(out))
            if ext == "jpg":
                picture.convert("RGB").save(tmp, "JPEG", quality=95, subsampling=0, optimize=True)
            else:
                picture.save(tmp, "PNG")
            # The post's own date, as gallery-dl gave the pieces.
            when = max(os.stat(paths[i]).st_mtime for i in idx)
            os.utime(tmp, (when, when))
            done.append({"out": out, "pieces": names, "layout": layout,
                         "size": [picture.width, picture.height]})
        return done
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {exc}"[:300]}


def lower_priority():
    """Worker initializer: the stitcher shares the machine with the server."""
    try:
        os.nice(10)
    except OSError:
        pass
