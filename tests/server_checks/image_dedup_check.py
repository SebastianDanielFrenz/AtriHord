"""Exact-copy image de-duplication: image_dedup.py's decisions and file operations
on a temp tree (walk, hash cache, which name stays, which inode all names share,
the refusals), then server.py's passes out of a STAGED server.py's AST over a temp
library with real files, hard links, the real trash with its bans (0-byte
tombstones, the empty-file registry, manifests), favourites and share links,
a temp AI index, the real trash restore route, the library endpoint (dry run by
default) and the check after a download (and its place after the stitch).

  venv/bin/python image_dedup_check.py <staged server.py>
image_dedup.py (and the other modules server.py imports) are taken from beside it.
On a server.py without the feature the module checks pass and the server checks FAIL.
"""
import ast
import atexit
import json
import os
import random
import shutil
import sys
import tempfile
import time

BASE = os.environ.get("ATRIHORD_DIR") or os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SERVER = os.path.abspath(sys.argv[1])
STAGE = os.path.dirname(SERVER)
sys.path.insert(0, BASE)
sys.path.insert(0, STAGE)
tmp = tempfile.mkdtemp(prefix="imgdedup_check_")
atexit.register(shutil.rmtree, tmp, True)
os.environ["KSF_MEDIA_AI_DIR"] = os.path.join(tmp, "ai")
os.makedirs(os.environ["KSF_MEDIA_AI_DIR"])
import image_dedup as idd        # noqa: E402
import media_ai                  # noqa: E402

assert os.path.dirname(os.path.abspath(idd.__file__)) == STAGE, idd.__file__
ok = True


def check(label, cond, detail=""):
    global ok
    ok = ok and bool(cond)
    print(("PASS " if cond else "FAIL ") + label + (f"  ({detail})" if detail and not cond else ""), flush=True)


rng = random.Random(7)


def blob(n):
    return rng.randbytes(n)


def put(root, rel, data, mtime=None):
    path = os.path.join(root, *rel.split("/"))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(data)
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


def snapshot(root):
    out = {}
    for b, _d, fs in os.walk(root):
        for n in fs:
            st = os.lstat(os.path.join(b, n))
            out[os.path.relpath(os.path.join(b, n), root)] = (st.st_ino, st.st_size, st.st_mtime_ns, st.st_nlink)
    return out


def ino(root, rel):
    return os.lstat(os.path.join(root, *rel.split("/"))).st_ino


# =================================================================== the module
m = os.path.join(tmp, "mod")
A, B, C = blob(5000), blob(5000), blob(7000)         # A and B: one size, different bytes
put(m, "twitter/@a/10_1.jpg", A, 1000)
put(m, "twitter/@a/20_1.jpg", A, 2000)               # a copy in the same folder
put(m, "twitter/@a/30_1.jpg", B, 3000)               # same size, other bytes
put(m, "twitter/@b/40_1.jpg", A, 500)                # an older copy elsewhere
put(m, "twitter/@b/50_1.jpg", C, 4000)
put(m, "twitter/@b/.hidden.jpg", A, 1)
put(m, "twitter/@b/60_1.jpg.part", A, 1)
put(m, "twitter/@b/70_1.txt", A, 1)
put(m, "twitter/@b/80_1-poster.jpg", A, 1)
put(m, "twitter/@b/90_1.jpg", b"", 1)                # a tombstone
put(m, "twitter/@b/.dedup-abcd", A, 1)               # a stopped pass's temporary name
put(m, "twitter/.trash/x/10_1.jpg", A, 1)            # no dot folder is entered
put(m, "youtube/t/1.jpg", A, 1)                      # not a category in scope
os.symlink(os.path.join(m, "twitter/@a/10_1.jpg"), os.path.join(m, "twitter/@b/link.jpg"))
wstats = {}
got = sorted(e.rel for e in idd.walk(m, stats=wstats))
check("walk: pictures of the scoped folders only - no dot folder, hidden/partial/poster/text file, tombstone, symlink or foreign folder",
      got == ["twitter/@a/10_1.jpg", "twitter/@a/20_1.jpg", "twitter/@a/30_1.jpg", "twitter/@b/40_1.jpg",
              "twitter/@b/50_1.jpg"], got)
