"""Safe covers for the character and people circles. Out of a server.py's AST
and the staged media_ai.py (the directory of <server.py> goes first on the
path), over a temp AI store and library: the real /api/media/characters,
/api/media/characters/pool and /api/media/people routes. With "old" it shows
the covers the old code picks.

  venv/bin/python safecover_check.py <server.py> [old]
"""
import ast
import os
import shutil
import sys
import tempfile

BASE = os.environ.get("ATRIHORD_DIR") or os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SERVER = sys.argv[1]
OLD = len(sys.argv) > 2 and sys.argv[2] == "old"
sys.path.insert(0, BASE)
sys.path.insert(0, os.path.dirname(os.path.abspath(SERVER)))
tmp = tempfile.mkdtemp(prefix="safecover_check_")
os.environ["KSF_MEDIA_AI_DIR"] = os.path.join(tmp, "ai")
os.makedirs(os.environ["KSF_MEDIA_AI_DIR"])
import media_ai                      # noqa: E402

ok = True


def check(label, cond, detail=""):
    global ok
    ok = ok and bool(cond)
    print(("PASS " if cond else "FAIL ") + label + (f"  ({detail})" if detail and not cond else ""), flush=True)


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


R, P = "downloads", "private"
lib = os.path.join(tmp, "lib")
priv = os.path.join(tmp, "priv")
ns["BASE_DIR"] = tmp
ns["DEFAULT_ROOT_ID"] = R
USER = {"username": "alice", "role": "admin"}
ns["get_authenticated_user"] = lambda: USER
ROOTS = {R: lib, P: priv}
ns["get_root"] = lambda root_id: {"id": root_id, "path": ROOTS[root_id]} if root_id in ROOTS else None
ns["roots_for_user"] = lambda user: [{"id": R}]          # "private" is not visible to this caller
preload("app")
app = ns["app"]

conn = media_ai.connect(tmp)
RATING = {"safe": "general", "sugg": "sensitive", "q": "questionable", "expl": "explicit", "none": None}
ids = {}


def media(name, rating, root=R, trashed=False):
    base = ROOTS[root]
    os.makedirs(base, exist_ok=True)
    open(os.path.join(base, name), "wb").write(b"x")
    cur = conn.execute("INSERT INTO media (root, path, size, kind, trashed) VALUES (?, ?, 1, 'image', ?)",
                       (root, name, 1.0 if trashed else None))
    ids[name] = cur.lastrowid
    if RATING[rating]:
        conn.execute("INSERT INTO media_tags (media_id, tag, category, p) VALUES (?, ?, 'rating', 0.9)",
                     (cur.lastrowid, RATING[rating]))
    return cur.lastrowid


def tag(name, character, p):
    conn.execute("INSERT INTO media_tags (media_id, tag, category, p) VALUES (?, ?, 'character', ?)",
                 (ids[name], character, p))


# Characters. alice: the surest picture is explicit, the Safe one the least sure.
for n, r, p in (("a1.jpg", "expl", .95), ("a2.jpg", "q", .9), ("a3.jpg", "sugg", .8), ("a4.jpg", "safe", .7)):
    media(n, r); tag(n, "alice", p)
for n, r, p in (("b1.jpg", "safe", .9), ("b2.jpg", "safe", .8)):          # bob: already Safe
    media(n, r); tag(n, "bob", p)
for n, r, p in (("c1.jpg", "expl", .9), ("c2.jpg", "sugg", .5)):          # carol: no Safe one
    media(n, r); tag(n, "carol", p)
for n, r, p in (("d1.jpg", "expl", .9), ("d2.jpg", "none", .8)):          # dave: unrated beats explicit
    media(n, r); tag(n, "dave", p)
for n, r, p in (("f1.jpg", "expl", .9),):                                 # frank: the Safe one is trashed
    media(n, r); tag(n, "frank", p)
media("f2.jpg", "safe", trashed=True); tag("f2.jpg", "frank", .8)
media("g1.jpg", "safe", root=P); tag("g1.jpg", "gina", .9)                # gina: the Safe one is not visible
media("g2.jpg", "expl"); tag("g2.jpg", "gina", .5)
# eve has a costume variant; only the costume has a Safe picture.
media("e1.jpg", "expl"); tag("e1.jpg", "eve", .95)
media("e2.jpg", "safe"); tag("e2.jpg", "eve_(swimsuit)", .6)
conn.execute("INSERT INTO tag_variants (tag, group_key, category) VALUES ('eve', 'eve', 'character')")
conn.execute("INSERT INTO tag_variants (tag, group_key, category) VALUES ('eve_(swimsuit)', 'eve', 'character')")

# People: faces by quality; the stored cover is the best face.
faces = {}


def face(name, cluster, q):
    cur = conn.execute("INSERT INTO faces (media_id, bbox, cluster_id, quality) VALUES (?, '[0,0,10,10]', ?, ?)",
                       (ids[name], cluster, q))
    faces[(name, cluster)] = cur.lastrowid
    return cur.lastrowid


for n, r in (("p1.jpg", "expl"), ("p2.jpg", "sugg"), ("p3.jpg", "safe"), ("p4.jpg", "safe"),
             ("p5.jpg", "expl"), ("p6.jpg", "none"), ("p7.jpg", "safe"), ("p8.jpg", "sugg")):
    media(n, r)
