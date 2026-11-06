"""POST /api/media/upscale on a cached upscale answers with what the details
panel shows (factor, model, reason, size, GPU time, when made) - out of a
STAGED server.py's AST, with planning and the cache index replaced.

  venv/bin/python upscale_ready_check.py /tmp/.../server.py
"""
import ast
import os
import sys
import time

BASE = os.environ.get("ATRIHORD_DIR") or os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SERVER = sys.argv[1]
sys.path.insert(0, BASE)

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
    for _ in range(300):
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


load("app")
load("media_upscale")
created = time.time() - 120
ns["get_authenticated_user"] = lambda: {"username": "alice", "role": "admin"}
ns["root_visible_to"] = lambda user, root: True
ns["plan_upscale"] = lambda root, path, w, h, **kw: ({"key": "k" * 40, "factor": 4, "model": "anime",
                                               "reason": "tag 1girl"}, None)
ns["_touch_upscale"] = lambda key: {"factor": 4, "model": "anime", "reason": "tag 1girl", "out": [1600, 1200],
                                    "seconds": 1.04, "created": created}
with ns["app"].test_request_context("/api/media/upscale", method="POST",
                                    json={"root": "downloads", "path": "a/b.png", "need_w": 800, "need_h": 600}):
    resp = resolve(lambda: ns["media_upscale"]())
data = (resp[0] if isinstance(resp, tuple) else resp).get_json()
want = {"status": "ready", "factor": 4, "model": "anime", "reason": "tag 1girl", "width": 1600, "height": 1200,
        "seconds": 1.04, "created": created}
bad = {k: (data.get(k), v) for k, v in want.items() if data.get(k) != v}
print(("PASS" if not bad else "FAIL") + " ready answer carries what the details panel shows" + (f"  ({bad})" if bad else ""))
print("ALL PASS" if not bad else "SOME CHECKS FAILED")
