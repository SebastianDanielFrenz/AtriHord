"""Trash and tombstones in search. Out of a server.py's AST over a temp root:
the search index, the search route, build_entries_for, a ban delete through
trash_resolved, a restore through the real route, check_empty_files and the
recent-list seed. Run against the OLD server.py with "old" to show the bug.

  venv/bin/python search_check.py <server.py> [old]
"""
import ast
import json
import os
import shutil
import sys
import tempfile

BASE = os.environ.get("ATRIHORD_DIR") or os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SERVER = sys.argv[1]
OLD = len(sys.argv) > 2 and sys.argv[2] == "old"
sys.path.insert(0, BASE)
tmp = tempfile.mkdtemp(prefix="search_check_")
os.environ["KSF_MEDIA_AI_DIR"] = os.path.join(tmp, "ai")
os.makedirs(os.environ["KSF_MEDIA_AI_DIR"])
from PIL import Image                # noqa: E402

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
    """Loads a function and, first, every server.py name it uses - so no call
    is re-run half-way by resolve() after a NameError. A re-run repeats side
    effects: a file already moved to the trash, a path already in a set."""
    seen = set() if seen is None else seen
    if name in seen or name not in defs:
        return
    seen.add(name)
    node = defs[name]
    code = compile(ast.Module(body=[node], type_ignores=[]), SERVER, "exec")
    for used in _names(code):
        if used not in ns:
            preload(used, seen)
    if name not in ns:
        load(name)


def F(name):
    preload(name)
    return ns[name]


# The library: an account with two pictures, one of them banned before (a
# tombstone), a trash batch holding a folder, and a dot-file.
lib = os.path.join(tmp, "lib")
acct = os.path.join(lib, "twitter", "@alice")
os.makedirs(acct)
Image.new("RGB", (40, 30), (1, 2, 3)).save(os.path.join(acct, "keep.jpg"))
Image.new("RGB", (40, 30), (3, 2, 1)).save(os.path.join(acct, "ban_me.jpg"))
open(os.path.join(acct, "old_ban.jpg"), "wb").close()                       # an old tombstone
trashed = os.path.join(lib, ".trash", "20260911-101240-b8aaaa", "twitter", "@alice")
os.makedirs(trashed)
Image.new("RGB", (40, 30), (9, 9, 9)).save(os.path.join(trashed, "gone.jpg"))
open(os.path.join(lib, "twitter", ".hidden.jpg"), "wb").close()

R = "downloads"
ns["BASE_DIR"] = tmp
ns["DEFAULT_ROOT_ID"] = R
ns["THUMB_CACHE_DIR"] = os.path.join(tmp, "thumbs")
ADMIN = {"username": "alice", "role": "admin"}
ns["get_authenticated_user"] = lambda: ADMIN
ns["get_root"] = lambda root_id: {"id": R, "path": lib, "name": "Downloads"} if root_id == R else None
ns["get_all_roots"] = lambda: [{"id": R, "path": lib, "name": "Downloads"}]
ns["roots_for_user"] = lambda user: [{"id": R}]
ns["root_visible_to"] = lambda user, root_id: root_id == R
ns["attach_media_dimensions"] = lambda *a, **k: None
ns["attach_favorites"] = lambda entries, user: entries
ns["download_category_of"] = lambda root_id, rel: rel.split("/")[0]
for name in ("app", "_media_index_cache", "_media_index_lock", "_media_index_build_lock",
             "MEDIA_INDEX_CACHE_SECONDS", "_recent_media_page_cache", "_recent_media_page_lock",
             "_folder_stats_lock", "_folder_stats_cache", "_everything_cache", "TRASH_DIR_NAME",
             "SEARCH_MODES", "DELETE_BAN", "DELETE_RELEASE", "GALLERY_DL_CATEGORIES"):
    if name not in ns and name in defs:
        load(name)
if not OLD:
    load("EMPTY_FILES_STORE")
    ns["EMPTY_FILES_STORE"] = os.path.join(tmp, "empty_files.json")
    # All of the registry up front: the lazy loader re-runs a call after a
    # NameError, and a half-run note_empty_files would already have changed
    # the set in memory - so the re-run saves nothing.
    for name in ("_empty_files", "_empty_files_lock", "_empty_files_loaded", "_save_empty_files",
                 "note_empty_files", "empty_files_snapshot", "check_empty_files"):
        load(name)
app = ns["app"]


def index_paths():
    ns["_media_index_cache"]["index"] = None
    return [row[1] for row in resolve(lambda: F("get_media_index")())]


def search(q):
    with app.test_request_context(f"/api/media/search?q={q}&limit=100"):
        rv = resolve(lambda: F("search_media")())
    data = rv.get_json()
    return data, [i["path"] for i in data["items"]]


