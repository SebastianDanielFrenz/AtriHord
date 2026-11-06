"""Shares overhaul checks of a STAGED server.py, run out of its AST in Flask
test requests. Expiry, view counting, PATCH and bulk run on synthetic shares in
a temp dir; the management listing runs on a temp COPY of the real shares.json
and reads the AI index. Share ids are never printed. Nothing under /data2 is
written.

  venv/bin/python shares_server_check.py /tmp/.../server.py
"""
import ast
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import time

BASE = os.environ.get("ATRIHORD_DIR") or os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SERVER = sys.argv[1]
sys.path.insert(0, BASE)
tmp = tempfile.mkdtemp(prefix="shares_check_")

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


ok = True


def check(label, cond, detail=""):
    global ok
    ok = ok and bool(cond)
    print(("PASS " if cond else "FAIL ") + label + (f"  ({detail})" if detail and not cond else ""))


# The share store's file constant(s), found in its own reader and writer, are
# pointed into the temp dir - after noting where the real file is.
store_names = sorted({n.id for fn in ("_read_shares_unlocked", "_write_shares_unlocked")
                      for n in ast.walk(defs[fn]) if isinstance(n, ast.Name)
                      and (n.id.endswith("_FILE") or n.id.endswith("_PATH"))})
if not store_names:
    sys.exit("no *_FILE name in the share store functions")
load(store_names[0])
REAL_SHARES = ns[store_names[0]]
SHARES = os.path.join(tmp, "shares.json")
for name in store_names:
    ns[name] = SHARES
if os.path.exists(os.path.join(BASE, "config.json")):
    shutil.copy(os.path.join(BASE, "config.json"), os.path.join(tmp, "config.json"))
ns["CONFIG_FILE"] = os.path.join(tmp, "config.json")
ns["USERS_FILE"] = os.path.join(tmp, "users.json")
ns["SHARE_STATS_FILE"] = os.path.join(tmp, "share_stats.json")
ADMIN = {"username": "alice", "role": "admin"}
GUEST = {"username": "guest1", "role": "guest"}
CURRENT = {"user": ADMIN}
ns["get_authenticated_user"] = lambda: CURRENT["user"]
load("CONFIG")
for name in ("_read_roots_unlocked", "SHARE_TAG_SQL", "_share_tag_clauses", "share_listed", "share_content_tags",
             "app", "shares_lock", "_read_shares_unlocked", "_write_shares_unlocked", "load_shares", "share_summary",
             "SSO_PROXY_IPS", "find_share", "find_share_including_expired", "public_share_info", "public_share_page",
             "update_share", "bulk_shares", "manage_shares", "create_share_pool", "share_category",
             "_character_tag_counts", "record_share_view", "flush_share_stats", "default_share_expiry",
             "_share_stats", "_share_tags_cache"):
    load(name)
app = ns["app"]


def call(fn, path, method="GET", body=None, args=()):
    with app.test_request_context(path, method=method, json=body):
        resp = resolve(lambda: ns[fn](*args))
    status = 200
    if isinstance(resp, tuple):
        resp, status = resp[0], resp[1]
    else:
        status = getattr(resp, "status_code", 200)
    return status, (resp.get_json() if hasattr(resp, "get_json") else resp)


# ---------------------------------------------------------------- categories
cases = [({"kind": "query", "query": {"type": "character"}}, "character"),
         ({"kind": "query", "query": {"type": "person"}}, "person"),
         ({"kind": "pool", "items": []}, "pool"),
         ({"kind": "folder", "path": "twitter/alice"}, "account"),
         ({"kind": "folder", "path": "twitter/alice/2024"}, "folder"),
         ({"kind": "folder", "path": "misc/stuff"}, "folder"),
         ({"kind": "file", "path": "a/b.MP4"}, "video"),
         ({"kind": "file", "path": "a/b.png"}, "image"),
         ({"kind": "file", "path": "a/b.flac"}, "music"),
         ({"kind": "file", "path": "a/b.zip"}, "file")]
got = [resolve(lambda s=s: ns["share_category"](s)) for s, _ in cases]
check("share_category by content", got == [c for _, c in cases], got)

mem = sqlite3.connect(":memory:")
mem.row_factory = sqlite3.Row
mem.executescript("""
CREATE TABLE media (id INTEGER PRIMARY KEY, root TEXT, path TEXT, trashed REAL, UNIQUE(root, path));
CREATE TABLE media_tags (media_id INTEGER, tag TEXT, category TEXT, PRIMARY KEY (media_id, tag));""")
mem.executemany("INSERT INTO media VALUES (?,?,?,?)", [
    (1, "downloads", "twitter/alice/1.png", None), (2, "downloads", "twitter/alice/sub/2.png", None),
    (3, "downloads", "twitter/alice2/3.png", None), (4, "downloads", "twitter/alice/4.png", 1.0),
    (5, "other", "twitter/alice/5.png", None), (6, "downloads", "twitter/alice_x/6.png", None)])
