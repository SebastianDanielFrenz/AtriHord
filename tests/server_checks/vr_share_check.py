"""vr_share.py (the VR share for SKYBOX) over a temp library of videos made with
ffmpeg, and its wiring out of a STAGED server.py's AST. Nothing of the real
library is read.

  venv/bin/python vr_share_check.py <staged server.py>
"""
import ast
import atexit
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time

BASE = os.environ.get("ATRIHORD_DIR") or os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SERVER = os.path.abspath(sys.argv[1])
STAGE = os.path.dirname(SERVER)
sys.path.insert(0, BASE)
sys.path.insert(0, STAGE)
tmp = tempfile.mkdtemp(prefix="vr_share_check_")
atexit.register(shutil.rmtree, tmp, True)
import vr_share as vr                    # noqa: E402

assert os.path.dirname(os.path.abspath(vr.__file__)) == STAGE, vr.__file__
ok = True


def check(label, cond, detail=""):
    global ok
    ok = ok and bool(cond)
    print(("PASS " if cond else "FAIL ") + label + (f"  ({detail})" if detail and not cond else ""), flush=True)


# ---- names ------------------------------------------------------------------------
check("a VR word nominates", vr.TOKEN_RE.search("【VR180】ビビデバ [AlVUW5EM3WA].webm")
      and vr.TOKEN_RE.search("Beach walk 360° 8K.mp4") and vr.TOKEN_RE.search("clip_3dh.mp4")
      and vr.TOKEN_RE.search("【VR】【MMD】 dance.webm"))
check("... a resolution or a height does not", not vr.TOKEN_RE.search("Song 1080p [abc].webm")
      and not vr.TOKEN_RE.search("身長180㎝メイド.webm") and not vr.TOKEN_RE.search("give us a 360.webm")
      and not vr.TOKEN_RE.search("Vroom 2160p.mp4"))
check("layout words: SKYBOX's own", [vr.layout_words(v) for v in (
    {"fov": 180, "stereo": "LR", "proj": "equirect"}, {"fov": 180, "stereo": "", "proj": "mesh"},
    {"fov": 360, "stereo": "", "proj": "equirect"}, {"fov": 360, "stereo": "TB", "proj": "equirect"},
    {"fov": 360, "stereo": "", "proj": "eac"})] == ["180_LR", "180", "360", "360_TB", "EAC360"])
check("YouTube's VR180 pictures can be announced as fisheye",
      vr.layout_words({"fov": 180, "stereo": "LR", "proj": "mesh"}, "F180") == "F180_LR"
      and vr.layout_words({"fov": 180, "stereo": "LR", "proj": "equirect"}, "F180") == "180_LR")
name = vr.link_name("Downloads", "youtube/kazuhiro_k Ch./【VR180】ビビデバ 180deg【Hololive MMD 8K】 M [AlVUW5EM3WA].webm",
                    {"fov": 180, "stereo": "LR", "proj": "mesh"}, True)
check("a link name: the channel folder (no trailing dot - SMB clients choke on one), the title "
      "without SKYBOX's words, the layout at the end",
      name == "kazuhiro_k Ch/ビビデバ 【Hololive MMD 8K】 M [AlVUW5EM3WA]_180_LR.webm", name)
name = vr.link_name("Jellyfin", "cosplay/unsorted/Day 3 VR 3D [x].MP4", {"fov": 360, "stereo": "TB"}, False)
check("... another root's name goes in front of the folder, the extension in lower case",
      name == "Jellyfin - unsorted/Day 3 [x]_360_TB.mp4", name)
check("... a title of nothing but layout words still has a name",
      vr.clean("VR180 3D SBS") == "video")

# ---- side data --------------------------------------------------------------------
check("side data: stereo and a tiled equirectangular 180",
      vr.side_data({"side_data_list": [
          {"side_data_type": "Stereo 3D", "type": "side by side"},
          {"side_data_type": "Spherical Mapping", "projection": "tiled equirectangular",
           "bound_left": 1073741824, "bound_right": 1073741824}]}) ==
      {"st": "side by side", "sph": "tiled equirectangular", "fov": 180})
