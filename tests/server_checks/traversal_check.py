"""Can a share link reach files outside what it shares with "." / ".." path
segments? Out of a server.py's AST, with the real path_allowed_by_share and
resolve_media_path over a temp root: a folder share, a file share and a pool
share, and every public route that takes a path. Run it against the OLD
server.py to show the hole and the NEW one to show it closed.

  venv/bin/python traversal_check.py <server.py> [expect-open]
"""
import ast
import os
import shutil
import sys
import tempfile

BASE = os.environ.get("ATRIHORD_DIR") or os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SERVER = sys.argv[1]
EXPECT_OPEN = len(sys.argv) > 2 and sys.argv[2] == "expect-open"
sys.path.insert(0, BASE)
sys.path.insert(0, os.path.dirname(os.path.abspath(SERVER)))
tmp = tempfile.mkdtemp(prefix="traversal_check_")
os.environ["KSF_MEDIA_AI_DIR"] = os.path.join(tmp, "ai")
os.makedirs(os.environ["KSF_MEDIA_AI_DIR"])
for name in ("config.json",):
    if os.path.exists(os.path.join(BASE, name)):
        shutil.copy(os.path.join(BASE, name), os.path.join(tmp, name))
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


lib = os.path.join(tmp, "lib")
for d in ("shared", "private", "pool_a"):
    os.makedirs(os.path.join(lib, d))
Image.new("RGB", (40, 30), (1, 2, 3)).save(os.path.join(lib, "shared", "ok.png"))
Image.new("RGB", (40, 30), (9, 9, 9)).save(os.path.join(lib, "private", "secret.png"))
Image.new("RGB", (40, 30), (5, 5, 5)).save(os.path.join(lib, "pool_a", "a.png"))
ns["CONFIG_FILE"] = os.path.join(tmp, "config.json")
ns["THUMB_CACHE_DIR"] = os.path.join(tmp, "thumbs")
ns["get_authenticated_user"] = lambda: None
for name in ("BASE_DIR", "CONFIG", "DEFAULT_CONFIG", "app", "DEFAULT_ROOT_ID", "find_share", "find_share_including_expired",
             "share_expired", "share_root_of", "path_allowed_by_share", "resolve_media_path",
             "public_share_file", "public_share_browse", "public_share_thumbnail", "public_share_file_info"):
    if name not in ns:
        load(name)
app = ns["app"]
R = "downloads"
ns["get_root"] = lambda root_id: {"id": root_id, "path": lib} if root_id == R else None
ns["attach_media_dimensions"] = lambda files, root_id: None
SHARES = [
    {"id": "folderTok", "kind": "folder", "path": "shared", "root": R},
    {"id": "fileTok", "kind": "file", "path": "shared/ok.png", "root": R},
    {"id": "poolTok", "kind": "pool", "items": [{"path": "pool_a", "root": R, "kind": "folder"}]},
]
ns["load_shares"] = lambda: SHARES


def status_of(fn, tok, url):
    with app.test_request_context(url):
        rv = resolve(lambda: ns[fn](tok))
    if isinstance(rv, tuple):
        return rv[1]
    code = rv.status_code
    rv.close()
    return code


def q(path):
    from urllib.parse import quote
    return quote(path, safe="")


try:
    check("an ordinary shared file is served", status_of("public_share_file", "folderTok",
          f"/api/public/share/folderTok/file?path={q('shared/ok.png')}") == 200)
    check("an ordinary shared folder is listed", status_of("public_share_browse", "folderTok",
          f"/api/public/share/folderTok/browse?path={q('shared')}") == 200)
    attacks = [
        ("public_share_file", "folderTok", "file", "shared/../private/secret.png"),
        ("public_share_file", "folderTok", "file", "shared/./../private/secret.png"),
        ("public_share_file", "folderTok", "file", "shared\\..\\private\\secret.png"),
        ("public_share_file", "fileTok", "file", "shared/ok.png/../../private/secret.png"),
        ("public_share_file", "poolTok", "file", "pool_a/../private/secret.png"),
        ("public_share_thumbnail", "folderTok", "thumbnail", "shared/../private/secret.png"),
        ("public_share_file_info", "folderTok", "info", "shared/../private/secret.png"),
        ("public_share_browse", "folderTok", "browse", "shared/../private"),
    ]
    for fn, tok, route, path in attacks:
        code = status_of(fn, tok, f"/api/public/share/{tok}/{route}?path={q(path)}")
        if EXPECT_OPEN:
            check(f"OLD code: {tok} /{route} {path} gets through ({code})", code == 200, code)
        else:
            check(f"{tok} /{route} {path}: refused ({code})", code == 403, code)
finally:
    shutil.rmtree(tmp, ignore_errors=True)
print("ALL PASS" if ok else "SOME CHECKS FAILED")