check("... counts the tombstone and finds the temporary name", wstats.get("tombstones") == 1
      and wstats.get("temps") == ["twitter/@b/.dedup-abcd"], wstats)

store = idd.Store(os.path.join(tmp, "mod.sqlite"))
entries = list(idd.walk(m))
reads = []
real_sha = idd.sha256_of
idd.sha256_of = lambda p: (reads.append(p), real_sha(p))[1]
hasher = idd.Hasher(store, m)
plan = idd.build_plan(entries, hasher)
check("only files whose size another file shares are read (the 7000-byte file is not)",
      sorted(os.path.basename(p) for p in reads) == ["10_1.jpg", "20_1.jpg", "30_1.jpg", "40_1.jpg"], reads)
(g,) = plan["groups"]
check("one group: the oldest copy (mtime) is the file the names end up sharing", g["canon"].rel == "twitter/@b/40_1.jpg",
      g["canon"].rel)
check("... the same-folder copy is trashed against the OLDER name of its folder",
      [(e.rel, k.rel) for e, k in g["trash"]] == [("twitter/@a/20_1.jpg", "twitter/@a/10_1.jpg")], g["trash"])
check("... the surviving name in the other folder becomes a link; the trashed one is linked first too",
      [e.rel for e in g["links"]] == ["twitter/@a/10_1.jpg"] and [e.rel for e in g["trash_links"]] == ["twitter/@a/20_1.jpg"])
check("... and the bytes saved are the inodes that go (3 copies -> 1)", g["saved"] == 2 * 5000, g["saved"])
check("different bytes of one size are not a group", all("30_1.jpg" not in e.rel for e in
      [g["canon"]] + g["links"] + [x for x, _k in g["trash"]]))
before = len(reads)
idd.build_plan(entries, idd.Hasher(store, m))
check("a second plan reads nothing: the hashes are remembered per inode", len(reads) == before, len(reads) - before)
st0 = os.stat(os.path.join(m, "twitter/@a/20_1.jpg"))
os.utime(os.path.join(m, "twitter/@a/20_1.jpg"), (st0.st_atime, st0.st_mtime + 5))
changed = [e for e in idd.walk(m)]
idd.build_plan(changed, idd.Hasher(store, m))
check("... and a file whose mtime changed is read again", len(reads) == before + 1, len(reads) - before)
idd.sha256_of = real_sha

# big files: a digest of the first and last 64 KiB settles most same-size look-alikes without reading them whole
big = os.path.join(tmp, "big")
BIG = blob(300_000)
head_other = bytes([BIG[0] ^ 1]) + BIG[1:]
mid_other = BIG[:150_000] + bytes([BIG[150_000] ^ 1]) + BIG[150_001:]
put(big, "twitter/@a/1_1.jpg", BIG, 10)
put(big, "twitter/@b/2_1.jpg", head_other, 20)               # differs in the first bytes
put(big, "twitter/@c/3_1.jpg", mid_other, 30)                # differs only in the middle
put(big, "twitter/@d/4_1.jpg", BIG, 40)                      # identical
bstore = idd.Store(os.path.join(tmp, "big.sqlite"))
bh = idd.Hasher(bstore, big)
bplan = idd.build_plan(list(idd.walk(big)), bh)
check("big files: four of one size cost four quick digests, and only the three that agree on both ends are read whole",
      bh.quick_files == 4 and bh.files == 3, (bh.quick_files, bh.files))
check("... the group is the two identical files; the one differing in the middle is not in it",
      len(bplan["groups"]) == 1 and bplan["groups"][0]["canon"].rel == "twitter/@a/1_1.jpg"
      and [e.rel for e in bplan["groups"][0]["links"]] == ["twitter/@d/4_1.jpg"], bplan["groups"] and bplan["groups"][0]["links"])
bh2 = idd.Hasher(bstore, big)
idd.build_plan(list(idd.walk(big)), bh2)
check("... and a second plan reads nothing", bh2.quick_files == 0 and bh2.files == 0 and bh2.cached >= 4, (bh2.quick_files, bh2.files))
bstore.close()