check("... Matroska's StereoMode tag when there is no side data",
      vr.side_data({"tags": {"stereo_mode": "top_bottom"}}) == {"st": "top and bottom"})
check("... a plain video has none", vr.side_data({"side_data_list": [{"side_data_type": "Display Matrix"}]}) == {})
check("nomination: metadata, then the name, then the shape",
      vr.nominated("a.mp4", 1920, 1080, {"st": "2D"}) == "metadata"
      and vr.nominated("a VR180.mp4", 1920, 1080, {}) == "name"
      and vr.nominated("a.mp4", 3840, 1920, {}) == "shape"
      and vr.nominated("a.mp4", 1920, 960, {}) is None
      and vr.nominated("a.mp4", 1920, 1080, {}) is None)
j = vr.judge("metadata", {"sph": "equirectangular", "fov": 360, "st": "top and bottom"}, [], 4096, 4096)
check("a projection is proof, no frames needed", j["vr"] and j["fov"] == 360 and j["stereo"] == "TB", j)
j = vr.judge("metadata", {"sph": "cubemap"}, [], 3840, 2560)
check("... a cubemap is YouTube's EAC", j["vr"] and vr.layout_words(j) == "EAC360", j)

# ---- videos made with ffmpeg --------------------------------------------------------
lib = os.path.join(tmp, "lib")
lib2 = os.path.join(tmp, "lib2")
share = os.path.join(tmp, "share")
for d in (lib, lib2, share):
    os.makedirs(d)


def make(rel, vf, size, seconds=6, base=lib, extra=()):
    path = os.path.join(base, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    cmd = ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"testsrc2=size={size}:rate=10:duration={seconds}"]
    if vf:
        cmd += ["-vf", vf]
    subprocess.run(cmd + ["-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", *extra, path],
                   check=True)
    return path


# one eye: the picture inside a rounded rectangle, black all round (YouTube VR180)
EYE = ("format=gray,geq=lum='if(lte(pow(abs(X-W/2)/(W*0.46),4)+pow(abs(Y-H/2)/(H*0.46),4),1),"
       "40+0.8*lum(X,Y),0)'")
VR180_LR = f"split[a][b];[a]{EYE}[l];[b]{EYE}[r];[l][r]hstack"
# an equirectangular 360: a picture and its mirror image, so the right edge runs
# on into the left one, fading to one grey along the top and bottom rows (the poles)
EQUI = ("format=gray,split[a][b];[b]hflip[c];[a][c]hstack,"
        "geq=lum='128+(lum(X,Y)-128)*sin(PI*Y/H)'")
P = {
    "sbs": make("youtube/Chan/【VR180】Dance [AAAAAAAAAAA].mp4", VR180_LR, "960x1080"),
    "mono": make("youtube/Chan/Close VR180 [BBBBBBBBBBB].mp4", EYE, "1440x1080"),
    "mono_meta": make("youtube/Chan/Just a title [CCCCCCCCCCC].mkv", EYE, "1920x1080"),
    "flat_named": make("youtube/Chan/【8K VR180 3D】C102 Day 3 [DDDDDDDDDDD].mp4", None, "1920x1080"),
    "equi": make("youtube/Walks/Beach 360° walk [EEEEEEEEEEE].mp4", EQUI, "1280x1280"),
    "flat_shape": make("twitter/@someone/123_1.mp4", None, "2560x1280"),
    "plain": make("youtube/Chan/A normal video [FFFFFFFFFFF].mp4", None, "1280x720"),
}
P2 = {"jelly": make("cosplay/unsorted/Hall VR180 [GGGGGGGGGGG].mp4", VR180_LR, "960x1080", base=lib2)}
rel = lambda p: os.path.relpath(p, lib).replace(os.sep, "/")
rel2 = lambda p: os.path.relpath(p, lib2).replace(os.sep, "/")
VIDEOS = [("downloads", rel(p)) for p in P.values()] + [("jelly", rel2(p)) for p in P2.values()]
VIDEOS.append(("downloads", "youtube/Chan/gone [HHHHHHHHHHH].mp4"))     # listed, not on disk
ROOTS = [("downloads", "Downloads", lib, True), ("jelly", "Jellyfin", lib2, False)]

