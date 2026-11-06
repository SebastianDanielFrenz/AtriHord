"""Grouping detected faces into people, and checking whether it worked.

Clustering here is agglomerative-by-threshold over cosine similarity, done
greedily against cluster centroids. Not DBSCAN or a full O(n^2) linkage: this
has to run over hundreds of thousands of faces on a box with 11 GB of RAM, and
it has to be re-runnable as the index grows without rebuilding the world.

The interesting part is not the clustering, it is the CHECK.

Immich cannot tell whether a cluster is right. We can, for most of the
library: a face that comes from `twitter/@someone/` almost certainly belongs
to that account, because the account is one person posting pictures of
themselves. So for any cluster drawn mainly from twitter, the share of its
faces coming from the single most common profile is a free correctness
signal - high purity means the threshold is doing its job, low purity means
two people have been merged. That is measured, not assumed, and it is how the
threshold below was chosen rather than copied from a tutorial.

Cosplay makes this harder than ordinary face grouping - wigs, contacts and
heavy makeup move a face measurably - so the threshold is deliberately tight.
Splitting one person into two clusters is a nuisance a human fixes with the
merge button; merging two people is a wrong answer nobody notices.
"""
from __future__ import annotations

import collections
import math
import re
import time

import numpy as np

import media_ai

# Cosine similarity, on normalised ArcFace embeddings. 0.5 is the usual
# starting point for buffalo_l; measure_purity() is what says whether it is
# right for THIS library.
DEFAULT_THRESHOLD = 0.5

# A cluster below this is not a person yet, it is a coincidence. Shown in the
# UI only once it has this many faces, so the grid is not thousands of
# one-shot strangers.
MIN_CLUSTER_SIZE = 3

_PROFILE = re.compile(r"^twitter/(@[^/]+)/")


def profile_of(path: str):
    """The twitter account a file came from, or None. This is the ground truth
    the purity check leans on - and the reason it only applies to twitter."""
    m = _PROFILE.match(path or "")
    return m.group(1) if m else None


# Tags that say a picture is a photograph - the same set the review rules use,
# calibrated against pixiv where all four sit at 0.0 percent.
PHOTO_TAGS = ("realistic", "photo_background", "lips", "depth_of_field")


def load_faces(conn, limit=None, photographic_only=True):
    """(face_id, media_id, path, embedding) for every face, best first.

    Ordered by quality so the greedy pass sees the clearest examples first:
    a centroid seeded from a sharp face attracts the right neighbours, one
    seeded from a blurred profile shot attracts anything.

    photographic_only drops faces on files the tagger judged NOT photographic.
    ArcFace was trained on photographs and embeds one illustration close to
    every other, so drawn faces do two kinds of damage: they form enormous
    false groups (cluster 39 was 781 drawn files of 941, across 142 accounts),
    and they BRIDGE real people, because greedy merging chains through them -
    cluster 0 spanned 258 accounts with 1076 photographic and 696 drawn files.
    Neither is fixable by threshold: purity and completeness were swept
    together and the harmonic mean peaks at 0.45, so a threshold high enough to
    separate drawings shatters real people instead.

    A face on a file with NO tags yet is kept. Absence of evidence is not
    evidence - the same rule the People grid follows when it shows an unjudged
    cluster rather than hiding it.
    """
    holes = ",".join("?" * len(PHOTO_TAGS))
    sql = ("SELECT f.id, f.media_id, m.path, f.embedding, f.quality"
           " FROM faces f JOIN media m ON m.id = f.media_id"
           " WHERE f.embedding IS NOT NULL")
    args = []
    if photographic_only:
        sql += (" AND (m.tags IS NULL OR m.tags = '' OR EXISTS ("
                "   SELECT 1 FROM media_tags t WHERE t.media_id = m.id"
                f"    AND t.tag IN ({holes}) AND t.p >= 0.5))")
        args = list(PHOTO_TAGS)
    sql += " ORDER BY f.quality DESC"
    if limit:
        sql += " LIMIT %d" % int(limit)
    rows = conn.execute(sql, args).fetchall()
    ids, media_ids, paths, embs = [], [], [], []
    for r in rows:
        vec = np.frombuffer(r["embedding"], dtype=np.float32)
        if vec.size != 512:
            continue
        ids.append(r["id"])
        media_ids.append(r["media_id"])
        paths.append(r["path"])
        embs.append(vec)
    if not embs:
        return [], [], [], np.zeros((0, 512), dtype=np.float32)
    return ids, media_ids, paths, np.vstack(embs)