# which name stays
mk = lambda rel, mt, ino=1, nl=1: idd.Entry(rel, 10, mt, 1, ino, nl)
check("keep: the lower post id stays, whatever the mtime",
      min([mk("t/@a/2095_1.jpg", 9), mk("t/@a/2099_2.jpg", 1)], key=idd.keep_key).rel == "t/@a/2095_1.jpg")
check("keep: a name without an id loses to one with an id; two without: the older mtime",
      min([mk("t/@a/photo.jpg", 1), mk("t/@a/12_1.jpg", 9)], key=idd.keep_key).rel == "t/@a/12_1.jpg"
      and min([mk("t/@a/b.jpg", 5), mk("t/@a/a.jpg", 7)], key=idd.keep_key).rel == "t/@a/b.jpg")
check("post_token: the digits a name starts with", idd.post_token("t/@a/123_p0.jpg") == "123"
      and idd.post_token("t/@a/cover.jpg") is None)

# relink: the checks before a name is replaced
r = os.path.join(tmp, "rel")
pa = put(r, "twitter/@a/1_1.jpg", A, 100)
pb = put(r, "twitter/@b/2_1.jpg", A, 200)
pc = put(r, "twitter/@c/3_1.jpg", B, 200)             # the size of A, other bytes
ents = {e.rel: e for e in idd.walk(r)}
canon = ents["twitter/@a/1_1.jpg"]
try:
    idd.relink(r, canon, ents["twitter/@c/3_1.jpg"])
    check("relink refuses different bytes of one size", False)
except idd.Refused as exc:
    check("relink refuses different bytes of one size (the data under the old inode would be lost)",
          "differ" in str(exc) and os.path.getsize(pc) == 5000 and open(pc, "rb").read() == B, str(exc))
os.utime(pb, (300, 300))
try:
    idd.relink(r, canon, ents["twitter/@b/2_1.jpg"])
    check("relink refuses a file changed since the plan", False)
except idd.Refused as exc:
    check("relink refuses a file whose mtime changed since the plan", "changed" in str(exc) and ino(r, "twitter/@b/2_1.jpg") != ino(r, "twitter/@a/1_1.jpg"), str(exc))
ents = {e.rel: e for e in idd.walk(r)}
check("relink: the name now is the file (atomic, no temporary name left)",
      idd.relink(r, ents["twitter/@a/1_1.jpg"], ents["twitter/@b/2_1.jpg"]) is True
      and ino(r, "twitter/@b/2_1.jpg") == ino(r, "twitter/@a/1_1.jpg")
      and not [f for f in os.listdir(os.path.dirname(pb)) if f.startswith(idd.TEMP_PREFIX)]
      and os.stat(pb).st_mtime_ns == os.stat(pa).st_mtime_ns)
ents = {e.rel: e for e in idd.walk(r)}
check("... done again: already one file, nothing to do", idd.relink(r, ents["twitter/@a/1_1.jpg"], ents["twitter/@b/2_1.jpg"]) is False)
store.exempt_add(["twitter/@a/x.jpg"])
check("the exempt list is kept", store.exempt_all() == {"twitter/@a/x.jpg"})
store.close()

# =================================================================== the server
check("server.py has the library pass, the download check, the restore exemption and the endpoint",
      all(n in open(SERVER, encoding="utf-8").read() for n in
          ("def run_image_dedup_library", "def image_dedup_after_download", "def image_dedup_exempt",
           "/api/images/dedup", "image_dedup.enabled", "IMGDEDUP_REASON")))
src = open(SERVER, encoding="utf-8").read()
tree = ast.parse(src)
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
    for _ in range(600):
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


def source_of(name):
    return ast.get_source_segment(src, defs[name]) if name in defs else ""


hook = [ast.get_source_segment(src, n) or "" for n in tree.body if isinstance(n, ast.FunctionDef)
        and n.name != "stitch_after_download" and "stitch_after_download(new_paths" in (ast.get_source_segment(src, n) or "")]
check("a finished download is followed by the check, after the stitch",
      len(hook) == 1 and "image_dedup_enabled()" in hook[0] and "then=dedup_next" in hook[0]
      and "image_dedup_after_download(new_paths)" in hook[0])
