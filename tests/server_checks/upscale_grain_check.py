"""Film grain on artwork upscales, out of a STAGED server.py's AST: the setting,
the plan asking the worker for grain on the anime model only (and keying the
cache on it), the film_grain tag as a hint, the request fields and the
worker's X-Upscale-Grain answer stored with the copy, the cache lookup taking
only copies made the same way, and the ready answer carrying it to the page.
One real library picture is planned, never upscaled; the AI store is a temp one.

  venv/bin/python upscale_grain_check.py /tmp/.../server.py
"""
import ast
import json
import os
import shutil
import sys
import tempfile
import threading
import urllib.request

BASE = os.environ.get("ATRIHORD_DIR") or os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SERVER = sys.argv[1]
sys.path.insert(0, BASE)
tmp = tempfile.mkdtemp(prefix="upscale_grain_check_")
os.environ["KSF_MEDIA_AI_DIR"] = os.path.join(tmp, "ai")
os.makedirs(os.environ["KSF_MEDIA_AI_DIR"])
import media_ai  # noqa: E402

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
    print(("PASS " if cond else "FAIL ") + label + (f"  ({detail})" if detail and not cond else ""), flush=True)


# A loaded function names CONFIG, which resolves to `CONFIG = load_config()`:
# let it load and write back a copy, never the live config.json (writeguard.py).
ns["CONFIG_FILE"] = shutil.copy(os.path.join(BASE, "config.json"), os.path.join(tmp, "config.json"))
for name in ("_read_roots_unlocked", "app", "DEFAULT_CONFIG", "SETTINGS_SCHEMA", "plan_upscale", "_run_upscale",
             "upscale_settings", "parse_grain_header", "GRAIN_INFO_KEYS", "GRAIN_VERSION", "film_grain_hint",
             "cached_upscale_of", "_ready_answer", "_upscale_cv", "upscale_version"):
    load(name)

