"""A STAGED server.py, out of its AST in Flask test requests: live upscaling for
music covers. Real audio files with embedded covers (m4a, opus, mp3) in a temp
library, a fake vision worker (urlopen replaced - nothing leaves this process),
the real job loop in a thread. Temp config, AI index, cover store and cache;
nothing under /data2 is written.

  venv/bin/python cover_server_check.py /tmp/cover/server.py
"""
import ast
import base64
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

BASE = os.environ.get("ATRIHORD_DIR") or os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SERVER = sys.argv[1]
STAGE = os.path.dirname(os.path.abspath(SERVER))
sys.path.insert(0, BASE)
sys.path.insert(0, STAGE)
tmp = tempfile.mkdtemp(prefix="cover_check_")
store = os.path.join(tmp, "ai")
os.makedirs(store)
os.environ["KSF_MEDIA_AI_DIR"] = store
for name in ("config.json", "indexer_config.json"):
    if os.path.exists(os.path.join(BASE, name)):
        shutil.copy(os.path.join(BASE, name), os.path.join(tmp, name))

import media_ai                      # noqa: E402
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
cache = os.path.join(tmp, "cache")
os.makedirs(os.path.join(lib, "music"))
os.makedirs(cache)
ns["CONFIG_FILE"] = os.path.join(tmp, "config.json")
ns["USERS_FILE"] = os.path.join(tmp, "users.json")
ns["get_authenticated_user"] = lambda: {"username": "alice", "role": "admin"}
ns["UPSCALE_COVER_STORE"] = os.path.join(tmp, "upscale_covers.json")
ns["SOFT_EDGE_STORE"] = os.path.join(tmp, "upscale_cover_edges.json")
for name in ("BASE_DIR", "CONFIG", "DEFAULT_CONFIG", "app", "_read_roots_unlocked", "_read_users_unlocked",
             "UPSCALE_PHOTO_MODELS", "photo_upscale_model", "choose_upscale_model", "upscale_model_from",
             "upscale_settings", "UPSCALE_CAPTION_PHOTO", "UPSCALE_CAPTION_ART", "UPSCALE_CAPTION_COSTUME",
             "classify_media_type", "MEDIA_EXTENSIONS", "get_embedded_cover", "get_mka_cover", "UPSCALE_EXTS",
             "cover_upscale_model", "_cover_known", "_cover_store", "_analyse_cover_now", "analyse_cover",
             "upscale_cover_size", "upscale_cover_bytes", "plan_upscale", "_run_upscale", "_upscale_worker_loop",
             # _run_upscale runs on the upscale thread, where a missing name is
             # not loaded on demand - everything it calls must be here.
             "parse_grain_header", "GRAIN_INFO_KEYS", "film_grain_hint",
             "media_upscale", "media_upscaled_file", "_touch_upscale", "_upscale_entries", "_save_upscale_index",
             "vision_worker_address", "UpscaleLimitError", "note_worker_upscale_limit", "UPSCALE_MIN_SCALE",
             "UPSCALE_QUEUE_MAX", "UPSCALE_COVER_STORE_MAX", "_upscale_cv", "_upscale_queue", "_upscale_jobs",
             "_upscale_index", "_cover_lock", "_cover_sizes", "_cover_analysis", "_cover_failures", "_cover_busy",
             "soft_cover_blob", "soft_cover_folder", "_soft_folder_rules", "_song_names", "_soft_names",
             "cached_upscale_of", "upscale_version", "cover_size_cap", "_over_cover_cap",
             "edge_steepness", "_max_filter", "cover_edge_steepness", "_soft_edge_store", "_UNMEASURED",
             "SOFT_AUTO_MAX_SCALE", "SOFT_EDGE_STORE_MAX", "_soft_edge_lock", "_soft_edges"):
    if name not in ns:
        load(name)
# The music catalogue the soft-cover rule reads a song's other names from, and the roots it names folders by.
ns["_music_lock"] = threading.Lock()
ns["_music"] = {"catalog": None}
ns["get_all_roots"] = lambda: [{"id": "downloads", "name": "Downloads", "path": "/lib"},
                               {"id": "jf", "name": "Jellyfin", "path": "/jf"}]
app = ns["app"]
check("the staged cover store is the temp one", ns["UPSCALE_COVER_STORE"].startswith(tmp))
R = "downloads"
PHOTO = ns["DEFAULT_CONFIG"]["live_upscale"]["photo_model"]
ns["resolve_media_path"] = lambda root, rel: os.path.join(lib, rel.replace("/", os.sep))
ns["root_visible_to"] = lambda user, root: True
ns["media_dimensions_map"] = lambda: {}
ns["worker_upscale_limit_px"] = lambda refresh=False: 256_000_000
ns["upscale_cache_dir"] = lambda: cache
ns["CONFIG"]["live_upscale"] = {"enabled": True, "max_output_megapixels": 64, "max_factor": 4}


# ---------------------------------------------------------------- test library
def png(w, h, colour):
    buf = io.BytesIO()
    Image.new("RGB", (w, h), colour).save(buf, "PNG")
    return buf.getvalue()


def jpeg(w, h, colour):
    buf = io.BytesIO()
    Image.new("RGB", (w, h), colour).save(buf, "JPEG", quality=90)
    return buf.getvalue()