sbs_meta = make("meta/sbs.mkv", VR180_LR, "960x1080", base=tmp, extra=("-metadata:s:v:0", "stereo_mode=left_right"))
pr = vr.probe(sbs_meta, low=False)
check("probe: size, length and Matroska's stereo mode", pr and pr["w"] == 1920 and pr["h"] == 1080
      and 5 <= pr["d"] <= 7 and pr.get("st") == "side by side", pr)
check("probe: a plain video has only its size", set(vr.probe(P["plain"], low=False)) == {"w", "h", "d"})
check("probe: not a video", vr.probe(os.path.join(tmp, "nothing.mp4"), low=False) is None)

base_dir = os.path.join(tmp, "base")
os.makedirs(base_dir)
share_obj = vr.VrShare(base_dir, lambda: ROOTS, lambda: iter(VIDEOS), log=lambda *a: None)
# YouTube's mono VR formats carry a "2D" Stereo 3D box, which ffmpeg 4.4 cannot write
probed, judged = share_obj.scan(probe_fn=lambda p: dict(vr.probe(p), st="2D") if "CCCCCCCCCCC" in p else vr.probe(p))
data = share_obj.state.data
verdict = {}
for key, v in data["verdict"].items():
    verdict[v.get("path") or key] = v
got = {r["path"]: r["layout"] for r in share_obj.listing()}
check("scan: every video on disk probed once", probed == 8, probed)
check("... the VR videos found, with their layout", got == {
    rel(P["sbs"]): "180_LR", rel(P["mono"]): "180", rel(P["mono_meta"]): "180",
    rel(P["equi"]): "360", rel2(P2["jelly"]): "180_LR"}, got)
check("... the flat copy called VR180 and the flat 2:1 picture stay out",
      rel(P["flat_named"]) not in got and rel(P["flat_shape"]) not in got and rel(P["plain"]) not in got)
check("... a plain video is not even looked at", judged == 7, judged)
probed2, judged2 = share_obj.scan()
check("a second scan reads and looks at nothing again", (probed2, judged2) == (0, 0), (probed2, judged2))

# ---- the links ----------------------------------------------------------------------
foreign = os.path.join(share, "mine.txt")
open(foreign, "w").write("mine")
rep = share_obj.sync(share)
names = sorted(share_obj.state.data["links"])
check("sync: one link per VR video", rep["linked"] == 5 and rep["total"] == 5 and not rep["failed"], rep)
check("... named for SKYBOX, in the source folder", names == [
    "Chan/Close [BBBBBBBBBBB]_180.mp4",
    "Chan/Dance [AAAAAAAAAAA]_180_LR.mp4", "Chan/Just a title [CCCCCCCCCCC]_180.mkv",
    "Jellyfin - unsorted/Hall [GGGGGGGGGGG]_180_LR.mp4", "Walks/Beach walk [EEEEEEEEEEE]_360.mp4"], names)
link_sbs = os.path.join(share, "Chan/Dance [AAAAAAAAAAA]_180_LR.mp4")
check("... hard links: the library's own file, nothing copied",
      os.stat(link_sbs).st_ino == os.stat(P["sbs"]).st_ino and os.stat(P["sbs"]).st_nlink == 2)
check("... another file in the folder is left alone", open(foreign).read() == "mine")
rep = share_obj.sync(share)
check("a second sync changes nothing", not any(rep[k] for k in ("linked", "relinked", "removed", "failed")), rep)
rep = share_obj.sync(share, mesh_as="F180")
check("fisheye chosen: YouTube's VR180 names change, the 360 and nothing else",
      rep["linked"] == 4 and rep["removed"] == 4 and os.path.exists(
          os.path.join(share, "Chan/Dance [AAAAAAAAAAA]_F180_LR.mp4"))
      and os.path.exists(os.path.join(share, "Walks/Beach walk [EEEEEEEEEEE]_360.mp4"))
      and os.stat(P["sbs"]).st_nlink == 2, rep)
share_obj.sync(share)

share_obj.set_excluded("downloads", rel(P["equi"]), True)
rep = share_obj.sync(share)
check("left out: its link goes, the video stays", rep["removed"] == 1 and os.path.exists(P["equi"])
      and not os.path.exists(os.path.join(share, "Walks")) and os.stat(P["equi"]).st_nlink == 1, rep)