def cluster(embs: np.ndarray, threshold=DEFAULT_THRESHOLD, batch=2048):
    """Greedy assignment to running centroids. Returns a label per row.

    Compared against every centroid at once in blocks: the centroid matrix is
    (clusters x 512), so one matmul per block of faces rather than a Python
    loop per pair. That is what keeps this minutes rather than days.
    """
    n = embs.shape[0]
    labels = np.full(n, -1, dtype=np.int64)
    if n == 0:
        return labels
    centroids = np.zeros((0, 512), dtype=np.float32)
    counts = []
    for start in range(0, n, batch):
        chunk = embs[start:start + batch]
        for i, vec in enumerate(chunk):
            idx = start + i
            if centroids.shape[0]:
                sims = centroids @ vec
                best = int(np.argmax(sims))
                if sims[best] >= threshold:
                    labels[idx] = best
                    # Running mean, renormalised: an un-normalised centroid
                    # drifts in magnitude and the cosine threshold stops
                    # meaning what it meant.
                    counts[best] += 1
                    c = centroids[best] + (vec - centroids[best]) / counts[best]
                    centroids[best] = c / (np.linalg.norm(c) or 1.0)
                    continue
            centroids = np.vstack([centroids, vec[None, :]])
            counts.append(1)
            labels[idx] = centroids.shape[0] - 1
    return labels


# Clustering happens in two steps with two different thresholds. See
# cluster_by_account for the measurement that chose them.
WITHIN_ACCOUNT_THRESHOLD = 0.35
ACROSS_ACCOUNT_THRESHOLD = 0.85


def _greedy_rows(vectors, threshold):
    """(label per row, centroids): greedy assignment to running centroids.

    The rule the two loops below used to apply, with each row compared against
    EVERY centroid in one matrix product instead of a Python loop per centroid.
    The loops were fine at 15k faces; a full face pass produces hundreds of
    thousands, where the cross-account merge alone would have run for days
    inside the indexer. Centroids live in a preallocated matrix that doubles,
    because stacking a row at a time copies the whole matrix every time.
    A tie or a value sitting exactly on the threshold can come out differently
    in the last floating-point digit than the old per-pair dot products, so
    this is equivalent rather than bit-identical.
    """
    n, dim = vectors.shape
    labels = np.empty(n, dtype=np.int64)
    cap = 64
    cents = np.empty((cap, dim), dtype=np.float32)
    counts = []
    k = 0
    for i in range(n):
        v = vectors[i]
        if k:
            sims = cents[:k] @ v
            bi = int(np.argmax(sims))
            if sims[bi] >= threshold:
                counts[bi] += 1
                c = cents[bi] + (v - cents[bi]) / counts[bi]
                cents[bi] = c / np.linalg.norm(c)
                labels[i] = bi
                continue
        if k == cap:
            cap *= 2
            grown = np.empty((cap, dim), dtype=np.float32)
            grown[:k] = cents[:k]
            cents = grown
        cents[k] = v
        counts.append(1)
        labels[i] = k
        k += 1
    return labels, cents[:k]


def _greedy(embs, index, threshold):
    """Greedy centroid pass over a subset. Returns (groups, centroids)."""
    index = list(index)
    if not index:
        return [], []
    labels, cents = _greedy_rows(embs[index], threshold)
    groups = [[] for _ in range(len(cents))]
    for pos, g in zip(index, labels.tolist()):
        groups[g].append(pos)
    return groups, list(cents)