try:
    # ---- the setting ---------------------------------------------------------
    check("restore_grain is on by default", ns["DEFAULT_CONFIG"]["live_upscale"].get("restore_grain") is True)
    fields = {f["key"]: f for sec in ns["SETTINGS_SCHEMA"] for f in sec.get("fields", [])}
    check("... and is a switch in Settings", fields.get("live_upscale.restore_grain", {}).get("type") == "bool",
          fields.get("live_upscale.restore_grain"))

    # ---- the worker's answer -------------------------------------------------
    parse = ns["parse_grain_header"]
    check("no header: nothing", parse(None) is None and parse("") is None)
    check("not JSON, or not an object: nothing", parse("added") is None and parse("[1, 2]") is None)
    check("an object without 'added': nothing", parse('{"sigma": 5}') is None)
    got = parse(json.dumps({"added": True, "sigma": 5.0789, "kept": 0.22, "added_sigma": 5.07, "mono": 0.991,
                            "size": 0.221, "fine": 2.0, "hinted": False, "seconds": 1.13, "psd": [1, 2], "evil": "<b>"}))
    check("a real answer keeps the panel's fields, rounded", got == {
        "added": True, "sigma": 5.079, "kept": 0.22, "added_sigma": 5.07, "mono": 0.991, "size": 0.221,
        "fine": 2.0, "hinted": False, "seconds": 1.13}, got)
    long_why = parse(json.dumps({"added": False, "why": "x" * 500}))
    check("... and a long reason is cut to 200 characters", long_why and len(long_why["why"]) == 200, long_why)

    # ---- the tagger's hint ---------------------------------------------------
    conn = media_ai.connect(BASE)
    media_ai.upsert(conn, "downloads", "art/grainy.jpg", 10, "image", {"tags": "film_grain"})
    media_ai.upsert(conn, "downloads", "art/faint.jpg", 10, "image", {"tags": "film_grain"})
    media_ai.upsert(conn, "downloads", "art/other.jpg", 10, "image", {"tags": "1girl"})
    ids = {r["path"]: r["id"] for r in conn.execute("SELECT id, path FROM media")}
    conn.execute("INSERT INTO media_tags (media_id, tag, category, p) VALUES (?, 'film_grain', 'general', 0.9)",
                 (ids["art/grainy.jpg"],))
    conn.execute("INSERT INTO media_tags (media_id, tag, category, p) VALUES (?, 'film_grain', 'general', 0.2)",
                 (ids["art/faint.jpg"],))
    conn.execute("INSERT INTO media_tags (media_id, tag, category, p) VALUES (?, '1girl', 'general', 0.99)",
                 (ids["art/other.jpg"],))
    conn.commit()
    ns["BASE_DIR"] = BASE
    cfg = {"tag_min_confidence": 0.35}
    hint = ns["film_grain_hint"]
    check("film_grain tagged at 0.9: a hint", resolve(lambda: hint("downloads", "art/grainy.jpg", cfg)) is True)
    check("tagged under the confidence setting: no hint",
          resolve(lambda: hint("downloads", "art/faint.jpg", cfg)) is False)
    check("other tags only, or not indexed: no hint",
          resolve(lambda: hint("downloads", "art/other.jpg", cfg)) is False
          and resolve(lambda: hint("downloads", "art/missing.jpg", cfg)) is False)

    # ---- the plan --------------------------------------------------------------
    ROOT, PATH, W, H = "downloads", "twitter/@yoake_cos_/2091141486901694803_1.jpg", 2730, 4096
    real_settings = resolve(lambda: ns["upscale_settings"]())
    hints = {"value": True}
    model_pick = {"value": ("anime", "tag 1girl")}
    ns["choose_upscale_model"] = lambda r, p: model_pick["value"]
    ns["film_grain_hint"] = lambda r, p, c=None: hints["value"]
    ns["upscale_model_override"] = lambda r, p: None
    ns["worker_upscale_limit_px"] = lambda refresh=False: 256_000_000

    def plan(**override):
        c = dict(real_settings, enabled=True, max_factor=4, max_output_megapixels=256, **override)
        ns["upscale_settings"] = lambda: c
        return resolve(lambda: ns["plan_upscale"](ROOT, PATH, W * 2, H * 2))

    spec, status = plan(restore_grain=True)
    check("artwork with the setting on: the worker is asked for grain", spec and spec["grain"] is True,
          spec or status)
    check("... with the tagger's hint", spec and spec["grain_hint"] is True, spec and spec.get("grain_hint"))
    hints["value"] = False
    spec_nohint, _ = plan(restore_grain=True)
    check("... and without it when the tag is not there", spec_nohint["grain_hint"] is False)
    spec_off, _ = plan(restore_grain=False)
    check("the setting off: no grain asked, no hint", spec_off["grain"] is False and not spec_off["grain_hint"])
    check("the copy with grain and the one without never share a cache key", spec_off["key"] != spec["key"])
    version = ns["GRAIN_VERSION"]
    check("the plan names the way grain is drawn now (GRAIN_VERSION >= 2)",
          version >= 2 and spec["grain_version"] == version and spec_off["grain_version"] is None,
          (version, spec.get("grain_version"), spec_off.get("grain_version")))
    ns["GRAIN_VERSION"] = version - 1
    spec_old, _ = plan(restore_grain=True)
    ns["GRAIN_VERSION"] = version
    check("... and a copy made under an older version never shares its key", spec_old["key"] != spec["key"])
    model_pick["value"] = ("nomoswebphoto-realplksr", "tag realistic")
    hints["value"] = True
    spec_photo, _ = plan(restore_grain=True)
    check("a photo: no grain, whatever the tagger says - a camera's noise is not a texture",
          spec_photo["grain"] is False and not spec_photo["grain_hint"], spec_photo)

    # ---- the request, and the answer stored with the copy -------------------
    cache = os.path.join(tmp, "cache")
    os.makedirs(cache)
    ns["upscale_cache_dir"] = lambda: cache
    ns["upscale_settings"] = lambda: dict(real_settings, quality=92)
    src = os.path.join(tmp, "src.png")
    open(src, "wb").write(b"\x89PNG not really")
    sent = {}

    class Answer:
        def __init__(self, headers):
            self.headers = headers

        def read(self):
            return b"RIFF....WEBPVP8 "

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(headers):
        def opener(req, timeout=None):
            sent["body"] = req.data
            return Answer(headers)
        return opener

    grain_answer = json.dumps({"added": True, "sigma": 5.08, "kept": 0.22, "added_sigma": 5.07, "mono": 0.99,
                               "size": 0.22, "hinted": True, "seconds": 1.1})
    native = urllib.request.urlopen
    urllib.request.urlopen = fake_urlopen({"Content-Type": "image/webp", "X-Upscale-Size": "200x300",
                                           "X-Upscale-Seconds": "1.5", "X-Upscale-Grain": grain_answer})
    try:
        entry = resolve(lambda: ns["_run_upscale"]({
            "key": "a" * 40, "full": src, "model": "anime", "factor": 2, "root": ROOT, "path": PATH,
            "reason": "tag 1girl", "src": [100, 150], "limited": None, "target": [200, 300],
            "grain": True, "grain_hint": True, "grain_version": ns["GRAIN_VERSION"], "version": "1:2"}))
    finally:
        urllib.request.urlopen = native
    body = sent.get("body") or b""
    check("the request asks for grain", b'name="grain"\r\n\r\n1\r\n' in body)
    check("... and passes the hint", b'name="grain_hint"\r\n\r\n1\r\n' in body)
    check("the copy remembers grain was asked, and how it is drawn now",
          entry.get("grain_asked") is True and entry.get("grain_version") == ns["GRAIN_VERSION"], entry)
    check("... and what the worker did", (entry.get("grain") or {}).get("added") is True
          and entry["grain"]["sigma"] == 5.08, entry.get("grain"))

    urllib.request.urlopen = fake_urlopen({"Content-Type": "image/webp", "X-Upscale-Size": "200x300",
                                           "X-Upscale-Seconds": "1.5"})
    try:
        plain = resolve(lambda: ns["_run_upscale"]({
            "key": "b" * 40, "full": src, "model": "anime", "factor": 2, "root": ROOT, "path": PATH,
            "reason": "tag 1girl", "src": [100, 150], "limited": None, "target": [200, 300],
            "grain": False, "grain_hint": False, "version": "1:2"}))
    finally:
        urllib.request.urlopen = native
    body = sent.get("body") or b""
    check("grain not wanted: the request says so", b'name="grain"\r\n\r\n0\r\n' in body)
    check("... and the copy carries neither mark", "grain_asked" not in plain and "grain" not in plain, plain)

    # ---- the cache lookup ------------------------------------------------------
    pic = os.path.join(tmp, "pic.jpg")
    open(pic, "wb").write(b"x" * 100)
    version = resolve(lambda: ns["upscale_version"](os.stat(pic)))
    entries = {
        "g" * 40: {"root": "downloads", "path": "a/pic.jpg", "model": "anime", "version": version,
                   "out": [400, 400], "factor": 2, "grain_asked": True, "grain_version": ns["GRAIN_VERSION"]},
        "p" * 40: {"root": "downloads", "path": "a/pic.jpg", "model": "anime", "version": version,
                   "out": [800, 800], "factor": 4},
    }
    ns["resolve_media_path"] = lambda r, p: pic
    ns["_upscale_entries"] = lambda: entries
    ns["_touch_upscale"] = lambda key: entries.get(key)
    look = ns["cached_upscale_of"]
    hit = resolve(lambda: look("downloads", "a/pic.jpg", want=[400, 400], model="anime", grain=True))
    check("a plan with grain only takes a copy made with grain", hit and hit[0] == "g" * 40, hit and hit[0])
    hit = resolve(lambda: look("downloads", "a/pic.jpg", want=[400, 400], model="anime", grain=False))
    check("a plan without grain only takes a copy made without it", hit and hit[0] == "p" * 40, hit and hit[0])
    hit = resolve(lambda: look("downloads", "a/pic.jpg"))
    check("no plan (opening a cover from the cache): the copy with grain comes first, even over a larger one",
          hit and hit[0] == "g" * 40, hit and hit[0])
    ns["upscale_settings"] = lambda: dict(real_settings, quality=92, restore_grain=False)
    hit = resolve(lambda: look("downloads", "a/pic.jpg"))
    check("... and with grain switched off, the copy without it", hit and hit[0] == "p" * 40, hit and hit[0])
    ns["upscale_settings"] = lambda: dict(real_settings, quality=92)
    del entries["g" * 40]
    hit = resolve(lambda: look("downloads", "a/pic.jpg", want=[400, 400], model="anime", grain=True))
    check("only a copy from before grain was put back: none - the new one gets made", hit is None, hit)
    entries["o" * 40] = {"root": "downloads", "path": "a/pic.jpg", "model": "anime", "version": version,
                         "out": [900, 900], "factor": 4, "grain_asked": True}
    hit = resolve(lambda: look("downloads", "a/pic.jpg", want=[400, 400], model="anime", grain=True))
    check("a copy with grain drawn the old way (no version mark = 1) is not taken - it gets made again",
          hit is None, hit)
    entries["g" * 40] = {"root": "downloads", "path": "a/pic.jpg", "model": "anime", "version": version,
                         "out": [400, 400], "factor": 2, "grain_asked": True, "grain_version": ns["GRAIN_VERSION"]}
    hit = resolve(lambda: look("downloads", "a/pic.jpg"))
    check("no plan: the copy with today's grain comes first, over a larger one with the old grain",
          hit and hit[0] == "g" * 40, hit and hit[0])
    del entries["g" * 40], entries["o" * 40]
    hit = resolve(lambda: look("downloads", "a/pic.jpg", want=[400, 400]))
    check("no plan (a quick look on opening): any copy will do", hit and hit[0] == "p" * 40, hit and hit[0])

    # ---- the ready answer ------------------------------------------------------
    ns["upscale_model_override"] = lambda r, p: None
    ready = resolve(lambda: ns["_ready_answer"]("a" * 40, dict(entry, file="a.webp"), lambda k: "/u?key=" + k))
    check("the page is told what was done about grain", (ready.get("grain") or {}).get("added") is True, ready)
    old = resolve(lambda: ns["_ready_answer"]("b" * 40, dict(plain, file="b.webp"), lambda k: "/u?key=" + k))
    check("... and a copy without it answers with none", old.get("grain") is None, old)
finally:
    shutil.rmtree(tmp, True)

print("ALL PASS" if ok else "SOME CHECKS FAILED")
sys.exit(0 if ok else 1)