check("... and is listed as left out",
      [r["excluded"] for r in share_obj.listing() if r["path"] == rel(P["equi"])] == [True])
share_obj.set_excluded("downloads", rel(P["equi"]), False)
check("put back: linked again", share_obj.sync(share)["linked"] == 1)

# trashed: the link lets go (the trash still holds a name, so nothing is lost)
trash_to = os.path.join(lib, ".trash", "20260929-120000-abcdef", rel(P["mono"]))
os.makedirs(os.path.dirname(trash_to))
os.rename(P["mono"], trash_to)
rep = share_obj.sync(share)
check("trashed: the link goes at the next sync, the trash keeps the data",
      rep["removed"] == 1 and os.stat(trash_to).st_nlink == 1, rep)
os.rename(trash_to, P["mono"])
check("restored: linked again", share_obj.sync(share)["linked"] == 1)

# replaced under the same name (a re-encode): linked to the new file after the next scan
old_ino = os.stat(P["sbs"]).st_ino
kept_old = os.path.join(lib, ".trash", "20260929-120000-abcdef", "old.mp4")
os.link(P["sbs"], kept_old)
tmp_new = P["sbs"] + ".new"
shutil.copyfile(P["sbs"], tmp_new)
os.replace(tmp_new, P["sbs"])
rep = share_obj.sync(share)
check("replaced: the old version's link goes until the new one is judged", rep["removed"] == 1, rep)
share_obj.scan()
rep = share_obj.sync(share)
check("... and the new version is linked after the scan",
      rep["linked"] == 1 and os.stat(link_sbs).st_ino == os.stat(P["sbs"]).st_ino != old_ino, rep)
os.unlink(kept_old)

# deleted outside media-dl: the link is the last copy - set aside, never deleted
os.unlink(P2["jelly"])
rep = share_obj.sync(share)
copies = share_obj.last_copies(share)
check("deleted elsewhere: the last copy is set aside, out of the share, not deleted",
      rep["kept_last_copy"] == 1 and copies == [("Jellyfin - unsorted/Hall [GGGGGGGGGGG]_180_LR.mp4", copies[0][1])]
      and not os.path.exists(os.path.join(share, "Jellyfin - unsorted")), (rep, copies))
check("... and deleted only when asked", share_obj.drop_last_copies(share) == 1 and share_obj.last_copies(share) == [])

# a name taken by somebody else's file is not overwritten
squatter = os.path.join(share, "Chan", "Close [BBBBBBBBBBB]_180.mp4")
share_obj.set_excluded("downloads", rel(P["mono"]), True)
share_obj.sync(share)
open(squatter, "w").write("not ours")
share_obj.set_excluded("downloads", rel(P["mono"]), False)
rep = share_obj.sync(share)
check("a name somebody else's file holds is left alone and reported",
      rep["failed"] == 1 and open(squatter).read() == "not ours"
      and "already in the folder" in share_obj.state.data["problems"].get("Chan/Close [BBBBBBBBBBB]_180.mp4", ""), rep)
os.unlink(squatter)
share_obj.sync(share)

# renamed in the library (same file): the scan follows it
moved = os.path.join(lib, "youtube", "Chan2", os.path.basename(P["mono_meta"]))
os.makedirs(os.path.dirname(moved))
os.rename(P["mono_meta"], moved)
VIDEOS[:] = [v for v in VIDEOS if v[1] != rel(P["mono_meta"])] + [("downloads", rel(moved))]
share_obj.scan()
rep = share_obj.sync(share)
check("moved in the library: the scan follows it, the link moves with it",
      rep["linked"] == 1 and rep["removed"] == 1 and os.path.exists(
          os.path.join(share, "Chan2/Just a title [CCCCCCCCCCC]_180.mkv")), rep)
check("... nothing was probed again", share_obj.status["probed"] == 0)

state_file = os.path.join(base_dir, vr.STATE_NAME)
again = vr.VrShare(base_dir, lambda: ROOTS, lambda: iter(VIDEOS))
check("state survives a restart", again.state.data["links"] == share_obj.state.data["links"]
      and json.load(open(state_file, encoding="utf-8"))["folder"] == share)