def cluster_by_account(paths, embs, within=WITHIN_ACCOUNT_THRESHOLD,
                       across=ACROSS_ACCOUNT_THRESHOLD, verify=None):
    """Cluster inside each account first, then merge across accounts strictly.

    WHY NOT ONE GLOBAL PASS. Measured on this library at 9040 faces, a single
    pass at 0.45 gives FACE-WEIGHTED purity 0.482 - more than half of all
    clustered face pairs come from different accounts - and leaves one cluster
    holding 1627 faces, 18 percent of everything, spanning 234 accounts.
    Raising the global threshold trades that away for fragmentation: 0.65 gets
    purity to 0.880 but completeness falls from 0.618 to 0.394.

    The reason no threshold works is that cosplay defeats the embedding. Mean
    pairwise similarity inside a CORRECTLY named cluster measured 0.40 to 0.53,
    while the 234-account blob measured 0.378 - the distributions overlap,
    because a wig, contacts and heavy makeup move a face further apart than two
    different people wearing similar makeup do.

    So use the signal that is not in the embedding: one account is
    overwhelmingly one person. Inside an account be generous (0.35), since two
    faces there are probably the same person in different costumes. Across
    accounts be strict (0.85), because that is where every false merge came
    from. Measured, this scores weighted purity 0.985 and completeness 0.816,
    against 0.482 and 0.618 for the global pass, and the largest cluster drops
    from 1627 faces to 126.

    ALTERNATIVES. (a) Global threshold tuning - swept, best harmonic 0.615 at
    0.55, still leaves an 884-face blob. (b) Never merge across accounts -
    purity 1.0 by construction, which makes the number meaningless, and it
    yields 378 groups against 446, so it loses 68 real cross-account people for
    no measurable gain. (c) A different algorithm such as HDBSCAN - not tried,
    because the account prior is a stronger signal than any better use of the
    same weak embedding, and it is explainable.

    WHAT THIS COSTS. A person who appears under two accounts is merged only if
    the two centroids clear 0.85, so some real people will still appear twice.
    That is the deliberate direction of the error: two tiles for one person is a
    merge away in the UI, while 234 people in one tile is not repairable by
    hand.

    verify: a face-link threshold. Every merged group spanning two or more
    accounts is then re-decided face by face (verify_across_accounts) - the
    averages compared here stop meaning one person once enough made-up faces
    are averaged. recluster passes FACE_LINK_THRESHOLD; None reproduces the
    centroid-only grouping.
    """
    by_account = {}
    for i, path in enumerate(paths):
        who = profile_of(path) or ("", i)     # no account: its own bucket
        by_account.setdefault(who, []).append(i)

    local, cents = [], []
    for index in by_account.values():
        groups, centroids = _greedy(embs, index, within)
        local.extend(groups)
        cents.extend(centroids)

    labels = np.full(len(paths), -1, dtype=np.int64)
    if not local:
        return labels

    merge_labels, _ = _greedy_rows(np.asarray(cents, dtype=np.float32), across)
    if verify:
        merge_labels = verify_across_accounts(paths, embs, local, merge_labels, verify)
    for gi, final_id in enumerate(merge_labels.tolist()):
        for i in local[gi]:
            labels[i] = final_id
    return labels


# The cross-account merge above compares AVERAGES, and the average of one
# person's faces under many wigs, lenses and heavy makeup drifts towards a
# generic "cosplay face" - so two such averages clear even a strict 0.85 while
# the people are different. On 2026-09-18 that left one face holding 7,145
# files from 40 different cosplayers (vault KSF-039). So every face that spans
# two or more accounts is checked again, face by face (user: "fix the grouping
# so accounts compare face by face").
#
# The link between two groups of faces: for each face in one, its best match
# in the other; the median of those; the lower of the two directions. Taken
# both ways because a photographer's pile of many people matches each of them
# in one direction only - so it cannot bridge two cosplayers any more.
# Measured on pairs judged by eye (KSF-039): the same person across two
# accounts 0.631-0.725, different cosplayers merged into that one face
# 0.519-0.621. Nothing else tried separated better (neighbour mixing, the
# 90th percentile, the sharpest faces only).
FACE_LINK_THRESHOLD = 0.63
FACE_LINK_SAMPLE = 150      # faces per side, sharpest first: 150 x 150 per link
FACE_LINK_MIN_UNIT = 30     # smaller groups are judged one way only (see below)


