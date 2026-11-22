"""ATRI-11 checks of a STAGED server.py, run out of its AST in Flask test requests: the streamed zip of a
multi-selection (POST /api/media/zip) and the pool share made with its items in one call (POST /api/share/pool).
Everything happens in a temp dir (two fake media roots, a temp shares.json); nothing under /data2 is touched.

  venv/bin/python multi_download_share_check.py /tmp/.../server.py
"""
import ast
import io
import json
import os
import shutil
import sys
import tempfile
import zipfile

BASE = os.environ.get("ATRIHORD_DIR") or os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SERVER = sys.argv[1] if len(sys.argv) > 1 else os.path.join(BASE, "server.py")
sys.path.insert(0, BASE)
tmp = tempfile.mkdtemp(prefix="multi_dl_check_")

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


# ---------------------------------------------------------------- fixture
def put(root, rel, data=b"x"):
    full = os.path.join(root, *rel.split("/"))
    os.makedirs(os.path.dirname(full), exist_ok=True)
    with open(full, "wb") as f:
        f.write(data)


pub = os.path.join(tmp, "pub")        # guest-visible root (the default one)
priv = os.path.join(tmp, "priv")      # admin-only root
outside = os.path.join(tmp, "outside")
for d in (pub, priv, outside):
    os.makedirs(d)
put(pub, "a/one.jpg", b"1" * 10)
put(pub, "a/sub/two.mp4", b"2" * 3000)
put(pub, "a/sub/two-poster.jpg", b"p")        # poster of a video: not in a listing, not in the zip
put(pub, "a/.hidden.jpg", b"h")
put(pub, "a/clip.mp4.part", b"part")
put(pub, "a/Screenshot_2026.png", b"s")
os.makedirs(os.path.join(pub, "a", "empty"))
put(pub, "b/three.png", b"3" * 5)
put(pub, "b/other/one.jpg", b"o")              # same file name as a/one.jpg, other folder
put(pub, "single.txt", b"S")
put(pub, "shot/Screenshot_1.png", b"s")
put(pub, "Ünï cødé/日本語.jpg", b"u")
put(outside, "secret.txt", b"SECRET")
put(priv, "private.txt", b"P")
try:
    os.symlink(outside, os.path.join(pub, "a", "escape"), target_is_directory=True)
    os.symlink(os.path.join(outside, "secret.txt"), os.path.join(pub, "a", "leak.txt"))
    os.symlink(os.path.join(pub, "b", "three.png"), os.path.join(pub, "a", "inner_link.png"))
    symlinks = True
except (OSError, NotImplementedError):
    symlinks = False

ROOTS = [{"id": "downloads", "path": pub, "guest_visible": True}, {"id": "priv", "path": priv, "guest_visible": False}]
ns["get_all_roots"] = lambda: ROOTS
ns["get_root"] = lambda rid: next((r for r in ROOTS if r["id"] == rid), None)
ADMIN = {"username": "alice", "role": "admin"}
GUEST = {"username": "guest1", "role": "guest"}
CURRENT = {"user": ADMIN}
ns["get_authenticated_user"] = lambda: CURRENT["user"]

for name in ("DEFAULT_ROOT_ID", "app", "is_admin", "roots_for_user", "root_visible_to", "resolve_media_path",
             "_resolve_downloads_subpath", "share_hidden_name", "share_pool_items_checked", "MAX_POOL_ITEMS_AT_ONCE",
             "_is_hidden_or_partial", "poster_companion_names", "MEDIA_EXTENSIONS", "POSTER_STEM_SUFFIX", "_YTDLP_FRAGMENT_NAME", "_ZipSink", "zip_selection_entries", "stream_zip",
             "media_zip", "create_share_pool", "share_summary", "default_share_expiry", "shares_lock"):
    load(name)
app = ns["app"]
ns["default_share_expiry"] = lambda created=None: None
ns["share_summary"] = lambda share: {"id": share["id"], "kind": share["kind"], "items": share["items"],
                                      "label": share.get("label", ""), "url": "/share/" + share["id"]}
STORE = []
ns["_read_shares_unlocked"] = lambda: STORE
ns["_write_shares_unlocked"] = lambda shares: None


def call(fn, path, body=None, form=None, user=ADMIN):
    CURRENT["user"] = user
    with app.test_request_context(path, method="POST", json=body, data=form):
        resp = resolve(lambda: ns[fn]())
        if isinstance(resp, tuple):
            resp, status = resp[0], resp[1]
        else:
            status = resp.status_code
        data = resp.get_data()
        return status, resp, data


def zip_names(data):
    z = zipfile.ZipFile(io.BytesIO(data))
    return z, sorted(z.namelist())


# ---------------------------------------------------------------- zip
st, resp, data = call("media_zip", "/api/media/zip", {"items": [{"path": "a", "root": "downloads"}]})
check("one folder streams a zip", st == 200 and resp.mimetype == "application/zip", st)
z, names = zip_names(data)
check("zip is valid and uncompressed (STORED)", z.testzip() is None and all(i.compress_type == zipfile.ZIP_STORED for i in z.infolist()))
check("folder keeps its structure under its own name", "a/one.jpg" in names and "a/sub/two.mp4" in names, names)
check("content is intact", z.read("a/sub/two.mp4") == b"2" * 3000 and z.read("a/one.jpg") == b"1" * 10)
check("dot files, partial downloads, posters and screenshots are skipped",
      not any(n.endswith((".hidden.jpg", ".part", "two-poster.jpg")) or "Screenshot_" in n for n in names), names)
