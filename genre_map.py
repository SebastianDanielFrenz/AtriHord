"""The genre map: every analysed track placed in 2-D and 3-D, so that tracks
the genre model hears alike sit together (KSF-042).

Input: the 1280-d Discogs-EffNet embedding and the styles the genre pass
stored for each track (media_indexer.store_genre). Output: genre_map.json in
the AI store's directory, which server.py's /api/media/genre-map serves.

The server runs this in the background when the map is missing or behind the
store:
    python genre_map.py <base_dir>

The layout is t-SNE, written out in numpy because the app's venv has nothing
else (no scikit-learn, no scipy). Exact gradients take O(n^2) memory, so up to
EXACT_MAX tracks are laid out directly; past that a sample of EXACT_MAX is,
and every other track goes where its nearest laid-out neighbours are.

A rebuild starts from the previous map: a track already on it starts where it
was, a new one beside its nearest old neighbours, and no early exaggeration
runs. The map then keeps its shape and orientation as the library grows,
instead of reshuffling every time a sweep adds a few songs.
"""
from __future__ import annotations

import json
import os
import sys
import time

import media_ai

MAP_NAME = "genre_map.json"
VERSION = 1
EXACT_MAX = 6000          # dense n x n float32 matrices: ~140 MB each at 6000
PCA_DIMS = 50
PERPLEXITY = 30.0
ITERATIONS = 1000         # of which EXAGGERATION_ITERS with the clusters pulled together
EXAGGERATION = 12.0
EXAGGERATION_ITERS = 250
WARM_ITERATIONS = 400     # a rebuild from the previous map
WARM_SHARE = 0.5          # ... when at least this share of tracks was on it
NEIGHBOURS = 8            # where a new or unsampled track is put
TOP_STYLES = 4            # styles per track in the map


def map_path(base_dir: str) -> str:
    return os.path.join(media_ai.store_dir(base_dir), MAP_NAME)


def load_map(base_dir: str):
    try:
        with open(map_path(base_dir), encoding="utf-8") as fh:
            data = json.load(fh)
        return data if data.get("version") == VERSION else None
    except (OSError, ValueError):
        return None


def parent_of(style: str) -> str:
    """'Electronic---Techno' -> 'Electronic'."""
    return style.split("---", 1)[0]


def _pca(X, k):
    """The first k principal components of the rows of X (n x d, d small
    enough for its covariance: 1280 here)."""
    import numpy as np
    X = X - X.mean(0)
    w, V = np.linalg.eigh((X.T @ X) / max(1, len(X) - 1))
    return (X @ V[:, ::-1][:, :min(k, X.shape[1])]).astype(np.float32)


def _sq_dists(A, B):
    import numpy as np
    d = (A * A).sum(1)[:, None] + (B * B).sum(1)[None, :] - 2 * (A @ B.T)
    np.maximum(d, 0, out=d)
    return d


def _joint_p(Z, perplexity):
    """t-SNE's symmetric P over the rows of Z, from each row's 3*perplexity
    nearest neighbours (as Barnes-Hut t-SNE does): dense n x n float32."""
    import numpy as np
    n = len(Z)
    k = int(min(n - 1, 3 * perplexity + 1))
    D = _sq_dists(Z, Z)
    np.fill_diagonal(D, np.inf)
    idx = np.argpartition(D, k - 1, axis=1)[:, :k]
    Dk = np.take_along_axis(D, idx, 1).astype(np.float64)
    del D
    Dk -= Dk.min(1, keepdims=True)          # a shift per row changes neither P nor its entropy
    target = np.log(perplexity)
    beta = np.ones(n)
    lo = np.zeros(n)
    hi = np.full(n, np.inf)
    for _ in range(100):
        W = np.exp(-Dk * beta[:, None])
        S = W.sum(1)
        H = np.log(S) + beta * (Dk * W).sum(1) / S
        diff = H - target
        if np.abs(diff).max() < 1e-5:
            break
        flat = diff > 0                      # too flat: sharpen (a larger beta)
        lo = np.where(flat, beta, lo)
        hi = np.where(flat, hi, beta)
        beta = np.where(flat, np.where(np.isinf(hi), beta * 2, (beta + hi) / 2), (beta + lo) / 2)
    W = np.exp(-Dk * beta[:, None])
    W /= W.sum(1, keepdims=True)
    P = np.zeros((n, n), dtype=np.float32)
    np.put_along_axis(P, idx, W.astype(np.float32), 1)
    P += P.T
    P /= P.sum()
    np.maximum(P, 1e-12, out=P)
    return P