def _median_best_match(a, b):
    return float(np.median((a @ b.T).max(axis=1)))


def face_link(embs, p, q, sample=FACE_LINK_SAMPLE):
    """Face-by-face link between two lists of row indices (sharpest first)."""
    a, b = embs[p[:sample]], embs[q[:sample]]
    return min(_median_best_match(a, b), _median_best_match(b, a))


def verify_across_accounts(paths, embs, local, merge_labels, threshold=FACE_LINK_THRESHOLD,
                           sample=FACE_LINK_SAMPLE, min_unit=FACE_LINK_MIN_UNIT):
    """merge_labels re-decided face by face for every merged group that spans
    two or more accounts; returns a new label per local group.

    Inside such a group, the local groups of at least min_unit faces are
    joined best link first (average-style, so the order of processing does
    not matter and one strong link cannot pull in a weak third) while the
    best link clears the threshold. A smaller group is judged ONE way - its
    faces' best matches in the bigger one - because a best match found among
    a handful of faces is lower by chance and would split it off unfairly; it
    joins the component it matches best if that clears the threshold, and
    otherwise stands alone. A merged group whose units never clear the
    threshold becomes several faces; the biggest keeps the label.
    """
    merge_labels = np.asarray(merge_labels).copy()
    members = collections.defaultdict(list)
    for gi, lab in enumerate(merge_labels.tolist()):
        members[lab].append(gi)
    next_label = int(merge_labels.max()) + 1 if len(merge_labels) else 0
    for lab, gis in members.items():
        accounts = {profile_of(paths[local[g][0]]) for g in gis} - {None}
        if len(accounts) < 2:
            continue
        big = [g for g in gis if len(local[g]) >= min_unit]
        if not big:
            continue
        # Components: (local group ids, row indices sharpest first).
        comps = [([g], sorted(local[g])) for g in big]
        links = {}
        for i in range(len(comps)):
            for j in range(i + 1, len(comps)):
                links[(i, j)] = face_link(embs, comps[i][1], comps[j][1], sample)
        alive = set(range(len(comps)))
        while len(alive) > 1:
            (i, j), best = max(((k, v) for k, v in links.items()), key=lambda kv: kv[1])
            if best < threshold:
                break
            comps[i] = (comps[i][0] + comps[j][0], sorted(comps[i][1] + comps[j][1]))
            alive.discard(j)
            links = {k: v for k, v in links.items() if i not in k and j not in k}
            for o in alive:
                if o != i:
                    links[(min(i, o), max(i, o))] = face_link(embs, comps[i][1], comps[o][1], sample)
        comps = [comps[k] for k in sorted(alive)]
        for g in gis:
            if len(local[g]) >= min_unit:
                continue
            rows = sorted(local[g])[:sample]
            scores = [_median_best_match(embs[rows], embs[c[1][:sample]]) for c in comps]
            best = int(np.argmax(scores))
            if scores[best] >= threshold:
                comps[best] = (comps[best][0] + [g], comps[best][1])
            else:
                comps.append(([g], rows))
        comps.sort(key=lambda c: -sum(len(local[g]) for g in c[0]))
        for n, (groups, _rows) in enumerate(comps):
            new = lab if n == 0 else next_label
            if n:
                next_label += 1
            for g in groups:
                merge_labels[g] = new
    return merge_labels


