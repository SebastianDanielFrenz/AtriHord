"""A STAGED server.py, out of its AST in Flask test requests: the upscale model
choice by medium. A photo word in the caption beats an artwork word, an artwork
word beats a costume or a photo in passing, a face detected surely enough beats
the anime tags, "anime" alone decides nothing, photo tags still come first, and
the Settings field for the face score. Synthetic AI index and temp config
copies; nothing under /data2 is written.

  venv/bin/python modelpick_server_check.py /tmp/modelpick/server.py
"""
import ast
import json
import os
import shutil
import sys
import tempfile

BASE = os.environ.get("ATRIHORD_DIR") or os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SERVER = sys.argv[1]
STAGE = os.path.dirname(os.path.abspath(SERVER))
sys.path.insert(0, BASE)
sys.path.insert(0, STAGE)
tmp = tempfile.mkdtemp(prefix="modelpick_check_")
store = os.path.join(tmp, "ai")
os.makedirs(store)
os.environ["KSF_MEDIA_AI_DIR"] = store
for name in ("config.json", "indexer_config.json"):
    if os.path.exists(os.path.join(BASE, name)):
        shutil.copy(os.path.join(BASE, name), os.path.join(tmp, name))

import media_ai                      # noqa: E402

ok = True


def check(label, cond, detail=""):
    global ok
    ok = ok and bool(cond)
    print(("PASS " if cond else "FAIL ") + label + (f"  ({detail})" if detail and not cond else ""))


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


ns["CONFIG_FILE"] = os.path.join(tmp, "config.json")
ns["USERS_FILE"] = os.path.join(tmp, "users.json")
ns["INDEXER_CONFIG_FILE"] = os.path.join(tmp, "indexer_config.json")
ns["USAGE_FILE"] = os.path.join(tmp, "pipeline_usage.json")
ns["DOWNLOAD_VOLUME_FILE"] = os.path.join(tmp, "download_volume.json")
ns["get_authenticated_user"] = lambda: {"username": "alice", "role": "admin"}
# BASE_DIR up front: choose_upscale_model catches its own exceptions, so a name
# first needed inside its try would be reported as "not indexed yet".
for name in ("BASE_DIR", "CONFIG", "DEFAULT_CONFIG", "app", "_read_roots_unlocked", "_read_users_unlocked",
             "_read_usage_unlocked", "UPSCALE_PHOTO_MODELS", "photo_upscale_model", "choose_upscale_model",
             "upscale_settings", "get_settings", "put_settings", "SETTINGS_SCHEMA",
             "UPSCALE_CAPTION_PHOTO", "UPSCALE_CAPTION_ART", "UPSCALE_CAPTION_COSTUME"):
    load(name)
app = ns["app"]
check("server imports the staged media_ai", ns.get("media_ai") is media_ai)
PHOTO = ns["DEFAULT_CONFIG"]["live_upscale"]["photo_model"]

# ---------------------------------------------------------------- synthetic index
REPORTED = ("The image shows a woman with blue hair and a blue wig sitting on the ground in front of a wall. She is "
            "wearing a cosplay costume, likely inspired by the popular anime series Vocaloid, and is likely a fan of the game.")
PLAIN = "The image shows a woman in a white dress standing in front of a wall."
ANIME_WORD = "The image shows a group of people, likely inspired by the popular anime series."
FILES = [  # id, path, caption, tags, face scores
    (1, "t/reported.jpg", REPORTED, [], [0.84]),
    (2, "t/animated.jpg", "This is an animated image. In this image we can see two women on the bed.", [], []),
    (3, "t/banner.jpg", "The image shows a woman in a cosplay costume standing in front of a banner with animated "
                        "pictures and text.", [("1girl", 0.9)], [0.7]),
    (4, "t/pirate.jpg", "The image shows an animated woman in a pirate costume, holding a sword.", [("1girl", 0.9)], [0.86]),
    (5, "t/bunny.jpg", "The image shows a woman in a bunny costume sitting on a chair in front of a window.",
     [("1girl", 0.9)], []),
    (6, "t/face_high.jpg", PLAIN, [("1girl", 0.9)], [0.62, 0.86]),
    (7, "t/face_low.jpg", PLAIN, [("1girl", 0.9)], [0.72]),
    (8, "t/anime_word.jpg", ANIME_WORD, [("1girl", 0.9)], []),
    (9, "t/anime_word_untagged.jpg", ANIME_WORD, [], []),
    (10, "t/realistic_animated.jpg", "This is an animated image of a woman.", [("realistic", 0.9)], [0.88]),
    (11, "t/face_only.jpg", None, [], [0.9]),
    (12, "t/painting.jpg", "The image shows a woman standing in front of a wall with a painting of a cat on it.",
     [("1girl", 0.9)], []),
    (13, "t/selfie.jpg", "The image shows a woman taking a selfie in a mirror.", [("1girl", 0.9), ("comic", 0.8)], []),
]
R = "downloads"
conn = media_ai.connect(tmp)
for mid, path, caption, tags, faces in FILES:
    conn.execute("INSERT INTO media (id, root, path, kind, caption, tag_model) VALUES (?,?,?,?,?,?)",
                 (mid, R, path, "image", caption, "wd" if tags else None))
    for tag, p in tags:
        conn.execute("INSERT INTO media_tags (media_id, tag, category, p) VALUES (?,?,?,?)", (mid, tag, "general", p))
    for score in faces:
        try:
            conn.execute("INSERT INTO faces (media_id, bbox, det_score) VALUES (?,?,?)", (mid, "[0, 0, 10, 10]", score))
        except Exception:
            conn.execute("INSERT INTO faces (media_id, bbox, det_score, embedding, quality) VALUES (?,?,?,?,?)",
                         (mid, "[0, 0, 10, 10]", score, b"\0" * 8, 10.0))
