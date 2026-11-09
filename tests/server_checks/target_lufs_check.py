"""Target LUFS: the staged music_loudness / music_library and the settings
route. python lufs_check.py <staged dir>"""
import ast
import math
import os
import random
import sys
import tempfile

D = os.path.abspath(sys.argv[1])
sys.path.insert(0, D)
ok = True


def check(label, cond, detail=""):
    global ok
    ok = ok and bool(cond)
    print(("PASS " if cond else "FAIL ") + label + (f"  ({detail})" if detail and not cond else ""), flush=True)


import music_loudness as ml  # noqa: E402

rnd = random.Random(1)
measured = {("r", f"f{i}"): [rnd.uniform(-20, -4), rnd.uniform(-8, 1)] for i in range(500)}
measured[("r", "silent")] = [None, None]
p = ml.plan(measured)
check("plan has levels", "levels" in p and len(p["levels"]["r"]) == 500)


def js_gain(want, level):  # music.js gainDbOf with a level
    return math.floor((min(want, level[1]) - level[0]) * 10 + 1e-9) / 10


worst_up = -99
same = 0
for (root, path), v in measured.items():
    lv = p["levels"].get(root, {}).get(path)
    if v[0] is None:
        continue
    for want in (-30, -14, -9, -5, p["target"]):
        g = js_gain(want, lv)
        worst_up = max(worst_up, v[0] + g + (0 - v[1]) * -1 - (v[0] + (0 - v[1])))  # peak after gain - 0
        # true peak after the gain must stay at or under 0 dBTP
        check_peak = v[1] + g <= 1e-9
        if not check_peak:
            check(f"no clipping {path} at {want}", False, (v, lv, g))
        # never louder than asked
        if v[0] + g > want + 1e-9:
            check(f"not above the target {path} at {want}", False, (v, lv, g))
    if abs(js_gain(p["target"], lv) - p["gains"][root][path]) <= 0.1 + 1e-9:
        same += 1
check("at the library target the page gain matches the server's within 0.1 dB", same == 500, same)
check("no clipping, never above the target (500 files x 5 targets)", ok)

import music_library as mlib  # noqa: E402

tmp = tempfile.mkdtemp()
L = mlib.Listening(os.path.join(tmp, "l.json"))
check("default: automatic", L.summary("u").get("target_lufs") is None)
L.set_target_lufs("u", -14)
check("set", mlib.Listening(os.path.join(tmp, "l.json")).summary("u")["target_lufs"] == -14.0)
L.set_target_lufs("u", None)
check("back to automatic", mlib.Listening(os.path.join(tmp, "l.json")).summary("u").get("target_lufs") is None)
L.set_autoplay("u", False)
check("autoplay untouched", L.summary("u")["autoplay"] is False)

src = open(os.path.join(D, "server.py"), encoding="utf-8").read()
tree = ast.parse(src)
fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "music_settings")
body = ast.get_source_segment(src, fn)
check("route validates the range", "MUSIC_TARGET_LUFS_MIN <= target <= MUSIC_TARGET_LUFS_MAX" in body)
check("route rejects booleans", "isinstance(target, bool)" in body)
check("autoplay-only request still works", '"autoplay" in data' in body and "set_autoplay" in body)
lib_src = open(os.path.join(D, "music_library.py"), encoding="utf-8").read()
check("row fields", '"gain", "video_gain", "level", "video_level"' in lib_src)
print("ALL PASS" if ok else "SOME CHECKS FAILED")
sys.exit(0 if ok else 1)