mem.executemany("INSERT INTO media_tags VALUES (?,?,?)", [
    (1, "miku", "character"), (2, "miku", "character"), (2, "rin", "character"), (3, "luka", "character"),
    (4, "kaito", "character"), (5, "meiko", "character"), (6, "gumi", "character"), (1, "1girl", "general")])
got = dict(resolve(lambda: ns["_character_tag_counts"](mem, [("downloads", "twitter/alice", True)])))
check("folder tags: own subtree only (not alice2/alice_x, trashed, other roots)", got == {"miku": 2, "rin": 1}, got)
got = dict(resolve(lambda: ns["_character_tag_counts"](mem, [("downloads", "twitter/alice2/3.png", False),
                                                             ("other", "twitter/alice/5.png", False)])))
check("file tags: exact paths", got == {"luka": 1, "meiko": 1}, got)

# ------------------------------------------------------------ default expiry
ns["CONFIG"]["shares"] = {"default_expiry_days": 7}
v = resolve(lambda: ns["default_share_expiry"]())
check("default expiry of 7 days", v and abs(v - (time.time() + 7 * 86400)) < 5, v)
ns["CONFIG"]["shares"] = {"default_expiry_days": 0}
check("default expiry 0 = never", resolve(lambda: ns["default_share_expiry"]()) is None)

# ---------------------------------------------------------------- synthetic
now = time.time()


def sid(i):
    return f"harness-share-{i:02d}-" + "x" * 16


synthetic = [
    {"id": sid(1), "kind": "file", "path": "twitter/alice/a.mp4", "created_by": "alice", "created_at": now - 50, "revoked": False},
    {"id": sid(2), "kind": "file", "path": "twitter/alice/b.png", "created_by": "alice", "created_at": now - 40, "revoked": False, "expires_at": now - 10},
    {"id": sid(3), "kind": "folder", "path": "twitter/alice", "created_by": "guest1", "created_at": now - 30, "revoked": False},
    {"id": sid(4), "kind": "pool", "label": "P", "items": [], "created_by": "alice", "created_at": now - 20, "revoked": False},
    {"id": sid(5), "kind": "file", "path": "x/y.zip", "created_by": "alice", "created_at": now - 10, "revoked": True, "revoked_at": now - 5},
]
resolve(lambda: ns["_write_shares_unlocked"](synthetic))
resolve(lambda: ns["share_summary"](synthetic[0]))                      # warm: names resolved once


def find(i):
    return resolve(lambda: ns["find_share"](sid(i)))


check("find_share: an active share", (find(1) or {}).get("id") == sid(1))
check("find_share: an expired share is gone", find(2) is None)
check("find_share: a revoked share is gone", find(5) is None)
check("find_share_including_expired still sees it", (resolve(lambda: ns["find_share_including_expired"](sid(2))) or {}).get("id") == sid(2))
status, data = call("public_share_info", "/api/public/share/x", args=(sid(2),))
check("public info of an expired link: 410 expired", status == 410 and data == {"error": "expired"}, (status, data))
status, data = call("public_share_info", "/api/public/share/x", args=("no-such-token",))
check("public info of an unknown link: 404", status == 404, status)

# --------------------------------------------------------------------- views
CHROME = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0 Safari/537.36"
if "SSO_PROXY_IPS" not in ns:
    load("SSO_PROXY_IPS")


def page(i, ua, xff=None, remote=None, method="GET"):
    headers = {"User-Agent": ua}
    if xff:
        headers["X-Forwarded-For"] = xff
    remote = remote or sorted(ns["SSO_PROXY_IPS"])[0]
    with app.test_request_context("/share/x", method=method, headers=headers, environ_base={"REMOTE_ADDR": remote}):
        try:
            resolve(lambda: ns["public_share_page"](sid(i)))
        except Exception as exc:
            print(f"     (page render raised after counting: {type(exc).__name__}: {exc})")


