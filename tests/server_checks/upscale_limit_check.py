"""Upscale limits, out of a STAGED server.py's AST: plan_upscale within the lower
of the "Largest result" setting and the vision worker's ceiling (and naming
which), the worker's error answers turned into words, the ready answer carrying
what limited it - and, against the DEPLOYED worker, its reported ceiling and its
413 answer. One real library picture is planned, never upscaled.

  venv/bin/python upscale_limit_check.py /tmp/.../server.py
"""
import ast
import io
import json
import math
import os
import secrets
import shutil
import sys
import tempfile
import time
import urllib.error
import urllib.request

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


ok = True


def check(label, cond, detail=""):
    global ok
    ok = ok and bool(cond)
    print(("PASS " if cond else "FAIL ") + label + (f"  ({detail})" if detail and not cond else ""))


# A loaded function names CONFIG, which resolves to `CONFIG = load_config()`:
# let it load and write back a copy, never the live config.json (writeguard.py).
cfg_tmp = tempfile.mkdtemp(prefix="upscale_limit_check_")
ns["CONFIG_FILE"] = shutil.copy(os.path.join(BASE, "config.json"), os.path.join(cfg_tmp, "config.json"))
for name in ("_read_roots_unlocked", "app", "plan_upscale", "_run_upscale", "UpscaleLimitError",
             "worker_upscale_limit_px", "note_worker_upscale_limit", "_worker_upscale_limit",
             "_worker_upscale_limit_lock", "upscale_settings", "choose_upscale_model", "vision_worker_address",
             "media_upscale"):
    load(name)
real_settings = ns["upscale_settings"]
real_limit = ns["worker_upscale_limit_px"]
ROOT, PATH, W, H = "downloads", "twitter/@yoake_cos_/2091141486901694803_1.jpg", 2730, 4096


def plan(mult, worker_px, **override):
    cfg = dict(resolve(lambda: real_settings()))
    cfg.update(enabled=True, **override)
    ns["upscale_settings"] = lambda: cfg
    ns["worker_upscale_limit_px"] = lambda refresh=False: worker_px
    try:
        return resolve(lambda: ns["plan_upscale"](ROOT, PATH, int(W * mult), int(H * mult)))
    finally:
        ns["upscale_settings"] = real_settings
        ns["worker_upscale_limit_px"] = real_limit


AREA = W * H


def bound_ok(spec, limit_px):
    """At the limit: within it, less than half a percent under, same proportions."""
    tw, th = spec["target"]
    return tw * th <= limit_px and tw * th > limit_px * 0.995 and abs(tw / W - th / H) < 0.002


def shape(spec, status):
    return spec and {k: spec.get(k) for k in ("factor", "target", "scale", "limited")} or status


spec, status = plan(3, 64_000_000, max_factor=8, max_output_megapixels=256)
check("64 MP worker, 3x needed: the largest picture within 64 MP (~2.39x), the worker named",
      spec and bound_ok(spec, 64_000_000) and spec["scale"] == 2.39 and spec["factor"] == 3
      and spec["limited"] == {"needed": 3, "by": "worker", "limit_mp": 64.0}, shape(spec, status))
spec, status = plan(3, 256_000_000, max_factor=8, max_output_megapixels=256)
check("256 MP both: a whole 3x (100.6 MP), not limited", spec and spec["factor"] == 3
      and spec["target"] == [W * 3, H * 3] and spec["scale"] == 3.0 and spec["limited"] is None, shape(spec, status))
spec, status = plan(3, 256_000_000, max_factor=8, max_output_megapixels=64)
check("64 MP setting under a 256 MP worker: at 64 MP, the setting named",
      spec and bound_ok(spec, 64e6) and spec["limited"]["by"] == "setting", shape(spec, status))
spec, status = plan(3, 40_000_000, max_factor=8, max_output_megapixels=256)
check("40 MP worker, below 2x (44.7 MP): served at 40 MP (~1.89x) instead of skipped",
      spec and bound_ok(spec, 40e6) and spec["scale"] == 1.89 and spec["factor"] == 2, shape(spec, status))
spec, status = plan(5, 256_000_000, max_factor=8, max_output_megapixels=256)
check("5x needed under 256 MP: ~4.78x at 256 MP, not a whole 4x (179 MP)",
      spec and bound_ok(spec, 256e6) and spec["scale"] == 4.78 and spec["factor"] == 5, shape(spec, status))
