"""The VR share: a folder of hard links to the library's 180 and 360 degree
videos, named so SKYBOX picks the right layout by itself, published read-only
over Samba for the Steam Frame.

User 2026-09-29: "For my Steam Frame, I want an exposed share of some kind that
I can play using Skybox. It should only contain 180/360° videos".

Which videos are VR is decided in three steps, each cached per file version
(inode, size, mtime) in BASE_DIR/vr_share.json:

1. Probe - every library video's first video stream is read once with ffprobe
   for its "Spherical Mapping" and "Stereo 3D" side data. A projection is
   proof. YouTube's VR formats carry a Stereo 3D box even when mono ("2D"),
   plain videos carry none - the flat copy YouTube hands out for a VR180
   upload has none either, although its title still says "VR180".
2. Candidates - a projection or a Stereo 3D box, a VR word in the name
   (VR180, 360°, SBS, fisheye...) or a 2:1 / 1:1 picture of 2560 pixels and up.
3. Frames - three frames of each candidate: left half against right half
   (side by side), top against bottom (over-under), and the picture of one
   eye. YouTube's VR180 frames are a bright rounded rectangle on black - dark
   corners and dark edges all round; an equirectangular 360 picture continues
   from its right edge into its left and has a single colour along its top and
   bottom rows (the poles). Names and shapes only nominate: a candidate without
   a projection is linked only when its frames say so.

The folder holds one hard link per VR video, under <source folder>/<title>_<layout>.<ext>,
the layout words being SKYBOX's own (180_LR, 360_TB, ...). Hard links, not
symlinks: Samba follows a symlink out of a share only with "wide links" plus
the global "allow insecure wide links", and a link name can carry the layout
words without renaming anything in the library. The price, handled by sync():
a link keeps a trashed file's data alive, so links whose file left its place
are removed on every sync (and purge_trash() syncs first), and a file replaced
under the same name (a re-encode) is linked again. A link that has become the
last name of its data is never deleted - that is reported instead.
"""
import json
import os
import re
import subprocess
import tempfile
import threading
import time

STATE_NAME = "vr_share.json"
VIDEO_EXTS = {".mp4", ".webm", ".mkv", ".mov", ".m4v", ".avi", ".flv", ".ts", ".mpg", ".mpeg"}
TRASH_DIR_NAME = ".trash"
LAST_COPIES = ".last-copies"   # inside the share folder; Samba vetoes dot names
LOW = ["ionice", "-c3", "nice", "-n", "19"]

# A word in a name that makes a video worth a look (not proof: "give us a
# 360" is a dance move, "【8K VR180 3D】" was a flat copy).
TOKEN_RE = re.compile(
    r"(?i)(?:vr\s*180|vr\s*360|(?<![a-z0-9])vr(?![a-z0-9])|(?<![0-9])(?:180|360)\s*(?:°|º|deg\w*|度)"
    r"|(?<![a-z0-9])(?:sbs|3dh|3dv|fisheye\d*|equirect\w*|eac360|180x180|360x180)(?![a-z0-9])|全景|パノラマ)")
# Words SKYBOX reads as a layout, dropped from a link's title so only the
# layout written at its end counts ("VR180" alone means fisheye to SKYBOX).
SKYBOX_WORD_RE = re.compile(
    r"(?i)(?<![a-z0-9])(?:vr\s*180|vr\s*360|f180|eac360|180x180|360x180|fisheye\d*|full[_ ]?sbs|h?sbs|3dh|3dv|"
    r"h?ou|lr|tb|3d|vr|(?:180|360)\s*(?:°|º|deg\w*|度)?)(?![a-z0-9])")
_BAD_NAME_RE = re.compile(r'[\\/:*?"<>|\x00-\x1f]')

FRAME_W = 384
SAME_EYES = 0.85           # correlation of the two halves of a stereo frame
DARK_CORNER = 15.0         # 0-255: brightest corner patch of a VR180 eye
DARK_EDGE = 8.0            # spread along the side edges of a VR180 eye
DARK_POLE = 6.0            # spread along its top and bottom rows
LIT_CENTER = 30.0
WRAP = 1.5                 # right edge -> left edge, against neighbouring columns
POLE_360 = 8.0
EDGE_360 = 12.0            # a 360 picture's edges carry picture, not a border