def checker(w, h, blur=None):
    """Hard-edged 60 px squares - edges one pixel wide - or, with blur, the
    same shrunk by that factor and stretched back: soft edges, as a small
    render stretched to 4K has them."""
    import numpy as np
    y, x = np.indices((h, w))
    img = Image.fromarray((((x // 60 + y // 60) % 2) * 255).astype(np.uint8), "L")
    if blur:
        img = img.resize((w // blur, h // blur), Image.BICUBIC).resize((w, h), Image.BICUBIC)
    buf = io.BytesIO()
    img.convert("RGB").save(buf, "PNG")
    return buf.getvalue()


def ffmpeg(*args):
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo",
                    "-t", "1", *args], check=True)


COVERS = {"music/anime.m4a": png(300, 300, (200, 30, 90)), "music/photo.opus": jpeg(400, 400, (30, 120, 200)),
          "music/face.mp3": jpeg(320, 320, (90, 90, 90)), "music/plain.m4a": png(250, 250, (10, 10, 10)),
          "music/big.m4a": png(3000, 3000, (1, 2, 3)), "music/down.m4a": png(260, 260, (4, 5, 6)),
          "music/busy.m4a": png(280, 280, (7, 8, 9)),
          # Soft covers (vault KSF-060): a listed channel's 4K and 1080p covers, another channel's 4K one.
          "music/MHM/soft.m4a": png(3840, 2160, (11, 12, 13)), "music/MHM/small.m4a": png(1920, 1080, (17, 18, 19)),
          "music/Geo/sharp.m4a": png(3840, 2160, (14, 15, 16)),
          # Soft covers found by measuring: real edges, hard or stretched from a quarter/eighth of the size.
          "music/Other/crisp.m4a": checker(3840, 2160), "music/Other/blurry.m4a": checker(3840, 2160, blur=4),
          "music/Other/blurry2.m4a": checker(3840, 2160, blur=4), "music/Other/blurry8k.m4a": checker(7680, 4320, blur=8),
          "music/Other/blurry1440.m4a": checker(2560, 1440, blur=4),
          "music/Other/blurry1080.m4a": checker(1920, 1080, blur=4)}
from mutagen.mp4 import MP4, MP4Cover          # noqa: E402
from mutagen.oggopus import OggOpus            # noqa: E402
from mutagen.flac import Picture               # noqa: E402
from mutagen.id3 import ID3, APIC              # noqa: E402
for rel, blob in COVERS.items():
    full = os.path.join(lib, rel)
    os.makedirs(os.path.dirname(full), exist_ok=True)
    ext = os.path.splitext(rel)[1]
    if ext == ".m4a":
        ffmpeg("-c:a", "aac", full)
        f = MP4(full)
        f["covr"] = [MP4Cover(blob, imageformat=MP4Cover.FORMAT_PNG)]
        f.save()
    elif ext == ".opus":
        ffmpeg("-c:a", "libopus", full)
        f = OggOpus(full)
        pic = Picture()
        pic.type, pic.mime, pic.data = 3, "image/jpeg", blob
        f["metadata_block_picture"] = [base64.b64encode(pic.write()).decode("ascii")]
        f.save()
    else:
        ffmpeg("-c:a", "libmp3lame", full)
        f = ID3(full)
        f.add(APIC(encoding=3, mime="image/jpeg", type=3, desc="Cover", data=blob))
        f.save()
ffmpeg("-c:a", "aac", os.path.join(lib, "music/nocover.m4a"))
Image.new("RGB", (200, 150), (50, 60, 70)).save(os.path.join(lib, "pic.jpg"), "JPEG")

# ---------------------------------------------------------------- fake vision worker
# What /v1/analyze says per cover, by the cover's size (the only thing the fake can see).
ANALYSIS = {
    300: {"caption": "The image shows an animated girl with pink hair holding a microphone.",
          "tags": {"general": [{"tag": "1girl", "p": 0.95}], "character": [], "rating": [{"tag": "general", "p": 0.9}]},
          "faces": []},
    400: {"caption": "The image shows a city at night.", "tags": {"general": [{"tag": "realistic", "p": 0.8}],
                                                                  "character": [], "rating": []}, "faces": []},
    320: {"caption": "The image shows a man standing in front of a wall.", "tags": {"general": [], "character": [],
                                                                                   "rating": []},
          "faces": [{"det_score": 0.62}, {"det_score": 0.9312}]},
    250: {"caption": "The image shows a black square.", "tags": {}, "faces": []},
    280: {"caption": "The image shows an animated cat.", "tags": {}, "faces": []},
    3840: {"caption": "The image shows a colourful abstract pattern.", "tags": {}, "faces": []},
    1920: {"caption": "The image shows a colourful abstract pattern.", "tags": {}, "faces": []},
    7680: {"caption": "The image shows a colourful abstract pattern.", "tags": {}, "faces": []},
    2560: {"caption": "The image shows a colourful abstract pattern.", "tags": {}, "faces": []},
}
calls = {"analyze": [], "upscale": []}
worker = {"down": False, "gate": None}


class FakeResponse:
    def __init__(self, body, headers):
        self.body, self.headers = body, headers

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self.body


def image_in(body):
    start = body.index(b"\r\n\r\n") + 4
    end = body.index(b"\r\n--", start)
    return body[start:end]


def fake_urlopen(req, timeout=None):
    url = req.full_url if hasattr(req, "full_url") else str(req)
    if url.endswith("/v1/analyze"):
        body = req.data
        calls["analyze"].append(body)
        if worker["gate"]:
            worker["gate"].wait(10)
        if worker["down"]:
            raise urllib.error.HTTPError(url, 503, "busy", {}, io.BytesIO(b'{"error": "outside processing window"}'))
        w = Image.open(io.BytesIO(image_in(body))).size[0]
        return FakeResponse(json.dumps({"results": [{"name": "cover", **ANALYSIS[w]}], "failed": []}).encode(), {})
    if url.endswith("/v1/upscale"):
        body = req.data
        calls["upscale"].append(body)
        w, h = Image.open(io.BytesIO(image_in(body))).size
        return FakeResponse(b"RIFF0000WEBP", {"Content-Type": "image/webp", "X-Upscale-Size": f"{w * 2}x{h * 2}",
                                               "X-Upscale-Seconds": "1.5"})
    raise AssertionError("the harness let a request out: " + url)


real_urlopen = urllib.request.urlopen
urllib.request.urlopen = fake_urlopen
ns["urllib"].request.urlopen = fake_urlopen
threading.Thread(target=ns["_upscale_worker_loop"], daemon=True).start()


def ask(rel, check_only=False, need=(600, 600)):
    body = {"root": R, "path": rel, "need_w": need[0], "need_h": need[1]}
    if check_only:
        body["check_only"] = True
    with app.test_request_context("/api/media/upscale", method="POST", json=body):
        rv = resolve(lambda: ns["media_upscale"]())
    resp = rv[0] if isinstance(rv, tuple) else rv
    return resp.get_json()


def until_ready(rel, need=(600, 600)):
    for _ in range(100):
        data = ask(rel, need=need)
        if data.get("status") not in ("queued", "working"):
            return data
        time.sleep(0.05)
    return data


try:
    # ------------------------------------------------------------ covers are read
    for rel, blob in COVERS.items():
        full = os.path.join(lib, rel)
        size = resolve(lambda: ns["upscale_cover_size"](full, os.stat(full)))
        want = Image.open(io.BytesIO(blob)).size
        check(f"the cover size of {rel} is read: {want}", size == want, size)
    full = os.path.join(lib, "music/nocover.m4a")
    check("a track with no cover has no size", resolve(lambda: ns["upscale_cover_size"](full, os.stat(full))) is None)
    reads = []
    real_bytes = ns["upscale_cover_bytes"]
    ns["upscale_cover_bytes"] = lambda f: reads.append(f) or real_bytes(f)
    full = os.path.join(lib, "music/anime.m4a")
    ns["upscale_cover_size"](full, os.stat(full))
    check("the size is remembered: asking again reads nothing out of the file", reads == [], reads)
    ns["upscale_cover_bytes"] = real_bytes

    # ------------------------------------------------------------ the check on opening
    data = ask("music/anime.m4a", check_only=True)
    check("opening a track: status none, the cover not analysed yet", data == {"status": "none"}, data)
    check("... and the worker is not asked on opening", calls["analyze"] == [], len(calls["analyze"]))
    data = ask("music/nocover.m4a")
    check("a track with no cover: unsupported", data.get("status") == "unsupported", data)
    data = ask("music/big.m4a")
    check("a 3000px cover on a 600px need: not needed, with its size", data.get("status") == "not_needed"
          and data.get("width") == 3000, data)
    check("... and no analysis for a cover that needs no upscale", calls["analyze"] == [], len(calls["analyze"]))

    # ------------------------------------------------------------ the ask after view_seconds
    data = ask("music/anime.m4a")
    check("the ask after viewing: queued, on the anime model, because of the cover's caption",
          data.get("status") in ("queued", "working") and data.get("model") == "anime"
          and data.get("reason") == "cover: caption describes artwork", data)
    check("... after one analysis", len(calls["analyze"]) == 1, len(calls["analyze"]))
    sent = calls["analyze"][0]
    check("... asked for caption, tags and faces, as urgent", b'name="tasks"\r\n\r\ncaption,tags,faces\r\n' in sent
          and b'name="urgent"\r\n\r\n1\r\n' in sent)
    check("... with the cover's bytes, not the audio file", image_in(sent) == COVERS["music/anime.m4a"])
    data = until_ready("music/anime.m4a")
    check("the job finishes: ready, flagged as a cover", data.get("status") == "ready" and data.get("cover") is True, data)
    check("... the worker upscaled the cover itself", calls["upscale"] and image_in(calls["upscale"][-1]) == COVERS["music/anime.m4a"])
    check("... 2x of 300: 600x600", data.get("width") == 600 and data.get("factor") == 2, data)
    check("... still one analysis", len(calls["analyze"]) == 1, len(calls["analyze"]))
    data = ask("music/anime.m4a", check_only=True)
    check("the next opening finds it cached at once", data.get("status") == "ready", data)

    saved = json.load(open(ns["UPSCALE_COVER_STORE"], encoding="utf-8"))
    entry = saved.get(R + "\0music/anime.m4a") or {}
    full = os.path.join(lib, "music/anime.m4a")
    check("the analysis is stored with the file's size and mtime", entry.get("size") == os.path.getsize(full)
          and entry.get("mtime_ns") == os.stat(full).st_mtime_ns and entry.get("tags", {}).get("1girl") == 0.95, entry)
    ns["_cover_analysis"].update(loaded=False, entries={})
    data = ask("music/anime.m4a", check_only=True)
    check("after a restart the stored analysis still finds the cached copy", data.get("status") == "ready", data)

    # ------------------------------------------------------------ the same rules
    for rel, want in (("music/photo.opus", (PHOTO, "cover: tag realistic")),
                      ("music/face.mp3", (PHOTO, "cover: face detected at 0.93")),
                      ("music/plain.m4a", (PHOTO, "cover: nothing in it decides"))):
        data = ask(rel)
        check(f"{rel}: {want}", (data.get("model"), data.get("reason")) == want, data)
        data = until_ready(rel)
        check(f"... ready", data.get("status") == "ready", data)
    ns["CONFIG"]["live_upscale"]["default_model"] = "anime"
    ns["CONFIG"]["live_upscale"]["face_min_score"] = 0.95
    data = ask("music/face.mp3", check_only=True)
    check("Settings apply to a stored analysis: face score 0.95 and default anime -> anime, no new analysis",
          data.get("status") == "none" and data.get("model") == "anime", data)
    check("... (still four analyses)", len(calls["analyze"]) == 4, len(calls["analyze"]))
    del ns["CONFIG"]["live_upscale"]["default_model"], ns["CONFIG"]["live_upscale"]["face_min_score"]

    # ------------------------------------------------------------ a changed file
    full = os.path.join(lib, "music/anime.m4a")
    os.utime(full, ns=(time.time_ns(), time.time_ns() + 5_000_000_000))
    data = ask("music/anime.m4a", check_only=True)
    check("a track changed on disk: its analysis no longer counts", data == {"status": "none"}, data)

    # ------------------------------------------------------------ the worker refusing
    worker["down"] = True
    data = ask("music/down.m4a")
    check("worker refuses the analysis: the default model, and why",
          data.get("status") in ("queued", "working") and data.get("model") == PHOTO
          and (data.get("reason") or "").startswith("cover not analysed: the vision worker refused it (HTTP 503)"), data)
    n = len(calls["analyze"])
    data = until_ready("music/down.m4a")
    check("... the cover is still upscaled", data.get("status") == "ready", data)
    worker["down"] = False
    ask("music/down.m4a")
    check("... and the worker is not asked again within ten minutes", len(calls["analyze"]) == n, len(calls["analyze"]))

    # ------------------------------------------------------------ two asks at once
    worker["gate"] = threading.Event()
    first = {}
    t = threading.Thread(target=lambda: first.update(ask("music/busy.m4a")))
    t.start()
    for _ in range(100):
        if len(calls["analyze"]) > n:
            break
        time.sleep(0.02)
    data = ask("music/busy.m4a")
    check("a second ask while the first is analysing: working, no second analysis",
          data == {"status": "working"} and len(calls["analyze"]) == n + 1, (data, len(calls["analyze"]) - n))
    worker["gate"].set()
    t.join(10)
    worker["gate"] = None
    check("... the first ask gets its answer", first.get("model") == "anime", first)

    # ------------------------------------------------------------ download name
    key = [k for k, e in ns["_upscale_entries"]().items() if e.get("path") == "music/plain.m4a"][0]
    open(os.path.join(cache, ns["_upscale_entries"]()[key]["file"]), "wb").write(b"RIFF0000WEBP")
    with app.test_request_context(f"/api/media/upscaled?key={key}&download=1"):
        rv = resolve(lambda: ns["media_upscaled_file"]())
    cd = rv.headers.get("Content-Disposition", "")
    rv.close()
    check("Download upscaled names the cover: plain.cover.upscaled-2x.webp", "plain.cover.upscaled-2x.webp" in cd, cd)

    # ------------------------------------------------------------ pictures unchanged
    data = ask("pic.jpg", check_only=True)
    check("a picture's check on opening is unchanged: none, with its model", data.get("status") == "none"
          and data.get("model") == PHOTO, data)
    data = until_ready("pic.jpg")
    check("a picture is upscaled from its own bytes, no cover flag", data.get("status") == "ready" and not data.get("cover")
          and image_in(calls["upscale"][-1]) == open(os.path.join(lib, "pic.jpg"), "rb").read(), data)
    check("... and without an analysis", len(calls["analyze"]) == n + 1, len(calls["analyze"]))

    # ------------------------------------------------------------ soft covers (vault KSF-060)
    UHD, BIG = (3840, 2160), (7680, 4320)
    lu = ns["CONFIG"]["live_upscale"]
    # Before its folder is listed: a 4K cover on a 4K screen is not needed; on an 8K need it is
    # upscaled from the whole cover - the copy a soft plan must NOT take for its own.
    data = ask("music/MHM/soft.m4a", need=UHD)
    check("unlisted: a 3840 x 2160 cover on a 4K screen is not needed", data.get("status") == "not_needed"
          and data.get("width") == 3840 and "soft" not in data, data)
    data = until_ready("music/MHM/soft.m4a", need=BIG)
    check("... an 8K need upscales the whole cover (3840 wide sent)", data.get("status") == "ready"
          and Image.open(io.BytesIO(image_in(calls["upscale"][-1]))).size == UHD and not data.get("soft"), data)
    ups = len(calls["upscale"])

    lu["soft_cover_folders"] = ["Downloads / music/MHM"]
    data = ask("music/MHM/soft.m4a", need=UHD)
    check("listed: the 4K cover is taken as 2560 x 1440 and planned 2x, not taken from the cache",
          data.get("status") in ("queued", "working") and data.get("factor") == 2, data)
    data = until_ready("music/MHM/soft.m4a", need=UHD)
    sent = Image.open(io.BytesIO(image_in(calls["upscale"][-1])))
    check("... the worker got the cover shrunk to 2560 x 1440, as PNG", len(calls["upscale"]) == ups + 1
          and sent.size == (2560, 1440) and sent.format == "PNG", (len(calls["upscale"]) - ups, sent.size, sent.format))
    check("... the shrunk picture is the cover's own colour", sent.convert("RGB").getpixel((5, 5)) == (11, 12, 13),
          sent.convert("RGB").getpixel((5, 5)))
    check("... ready, and the answer says what it was taken as and why",
          data.get("status") == "ready" and data.get("soft") == {"height": 1440, "folder": "music/MHM",
                                                                 "file": [3840, 2160], "size": [2560, 1440]}, data)
    check("... served at 5120 x 2880 - bigger than the screen needs, never shrunk under 1440p (the fake worker doubles)",
          (data.get("width"), data.get("height")) == (5120, 2880) and data.get("scale") == 2, data)
    data = ask("music/MHM/soft.m4a", check_only=True, need=UHD)
    check("the next opening finds the soft copy at once", data.get("status") == "ready" and data.get("soft"), data)
    plan = lambda path, need: resolve(lambda: ns["plan_upscale"](R, path, need[0], need[1]))
    spec, status = plan("music/MHM/soft.m4a", (1280, 720))
    check("a small screen the soft size fills: never 'not needed', still 2x from 2560 x 1440",
          spec and spec.get("factor") == 2 and spec.get("src") == [2560, 1440]
          and (spec.get("soft") or {}).get("size") == [2560, 1440], (spec, status))
    spec, status = plan("music/MHM/soft.m4a", (2560, 1440))
    check("... a 1440p screen too (it came out 'not needed' there, 2026-09-28)",
          spec and spec.get("factor") == 2 and spec.get("target") == [5120, 2880], (spec, status))

    data = ask("music/Geo/sharp.m4a", need=UHD)
    check("another channel's 4K cover is still not needed (Geoxor stays as it is)",
          data.get("status") == "not_needed" and data.get("width") == 3840, data)
    data = until_ready("music/MHM/small.m4a", need=UHD)
    check("a listed channel's 1080p cover is not shrunk: soft at its own 1920 x 1080, upscaled from it",
          data.get("status") == "ready" and data.get("soft") == {"height": 1080, "folder": "music/MHM",
                                                                 "file": [1920, 1080], "size": [1920, 1080]}
          and Image.open(io.BytesIO(image_in(calls["upscale"][-1]))).size == (1920, 1080), data)
    for need in ((1920, 1080), (1280, 720)):
        spec, status = resolve(lambda: ns["plan_upscale"](R, "music/MHM/small.m4a", need[0], need[1]))
        check(f"... never 'not needed' on a screen it fills ({need[0]} x {need[1]}): 2x, 3840 x 2160 "
              "(a 2560 x 1440 MHM cover was, 2026-09-28)",
              spec and spec.get("factor") == 2 and spec.get("src") == [1920, 1080]
              and spec.get("target") == [3840, 2160], (spec, status))

    # A playlist's hard link to the song: the catalogue knows it by both names.
    os.makedirs(os.path.join(lib, "music/Mix"))
    os.link(os.path.join(lib, "music/MHM/soft.m4a"), os.path.join(lib, "music/Mix/soft.m4a"))
    data = ask("music/Mix/soft.m4a", need=UHD)
    check("without a catalogue, a playlist's link is only its own folder: not needed",
          data.get("status") == "not_needed" and "soft" not in data, data)
    ns["_music"]["catalog"] = {"songs": [{"root": R, "path": "music/MHM/soft.m4a",
                                          "names": [[R, "music/Mix/soft.m4a"]]}]}
    data = until_ready("music/Mix/soft.m4a", need=UHD)
    check("with the catalogue, the playlist's link is a soft cover too", data.get("status") == "ready"
          and (data.get("soft") or {}).get("folder") == "music/MHM", data)

    fold = lambda path, rules: resolve(lambda: ns["soft_cover_folder"](R, path, dict(lu, soft_cover_folders=rules)))
    check("a bare path counts in every root", fold("music/MHM/x.m4a", ["music/MHM"]) == "music/MHM")
    check("subfolders count", fold("music/MHM/2024/x.m4a", ["Downloads / music/MHM"]) == "music/MHM")
    check("a folder is matched whole: music/MH is not music/MHM", fold("music/MHM/x.m4a", ["music/MH"]) is None)
    check("another root's entry does not count here", fold("music/MHM/x.m4a", ["Jellyfin / music/MHM"]) is None)
    check("the root name in any case, spaces around the path", fold("music/MHM/x.m4a", ["downloads /  music/MHM/ "])
          == "music/MHM")
    import unicodedata
    check("the same name in another Unicode form still matches (NFD entry, NFC file name)",
          fold("music/Café/x.m4a", [unicodedata.normalize("NFD", "music/Café")]) is not None)
    check("an empty list: no soft covers", fold("music/MHM/x.m4a", []) is None)
    spec, status = plan("music/MHM/soft.m4a", UHD)
    check("the plan of a soft cover starts from the size it is taken as",
          spec and spec["soft"]["size"] == [2560, 1440] and spec["src"] == [2560, 1440], (spec, status))
    lu["soft_cover_height"] = 0
    spec, status = plan("music/MHM/soft.m4a", UHD)
    check("soft_cover_height 0 turns it off: the listed 4K cover is not needed again",
          spec is None and status.get("status") == "not_needed" and status.get("width") == 3840, status)
    lu.pop("soft_cover_folders"), lu.pop("soft_cover_height")

    # ------------------------------------------------------------ soft covers found by measuring (KSF-060)
    real_steep = ns["edge_steepness"]
    measured = []
    ns["edge_steepness"] = lambda blob: measured.append(1) or real_steep(blob)
    crisp_e = real_steep(COVERS["music/Other/crisp.m4a"])
    blurry_e = real_steep(COVERS["music/Other/blurry.m4a"])
    e8k = real_steep(COVERS["music/Other/blurry8k.m4a"])
    check("edge steepness: hard one-pixel edges measure 1.0", abs(crisp_e - 1.0) < 0.01, crisp_e)
    check("... edges stretched from a quarter of the size measure under the 0.40 limit", blurry_e < 0.40, blurry_e)
    check("... an 8K cover is measured as a 4K screen shows it: stretched 8x is 4x there, soft too",
          e8k < 0.40 and abs(e8k - blurry_e) < 0.1, (e8k, blurry_e))
    check("... a flat cover has no edges to tell by: None, never soft", real_steep(COVERS["music/MHM/soft.m4a"]) is None)
    edge_store = lambda: (json.load(open(ns["SOFT_EDGE_STORE"], encoding="utf-8"))
                     if os.path.exists(ns["SOFT_EDGE_STORE"]) else {})
    measure_plan = lambda path, need: resolve(lambda: ns["plan_upscale"](R, path, need[0], need[1], analyse=True))

    data = ask("music/Other/blurry.m4a", check_only=True, need=UHD)
    check("a 4K cover not measured yet: the check on opening answers none at once, measuring nothing",
          data == {"status": "none"} and not measured and (R + "\0music/Other/blurry.m4a") not in edge_store(),
          (data, measured))
    data = ask("music/Other/blurry.m4a", need=UHD)
    check("the ask after it measures the cover and finds it soft: planned 2x",
          data.get("status") in ("queued", "working") and data.get("factor") == 2 and len(measured) == 1,
          (data, measured))
    ups = len(calls["upscale"])
    data = until_ready("music/Other/blurry.m4a", need=UHD)
    sent = Image.open(io.BytesIO(image_in(calls["upscale"][-1])))
    check("... the worker got it shrunk to 2560 x 1440, never under 1440p", len(calls["upscale"]) == ups + 1
          and sent.size == (2560, 1440), sent.size)
    soft = data.get("soft") or {}
    check("... ready, and the answer says it was found by measuring, with the value and the limit",
          data.get("status") == "ready" and soft.get("folder") is None and soft.get("size") == [2560, 1440]
          and soft.get("file") == [3840, 2160] and soft.get("height") == 1440
          and soft.get("auto") == {"edge": round(blurry_e, 3), "limit": 0.4}, data)
    check("... measured once, however often it was asked", len(measured) == 1, measured)
    full = os.path.join(lib, "music/Other/blurry.m4a")
    entry = edge_store().get(R + "\0music/Other/blurry.m4a") or {}
    check("the measurement is stored with the file's size and mtime", entry.get("size") == os.path.getsize(full)
          and entry.get("mtime_ns") == os.stat(full).st_mtime_ns and abs(entry.get("edge", 9) - blurry_e) < 1e-3,
          entry)
    ns["_soft_edges"].update(loaded=False, entries={})
    data = ask("music/Other/blurry.m4a", check_only=True, need=UHD)
    check("after a restart the next opening finds the soft copy at once, measuring nothing",
          data.get("status") == "ready" and (data.get("soft") or {}).get("auto") and len(measured) == 1,
          (data, measured))

    data = ask("music/Other/crisp.m4a", need=UHD)
    check("a crisp 4K cover is measured and stays not needed", data.get("status") == "not_needed"
          and data.get("width") == 3840 and "soft" not in data and len(measured) == 2, (data, measured))
    ask("music/Other/crisp.m4a", need=UHD)
    check("... and is not measured again", len(measured) == 2, measured)
    data = until_ready("music/Other/blurry8k.m4a", need=UHD)
    check("an 8K soft cover is found too, and shrunk to 2560 x 1440",
          data.get("status") == "ready" and (data.get("soft") or {}).get("file") == [7680, 4320]
          and (data.get("soft") or {}).get("auto")
          and Image.open(io.BytesIO(image_in(calls["upscale"][-1]))).size == (2560, 1440), data)
    spec, status = measure_plan("music/Other/blurry8k.m4a", (1920, 1080))
    check("a screen the soft size already fills: still upscaled 2x from it - soft covers always are",
          spec and spec.get("factor") == 2 and (spec.get("soft") or {}).get("size") == [2560, 1440]
          and (spec.get("soft") or {}).get("auto") and len(measured) == 3, (spec, status, measured))
    # Every cover since 2026-09-28, not only 4K - but never taken under
    # soft_cover_height (1440): a 1440p or 1080p cover is not shrunk, so not measured either.
    m = len(measured)
    data = until_ready("music/Other/blurry1440.m4a", need=UHD)
    check("a soft 2560 x 1440 cover is never shrunk under 1440p: not measured, upscaled from its own size",
          data.get("status") == "ready" and not data.get("soft") and len(measured) == m
          and Image.open(io.BytesIO(image_in(calls["upscale"][-1]))).size == (2560, 1440), data)
    spec, status = measure_plan("music/Other/blurry1080.m4a", UHD)
    check("... nor a 1080p one: planned 2x from its own size, nothing measured",
          spec and not spec.get("soft") and spec.get("src") == [1920, 1080] and len(measured) == m, (spec, status))

    # A lower soft_cover_height lets smaller covers be taken smaller - never
    # below half the size they are measured at.
    lu["soft_cover_height"] = 540
    data = until_ready("music/Other/blurry1440.m4a", need=UHD)
    soft = data.get("soft") or {}
    check("at 540 the soft 1440p cover is measured and taken as 1280 x 720 - half its size, not 540 (3x to 4K)",
          data.get("status") == "ready" and soft.get("size") == [1280, 720] and soft.get("height") == 720
          and soft.get("file") == [2560, 1440] and soft.get("auto") and len(measured) == m + 1
          and data.get("factor") == 3
          and Image.open(io.BytesIO(image_in(calls["upscale"][-1]))).size == (1280, 720), data)
    lu["soft_cover_auto_min_long"] = 3800
    spec, status = measure_plan("music/Other/blurry1440.m4a", UHD)
    check("soft_cover_auto_min_long 3800: only 4K covers again - the 1440p one is planned from its own size",
          spec and not spec.get("soft") and spec.get("src") == [2560, 1440] and len(measured) == m + 1,
          (spec, status))
    lu.pop("soft_cover_auto_min_long")

    # A copy made before covers of its size were measured: not shown on the
    # check (the page would keep it and never ask), measured on the ask.
    lu["soft_cover_auto"] = False
    data = until_ready("music/Other/blurry1080.m4a", need=UHD)
    check("(a 1080p cover upscaled with measuring off: 2x from its own size)",
          data.get("status") == "ready" and not data.get("soft")
          and Image.open(io.BytesIO(image_in(calls["upscale"][-1]))).size == (1920, 1080), data)
    lu["soft_cover_auto"] = True
    m = len(measured)
    data = ask("music/Other/blurry1080.m4a", check_only=True, need=UHD)
    check("measuring on again: the check answers none instead of the old copy, measuring nothing",
          data == {"status": "none"} and len(measured) == m, data)
    data = until_ready("music/Other/blurry1080.m4a", need=UHD)
    soft = data.get("soft") or {}
    check("... the ask measures it: soft, taken as 960 x 540 and made 4x",
          data.get("status") == "ready" and soft.get("size") == [960, 540] and soft.get("height") == 540
          and soft.get("auto") and data.get("factor") == 4 and len(measured) == m + 1
          and Image.open(io.BytesIO(image_in(calls["upscale"][-1]))).size == (960, 540), data)
    data = ask("music/Other/blurry1080.m4a", check_only=True, need=UHD)
    check("... and the next opening shows the soft copy at once",
          data.get("status") == "ready" and ((data.get("soft") or {}).get("auto")) and len(measured) == m + 1, data)
    lu["soft_cover_auto"] = False
    sharp_old = until_ready("music/Other/crisp.m4a", need=(7680, 4320))
    lu["soft_cover_auto"] = True
    ups = len(calls["upscale"])
    ns["_soft_edges"]["entries"].pop(R + "\0music/Other/crisp.m4a", None)
    data = ask("music/Other/crisp.m4a", check_only=True, need=(7680, 4320))
    first = data.get("status")
    data = ask("music/Other/crisp.m4a", need=(7680, 4320))
    check("a cover with an old copy found sharp on the ask: none on the check, then its old copy at once",
          first == "none" and data.get("status") == "ready" and data.get("url") == sharp_old.get("url")
          and not data.get("soft") and len(calls["upscale"]) == ups and len(measured) == m + 2,
          (first, data, sharp_old.get("url")))

    # Never smaller than max_factor can bring back to the screen, and not at
    # all when that leaves most of the file.
    lu["max_factor"] = 3
    spec, status = measure_plan("music/Other/blurry1080.m4a", UHD)
    check("max_factor 3: the 1080p soft cover is taken as 1280 x 720, which 3x brings to 4K",
          spec and (spec.get("soft") or {}).get("size") == [1280, 720] and spec.get("factor") == 3, (spec, status))
    lu["max_factor"] = 2
    spec, status = measure_plan("music/Other/blurry1080.m4a", UHD)
    check("max_factor 2: taking it smaller could not reach 4K - planned 2x from its own size",
          spec and not spec.get("soft") and spec.get("src") == [1920, 1080] and spec.get("factor") == 2,
          (spec, status))
    lu["max_factor"] = 4
    spec, status = measure_plan("music/Other/blurry1440.m4a", (7680, 4320))
    check("a 1440p soft cover on an 8K screen: taken as 1920 x 1080, as small as 4x allows",
          spec and (spec.get("soft") or {}).get("size") == [1920, 1080] and spec.get("factor") == 4, (spec, status))
    m = len(measured)
    spec, status = measure_plan("music/Other/blurry1080.m4a", (7680, 4320))
    check("a 1080p soft cover on an 8K screen: 4x needs all of it - not taken smaller",
          spec and not spec.get("soft") and spec.get("src") == [1920, 1080] and len(measured) == m, (spec, status))
    lu.pop("soft_cover_height")
    spec, status = measure_plan("music/Other/blurry1080.m4a", UHD)
    check("back at 1440: the 1080p cover, stored as soft, is still not taken smaller",
          spec and not spec.get("soft") and spec.get("src") == [1920, 1080] and len(measured) == m, (spec, status))

    lu["soft_cover_auto"] = False
    spec, status = measure_plan("music/Other/blurry2.m4a", UHD)
    check("Settings off: a soft 4K cover is not needed again, nothing measured",
          spec is None and status.get("status") == "not_needed" and len(measured) == m, (status, measured))
    lu["soft_cover_auto"] = True
    lu["soft_cover_auto_edge"] = 1.01
    spec, status = measure_plan("music/Other/crisp.m4a", UHD)
    check("the limit is the setting: at 1.01 even the crisp cover is soft (its stored value, nothing measured)",
          spec and ((spec.get("soft") or {}).get("auto") or {}).get("limit") == 1.01 and len(measured) == m,
          (spec, status))
    lu.pop("soft_cover_auto_edge")
    lu["soft_cover_folders"] = ["Downloads / music/Other"]
    spec, status = measure_plan("music/Other/blurry2.m4a", UHD)
    check("a cover in a listed folder is not measured: the list says it",
          spec and spec["soft"]["folder"] == "music/Other" and "auto" not in spec["soft"] and len(measured) == m,
          (spec, status))
    spec, status = measure_plan("music/Other/blurry1080.m4a", UHD)
    check("... and one below soft_cover_height (1080p) is soft at its own size, not measured either",
          spec and spec.get("soft") == {"height": 1080, "folder": "music/Other", "file": [1920, 1080],
                                        "size": [1920, 1080]}
          and spec.get("src") == [1920, 1080] and len(measured) == m, (spec, status))
    lu.pop("soft_cover_folders")

    full = os.path.join(lib, "music/Other/crisp.m4a")
    os.utime(full, ns=(time.time_ns(), time.time_ns() + 7_000_000_000))
    data = ask("music/Other/crisp.m4a", check_only=True, need=UHD)
    check("a cover changed on disk: its measurement no longer counts (none on opening)", data == {"status": "none"},
          data)
    ask("music/Other/crisp.m4a", need=UHD)
    check("... and the next ask measures it again", len(measured) == m + 1, measured)

    ns["edge_steepness"] = lambda blob: (_ for _ in ()).throw(ValueError("broken picture"))
    spec, status = measure_plan("music/Other/blurry2.m4a", UHD)
    check("a cover that cannot be measured is not taken as soft, and nothing is stored (asked again next time)",
          spec is None and status.get("status") == "not_needed"
          and (R + "\0music/Other/blurry2.m4a") not in edge_store(), (status, edge_store().get(R + "\0music/Other/blurry2.m4a")))

    gate, started = threading.Event(), threading.Event()

    def slow(blob):
        measured.append(1)
        started.set()
        gate.wait(10)
        return real_steep(blob)

    ns["edge_steepness"] = slow
    m = len(measured)
    results = []
    threads = [threading.Thread(target=lambda: results.append(measure_plan("music/Other/blurry2.m4a", UHD)))
               for _ in range(2)]
    threads[0].start()
    started.wait(10)
    threads[1].start()
    time.sleep(0.2)
    gate.set()
    for t in threads:
        t.join(10)
    check("two asks at once measure the cover once; each plans it soft or waits on the analysis",
          len(measured) == m + 1 and len(results) == 2
          and all((r[0] and r[0]["soft"].get("auto")) or (r[1] or {}).get("status") == "working" for r in results)
          and any(r[0] for r in results), (len(measured) - m, results))
    ns["edge_steepness"] = real_steep

    # ------------------------------------------------------------ Largest song cover (2026-09-28)
    cap_of = lambda value: resolve(lambda: ns["cover_size_cap"]({"cover_max_size": value}))
    check("the cover cap: 8K by default, a box as the setting names it, none for 'none'",
          resolve(lambda: ns["cover_size_cap"](ns["upscale_settings"]())) == (7680, 4320)
          and cap_of("3840x2160") == (3840, 2160) and cap_of("3840 × 2160") == (3840, 2160)
          and cap_of("none") is None and cap_of("") is None and cap_of("big") is None)
    HUGE = (15360, 8640)
    spec, status = measure_plan("music/Geo/sharp.m4a", HUGE)
    check("a 4K cover asked for 16K: planned into the 8K box - 2x, 7680 x 4320, the worker told 2.0 at most",
          spec and spec.get("target") == [7680, 4320] and spec.get("factor") == 2 and spec.get("max_scale") == 2.0
          and spec.get("limited") == {"needed": 4, "by": "cover_size", "size": [7680, 4320]}, (spec, status))
    lu["cover_max_size"] = "none"
    spec, status = measure_plan("music/Geo/sharp.m4a", HUGE)
    check("... 'No limit': the megapixel limit binds again (64 MP), and a pass may overshoot to 4x",
          spec and spec.get("target")[0] == 10666 and (spec.get("limited") or {}).get("by") == "setting"
          and spec.get("max_scale") == 4, (spec, status))
    lu["cover_max_size"] = "3840x2160"
    spec, status = measure_plan("music/Geo/sharp.m4a", (7680, 4320))
    check("... a 4K box: a 4K cover is not upscaled past it - too large, and the answer names the box",
          spec is None and status.get("status") == "too_large" and status.get("limit_by") == "cover_size"
          and status.get("limit_size") == [3840, 2160], status)
    lu.pop("cover_max_size")
    lu["soft_cover_folders"] = ["Downloads / music/MHM"]
    spec, status = measure_plan("music/MHM/soft.m4a", UHD)
    check("a soft 2560 x 1440 cover: 2x as before, and a 4x pass is shrunk to 3x (7680 x 4320), not 10240 x 5760",
          spec and spec.get("target") == [5120, 2880] and spec.get("factor") == 2 and spec.get("max_scale") == 3.0
          and not spec.get("limited"), (spec, status))
    lu.pop("soft_cover_folders")
    spec, status = measure_plan("pic.jpg", HUGE)
    check("a picture has no box: its bounds are unchanged", spec and spec.get("max_scale") == 4
          and (spec.get("limited") or {}).get("by") != "cover_size", (spec, status))

    ups = len(calls["upscale"])
    data = until_ready("music/Geo/sharp.m4a", need=HUGE)
    body = calls["upscale"][-1] if len(calls["upscale"]) > ups else b""
    check("the worker is asked for 7680 x 4320 with max_scale 2.0",
          all(b'name="%s"\r\n\r\n%s\r\n' % (f, v) in body
              for f, v in ((b"width", b"7680"), (b"height", b"4320"), (b"max_scale", b"2.0"), (b"keep_larger", b"1")))
          and data.get("status") == "ready", data)
    entries = ns["_upscale_entries"]()
    good = data.get("key") or next(k for k, e in entries.items() if e.get("path") == "music/Geo/sharp.m4a")
    big = "b" * 40
    entries[big] = dict(entries[good], out=[10240, 5760], file=big + ".webp", last_viewed=time.time() + 60)
    open(os.path.join(cache, big + ".webp"), "wb").write(b"RIFF0000WEBP")
    check("a cover's copy over the box is never handed out, even by its own key",
          resolve(lambda: ns["_touch_upscale"](big)) is None and resolve(lambda: ns["_touch_upscale"](good)))
    hit = resolve(lambda: ns["cached_upscale_of"](R, "music/Geo/sharp.m4a", want=[15360, 8640]))
    check("... nor picked as the best cached copy, though it is the largest", hit and hit[0] == good, hit and hit[0])
    lu["cover_max_size"] = "none"
    check("... with 'No limit' it is a copy like any other",
          resolve(lambda: ns["_touch_upscale"](big)) is not None)
    lu.pop("cover_max_size")
    pic_big = "c" * 40
    entries[pic_big] = {"root": R, "path": "pic.jpg", "file": pic_big + ".webp", "out": [20000, 15000],
                        "last_viewed": time.time(), "bytes": 12}
    open(os.path.join(cache, pic_big + ".webp"), "wb").write(b"RIFF0000WEBP")
    if "upscale_evict_once" not in ns:
        load("upscale_evict_once")
    removed = resolve(lambda: ns["upscale_evict_once"]())
    check("the cache sweep drops the cover copy over the box - and only it",
          big not in entries and good in entries and pic_big in entries and removed >= 1
          and not os.path.exists(os.path.join(cache, big + ".webp")), (removed, big in entries))
finally:
    urllib.request.urlopen = real_urlopen
    shutil.rmtree(tmp, ignore_errors=True)
print("ALL PASS" if ok else "SOME CHECKS FAILED")
