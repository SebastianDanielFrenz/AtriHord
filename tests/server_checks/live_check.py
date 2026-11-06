"""Read-only, on the real library and AI store, with the DEPLOYED server.py's
own functions: does the search index still hold trash or empty files, and
what rating do the character and people covers have now?
  venv/bin/python live_check.py $ATRIHORD_DIR/server.py
"""
import ast
import os
import sys
import time

BASE = os.environ.get("ATRIHORD_DIR") or os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SERVER = sys.argv[1]
sys.path.insert(0, BASE)
os.chdir(BASE)
import media_ai                      # noqa: E402

tree = ast.parse(open(SERVER, encoding="utf-8").read())
defs = {}
ns = {"__name__": "live", "__file__": os.path.join(BASE, "server.py")}
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


t = time.time()
index = F("_walk_media_index")()
files = [r for r in index if not r[2]]
print(f"index: {len(index)} rows, {len(files)} files, {time.time() - t:.1f}s")
print("rows inside a dot-folder:", sum(1 for r in index if any(p.startswith(".") for p in r[1].split("/"))))
roots = {r["id"]: r["path"] for r in F("get_all_roots")()}
t = time.time()
empty = 0
for root_id, rel, _d in files:
    try:
        if os.path.getsize(os.path.join(roots[root_id], rel)) == 0:
            empty += 1
    except OSError:
        pass
print(f"empty files still in the index: {empty} ({time.time() - t:.1f}s to stat them all)")
reg = F("empty_files_snapshot")()
print("registry:", {k: len(v) for k, v in reg.items()})

conn = media_ai.connect(BASE)
t = time.time()
levels = media_ai.content_levels(conn)
print(f"content levels: {len(levels)} ({time.time() - t:.1f}s)")
ns["content_levels_map"] = lambda wait=True: levels
allowed = set(roots)
rank = F("cover_rank")
names = {0: "safe", 1: "suggestive", 2: "unrated", 3: "explicit"}
pools = media_ai.tag_pools(conn, category="character", limit=300)
before = {}
for p in pools:
    k = names[rank(levels, *p["cover"])]
    before[k] = before.get(k, 0) + 1
t = time.time()
pools = F("use_safe_covers")(conn, pools, allowed)
dt = time.time() - t
after = {}
for p in pools:
    k = names[rank(levels, *p["cover"])]
    after[k] = after.get(k, 0) + 1
print(f"top 300 character covers: before {before}, after {after}, choosing took {dt:.2f}s")

rows = conn.execute(
    "SELECT c.id, fm.root, fm.path FROM clusters c LEFT JOIN faces f ON f.id = c.cover_face"
    " LEFT JOIN media fm ON fm.id = f.media_id WHERE c.merged_into IS NULL AND c.face_count >= 3"
    " AND COALESCE(c.hidden, 0) = 0 ORDER BY c.face_count DESC LIMIT 500").fetchall()
before, after, queries = {}, {}, 0
t = time.time()
for cid, root, path in rows:
    r0 = rank(levels, root, path) if path else 2
    before[names[r0]] = before.get(names[r0], 0) + 1
    if r0 == 0 and root in allowed:
        after["safe"] = after.get("safe", 0) + 1
        continue
    queries += 1
    pick = F("safe_person_cover")(conn, cid, levels, allowed)
    k = names[pick[0]] if pick else "none"
    after[k] = after.get(k, 0) + 1
print(f"top {len(rows)} people covers: before {before}, after {after}, {queries} walks in {time.time() - t:.2f}s")