def stat_key(st):
    return f"{st.st_ino}:{st.st_size}:{st.st_mtime_ns}"


def _free(path):
    """path, or path with " (n)" before the extension when taken."""
    if not os.path.lexists(path):
        return path
    stem, ext = os.path.splitext(path)
    n = 2
    while os.path.lexists(f"{stem} ({n}){ext}"):
        n += 1
    return f"{stem} ({n}){ext}"


def is_video(path):
    return os.path.splitext(path)[1].lower() in VIDEO_EXTS


def in_trash(rel):
    return rel == TRASH_DIR_NAME or rel.startswith(TRASH_DIR_NAME + "/") or ("/" + TRASH_DIR_NAME + "/") in rel


# ---- 1. probe ------------------------------------------------------------------

def probe(path, low=True):
    """The first video stream: {"w", "h", "d" (seconds)} plus its side data
    ("st": stereo type, "sph": projection, "fov": degrees across) when there
    is any; None when ffprobe cannot read the file."""
    cmd = (LOW if low else []) + ["ffprobe", "-v", "error", "-select_streams", "v:0",
                                  "-show_streams", "-show_entries", "format=duration", "-of", "json", path]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        info = json.loads(r.stdout or "{}")
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    streams = info.get("streams") or []
    if not streams:
        return None
    out = side_data(streams[0])
    out["w"], out["h"] = int(streams[0].get("width") or 0), int(streams[0].get("height") or 0)
    try:
        out["d"] = round(float((info.get("format") or {}).get("duration") or 0), 2)
    except ValueError:
        out["d"] = 0
    return out


def side_data(stream):
    out = {}
    for sd in stream.get("side_data_list") or []:
        kind = sd.get("side_data_type")
        if kind == "Stereo 3D" and sd.get("type"):
            out["st"] = str(sd["type"])
        elif kind == "Spherical Mapping" and sd.get("projection"):
            out["sph"] = str(sd["projection"])
            try:
                cut = (int(sd.get("bound_left") or 0) + int(sd.get("bound_right") or 0)) / 2 ** 32
                out["fov"] = int(round(360 * (1 - cut)))
            except (TypeError, ValueError):
                out["fov"] = 360
    tags = {k.lower(): str(v).lower() for k, v in (stream.get("tags") or {}).items()}
    if "st" not in out and tags.get("stereo_mode") in ("left_right", "right_left"):
        out["st"] = "side by side"
    elif "st" not in out and tags.get("stereo_mode") in ("top_bottom", "bottom_top"):
        out["st"] = "top and bottom"
    return out


# ---- 2. candidates ---------------------------------------------------------------

def shape_of(w, h):
    if not w or not h:
        return None
    ar = w / h
    for name, want in (("2:1", 2.0), ("1:1", 1.0)):
        if abs(ar - want) < 0.02 and max(w, h) >= 2560:
            return name
    return None


def nominated(rel, w, h, sd):
    """Why this video is worth a frame test, or None."""
    if sd and (sd.get("sph") or sd.get("st")):
        return "metadata"
    if TOKEN_RE.search(os.path.basename(rel)):
        return "name"
    if shape_of(w, h):
        return "shape"
    return None


# ---- 3. frames -------------------------------------------------------------------

def _frame(path, t, w, h):
    import numpy as np
    oh = max(2, int(round(FRAME_W * h / w / 2)) * 2)
    try:
        r = subprocess.run(LOW + ["ffmpeg", "-v", "error", "-ss", f"{t:.2f}", "-i", path, "-frames:v", "1",
                                  "-vf", f"scale={FRAME_W}:{oh}", "-f", "rawvideo", "-pix_fmt", "gray", "-"],
                           capture_output=True, timeout=180)
    except (OSError, subprocess.SubprocessError):
        return None
    if len(r.stdout) != FRAME_W * oh:
        return None
    return np.frombuffer(r.stdout, np.uint8).reshape(oh, FRAME_W).astype(np.float32)


def _corr(a, b):
    a = a - a.mean()
    b = b - b.mean()
    d = float(((a * a).sum() * (b * b).sum()) ** 0.5)
    return float((a * b).sum() / d) if d > 1e-6 else 0.0


