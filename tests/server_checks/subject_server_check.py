"""A STAGED server.py and media_ai.py, out of the AST in Flask test requests:
words narrow a character pool, a person and the favourites in every search type
(only with narrow=1); Context reads tags as well as captions; a pool is no longer
cut at 5,000 files; live share links keep the words. Synthetic AI index, path
index and shares file in a temp dir; nothing under /data2 is written.

  venv/bin/python subject_server_check.py /tmp/subject/server.py
"""
import ast
import json
import os
import shutil
import sys
import tempfile

BASE = os.environ.get("ATRIHORD_DIR") or os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SERVER = sys.argv[1]
STAGE = os.path.dirname(os.path.abspath(SERVER))
sys.path.insert(0, BASE)
sys.path.insert(0, STAGE)
tmp = tempfile.mkdtemp(prefix="subject_check_")
store = os.path.join(tmp, "ai")
os.makedirs(store)
os.environ["KSF_MEDIA_AI_DIR"] = store
shutil.copy(os.path.join(BASE, "config.json"), os.path.join(tmp, "config.json"))

import media_ai                      # noqa: E402

ok = True


def check(label, cond, detail=""):
    global ok
    ok = ok and bool(cond)
    print(("PASS " if cond else "FAIL ") + label + (f"  ({detail})" if detail and not cond else ""))


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


ns["CONFIG_FILE"] = os.path.join(tmp, "config.json")
ns["SHARES_FILE"] = os.path.join(tmp, "shares.json")
ADMIN = {"username": "alice", "role": "admin"}
GUEST = {"username": "guest", "role": "guest"}
CURRENT = {"user": ADMIN}
ns["get_authenticated_user"] = lambda: CURRENT["user"]
# BASE_DIR first: helpers that catch their own exceptions hide a NameError.
for name in ("BASE_DIR", "CONFIG", "DEFAULT_CONFIG", "app", "SEARCH_MODES", "AI_SEARCH_COLUMNS", "narrow_to_words",
             "search_media", "create_query_share", "query_share_files", "share_detail", "share_summary",
             "_read_shares_unlocked", "shares_lock",
             # _compute_query_share swallows its own errors, so a person link's
             # helper that is loaded on demand would read as "no files".
             "resolve_person_anchor", "PERSON_ANCHOR_FACES", "PERSON_ANCHOR_MIN_SHARE", "_compute_query_share"):
    load(name)
app = ns["app"]
check("server imports the staged media_ai", ns.get("media_ai") is media_ai)
check("media_ai has search_ids and ids_for_paths", hasattr(media_ai, "search_ids") and hasattr(media_ai, "ids_for_paths"))

ns["roots_for_user"] = lambda user: [{"id": "downloads"}] + ([{"id": "other"}] if user and user.get("role") == "admin" else [])
ROWS = [("downloads", "miku", True), ("downloads", "miku/beach.jpg", False), ("downloads", "miku/city.jpg", False),
        ("downloads", "miku/sword.jpg", False), ("downloads", "miku/beach_trip_night.jpg", False),
        ("downloads", "miku/trashed_beach.jpg", False), ("downloads", "rin/beach.jpg", False),
        ("downloads", "person/p1_beach.jpg", False), ("downloads", "person/p1_room.jpg", False),
        ("other", "miku_beach.jpg", False)]
ROWS += [("downloads", "big/f%04d.jpg" % i, False) for i in range(5200)]
ns["get_media_index"] = lambda: ROWS
ns["build_entries_for"] = lambda rows: [{"root": r[0], "path": r[1], "name": r[1].rsplit("/", 1)[-1],
                                         "is_dir": bool(r[2]) if len(r) > 2 else False} for r in rows]
ns["attach_media_dimensions"] = lambda page: None
ns["attach_favorites"] = lambda page, user: None
FAVS = {"downloads": {"miku/city.jpg": 2.0, "rin/beach.jpg": 3.0, "person/p1_room.jpg": 1.0}}
ns["user_favorites"] = lambda username: FAVS if username == "alice" else {}

conn = media_ai.connect(tmp)


def add(mid, path, root="downloads", caption=None, ocr=None, character=(), general=(), trashed=None, clusters=()):
    tags = {"character": [{"tag": t, "p": 0.9} for t in character], "general": [{"tag": t, "p": 0.8} for t in general]}
    conn.execute("INSERT INTO media (id, root, path, kind, caption, ocr, tags, tag_model, trashed)"
                 " VALUES (?,?,?,?,?,?,?,?,?)", (mid, root, path, "image", caption, ocr, json.dumps(tags), "wd", trashed))
    media_ai.replace_tags(conn, mid, tags)
    for cluster in clusters:
        conn.execute("INSERT INTO faces (media_id, bbox, det_score, quality, cluster_id) VALUES (?,?,?,?,?)",
                     (mid, "[0, 0, 1, 1]", 0.9, 50.0, cluster))


