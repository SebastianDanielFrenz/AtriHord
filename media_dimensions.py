"""Pixel dimensions for every video and picture in the library, so they can be
filtered without touching the disk.

The library is ~39k videos on a spinning disk. ffprobe per file is fine once
and hopeless per request, so the answers live in one JSON file keyed by
root + relative path. Filtering then costs a dict lookup.

Invalidated on SIZE, not mtime: this filesystem hands out the same
st_mtime_ns for consecutive writes (measured), so an mtime-keyed cache here
would silently serve stale data. Size changing means the file was replaced;
for downloaded media that is the only way it changes at all.

Written by two independent processes - the server and a backfill run - so
every write takes an flock and rewrites through a temp file.
"""
import fcntl
import json
import os
import subprocess
import tempfile

STORE_NAME = "media_dimensions.json"

# What is worth probing. Pictures were left out at first ("the filters this
# feeds are about video"); the user wants them in the resolution filter too
# (2026-09-11). A picture's size is a header read with PIL, ~11 ms each on this
# disk against a spawned ffprobe per video, so ~460k of them is a one-off pass
# of about an hour and a half on one thread.
VIDEO_EXTS = {".mp4", ".webm", ".mkv", ".mov", ".m4v", ".avi", ".flv", ".ts", ".mpg", ".mpeg"}
IMAGE_EXTS = {".jpg", ".jpeg", ".jfif", ".png", ".webp", ".gif", ".bmp", ".avif",
              ".heic", ".tif", ".tiff"}


def store_path(base_dir):
    return os.path.join(base_dir, STORE_NAME)


def key_for(root_id, rel_path):
    return f"{root_id}|{rel_path}"


def is_video_name(name):
    return os.path.splitext(name)[1].lower() in VIDEO_EXTS


def is_image_name(name):
    return os.path.splitext(name)[1].lower() in IMAGE_EXTS


def probe_image(file_path):
    """(width, height) as a picture is SHOWN, from its header, or None.

    EXIF orientations 5-8 mean the stored pixels are turned a quarter, so the
    visible width is the stored height - without this the orientation filter
    would file phone photos under the wrong shape. (Twitter strips EXIF, so it
    is rare here: 0 of 1,805 sampled.) A 0-byte ban tombstone is not a picture
    and comes back None, like any file PIL cannot identify.
    """
    try:
        from PIL import Image
    except ImportError:
        return None
    try:
        with Image.open(file_path) as im:
            width, height = im.size
            try:
                if im.getexif().get(0x0112) in (5, 6, 7, 8):
                    width, height = height, width
            except Exception:
                pass
    except Exception:
        return None
    return (width, height) if width > 0 and height > 0 else None


def load(base_dir):
    """The whole map, or {} if it does not exist yet."""
    try:
        with open(store_path(base_dir), "r", encoding="utf-8") as fh:
            fcntl.flock(fh, fcntl.LOCK_SH)
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def save(base_dir, data):
    path = store_path(base_dir)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path) or ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, separators=(",", ":"))
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def merge(base_dir, updates):
    """Folds new entries into whatever is on disk right now.

    Read-modify-write under an exclusive lock rather than saving a snapshot
    taken earlier: the backfill and the server both write here, and the
    server's newly-downloaded entries must not be lost to a batch the
    backfill started before they existed.
    """
    if not updates:
        return 0
    path = store_path(base_dir)
    lock_path = path + ".lock"
    with open(lock_path, "a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        data = load(base_dir)
        data.update(updates)
        save(base_dir, data)
        return len(data)


def prune(base_dir, keys):
    """Drops entries for files that are no longer media - gone from disk, or
    replaced by a 0-byte ban tombstone. Returns how many were removed.

    Left in, they count as "videos still to measure" in the filter panel for
    ever (1,740 tombstones and 214 deleted files on 2026-09-13), and a
    tombstone's old dimensions still pass the resolution filters. A file that
    comes back - a restore - has a size again and is measured on the next run.
    Same lock as merge(), for the same reason.
    """
    keys = set(keys)
    if not keys:
        return 0
    path = store_path(base_dir)
    with open(path + ".lock", "a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        data = load(base_dir)
        before = len(data)
        for key in keys:
            data.pop(key, None)
        if len(data) != before:
            save(base_dir, data)
        return before - len(data)


def parse_frame_rate(value):
    """ffprobe's rational frame rate ("30000/1001") as fps, or None.

    "0/0" is how ffprobe says it has no figure; that is None, not a crash.
    """
    try:
        num, _, den = (value or "").partition("/")
        fps = float(num) / float(den or 1)
        return round(fps, 2) if fps > 0 else None
    except (ValueError, ZeroDivisionError):
        return None


def stream_frame_rate(stream):
    """The rate a video stream is SEEN at: its average, and the nominal rate
    only when there is no average.

    The two disagree on variable-frame-rate files (12 of 400 sampled): a phone
    clip averaging 29.98 can claim a nominal 120, one averaging 20.5 a nominal
    29.97. The average is what a viewer gets. The details box and the frame
    rate filter both read this, so they cannot disagree about a file.
    """
    return (parse_frame_rate(stream.get("avg_frame_rate"))
            or parse_frame_rate(stream.get("r_frame_rate")))


def parse_duration(value):
    """ffprobe's format duration ("12.345000", or "N/A") in seconds, or None."""
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return None
    return round(seconds, 2) if seconds > 0 else None


def probe(file_path):
    """(width, height, fps, duration) of a video, or None. fps and duration may
    be None.

    Reads the FIRST video stream only. A file whose video stream is cover art
    (an audio file with an embedded picture) would otherwise report the
    artwork's size as the video's - which is exactly the mistake
    probe_media_details already guards against elsewhere.
    """
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries",
             "stream=width,height,avg_frame_rate,r_frame_rate:stream_disposition=attached_pic"
             ":format=duration",
             "-of", "json", file_path],
            capture_output=True, text=True, timeout=60)
        data = json.loads(out.stdout or "{}")
        streams = data.get("streams") or []
        if not streams:
            return None
        stream = streams[0]
        if (stream.get("disposition") or {}).get("attached_pic"):
            return None
        width = int(stream.get("width") or 0)
        height = int(stream.get("height") or 0)
        if width > 0 and height > 0:
            return (width, height, stream_frame_rate(stream),
                    parse_duration((data.get("format") or {}).get("duration")))
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    return None


