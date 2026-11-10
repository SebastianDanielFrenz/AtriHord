"""Same-person suggestions for the People grid. Out of the media_faces.py /
media_ai.py next to a server.py, over a temp AI store with SYNTHETIC face
embeddings (nothing here touches the live library or the vision worker):

  - the score (media_faces.link_score) and the two-stage search
    (SuggestIndex.shortlist + suggest_same_person): the same person split over
    clusters of 40, 12 and 4 faces is found from any of them, a different
    person, a look-alike and 400 strangers are not;
  - the rejection memory (record_not_same / rejected_clusters) and the index
    kept in step with a merge (SuggestIndex.on_merge);
  - the real routes /api/media/people/<id>/similar, /not_same and the merge POST,
    when Flask is importable (the media VM's venv has it).

  venv/bin/python person_similar_check.py <server.py>
"""
import ast
import os
import shutil
import sys
import tempfile
import time

BASE = os.environ.get("ATRIHORD_DIR") or os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SERVER = sys.argv[1] if len(sys.argv) > 1 else os.path.join(BASE, "server.py")
sys.path.insert(0, BASE)
sys.path.insert(0, os.path.dirname(os.path.abspath(SERVER)))
tmp = tempfile.mkdtemp(prefix="person_similar_check_")
os.environ["KSF_MEDIA_AI_DIR"] = os.path.join(tmp, "ai")
os.makedirs(os.environ["KSF_MEDIA_AI_DIR"])
import numpy as np                   # noqa: E402
import media_ai                      # noqa: E402
import media_faces                   # noqa: E402

ok = True


def check(label, cond, detail=""):
    global ok
    ok = ok and bool(cond)
    print(("PASS " if cond else "FAIL ") + label + (f"  ({detail})" if detail and not cond else ""), flush=True)


rng = np.random.default_rng(7)
NOISE = 0.025          # per dimension: two faces of one person land near cosine 0.75-0.78 (a celebrity, 0.77-0.80 in the library)


def unit(v):
    return v / np.linalg.norm(v)


def face_of(base):
    return unit(base + rng.normal(0, NOISE, 512)).astype(np.float32)


conn = media_ai.connect(tmp)
R = "downloads"
face_ids = {}          # cluster id -> [face ids] best first


def make_cluster(cid, base, n, hidden=0):
    mid = conn.execute("INSERT INTO media (root, path, size, kind) VALUES (?, ?, 1, 'image')",
                       (R, f"pixiv/{cid}/f.jpg")).lastrowid
    ids = []
    for k in range(n):
        vec = face_of(base)
        fid = conn.execute(
            "INSERT INTO faces (media_id, bbox, embedding, cluster_id, quality) VALUES (?, '[0,0,9,9]', ?, ?, ?)",
            (mid, vec.tobytes(), cid, 1.0 - k * 0.001)).lastrowid
        ids.append(fid)
    conn.execute("INSERT INTO clusters (id, name, cover_face, face_count, hidden) VALUES (?, NULL, ?, ?, ?)",
                 (cid, ids[0], n, hidden))
    face_ids[cid] = ids


base_a = unit(rng.normal(size=512))
base_b = unit(rng.normal(size=512))
# A look-alike: shares 45 % of person A's direction, so face-to-face it lands
# near cosine 0.3 - the kind of near miss the shortlist must not promote.
base_d = unit(0.45 * base_a + 0.893 * unit(rng.normal(size=512)))
make_cluster(1, base_a, 40)
make_cluster(2, base_a, 12)
make_cluster(3, base_a, 4)
make_cluster(4, base_b, 30)
make_cluster(5, base_d, 30)
make_cluster(6, base_a, 12, hidden=1)          # the same person, but hidden
for cid in range(100, 500):                    # 400 strangers
    make_cluster(cid, unit(rng.normal(size=512)), int(rng.integers(3, 6)))
conn.commit()
STRANGERS = set(range(100, 500))