conn.commit()


def choose(path, **settings):
    ns["CONFIG"]["live_upscale"] = dict(settings)
    return resolve(lambda: ns["choose_upscale_model"](R, path))


cases = [
    ("the reported cosplay photo, untagged: its caption names cosplay and a wig",
     "t/reported.jpg", {}, (PHOTO, "caption describes a photo")),
    ("Florence's word for drawings, untagged", "t/animated.jpg", {}, ("anime", "caption describes artwork")),
    ("a photo word beats \"animated pictures\" on a banner behind the cosplayer",
     "t/banner.jpg", {}, (PHOTO, "caption describes a photo")),
    ("an artwork word beats a costume and a 0.86 face", "t/pirate.jpg", {}, ("anime", "caption describes artwork")),
    ("a costume in passing beats 1girl", "t/bunny.jpg", {}, (PHOTO, "caption mentions a costume or a photo")),
    ("the surest face counts, and beats 1girl", "t/face_high.jpg", {}, (PHOTO, "face detected at 0.86")),
    ("a face under 0.8 leaves it to 1girl", "t/face_low.jpg", {}, ("anime", "tag 1girl")),
    ("\"anime\" in the caption alone decides nothing; 1girl does", "t/anime_word.jpg", {}, ("anime", "tag 1girl")),
    ("\"anime\" alone, untagged: the default", "t/anime_word_untagged.jpg", {}, (PHOTO, "no deciding tag")),
    ("photo tags still come first", "t/realistic_animated.jpg", {}, (PHOTO, "tag realistic")),
    ("a face and nothing else", "t/face_only.jpg", {}, (PHOTO, "face detected at 0.90")),
    ("a painting on the wall is not a medium", "t/painting.jpg", {}, ("anime", "tag 1girl")),
    ("a selfie beats the comic tag", "t/selfie.jpg", {}, (PHOTO, "caption describes a photo")),
    ("Settings 0.9: the 0.86 face no longer decides", "t/face_high.jpg", {"face_min_score": 0.9}, ("anime", "tag 1girl")),
    ("Settings 0.85: it does", "t/face_high.jpg", {"face_min_score": 0.85}, (PHOTO, "face detected at 0.86")),
    ("Settings 1 = off: a face alone is undecided", "t/face_only.jpg", {"face_min_score": 1}, (PHOTO, "no deciding tag")),
    ("undecided with default_model anime", "t/anime_word_untagged.jpg", {"default_model": "anime"},
     ("anime", "no deciding tag")),
    ("a file the index has never seen", "x/unknown.jpg", {}, (PHOTO, "not indexed yet")),
    ("Settings' photo upscaler is used for a face decision", "t/face_high.jpg", {"photo_model": "nmkd-siax"},
     ("nmkd-siax", "face detected at 0.86")),
]
for label, path, settings, want in cases:
    got = choose(path, **settings)
    check(label, got == want, (got, want))

# ---------------------------------------------------------------- Settings field
check("default face_min_score is 0.8", ns["DEFAULT_CONFIG"]["live_upscale"].get("face_min_score") == 0.8)
schema_fields = [f for s in ns["SETTINGS_SCHEMA"] if s.get("id") == "live_upscale" for f in s["fields"]]
keys = [f["key"] for f in schema_fields]
field = next((f for f in schema_fields if f["key"] == "live_upscale.face_min_score"), {})
check("the Settings field sits after Tag confidence needed",
      "live_upscale.face_min_score" in keys and keys.index("live_upscale.face_min_score") == keys.index("live_upscale.tag_min_confidence") + 1,
      keys)
check("... a float from 0.5 to 1", field.get("type") == "float" and field.get("min") == 0.5 and field.get("max") == 1, field)


def call(fn, path, method="GET", body=None):
    with app.test_request_context(path, method=method, json=body):
        rv = resolve(lambda: ns[fn]())
    if isinstance(rv, tuple):
        resp, status = rv[0], rv[1]
    else:
        resp, status = rv, rv.status_code
    return status, resp.get_json()


ns["CONFIG"]["live_upscale"] = {}
status, data = call("get_settings", "/api/settings")
check("GET /api/settings shows 0.8 when config.json has no value",
      status == 200 and (data or {}).get("values", {}).get("live_upscale.face_min_score") == 0.8,
      (status, (data or {}).get("values", {}).get("live_upscale.face_min_score")))
status, data = call("put_settings", "/api/settings", "PUT", {"values": {"live_upscale.face_min_score": 0.85}})
saved = json.load(open(os.path.join(tmp, "config.json"), encoding="utf-8"))
check("saving 0.85", status == 200 and saved.get("live_upscale", {}).get("face_min_score") == 0.85, (status, data))
for bad in (1.5, 0.3):
    status, data = call("put_settings", "/api/settings", "PUT", {"values": {"live_upscale.face_min_score": bad}})
    check("%s is refused, by name" % bad, status == 400 and "live_upscale.face_min_score" in (data or {}).get("errors", {}),
          (status, data))

shutil.rmtree(tmp, ignore_errors=True)
print("ALL PASS" if ok else "SOME CHECKS FAILED")
