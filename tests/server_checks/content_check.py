"""Content-level filter checks of a STAGED server.py and media_ai.py, out of the
AST in Flask test requests. Levels are computed on a synthetic AI index in a
temp dir; the real index is only opened read-only, once, to time the build.
Share writes go to a temp shares.json. Nothing under /data2 is written.

  venv/bin/python content_check.py /tmp/content/server.py
"""
import ast
import os
import resource
import shutil
import sqlite3
import sys
import tempfile
import time

BASE = os.environ.get("ATRIHORD_DIR") or os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SERVER = sys.argv[1]
STAGE = os.path.dirname(os.path.abspath(SERVER))
sys.path.insert(0, BASE)
sys.path.insert(0, STAGE)            # the staged media_ai.py, not the deployed one
tmp = tempfile.mkdtemp(prefix="content_check_")
store = os.path.join(tmp, "ai")
os.makedirs(store)
REAL_STORE_DIR = None
import json                          # noqa: E402
with open(os.path.join(BASE, "config.json"), encoding="utf-8") as fh:
    REAL_STORE_DIR = (json.load(fh) or {}).get("ai_data_dir") or os.path.join(BASE, "ai_data")
os.environ["KSF_MEDIA_AI_DIR"] = store

import media_ai                      # noqa: E402

ok = True


def check(label, cond, detail=""):
    global ok
    ok = ok and bool(cond)
    print(("PASS " if cond else "FAIL ") + label + (f"  ({detail})" if detail and not cond else ""))


check("the staged media_ai.py is the one imported",
      os.path.dirname(os.path.abspath(media_ai.__file__)) == STAGE, media_ai.__file__)

# ------------------------------------------------------------ synthetic index
conn = media_ai.connect(tmp)
cols = {r[1] for r in conn.execute("PRAGMA table_info(media)")}
check("schema has media.trashed", "trashed" in cols, cols)
R = "downloads"
PIC = "twitter/@yoake_cos_/2091141486901694803_1.jpg"


def add(mid, root, path, trashed=None, tags=()):
    conn.execute("INSERT INTO media (id, root, path, kind, trashed) VALUES (?,?,?,?,?)",
                 (mid, root, path, "video" if path.endswith(".mp4") else "image", trashed))
    for tag, cat, p in tags:
        conn.execute("INSERT INTO media_tags (media_id, tag, category, p) VALUES (?,?,?,?)", (mid, tag, cat, p))


add(1, R, "a/general.jpg", tags=[("general", "rating", 0.9)])
add(2, R, "a/sensitive.jpg", tags=[("sensitive", "rating", 0.8)])
add(3, R, "a/both.jpg", tags=[("general", "rating", 0.6), ("sensitive", "rating", 0.55)])
add(4, R, "a/questionable.jpg", tags=[("questionable", "rating", 0.7)])
add(5, R, "a/explicit.jpg", tags=[("explicit", "rating", 0.95)])
add(6, R, "p/nipples.jpg", tags=[("sensitive", "rating", 0.8), ("nipples", "general", 0.7)])
add(7, R, "p/penis.jpg", tags=[("general", "rating", 0.7), ("penis", "general", 0.6)])
add(8, R, "p/weak.jpg", tags=[("sensitive", "rating", 0.8), ("nipples", "general", 0.4)])
add(9, R, "p/censored.jpg", tags=[("sensitive", "rating", 0.9), ("censored", "general", 0.9)])
add(10, R, "p/nude_unrated.jpg", tags=[("nude", "general", 0.8), ("1girl", "general", 0.9)])
add(11, R, "p/unrated.jpg", tags=[("1girl", "general", 0.9)])
add(12, R, "p/trashed.jpg", trashed=time.time(), tags=[("explicit", "rating", 0.9)])
add(13, "other", "a/general.jpg", tags=[("general", "rating", 0.9)])
add(14, R, "v/clip.mp4", tags=[("sensitive", "rating", 0.6), ("sex", "general", 0.55)])
add(15, R, "c/miku.jpg", tags=[("hatsune_miku", "character", 0.9), ("general", "rating", 0.9)])
add(16, R, PIC, tags=[("sensitive", "rating", 0.8), ("nipples", "general", 0.3)])
conn.commit()
EXPECTED = {(R, "a/general.jpg"): 0, (R, "a/sensitive.jpg"): 1, (R, "a/both.jpg"): 1, (R, "a/questionable.jpg"): 2,
            (R, "a/explicit.jpg"): 3, (R, "p/nipples.jpg"): 2, (R, "p/penis.jpg"): 3, (R, "p/weak.jpg"): 1,
            (R, "p/censored.jpg"): 1, (R, "p/nude_unrated.jpg"): 2, ("other", "a/general.jpg"): 0,
            (R, "v/clip.mp4"): 3, (R, "c/miku.jpg"): 0, (R, PIC): 1}