# ---------------------------------------------------------------- the score --
_, a1 = media_faces.cluster_faces(conn, 1, 60)
_, a2 = media_faces.cluster_faces(conn, 2, 60)
_, a3 = media_faces.cluster_faces(conn, 3, 60)
_, b = media_faces.cluster_faces(conn, 4, 60)
_, d = media_faces.cluster_faces(conn, 5, 60)
s12, s13, s1b, s1d = (media_faces.link_score(a1, a2), media_faces.link_score(a1, a3),
                      media_faces.link_score(a1, b), media_faces.link_score(a1, d))
check("one person's two clusters score at or above the measured same-person line",
      s12 >= media_faces.SUGGEST_LIKELY and s13 >= media_faces.SUGGEST_LIKELY, (s12, s13))
check("a different person and a look-alike score under the offer floor",
      s1b < media_faces.SUGGEST_FLOOR and s1d < media_faces.SUGGEST_FLOOR, (s1b, s1d))
check("a 4-face cluster is judged one way (its best matches in the big one), not both",
      abs(s13 - media_faces._median_best_match(a3, a1)) < 1e-9, s13)
check("... and the reverse direction alone would score it lower, which is why",
      media_faces._median_best_match(a1, a3) < s13, (media_faces._median_best_match(a1, a3), s13))
check("an empty side scores 0", media_faces.link_score(a1, a1[:0]) == 0.0)
# The green line is set from what the owner saw (2026-11-10): correct suggestions
# for famous western faces 0.77-0.80, for Jenna Ortega 0.87 down to 0.70, wrong
# ones for cosplayers 0.62-0.71 (best 0.68 / 0.71). The plain line must sit above
# every wrong match and the strong line may go down to the lowest correct one.
ORTEGA = [0.87, 0.83, 0.81, 0.77, 0.75, 0.74, 0.74, 0.73, 0.73] + [0.72] * 5 + [0.71] * 3 + [0.70] * 2
EMMA = [0.80, 0.80, 0.79, 0.79, 0.78, 0.78, 0.78, 0.77, 0.77, 0.77, 0.77, 0.77]
CHI_MUU = [0.68, 0.67, 0.66, 0.66, 0.65, 0.65, 0.64, 0.64, 0.63, 0.63, 0.62, 0.62]
WELLCHANNEL = [0.71, 0.71, 0.70, 0.69, 0.68, 0.67, 0.66, 0.66, 0.65, 0.64, 0.63]
check("the plain 'likely' line is above every observed wrong match (<= 0.71) and under the correct ones (>= 0.77)",
      0.71 < media_faces.SUGGEST_LIKELY <= 0.77, media_faces.SUGGEST_LIKELY)
check("... and is no longer the 0.63 auto-link threshold that marked every cosplay match green",
      media_faces.SUGGEST_LIKELY > media_faces.FACE_LINK_THRESHOLD)
check("the strong line reaches the lowest correct match seen (0.70) and is under the plain one",
      media_faces.SUGGEST_LIKELY_STRONG <= 0.70 < media_faces.SUGGEST_LIKELY, media_faces.SUGGEST_LIKELY_STRONG)
check("an anchor with many high matches (Ortega: 5 at 0.75+, Emma Watson: 12) is judged by the strong line",
      media_faces.likely_line(ORTEGA) == media_faces.SUGGEST_LIKELY_STRONG
      and media_faces.likely_line(EMMA) == media_faces.SUGGEST_LIKELY_STRONG)
check("... a cosplayer's matches (none at 0.75) keep the plain line, and none of them reaches it",
      media_faces.likely_line(CHI_MUU) == media_faces.SUGGEST_LIKELY and media_faces.likely_line(WELLCHANNEL) == media_faces.SUGGEST_LIKELY
      and not any(s >= media_faces.SUGGEST_LIKELY for s in CHI_MUU + WELLCHANNEL))
check("... under the strong line every correct Ortega match is likely, and 100 % of the cosplayers' would NOT be spared if they had a core",
      all(s >= media_faces.SUGGEST_LIKELY_STRONG for s in ORTEGA))