other = os.path.join(tmp, "share2")
os.makedirs(other)
rep = again.sync(other)
check("another folder: linked afresh there, the old folder's links left where they were",
      rep["linked"] == rep["total"] == 4 and os.path.exists(link_sbs), rep)
check("a missing folder is reported, nothing made",
      again.sync(os.path.join(tmp, "absent"))["error"] == "folder missing"
      and not os.path.exists(os.path.join(tmp, "absent")))

# ---- the server, out of the staged server.py ------------------------------------------
tree = ast.parse(open(SERVER, encoding="utf-8").read())
defs = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}
src = open(SERVER, encoding="utf-8").read()
purge = ast.get_source_segment(src, defs["purge_trash"])
check("purge: the share lets go of the batch before the batch is deleted",
      0 < purge.find('vr_share_sync("purge")') < purge.find("shutil.rmtree(folder"))
check("trashing and restoring wake the share's sync",
      "_vr_sync_now.set()" in ast.get_source_segment(src, defs["trash_resolved"])
      and "_vr_sync_now.set()" in ast.get_source_segment(src, defs["restore_trash"]))
check("the loop is started", 'threading.Thread(target=_vr_share_loop, daemon=True, name="vr-share").start()' in src)
check("config default and merge", '"vr_share": {"enabled": True, "folder": "/mnt/media/vr", "youtube_vr180": "180"}' in src
      and '"twitter_stitch", "song_merge", "vr_share") and isinstance(value, dict)' in src)
routes = {d.name: [ast.get_source_segment(src, x) for x in d.decorator_list] for d in defs.values()}
for fn, route in (("settings_vr", '"/api/settings/vr"'), ("settings_vr_scan", '"/api/settings/vr/scan"'),
                  ("settings_vr_exclude", '"/api/settings/vr/exclude"'),
                  ("settings_vr_last_copies", '"/api/settings/vr/last-copies"')):
    check(f"route {route}: admin only", any(route in r for r in routes.get(fn, []))
          and "require_admin" in routes.get(fn, []))

dims = {f"downloads|{rel(P['sbs'])}": [1, 1, 1], "downloads|a/pic.jpg": [1, 1, 1], f"jelly|x/y.webm": [1, 1, 1]}


class _Recent:
    @staticmethod
    def read_recent_media_paths(base):
        return [rel(P["sbs"]), "new/clip.mkv", "new/pic.png", None]


import media_dimensions                  # noqa: E402
ns = {"os": os, "media_dimensions": media_dimensions, "media_dimensions_map": lambda: dims,
      "recent_media_store": _Recent, "BASE_DIR": base_dir, "DEFAULT_ROOT_ID": "downloads",
      "CONFIG": {"vr_share": {"enabled": True, "folder": share, "youtube_vr180": "F180"}},
      "get_all_roots": lambda: [{"id": "downloads", "name": "Downloads", "path": lib}, {"id": "jelly", "path": lib2}]}
for fn in ("_vr_roots", "_vr_videos", "vr_share_cfg", "vr_share_enabled", "vr_share_folder", "vr_share_mesh_as"):
    exec(compile(ast.Module(body=[defs[fn]], type_ignores=[]), SERVER, "exec"), ns)
check("server: videos are the measured ones plus unmeasured recent downloads, each once",
      sorted(ns["_vr_videos"]()) == sorted([("downloads", rel(P["sbs"])), ("jelly", "x/y.webm"),
                                            ("downloads", "new/clip.mkv")]), sorted(ns["_vr_videos"]()))
check("server: roots with their names, the downloads root marked",
      ns["_vr_roots"]() == [("downloads", "Downloads", lib, True), ("jelly", "jelly", lib2, False)])
check("server: the settings", ns["vr_share_enabled"]() and ns["vr_share_folder"]() == share
      and ns["vr_share_mesh_as"]() == "F180")
ns["CONFIG"] = {}
check("server: defaults when config.json has no vr_share",
      ns["vr_share_enabled"]() and ns["vr_share_folder"]() == "/mnt/media/vr" and ns["vr_share_mesh_as"]() == "180")

print("ALL PASS" if ok else "SOME FAILED")