add(1, "miku/beach.jpg", caption="A girl standing on the sand.", character=["hatsune_miku"], general=["beach"])
add(2, "miku/city.jpg", caption="The image shows a woman at the beach.", character=["hatsune_miku"], general=["city"])
add(3, "miku/sword.jpg", ocr="BEACH PARTY", character=["hatsune_miku"], general=["sword"])
add(4, "miku/beach_trip_night.jpg", character=["hatsune_miku"], general=["night"])
add(5, "rin/beach.jpg", character=["kagamine_rin"], general=["beach"])
add(6, "person/p1_beach.jpg", general=["beach"], clusters=[7])
add(7, "person/p1_room.jpg", general=["bedroom"], clusters=[7])
add(8, "miku/trashed_beach.jpg", character=["hatsune_miku"], general=["beach"], trashed=1.0)
add(9, "miku_beach.jpg", root="other", character=["hatsune_miku"], general=["beach"])
for i in range(5200):
    add(100 + i, "big/f%04d.jpg" % i, character=["big_pool"], general=["beach"] if i % 100 == 0 else ["indoors"])
conn.commit()


def search(**params):
    params.setdefault("limit", "100")
    with app.test_request_context("/api/media/search", query_string=params):
        rv = resolve(lambda: ns["search_media"]())
    resp, status = (rv[0], rv[1]) if isinstance(rv, tuple) else (rv, rv.status_code)
    return status, resp.get_json()


def paths(data):
    return sorted("%s:%s" % (i["root"], i["path"]) for i in (data or {}).get("items", []))


M = "hatsune_miku"
POOL = ["downloads:miku/beach.jpg", "downloads:miku/beach_trip_night.jpg", "downloads:miku/city.jpg",
        "downloads:miku/sword.jpg", "other:miku_beach.jpg"]
cases = [
    ("a pool without words: its files (not the trashed one), both roots for an admin", dict(q="", tag=M), POOL),
    ("pool + beach, Context: its beach tag or its caption", dict(q="beach", tag=M, mode="context", narrow="1"),
     ["downloads:miku/beach.jpg", "downloads:miku/city.jpg", "other:miku_beach.jpg"]),
    ("... the word's case does not matter", dict(q="Beach", tag=M, mode="context", narrow="1"),
     ["downloads:miku/beach.jpg", "downloads:miku/city.jpg", "other:miku_beach.jpg"]),
    ("pool + beach, Character: tags only", dict(q="beach", tag=M, mode="character", narrow="1"),
     ["downloads:miku/beach.jpg", "other:miku_beach.jpg"]),
    ("pool + beach, Text in image: OCR only", dict(q="beach", tag=M, mode="ocr", narrow="1"), ["downloads:miku/sword.jpg"]),
    ("pool + beach, Filename: the path", dict(q="beach", tag=M, mode="filename", narrow="1"),
     ["downloads:miku/beach.jpg", "downloads:miku/beach_trip_night.jpg", "other:miku_beach.jpg"]),
    ("pool + beach, Everything: path and index together", dict(q="beach", tag=M, mode="all", narrow="1"), POOL),
    ("pool + a * wildcard in Filename", dict(q="beach*night", tag=M, mode="filename", narrow="1"),
     ["downloads:miku/beach_trip_night.jpg"]),
    ("pool + a lone *: nothing narrowed", dict(q="*", tag=M, mode="context", narrow="1"), POOL),
    ("pool + words matching nothing: empty", dict(q="zzzz", tag=M, mode="context", narrow="1"), []),
    ("without narrow=1 (a page from before) the words are ignored as before", dict(q="Hatsune Miku", tag=M, mode="context"), POOL),
    ("a person without words: every file their face is in", dict(q="", person="7"),
     ["downloads:person/p1_beach.jpg", "downloads:person/p1_room.jpg"]),
    ("a person + beach", dict(q="beach", person="7", mode="context", narrow="1"), ["downloads:person/p1_beach.jpg"]),
]
for label, params, want in cases:
    status, data = search(**params)
    check(label, status == 200 and paths(data) == sorted(want), (status, paths(data)))

status, data = search(q="beach", favorites="1", mode="context", narrow="1")
order = ["%s:%s" % (i["root"], i["path"]) for i in (data or {}).get("items", [])]
check("favourites + beach: narrowed, still newest favourite first",
      order == ["downloads:rin/beach.jpg", "downloads:miku/city.jpg"], order)