COUNTS = {"safe": 3, "suggestive": 5, "explicit": 6}

got = media_ai.content_levels(conn)
check("levels: highest rating, lifted by confident nudity/sex tags; weak, censored, unrated, trashed are not",
      got == EXPECTED, sorted(set(got.items()) ^ set(EXPECTED.items())))
check("one root string shared by all its keys", len({id(k[0]) for k in got if k[0] == R}) == 1)
lvl, ev = media_ai.content_evidence(conn, R, "p/nipples.jpg")
check("evidence: level, strongest first", lvl == 2 and [(e["tag"], e["kind"], e["level"]) for e in ev]
      == [("nipples", "general", 2), ("sensitive", "rating", 1)], (lvl, ev))
check("evidence: a marker under 0.5 is not evidence",
      [e["tag"] for e in media_ai.content_evidence(conn, R, "p/weak.jpg")[1]] == ["sensitive"])
check("evidence: unrated and unknown files", media_ai.content_evidence(conn, R, "p/unrated.jpg") == (None, [])
      and media_ai.content_evidence(conn, R, "nope.jpg") == (None, []))
check("evidence: trashed row still answers for its path (the route only asks for files on disk)",
      media_ai.content_evidence(conn, R, "p/trashed.jpg")[0] == 3)

# ------------------------------------------------------------ server, out of its AST
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


check("server imports the same staged media_ai", ns.get("media_ai") is media_ai)
store_names = sorted({n.id for fn in ("_read_shares_unlocked", "_write_shares_unlocked")
                      for n in ast.walk(defs[fn]) if isinstance(n, ast.Name)
                      and (n.id.endswith("_FILE") or n.id.endswith("_PATH"))})
for name in store_names:
    ns[name] = os.path.join(tmp, "shares.json")
if os.path.exists(os.path.join(BASE, "config.json")):
    shutil.copy(os.path.join(BASE, "config.json"), os.path.join(tmp, "config.json"))
ns["CONFIG_FILE"] = os.path.join(tmp, "config.json")
ns["USERS_FILE"] = os.path.join(tmp, "users.json")
ns["FAVORITES_FILE"] = os.path.join(tmp, "favorites.json")
ADMIN = {"username": "alice-harness", "role": "admin"}
ns["get_authenticated_user"] = lambda: ADMIN
for name in ("CONFIG", "_read_roots_unlocked", "app", "DEFAULT_ROOT_ID", "BASE_DIR", "CONTENT_BUCKETS",
             "CONTENT_OPTIONS", "CONTENT_BUCKET_OF", "CONTENT_REFRESH_S", "_content_cache", "_content_lock",
             "_content_build_lock", "parse_content", "content_matches", "_build_content_levels",
             "_refresh_content_levels", "content_levels_map", "content_counts", "content_info",
             "parse_media_filters", "filters_active", "apply_media_filters", "media_filter_options",
             "search_media", "media_info", "create_query_share", "_compute_query_share",
             "QUERY_SHARE_FILTER_KEYS", "_query_share_cache", "_query_share_lock", "resolve_media_path"):
    load(name)
app = ns["app"]


def call(fn, path, method="GET", body=None, query=None, args=()):
    with app.test_request_context(path, method=method, json=body, query_string=query):
        resp = resolve(lambda: ns[fn](*args))
        status = 200
        if isinstance(resp, tuple):
            resp, status = resp[0], resp[1]
        else:
            status = resp.status_code
        data = resp.get_json(silent=True) if getattr(resp, "mimetype", "") == "application/json" else None
    return status, data