page(1, CHROME, xff="192.0.2.200")                                    # warm-up, then forget it
ns["_share_stats"].update(data={}, seen={}, dirty=False, loaded=True)
page(1, CHROME, xff="203.0.113.5")
page(1, CHROME, xff="203.0.113.5")
page(1, CHROME, xff="198.51.100.7, 10.0.0.1")
page(1, "Mozilla/5.0 (compatible; Discordbot/2.0; +https://discordapp.com)", xff="203.0.113.9")
page(1, "WhatsApp/2.23.20.0", xff="203.0.113.9")
page(1, CHROME, xff="203.0.113.5", method="HEAD")
page(2, CHROME, xff="203.0.113.5")                                    # expired: not counted
page(1, CHROME, xff="192.0.2.1", remote="192.0.2.50")                 # not the proxy: its XFF is ignored
page(1, CHROME, xff="192.0.2.99", remote="192.0.2.50")
stats = ns["_share_stats"]["data"]
e = stats.get(sid(1), {})
check("views: GET page loads only, link previews apart", e.get("views") == 5 and e.get("previews") == 2, e)
check("visitors: distinct per day; X-Forwarded-For only from the proxy", e.get("visitors") == 3, e)
check("an expired link's page is not counted", sid(2) not in stats)
check("last viewed and per-day counts kept", e.get("last_viewed_at") and sum((e.get("days") or {}).values()) == 5, e)
wrote = resolve(lambda: ns["flush_share_stats"]())
raw = open(ns["SHARE_STATS_FILE"]).read()
check("flush writes the counts", wrote and json.loads(raw)[sid(1)]["views"] == 5)
check("nothing identifying is written", not any(x in raw for x in ("203.0.113", "198.51", "192.0.2", "Mozilla", "Discord")))
check("a flush with nothing new writes nothing", resolve(lambda: ns["flush_share_stats"]()) is False)

# --------------------------------------------------------------------- PATCH
status, data = call("update_share", "/api/share/x", "PATCH", {"label": "  Holiday clip ", "expires_at": now + 86400}, args=(sid(1),))
check("PATCH label and expiry", status == 200 and data["label"] == "Holiday clip"
      and abs(data["expires_at"] - (now + 86400)) < 1 and data["status"] == "active", (status, data))
status, data = call("update_share", "/api/share/x", "PATCH", {"label": ""}, args=(sid(1),))
check("an empty label falls back to the default name", status == 200 and data["label"] == "a.mp4", data)
bad = [call("update_share", "/api/share/x", "PATCH", body, args=(sid(1),))[0]
       for body in ({"expires_at": "tomorrow"}, {"expires_at": True}, {"expires_at": -5},
                    {"expires_at": now + 30 * 365 * 86400}, {"label": 5}, {"label": "x" * 121}, {})]
check("PATCH rejects bad values", bad == [400] * 7, bad)
check("PATCH of an unknown share: 404", call("update_share", "/api/share/x", "PATCH", {"label": "y"}, args=("nope",))[0] == 404)
status, data = call("update_share", "/api/share/x", "PATCH", {"expires_at": None}, args=(sid(2),))
check("clearing an expiry revives the link", status == 200 and data["status"] == "active" and find(2), (status, data))
status, data = call("update_share", "/api/share/x", "PATCH", {"expires_at": time.time()}, args=(sid(4),))
check("'expire now' ends the link", status == 200 and data["status"] == "expired" and find(4) is None, (status, data))
CURRENT["user"] = GUEST
check("a guest cannot change someone else's share", call("update_share", "/api/share/x", "PATCH", {"label": "z"}, args=(sid(1),))[0] == 403)
check("a guest can change their own share", call("update_share", "/api/share/x", "PATCH", {"label": "mine"}, args=(sid(3),))[0] == 200)
check("a guest cannot use bulk", call("bulk_shares", "/api/share/bulk", "POST", {"ids": [sid(1)], "action": "revoke"})[0] == 403)
check("a guest cannot list for management", call("manage_shares", "/api/share/manage")[0] == 403)
CURRENT["user"] = ADMIN

# ---------------------------------------------------------------------- bulk
call("bulk_shares", "/api/share/bulk", "POST", {"ids": ["warm-up-no-such-id"], "action": "revoke"})
exp = time.time() + 3600
status, data = call("bulk_shares", "/api/share/bulk", "POST", {"ids": [sid(1), sid(3)], "action": "expiry", "expires_at": exp})
shares_now = {s["id"]: s for s in resolve(lambda: ns["load_shares"]())}
check("bulk expiry", status == 200 and data["changed"] == 2
      and all(abs(shares_now[sid(i)]["expires_at"] - exp) < 1 for i in (1, 3)), (status, data))