CURRENT["user"] = GUEST
status, data = search(q="beach", tag=M, mode="context", narrow="1")
check("a guest's narrowed pool stays inside the roots it may see",
      paths(data) == ["downloads:miku/beach.jpg", "downloads:miku/city.jpg"], paths(data))
CURRENT["user"] = ADMIN

status, data = search(q="beach", tag=M, mode="context", narrow="1", limit="1", offset="1")
check("paging over a narrowed pool: one item, the total of three, more to come",
      len(data["items"]) == 1 and data["total"] == 3 and data["has_more"] is True, data and (len(data["items"]), data["total"]))

status, data = search(q="beach", mode="context")
got = set(paths({"items": data["items"]}))
check("Context without a subject now finds tag-only files too (57: 56 beach tags + 1 caption)",
      data["total"] == 57 and {"downloads:rin/beach.jpg", "downloads:miku/city.jpg", "downloads:person/p1_beach.jpg"} <= got
      and "downloads:miku/trashed_beach.jpg" not in got, (data["total"], sorted(got)[:6]))

status, data = search(q="", tag="big_pool")
check("a pool of 5,200 files is listed whole, not cut at 5,000", data["total"] == 5200, data["total"])
status, data = search(q="beach", tag="big_pool", mode="context", narrow="1")
check("... and narrowing it counts all of it (52)", data["total"] == 52, data["total"])

ids = media_ai.ids_for_paths(conn, {("downloads", "miku/beach.jpg"), ("downloads", "nope.jpg")})
check("ids_for_paths maps known paths only", ids == {1: ("downloads", "miku/beach.jpg")}, ids)
check("search_ids keeps only ids it was given", media_ai.search_ids(conn, "beach", "tags", {1, 5, 3}) == {1, 5})
check("search_ids with an unparseable or empty text is empty", media_ai.search_ids(conn, "", "tags", {1}) == set())


# ---------------------------------------------------------------- live share links
def post_share(body):
    with app.test_request_context("/api/share/query", method="POST", json=body):
        rv = resolve(lambda: ns["create_query_share"]())
    resp, status = (rv[0], rv[1]) if isinstance(rv, tuple) else (rv, rv.status_code)
    return status, resp.get_json()


def stored(share_id):
    return next(s for s in json.load(open(ns["SHARES_FILE"])) if s["id"] == share_id)


status, data = post_share({"type": "character", "tags": [M], "text": "beach", "mode": "context", "label": "Miku"})
check("a live pool link with words: created, counting the narrowed files", status == 200 and data.get("total") == 3,
      (status, data))
share = stored(data["id"])
check("... the words and the type are stored with it", share["query"].get("text") == "beach" and share["query"].get("mode") == "context",
      share["query"])
files = sorted("%s:%s" % p for p in resolve(lambda: ns["query_share_files"](share))[0])
check("... and the link shows exactly the narrowed files",
      files == ["downloads:miku/beach.jpg", "downloads:miku/city.jpg", "other:miku_beach.jpg"], files)
check("... the Shares manager says so", 'narrowed to "beach"' in resolve(lambda: ns["share_detail"](share)),
      resolve(lambda: ns["share_detail"](share)))
check("... and the summary carries the words", data["query"].get("text") == "beach" and data["query"].get("mode") == "context",
      data.get("query"))

status, data = post_share({"type": "character", "tags": [M]})
check("a link without words is unchanged: whole pool, nothing stored", status == 200 and data.get("total") == 5
      and "text" not in stored(data["id"])["query"] and data["query"].get("text") == "", (status, data))
status, data = post_share({"type": "character", "tags": [M], "text": "*", "mode": "context"})
check("a lone * is not stored as words", status == 200 and data.get("total") == 5 and "text" not in stored(data["id"])["query"])
status, data = post_share({"type": "person", "person": 7, "text": "beach", "mode": "context"})
check("a live person link with words", status == 200 and data.get("total") == 1, (status, data))
for label, body in (("text that is not a string", {"type": "character", "tags": [M], "text": 5}),
                    ("an unknown search type", {"type": "character", "tags": [M], "text": "beach", "mode": "bogus"}),
                    ("words over 200 characters", {"type": "character", "tags": [M], "text": "x" * 201, "mode": "context"})):
    status, data = post_share(body)
    check("refused: " + label, status == 400, (status, data))

shutil.rmtree(tmp, ignore_errors=True)
print("ALL PASS" if ok else "SOME CHECKS FAILED")