# The main rule: a score is judged against the anchor's own background (mean/sd of
# its exact scores against ~48 clusters of the crowd that is not it), measured live.
SPANISH = (0.240, 0.039, [0.71, 0.65, 0.60, 0.60, 0.59, 0.54, 0.54, 0.54, 0.53, 0.53, 0.52, 0.52, 0.52, 0.52, 0.51])
ORTEGA_BG = (0.112, 0.049, ORTEGA)
CHI_MUU_BG = (0.462, 0.074, [0.69, 0.67, 0.64, 0.63, 0.62, 0.61, 0.60, 0.58, 0.56])
WELL_BG = (0.482, 0.075, [0.71, 0.68, 0.67, 0.66, 0.65, 0.64, 0.63, 0.62, 0.60, 0.57, 0.55])
Z = [media_faces.significance(s, m, sd) for m, sd, ss in (SPANISH, ORTEGA_BG) for s in ss]
Zw = [media_faces.significance(s, m, sd) for m, sd, ss in (CHI_MUU_BG, WELL_BG) for s in ss]
check("measured: every correct match is >= 6.8 sd above its anchor's background, every wrong cosplay one <= 3.2",
      min(Z) >= 6.8 and max(Zw) <= 3.2, (min(Z), max(Zw)))
check("the likely line (in sd) sits in that gap, so no observed correct match is missed and no observed wrong one is green",
      max(Zw) < media_faces.SUGGEST_Z_LIKELY <= min(Z), (max(Zw), media_faces.SUGGEST_Z_LIKELY, min(Z)))
check("... the same 0.60 score is likely for the Spanish cosplayer and nowhere near for @chi_muu",
      media_faces.significance(0.60, 0.240, 0.039) >= media_faces.SUGGEST_Z_LIKELY
      and media_faces.significance(0.60, 0.462, 0.074) < media_faces.SUGGEST_Z_LIKELY)
check("counts, not one match: a few high scores alone are not enough, nor a crowd at 0.70 without a high core",
      media_faces.likely_line([0.85, 0.80, 0.76]) == media_faces.SUGGEST_LIKELY
      and media_faces.likely_line([0.85, 0.80] + [0.72] * 10) == media_faces.SUGGEST_LIKELY
      and media_faces.likely_line([0.76, 0.76, 0.76] + [0.66] * 10) == media_faces.SUGGEST_LIKELY
      and media_faces.likely_line([0.76, 0.76, 0.76] + [0.71] * 5) == media_faces.SUGGEST_LIKELY_STRONG
      and media_faces.likely_line([]) == media_faces.SUGGEST_LIKELY)
check("the observed counts fall on the right sides of the two minimums (cosplayers <= 3 at 0.70, celebrities >= 12)",
      sum(s >= 0.70 for s in CHI_MUU + WELLCHANNEL) <= 3 and sum(s >= 0.70 for s in EMMA) >= 12
      and sum(s >= 0.70 for s in CHI_MUU) < media_faces.SUGGEST_WIDE_MIN <= sum(s >= 0.70 for s in EMMA)
      and media_faces.SUGGEST_BULK_MIN >= 3 and media_faces.SUGGEST_CORE_MIN >= 3)

# ------------------------------------------------------------------- search --
index = media_faces.SuggestIndex()
t0 = time.time()
index.build(conn)
build_s = time.time() - t0
st = index.status()
check("the index holds every live cluster of 3+ faces, not the hidden one",
      st["clusters"] == 405, st)
t0 = time.time()
found = media_faces.suggest_same_person(conn, index, 1)
ask_s = time.time() - t0
ids = [c for c, _ in found]
check("the other halves of person A are suggested, best first, nothing else",
      set(ids) == {2, 3} and [s for _, s in found] == sorted((s for _, s in found), reverse=True), found)
check("the hidden duplicate of A is not offered (it is not in the index)", 6 not in ids, ids)
check("a different person, a look-alike and 400 strangers are not offered",
      not ({4, 5} & set(ids)) and not (STRANGERS & set(ids)), ids)