status, data = call("bulk_shares", "/api/share/bulk", "POST", {"ids": [sid(1), sid(5)], "action": "revoke"})
shares_now = {s["id"]: s for s in resolve(lambda: ns["load_shares"]())}
check("bulk revoke (an already revoked one is not counted)", status == 200 and data["changed"] == 1
      and shares_now[sid(1)]["revoked"] and shares_now[sid(1)].get("revoked_at"), (status, data))
bad = [call("bulk_shares", "/api/share/bulk", "POST", body)[0] for body in (
    {"ids": "x", "action": "revoke"}, {"ids": [], "action": "revoke"}, {"ids": [sid(1)], "action": "delete"},
    {"ids": [sid(1)], "action": "expiry", "expires_at": "soon"}, {"ids": [1, 2], "action": "revoke"})]
check("bulk rejects bad requests", bad == [400] * 5, bad)

# ------------------------------------------------------- new share defaults
ns["CONFIG"]["shares"] = {"default_expiry_days": 30}
status, data = call("create_share_pool", "/api/share/pool", "POST", {"label": "Harness pool"})
check("a new pool gets the default expiry", status == 200 and data.get("expires_at")
      and abs(data["expires_at"] - (time.time() + 30 * 86400)) < 10, (status, data))
ns["CONFIG"]["shares"] = {"default_expiry_days": 0}

adapter = app.url_map.bind("localhost")
check("routes: manage and bulk are not read as share ids",
      adapter.match("/api/share/manage", method="GET")[0] == "manage_shares"
      and adapter.match("/api/share/bulk", method="POST")[0] == "bulk_shares"
      and adapter.match("/api/share/abc", method="PATCH")[0] == "update_share")

# ------------------------------------------- management list of REAL shares
shutil.copy(REAL_SHARES, SHARES)
real = [s for s in resolve(lambda: ns["load_shares"]()) if resolve(lambda s=s: ns["share_listed"](s))]
ns["_share_tags_cache"].clear()
t0 = time.time()
for s in real:
    resolve(lambda s=s: ns["share_content_tags"](s))
tags_s = time.time() - t0
t0 = time.time()
for s in real:
    resolve(lambda s=s: ns["share_summary"](s))
print("     content tags %.2fs, summaries %.2fs over %d shares" % (tags_s, time.time() - t0, len(real)))
ns["_share_tags_cache"].clear()
t0 = time.time()
status, data = call("manage_shares", "/api/share/manage")
first = time.time() - t0
t0 = time.time()
call("manage_shares", "/api/share/manage")
cached = time.time() - t0
rows = data["shares"] if status == 200 else []
counts = {c["key"]: c["count"] for c in data.get("categories", [])} if status == 200 else {}
check("management list of the real shares", status == 200 and rows and sum(counts.values()) == len(rows)
      and all(r["category"] in counts for r in rows), (status, counts))
check("every row has views, status, expiry and detail",
      all({"views", "visitors", "previews", "last_viewed_at", "status", "expires_at", "detail", "content_tags"} <= set(r) for r in rows))
check("listing is quick enough (first < 8 s, cached < 1 s)", first < 8 and cached < 1, (round(first, 2), round(cached, 2)))
print("     %d listed in %.2fs (cached %.2fs); categories %s" % (len(rows), first, cached, {k: v for k, v in counts.items() if v}))
for cat in ("character", "account", "folder", "pool", "image", "video"):
    tagged = [r["content_tags"] for r in rows if r["category"] == cat]
    if tagged:
        print("     %-9s %2d, %2d with characters, e.g. %s" % (cat, len(tagged), sum(1 for t in tagged if t),
                                                          [t for t in tagged if t][:2]))
people = [r["content_tags"] for r in rows if r["category"] == "person"]
print("     person    %2d, %d named" % (len(people), sum(1 for t in people if t and t != ["unnamed person"])))

conn = ns["media_ai"].connect(ns["BASE_DIR"])
for label, parts in (("folder", [("downloads", "twitter/someone", True)]),
                     ("file", [("downloads", "twitter/someone/1.png", False)]),
                     ("pool", [("downloads", "a/b.png", False), ("downloads", "twitter/x", True)])):
    clauses, args = resolve(lambda: ns["_share_tag_clauses"](parts))
    plan = [r[3] for r in conn.execute(
        "EXPLAIN QUERY PLAN " + ns["SHARE_TAG_SQL"].format(clauses=" OR ".join(clauses)), args)]
    check(f"{label} tag query starts from the path index, not the tag index",
          plan and "media_tags_tag" not in " ".join(plan) and any("sqlite_autoindex_media" in p for p in plan), plan)

shutil.rmtree(tmp, ignore_errors=True)
print("ALL PASS" if ok else "SOME CHECKS FAILED")