def _descend(P, Y, iterations, exaggeration, exaggeration_iters, learning_rate):
    """Exact-gradient t-SNE: momentum, and per-coordinate gains (delta-bar-delta),
    with the defaults scikit-learn uses."""
    import numpy as np
    Y = Y.astype(np.float32)
    update = np.zeros_like(Y)
    gains = np.ones_like(Y)
    for it in range(iterations):
        early = it < exaggeration_iters
        momentum = 0.5 if early else 0.8
        s = (Y * Y).sum(1)
        num = Y @ Y.T
        num *= -2
        num += s[:, None]
        num += s[None, :]
        num += 1
        np.reciprocal(num, out=num)           # the Student-t kernel
        np.fill_diagonal(num, 0)
        total = num.sum()
        pq = P * (exaggeration if early else 1.0)
        pq -= num / total
        pq *= num
        grad = 4 * (pq.sum(1)[:, None] * Y - pq @ Y)
        del pq, num
        same = (update * grad) < 0
        gains = np.where(same, gains + 0.2, gains * 0.8)
        np.maximum(gains, 0.01, out=gains)
        update = momentum * update - learning_rate * gains * grad
        Y += update
    return Y


def _place(Z_new, Z_known, Y_known, k=NEIGHBOURS, chunk=2000):
    """Positions for rows of Z_new: the mean position of their k nearest rows
    of Z_known, weighted by closeness."""
    import numpy as np
    out = np.zeros((len(Z_new), Y_known.shape[1]), dtype=np.float32)
    k = min(k, len(Z_known))
    for a in range(0, len(Z_new), chunk):
        D = _sq_dists(Z_new[a:a + chunk], Z_known)
        idx = np.argpartition(D, k - 1, axis=1)[:, :k]
        d = np.take_along_axis(D, idx, 1)
        w = 1.0 / (np.sqrt(d) + 1e-6)
        w /= w.sum(1, keepdims=True)
        out[a:a + chunk] = (Y_known[idx] * w[..., None]).sum(1)
    return out


def layout(Z, dims, warm=None, seed=0):
    """(raw coordinates n x dims, how it was laid out). warm: {row: previous
    raw position} for the rows that were on the previous map."""
    import numpy as np
    n = len(Z)
    rng = np.random.default_rng(seed)
    if n < 4:
        Y = np.zeros((n, dims), dtype=np.float32)
        Y[:, :min(dims, Z.shape[1])] = Z[:, :dims]
        return Y, "too few tracks for t-SNE"
    sample = np.arange(n) if n <= EXACT_MAX else np.sort(rng.choice(n, EXACT_MAX, replace=False))
    Zs = Z[sample]
    perplexity = min(PERPLEXITY, (len(Zs) - 1) / 3.0)
    warm = warm or {}
    known = np.array([i for i in sample if i in warm], dtype=int)
    rate = max(len(Zs) / EXAGGERATION / 4, 50.0)
    if len(known) >= WARM_SHARE * len(sample):
        pos = {int(i): j for j, i in enumerate(sample)}
        Y0 = np.zeros((len(sample), dims), dtype=np.float32)
        Yk = np.array([warm[int(i)] for i in known], dtype=np.float32)
        Y0[[pos[int(i)] for i in known]] = Yk
        new = np.array([j for j, i in enumerate(sample) if int(i) not in warm], dtype=int)
        if len(new):
            Y0[new] = _place(Zs[new], Z[known], Yk)
            Y0[new] += rng.normal(0, 1e-2, (len(new), dims)).astype(np.float32)
        Ys = _descend(_joint_p(Zs, perplexity), Y0, WARM_ITERATIONS, 1.0, 0, rate)
        how = f"t-SNE from the previous map ({len(known)} of {len(sample)} tracks were on it)"
    else:
        # PCA start, scaled the way scikit-learn does: deterministic, and it
        # keeps the big picture (which clusters neighbour which) that a random
        # start leaves to chance.
        Y0 = Zs[:, :dims] / (np.std(Zs[:, 0]) or 1.0) * 1e-4
        Ys = _descend(_joint_p(Zs, perplexity), Y0, ITERATIONS, EXAGGERATION, EXAGGERATION_ITERS, rate)
        how = f"t-SNE, perplexity {perplexity:.0f}"
    if len(sample) == n:
        return Ys, how
    Y = np.zeros((n, dims), dtype=np.float32)
    Y[sample] = Ys
    rest = np.setdiff1d(np.arange(n), sample)
    Y[rest] = _place(Z[rest], Zs, Ys)
    return Y, how + f" on {len(sample)} of {n}; the rest beside their nearest neighbours"