found3 = media_faces.suggest_same_person(conn, index, 3)
check("a 4-face cluster finds the big halves of its own person",
      {c for c, _ in found3} == {1, 2}, found3)
found_b = media_faces.suggest_same_person(conn, index, 4)
check("a person with no other half gets no suggestions", found_b == [], found_b)
# ------------------------------------------------ the line moves with the core --
plain = media_faces.suggest_same_person(conn, index, 1)
detail = media_faces.suggest_same_person(conn, index, 1, detail=True)
check("detail=True: the plain matches first (same scores and order), each with z, bg and likely (z line and score line), no cosplay field",
      [(d["id"], d["score"]) for d in detail][:len(plain)] == plain and not any("cosplay" in d for d in detail)
      and all(d["z"] is not None and d["bg"] is not None for d in detail)
      and all(d["likely"] == (d["z"] >= media_faces.SUGGEST_Z_LIKELY and d["score"] >= media_faces.SUGGEST_LIKELY_ABS_MIN)
              for d in detail), detail)
check("the cards are the ones z lets in: z >= the show line and score >= the minimum; extras beyond the plain floor are never likely",
      all(d["z"] >= media_faces.SUGGEST_Z_SHOW and d["score"] >= media_faces.SUGGEST_Z_ABS_MIN for d in detail)
      and all(not d["likely"] for d in detail[len(plain):]) and all(d["score"] < media_faces.SUGGEST_FLOOR for d in detail[len(plain):]), detail)
saved_show = media_faces.SUGGEST_Z_SHOW
try:
    media_faces.SUGGEST_Z_SHOW = 1e9
    check("a show line nobody reaches: no cards at all (the old 50 % floor no longer admits anything)",
          media_faces.suggest_same_person(conn, index, 1, detail=True) == [])
finally:
    media_faces.SUGGEST_Z_SHOW = saved_show
saved_min = media_faces.SUGGEST_Z_ABS_MIN
try:
    media_faces.SUGGEST_Z_ABS_MIN = 0.99
    check("the absolute minimum holds back even a 12-sd match", media_faces.suggest_same_person(conn, index, 1, detail=True) == [])
finally:
    media_faces.SUGGEST_Z_ABS_MIN = saved_min
stats = media_faces.background_stats(conn, index, media_faces.cluster_faces(conn, 1, media_faces.SUGGEST_FACES)[1], 1)
check("the background is measured on clusters that are not the source, with a floored sd",
      stats is not None and stats[1] >= media_faces.SUGGEST_BG_SD_MIN and 0.0 <= stats[0] < 0.5, stats)
bgids = index.background(media_faces.cluster_faces(conn, 1, media_faces.SUGGEST_FACES)[1], 1)
check("... never the source, never a dead cluster, no repeats",
      1 not in bgids and len(set(bgids)) == len(bgids) and not ({6} & set(bgids)), bgids)
saved = (media_faces.SUGGEST_Z_LIKELY,)
try:
    media_faces.SUGGEST_Z_LIKELY = 1e9
    check("a line nobody reaches: nothing is likely", not any(x["likely"] for x in media_faces.suggest_same_person(conn, index, 1, detail=True)))
    media_faces.SUGGEST_Z_LIKELY = -1e9
    d_all = media_faces.suggest_same_person(conn, index, 1, detail=True)
    check("a line everybody reaches: all at or above the score line are likely, the weaker ones still not",
          all(x["likely"] == (x["score"] >= media_faces.SUGGEST_LIKELY_ABS_MIN) for x in d_all) and any(x["likely"] for x in d_all), d_all)
finally:
    media_faces.SUGGEST_Z_LIKELY, = saved

# Fallback: where no background can be measured (too few clusters) the absolute
# lines decide, and the line moves with the core and the crowd.
saved = (media_faces.SUGGEST_LIKELY, media_faces.SUGGEST_CORE_MIN, media_faces.SUGGEST_LIKELY_STRONG,
         media_faces.SUGGEST_WIDE_MIN, media_faces.SUGGEST_BG_MIN)