spec, status = plan(3, None, max_factor=8, max_output_megapixels=12)
check("12 MP limit on an 11.2 MP file (under 1.1x of room): skipped, file and limit named",
      status == {"status": "too_large", "width": W, "height": H, "original_mp": 11.2, "limit_mp": 12.0,
                 "limit_by": "setting"}, status)
spec, status = plan(3, 256_000_000, max_factor=2, max_output_megapixels=256)
check("largest factor 2 while 3x is needed: a whole 2x, limited by max_factor",
      spec and spec["factor"] == 2 and spec["target"] == [W * 2, H * 2]
      and spec["limited"] == {"needed": 3, "by": "max_factor"}, shape(spec, status))
spec, status = plan(1, 256_000_000, max_factor=8, max_output_megapixels=256)
check("enough pixels already: not needed", status and status.get("status") == "not_needed", status)
bound_spec, _ = plan(3, 64_000_000, max_factor=8, max_output_megapixels=256)
whole_spec, _ = plan(3, 256_000_000, max_factor=8, max_output_megapixels=256)
check("a size at a limit and a whole factor never share a cache key", bound_spec["key"] != whole_spec["key"])

# ------------------------------------------------------------ worker errors
tmpimg = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
tmpimg.write(b"not really a picture")
tmpimg.close()
run_spec = {"key": "k" * 40, "full": tmpimg.name, "model": "general", "factor": 3, "root": ROOT, "path": PATH,
            "reason": "r", "src": [W, H], "limited": None}
native_urlopen = urllib.request.urlopen


def outcome(exc):
    def raise_it(*a, **k):
        raise exc
    urllib.request.urlopen = raise_it
    try:
        resolve(lambda: ns["_run_upscale"](run_spec))
        return None
    except Exception as got:
        return got
    finally:
        urllib.request.urlopen = native_urlopen


def http_error(code, body):
    return urllib.error.HTTPError("http://worker/v1/upscale", code, "x", {}, io.BytesIO(json.dumps(body).encode()))


got = outcome(http_error(413, {"error": "3x of 2730x4096 would be 100.6 MP, over this worker's limit of 64 MP",
                               "max_output_px": 64000000, "output_px": 100600000}))
check("413 with the limit: UpscaleLimitError carrying it, in words",
      isinstance(got, ns["UpscaleLimitError"]) and got.max_output_px == 64000000 and "100.6 MP" in str(got), repr(got))
got = outcome(http_error(500, {"error": "CUDA out of memory"}))
check("500: 'the vision worker refused it (HTTP 500): CUDA out of memory'",
      isinstance(got, RuntimeError) and not isinstance(got, ns["UpscaleLimitError"])
      and str(got) == "the vision worker refused it (HTTP 500): CUDA out of memory", repr(got))
got = outcome(urllib.error.URLError(ConnectionRefusedError(111, "Connection refused")))
check("unreachable: 'the vision worker could not be reached (...)'", str(got).startswith("the vision worker could not be reached"), repr(got))
got = outcome(TimeoutError("timed out"))
check("timeout: 'did not answer within 10 minutes'", str(got) == "the vision worker did not answer within 10 minutes", repr(got))
os.remove(tmpimg.name)

# ------------------------------------------------------- ready answer shape
ns["get_authenticated_user"] = lambda: {"username": "alice", "role": "admin"}
ns["root_visible_to"] = lambda user, root: True
limited = {"needed": 3, "by": "worker", "limit_mp": 64.0}
saved_plan = ns["plan_upscale"]
ns["plan_upscale"] = lambda r, p, w, h, **kw: ({"key": "k" * 40, "factor": 2, "model": "general", "reason": "tag realistic",
                                          "limited": limited}, None)
ns["_touch_upscale"] = lambda key: {"factor": 2, "model": "general", "reason": "tag realistic", "out": [5460, 8192],
                                    "seconds": 6.1, "created": time.time(), "limited": limited}
with ns["app"].test_request_context("/api/media/upscale", method="POST",
                                    json={"root": ROOT, "path": PATH, "need_w": 8190, "need_h": 12288}):
    resp = resolve(lambda: ns["media_upscale"]())