def normalise(Y):
    """Centred and scaled into about [-1, 1]; a few outliers may land just
    outside, which is better than squeezing everything else to fit them."""
    import numpy as np
    if not len(Y):
        return Y
    Y = Y - np.median(Y, 0)
    scale = float(np.percentile(np.abs(Y), 99.5)) or 1.0
    return Y / scale


def build(base_dir: str, previous=None):
    """The map as a dict (what genre_map.json holds)."""
    import numpy as np
    started = time.time()
    conn = media_ai.connect(base_dir)
    rows = media_ai.audio_embeddings(conn)
    styles_of = {}
    for mid, tag, p in conn.execute(
            "SELECT media_id, tag, p FROM media_tags WHERE category = 'genre'"):
        styles_of.setdefault(mid, []).append((tag, p or 0.0))
    # Not closed: media_ai.connect hands out one connection per thread.
    sizes = dict(conn.execute("SELECT id, size FROM media WHERE kind = 'audio'").fetchall())

    ids = [r[0] for r in rows]
    if rows:
        X = np.stack([r[3] for r in rows]).astype(np.float32)
        X /= np.linalg.norm(X, axis=1, keepdims=True) + 1e-9
        Z = _pca(X, PCA_DIMS)
    else:
        Z = np.zeros((0, PCA_DIMS), dtype=np.float32)

    old = {}
    if previous:
        for t in previous.get("tracks", []):
            old[t["id"]] = t
    row_of = {mid: i for i, mid in enumerate(ids)}
    warm2 = {row_of[m]: t["r2"] for m, t in old.items() if m in row_of and t.get("r2")}
    warm3 = {row_of[m]: t["r3"] for m, t in old.items() if m in row_of and t.get("r3")}
    raw2, how2 = layout(Z, 2, warm2)
    raw3, how3 = layout(Z, 3, warm3)
    p2, p3 = normalise(raw2), normalise(raw3)

    style_index = {}
    parent_index = {}
    tracks = []
    for i, (mid, root, path, _vec) in enumerate(rows):
        styles = sorted(styles_of.get(mid, []), key=lambda s: -s[1])
        # The parent genre with the most weight across the track's styles, not
        # just the top style's: "Electronic" twice at 0.3 beats "Rock" at 0.4.
        weight = {}
        for tag, p in styles:
            weight[parent_of(tag)] = weight.get(parent_of(tag), 0.0) + p
        parent = max(weight, key=weight.get) if weight else "Unknown"
        tracks.append({
            "id": mid, "root": root, "path": path, "size": sizes.get(mid) or 0,
            "g": parent_index.setdefault(parent, len(parent_index)),
            "s": [[style_index.setdefault(tag, len(style_index)), round(float(p), 3)]
                  for tag, p in styles[:TOP_STYLES]],
            "p2": [round(float(v), 4) for v in p2[i]],
            "p3": [round(float(v), 4) for v in p3[i]],
            "r2": [round(float(v), 3) for v in raw2[i]],
            "r3": [round(float(v), 3) for v in raw3[i]],
        })
    return {
        "version": VERSION,
        "model": "discogs-effnet-bsdynamic-1",
        "built": time.time(),
        "seconds": round(time.time() - started, 1),
        "count": len(tracks),
        "method": {"2d": how2, "3d": how3},
        "parents": list(parent_index),
        "styles": list(style_index),
        "tracks": tracks,
    }


def write_map(base_dir: str, data) -> str:
    path = map_path(base_dir)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, separators=(",", ":"))
    os.replace(tmp, path)
    return path


def main(argv):
    base_dir = os.path.abspath(argv[1] if len(argv) > 1 else os.path.dirname(__file__))
    try:
        os.nice(10)                          # a background job beside the web server
    except (AttributeError, OSError):
        pass
    data = build(base_dir, load_map(base_dir))
    path = write_map(base_dir, data)
    print(f"[genre-map] {data['count']} tracks in {data['seconds']} s -> {path}; "
          f"2-D: {data['method']['2d']}; 3-D: {data['method']['3d']}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