pc = lambda raw: resolve(lambda: ns["parse_content"](raw))
got = [pc("safe"), pc("safe,suggestive"), pc("Explicit"), pc(" explicit , bogus"), pc("bogus"), pc(""), pc(None)]
check("parse_content", got == [frozenset({0}), frozenset({0, 1}), frozenset({2, 3}), frozenset({2, 3}), None, None, None], got)
f = resolve(lambda: ns["parse_media_filters"]({"content": "safe"}))
check("parse_media_filters appends content, alone it is an active filter",
      len(f) == 8 and f[-1] == frozenset({0}) and resolve(lambda: ns["filters_active"](f)), f)
check("no filters: eight Nones", resolve(lambda: ns["parse_media_filters"]({})) == (None,) * 8)
check("share keys line up with the tuple", len(ns["QUERY_SHARE_FILTER_KEYS"]) == 8
      and ns["QUERY_SHARE_FILTER_KEYS"][-1] == "content")

# ------------------------------------------------------------ the level map
cache, blds = ns["_content_cache"], {"n": 0}
real_levels = media_ai.content_levels


def counting(c):
    blds["n"] += 1
    return real_levels(c)


media_ai.content_levels = counting


def reset_cache():
    cache.update(levels=None, counts=None, built_at=0.0)


def wait_build_free(limit=15):
    end = time.time() + limit
    while ns["_content_build_lock"].locked() and time.time() < end:
        time.sleep(0.02)
    return not ns["_content_build_lock"].locked()


clm = lambda wait=True: resolve(lambda: ns["content_levels_map"](wait))
reset_cache()
check("before any build, wait=False answers None", clm(False) is None)
check("... and starts a background build", wait_build_free() and cache["levels"] == EXPECTED and blds["n"] == 1)
check("counts per bucket", resolve(lambda: ns["content_counts"]()) == COUNTS, cache["counts"])
reset_cache()
first = clm()
check("a filtered listing waits for the first build", first == EXPECTED and blds["n"] == 2)
check("a fresh map is reused, not rebuilt", clm() is first and blds["n"] == 2)
cache["built_at"] = time.time() - ns["CONTENT_REFRESH_S"] - 1
stale = clm()
check("a stale map is answered at once", stale is first)
check("... while one background rebuild replaces it", wait_build_free() and blds["n"] == 3 and cache["levels"] is not first
      and cache["levels"] == EXPECTED)


def failing(c):
    raise sqlite3.OperationalError("database is locked")


media_ai.content_levels = failing
reset_cache()
check("a failed first build hides everything rather than guessing", clm() == {} and cache["levels"] is None)
media_ai.content_levels = counting
reset_cache()
clm()

# ------------------------------------------------------------ apply to entries
entries = [{"name": "general.jpg", "path": "a/general.jpg", "root": R}, {"path": "a/sensitive.jpg"},
           {"path": "p/nipples.jpg", "root": R}, {"path": "p/unrated.jpg", "root": R},
           {"path": "a/general.jpg", "root": "other"}, {"path": "v/clip.mp4", "root": R}]


def keep(query):
    fl = resolve(lambda: ns["parse_media_filters"](query))
    return [(e.get("root"), e["path"]) for e in resolve(lambda: ns["apply_media_filters"]([dict(e) for e in entries], fl, R))]


check("safe", keep({"content": "safe"}) == [(R, "a/general.jpg"), ("other", "a/general.jpg")], keep({"content": "safe"}))
check("safe + suggestive (an entry without a root uses the listing's)",
      keep({"content": "safe,suggestive"}) == [(R, "a/general.jpg"), (None, "a/sensitive.jpg"), ("other", "a/general.jpg")])