media("p9.jpg", "safe", trashed=True)
people = {
    1: [("p1.jpg", .99), ("p2.jpg", .9), ("p3.jpg", .8)],      # cover explicit -> the Safe face
    2: [("p4.jpg", .99), ("p1.jpg", .5)],                       # cover already Safe -> kept
    3: [("p5.jpg", .99), ("p6.jpg", .5)],                       # nothing Safe -> the unrated one
    4: [("p9.jpg", .99), ("p8.jpg", .9), ("p7.jpg", .1)],       # cover's file trashed -> the Safe one
}
for cid, members in people.items():
    fids = [face(n, cid, q) for n, q in members]
    conn.execute("INSERT INTO clusters (id, name, cover_face, face_count) VALUES (?, NULL, ?, ?)",
                 (cid, fids[0], len(fids)))
conn.commit()


def get(url, fn, *args):
    with app.test_request_context(url):
        rv = resolve(lambda: F(fn)(*args))
    return rv.get_json() if hasattr(rv, "get_json") else rv[0].get_json()


try:
    resolve(lambda: F("content_levels_map")())       # built, as the startup warm-up does
    pools = {p["tag"]: p for p in get("/api/media/characters?limit=50", "media_characters")["items"]}
    cover = lambda t: (pools[t]["cover"] or {}).get("path")
    if OLD:
        check("OLD: alice shows her explicit picture", cover("alice") == "a1.jpg", cover("alice"))
        check("OLD: person 1 shows the explicit face",
              {p["id"]: p for p in get("/api/media/people?min_size=1", "media_people")["items"]}[1]["cover"]["path"] == "p1.jpg")
        print("ALL PASS" if ok else "SOME CHECKS FAILED")
        sys.exit(0)

    check("alice: the Safe picture, although the tagger was least sure of it", cover("alice") == "a4.jpg", cover("alice"))
    check("bob: already Safe, unchanged (the surest one)", cover("bob") == "b1.jpg", cover("bob"))
    check("carol: nothing Safe, so Suggestive before Explicit", cover("carol") == "c2.jpg", cover("carol"))
    check("dave: not rated yet before Explicit", cover("dave") == "d2.jpg", cover("dave"))
    check("frank: a trashed Safe picture is never the cover", cover("frank") == "f1.jpg", cover("frank"))
    check("gina: a Safe picture the caller cannot see is not chosen", cover("gina") == "g2.jpg", cover("gina"))
    eve = pools["eve"]
    vc = {v["tag"]: (v["cover"] or {}).get("path") for v in eve["variants"]}
    check("eve: the pool shows its costume's Safe picture", cover("eve") == "e2.jpg", cover("eve"))
    check("... each variant keeps its own", vc == {"eve": "e1.jpg", "eve_(swimsuit)": "e2.jpg"}, vc)
    one = get("/api/media/characters/pool?tag=alice", "media_character_pool")
    check("the single-pool route (a reloaded character URL) agrees", (one["cover"] or {}).get("path") == "a4.jpg", one)
    typeahead = {p["tag"]: p for p in get("/api/media/characters?limit=8&q=ali", "media_characters")["items"]}
    check("the type-ahead agrees", (typeahead["alice"]["cover"] or {}).get("path") == "a4.jpg", typeahead)

    ppl = {p["id"]: p for p in get("/api/media/people?min_size=1", "media_people")["items"]}
    check("person 1: the Safe face, not the sharpest explicit one",
          ppl[1]["cover"]["path"] == "p3.jpg" and ppl[1]["face_id"] == faces[("p3.jpg", 1)] == ppl[1]["cover"]["face_id"],
          ppl[1])
    check("person 2: the stored Safe cover is kept", ppl[2]["face_id"] == faces[("p4.jpg", 2)], ppl[2])
    check("person 3: nothing Safe, so the unrated face", ppl[3]["cover"]["path"] == "p6.jpg", ppl[3])
    check("person 4: the cover's file is trashed, so the Safe face", ppl[4]["cover"]["path"] == "p7.jpg", ppl[4])
    stored = {r[0]: r[1] for r in conn.execute("SELECT id, cover_face FROM clusters")}
    check("the stored cover faces are untouched (names carry over by them)",
          stored == {cid: faces[(m[0][0], cid)] for cid, m in people.items()}, stored)

    # Before the first content map there is nothing to rank by: the old covers.
    ns["_content_cache"]["levels"] = None
    ns["content_levels_map"] = lambda wait=True: None
    pools = {p["tag"]: p for p in get("/api/media/characters?limit=50", "media_characters")["items"]}
    check("without a content map yet: the old covers, no error", cover("alice") == "a1.jpg", cover("alice"))

    plans = {
        "media_tags_character_best": [r[3] for r in conn.execute(
            "EXPLAIN QUERY PLAN SELECT t.p, m.root, m.path FROM media_tags t JOIN media m ON m.id = t.media_id"
            " WHERE t.category = 'character' AND t.tag = ? AND m.trashed IS NULL ORDER BY t.p DESC", ("alice",))],
        "faces_cluster_quality": [r[3] for r in conn.execute(
            "EXPLAIN QUERY PLAN SELECT f.id, f.bbox, m.root, m.path FROM faces f JOIN media m ON m.id = f.media_id"
            " WHERE f.cluster_id = ? AND m.trashed IS NULL ORDER BY f.quality DESC", (1,))],
    }
    for index, plan in plans.items():
        check(f"the walk uses {index}, with no sort", any(index in s for s in plan)
              and not any("TEMP B-TREE" in s for s in plan), plan)
finally:
    shutil.rmtree(tmp, ignore_errors=True)
print("ALL PASS" if ok else "SOME CHECKS FAILED")