def entry_for(file_path):
    """The stored shape: [width, height, size] for a picture and
    [width, height, size, fps, duration] for a video.

    Size stays third because it is the invalidator and every reader looks for
    it there. A video's fps or duration is null when ffprobe has no figure for
    it - still a five-element entry, so the backfill does not ask again.
    """
    if is_image_name(file_path):
        dims = probe_image(file_path)
    else:
        dims = probe(file_path)
    if not dims:
        return None
    try:
        size = os.path.getsize(file_path)
    except OSError:
        return None
    return [dims[0], dims[1], size, *dims[2:]]


def needs_video_fields(name, entry):
    """A video measured before its frame rate and then its duration were
    recorded (both 2026-09-13): [w, h, size] or [w, h, size, fps]."""
    return is_video_name(name) and len(entry) < 5


# ---------------------------------------------------------------- filters --

# The 16:9 standards, by their long edge. One set of numbers serves both
# filters: the general one applies it to the longest side, the vertical one
# applies it to the height (what a portrait screen actually fills).
STANDARDS = {
    "720p": 1280,
    "fhd": 1920,
    "wqhd": 2560,
    "4k": 3840,
    "8k": 7680,
}

STANDARD_LABELS = {
    "720p": "720p", "fhd": "FHD", "wqhd": "WQHD", "4k": "4K", "8k": "8K",
}


# Frame rates, picked from 400 library videos sampled 2026-09-13: 30 and 60 fps
# are 70% of them, then 24/25 (film), 10-15 (GIFs Twitter turned into MP4) and
# a few 50 and 120. Two families on the same principle as resolution - a floor
# and a ceiling - so they combine into a band: at least 30 and at most 30 is
# "30 fps". Half a frame of slack lets the NTSC rates count as what they are
# called: 23.976 is 24, 29.97 is 30, 59.94 is 60.
FPS_SLACK = 0.5
MIN_FRAME_RATES = {"24": 24, "30": 30, "50": 50, "60": 60, "120": 120, "240": 240}
MAX_FRAME_RATES = {"15": 15, "25": 25, "30": 30, "60": 60}


def duration_of(dims):
    """A video's stored duration in seconds, or None."""
    return dims[4] if dims and len(dims) > 4 else None


def frame_rate_of(dims):
    """The stored frame rate, or None. Pictures have none; a video measured
    before rates were recorded has none yet."""
    return dims[3] if dims and len(dims) > 3 else None


def orientation_of(width, height):
    if width > height:
        return "horizontal"
    if height > width:
        return "vertical"
    return "square"


def matches(dims, orientation=None, min_width=None, min_height=None, min_any=None,
            min_fps=None, max_fps=None):
    """Does one [w, h, size(, fps)] entry pass the active filters?

    Three resolution families, all using the SAME numbers from STANDARDS and
    differing only in which edge they measure:

      · horizontal - width  >= N. Fills a landscape screen edge to edge, so
        the bars (if any) are top and bottom.
      · vertical   - height >= N. Fills a portrait screen top to bottom, so
        the bars are at the sides. This is the one that makes a phone show
        every pixel at native resolution or better.
      · combined   - max(width, height) >= N, which is precisely the union of
        the two above: it passes if EITHER edge meets the standard.

    A file with no known dimensions never matches - it is not "probably
    fine", it is unknown, and quietly including it would make the filter
    untrustworthy in the one direction that matters. The same goes for a
    frame rate filter and a file with no known rate, which is every picture.
    """
    if not dims:
        return False
    width, height = dims[0], dims[1]
    if orientation and orientation_of(width, height) != orientation:
        return False
    if min_width and width < min_width:
        return False
    if min_height and height < min_height:
        return False
    if min_any and max(width, height) < min_any:
        return False
    if min_fps or max_fps:
        fps = frame_rate_of(dims)
        if not fps:
            return False
        if min_fps and fps < min_fps - FPS_SLACK:
            return False
        if max_fps and fps > max_fps + FPS_SLACK:
            return False
    return True