check("the settings list has the switch, the config default it and load_config keeps it",
      '"key": "image_dedup.enabled"' in src and '"image_dedup": {"enabled": True}' in src
      and '"twitter_stitch", "image_dedup"' in src)

lib = os.path.join(tmp, "lib")
R = "downloads"
ADMIN = {"username": "alice", "role": "admin"}
ns.update(BASE_DIR=tmp, DOWNLOADS_DIR=lib, DEFAULT_ROOT_ID=R, CONFIG={"twitter_stitch": {"enabled": True}, "image_dedup": {"enabled": True}},
          get_root=lambda root_id: {"id": R, "path": lib, "name": "Downloads"} if (root_id or R) == R else None,
          get_all_roots=lambda: [{"id": R, "path": lib, "name": "Downloads"}],
          roots_for_user=lambda user: [{"id": R, "path": lib, "name": "Downloads"}],
          get_authenticated_user=lambda: ADMIN,
          invalidate_media_caches=lambda: None,
          discovery_service=lambda: type("D", (), {"invalidate": lambda self: None})())
try:
    for name in ("app", "TRASH_DIR_NAME", "DELETE_BAN", "DELETE_RELEASE", "GALLERY_DL_CATEGORIES", "EMPTY_FILES_STORE",
                 "_empty_files", "_empty_files_lock", "_empty_files_loaded", "_save_empty_files", "note_empty_files",
                 "FAVORITES_FILE", "_favorites_lock", "_favorites", "SHARES_FILE", "shares_lock", "DEFAULT_CONFIG"):
        if name not in ns and name in defs:
            load(name)
    ns["EMPTY_FILES_STORE"] = os.path.join(tmp, "empty_files.json")
    ns["FAVORITES_FILE"] = os.path.join(tmp, "favorites.json")
    ns["SHARES_FILE"] = os.path.join(tmp, "shares.json")
    missing = [n for n in ("run_image_dedup_library", "image_dedup_after_download", "image_dedup_status",
                           "image_dedup_exempt", "image_dedup_apply") if n not in defs]
    if missing:
        raise RuntimeError("not in this server.py: " + ", ".join(missing))
    for name in ("run_image_dedup_library", "image_dedup_after_download", "image_dedup_status", "image_dedup_exempt",
                 "stitch_after_download", "restore_trash", "set_favorite", "user_favorites", "_read_shares_unlocked",
                 "trash_resolved", "kinds_of", "_is_hidden_or_partial"):
        preload(name)
    PATCHED = True
except Exception as exc:
    PATCHED = False
    check("server.py's image de-duplication loads", False, repr(exc))