check("explicit", keep({"content": "explicit"}) == [(R, "p/nipples.jpg"), (R, "v/clip.mp4")], keep({"content": "explicit"}))
check("explicit videos", keep({"content": "explicit", "kind": "video"}) == [(R, "v/clip.mp4")])
check("an unrated file matches no bucket", not any(p == "p/unrated.jpg" for _, p in keep({"content": "safe,suggestive,explicit"})))
check("no content filter leaves everything", len(keep({})) == len(entries))

# ------------------------------------------------------------ live shares
status, data = call("create_query_share", "/api/share/query", "POST",
                    {"type": "character", "tags": ["hatsune_miku"], "label": "harness", "filters": {"content": "safe,suggestive"}})
stored = json.load(open(os.path.join(tmp, "shares.json"))) if os.path.exists(os.path.join(tmp, "shares.json")) else []
check("a live share stores a content filter", status == 200 and stored
      and stored[-1].get("query", {}).get("filters") == {"content": "safe,suggestive"}, (status, data))
status, data = call("create_query_share", "/api/share/query", "POST",
                    {"type": "character", "tags": ["hatsune_miku"], "label": "harness", "filters": {"content": "bogus"}})
check("an unknown content value is refused", status == 400, (status, data))
ns["listed_media_paths"] = lambda: {(R, "c/miku.jpg"), (R, "a/general.jpg")}


def share_files(content):
    ns["_query_share_cache"].clear()
    share = {"id": "harness-query", "roots": [R],
             "query": {"type": "character", "tags": ["hatsune_miku"], "filters": {"content": content} if content else {}}}
    return resolve(lambda: ns["_compute_query_share"](share))[0]


check("share resolves with the content filter", share_files("safe") == [(R, "c/miku.jpg")] and share_files("explicit") == []
      and share_files(None) == [(R, "c/miku.jpg")], (share_files("safe"), share_files("explicit")))

# ------------------------------------------------------------ routes
status, data = call("media_filter_options", "/api/media/filters")
check("filter options offer the buckets and their counts", status == 200 and data.get("contents") == ns["CONTENT_OPTIONS"]
      and data.get("content_counts") == COUNTS, (status, data and data.get("content_counts")))
pic_full = resolve(lambda: ns["resolve_media_path"](R, PIC))
if pic_full and os.path.isfile(pic_full):
    status, data = call("media_info", "/api/media/info", query={"root": R, "path": PIC})
    c = (data or {}).get("content")
    check("info: a picture carries its content row with the evidence", status == 200 and c
          and c["bucket"] == "suggestive" and c["level"] == 1 and [e["tag"] for e in c["evidence"]] == ["sensitive"], (status, c))
    status, data = call("search_media", "/api/media/search", query={"q": "2091141486901694803_1", "content": "suggestive"})
    check("search keeps a file of the asked level", status == 200 and any(i["path"] == PIC for i in data["items"]),
          data and [i["path"] for i in data["items"]][:3])
    for content in ("safe", "explicit"):
        status, data = call("search_media", "/api/media/search", query={"q": "2091141486901694803_1", "content": content})
        check(f"search drops it for content={content} (total counts the filtered rows)",
              status == 200 and not any(i["path"] == PIC for i in data["items"]) and data["total"] == len(data["items"]),
              data and (data["total"], [i["path"] for i in data["items"]][:3]))
else:
    check("the real picture for the route checks exists", False, PIC)
media_ai.content_levels = real_levels

# ------------------------------------------------------------ the real index, read-only
ro = sqlite3.connect("file:%s?mode=ro" % os.path.join(REAL_STORE_DIR, media_ai.STORE_NAME), uri=True, timeout=60)
rss0 = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
t0 = time.time()
real = media_ai.content_levels(ro)
secs = time.time() - t0
rss1 = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
buckets = {k: sum(1 for v in real.values() if v in lv) for k, lv in ns["CONTENT_BUCKETS"].items()}
print("     real index: %d rated files in %.1fs, peak RSS +%d MB; %s" % (len(real), secs, (rss1 - rss0) // 1024, buckets))
check("the real build is complete and quick enough", len(real) > 400000 and secs < 120, (len(real), round(secs, 1)))
ro.close()

shutil.rmtree(tmp, ignore_errors=True)
print("ALL PASS" if ok else "SOME CHECKS FAILED")