data = (resp[0] if isinstance(resp, tuple) else resp).get_json()
check("ready answer says what limited it", data.get("status") == "ready" and data.get("limited") == limited, data)
ns["plan_upscale"] = saved_plan

bound_cache = tempfile.mkdtemp(prefix="bound_cache_")
open(os.path.join(bound_cache, "x.webp"), "wb").write(b"RIFF....WEBP")
ns["upscale_cache_dir"] = lambda: bound_cache
ns["_touch_upscale"] = lambda key: {"root": ROOT, "path": PATH, "factor": 5, "scale": 4.78, "file": "x.webp",
                                    "model": "general", "reason": "r", "out": [13062, 19598], "seconds": 90.0,
                                    "created": time.time(), "limited": None}
load("media_upscaled_file")
with ns["app"].test_request_context("/api/media/upscaled", query_string={"key": "a" * 40, "download": "1"}):
    resp = resolve(lambda: ns["media_upscaled_file"]())
    disposition = resp.headers.get("Content-Disposition", "")
    resp.close()
check("a result at a limit downloads as ...upscaled-4.78x.webp", "2091141486901694803_1.upscaled-4.78x.webp" in disposition,
      disposition)

# ------------------------------------------------------- the deployed worker
px = resolve(lambda: real_limit(refresh=True))
check("deployed worker reports its ceiling: 256 MP", px == 256_000_000, px)
from PIL import Image  # noqa: E402
buf = io.BytesIO()
Image.new("RGB", (1000, 1000), (90, 120, 150)).save(buf, "PNG")
base, token = resolve(lambda: ns["vision_worker_address"]())
boundary = secrets.token_hex(16)
body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"t.png\"\r\n"
        f"Content-Type: image/png\r\n\r\n").encode() + buf.getvalue() + b"\r\n"
for name, value in (("model", "general"), ("factor", "17")):
    body += f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n{value}\r\n".encode()
body += f"--{boundary}--\r\n".encode()
req = urllib.request.Request(base.rstrip("/") + "/v1/upscale", data=body, method="POST")
req.add_header("Content-Type", "multipart/form-data; boundary=" + boundary)
if token:
    req.add_header("Authorization", "Bearer " + token)
try:
    urllib.request.urlopen(req, timeout=30)
    answer = (200, None)
except urllib.error.HTTPError as exc:
    answer = (exc.code, json.loads(exc.read() or b"{}"))
check("deployed worker: 17x of 1000x1000 refused with 413, the limit and the numbers",
      answer[0] == 413 and answer[1].get("max_output_px") == 256_000_000 and "289.0 MP" in answer[1].get("error", ""), answer)


def worker_post(fields):
    boundary2 = secrets.token_hex(16)
    body2 = (f"--{boundary2}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"t.png\"\r\n"
             f"Content-Type: image/png\r\n\r\n").encode() + buf.getvalue() + b"\r\n"
    for name, value in fields.items():
        body2 += f"--{boundary2}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n{value}\r\n".encode()
    body2 += f"--{boundary2}--\r\n".encode()
    req2 = urllib.request.Request(base.rstrip("/") + "/v1/upscale", data=body2, method="POST")
    req2.add_header("Content-Type", "multipart/form-data; boundary=" + boundary2)
    if token:
        req2.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(req2, timeout=180) as r:
            r.read()
            return r.status, r.headers.get("X-Upscale-Size")
    except urllib.error.HTTPError as exc:
        return exc.code, (json.loads(exc.read() or b"{}") or {}).get("error")


got = worker_post({"model": "general", "factor": "2", "width": "2000", "height": "2000"})
check("deployed worker: an exact 2000x2000 target from 1000x1000", got == (200, "2000x2000"), got)
got = worker_post({"model": "general", "factor": "2", "width": "1500", "height": "1500"})
check("deployed worker: a fractional 1.5x target, exactly 1500x1500", got == (200, "1500x1500"), got)
got = worker_post({"model": "anime", "width": "2391", "height": "2391"})
check("deployed worker: 2.39x with the 4x anime model, exactly 2391x2391", got == (200, "2391x2391"), got)
got = worker_post({"model": "general", "width": "2000", "height": "1000"})
check("deployed worker: a target that changes the proportions is refused (400)", got[0] == 400, got)

shutil.rmtree(cfg_tmp, True)
print("ALL PASS" if ok else "SOME CHECKS FAILED")