low, high = min(s for _, s in plain), max(s for _, s in plain)
try:
    media_faces.SUGGEST_BG_MIN = 10 ** 6
    fb = media_faces.suggest_same_person(conn, index, 1, detail=True)
    check("no measurable background: z and bg are None and the absolute line decides",
          all(x["z"] is None and x["bg"] is None for x in fb)
          and all(x["likely"] == (x["score"] >= media_faces.likely_line([s for _, s in plain])) for x in fb), fb)
    # plain line = the best score; strong line = under the lowest; two matches reach it.
    media_faces.SUGGEST_LIKELY, media_faces.SUGGEST_LIKELY_STRONG, media_faces.SUGGEST_WIDE_MIN = high, low - 0.02, 2
    media_faces.SUGGEST_CORE_MIN = 2
    d = media_faces.suggest_same_person(conn, index, 1, detail=True)
    check("too few high matches for a core: the plain line applies (only the best one is likely)",
          [x["likely"] for x in d] == [True, False], d)
    media_faces.SUGGEST_CORE_MIN = 1
    d = media_faces.suggest_same_person(conn, index, 1, detail=True)
    check("a core and a crowd: the strong line applies and both are likely", [x["likely"] for x in d] == [True, True], d)
    media_faces.SUGGEST_WIDE_MIN = 3
    d = media_faces.suggest_same_person(conn, index, 1, detail=True)
    check("... a core without the crowd (3 needed, 2 found): plain line again", [x["likely"] for x in d] == [True, False], d)
finally:
    (media_faces.SUGGEST_LIKELY, media_faces.SUGGEST_CORE_MIN, media_faces.SUGGEST_LIKELY_STRONG,
     media_faces.SUGGEST_WIDE_MIN, media_faces.SUGGEST_BG_MIN) = saved
check("without detail the tuples and the plain floor are untouched",
      media_faces.suggest_same_person(conn, index, 1) == plain)
check("a cosplay tag changes nothing (the tag marks Ortega's photos too)",
      (conn.execute("INSERT OR REPLACE INTO media_tags (media_id, tag, category, p) SELECT id, 'cosplay', 'general', 0.9 FROM media"),
       media_faces.suggest_same_person(conn, index, 1, detail=True) == detail,
       conn.execute("DELETE FROM media_tags WHERE tag = 'cosplay'"), conn.commit())[1])

check("the source is never offered to itself", 1 not in [c for c, _ in media_faces.suggest_same_person(conn, index, 1, limit=30, floor=-1)])
check("the ok filter removes a candidate before it is scored",
      [c for c, _ in media_faces.suggest_same_person(conn, index, 1, ok=lambda c: c != 2)] == [3])
check("a cluster id with no faces gets no suggestions", media_faces.suggest_same_person(conn, index, 9999) == [])
check("limit and floor apply",
      len(media_faces.suggest_same_person(conn, index, 1, limit=1)) == 1
      and media_faces.suggest_same_person(conn, index, 1, floor=0.999) == [])
print("info: index of 405 clusters built in %.2f s, one suggestion %.3f s" % (build_s, ask_s), flush=True)

# ---------------------------------------------------------------- rejection --
check("rejecting a pair is remembered", media_faces.record_not_same(conn, 1, 2) is True)
check("... for both of them, whichever way round",
      media_faces.rejected_clusters(conn, 1) == {2} and media_faces.rejected_clusters(conn, 2) == {1})
check("... saying it twice changes nothing",
      media_faces.record_not_same(conn, 2, 1) is True
      and conn.execute("SELECT COUNT(*) FROM person_nomatch").fetchone()[0] == 1)
check("a cluster that does not exist cannot be rejected", media_faces.record_not_same(conn, 1, 9999) is False)
check("a rejected pair is filtered out of the suggestions when the caller passes the set",
      [c for c, _ in media_faces.suggest_same_person(
          conn, index, 1, ok=lambda c: c not in media_faces.rejected_clusters(conn, 1))] == [3])