def eye_features(e):
    h, w = e.shape
    ph, pw = max(2, int(h * 0.06)), max(2, int(w * 0.06))
    corners = (e[:ph, :pw], e[:ph, -pw:], e[-ph:, :pw], e[-ph:, -pw:])
    inner = float(abs(e[:, 1:] - e[:, :-1]).mean()) + 0.5      # the typical step between columns
    return {
        "corner": float(max(c.mean() for c in corners)),
        "center": float(e[h // 4:3 * h // 4, w // 4:3 * w // 4].mean()),
        "edge": float(e[:, [0, -1]].std()),
        "pole": float((e[0].std() + e[-1].std()) / 2),
        "wrap": float(abs(e[:, 0] - e[:, -1]).mean()) / inner,
    }


def frame_features(a):
    H, W = a.shape
    return {"sbs": _corr(a[:, :W // 2], a[:, W // 2:]), "tb": _corr(a[:H // 2], a[H // 2:]),
            "mono": eye_features(a), "left": eye_features(a[:, :W // 2]), "top": eye_features(a[:H // 2])}


def sample_frames(path, w, h, duration):
    times = [duration * f for f in (0.3, 0.5, 0.7)] if duration and duration > 3 else [0.5]
    out = []
    for t in times:
        a = _frame(path, t, w, h)
        if a is not None and a.std() >= 4:
            out.append(frame_features(a))
    return out


def _vignetted(e):
    return (e["corner"] <= DARK_CORNER and e["edge"] <= DARK_EDGE and e["pole"] <= DARK_POLE
            and e["center"] >= LIT_CENTER)


def _equirect(e):
    return e["wrap"] <= WRAP and e["edge"] >= EDGE_360 and e["pole"] <= POLE_360


def _most(frames, test):
    hits = sum(1 for f in frames if test(f))
    return hits * 2 > len(frames)


def _all(frames, test):
    return bool(frames) and all(test(f) for f in frames)


def judge(nomination, sd, frames, w, h):
    """{"vr": bool, "fov", "stereo" ("LR"/"TB"/""), "proj", "why"}."""
    sd = sd or {}
    st = sd.get("st") or ""
    stereo = "LR" if st.startswith("side by side") else "TB" if st.startswith("top and bottom") else ""
    if sd.get("sph"):
        proj = sd["sph"]
        if proj == "cubemap":
            return {"vr": True, "fov": 360, "stereo": stereo, "proj": "eac", "why": "cubemap projection"}
        fov = 180 if sd.get("fov", 360) <= 200 else 360
        return {"vr": True, "fov": fov, "stereo": stereo, "proj": "equirect", "why": f"{proj} projection"}
    if not frames:
        return {"vr": False, "why": "no frame could be read"}
    if not stereo:
        if _most(frames, lambda f: f["sbs"] >= SAME_EYES):
            stereo = "LR"
        elif _most(frames, lambda f: f["tb"] >= SAME_EYES):
            stereo = "TB"
    eye = {"LR": "left", "TB": "top", "": "mono"}[stereo]
    need = _most if nomination in ("metadata", "name") else _all
    ew, eh = (w / 2, h) if stereo == "LR" else (w, h / 2) if stereo == "TB" else (w, h)
    if need(frames, lambda f: _vignetted(f[eye])):
        return {"vr": True, "fov": 180, "stereo": stereo, "proj": "mesh",
                "why": "a bright picture on black all round (YouTube VR180)"}
    if abs(ew / eh - 2.0) < 0.05 and need(frames, lambda f: _equirect(f[eye])):
        return {"vr": True, "fov": 360, "stereo": stereo, "proj": "equirect",
                "why": "the picture wraps around and has poles (360)"}
    if stereo and nomination in ("metadata", "name") and abs(ew / eh - 1.0) < 0.05:
        return {"vr": True, "fov": 180, "stereo": stereo, "proj": "equirect",
                "why": "two matching square eyes (180 3D)"}
    return {"vr": False, "why": "the frames look flat"}


# ---- names -----------------------------------------------------------------------

def layout_words(v, mesh_as="180"):
    """SKYBOX's words for a verdict. mesh_as: how YouTube's VR180 pictures are
    announced ("180" equirectangular or "F180" fisheye) - SKYBOX reads VR180 as
    fisheye, and which one looks right is for the eye to tell."""
    if v.get("proj") == "eac":
        return "EAC360" + ("_TB" if v.get("stereo") == "TB" else "_LR" if v.get("stereo") == "LR" else "")
    fov = "360" if v.get("fov") == 360 else (mesh_as if v.get("proj") == "mesh" else "180")
    return fov + ("_" + v["stereo"] if v.get("stereo") else "")


def clean(text, limit=150):
    text = SKYBOX_WORD_RE.sub(" ", text)
    text = _BAD_NAME_RE.sub(" ", text)
    text = re.sub(r"[【】\[\]()]\s*[【】\[\]()]", " ", text)
    text = re.sub(r"\s+", " ", text).strip(" .-_")
    return text[:limit].rstrip(" .-_") or "video"


def link_name(root_name, rel, verdict, default_root, mesh_as="180"):
    """<source folder>/<clean title>_<layout><ext>, relative to the share."""
    parent = os.path.basename(os.path.dirname(rel)) or root_name
    folder = clean(parent, 80) if default_root else clean(f"{root_name} - {parent}", 80)
    stem, ext = os.path.splitext(os.path.basename(rel))
    return f"{folder}/{clean(stem)}_{layout_words(verdict, mesh_as)}{ext.lower()}"


# ---- state -----------------------------------------------------------------------

class State:
    """vr_share.json: probes and verdicts per file version, the links we made,
    and the files the user took out."""

    def __init__(self, path):
        self.path = path
        self.lock = threading.RLock()
        self.data = {"probe": {}, "verdict": {}, "links": {}, "exclude": [], "problems": {}}
        try:
            with open(path, encoding="utf-8") as fh:
                loaded = json.load(fh)
            for k in self.data:
                if isinstance(loaded.get(k), type(self.data[k])):
                    self.data[k] = loaded[k]
            for k, v in loaded.items():
                if k not in self.data:
                    self.data[k] = v
        except (OSError, ValueError):
            pass

    def save(self):
        with self.lock:
            fd, tmp = tempfile.mkstemp(prefix=".vr_share.", dir=os.path.dirname(self.path))
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(self.data, fh, ensure_ascii=False, separators=(",", ":"))
            os.replace(tmp, self.path)


# ---- the share ---------------------------------------------------------------------

class VrShare:
    """One per server. roots(): [(root_id, name, path, is_default)];
    videos(): iterable of (root_id, rel) for every video the library knows
    (the dimensions store, plus the recent downloads it has not measured)."""

    def __init__(self, base_dir, roots, videos, log=print):
        self.state = State(os.path.join(base_dir, STATE_NAME))
        self.roots = roots
        self.videos = videos
        self.log = log
        self.sync_lock = threading.Lock()
        self.scan_lock = threading.Lock()
        self.status = {"scanning": False, "scanned_at": None, "synced_at": None,
                       "probed": 0, "to_probe": 0, "judged": 0, "to_judge": 0, "current": None}

    # -- scanning (slow: ffprobe and frames) --

    def scan(self, stop=lambda: False, max_probe=None, probe_fn=probe, frames_fn=sample_frames):
        """Probes new videos, tests new candidates. Returns (probed, judged)."""
        if not self.scan_lock.acquire(blocking=False):
            return (0, 0)
        try:
            self.status.update(scanning=True, probed=0, judged=0, to_probe=0, to_judge=0)
            data = self.state.data
            roots = {r[0]: r for r in self.roots()}
            todo, judge_todo, live = [], [], set()
            for rid, rel in self.videos():
                if rid not in roots or in_trash(rel) or not is_video(rel):
                    continue
                full = os.path.join(roots[rid][2], rel)
                try:
                    st = os.stat(full)
                except OSError:
                    continue
                key = stat_key(st)
                if key in live:
                    continue           # one file under two names: judged once
                live.add(key)
                if key not in data["probe"]:
                    todo.append((key, full, rid, rel))
                elif key not in data["verdict"]:
                    judge_todo.append((key, full, rid, rel))
                else:
                    v = data["verdict"][key]
                    if v.get("vr") and (v.get("root"), v.get("path")) != (rid, rel) and not (
                            v.get("root") in roots and os.path.exists(os.path.join(roots[v["root"]][2], v["path"]))):
                        with self.state.lock:
                            v["root"], v["path"] = rid, rel      # renamed or moved

            self.status["to_probe"] = len(todo)
            probed = judged = 0
            for key, full, rid, rel in todo[:max_probe] if max_probe else todo:
                if stop():
                    break
                sd = probe_fn(full)
                with self.state.lock:
                    data["probe"][key] = sd if sd is not None else {"err": 1}
                probed += 1
                self.status["probed"] = probed
                if sd is not None and key not in data["verdict"]:
                    judge_todo.append((key, full, rid, rel))
                if probed % 500 == 0:
                    self.state.save()
            self.status["to_judge"] = len(judge_todo)
            for key, full, rid, rel in judge_todo:
                if stop():
                    break
                sd = data["probe"].get(key) or {}
                w, h = int(sd.get("w") or 0), int(sd.get("h") or 0)
                why = nominated(rel, w, h, sd) if w and h else None
                if why is None:
                    continue
                self.status["current"] = rel
                frames = frames_fn(full, w, h, float(sd.get("d") or 0))
                v = judge(why, sd, frames, w, h)
                v["nominated"] = why
                v["at"] = int(time.time())
                if v.get("vr"):
                    v["root"], v["path"] = rid, rel
                with self.state.lock:
                    data["verdict"][key] = v
                judged += 1
                self.status["judged"] = judged
            self.status["current"] = None
            if not stop():
                with self.state.lock:
                    # file versions that are gone (replaced, deleted) are forgotten
                    for store in ("probe", "verdict"):
                        for key in [k for k in data[store] if k not in live]:
                            del data[store][key]
            self.state.save()
            self.status["scanned_at"] = int(time.time())
            return (probed, judged)
        finally:
            self.status["scanning"] = False
            self.scan_lock.release()

    # -- the links (fast: stats and links only) --

    def _vr_files(self):
        """(root_id, rel, full, stat, verdict) of every VR verdict whose file is
        still that version, in its place. Stats only these files."""
        data = self.state.data
        roots = {r[0]: r for r in self.roots()}
        for key, v in list(data["verdict"].items()):
            if not v.get("vr") or v.get("root") not in roots or not v.get("path") or in_trash(v["path"]):
                continue
            full = os.path.join(roots[v["root"]][2], v["path"])
            try:
                st = os.stat(full)
            except OSError:
                continue
            if stat_key(st) == key:
                yield v["root"], v["path"], full, st, v

    def wanted(self, mesh_as="180"):
        """{link rel: (root_id, rel, full, stat)} for every VR video in place."""
        exclude = set(self.state.data.get("exclude") or [])
        roots = {r[0]: r for r in self.roots()}
        out = {}
        for rid, rel, full, st, v in sorted(self._vr_files(), key=lambda x: (x[0], x[1])):
            if f"{rid}|{rel}" in exclude:
                continue
            name = link_name(roots[rid][1], rel, v, roots[rid][3], mesh_as)
            if name in out:
                stem, ext = os.path.splitext(name)
                name = f"{stem} ({st.st_ino}){ext}"
            out[name] = (rid, rel, full, st)
        return out

    def sync(self, folder, mesh_as="180"):
        """Makes the folder hold exactly the wanted links (of the ones we
        made - nothing else in it is touched). Returns a report."""
        report = {"linked": 0, "relinked": 0, "removed": 0, "kept_last_copy": 0, "failed": 0, "total": 0}
        if not folder or not os.path.isdir(folder):
            report["error"] = "folder missing"
            return report
        with self.sync_lock, self.state.lock:
            data = self.state.data
            if data.get("folder") != folder:
                # another folder: the links in the old one are left where they are
                data["links"] = {}
                data["folder"] = folder
            links = data["links"]
            want = self.wanted(mesh_as)
            problems = {}
            # links that are no longer wanted
            for name in list(links):
                if name in want:
                    continue
                path = os.path.join(folder, name)
                try:
                    st = os.lstat(path)
                except FileNotFoundError:
                    del links[name]
                    continue
                if st.st_nlink <= 1:
                    # Its file was deleted meanwhile (outside media-dl - a purge
                    # syncs first): this link is the last name of the data. Not
                    # ours to delete; set aside where the share does not show it.
                    aside = _free(os.path.join(folder, LAST_COPIES, name))
                    os.makedirs(os.path.dirname(aside), exist_ok=True)
                    os.replace(path, aside)
                    del links[name]
                    report["kept_last_copy"] += 1
                    continue
                os.unlink(path)
                del links[name]
                report["removed"] += 1
            # wanted links: new, or pointing at an older version of the file
            for name, (rid, rel, full, st) in want.items():
                path = os.path.join(folder, name)
                try:
                    have = os.lstat(path)
                except FileNotFoundError:
                    have = None
                if have is not None and (have.st_ino, have.st_dev) == (st.st_ino, st.st_dev):
                    links[name] = {"root": rid, "path": rel}
                    continue
                if have is not None and name not in links:
                    problems[name] = "a file of that name is already in the folder"
                    report["failed"] += 1
                    continue
                if have is not None and have.st_nlink <= 1:
                    aside = _free(os.path.join(folder, LAST_COPIES, name))
                    os.makedirs(os.path.dirname(aside), exist_ok=True)
                    os.replace(path, aside)
                    report["kept_last_copy"] += 1
                try:
                    os.makedirs(os.path.dirname(path), exist_ok=True)
                    tmp = os.path.join(os.path.dirname(path), f".vr-link-{os.getpid()}-{st.st_ino}")
                    if os.path.lexists(tmp):
                        os.unlink(tmp)
                    os.link(full, tmp)
                    os.replace(tmp, path)
                except OSError as exc:
                    problems[name] = f"cannot link: {exc.strerror or exc}"
                    report["failed"] += 1
                    continue
                links[name] = {"root": rid, "path": rel}
                report["relinked" if have is not None else "linked"] += 1
            self._tidy_dirs(folder, keep=(os.path.join(folder, LAST_COPIES),))
            data["problems"] = problems
            report["total"] = sum(1 for n in want if n in links)
            self.state.save()
            self.status["synced_at"] = int(time.time())
        return report

    @staticmethod
    def _tidy_dirs(folder, keep=()):
        for dirpath, dirnames, filenames in os.walk(folder, topdown=False):
            if dirpath != folder and dirpath not in keep and not dirnames and not filenames:
                try:
                    os.rmdir(dirpath)
                except OSError:
                    pass

    def set_excluded(self, root_id, rel, excluded):
        key = f"{root_id}|{rel}"
        with self.state.lock:
            ex = [k for k in self.state.data.get("exclude") or [] if k != key]
            if excluded:
                ex.append(key)
            self.state.data["exclude"] = ex
            self.state.save()

    def listing(self, mesh_as="180"):
        """Every VR video in the library, linked or excluded, for the settings page."""
        exclude = set(self.state.data.get("exclude") or [])
        linked = {(v["root"], v["path"]): n for n, v in self.state.data["links"].items()}
        rows = []
        for rid, rel, full, st, v in self._vr_files():
            sd = self.state.data["probe"].get(stat_key(st)) or {}
            rows.append({"root": rid, "path": rel, "layout": layout_words(v, mesh_as), "why": v.get("why"),
                         "excluded": f"{rid}|{rel}" in exclude, "link": linked.get((rid, rel)),
                         "size": st.st_size, "width": sd.get("w"), "height": sd.get("h"),
                         "duration": sd.get("d")})
        rows.sort(key=lambda r: (r["excluded"], r["path"].lower()))
        return rows

    def last_copies(self, folder):
        """Links set aside because the video's own file was deleted meanwhile:
        [(name, bytes)]."""
        out = []
        base = os.path.join(folder or "", LAST_COPIES)
        for dirpath, _dirs, files in os.walk(base):
            for fn in files:
                p = os.path.join(dirpath, fn)
                try:
                    out.append((os.path.relpath(p, base).replace(os.sep, "/"), os.lstat(p).st_size))
                except OSError:
                    pass
        return sorted(out)

    def drop_last_copies(self, folder):
        """Deletes the set-aside links - only on the user's word (settings page)."""
        gone = 0
        for name, _size in self.last_copies(folder):
            try:
                os.unlink(os.path.join(folder, LAST_COPIES, name))
                gone += 1
            except OSError:
                pass
        self._tidy_dirs(os.path.join(folder, LAST_COPIES))
        return gone