if PATCHED:
    check("the default config switches the after-download check on", ns["DEFAULT_CONFIG"].get("image_dedup") == {"enabled": True})
    conn = media_ai.connect(tmp)

    def ai_row(path, trashed=False):
        cur = conn.execute("INSERT INTO media (root, path, size, kind, ino, trashed) VALUES (?, ?, 1, 'image', ?, ?)",
                           (R, path, "9:old-" + path, 1.0 if trashed else None))
        conn.commit()
        return cur.lastrowid

    X, Y, W, V = blob(40000), blob(30000), blob(25000), blob(20000)
    Z1, Z2 = blob(15000), blob(15000)
    T0 = 1_600_000_000
    lay = [("twitter/@a/1001_1.jpg", X, T0), ("twitter/@a/1005_2.jpg", X, T0 + 100_000),
           ("twitter/@a/1010_1.jpg", Y, T0 + 10), ("twitter/@a/1020_1.jpg", Z1, T0), ("twitter/@a/1021_1.jpg", Z2, T0),
           ("twitter/@b/2001_1.jpg", X, T0 + 50_000), ("pixiv/artist/50_p0.jpg", X, T0 - 1_000_000),
           ("pixiv/artist/60_p0.jpg", X, T0 + 7), ("youtube/thumb/x.jpg", X, T0 - 5),
           ("twitter/@c/3001_1.jpg", W, T0), ("twitter/@c/3002_1.jpg", W, T0 + 5),
           ("twitter/@d/4001_1.jpg", Y, T0 + 20), ("twitter/@d/.hidden.jpg", X, T0), ("twitter/@d/9000_1.jpg", b"", T0),
           ("twitter/@g/8001_1.jpg", V, T0), ("twitter/@g/8001_2.jpg", V, T0 + 1)]
    for rel, data, mt in lay:
        put(lib, rel, data, mt)
    # already links: across folders (nothing to do) and inside one folder (a name too many)
    L = blob(12000)
    put(lib, "twitter/@e/6002_1.jpg", L, T0)
    os.makedirs(os.path.join(lib, "twitter/@e2"))
    os.link(os.path.join(lib, "twitter/@e/6002_1.jpg"), os.path.join(lib, "twitter/@e2/6001_1.jpg"))
    K = blob(11000)
    put(lib, "twitter/@f/7001_1.jpg", K, T0)
    os.link(os.path.join(lib, "twitter/@f/7001_1.jpg"), os.path.join(lib, "twitter/@f/7002_1.jpg"))
    for rel in ("twitter/@a/1005_2.jpg", "twitter/@b/2001_1.jpg", "twitter/@d/4001_1.jpg"):
        ai_row(rel)
    ai_row("twitter/@a/1001_1.jpg")
    json.dump({"alice": {R: {"twitter/@a/1005_2.jpg": 1.0}}, "carol": {R: {"twitter/@c/3002_1.jpg": 2.0}}},
              open(ns["FAVORITES_FILE"], "w"))
    json.dump([{"id": "s1", "kind": "file", "root": R, "path": "twitter/@a/1005_2.jpg"}], open(ns["SHARES_FILE"], "w"))
    client = ns["app"].test_client()

    def api(method, url, body=None):
        rv = client.open(url, method=method, json=body)
        return rv.status_code, (rv.get_json(silent=True) or {})

    def wait(key="library", timeout=120):
        end = time.time() + timeout
        while time.time() < end:
            time.sleep(0.05)
            if not ns["_idd"]["running"] and ns["_idd"]["finished"] and key in ns["_idd_last"]:
                return ns["_idd"]
        raise RuntimeError("pass did not finish")

    # ---- a dry run changes nothing ------------------------------------------------------------
    snap0, fav0, shares0 = snapshot(lib), open(ns["FAVORITES_FILE"]).read(), open(ns["SHARES_FILE"]).read()
    code, body = api("POST", "/api/images/dedup")
    check("POST without a body starts a DRY RUN", code == 200 and body.get("started") is True and body.get("dry_run") is True, (code, body))
    st = wait("library-dry")
    rep = st["report"]
    check("dry run: nothing in the library changed - no inode, name, mtime, trash batch, favourite or share",
          snapshot(lib) == snap0 and not os.path.exists(os.path.join(lib, ".trash"))
          and open(ns["FAVORITES_FILE"]).read() == fav0 and open(ns["SHARES_FILE"]).read() == shares0
          and not st["batches"] and st["trashed"] == 0 and st["linked"] == 0)
    check("dry run: groups, same-folder copies to trash, cross-folder links and the bytes saved",
          rep and rep["groups"] == 5 and rep["trash_names"] == 4 and rep["link_names"] == 4
          and rep["saved_bytes"] == 4 * 40000 + 30000 + 25000 + 20000, rep and {k: rep[k] for k in
          ("groups", "trash_names", "link_names", "saved_bytes")})
    check("dry run: the same-post copy is left, and says so", rep and rep.get("left_same_post") == 1, rep)
    check("dry run: only files whose size another inode shares were read (not the links, not the lone files)",
          rep and rep["hashed_files"] == 13, rep and rep["hashed_files"])
    check("dry run: a youtube thumbnail of the same bytes is out of scope", not any("youtube" in json.dumps(e) for e in rep["examples"]))
    first_read = rep["hashed_files"]
    code, body = api("POST", "/api/images/dedup", {})
    st = wait("library-dry")
    check("a second dry run reads nothing again (hashes are remembered per inode)",
          st["report"]["hashed_files"] == 0 and st["report"]["hash_cached"] >= first_read - 1 and first_read > 0,
          (st["report"]["hashed_files"], st["report"]["hash_cached"], first_read))
    code, body = api("GET", "/api/images/dedup")
    check("GET: status, last passes and the switch", code == 200 and body.get("enabled") is True
          and "library-dry" in body.get("last", {}), body.keys() if isinstance(body, dict) else body)

    # ---- the real pass ------------------------------------------------------------------------
    ns["_idd_last"].pop("library", None)
    code, body = api("POST", "/api/images/dedup", {"dry_run": False})
    st = wait("library")
    check("the pass: no error, one trash batch, nothing refused or failed",
          not st["error"] and len(st["batches"]) == 1 and st["refused"] == 0 and st["failed"] == 0, st)
    canon_ino = ino(lib, "pixiv/artist/50_p0.jpg")
    same = [ino(lib, p) == canon_ino for p in ("twitter/@a/1001_1.jpg", "twitter/@b/2001_1.jpg")]
    check("a picture in several folders: every name stays, all are the oldest copy's file", all(same), same)
    check("... the names took the oldest mtime", os.stat(os.path.join(lib, "twitter/@a/1001_1.jpg")).st_mtime_ns
          == os.stat(os.path.join(lib, "pixiv/artist/50_p0.jpg")).st_mtime_ns)
    check("a second picture in two folders: linked", ino(lib, "twitter/@a/1010_1.jpg") == ino(lib, "twitter/@d/4001_1.jpg"))
    check("same size, other bytes: left apart, bytes intact",
          ino(lib, "twitter/@a/1020_1.jpg") != ino(lib, "twitter/@a/1021_1.jpg")
          and open(os.path.join(lib, "twitter/@a/1020_1.jpg"), "rb").read() == Z1
          and open(os.path.join(lib, "twitter/@a/1021_1.jpg"), "rb").read() == Z2)
    check("a youtube thumbnail of the same bytes is not touched", ino(lib, "youtube/thumb/x.jpg") != canon_ino
          and os.path.getsize(os.path.join(lib, "youtube/thumb/x.jpg")) == len(X))
    gone = ["twitter/@a/1005_2.jpg", "pixiv/artist/60_p0.jpg", "twitter/@c/3002_1.jpg", "twitter/@f/7002_1.jpg"]
    tombs = [p for p in gone if p.startswith("twitter")]
    check("a second name in a folder: a 0-byte tombstone for gallery-dl (it will not fetch it again; pixiv's ban is its db row, "
          "so no stand-in), the oldest kept",
          all(os.path.getsize(os.path.join(lib, p)) == 0 for p in tombs)
          and not os.path.exists(os.path.join(lib, "pixiv/artist/60_p0.jpg"))
          and all(os.path.getsize(os.path.join(lib, p)) > 0 for p in
                  ("twitter/@a/1001_1.jpg", "pixiv/artist/50_p0.jpg", "twitter/@c/3001_1.jpg", "twitter/@f/7001_1.jpg")))
    batch = st["batches"][0]
    manifest = json.load(open(os.path.join(lib, ".trash", batch, "_manifest.json")))
    check("... in ONE trash batch, as a ban, named for the pass, with the reason",
          sorted(manifest["items"]) == sorted(gone) and manifest["mode"] == "ban"
          and manifest["deleted_by"] == "auto: image dedup (library)" and manifest["reason"].startswith(ns["IMGDEDUP_REASON"]), manifest)
    tn = os.path.join(lib, ".trash", batch, "twitter/@a/1005_2.jpg")
    check("... the trashed name is a LINK to the file that stays (the space came back at once)",
          os.lstat(tn).st_ino == canon_ino and os.lstat(tn).st_nlink >= 5, os.lstat(tn).st_nlink)
    check("the tombstones are in the empty-file registry (search does not list them)",
          set(tombs) <= set(json.load(open(ns["EMPTY_FILES_STORE"])).get(R, [])))
    fav = json.load(open(ns["FAVORITES_FILE"]))
    check("a favourite of the trashed name: the name that stays is a favourite too",
          "twitter/@a/1001_1.jpg" in fav["alice"][R] and "twitter/@c/3001_1.jpg" in fav["carol"][R], fav)
    check("a share of the trashed name shows the one that stays",
          json.load(open(ns["SHARES_FILE"]))[0]["path"] == "twitter/@a/1001_1.jpg")
    rows = {r["path"]: r for r in conn.execute("SELECT path, ino, trashed FROM media")}
    check("AI index: the trashed name's row is marked trashed (kept for a restore), the linked names' rows name the shared file",
          rows["twitter/@a/1005_2.jpg"]["trashed"] is not None
          and rows["twitter/@b/2001_1.jpg"]["ino"] == f"{os.stat(lib).st_dev}:{canon_ino}"
          and rows["twitter/@a/1001_1.jpg"]["ino"] == f"{os.stat(lib).st_dev}:{canon_ino}"
          and rows["twitter/@d/4001_1.jpg"]["ino"] == f"{os.stat(lib).st_dev}:{ino(lib, 'twitter/@a/1010_1.jpg')}", {k: (v["ino"], v["trashed"]) for k, v in rows.items()})
    check("two identical pictures of ONE post: both names stay (they may be pieces), as one file",
          os.path.getsize(os.path.join(lib, "twitter/@g/8001_2.jpg")) > 0
          and ino(lib, "twitter/@g/8001_2.jpg") == ino(lib, "twitter/@g/8001_1.jpg"))
    check("names that were links already: left; the stored walk tells where everything is now",
          ino(lib, "twitter/@e/6002_1.jpg") == ino(lib, "twitter/@e2/6001_1.jpg")
          and ns["_idd_store"]().with_size(len(X)) and len({e.ino for e in ns["_idd_store"]().with_size(len(X))
                                                           if e.rel.startswith(("twitter", "pixiv"))}) == 1)
    snap1 = snapshot(lib)
    ns["_idd_last"].pop("library", None)
    api("POST", "/api/images/dedup", {"dry_run": False})
    st = wait("library")
    check("a second pass: nothing to do", st["groups"] == 0 and st["trashed"] == 0 and st["linked"] == 0 and not st["batches"]
          and snapshot(lib) == snap1, {k: st[k] for k in ("groups", "trashed", "linked")})

    # ---- restoring a name keeps it ---------------------------------------------------------
    code, body = api("POST", f"/api/media/trash/{batch}/restore", {"root": R, "items": ["twitter/@c/3002_1.jpg"]})
    check("restore through the existing trash route", code == 200 and body.get("restored") == ["twitter/@c/3002_1.jpg"], body)
    check("... the restored name is exempt", "twitter/@c/3002_1.jpg" in ns["_idd_store"]().exempt_all())
    ns["_idd_last"].pop("library", None)
    api("POST", "/api/images/dedup", {"dry_run": False})
    st = wait("library")
    check("a later pass leaves a restored name alone",
          os.path.getsize(os.path.join(lib, "twitter/@c/3002_1.jpg")) > 0 and not st["batches"] and st["trashed"] == 0, st["trashed"])

    # ---- a file that changed after the plan is not touched ------------------------------------
    P = blob(9000)
    put(lib, "twitter/@h/9101_1.jpg", P, T0)
    put(lib, "twitter/@i/9201_1.jpg", P, T0 + 9)
    put(lib, "twitter/@i/9202_1.jpg", P, T0 + 10)
    ents = list(idd.walk(lib, accept=ns["_idd_accept"]))
    plan = idd.build_plan(ents, idd.Hasher(None, lib))
    mine = [g for g in plan["groups"] if g["size"] == len(P)]
    other = bytearray(P)
    other[0] ^= 0xFF
    path = os.path.join(lib, "twitter/@i/9201_1.jpg")
    os.utime(path, (T0 + 9, T0 + 9))
    with open(path, "r+b") as fh:                              # same size, same mtime, other bytes
        fh.write(bytes(other))
    os.utime(path, (T0 + 9, T0 + 9))
    stt = dict(ns["_IDD_COUNTERS"], batches=[])
    F("image_dedup_apply")(mine, "test", stt)
    check("bytes that changed after the plan: refused, nothing linked or trashed for that name",
          stt["refused"] >= 1 and os.path.getsize(path) == len(P) and open(path, "rb").read() == bytes(other)
          and ino(lib, "twitter/@i/9201_1.jpg") != ino(lib, "twitter/@h/9101_1.jpg"), stt)

    # ---- the trash cannot take a name: it stays ----------------------------------------------
    Q = blob(8000)
    put(lib, "twitter/@j/9301_1.jpg", Q, T0)
    put(lib, "twitter/@j/9302_1.jpg", Q, T0 + 3)
    real_move = ns["move_to_trash"]
    ns["move_to_trash"] = lambda *a: (_ for _ in ()).throw(OSError("read-only"))
    ents = list(idd.walk(lib, accept=ns["_idd_accept"]))
    plan = idd.build_plan(ents, idd.Hasher(None, lib))
    stt = dict(ns["_IDD_COUNTERS"], batches=[])
    F("image_dedup_apply")([g for g in plan["groups"] if g["size"] == len(Q)], "test", stt)
    ns["move_to_trash"] = real_move
    check("a name the trash cannot take stays a full picture, counted as failed",
          stt["failed"] == 1 and open(os.path.join(lib, "twitter/@j/9302_1.jpg"), "rb").read() == Q, stt)

    # ---- after a download ----------------------------------------------------------------------
    last_before = dict(ns["_idd_last"].get("download") or {})
    put(lib, "twitter/@a/1100_1.jpg", X, T0 + 900_000)           # the picture again, in a folder that has it
    put(lib, "twitter/@k/1200_1.jpg", Y, T0 + 900_001)           # a picture another folder has
    put(lib, "twitter/@k/1201_1.jpg", blob(3333), T0 + 900_002)  # a new picture
    new = [{"root": R, "path": p} for p in ("twitter/@a/1100_1.jpg", "twitter/@k/1200_1.jpg", "twitter/@k/1201_1.jpg",
                                             "twitter/@k/9.mp4", "youtube/thumb/y.jpg")]
    ns["image_dedup_after_download"](new)
    end = time.time() + 60
    while time.time() < end:
        time.sleep(0.05)
        if "download" in ns["_idd_last"] and ns["_idd_dl"]["running"] == 0:
            break
    dl = ns["_idd_dl"]
    check("after a download: the picture a folder had already is trashed with a ban, the new copy of another folder's is a link",
          os.path.getsize(os.path.join(lib, "twitter/@a/1100_1.jpg")) == 0
          and ino(lib, "twitter/@k/1200_1.jpg") == ino(lib, "twitter/@a/1010_1.jpg")
          and os.path.getsize(os.path.join(lib, "twitter/@k/1201_1.jpg")) > 0
          and dl["trashed"] == 1 and dl["linked"] >= 2, {k: dl[k] for k in ("trashed", "linked", "refused", "error")})
    check("... in a batch named for the automatic pass",
          json.load(open(os.path.join(lib, ".trash", dl["batches"][0], "_manifest.json")))["deleted_by"] == "auto: image dedup")
    done = dict(ns["_idd_last"]["download"])
    ns["image_dedup_after_download"]([{"root": R, "path": "youtube/thumb/y.jpg"}, {"root": R, "path": "twitter/@k/9.mp4"}])
    time.sleep(0.3)
    check("a video or a folder out of scope starts nothing", ns["_idd_last"]["download"] == done and ns["_idd_dl"]["running"] == 0)

    # ---- the stitch comes first ------------------------------------------------------------------
    order = []
    ns["run_stitch"] = lambda *a: (time.sleep(0.2), order.append("stitch"))
    ns["stitch_after_download"]([{"root": R, "path": "twitter/@l/300_1.jpg"}, {"root": R, "path": "twitter/@l/300_2.jpg"}],
                                then=lambda: order.append("dedup"))
    time.sleep(0.8)
    check("stitch_after_download(then=): the dedup runs after the stitch pass", order == ["stitch", "dedup"], order)
    order.clear()
    ns["stitch_after_download"]([{"root": R, "path": "twitter/@l/other.mp4"}], then=lambda: order.append("dedup"))
    check("... and at once when no post needs joining", order == ["dedup"], order)

print("ALL PASS" if ok else "SOME CHECKS FAILED")
sys.exit(0 if ok else 1)