# A re-cluster renumbers every cluster (persist). The answer is keyed by the
# cover faces, so it follows the people to their new ids.
conn.execute("UPDATE clusters SET id = id + 1000 WHERE id IN (1, 2)")
check("the answer survives a renumbering (it is keyed by cover face)",
      media_faces.rejected_clusters(conn, 1001) == {1002}, media_faces.rejected_clusters(conn, 1001))
conn.execute("UPDATE clusters SET id = id - 1000 WHERE id IN (1001, 1002)")
conn.execute("DELETE FROM person_nomatch")
conn.commit()

# -------------------------------------------------------------------- merge --
conn.execute("UPDATE faces SET cluster_id = 1 WHERE cluster_id = 2")
conn.execute("UPDATE clusters SET merged_into = 1, face_count = 0 WHERE id = 2")
conn.execute("UPDATE clusters SET face_count = 52 WHERE id = 1")
conn.commit()
index.on_merge(conn, 2, 1)
after = media_faces.suggest_same_person(conn, index, 1)
check("after a merge the merged cluster is no longer offered, the rest still are",
      [c for c, _ in after] == [3], after)
check("... nor is the merged cluster found as a candidate for anyone else",
      2 not in [c for c, _ in media_faces.suggest_same_person(conn, index, 3)],
      media_faces.suggest_same_person(conn, index, 3))
check("on_merge on a cluster the index never held is harmless", index.on_merge(conn, 99999, 88888) is None)

# ---------------------------------------------- background build and status --
fresh = media_faces.SuggestIndex()
check("an index that has not been built answers 'not ready' and starts building",
      fresh.ensure(lambda: media_ai.connect(tmp)) is False)
for _ in range(100):
    if fresh.status()["clusters"] and not fresh.status()["building"]:
        break
    time.sleep(0.1)
check("... and is ready a moment later, without blocking the caller",
      fresh.ensure(lambda: media_ai.connect(tmp)) is True and fresh.status()["clusters"] == 404, fresh.status())

# ------------------------------------------------------------------- routes --
try:
    import flask  # noqa: F401
    have_flask = True
except ImportError:
    have_flask = False
    print("skip: Flask is not importable here, the route checks run on the media VM", flush=True)