check("an empty folder is kept as a directory entry", "a/empty/" in names, names)
if symlinks:
    check("symlinks that escape the root are skipped", not any("escape" in n or "leak" in n for n in names), names)
    check("a symlink staying inside the root is kept", "a/inner_link.png" in names, names)
check("nothing outside the root is in the zip", all(b"SECRET" not in z.read(n) for n in names if not n.endswith("/")))
cd = resp.headers.get("Content-Disposition", "")
check("single folder: file name is the folder name", "attachment" in cd and "a.zip" in cd, cd)

st, resp, data = call("media_zip", "/api/media/zip",
                      {"items": [{"path": "a/one.jpg", "root": "downloads"}, {"path": "b/other/one.jpg", "root": "downloads"},
                                 {"path": "single.txt", "root": "downloads"}, {"path": "b", "root": "downloads"}], "name": "photos"})
z, names = zip_names(data)
check("mixed selection: files and folder in one zip", st == 200 and "single.txt" in names and "b/three.png" in names, names)
check("two files with the same name do not collide", "one.jpg" in names and "one (2).jpg" in names, names)
check("selection name is <folder>-selection.zip", "photos-selection.zip" in resp.headers.get("Content-Disposition", ""),
      resp.headers.get("Content-Disposition"))

st, resp, data = call("media_zip", "/api/media/zip", None,
                      form={"items": json.dumps([{"path": "Ünï cødé", "root": "downloads"}, {"path": "single.txt"}])})
z, names = zip_names(data)
check("hidden form POST (items as JSON field) works", st == 200 and "single.txt" in names, st)
check("unicode names survive", "Ünï cødé/日本語.jpg" in names, names)

# big enough to cross several chunks: the zip stays valid when streamed in pieces
put(pub, "big/blob.bin", os.urandom(3 * 1024 * 1024 + 7))
st, resp, data = call("media_zip", "/api/media/zip", {"items": [{"path": "big", "root": "downloads"}, {"path": "single.txt"}]})
z, names = zip_names(data)
check("multi-chunk file streams intact", z.read("big/blob.bin") == open(os.path.join(pub, "big", "blob.bin"), "rb").read())

# ---------------------------------------------------------------- access
st, _, _ = call("media_zip", "/api/media/zip", {"items": [{"path": "single.txt"}]}, user=None)
check("not logged in: 401", st == 401, st)
st, _, _ = call("media_zip", "/api/media/zip", {"items": [{"path": "private.txt", "root": "priv"}, {"path": "single.txt"}]}, user=GUEST)
check("guest: a root they cannot see is refused (whole request)", st == 403, st)
st, resp, data = call("media_zip", "/api/media/zip", {"items": [{"path": "a", "root": "downloads"}, {"path": "single.txt"}]}, user=GUEST)
check("guest may download from a visible root", st == 200 and b"PK" == data[:2], st)
st, resp, data = call("media_zip", "/api/media/zip", {"items": [{"path": "private.txt", "root": "priv"}, {"path": "single.txt"}]})
check("admin may use the private root", st == 200 and "private.txt" in zip_names(data)[1])
for bad, want in (([{"path": "../outside/secret.txt"}], 400), ([{"path": "nope"}], 404), ([], 400), ("x", 400),
                  ([{"path": ""}], 400)):
    st, _, _ = call("media_zip", "/api/media/zip", {"items": bad})
    check(f"bad items {json.dumps(bad)[:30]}: {want}", st == want, st)
st, _, _ = call("media_zip", "/api/media/zip", {"items": [{"path": "shot/Screenshot_1.png"}]})
check("a screenshot file is refused like a share refuses it", st == 400, st)

# ---------------------------------------------------------------- pool with items
st, resp, _ = call("create_share_pool", "/api/share/pool",
                   {"label": "L", "items": [{"path": "a", "root": "downloads"}, {"path": "single.txt"}, {"path": "a"}]})
j = resp.get_json()
check("pool with items is created in one call", st == 200 and len(j["items"]) == 2, (st, j))
check("items carry kind and root, duplicates are dropped",
      {(i["path"], i["kind"], i["root"]) for i in j["items"]} == {("a", "folder", "downloads"), ("single.txt", "file", "downloads")}, j)
check("the pool was stored", len(STORE) == 1 and STORE[0]["kind"] == "pool" and STORE[0]["created_by"] == "alice")
before = len(STORE)
st, resp, _ = call("create_share_pool", "/api/share/pool", {"items": [{"path": "single.txt"}, {"path": "private.txt", "root": "priv"}]}, user=GUEST)
check("guest: one forbidden item refuses the whole pool, nothing stored", st == 403 and len(STORE) == before, st)
st, resp, _ = call("create_share_pool", "/api/share/pool", {"items": [{"path": "single.txt"}, {"path": "missing"}]})
check("a missing item refuses the whole pool", st == 404 and len(STORE) == before, st)
st, resp, _ = call("create_share_pool", "/api/share/pool", {"items": [{"path": "shot/Screenshot_1.png"}]})
check("screenshots are not shared", st == 400 and len(STORE) == before, st)
st, resp, _ = call("create_share_pool", "/api/share/pool", {"items": "x"})
check("items must be a list", st == 400, st)
st, resp, _ = call("create_share_pool", "/api/share/pool", {"label": "empty"})
check("old body (label only) still makes an empty pool", st == 200 and resp.get_json()["items"] == [], st)
st, resp, _ = call("create_share_pool", "/api/share/pool", None, user=None)
check("pool: not logged in is 401", st == 401, st)

shutil.rmtree(tmp, ignore_errors=True)
print("ALL PASS" if ok else "FAILED")
sys.exit(0 if ok else 1)