def measure_purity(paths, labels, min_size=MIN_CLUSTER_SIZE):
    """How often a cluster's twitter faces all come from one profile.

    Only clusters that are MOSTLY twitter are judged: a cluster of pixiv
    drawings has no profile to be pure about, and counting it either way would
    make the number meaningless.
    """
    by_cluster = collections.defaultdict(list)
    for path, label in zip(paths, labels):
        if label >= 0:
            by_cluster[int(label)].append(profile_of(path))
    scored, purities = 0, []
    for members in by_cluster.values():
        known = [p for p in members if p]
        if len(members) < min_size or len(known) < max(2, len(members) // 2):
            continue
        top = collections.Counter(known).most_common(1)[0][1]
        purities.append(top / len(known))
        scored += 1
    if not scored:
        return {"clusters_scored": 0}
    arr = np.array(purities)
    return {
        "clusters_scored": scored,
        "mean_purity": round(float(arr.mean()), 4),
        "median_purity": round(float(np.median(arr)), 4),
        # The number that matters: a cluster below 0.8 is probably two people.
        "clean_share": round(float((arr >= 0.8).mean()), 4),
        "suspect": int((arr < 0.8).sum()),
    }


def sweep_thresholds(conn, values=(0.35, 0.4, 0.45, 0.5, 0.55, 0.6, 0.65),
                     limit=None):
    """Purity and cluster count at each threshold, so the choice is made from
    this library rather than from a default."""
    ids, media_ids, paths, embs = load_faces(conn, limit=limit)
    out = []
    for t in values:
        labels = cluster(embs, threshold=t)
        stats = measure_purity(paths, labels)
        sizes = collections.Counter(labels[labels >= 0].tolist())
        out.append({
            "threshold": t,
            "clusters": len(sizes),
            "clusters_ge_min": sum(1 for v in sizes.values() if v >= MIN_CLUSTER_SIZE),
            "faces": int((labels >= 0).sum()),
            **stats,
        })
    return out


def persist(conn, ids, labels, paths):
    """Writes cluster ids back, and one clusters row per group.

    Existing NAMES are preserved by matching on the cluster's cover face:
    re-clustering renumbers everything, and losing the names a person typed
    because the numbering changed would make naming pointless.
    """
    names = {}
    for row in conn.execute(
            "SELECT id, name, cover_face FROM clusters WHERE name IS NOT NULL"):
        if row["cover_face"] is not None:
            names[row["cover_face"]] = row["name"]

    by_label = collections.defaultdict(list)
    for face_id, label in zip(ids, labels):
        if label >= 0:
            by_label[int(label)].append(face_id)

    conn.execute("DELETE FROM clusters")
    conn.execute("UPDATE faces SET cluster_id = NULL")
    now = time.time()
    for label, face_ids in by_label.items():
        cover = face_ids[0]          # load_faces ordered by quality
        name = next((names[f] for f in face_ids if f in names), None)
        conn.execute(
            "INSERT INTO clusters (id, name, cover_face, face_count, updated_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (label, name, cover, len(face_ids), now))
        conn.executemany("UPDATE faces SET cluster_id=? WHERE id=?",
                         [(label, f) for f in face_ids])
    conn.commit()
    return len(by_label)


def weighted_purity(paths, labels):
    """Purity weighted by FACES, not averaged over clusters.

    The unweighted mean was actively misleading here: one blob of 1627 faces
    spanning 234 accounts counted the same as one clean pair, so it reported
    0.834 for a clustering whose real figure was 0.482. Any metric used to
    choose between clusterings has to be weighted, or it rewards exactly the
    failure it exists to detect.
    """
    members = {}
    for i, l in enumerate(labels.tolist() if hasattr(labels, "tolist") else labels):
        if l >= 0:
            members.setdefault(l, []).append(i)
    top_total = seen_total = 0
    for idx in members.values():
        accounts = [profile_of(paths[i]) for i in idx]
        accounts = [a for a in accounts if a]
        if len(accounts) < 2:
            continue
        top_total += collections.Counter(accounts).most_common(1)[0][1]
        seen_total += len(accounts)
    biggest = max((len(v) for v in members.values()), default=0)
    return {
        "weighted_purity": round(top_total / seen_total, 4) if seen_total else None,
        "biggest_cluster": biggest,
        "biggest_share": round(biggest / max(1, sum(len(v) for v in members.values())), 4),
    }


def recluster(base_dir, threshold=None, limit=None, by_account=True):
    """Re-cluster every face and write the groups back.

    by_account uses the two-step account prior, which measured weighted purity
    0.985 against 0.482 for the single global pass it replaced. Pass
    by_account=False with a threshold to reproduce the old behaviour.
    """
    conn = media_ai.connect(base_dir)
    ids, media_ids, paths, embs = load_faces(conn, limit=limit)
    if not ids:
        return {"faces": 0, "clusters": 0}
    if by_account:
        norm = embs / np.linalg.norm(embs, axis=1, keepdims=True)
        labels = cluster_by_account(paths, norm, verify=FACE_LINK_THRESHOLD)
        used = "account prior %.2f/%.2f, face link %.2f" % (
            WITHIN_ACCOUNT_THRESHOLD, ACROSS_ACCOUNT_THRESHOLD, FACE_LINK_THRESHOLD)
    else:
        labels = cluster(embs, threshold=threshold or DEFAULT_THRESHOLD)
        used = "global %.2f" % (threshold or DEFAULT_THRESHOLD)
    n = persist(conn, ids, labels, paths)
    return {"faces": len(ids), "clusters": n, "method": used,
            **weighted_purity(paths, labels), **measure_purity(paths, labels)}

# An account is tied to its MAIN FACE (user, 2026-09-18) when it has at least
# MAIN_FACE_FILES files with a face and enough of them show that face - alone
# or beside others. Every account tied to a face names it.
#
# "Enough" falls as the account grows (user: "Bigger accounts should lower the
# threshold to a point"): MAIN_FACE_SHARE at MAIN_FACE_FILES files, down to
# MAIN_FACE_SHARE_FLOOR at MAIN_FACE_FLOOR_FILES and beyond, linear in the
# logarithm of the size - doubling an account buys the same step anywhere on
# the way. Measured 2026-09-18: small accounts are noisy (15-50 face files:
# median share 0.88, a third under 75%), big ones are clearly one person or
# clearly not (400-800: median 0.96). The floor is 60% because what a lower
# one adds is mostly wrong - photo studios and 3D-render accounts at 55-60%
# landing in faces that merge many people - while 60-75% adds cosplayers
# with friends in their pictures (@guri_cos_8888 67%, @kiku1569 74.9%).
MAIN_FACE_FILES = 15
MAIN_FACE_SHARE = 0.75
MAIN_FACE_SHARE_FLOOR = 0.60
MAIN_FACE_FLOOR_FILES = 300


def main_face_share_needed(files, top=MAIN_FACE_SHARE, floor=MAIN_FACE_SHARE_FLOOR,
                           start=MAIN_FACE_FILES, at=MAIN_FACE_FLOOR_FILES):
    """The share of its face files an account's main face must reach."""
    if files <= start:
        return top
    step = min(1.0, math.log(files / start) / math.log(at / start))
    return top - (top - floor) * step


def account_main_faces(conn, min_files=MAIN_FACE_FILES, needed=main_face_share_needed, roots=None):
    """({handle: {cluster, files, shows}}, per-cluster file counts).

    Read from the account's side, where the old naming read the cluster's:
    "is at least 75% of this face from one account" named a face only when a
    single account dominated it, so a person posting from two accounts, or
    shot by three photographers, stayed unnamed however clear each account
    was. Asked per account instead - "is this account one person?" - every
    account that is gets to name its face, and a face can carry several.

    Counted over distinct FILES, not faces: a video contributes a face per
    sampled frame, and a clip must count once. A file counts for every face
    in it: other people in the picture do not stop it counting for the
    account's own face (user, 2026-09-18). Counting only pictures that show
    the face ALONE, as first built, left 42 clear accounts under the line -
    @2ponlove's face is in 94% of its pictures but alone in 74.7%, because a
    fifth of them also show a photographer, a friend or a reflection. Files
    whose only faces were never clustered (drawings: load_faces keeps them
    out) are not counted at all - a drawn face is not a person the account
    could be about.

    `needed(files)` is the share the main face must reach
    (main_face_share_needed: it falls as the account grows).

    The second value, {cluster_id: {"total": files, handle: files}}, is what
    auto_names needs for the share of a face its accounts explain.
    """
    per_file = {}
    for media_id, cluster_id, path, root in conn.execute(
            "SELECT f.media_id, f.cluster_id, m.path, m.root FROM faces f"
            " JOIN media m ON m.id = f.media_id"
            " WHERE f.cluster_id IS NOT NULL AND m.trashed IS NULL"):
        if roots is not None: roots.setdefault(cluster_id, set()).add(root)
        entry = per_file.get(media_id)
        if entry is None:
            entry = per_file[media_id] = (profile_of(path), set())
        entry[1].add(cluster_id)

    by_account = {}
    cluster_files = collections.defaultdict(collections.Counter)
    for profile, clusters in per_file.values():
        for cid in clusters:
            cluster_files[cid]["total"] += 1
            if profile:
                cluster_files[cid][profile] += 1
        if not profile:
            continue
        counts = by_account.setdefault(profile, [0, collections.Counter()])
        counts[0] += 1
        counts[1].update(clusters)

    tied = {}
    for handle, (files, shows) in by_account.items():
        if files < min_files or not shows:
            continue
        cid, n = shows.most_common(1)[0]
        if n / files >= needed(files):
            tied[handle] = {"cluster": cid, "files": files, "shows": n}
    return tied, cluster_files


def auto_names(conn, min_files=MAIN_FACE_FILES, needed=main_face_share_needed, roots=None):
    """{cluster_id: {name, handles, accounts, share}} for every face that is
    some account's main face (account_main_faces).

    `name` lists every such account, the one with most pictures of the face
    first. `share` is how much of the face's files those accounts hold -
    shown beside the guess, so a face tied to one account but mostly made of
    other people's pictures does not read as settled.

    Deliberately NOT stored. A name written into the table would have to be
    told apart from one the user typed, and would go stale as the sweep finds
    more files.
    """
    tied, cluster_files = (account_main_faces(conn, min_files, needed, roots=roots) if roots is not None
                          else account_main_faces(conn, min_files, needed))
    by_cluster = collections.defaultdict(list)
    for handle, info in tied.items():
        by_cluster[info["cluster"]].append(handle)
    names = {}
    for cid, handles in by_cluster.items():
        counts = cluster_files[cid]
        handles.sort(key=lambda h: (-counts[h], h.lower()))
        total = counts["total"] or 1
        names[cid] = {
            "name": ", ".join(handles),
            "handles": handles,
            # files: this account's pictures showing the face; account_files:
            # all of its pictures with a face.
            "accounts": [{"handle": h, "files": counts[h], "account_files": tied[h]["files"]}
                         for h in handles],
            "share": round(sum(counts[h] for h in handles) / total, 3),
            "total": counts["total"],
        }
    return names


REAL_SHARE = 0.5           # more than half of the judgeable files
REAL_MIN_JUDGED = 2        # and at least this many to judge on


def realness(conn):
    """{cluster_id: {"real": bool|None, "photo": n, "drawn": n}}.

    `real` is None when nothing in the cluster has been tagged yet - which is
    most of them today, because the face pass runs far ahead of the tag pass.
    None means UNKNOWN and unknown is shown, not hidden: a grid that quietly
    drops people because the indexer has not got to them yet is worse than a
    grid with some anime faces in it.
    """
    counts = {}
    for row in conn.execute(
            "SELECT DISTINCT f.cluster_id AS cid, m.id AS mid,"
            " (SELECT MAX(t.p) FROM media_tags t WHERE t.media_id = m.id"
            # No 'cosplay' here either - see PHOTO_MARKERS in media_review:
            # danbooru applies it to drawings of characters in other
            # characters' outfits, so it marks a costume, not a camera.
            "  AND t.tag IN ('realistic','photo_background','lips',"
            "                'depth_of_field')) AS photo,"
            " (m.tags IS NOT NULL AND m.tags != '') AS tagged"
            " FROM faces f JOIN media m ON m.id = f.media_id"
            " WHERE f.cluster_id IS NOT NULL AND m.trashed IS NULL"):
        if not row["tagged"]:
            continue
        c = counts.setdefault(row["cid"], {"photo": 0, "drawn": 0})
        if (row["photo"] or 0) >= 0.5:
            c["photo"] += 1
        else:
            c["drawn"] += 1

    out = {}
    for cid, c in counts.items():
        judged = c["photo"] + c["drawn"]
        real = (None if judged < REAL_MIN_JUDGED
                else c["photo"] / judged > REAL_SHARE)
        out[cid] = {"real": real, **c, "judged": judged}
    return out