if have_flask:
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

    def load(name):
        exec(compile(ast.Module(body=[defs[name]], type_ignores=[]), SERVER, "exec"), ns)

    def _names(code):
        out = set(code.co_names)
        for const in code.co_consts:
            if hasattr(const, "co_names"):
                out |= _names(const)
        return out

    def F(name, seen=None):
        seen = set() if seen is None else seen
        if name not in ns and name in defs and name not in seen:
            seen.add(name)
            for used in _names(compile(ast.Module(body=[defs[name]], type_ignores=[]), SERVER, "exec")):
                if used not in ns:
                    F(used, seen)
            load(name)
        return ns.get(name)

    # A fresh store for the routes: the people snapshot is faked, so the rows
    # the route reads (clusters, faces, media) are the ones above, re-merged.
    ns["BASE_DIR"] = tmp
    ns["DEFAULT_ROOT_ID"] = R
    USER = {"username": "alice", "role": "admin"}
    ns["get_authenticated_user"] = lambda: USER
    ns["roots_for_user"] = lambda user: [{"id": R}]
    ns["content_levels_map"] = lambda wait=False: None

    class FakeService:
        def __init__(self):
            self.snapshot = None
            self.invalidated = 0

        def get(self, key, build):
            return self.snapshot

        def invalidate(self):
            self.invalidated += 1

    svc = FakeService()
    ns["discovery_service"] = lambda: svc
    F("app")
    app = ns["app"]
    for name in ("media_person_similar", "media_person_not_same", "media_person_update", "media_person_decision"):
        F(name)

    def get(path, fn="media_person_similar", **kw):
        with app.test_request_context(path):
            rv = ns[fn](kw["cid"])
        return rv if isinstance(rv, tuple) else (rv, 200)

    # Rebuild the situation: person A in 1 (52 faces after the merge above), 3.
    roots = {str(c): [R] for c in list(range(1, 6)) + list(STRANGERS)}
    svc.snapshot = {"auto": {}, "real": {}, "roots": roots}

    svc.snapshot = None
    rv, code = get("/api/media/people/1/similar", cid=1)
    check("no People snapshot yet: loading, no items",
          rv.get_json()["loading"] is True and rv.get_json()["items"] == [], rv.get_json())
    svc.snapshot = {"auto": {}, "real": {}, "roots": roots}
    for _ in range(200):
        rv, code = get("/api/media/people/1/similar", cid=1)
        if not rv.get_json()["loading"]:
            break
        time.sleep(0.1)
    body = rv.get_json()
    check("the route answers once the index is built", code == 200 and body["loading"] is False, body)
    check("it offers person A's other half, with a score and the 'likely' flag, and the source",
          body["items"][0]["id"] == 3 and not any(i["likely"] for i in body["items"][1:]) and body["items"][0]["similarity"] >= media_faces.SUGGEST_LIKELY
          and body["items"][0]["likely"] is True and body["source"]["id"] == 1, body)
    check("the response carries the bulk-offer constants for the page",
          body["bulk_min"] == media_faces.SUGGEST_BULK_MIN
          and body["likely_at"] == body["bulk_at"] == media_faces.likely_line([i["similarity"] for i in body["items"]])
          and not any("cosplay" in i for i in body["items"]), body)
    check("each tile carries its z (sd above the person's own background) and the page gets bg and the z line",
          all(isinstance(i["z"], float) for i in body["items"]) and isinstance(body["bg"], float)
          and body["z_likely"] == media_faces.SUGGEST_Z_LIKELY
          and all(i["likely"] == (i["z"] >= media_faces.SUGGEST_Z_LIKELY and i["similarity"] >= media_faces.SUGGEST_LIKELY_ABS_MIN) for i in body["items"]), body)
    check("... as full People tiles (count, cover face)",
          body["items"][0]["count"] >= 1 and body["items"][0]["face_id"] == face_ids[3][0], body["items"][0])
    rv, code = get("/api/media/people/777/similar", cid=777)
    check("an unknown person is a 404", code == 404, code)
    rv, code = get("/api/media/people/2/similar", cid=2)
    check("a merged-away person is a 404 too", code == 404, code)
    ns["roots_for_user"] = lambda user: [{"id": "elsewhere"}]
    rv, code = get("/api/media/people/1/similar", cid=1)
    check("a viewer who has no file of a person is not offered them", rv.get_json()["items"] == [], rv.get_json())
    ns["roots_for_user"] = lambda user: [{"id": R}]
    svc.snapshot = {"auto": {}, "real": {"3": {"real": False, "judged": 9}}, "roots": roots}
    rv, code = get("/api/media/people/1/similar", cid=1)
    check("a person judged to be drawn is not offered (the grid hides them too)",
          3 not in [i["id"] for i in rv.get_json()["items"]], rv.get_json())
    svc.snapshot = {"auto": {}, "real": {}, "roots": roots}

    def post(path, payload, fn, cid):
        with app.test_request_context(path, method="POST", json=payload):
            rv = ns[fn](cid)
        return rv if isinstance(rv, tuple) else (rv, 200)

    rv, code = post("/api/media/people/1/not_same", {"other": 3}, "media_person_not_same", 1)
    check("saying 'not the same' succeeds", code == 200 and rv.get_json() == {"ok": True}, (code, rv.get_json()))
    rv, code = get("/api/media/people/1/similar", cid=1)
    check("... and the pair is no longer offered", 3 not in [i["id"] for i in rv.get_json()["items"]], rv.get_json())
    rv, code = get("/api/media/people/3/similar", cid=3)
    check("... from either side", 1 not in [i["id"] for i in rv.get_json()["items"]], rv.get_json())
    rv, code = post("/api/media/people/1/not_same", {"other": 1}, "media_person_not_same", 1)
    check("a person cannot be 'not the same' as themselves", code == 400, code)
    rv, code = post("/api/media/people/1/not_same", {}, "media_person_not_same", 1)
    check("a body without `other` is a 400", code == 400, code)
    rv, code = post("/api/media/people/1/not_same", {"other": 4242}, "media_person_not_same", 1)
    check("an unknown other person is a 404", code == 404, code)
    USER["role"] = "guest"
    rv, code = post("/api/media/people/1/not_same", {"other": 3}, "media_person_not_same", 1)
    check("only an admin can record it", code == 403, code)
    rv, code = get("/api/media/people/1/similar", cid=1)
    check("any signed-in user can read the suggestions", code == 200, code)
    USER["role"] = "admin"

    # The decision log: a JSONL file beside the index, never a table.
    log = os.path.join(media_ai.store_dir(tmp), "person_decisions.jsonl")
    check("no decision log before a decision", not os.path.exists(log))
    good = {"other": 3, "decision": "merge", "score": 0.7812, "likely": True, "line": 0.7, "bulk": True, "z": 8.4, "bg": 0.24}
    rv, code = post("/api/media/people/1/decision", good, "media_person_decision", 1)
    check("a decision is logged", code == 200 and rv.get_json() == {"ok": True}, (code, rv.get_json()))
    rv, code = post("/api/media/people/1/decision", {**good, "decision": "not_same", "score": 0.66, "likely": False,
                                                    "line": 0.75, "bulk": False}, "media_person_decision", 1)
    import json as _json
    rows = [_json.loads(x) for x in open(log, encoding="utf-8").read().splitlines()]
    check("... one JSON line each, with the pair, the score and the flags it was judged with",
          len(rows) == 2 and rows[0]["anchor"] == 1 and rows[0]["other"] == 3 and rows[0]["decision"] == "merge"
          and rows[0]["score"] == 0.7812 and rows[0]["likely"] is True and rows[0]["bulk"] is True
          and rows[0]["user"] == "alice" and rows[1]["decision"] == "not_same" and rows[1]["line"] == 0.75 and rows[0]["line"] == 0.7
          and rows[1]["bulk"] is False and isinstance(rows[0]["at"], float), rows)
    check("... and the z and background of the judgement (blank when the client had none)",
          rows[0]["z"] == 8.4 and rows[0]["bg"] == 0.24, rows)
    for bad in ({**good, "decision": "delete"}, {**good, "score": 1.5}, {**good, "score": "x"},
                {**good, "other": 1}, {**good, "line": 2}, {"decision": "merge", "score": 0.7}):
        rv, code = post("/api/media/people/1/decision", bad, "media_person_decision", 1)
        check("a malformed decision is a 400 and writes nothing: %s" % (bad,), code == 400, code)
    check("... the log still has its two lines", len(open(log, encoding="utf-8").read().splitlines()) == 2)
    check("the log is not in the media index database (no schema change)",
          conn.execute("SELECT 1 FROM sqlite_master WHERE name LIKE '%decision%'").fetchone() is None)
    USER["role"] = "guest"
    rv, code = post("/api/media/people/1/decision", good, "media_person_decision", 1)
    check("only an admin can log a decision", code == 403, code)
    USER["role"] = "admin"
    conn.execute("DELETE FROM person_nomatch")
    conn.commit()

    # The merge POST keeps the resident index in step: merge 3 into 1, and the
    # next suggestion for anyone no longer offers 3.
    rv, code = post("/api/media/people/3", {"merge_into": 1}, "media_person_update", 3)
    check("the merge POST still merges", code == 200 and rv.get_json()["into"] == 1, (code, rv.get_json()))
    check("... and invalidates the People snapshot", svc.invalidated == 1, svc.invalidated)
    rv, code = get("/api/media/people/1/similar", cid=1)
    check("... and the merged person is gone from the suggestions", not ({2, 3} & {i["id"] for i in rv.get_json()["items"]}), rv.get_json())

conn.close()
shutil.rmtree(tmp, ignore_errors=True)
print("ALL PASS" if ok else "SOME CHECKS FAILED")
sys.exit(0 if ok else 1)