try:
    rows = index_paths()
    trash_rows = [p for p in rows if p.startswith(".trash")]
    data, hits = search("alice")
    if OLD:
        check(f"OLD: the index holds the trash ({len(trash_rows)} rows)", trash_rows, rows)
        check("OLD: searching the account finds the trashed folder", any(".trash" in p for p in hits), hits)
        check("OLD: the old tombstone is in the index", "twitter/@alice/old_ban.jpg" in rows, rows)
        print("ALL PASS" if ok else "SOME CHECKS FAILED")
        sys.exit(0)

    check("no index row inside .trash", not trash_rows, trash_rows)
    check("the account folder and the kept picture are indexed",
          "twitter/@alice" in rows and "twitter/@alice/keep.jpg" in rows, rows)
    check("dot-files stay out", not any(".hidden" in p for p in rows))
    check("searching the account: no trash in the results", not any(".trash" in p for p in hits), hits)

    # A tombstone the registry does not know yet: listed by the index, never
    # shown, and learned from the listing.
    check("an unknown tombstone is still an index row (the index stats nothing)",
          "twitter/@alice/old_ban.jpg" in rows)
    check("... but the results never show it", "twitter/@alice/old_ban.jpg" not in hits, hits)
    reg = json.load(open(ns["EMPTY_FILES_STORE"]))
    check("... and meeting it recorded it", reg.get(R) == ["twitter/@alice/old_ban.jpg"], reg)
    rows = index_paths()
    data, hits = search("alice")
    check("the next index leaves it out", "twitter/@alice/old_ban.jpg" not in rows, rows)
    check("... so the total counts only what is shown", data["total"] == len(hits) == 3, (data["total"], hits))

    # A ban delete through the real path: the file moves to the trash, a
    # tombstone takes its place, and search shows neither.
    resolved = [(R, "twitter/@alice/ban_me.jpg", os.path.join(acct, "ban_me.jpg"))]
    batch, deleted, notes, failed = resolve(lambda: F("trash_resolved")(resolved, F("DELETE_BAN"), ADMIN))
    check("the ban left a tombstone", os.path.getsize(os.path.join(acct, "ban_me.jpg")) == 0 and not failed, (notes, failed))
    reg = json.load(open(ns["EMPTY_FILES_STORE"]))
    check("... and recorded it", "twitter/@alice/ban_me.jpg" in reg.get(R, []), reg)
    rows = index_paths()
    data, hits = search("ban_me")
    check("search finds neither the tombstone nor the trashed file", hits == [] and data["total"] == 0,
          (hits, [p for p in rows if "ban_me" in p]))

    # Restore through the route: the tombstone is replaced and forgotten.
    with app.test_request_context(f"/api/media/trash/{batch}/restore", method="POST", json={"root": R}):
        rv = resolve(lambda: F("restore_trash")(batch))
    body = rv.get_json() if hasattr(rv, "get_json") else rv[0].get_json()
    check("the restore put it back", body.get("restored") == ["twitter/@alice/ban_me.jpg"], body)
    reg = json.load(open(ns["EMPTY_FILES_STORE"]))
    check("... and the registry forgot it", "twitter/@alice/ban_me.jpg" not in reg.get(R, []), reg)
    data, hits = search("ban_me")
    check("... so search finds it again", hits == ["twitter/@alice/ban_me.jpg"], hits)

    # A registered path that holds a real file again, and one that is gone.
    resolve(lambda: F("note_empty_files")([(R, "twitter/@alice/keep.jpg"), (R, "twitter/@alice/vanished.jpg")]))
    rows = index_paths()
    check("(a real file wrongly registered is hidden until checked)", "twitter/@alice/keep.jpg" not in rows)
    resolve(lambda: F("check_empty_files")())
    reg = json.load(open(ns["EMPTY_FILES_STORE"]))
    check("check_empty_files forgets a real file and a vanished path",
          reg.get(R) == ["twitter/@alice/old_ban.jpg"], reg)
    rows = index_paths()
    check("... and the real file is back in the index", "twitter/@alice/keep.jpg" in rows, rows)

    # The recent-list seed walks the library too.
    seeded = []
    ns["recent_media_store"] = type("S", (), {
        "max_recent_media_items": staticmethod(lambda base: 100),
        "write_recent_media_paths": staticmethod(lambda base, paths: seeded.extend(paths))})
    resolve(lambda: F("bootstrap_recent_media_list")())
    got = sorted(p["path"] for p in seeded)
    check("the recent-list seed skips the trash and tombstones",
          got == ["twitter/@alice/ban_me.jpg", "twitter/@alice/keep.jpg"], got)
finally:
    shutil.rmtree(tmp, ignore_errors=True)
print("ALL PASS" if ok else "SOME CHECKS FAILED")
