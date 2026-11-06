import copy
import discovery_catalogue
import os
import re
import sys
import io
import json
import time
import queue
import secrets
import hashlib
import math
import logging
import threading
import subprocess
import urllib.request
import shlex
import signal
import traceback
import heapq
import shutil
import sqlite3
import zipfile
import tempfile
import mimetypes
import unicodedata
from html import escape as html_escape
import psutil
import genre_map
import media_ai
import media_dates
import media_places
import youtube_music_extractor as music_store
import media_dimensions
import media_faces
import image_search
import media_review
import post_stitch
import image_dedup
import recent_media_store
import song_covers
import stereo3d
import video_trim
import interp_redo
from functools import wraps
from urllib.parse import quote
from datetime import date, timedelta
from flask import (
    Flask, request, send_from_directory, send_file,
    redirect, jsonify, Response
)
from collections import deque

app = Flask(__name__)
BASE_DIR = os.path.abspath(os.path.dirname(__file__))
WEB_FILES_DIR = os.path.join(BASE_DIR, "web_files")


def _load_env_file():
    """Settings can live in atrihord.env next to server.py (KEY=value lines, # comments) instead of the service
    unit. A variable that is already set in the real environment wins."""
    path = os.path.join(BASE_DIR, "atrihord.env")
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, _, value = line.partition("=")
                    os.environ.setdefault(key.strip(), value.strip().strip("'\""))
    except OSError:
        pass


_load_env_file()


# Where things live on disk, each optional (unset = next to the app, as before):
#   MEDIA_LIBRARY_DIR  the built-in "downloads" library (the stored media; more libraries are added in Settings)
#   MEDIA_CACHE_DIR    derived media that is big and can be rebuilt: 2D->3D, upscaled copies, ugoira videos,
#                      audio remuxes, video-trim analysis (a sub-folder each). Put it on the big disk.
#   MEDIA_PREVIEW_DIR  thumbnails and face crops: many small files, wants a fast disk
# The 3D and upscale folders can still be set singly (MEDIA_3D_CACHE, Settings > cache_dir); a single setting wins.
def _env_dir(name):
    value = os.environ.get(name, "").strip()
    return os.path.abspath(os.path.expanduser(value)) if value else ""


MEDIA_LIBRARY_DIR = _env_dir("MEDIA_LIBRARY_DIR")
MEDIA_CACHE_DIR = _env_dir("MEDIA_CACHE_DIR")
MEDIA_PREVIEW_DIR = _env_dir("MEDIA_PREVIEW_DIR")


def cache_location(kind, legacy):
    return os.path.join(MEDIA_CACHE_DIR, kind) if MEDIA_CACHE_DIR else legacy


def preview_location(kind, legacy):
    return os.path.join(MEDIA_PREVIEW_DIR, kind) if MEDIA_PREVIEW_DIR else legacy


USERS_FILE = os.path.join(BASE_DIR, "users.json")
CONFIG_FILE = os.path.join(BASE_DIR, "config.json")
USAGE_FILE = os.path.join(BASE_DIR, "pipeline_usage.json")
DOWNLOADS_DIR = MEDIA_LIBRARY_DIR or os.path.join(WEB_FILES_DIR, "downloads")
TASKS_STATE_FILE = os.path.join(BASE_DIR, "tasks.json")
SHARES_FILE = os.path.join(BASE_DIR, "shares.json")

# Deployment settings come from the environment, so no host name lives in the code:
#   MEDIA_AUTH_URL       sign-in site of the reverse proxy's identity provider (Authelia: https://auth.example.org).
#                        Empty: there is no external login page - logout returns to "/", and a browser that
#                        reaches the app without the proxy's headers gets a plain 401.
#   MEDIA_PUBLIC_URL     where the app is reached from outside; the address logout returns to.
#   MEDIA_VISION_WORKER  address of the vision worker when indexer_config.json does not name one.
AUTH_URL = os.environ.get("MEDIA_AUTH_URL", "").rstrip("/")
PUBLIC_URL = os.environ.get("MEDIA_PUBLIC_URL", "").rstrip("/")
DEFAULT_WORKER_URL = os.environ.get("MEDIA_VISION_WORKER", "http://127.0.0.1:8410")
SHARE_THUMBS_DIR = os.path.join(BASE_DIR, "share_thumbnails")
ROOTS_FILE = os.path.join(BASE_DIR, "roots.json")
# Identifier for the built-in downloads root. Stored paths that carry no
# root (everything written before roots existed) resolve against this one.
DEFAULT_ROOT_ID = "downloads"

# Limits & Cleanup Settings
MAX_LOG_LINES = 500
MAX_COMPLETED_AGE_SECONDS = 10 * 60
MAX_COMPLETED_TASKS = 25
USAGE_RETENTION_DAYS = 7

ALL_CATEGORIES = [
    "youtube", "twitter", "pixiv", "danbooru",
    "civitai", "patreon", "bluesky", "deviantart", "discord", "bilibili", "instagram"
]

DEFAULT_CONFIG = {
    "pipeline_wait_seconds": {cat: 5 for cat in ALL_CATEGORIES},
    # Single YouTube videos never sleep. For a playlist/channel, a random
    # pause p in [min_seconds, max_seconds] is inserted between videos, but
    # only once more than threshold_count videos have been downloaded within
    # the last window_minutes minutes (see youtube_rate_limit_guard.py).
    "youtube_playlist_sleep": {
        "min_seconds": 0,
        "max_seconds": 180,
        "threshold_count": 100,
        "window_minutes": 60
    },
    "max_displayed_tasks_per_pipe": 5,
    # How many entries the "Recently Downloaded" list keeps. Raising it
    # costs memory, not I/O - entries are built a page at a time - and
    # widens the window the 24h download-volume seed can see after a
    # restart. KSF_MAX_RECENT_MEDIA_ITEMS overrides it.
    "max_recent_media_items": recent_media_store.DEFAULT_MAX_RECENT_MEDIA_ITEMS,
    # Live upscaling (see "Live upscaling" below). An image looked at for
    # view_seconds is upscaled on the vision worker until a zoom of zoom_factor
    # on the viewer's screen stays sharp; results are cached and evicted
    # cache_days after they were last looked at.
    # Frame interpolation in the video player (Lossless Scaling frame generation
    # on the vision worker, vision/vidinterp). The viewer turns it on per device
    # and picks a bandwidth cap there; these are the server-side bounds.
    "interpolation": {
        "enabled": True,
        # The bandwidth caps the player offers, in Mbit/s. 0 is "Max": no cap,
        # the encoder's quality target alone decides (~57 Mbit/s at 4K120).
        "bitrates_mbps": [5, 10, 20, 40, 80, 0],
        # 20 Mbit/s: enough for 1080p120 and a clean 1440p120, a sensible floor
        # for 4K120 on a home connection; LAN viewers pick Max.
        "default_mbps": 20,
        # Highest frame rate to generate, whatever the screen reports.
        "max_output_fps": 240,
        # Resolution the stream is interpolated at, at most.
        "max_height": 2160,
    },
    # Covers that show the wrong frame - a channel intro, a fade to black -
    # remade from a moment chosen per video (song_covers.py).
    "song_covers": dict(song_covers.DEFAULTS),
    "live_upscale": {
        "enabled": True,
        # Share pages too (public_share_upscale): visitors of a link get the same
        # upscaling, on the same GPU queue. Only while "enabled" is on as well.
        "shares": True,
        "view_seconds": 5,
        "zoom_factor": 2,
        "max_factor": 4,
        "max_output_megapixels": 64,
        # A song cover's copy fits this box, "none" = no box (cover_size_cap).
        "cover_max_size": "7680x4320",
        "cache_days": 7,
        "cache_max_gb": 20,
        # "" = beside the downloads on the media disk (hdd1, thick-provisioned).
        "cache_dir": "",
        "quality": 92,
        "tag_min_confidence": 0.35,
        # When no tag or caption decides, a face InsightFace detected at least
        # this surely means a photograph: drawn faces score lower. At 0.8, 47% of
        # photos and 2.5% of drawings have one (vault KSF-019). 1 = never.
        "face_min_score": 0.8,
        # Checked in this order; the first that matches picks the model.
        "photo_tags": ["realistic", "photorealistic", "photo_(medium)", "cosplay",
                       "photo_background", "3d", "asian", "selfie"],
        "anime_tags": ["anime_coloring", "1girl", "1boy", "2girls", "multiple_girls",
                       "chibi", "comic", "manga", "monochrome", "greyscale", "sketch"],
        # "general" here means the photo upscaler below.
        "default_model": "general",
        # One of UPSCALE_PHOTO_MODELS: what photos, cosplay and renders get.
        "photo_model": "nomoswebphoto-realplksr",
        # Artwork only: the vision worker measures the film grain an artist laid
        # over the picture and puts back what the anime model painted out
        # (vision/grain.py, vault KSF-045).
        "restore_grain": True,
        # Song covers larger than they are sharp ("Soft covers" in plan_upscale):
        # folders, "Root / path" or a path in any root, whose covers count as
        # at most soft_cover_height pixels on their short side - and no soft
        # cover, listed or measured, is taken smaller than that.
        "soft_cover_folders": [],
        "soft_cover_height": 1440,
        # And covers anywhere whose edges measure no steeper than this ("Soft
        # covers, found by measuring"): 0.40 is a sharp cover limited to half
        # its size. Measured from soft_cover_auto_min_long pixels on the long
        # side up: 0 = every cover, 3800 = 4K covers only.
        "soft_cover_auto": True,
        "soft_cover_auto_edge": 0.40,
        "soft_cover_auto_min_long": 0
    },
    # Rules whose pending suggestions are trashed without review (settings page,
    # Image processing). Everything else waits in the review queue.
    "auto_trash_reasons": ["no subject", "screenshot in a photo profile"],
    # Trash batches older than this many days are emptied for good; 0 = never.
    "trash_purge_days": 30,
    # Character blacklist (settings page): files of listed characters are trashed
    # (ban) when the tagger gave the tag at least min_p - the floor the tagger
    # stores tags at, so the grid's counts and the sweep agree. At most
    # max_per_sweep files go in one sweep; the rest follow in the next.
    "character_blacklist": {"min_p": 0.35, "max_per_sweep": 5000},
    # Measuring new downloads (dimensions, frame rate, duration) every night.
    "dimensions_backfill": {"enabled": False, "hour": 4},
    # Twitter posts that are one picture cut into pieces are joined after each
    # download, the pieces trashed with a ban (post_stitch.py).
    "twitter_stitch": {"enabled": True},
    # Pictures that are byte-identical to one already in the library: a copy in
    # another folder becomes a hard link, a second name in the SAME folder goes
    # to the trash with a ban (image_dedup.py). This switch is the after-download
    # check; the library pass is POST /api/images/dedup (a dry run by default).
    "image_dedup": {"enabled": True},
    # A song held twice - one title with the artist in front, one without - is
    # merged after each catalogue build: the better file stays, under the
    # clean title (song_merge.py).
    "song_merge": {"enabled": True},
    # Intros and outros that repeat across a folder's videos, found and cut
    # without a re-encode once approved (video_trim.py).
    "video_trim": dict(video_trim.DEFAULTS),
    # New share links expire after this many days; 0 = never.
    "shares": {"default_expiry_days": 0},
    # 2D -> 3D conversion on the vision worker (stereo3d.py). On demand: nothing converts
    # until "Convert to 3D" or (later) a favourite asks. Pictures are never resolution-capped.
    "stereo3d": {
        "enabled": True,
        # "" = ~/.cache/media-dl-3d (or MEDIA_3D_CACHE). Never created here, never /data2 or the app dir.
        "cache_dir": "",
        "cache_max_gb": 150,
        # Videos only: the longer side of each eye, in pixels (2160 = the headset panel).
        "max_eye_long_side": 2160,
        "divergence": 2.0,
        "convergence": 0.5,
        # Bitrate cap of the side-by-side stream by eye resolution.
        "mbps_1080": 30,
        "mbps_1440": 50,
        "mbps_2160": 80,
        # Favourites of admins are converted in the background (hook and backfill: later phase).
        "favorites_auto": True,
        "favorites_backfill_days": 7,
        # Folders whose videos and pictures are all converted and never leave the cache - one per
        # line, "Downloads / youtube/Some channel" or a bare path for every library.
        "permanent_folders": [],
        "max_attempts": 2,
        # The VR app asks for 3D when it opens a picture/video without one (POST /api/media/3d/view):
        # view_auto off = never; view_auto_admin_only = only admins' views count.
        "view_auto": True,
        "view_auto_admin_only": False,
        # Queued "viewing" items per user; a newer view demotes the oldest to the manual lane.
        "viewing_max_per_user": 3
    }
}

DEFAULT_PREFS = {"advanced_mode": False}

def load_config():
    """Liest config.json ein, merged fehlende Keys mit den Defaults auf und
    schreibt die Datei beim ersten Start (oder bei fehlenden Keys) zurueck."""
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))  # deep copy
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            for key, value in loaded.items():
                if key in ("pipeline_wait_seconds", "youtube_playlist_sleep", "live_upscale",
                           "dimensions_backfill", "shares", "song_covers",
                           "twitter_stitch", "image_dedup", "song_merge", "stereo3d", "video_trim",
                           "character_blacklist") and isinstance(value, dict):
                    cfg[key].update(value)
                else:
                    cfg[key] = value
        except Exception as e:
            print(f"[Warning] Failed to load {CONFIG_FILE}, using defaults: {str(e)}")

    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=4, ensure_ascii=False)
    except Exception as e:
        print(f"[Warning] Failed to write {CONFIG_FILE}: {str(e)}")

    return cfg

CONFIG = load_config()

# Ensure download subdirectories exist
for category in ALL_CATEGORIES:
    os.makedirs(os.path.join(DOWNLOADS_DIR, category), exist_ok=True)

# In-memory tracking for active and historic downloads
active_tasks = {}
task_lock = threading.Lock()
users_lock = threading.Lock()
usage_lock = threading.Lock()
logging.getLogger('werkzeug').setLevel(logging.ERROR)

# Login rate limiting: per-IP sliding window over failed attempts only -
# successful logins clear the counter. Same deque+cutoff pattern used
# elsewhere in this file (e.g. item_download_events).
LOGIN_RATE_LIMIT_MAX_ATTEMPTS = 5
LOGIN_RATE_LIMIT_WINDOW_SECONDS = 300

pipelines = {cat: queue.Queue() for cat in ALL_CATEGORIES}
running_processes = {}

# Set by the shutdown handler. A download killed by a restart must not be
# recorded as "Failed" - it has been queued to resume, and Failed is a
# status the startup recovery deliberately does not touch.
SHUTTING_DOWN = threading.Event()

pipeline_stats = {
    cat: {
        "rate_mbps": 0.0,
        "items_downloaded": 0,
        "session_total_mb": 0.0,
        "last_time": time.time()
    } for cat in ALL_CATEGORIES
}

# Global (all-pipeline) item-download timestamps, used for the "items in the
# last N minutes" system stat. Mutated/read only while holding task_lock,
# since it's appended to from the same code path as pipeline_stats.
item_download_events = deque()

system_stats_lock = threading.Lock()
SYSTEM_STATS = {
    "cpu_percent": 0.0,
    "ram_percent": 0.0,
    "ram_used_gb": 0.0,
    "ram_total_gb": 0.0,
    "net_sent_mbps": 0.0,
    "net_recv_mbps": 0.0,
    "disk_free_gb": 0.0,
    "disk_total_gb": 0.0,
    "disk_used_percent": 0.0,
    "downloaded_bytes_24h": 0,
    "items_last_60min": 0
}

# -------------------------------------------------------------------
# Auth Helpers
# -------------------------------------------------------------------

def _read_users_unlocked():
    if not os.path.exists(USERS_FILE):
        return []
    with open(USERS_FILE, "r", encoding="utf-8") as f:
        return json.load(f)

def _write_users_unlocked(users):
    temp_file = USERS_FILE + ".tmp"
    with open(temp_file, "w", encoding="utf-8") as f:
        json.dump(users, f, indent=2, ensure_ascii=False)
    try:
        os.chmod(temp_file, 0o600)  # holds API tokens: readable by the service account only
    except OSError:
        pass
    os.replace(temp_file, USERS_FILE)

def load_users():
    with users_lock:
        try:
            return _read_users_unlocked()
        except Exception as e:
            print(f"[Warning] Failed to load {USERS_FILE}: {str(e)}")
            return []

def save_users(users):
    with users_lock:
        _write_users_unlocked(users)

def update_user_prefs(username, updates):
    """Liest, aendert und schreibt users.json in einer einzigen kritischen
    Sektion, um Lost-Update-Races bei getrennten load/save-Aufrufen zu vermeiden."""
    with users_lock:
        try:
            users = _read_users_unlocked()
        except Exception:
            return None
        for u in users:
            if u["username"].lower() == username.lower():
                u.setdefault("prefs", dict(DEFAULT_PREFS))
                u["prefs"].update(updates)
                _write_users_unlocked(users)
                return u
        return None

# --- Single sign-on -------------------------------------------------------
# Caddy runs this site behind Authelia (forward_auth) and forwards the signed-in
# identity as Remote-User / Remote-Groups. Honouring that means one login for
# the whole estate instead of signing in to Authelia and then again here.
#
# The headers are only trusted when the request actually arrived FROM Caddy.
# They are trivially forgeable by anything that can reach this port directly,
# so without the peer check a lab host could simply declare itself an admin.
def _env_set(name, default):
    raw = os.environ.get(name)
    return {x.strip() for x in (raw if raw is not None else default).split(",") if x.strip()}


# Settings (environment, comma separated):
#   MEDIA_PROXY_IPS     addresses of the reverse proxy; only requests from these may carry Remote-User
#                       (default: this machine, 127.0.0.1 and ::1)
#   MEDIA_ADMIN_GROUPS  groups that make a signed-in user an admin
#   MEDIA_GUEST_GROUPS  groups that make a signed-in user a media-only guest
#   MEDIA_SINGLE_USER   name of one admin that EVERY request is treated as, with no login at all.
#                       For a private machine or a trial run; never set it on a server others can reach.
SSO_PROXY_IPS = _env_set("MEDIA_PROXY_IPS", "127.0.0.1,::1")
SSO_ADMIN_GROUPS = _env_set("MEDIA_ADMIN_GROUPS", "admins,web-dl_admin,web-dl_download")
SSO_GUEST_GROUPS = _env_set("MEDIA_GUEST_GROUPS", "web-dl_media")
SINGLE_USER = os.environ.get("MEDIA_SINGLE_USER", "").strip()


def get_sso_user():
    """The Authelia identity for this request, or None if there isn't a
    trustworthy one. Groups map onto this app's existing admin/guest roles."""
    if SINGLE_USER:
        return ensure_user_record(SINGLE_USER, "admin")
    if request.remote_addr not in SSO_PROXY_IPS:
        return None
    username = (request.headers.get("Remote-User") or "").strip()
    if not username:
        return None

    groups = {g.strip() for g in
              (request.headers.get("Remote-Groups") or "").split(",") if g.strip()}
    if groups & SSO_ADMIN_GROUPS:
        role = "admin"
    elif groups & SSO_GUEST_GROUPS:
        role = "guest"
    else:
        # Authenticated to Authelia but with no web-dl capability. Caddy should
        # already have refused; deny here too rather than guessing a role.
        return None

    return ensure_user_record(username, role)


def ensure_user_record(username, role):
    """Authelia owns identity now. users.json is no longer an account database:
    it carries only per-user state -- preferences and API tokens -- so a row is
    created the first time someone signs in, and the role is refreshed from
    their current Authelia groups on every visit."""
    with users_lock:
        try:
            users = _read_users_unlocked()
        except Exception:
            return {"username": username, "role": role, "prefs": dict(DEFAULT_PREFS)}
        for u in users:
            if u["username"].lower() == username.lower():
                changed = False
                if u.get("role") != role:
                    u["role"] = role          # groups changed in Authelia
                    changed = True
                # For the settings page's user list; hourly, not per request.
                if time.time() - float(u.get("last_seen") or 0) > 3600:
                    u["last_seen"] = time.time()
                    changed = True
                if changed:
                    _write_users_unlocked(users)
                return u
        record = {"username": username, "role": role, "prefs": dict(DEFAULT_PREFS)}
        users.append(record)
        _write_users_unlocked(users)
        return record


def get_api_token_user():
    """Identity from a long-lived API token, for clients that cannot hold a
    browser session: the phone's share-to-disk shortcut, scripts, cron.

    Deliberately independent of the session cookie. A cookie is the wrong
    credential for automation -- it expires, it is 64 characters of hex that is
    easy to mangle in a text field, and a client-side cookie jar can silently
    override the header you set, which is exactly the failure this replaces.
    Rotating a browser session no longer breaks automation, and revoking a
    token no longer logs anyone out."""
    token = (request.headers.get("X-API-Key") or "").strip()
    if not token:
        return None
    for u in load_users():
        stored = u.get("api_token")
        # compare_digest on both sides: a plain == would leak the token a
        # character at a time through response timing.
        if stored and secrets.compare_digest(str(stored), token):
            return u
    return None


def get_authenticated_user():
    sso = get_sso_user()
    if sso:
        return sso
    return get_api_token_user()

def is_admin(user):
    # Users created before roles existed have no "role" key - treat those as admin.
    return user.get("role", "admin") == "admin"

def require_admin(view_fn):
    """Route decorator: 401 if not logged in, 403 if logged in but not an
    admin (i.e. a guest account). Guests are limited to the media browser -
    see serve_static() and the /api/media/* routes."""
    @wraps(view_fn)
    def wrapper(*args, **kwargs):
        user = get_authenticated_user()
        if not user:
            return jsonify({"error": "Unauthorized"}), 401
        if not is_admin(user):
            return jsonify({"error": "Forbidden"}), 403
        return view_fn(*args, **kwargs)
    return wrapper

# -------------------------------------------------------------------
# Background Downloader Engine
# -------------------------------------------------------------------

def sanitize_folder_name(name: str) -> str:
    return re.sub(r'[\\/*?:"<>|]', "", name).strip()

# gallery_dl's own wording when it sleeps off a rate limit, e.g.
# "[twitter][info] Waiting for 14 minutes until 11:06:18 (rate limit)".
RATE_LIMIT_WAIT_RE = re.compile(r"\(rate limit\)", re.IGNORECASE)

class RateLimitStallDetector:
    """Watches a downloader's output for a rate limit it won't recover from.

    gallery_dl answers a rate limit by sleeping until the reset and then
    retrying, which is the right thing while the run is still producing
    files. Twitter can instead settle into a loop where every retry is
    limited again straight away; left alone that repeats indefinitely
    (observed: 9 hours, 36 waits, not one file) while holding the
    category's pipeline slot the whole time.

    Waiting is therefore only treated as a stall when two waits in a row
    have nothing downloaded between them - a run that is still making
    progress keeps waiting as long as it needs to."""

    def __init__(self, dest_path):
        self.dest_path = dest_path
        self.files_downloaded = 0
        self._files_at_last_wait = None

    def observe(self, line):
        """Feeds one output line in. Returns True once the run is stalled."""
        # gallery_dl prints each file's destination path as it saves it;
        # a file it already has is the same line prefixed with "# ".
        #
        # A skipped file counts as progress too. It means the run is walking
        # forward through the timeline, which is the thing this detector
        # exists to distinguish from going nowhere - and it is ALL a resumed
        # run does at first, because everything fetched before the restart is
        # already on disk. Counting only new files killed exactly the
        # downloads a restart had just brought back: two rate-limit waits
        # during that skip phase and the run was declared stalled, having in
        # fact been making perfectly good progress.
        if self.dest_path and (line.startswith(self.dest_path)
                               or line.startswith("# " + self.dest_path)):
            self.files_downloaded += 1
            return False

        if not RATE_LIMIT_WAIT_RE.search(line):
            return False

        stalled = self._files_at_last_wait == self.files_downloaded
        self._files_at_last_wait = self.files_downloaded
        return stalled

MEDIA_EXTENSIONS = {
    "image": {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".svg"},
    "video": {".mp4", ".webm", ".mkv", ".mov", ".avi", ".m4v"},
    "audio": {".mp3", ".m4a", ".flac", ".ogg", ".opus", ".wav", ".mka"},
}

# Python's mimetypes table has no entry for .mka, so Flask would serve it as
# application/octet-stream and the browser would download it instead of
# playing it. (.mkv it does know.) Registering it here also makes
# send_from_directory correct for anyone fetching the original with ?raw=1.
mimetypes.add_type("audio/x-matroska", ".mka")

def classify_media_type(filename):
    ext = os.path.splitext(filename)[1].lower()
    for kind, exts in MEDIA_EXTENSIONS.items():
        if ext in exts:
            return kind
    return "other"


# What a file counts AS when filtering, which is not the same question as what
# it is played back as. A GIF is a silent video that every browser draws like a
# picture, and a library has both kinds of GIF in it - a reaction clip and a
# static meme - so it answers to both filters rather than being sorted into the
# wrong one. classify_media_type keeps calling it an image, because that is
# what decides how it is displayed.
KIND_ALIASES = {".gif": ("image", "video")}

# What the UI calls them. "music" reads better than "audio" on a button, and
# the API takes either.
KIND_SYNONYMS = {"music": "audio", "audio": "audio",
                 "video": "video", "videos": "video",
                 "image": "image", "images": "image", "picture": "image"}


def kinds_of(filename):
    """Every kind a file answers to. Usually one."""
    ext = os.path.splitext(filename)[1].lower()
    if ext in KIND_ALIASES:
        return KIND_ALIASES[ext]
    return (classify_media_type(filename),)


def parse_kinds(raw):
    """The requested kinds as a set, or None for "no kind filter". Accepts a
    comma-separated list so several can be shown at once."""
    if not raw:
        return None
    wanted = {KIND_SYNONYMS.get(part.strip().lower())
              for part in str(raw).split(",") if part.strip()}
    wanted.discard(None)
    return wanted or None


def kind_matches(filename, wanted):
    return wanted is None or bool(wanted.intersection(kinds_of(filename)))

def get_embedded_cover(file_path):
    """Reads the cover art youtube_music_extractor.py embeds into audio
    files (see embed_tags() there) back out. Returns (mime, bytes) or None
    if the format is unsupported or the file has no embedded picture."""
    ext = os.path.splitext(file_path)[1].lower()
    # Matroska keeps its cover as an attachment, not a tag, and mutagen has no
    # Matroska support at all - so this one goes out to ffmpeg instead.
    if ext == ".mka":
        return get_mka_cover(file_path)
    try:
        if ext == ".m4a":
            from mutagen.mp4 import MP4
            audio = MP4(file_path)
            covr = audio.tags.get("covr") if audio.tags else None
            if covr:
                cover = covr[0]
                mime = "image/png" if cover.imageformat == cover.FORMAT_PNG else "image/jpeg"
                return mime, bytes(cover)

        elif ext in (".opus", ".ogg"):
            import base64
            from mutagen.flac import Picture
            if ext == ".opus":
                from mutagen.oggopus import OggOpus
                audio = OggOpus(file_path)
            else:
                from mutagen.oggvorbis import OggVorbis
                audio = OggVorbis(file_path)
            raw = audio.get("metadata_block_picture")
            if raw:
                pic = Picture(base64.b64decode(raw[0]))
                return pic.mime or "image/jpeg", pic.data

        elif ext == ".mp3":
            from mutagen.mp3 import MP3
            audio = MP3(file_path)
            if audio.tags:
                apic_keys = [k for k in audio.tags.keys() if k.startswith("APIC")]
                if apic_keys:
                    apic = audio.tags[apic_keys[0]]
                    return apic.mime or "image/jpeg", apic.data

        elif ext == ".flac":
            from mutagen.flac import FLAC
            audio = FLAC(file_path)
            if audio.pictures:
                pic = audio.pictures[0]
                return pic.mime or "image/jpeg", pic.data
    except Exception as e:
        print(f"[Warning] Failed to read embedded cover from {file_path}: {str(e)}")
    return None

THUMB_CACHE_DIR = preview_location("thumbs", os.path.join(BASE_DIR, "media_thumb_cache"))

# How many thumbnails may be GENERATED at once. Serving cached ones is not
# gated. Two is chosen for spinning disks: each build reads a different large
# file, and concurrent readers turn sequential reads into seek thrash - the
# limit makes a cold folder render sooner, not later. Raise it if the library
# ever moves back onto flash.
THUMB_BUILD_CONCURRENCY = int(os.environ.get("KSF_THUMB_BUILD_CONCURRENCY", "2"))
_thumb_build_semaphore = threading.BoundedSemaphore(THUMB_BUILD_CONCURRENCY)

THUMB_MAX_DIMENSION = 400

def _cached_thumbnail(file_path, build_arg_variants):
    """Shared disk cache for generated thumbnails. build_arg_variants is a
    list of ffmpeg argv candidates (each minus the output path), tried in
    order until one actually produces a non-empty file - ffmpeg exits 0 while
    writing nothing when e.g. the seek lands past the end of a very short
    clip, so the output has to be checked rather than just the exit code.
    Cache entries are keyed by source path and invalidated when it changes."""
    os.makedirs(THUMB_CACHE_DIR, exist_ok=True)
    cache_key = hashlib.sha256(file_path.encode("utf-8")).hexdigest()
    cache_path = os.path.join(THUMB_CACHE_DIR, cache_key + ".jpg")

    src_mtime = None
    try:
        src_mtime = os.path.getmtime(file_path)
        if os.path.exists(cache_path) and os.path.getmtime(cache_path) >= src_mtime:
            with open(cache_path, "rb") as f:
                return "image/jpeg", f.read()
    except OSError:
        pass

    # Cache miss: this is where a thumbnail costs a read of the source file.
    # A folder of forty unseen videos arrives as forty parallel requests
    # (Flask is threaded), and forty ffmpeg processes each seeking into a
    # different large file is the worst thing you can ask of a spinning disk -
    # they finish LATER in total than if they had queued. The gate is around
    # generation only; a cached thumbnail never waits behind it.
    with _thumb_build_semaphore:
        # Another request may have built it while this one waited, which is
        # the common case for a grid: the wait is not wasted.
        try:
            if (src_mtime is not None and os.path.exists(cache_path)
                    and os.path.getmtime(cache_path) >= src_mtime):
                with open(cache_path, "rb") as f:
                    return "image/jpeg", f.read()
        except OSError:
            pass

        for build_args in build_arg_variants:
            try:
                subprocess.run(build_args + [cache_path], capture_output=True, timeout=20, check=True)
                if os.path.getsize(cache_path) > 0:
                    with open(cache_path, "rb") as f:
                        return "image/jpeg", f.read()
            except Exception:
                continue

    print(f"[Warning] Failed to generate thumbnail for {file_path}")
    return None

def get_video_frame_thumbnail(file_path):
    """Grabs a frame via ffmpeg for a video preview thumbnail. Most video
    containers here (webm especially) don't carry an embedded cover picture
    the way audio files do (see get_embedded_cover above), so this actually
    decodes a frame instead - cached to disk since that's too expensive to
    redo on every grid render/scroll."""
    scale = f"scale='min({THUMB_MAX_DIMENSION},iw)':-1"
    return _cached_thumbnail(file_path, [
        # -ss before -i is a fast (approximate) input seek - fine for a
        # throwaway preview frame and much quicker than decoding from 0.
        ["ffmpeg", "-y", "-ss", "3", "-i", file_path,
         "-frames:v", "1", "-vf", scale, "-q:v", "4"],
        # Clips shorter than the seek (twitter is full of sub-second ones)
        # encode nothing at all at 3s, so fall back to the first frame.
        ["ffmpeg", "-y", "-i", file_path,
         "-frames:v", "1", "-vf", scale, "-q:v", "4"],
    ])

def get_image_downscaled_thumbnail(file_path):
    """A small JPEG stand-in for a full-size image. The grid renders these
    into ~140px tiles; serving the originals meant pushing ~15MB per page
    (up to ~97MB) and decoding multi-megapixel images just to shrink them
    in CSS, which is what made scrolling the recent feed stutter."""
    return _cached_thumbnail(file_path, [
        ["ffmpeg", "-y", "-i", file_path,
         "-vf", f"scale='min({THUMB_MAX_DIMENSION},iw)':-1",
         "-frames:v", "1", "-q:v", "5"],
    ])

# -------------------------------------------------------------------
# Pixiv ugoira (animated zips)
# -------------------------------------------------------------------
# Pixiv serves its animations as a .zip of numbered still frames plus a
# per-frame delay table - there is no video file to hand a browser. These
# get transcoded to a looping mp4 on first view and cached, so the rest of
# the app (grid tiles, the player, shares, link previews) can treat them
# as ordinary videos.

UGOIRA_CACHE_DIR = cache_location("ugoira", os.path.join(BASE_DIR, "media_ugoira_cache"))
UGOIRA_FRAME_EXTS = (".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp")
# Used only when the archive carries no delay table (every zip downloaded
# before writeUgoiraInfo was switched on). 12.5fps sits in the middle of
# the range pixiv itself uses; a wrong guess changes the playback speed,
# not whether it plays.
UGOIRA_DEFAULT_FRAME_MS = 80
UGOIRA_MAX_BUILD_SECONDS = 300

_ugoira_probe_cache = {}
_ugoira_probe_lock = threading.Lock()
_ugoira_build_locks = {}
_ugoira_build_locks_guard = threading.Lock()

def is_ugoira_zip(file_path):
    """True for a .zip that is really an animation: nothing but image
    frames inside. Reading a zip's central directory is one seek, and the
    answer is cached per (mtime, size), so this stays cheap even though
    directory listings call it for every archive they pass."""
    if not file_path.lower().endswith(".zip"):
        return False
    try:
        st = os.stat(file_path)
    except OSError:
        return False
    stamp = (st.st_mtime, st.st_size)

    with _ugoira_probe_lock:
        cached = _ugoira_probe_cache.get(file_path)
        if cached and cached[0] == stamp:
            return cached[1]

    try:
        with zipfile.ZipFile(file_path) as z:
            names = [n for n in z.namelist() if not n.endswith("/")]
        frames = [n for n in names if n.lower().endswith(UGOIRA_FRAME_EXTS)]
        # animation.json is pixiv's own delay table; anything else in there
        # means this is a regular archive that happens to hold pictures.
        result = len(frames) >= 2 and all(
            n in frames or os.path.basename(n) == "animation.json" for n in names
        )
    except Exception:
        result = False

    with _ugoira_probe_lock:
        if len(_ugoira_probe_cache) > 4096:
            _ugoira_probe_cache.clear()
        _ugoira_probe_cache[file_path] = (stamp, result)
    return result

def classify_media_path(file_path):
    """classify_media_type, but for a path that exists on disk - so an
    ugoira zip can be reported as the video it becomes."""
    kind = classify_media_type(file_path)
    if kind == "other" and is_ugoira_zip(file_path):
        return "video"
    return kind

def _ugoira_delays(zip_file, names):
    """Per-frame delays in ms. Prefers pixiv's own table (animation.json
    inside the archive, or the .js sidecar PixivUtil2 writes next to it),
    and falls back to a constant delay when neither exists."""
    raw = None
    entry = next((n for n in zip_file.namelist()
                  if os.path.basename(n) == "animation.json"), None)
    if entry:
        try:
            raw = zip_file.read(entry).decode("utf-8", "replace")
        except Exception:
            raw = None
    if raw is None and zip_file.filename:
        sidecar = zip_file.filename[:-4] + ".js"
        try:
            with open(sidecar, "r", encoding="utf-8", errors="replace") as f:
                raw = f.read()
        except OSError:
            raw = None

    delays = {}
    if raw:
        try:
            data = json.loads(raw)
            for frame in (data.get("frames") or []):
                delays[frame["file"]] = float(frame["delay"])
        except Exception:
            delays = {}

    return [max(10.0, delays.get(n, UGOIRA_DEFAULT_FRAME_MS)) for n in names]

def _build_ugoira_video(file_path, out_path):
    """Transcodes one ugoira zip into out_path (mp4). Returns True on
    success. The frames are fed to ffmpeg through a concat script, which
    is what lets each frame keep its own duration."""
    os.makedirs(UGOIRA_CACHE_DIR, exist_ok=True)
    temp_dir = tempfile.mkdtemp(prefix="ugoira_", dir=UGOIRA_CACHE_DIR)
    try:
        with zipfile.ZipFile(file_path) as z:
            names = sorted(n for n in z.namelist()
                           if n.lower().endswith(UGOIRA_FRAME_EXTS))
            if not names:
                return False
            for name in names:
                # Flattened on purpose: a zip entry path is untrusted input
                # and extractall() would happily honour "../" inside it.
                with z.open(name) as src, open(
                        os.path.join(temp_dir, os.path.basename(name)), "wb") as dst:
                    shutil.copyfileobj(src, dst)
            delays = _ugoira_delays(z, names)

        lines = ["ffconcat version 1.0"]
        for name, delay in zip(names, delays):
            lines.append("file '" + os.path.basename(name) + "'")
            lines.append(f"duration {delay / 1000.0:.4f}")
        # The concat demuxer gives the final entry no duration of its own,
        # so the last frame would flash past; repeating it fixes that.
        lines.append("file '" + os.path.basename(names[-1]) + "'")
        script = os.path.join(temp_dir, "frames.ffconcat")
        with open(script, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")

        temp_out = os.path.join(temp_dir, "out.mp4")
        result = subprocess.run(
            ["ffmpeg", "-y", "-safe", "0", "-i", script,
             # h264 needs even dimensions and yuv420p to play everywhere.
             "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2",
             "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
             "-pix_fmt", "yuv420p", "-vsync", "vfr",  # -fps_mode needs ffmpeg 5.1+
             "-movflags", "+faststart", temp_out],
            capture_output=True, timeout=UGOIRA_MAX_BUILD_SECONDS)
        if result.returncode != 0 or not os.path.exists(temp_out) or os.path.getsize(temp_out) == 0:
            tail = result.stderr.decode("utf-8", "replace")[-400:]
            print(f"[Warning] ugoira conversion failed for {file_path}: {tail}")
            return False
        os.replace(temp_out, out_path)
        return True
    except Exception as e:
        print(f"[Warning] ugoira conversion failed for {file_path}: {e}")
        return False
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)

def get_ugoira_video(file_path):
    """Path to the cached mp4 for an ugoira zip, building it on first use.
    Returns None if the conversion fails. Two viewers opening the same
    animation at once wait on one build instead of racing."""
    os.makedirs(UGOIRA_CACHE_DIR, exist_ok=True)
    cache_key = hashlib.sha256(file_path.encode("utf-8")).hexdigest()
    out_path = os.path.join(UGOIRA_CACHE_DIR, cache_key + ".mp4")

    def is_fresh():
        try:
            return (os.path.exists(out_path)
                    and os.path.getsize(out_path) > 0
                    and os.path.getmtime(out_path) >= os.path.getmtime(file_path))
        except OSError:
            return False

    if is_fresh():
        return out_path

    with _ugoira_build_locks_guard:
        lock = _ugoira_build_locks.setdefault(cache_key, threading.Lock())
    with lock:
        if is_fresh():
            return out_path
        return out_path if _build_ugoira_video(file_path, out_path) else None

# --- Matroska audio (.mka) ------------------------------------------------
# yt-dlp reaches for .mka when the audio it kept does not fit a simpler
# container. No browser plays Matroska audio, and mutagen cannot read it at
# all, so a .mka is handled the same way an ugoira zip is: keep the original
# on disk, serve a cached rendition the browser understands, and let ?raw=1
# still hand over the real file.
MKA_CACHE_DIR = cache_location("mka", os.path.join(BASE_DIR, "media_mka_cache"))
MKA_REMUX_TIMEOUT = 120

# Codec -> (container, mimetype, ffmpeg muxer). Every one of these is a stream
# COPY, so the rendition is lossless and costs no more than reading the file
# once. The muxer is named explicitly because the build writes to a ".part"
# file first: ffmpeg picks its output format from the extension, and a name
# ending in .part matches nothing ("Unable to find a suitable output format").
MKA_COPY_TARGETS = {
    "opus":   (".ogg",  "audio/ogg",   "ogg"),
    "vorbis": (".ogg",  "audio/ogg",   "ogg"),
    "aac":    (".m4a",  "audio/mp4",   "ipod"),
    "alac":   (".m4a",  "audio/mp4",   "ipod"),
    "flac":   (".flac", "audio/flac",  "flac"),
    "mp3":    (".mp3",  "audio/mpeg",  "mp3"),
    "ac3":    (".ogg",  "audio/ogg",   "ogg"),   # not copyable; re-encoded below
    "eac3":   (".ogg",  "audio/ogg",   "ogg"),
    "dts":    (".ogg",  "audio/ogg",   "ogg"),
    "pcm_s16le": (".wav", "audio/wav", "wav"),
}
# Anything the browser cannot play even after a remux gets re-encoded to opus.
MKA_REENCODE = {"ac3", "eac3", "dts", "truehd", "mlp", "wmav2", "wmapro"}

_mka_build_locks = {}
_mka_build_locks_guard = threading.Lock()

def _mka_audio_codec(file_path):
    """The codec name of the first audio stream, or None if ffprobe cannot
    tell us - a file with no audio stream is not something to remux."""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "a:0",
             "-show_entries", "stream=codec_name", "-of", "csv=p=0", file_path],
            capture_output=True, text=True, timeout=30)
        return (out.stdout or "").strip().lower() or None
    except Exception:
        return None

def get_mka_playable(file_path):
    """(path, mimetype) of a browser-playable rendition of a .mka, built on
    first use and cached. Returns None if the file cannot be converted, in
    which case the caller serves the original and the browser offers it as a
    download - which is still better than a silent failure."""
    codec = _mka_audio_codec(file_path)
    if not codec:
        return None
    ext, mime, fmt = MKA_COPY_TARGETS.get(codec, (".ogg", "audio/ogg", "ogg"))

    os.makedirs(MKA_CACHE_DIR, exist_ok=True)
    cache_key = hashlib.sha256(file_path.encode("utf-8")).hexdigest()
    out_path = os.path.join(MKA_CACHE_DIR, cache_key + ext)

    def is_fresh():
        try:
            return (os.path.exists(out_path)
                    and os.path.getsize(out_path) > 0
                    and os.path.getmtime(out_path) >= os.path.getmtime(file_path))
        except OSError:
            return False

    if is_fresh():
        return out_path, mime

    # Same one-build-per-file discipline as the ugoira cache: two viewers
    # opening the same track wait on one remux instead of racing.
    with _mka_build_locks_guard:
        lock = _mka_build_locks.setdefault(cache_key, threading.Lock())
    with lock:
        if is_fresh():
            return out_path, mime
        # -vn drops the attached cover art: it is a video stream as far as
        # ffmpeg is concerned, and carrying it into an ogg breaks playback.
        if codec in MKA_REENCODE:
            args = ["-vn", "-c:a", "libopus", "-b:a", "160k"]
        else:
            args = ["-vn", "-c:a", "copy"]
        args += ["-f", fmt]
        tmp_path = out_path + ".part"
        try:
            proc = subprocess.run(
                ["ffmpeg", "-y", "-i", file_path] + args + [tmp_path],
                capture_output=True, text=True, timeout=MKA_REMUX_TIMEOUT)
            # ffmpeg exits 0 having written nothing often enough that the
            # size check matters more than the return code.
            if proc.returncode != 0 or not os.path.exists(tmp_path) \
                    or os.path.getsize(tmp_path) == 0:
                print(f"[Warning] .mka remux failed for {file_path}: "
                      f"{(proc.stderr or '').strip()[-300:]}")
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
                return None
            os.replace(tmp_path, out_path)
            return out_path, mime
        except Exception as e:
            print(f"[Warning] .mka remux errored for {file_path}: {e}")
            if os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass
            return None

def get_mka_cover(file_path):
    """Cover art out of a .mka. Matroska carries it as an attachment rather
    than a tag, so mutagen (which cannot open Matroska at all) is no help
    and ffmpeg does the extraction."""
    try:
        with tempfile.TemporaryDirectory(prefix="mka_cover_") as temp_dir:
            out = os.path.join(temp_dir, "cover.png")
            proc = subprocess.run(
                ["ffmpeg", "-y", "-i", file_path, "-an", "-c:v", "copy",
                 "-frames:v", "1", out],
                capture_output=True, text=True, timeout=60)
            if proc.returncode != 0 or not os.path.exists(out) \
                    or os.path.getsize(out) == 0:
                return None
            with open(out, "rb") as f:
                return "image/png", f.read()
    except Exception as e:
        print(f"[Warning] Failed to read cover from {file_path}: {e}")
        return None

def playable_media_path(file_path):
    """The file a browser can actually play for this path: an ugoira zip
    becomes its converted mp4, a .mka becomes a remuxed container, and
    everything else is itself. Returns (path, mimetype_or_None)."""
    if is_ugoira_zip(file_path):
        video = get_ugoira_video(file_path)
        if video:
            return video, "video/mp4"
    if file_path.lower().endswith(".mka"):
        remuxed = get_mka_playable(file_path)
        if remuxed:
            return remuxed
    return file_path, None

def send_media_file(file_path):
    """Serves a media file, transparently substituting the playable
    rendition for formats a browser can't open (see playable_media_path).
    ?raw=1 always serves what is on disk, so the archive itself stays
    downloadable.

    ?download=1 sends it as an attachment. That exists because the menu's
    Download used to rely solely on an anchor's `download` attribute, which
    is a REQUEST to the browser: the response said
    `Content-Disposition: inline` and a managed browser is entitled to honour
    that instead, which is exactly what happened on a locked-down laptop -
    the click ran, the anchor was correct, and nothing was saved. A response
    that says `attachment` is not a hint, so no client policy has to be
    talked round.
    """
    as_attachment = request.args.get("download") in ("1", "true")
    if request.args.get("raw") not in ("1", "true"):
        served, mimetype = playable_media_path(file_path)
        if mimetype:
            # The substitute's own extension, not a hardcoded .mp4: an ugoira
            # becomes video, but a .mka becomes .ogg/.m4a/.flac depending on
            # what codec was inside it.
            name = (os.path.splitext(os.path.basename(file_path))[0]
                    + os.path.splitext(served)[1])
            # conditional=True keeps range requests working, without which
            # seeking in the player re-fetches the whole clip.
            return send_file(served, mimetype=mimetype, conditional=True,
                             download_name=name, as_attachment=as_attachment)
    directory, filename = os.path.split(file_path)
    # download_name is passed explicitly so the attachment keeps the real
    # name; Flask encodes non-ASCII per RFC 5987, which this library needs -
    # plenty of these files are called things like "\u3010KaFKa\u3011...".
    return send_from_directory(directory, filename,
                               as_attachment=as_attachment,
                               download_name=filename if as_attachment else None)

def get_ugoira_thumbnail(file_path):
    """First frame of an animation, downscaled like any other still."""
    temp_dir = tempfile.mkdtemp(prefix="ugoira_thumb_")
    try:
        with zipfile.ZipFile(file_path) as z:
            names = sorted(n for n in z.namelist()
                           if n.lower().endswith(UGOIRA_FRAME_EXTS))
            if not names:
                return None
            frame = os.path.join(temp_dir, os.path.basename(names[0]))
            with z.open(names[0]) as src, open(frame, "wb") as dst:
                shutil.copyfileobj(src, dst)
        # Keyed on the zip, so the cache entry invalidates with the source.
        return _cached_thumbnail(file_path, [
            ["ffmpeg", "-y", "-i", frame,
             "-vf", f"scale='min({THUMB_MAX_DIMENSION},iw)':-1",
             "-frames:v", "1", "-q:v", "5"],
        ])
    except Exception:
        return None
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)

def probe_media_details(file_path):
    """Technical metadata via ffprobe: dimensions, duration, codecs, and any
    container tags (title/artist/album/genre). Returns {} when ffprobe can't
    read the file, so a preview never fails just because its info is
    unavailable."""
    # An ugoira zip has no stream of its own to read - report the mp4 it
    # is played back as. The original name is kept: a playable stand-in can
    # be a remuxed temp file, whose name carries none of the source's.
    original_name = os.path.basename(file_path)
    file_path, _mime = playable_media_path(file_path)
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_format", "-show_streams", "-of", "json", file_path],
            capture_output=True, text=True, timeout=20, check=True
        )
        data = json.loads(result.stdout)
    except Exception:
        return {}

    fmt = data.get("format") or {}
    streams = data.get("streams") or []
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)

    details = {}

    media_type = classify_media_path(file_path)

    # An image is a single-frame "video" stream to ffprobe, so this covers
    # dimensions for stills as well as real video. Audio is excluded on
    # purpose: its "video stream" is the embedded cover art, and reporting
    # the artwork's size as the song's dimensions is just wrong.
    if video and media_type in ("image", "video"):
        details["width"] = video.get("width")
        details["height"] = video.get("height")
        if media_type == "video":
            details["video_codec"] = video.get("codec_name")
            # The frame rate filter's own reading, so the details box and the
            # filter cannot disagree about a file.
            details["fps"] = media_dimensions.stream_frame_rate(video)

    if audio:
        details["audio_codec"] = audio.get("codec_name")
        details["channels"] = audio.get("channels")
        if audio.get("sample_rate"):
            try:
                details["sample_rate"] = int(audio["sample_rate"])
            except ValueError:
                pass

    # Duration/bitrate/container are only meaningful for time-based media.
    # ffprobe models a still image as a one-frame video at 25fps, which
    # yields a nonsensical "0.04s, 74 Mbit/s, image2 sequence" for a JPEG.
    if media_type in ("audio", "video"):
        if fmt.get("duration"):
            try:
                details["duration"] = float(fmt["duration"])
            except ValueError:
                pass
        if fmt.get("bit_rate"):
            try:
                details["bit_rate"] = int(fmt["bit_rate"])
            except ValueError:
                pass
        details["container"] = fmt.get("format_long_name") or fmt.get("format_name")

    # Metadata tags - "song category" is the genre field for music. Which
    # level they live at is container-dependent: MP4/Matroska put them on
    # the format, while Ogg/Opus (most of the music here) puts them on the
    # stream, so both are merged with the format winning ties.
    tags = {}
    for source in (fmt, audio, video):
        for key, value in ((source or {}).get("tags") or {}).items():
            tags.setdefault(key.lower(), value)
    for key in ("title", "artist", "album", "genre", "date", "comment"):
        if tags.get(key):
            details[key] = tags[key]

    # The source video id. It is stamped into the tags on purpose (see
    # youtube_id_for) but nothing was reading it back out for the info
    # panel, so the one field that says *which* video this is was the one
    # field you could not see.
    for key in ("purl", "comment", "woas", "description", "url"):
        value = tags.get(key)
        if not value:
            continue
        match = re.search(r"[?&]v=([A-Za-z0-9_-]{11})", str(value))
        if match:
            details["source_id"] = match.group(1)
            break
    if "source_id" not in details:
        # Videos keep it in the name yt-dlp wrote. Deliberately only these
        # two free sources: both come from data already in hand, so opening
        # a preview never costs an extra read of the file.
        match = _YOUTUBE_ID_IN_NAME.search(original_name)
        if match:
            details["source_id"] = match.group(1)
    if details.get("source_id"):
        details["source_url"] = f"https://www.youtube.com/watch?v={details['source_id']}"

    return {k: v for k, v in details.items() if v not in (None, "")}

def mark_task_started(task):
    """Records the first Running transition. Must be called while holding
    task_lock. Uses an explicit None-check (not dict.setdefault) since the
    task dict already has a "started_at": None key from creation - setdefault
    only fires when a key is *missing*, not when it's present-but-None."""
    if task.get("started_at") is None:
        task["started_at"] = time.time()

def flatten_youtube_entry_dicts(entries):
    """Recursively flattens yt-dlp's --flat-playlist entry tree (a channel
    URL nests as channel -> tabs (Videos/Shorts/...) -> videos) down to a
    flat list of the video entry dicts, in encounter order."""
    result = []
    for e in entries or []:
        if not e:
            continue
        if e.get("entries") is not None:
            result.extend(flatten_youtube_entry_dicts(e.get("entries")))
        elif e.get("id"):
            result.append(e)
    return result

def flatten_youtube_entries(entries):
    """Same traversal as flatten_youtube_entry_dicts, but just the IDs."""
    return [e["id"] for e in flatten_youtube_entry_dicts(entries)]

# yt-dlp's per-format intermediates: "<title> [<id>].f251.webm". Present only
# between download and merge, and only ever one stream.
_YTDLP_FORMAT_PART = re.compile(r"\.f\d+\.[A-Za-z0-9]+$")
# The same, as the library sees it: only after the "[<id>]" yt-dlp names its
# files with, so a file of anyone else's that happens to end ".f1.mp3" stays.
# "-sr" and similar suffixes are YouTube's own format-id variants (f399-sr).
_YTDLP_FRAGMENT_NAME = re.compile(r"\]\.f\d+(?:-[A-Za-z]+)?\.[A-Za-z0-9]+$")

# What a finished YouTube download can actually be. Used to keep a sidecar
# (.info.json, .description) from outranking the media just by being bigger.
_DOWNLOADED_MEDIA_EXTS = {
    ".webm", ".mkv", ".mp4", ".m4v", ".mov", ".avi", ".flv",
    ".m4a", ".mp3", ".opus", ".ogg", ".flac", ".wav", ".mka",
}

def find_downloaded_media_file(target_dir, video_id, listing=None):
    """Locates the media file yt-dlp just produced for a given video ID
    (naming template is "%(title)s [%(id)s].%(ext)s"), skipping temp files.

    Pass `listing` (a pre-read os.listdir of target_dir) when resolving many
    tracks from one folder, so a 200-track playlist doesn't re-read the same
    directory 200 times off a spinning disk."""
    if not video_id or (listing is None and not os.path.isdir(target_dir)):
        return None
    if listing is None:
        listing = os.listdir(target_dir)
    marker = f"[{video_id}]"
    candidates = [
        f for f in listing
        if marker in f and not f.endswith((".part", ".ytdl"))
        # "<title> [<id>].f251.webm" is one half of a download yt-dlp is
        # about to merge, not the download. It carries the same marker, it
        # is deleted the moment the merge finishes, and it holds only one
        # stream - so matching it means either racing the merger to a file
        # that is about to vanish, or extracting audio from a video-only
        # track. Both were observed while a playlist was still running.
        and not _YTDLP_FORMAT_PART.search(f)
    ]
    if not candidates:
        return None
    # Sorting by size alone hands the win to any sidecar bigger than the
    # media, and an .info.json easily is. Prefer things that are actually
    # playable; fall back to the old rule only if nothing looks like media.
    media = [f for f in candidates
             if os.path.splitext(f)[1].lower() in _DOWNLOADED_MEDIA_EXTS]
    candidates = media or candidates

    # Prefer the largest match in case of leftover thumbnail/info sidecars.
    # A file can still disappear between listing and stat while a download is
    # in flight, so a vanished candidate sorts last rather than raising.
    def _size(name):
        try:
            return os.path.getsize(os.path.join(target_dir, name))
        except OSError:
            return -1

    candidates.sort(key=_size, reverse=True)
    if _size(candidates[0]) < 0:
        return None
    return os.path.join(target_dir, candidates[0])

def clean_artist_name(name):
    """YouTube Music auto-generates "<Artist> - Topic" channels for
    non-official uploads - strip that suffix, nobody wants it in their
    folder name or artist tag."""
    name = name or "Unknown Artist"
    return re.sub(r'\s*-\s*Topic$', '', name, flags=re.IGNORECASE).strip() or name

def _music_extraction_cmd(track, target_dir, listing=None, keep_video=False):
    """Builds the extractor argv for one track, or None if its downloaded
    video isn't sitting in target_dir.

    A missing video is also how an already-converted track is recognised:
    the extractor deletes the source once the tagged audio is safely
    written, so "no video here" means either "not downloaded" or "already
    done" - both of which mean there is nothing to do."""
    video_path = find_downloaded_media_file(target_dir, track["id"], listing=listing)
    if not video_path:
        return None, None

    artist = clean_artist_name(track.get("artist"))
    # The song itself lives under its artist, once, however many playlists
    # hold it (user, 2026-09-20). A playlist still stays together under
    # <Owner>/<Playlist>/ - as a folder of hard links to those files, so it
    # reads and plays exactly as before while the audio is stored once.
    output_dir = os.path.join(DOWNLOADS_DIR, "youtube_music", sanitize_folder_name(artist))

    extractor_script = os.path.join(BASE_DIR, "youtube_music_extractor.py")
    cmd = [
        sys.executable, "-u", extractor_script, video_path, output_dir,
        track.get("title") or "Unknown Title", artist, track["id"] or ""
    ]
    if track.get("thumbnail"):
        cmd.append(track["thumbnail"])
    link_dir = playlist_dir_for(track)
    if link_dir:
        cmd += ["--link", link_dir]
    if keep_video:
        cmd.append("--keep-video")
    return cmd, output_dir


def playlist_dir_for(track):
    """<Owner>/<Playlist>/ for a track that came from one, else None: a single
    video has no playlist folder, only its artist folder."""
    if not track.get("album"):
        return None
    folder_owner = clean_artist_name(track.get("folder_owner") or track.get("artist"))
    return os.path.join(DOWNLOADS_DIR, "youtube_music", sanitize_folder_name(folder_owner),
                        sanitize_folder_name(track["album"]))


def link_stored_songs(tracks):
    """Puts the songs a playlist holds that are ALREADY stored into its folder,
    as hard links. Nothing is downloaded and nothing is stored twice.

    This is what a playlist sharing songs with an earlier one needs: yt-dlp's
    archive knows the video and skips it, so before this the second playlist
    simply had a hole where that song should be."""
    index = music_store.read_songs_index(DOWNLOADS_DIR)
    if not index:
        return 0
    linked = 0
    for track in tracks:
        link_dir = playlist_dir_for(track)
        rel = index.get(track.get("id") or "")
        if not link_dir or not rel:
            continue
        song = os.path.join(DOWNLOADS_DIR, rel)
        if not os.path.isfile(song):
            continue                      # the index remembers a file since gone
        try:
            _name, created = music_store.link_into(song, link_dir, track.get("id") or "")
            linked += 1 if created else 0
        except Exception as exc:
            print(f"[Warning] could not link {rel} into {link_dir}: {exc}")
    return linked

def _run_music_extractions(tracks, target_dir, keep_video=False):
    """Runs the extractor for each track one at a time. Sequential on
    purpose: a 90-track playlist would otherwise fork 90 concurrent
    ffmpeg + thumbnail-download processes and bury the machine. Still fully
    decoupled from the pipeline - this runs on its own thread and the
    download worker never waits on it."""
    log_path = os.path.join(BASE_DIR, "youtube_music_extractor.log")
    try:
        listing = os.listdir(target_dir)
    except OSError:
        return
    for track in tracks:
        cmd, output_dir = _music_extraction_cmd(track, target_dir, listing=listing, keep_video=keep_video)
        if not cmd:
            # Not an error worth shouting about: on a resume or a re-run this
            # is simply every track that was already converted.
            continue
        try:
            with open(log_path, "a", encoding="utf-8") as log_file:
                log_file.write(f"\n--- {time.strftime('%Y-%m-%d %H:%M:%S')} | {track.get('title')} ({track['id']}) ---\n")
                log_file.flush()
                subprocess.run(cmd, stdout=log_file, stderr=subprocess.STDOUT, start_new_session=True, close_fds=True)
        except Exception as e:
            print(f"[Warning] youtube_music extractor failed for {track['id']}: {str(e)}")

def merge_download_archive(run_archive, shared_archive):
    """Adds a dual run's own archive to the shared one (lines not already
    there) and removes it: a later plain download then skips these videos
    like any other it has fetched."""
    try:
        with open(run_archive, encoding="utf-8") as fh:
            new = [line.strip() for line in fh if line.strip()]
    except OSError:
        return
    try:
        with open(shared_archive, encoding="utf-8") as fh:
            have = {line.strip() for line in fh}
    except OSError:
        have = set()
    add = [line for line in dict.fromkeys(new) if line not in have]
    try:
        if add:
            with open(shared_archive, "a", encoding="utf-8") as fh:
                fh.write("".join(line + "\n" for line in add))
        os.remove(run_archive)
    except OSError as exc:
        print(f"[Warning] yt-dual: archive {run_archive} not merged: {exc}", flush=True)


def dual_complete_ids():
    """The video ids that have BOTH halves of a "yt-dual/" download already: a
    song in the song store and a video - one this pipeline kept under
    music_videos/, or any copy the music catalogue matched to the song
    (downloaded as a video before, the 4K120 copies...). An id missing
    either half is fetched again, past the shared download archive."""
    songs = {vid for vid, rel in music_store.read_songs_index(DOWNLOADS_DIR).items()
             if os.path.isfile(os.path.join(DOWNLOADS_DIR, rel))}
    videos = set()
    kept = os.path.join(DOWNLOADS_DIR, music_store.MUSIC_VIDEOS_NAME)
    for _dirpath, _dirs, files in os.walk(kept):
        for name in files:
            m = music_library.NAME_ID_RE.search(os.path.splitext(name)[0])
            if m and music_library.is_video(name):
                videos.add(m.group(1))
    catalog, _building = music_catalog()
    for s in (catalog or {}).get("songs", []):
        if s.get("video") and s.get("video_id"):
            videos.add(s["video_id"])
    return songs & videos


def maybe_spawn_youtube_music_extraction(url, info_data, target_dir, downloaded_ids=None, keep_video=False):
    """Turns just-downloaded YouTube Music video(s) into tagged,
    Jellyfin-friendly audio files under a separate youtube_music/ tree -
    decoupled from the main pipeline entirely (no queueing, no rate
    limiting, and server.py never waits on it).

    Runs after the downloader exits whether or not it exited cleanly: a
    cancelled or rate-limited playlist still leaves fully downloaded videos
    on disk, and those are exactly as convertible as the ones from a run
    that finished. Nothing here re-downloads anything.

    downloaded_ids is unused now (kept for callers): every playlist entry is
    considered, and the ones already converted cost a dict lookup because
    their source video is gone.

    keep_video: a "yt-dual/" download, from either kind of YouTube link - the
    extractor keeps each video (music_videos/) instead of deleting it."""
    if "music.youtube.com" not in url.lower() and not keep_video:
        return

    entries = flatten_youtube_entry_dicts(info_data.get("entries"))
    if not entries:
        tracks = [{
            "id": info_data.get("id"),
            "title": info_data.get("title"),
            "artist": info_data.get("uploader") or info_data.get("channel"),
            "thumbnail": info_data.get("thumbnail") or "",
            "album": None,
        }]
    else:
        # Per-track metadata (title/artist/thumbnail) comes from the flat
        # playlist entries, but the playlist is kept together on disk under
        # the owner's name - the same <Owner>/<Playlist>/ shape the videos
        # were downloaded into.
        #
        # Every entry is offered, not just the ones fetched this run. The
        # run that downloads a track and the run that converts it are no
        # longer required to be the same one - which is what was broken:
        # a cancelled run downloaded 51 videos and converted none, and no
        # later run would ever look at them again, because the archive
        # filter had already removed them from this run's id list.
        album = info_data.get("title") or info_data.get("playlist_title")
        owner = (info_data.get("uploader") or info_data.get("channel")
                 or info_data.get("uploader_id") or "Unknown_Channel")
        tracks = [{
            "id": entry.get("id"),
            "title": entry.get("title"),
            "artist": entry.get("uploader") or entry.get("channel"),
            "folder_owner": owner,
            "thumbnail": entry.get("thumbnail") or "",
            "album": album,
        } for entry in entries]

    if not tracks:
        return
    if entries:
        record_music_source(url, tracks)

    def work():
        # First the songs this run fetched, then the ones a previous run
        # already stored: a playlist ends up holding all of its songs either
        # way, each of them a name for one stored file.
        _run_music_extractions(tracks, target_dir, keep_video)
        linked = link_stored_songs(tracks)
        if linked:
            print(f"[youtube-music] linked {linked} song(s) already stored into their playlist")

    threading.Thread(target=work, daemon=True).start()

def classify_url(url):
    url_lower = url.lower()
    if any(k in url_lower for k in ["youtube.com", "youtu.be"]):
        return "youtube"
    # Ahead of twitter: "x.com" is also the end of a profile like instagram.com/max.com.
    elif "instagram.com" in url_lower or "instagr.am" in url_lower:
        return "instagram"
    elif any(k in url_lower for k in ["twitter.com", "x.com"]) or url.startswith("@"):
        return "twitter"
    elif "pixiv" in url_lower:
        return "pixiv"
    elif "danbooru" in url_lower:
        return "danbooru"
    elif "civitai.com" in url_lower:
        return "civitai"
    elif "patreon.com" in url_lower:
        return "patreon"
    elif "bsky.app" in url_lower or "bluesky" in url_lower:
        return "bluesky"
    elif "deviantart.com" in url_lower:
        return "deviantart"
    elif "discord.com" in url_lower or "discordapp.com" in url_lower:
        return "discord"
    elif "bilibili.com" in url_lower or "b23.tv" in url_lower:
        return "bilibili"
    return "unknown"


# Bilibili links gallery-dl handles: the pictures of an article / opus post, a
# user's article list and their favourited opus posts. Everything else on
# bilibili.com (videos, a user's videos, collections, favourites, bangumi)
# is yt-dlp's.
BILIBILI_GALLERY_RE = re.compile(r"/opus/|/read/cv|space\.bilibili\.com/\d+/article|fid=opus", re.I)


def resolve_bilibili_short_link(url):
    """b23.tv is Bilibili's link shortener and has no extractor of its own in
    yt-dlp; the page it redirects to does. Returns the link unchanged when the
    redirect cannot be followed."""
    link = url if re.match(r"^[a-z]+://", url, re.I) else "https://" + url
    try:
        parsed = urllib.parse.urlsplit(link)
    except ValueError:
        return url
    # Hostname must BE the shortener: a substring test let any address that merely contained "b23.tv"
    # make the server fetch it (internal hosts included).
    if parsed.scheme not in ("http", "https") or (parsed.hostname or "").lower() != "b23.tv":
        return url
    try:
        req = urllib.request.Request(link, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.geturl() or url
    except Exception:
        return url

def parse_progress_metrics(category, clean_line):
    stats = pipeline_stats[category]
    speed_match = re.search(r'(\d+(?:\.\d+)?)\s*([KMGT]?i?B)/s', clean_line, re.IGNORECASE)
    if speed_match:
        val, unit = float(speed_match.group(1)), speed_match.group(2).upper()
        mult = 1.0
        if "K" in unit: mult = 1/1024
        elif "G" in unit: mult = 1024
        stats["rate_mbps"] = val * mult

    if any(k in clean_line.lower() for k in ["[download] destination:", "downloading item", "writing"]):
        stats["items_downloaded"] += 1
        item_download_events.append(time.time())

# yt-dlp's refusals of a single video, as it prints them: "ERROR: [youtube] <id>:
# <why>". A channel or playlist run carries on past each one and exits 1 at the
# end, which on its own made every run with one age-restricted video read
# "Failed" - and said nothing about why.
YOUTUBE_ERROR_LINE = re.compile(r"^ERROR: (?:\[[^\]]+\] )?(?:[A-Za-z0-9_-]{11}: )?(.*)")
YOUTUBE_SKIP_REASONS = (
    ("age_restricted", re.compile(r"confirm your age|inappropriate for some users", re.I)),
    ("members_only", re.compile(r"members.only|join this channel|channel.s members", re.I)),
    ("private", re.compile(r"private video|video is private", re.I)),
    ("geo_blocked", re.compile(r"available in your country", re.I)),
    ("not_released", re.compile(r"premieres in|live event will begin", re.I)),
    ("unavailable", re.compile(r"video unavailable|this video is (?:no longer|not) available|"
                               r"has been removed|been terminated|copyright", re.I)),
)
YOUTUBE_SKIP_LABELS = {"age_restricted": "age-restricted", "members_only": "members-only",
                       "private": "private", "geo_blocked": "blocked in this country",
                       "not_released": "not released yet", "unavailable": "unavailable",
                       "error": "other error"}


def youtube_skip_reason(line):
    """Why yt-dlp gave up, from one output line: a key of YOUTUBE_SKIP_LABELS -
    "error" for any other ERROR (a bot check, HTTP 403, a missing format) - or
    None when the line is not an error at all."""
    m = YOUTUBE_ERROR_LINE.match(line)
    if not m:
        return None
    for key, pattern in YOUTUBE_SKIP_REASONS:
        if pattern.search(m.group(1)):
            return key
    return "error"


def youtube_skip_summary(skips, cookies_signed_out=False):
    """ "11 videos skipped: 9 age-restricted, 2 other errors", or "". """
    total = sum(skips.values())
    if not total:
        return ""
    parts = [f"{n} {YOUTUBE_SKIP_LABELS.get(key, key)}{'s' if key == 'error' and n != 1 else ''}"
             for key, n in sorted(skips.items(), key=lambda kv: (-kv[1], kv[0]))]
    text = f"{total} video{'' if total == 1 else 's'} skipped: {', '.join(parts)}"
    if cookies_signed_out and (skips.get("age_restricted") or skips.get("members_only")):
        text += " (YouTube cookies are signed out)"
    return text


def download_outcome(category, returncode, skips, cookies_signed_out=False):
    """(status, progress) for a finished downloader. yt-dlp exits 1 when any
    video failed; when every failure was a known refusal of one video, the run
    did all it could and is Completed, with the skips named. Anything
    unexplained still fails."""
    summary = youtube_skip_summary(skips, cookies_signed_out)
    tail = f" - {summary}" if summary else ""
    if returncode == 0 or (category == "youtube" and returncode == 1
                           and skips and not skips.get("error")):
        return "Completed", "100% Finished" + tail
    return "Failed", f"Exited code {returncode}" + tail


YOUTUBE_AUTH_COOKIES = {"SAPISID", "__Secure-1PAPISID", "__Secure-3PAPISID"}


def youtube_cookie_status(path=None):
    """Whether yt-dlp will treat the YouTube cookie file as signed in, by its own
    rule (extractor/youtube/_base.py, _has_auth_cookies): LOGIN_INFO plus one of
    SAPISID / __Secure-1PAPISID / __Secure-3PAPISID. YouTube clears LOGIN_INFO
    when it ends an exported session and leaves the rest, and yt-dlp then runs
    signed out without a warning. Reads cookie names and expiry, never values."""
    path = path or os.path.join(BASE_DIR, "cookies_youtube.txt")
    out = {"present": False, "signed_in": False, "has_login_info": False,
           "cookies": 0, "modified_at": None, "path": path}
    names, now = set(), time.time()
    try:
        modified = os.stat(path).st_mtime
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if line.startswith("#HttpOnly_"):
                    line = line[len("#HttpOnly_"):]
                elif line.startswith("#"):
                    continue
                fields = line.rstrip("\r\n").split("\t")
                if len(fields) < 7 or not fields[0].lstrip(".").endswith("youtube.com"):
                    continue
                try:
                    expires = int(float(fields[4] or 0))
                except ValueError:
                    expires = 0
                if not expires or expires >= now:
                    names.add(fields[5])
    except OSError:
        return out
    out.update(present=True, modified_at=modified, cookies=len(names),
               has_login_info="LOGIN_INFO" in names,
               signed_in="LOGIN_INFO" in names and bool(names & YOUTUBE_AUTH_COOKIES))
    return out


def execute_download_task(task_id, url, category):
    dest_path = os.path.join(DOWNLOADS_DIR, category)
    # Overridden below for categories (youtube, civitai) where the exact
    # download folder is computed up front. Everything else (gallery_dl,
    # PixivUtil2) picks its own subfolder at runtime via its own naming
    # template, so the category root is the best we can point the media
    # browser at.
    target_dir = dest_path
    cookies_youtube_path = os.path.join(BASE_DIR, "cookies_youtube.txt")
    cookies_twitter_path = os.path.join(BASE_DIR, "cookies_twitter.txt")
    cookies_patreon_path = os.path.join(BASE_DIR, "cookies_patreon.txt")
    cookies_deviantart_path = os.path.join(BASE_DIR, "cookies_deviantart.txt")
    # A signed-in session gets 1080p; 4K, 1080p high-bitrate/60 fps, HDR and
    # Dolby need a premium (大会员) account. Without the file: 480p at most.
    cookies_bilibili_path = os.path.join(BASE_DIR, "cookies_bilibili.txt")
    cookies_instagram_path = os.path.join(BASE_DIR, "cookies_instagram.txt")
    pixiv_script_path = os.path.join(BASE_DIR, "PixivUtil2", "PixivUtil2.py")
    
    if category == "youtube":
        # "yt-dual/<link>": the prefix stays in the task (what the dashboard
        # shows and re-runs); from here on only the link itself is used.
        url, dual = split_dual_link(url)
        cookie_state = youtube_cookie_status(cookies_youtube_path)
        signed_out = cookie_state["present"] and not cookie_state["signed_in"]
        with task_lock:
            if task_id in active_tasks:
                active_tasks[task_id]["status"] = "Running"
                mark_task_started(active_tasks[task_id])
                active_tasks[task_id]["progress"] = "Extracting video metadata..."
                active_tasks[task_id]["cookies_signed_out"] = signed_out
                active_tasks[task_id].pop("skipped", None)
                if signed_out:
                    # Said up front, because yt-dlp itself never says it.
                    active_tasks[task_id]["logs"].append(
                        "[media-dl] YouTube cookies are signed out (LOGIN_INFO is missing) - "
                        "age-restricted and members-only videos will be skipped. "
                        "Re-export them: Settings > Download behaviour.")

        # Step 1: Extract Metadata (No Download)
        info_cmd = [
            sys.executable, "-u", "-m", "yt_dlp",
            "--dump-single-json",
            # Playlist context on a video link must not expand the download.
            "--no-playlist",
            "--flat-playlist",
            "--no-warnings"
        ]
        if os.path.exists(cookies_youtube_path):
            info_cmd.extend(["--cookies", cookies_youtube_path])
        info_cmd.append(url)

        try:
            info_result = subprocess.run(info_cmd, capture_output=True, text=True, check=True)
            info_data = json.loads(info_result.stdout)
        except Exception as e:
            # A CalledProcessError's own text is just the command line; what
            # went wrong is yt-dlp's last ERROR line.
            errors = [l.strip() for l in (getattr(e, "stderr", None) or "").splitlines()
                      if l.startswith("ERROR:")]
            detail = errors[-1][len("ERROR:"):].strip()[:240] if errors else str(e)
            reason = youtube_skip_reason(errors[-1]) if errors else None
            with task_lock:
                if task_id in active_tasks:
                    task = active_tasks[task_id]
                    task["status"] = "Failed"
                    task["completed_at"] = time.time()
                    progress = f"Failed to extract metadata: {detail}"
                    if reason and reason != "error":
                        task["skipped"] = {reason: 1}
                        if reason in ("age_restricted", "members_only") and task.get("cookies_signed_out"):
                            progress += " (YouTube cookies are signed out)"
                    task["progress"] = progress
                    task["logs"].extend(errors[-5:])
            return

        # Step 2: Determine Folder Structure in Python
        _type = info_data.get("_type", "video")
        
        is_playlist = _type == "playlist" or "entries" in info_data
        # A channel page and a real playlist BOTH come back as
        # _type == "playlist" from the same "youtube:tab" extractor, so the
        # extractor name can't tell them apart. What does: for a channel the
        # top-level id IS the channel id (UC...), while a playlist's id is
        # its list id (PL... / OLAK5uy_...) and differs from channel_id.
        top_id = info_data.get("id")
        is_channel = bool(top_id) and top_id == info_data.get("channel_id")

        if is_channel:
            channel_name = info_data.get("uploader") or info_data.get("channel") or info_data.get("uploader_id") or "Unknown_Channel"
            folder_parts = [sanitize_folder_name(channel_name)]
        elif is_playlist:
            # Playlists nest as <Owner>/<Playlist>/ (replacing the older flat
            # "Owner____Playlist"), so everything by one uploader groups
            # under a single folder.
            uploader = info_data.get("uploader") or info_data.get("channel") or info_data.get("uploader_id") or "Unknown_Channel"
            title = info_data.get("title") or info_data.get("playlist_title") or "Unknown_Playlist"
            folder_parts = [sanitize_folder_name(uploader), sanitize_folder_name(title)]
        else:
            uploader = info_data.get("uploader") or info_data.get("channel") or info_data.get("uploader_id") or "Unknown_Channel"
            folder_parts = [sanitize_folder_name(uploader)]

        target_dir = os.path.join(dest_path, *folder_parts)
        youtube_archive_path = os.path.join(dest_path, ".yt_dlp_archive.txt")
        # A dual run cannot use the shared archive: it lists every video ever
        # downloaded, so the videos of songs already kept as music - exactly
        # what such a run is for - would be skipped. Its own archive still
        # lets a requeued run resume, and is merged into the shared one when
        # the run ends, so a later plain download skips what this one got.
        run_archive_path = (os.path.join(dest_path, f".yt_dlp_archive.dual-{task_id}.txt")
                            if dual else youtube_archive_path)

        # For a playlist/channel resume, filter out video IDs already
        # recorded in the archive *before* invoking yt-dlp at all, and pass
        # only the missing videos as explicit URLs. This is what actually
        # avoids per-video extraction requests (webpage + player API calls)
        # for already-downloaded videos on a resume - --download-archive
        # alone still lets yt-dlp fully extract every playlist entry before
        # it notices the file already exists, which burns API calls for
        # nothing and works against the whole point of the rate limiter.
        download_urls = [url]
        # Which video IDs this run actually fetches - used afterward to
        # convert only those to audio for a YouTube Music playlist.
        downloaded_ids = None
        if is_playlist:
            already_downloaded_ids = set()
            if os.path.exists(youtube_archive_path):
                try:
                    with open(youtube_archive_path, "r", encoding="utf-8") as f:
                        for line in f:
                            parts = line.strip().split()
                            if len(parts) == 2 and parts[0] == "youtube":
                                already_downloaded_ids.add(parts[1])
                except Exception:
                    pass

            if dual:
                # Done means a song AND a video, not "downloaded once".
                already_downloaded_ids = dual_complete_ids()

            entry_ids = flatten_youtube_entries(info_data.get("entries"))
            if entry_ids:
                remaining_ids = [vid for vid in entry_ids if vid not in already_downloaded_ids]
                if not remaining_ids:
                    with task_lock:
                        if task_id in active_tasks:
                            active_tasks[task_id]["status"] = "Completed"
                            active_tasks[task_id]["completed_at"] = time.time()
                            active_tasks[task_id]["progress"] = (
                                "Every song is already here as music and as video" if dual
                                else "All videos already downloaded (skipped, no API calls made)")
                            active_tasks[task_id]["media_path"] = os.path.relpath(target_dir, DOWNLOADS_DIR).replace(os.sep, "/")
                    # Downloaded is not the same as converted. Re-submitting
                    # the playlist is the natural way to ask for a stalled
                    # one to be finished, so let this path pick up any
                    # videos still waiting for their audio.
                    maybe_spawn_youtube_music_extraction(url, info_data, target_dir, None, keep_video=dual)
                    return
                download_urls = [f"https://www.youtube.com/watch?v={vid}" for vid in remaining_ids]
                downloaded_ids = remaining_ids

        # Step 3: Run full download directly inside computed directory.
        # --continue (resume partial files) and a shared --download-archive
        # are what let this pick back up at the interrupted video instead of
        # restarting from scratch whenever a task gets requeued (server
        # restart recovery) or aborted and retried.
        cmd = [
            sys.executable, "-u", "-m", "yt_dlp", "--remote-components", "ejs:github",
            "--no-playlist",
            "--newline",
            "--continue",
            "--download-archive", run_archive_path,
            # The id is in the filename, but a filename is the one piece of
            # a file anyone can change. --embed-metadata writes the source
            # URL into the container's own tags (yt-dlp's PURL field), so a
            # renamed or re-filed video still knows what it is - the same
            # record the extracted audio carries. Costs one stream-copy
            # remux per video, which is nothing next to fetching it.
            "--embed-metadata", "--no-write-comments",
            "-P", target_dir,
            "-o", "%(title)s [%(id)s].%(ext)s"
        ]
        if is_playlist:
            rate_limit_guard = os.path.join(BASE_DIR, "youtube_rate_limit_guard.py")
            # %(filepath)q lets the guard tell a genuinely fresh download
            # apart from an entry yt-dlp skipped because it (or the archive
            # entry) already existed on disk from a previous run - only the
            # former should count toward the rate limit / trigger a sleep.
            exec_cmd = f"{shlex.quote(sys.executable)} {shlex.quote(rate_limit_guard)} %(filepath)q"
            cmd.extend(["--exec", exec_cmd])
            if dual or "music.youtube.com" in url.lower():
                # Convert each track as it lands. The end-of-run extraction
                # below still exists as a backstop, but on a playlist this
                # size it would be the FIRST audio to appear after eleven
                # hours of downloading - and nothing at all if the run were
                # stopped before then.
                music_hook = os.path.join(BASE_DIR, "youtube_music_hook.py")
                cmd.extend(["--exec",
                            f"{shlex.quote(sys.executable)} {shlex.quote(music_hook)} "
                            f"%(filepath)q %(id)q %(title)q %(uploader)q %(thumbnail)q"
                            + (" --keep-video" if dual else "")])
        if os.path.exists(cookies_youtube_path):
            cmd.extend(["--cookies", cookies_youtube_path])
        cmd.extend(download_urls)

    elif category == "twitter":
        target_url = url
        if target_url.startswith("@"):
            handle = target_url.lstrip("@").strip()
            target_url = f"https://x.com/{handle}/media"

        cmd = [
            sys.executable, "-u", "-m", "gallery_dl",
            "-o", f"base-directory={dest_path}",
            "-o", 'directory=["@{author[name]}"]'
        ]
        if os.path.exists(cookies_twitter_path):
            cmd.extend(["--cookies", cookies_twitter_path])
        cmd.append(target_url)

    elif category == "pixiv":
        artist_match = re.search(r'pixiv\.net/(?:[a-zA-Z]+/)?users/(\d+)', url)
        artwork_match = re.search(r'pixiv\.net/(?:[a-zA-Z]+/)?artworks/(\d+)', url)

        pixiv_python_executable = os.path.join(BASE_DIR, "PixivUtil2", "pixiv_venv", "bin", "python")

        if artist_match:
            artist_id = artist_match.group(1)
            cmd = [pixiv_python_executable, "-u", pixiv_script_path, "-s", "1", artist_id, "-x"]
        elif artwork_match:
            artwork_id = artwork_match.group(1)
            cmd = [pixiv_python_executable, "-u", pixiv_script_path, "-s", "2", artwork_id, "-x"]
        else:
            with task_lock:
                if task_id in active_tasks:
                    active_tasks[task_id]["status"] = "Failed"
                    active_tasks[task_id]["completed_at"] = time.time()
                    active_tasks[task_id]["progress"] = "Unsupported or invalid Pixiv URL format."
            return

    elif category == "danbooru":
        cmd = [
            sys.executable, "-u", "-m", "gallery_dl",
            "-o", f"base-directory={dest_path}",
            "-o", 'directory=["{search_tags}"]',
            url
        ]

    elif category == "bluesky":
        cmd = [
            sys.executable, "-u", "-m", "gallery_dl",
            "-o", f"base-directory={dest_path}",
            "-o", 'directory=["@{author[handle]}"]',
            url
        ]

    elif category == "patreon":
        cmd = [
            sys.executable, "-u", "-m", "gallery_dl",
            "-o", f"base-directory={dest_path}",
            "-o", 'directory=["{creator}"]'
        ]
        if os.path.exists(cookies_patreon_path):
            cmd.extend(["--cookies", cookies_patreon_path])
        cmd.append(url)

    elif category == "instagram":
        # Instagram sends everyone without a session to its login page.
        if not os.path.exists(cookies_instagram_path):
            with task_lock:
                if task_id in active_tasks:
                    active_tasks[task_id]["status"] = "Failed"
                    active_tasks[task_id]["completed_at"] = time.time()
                    active_tasks[task_id]["progress"] = "Instagram needs a signed-in cookie file (cookies_instagram.txt)."
            return
        # Post id first: the leading digits are what image_dedup.py groups a post
        # by and what orders posts; the shortcode is what the post's URL is made of.
        cmd = [
            sys.executable, "-u", "-m", "gallery_dl",
            "-o", f"base-directory={dest_path}",
            "-o", 'directory=["@{username}"]',
            "-o", "filename={post_id}_{num}_{post_shortcode}.{extension}",
            "--cookies", cookies_instagram_path,
            url
        ]
    elif category == "deviantart":
        cmd = [
            sys.executable, "-u", "-m", "gallery_dl",
            "-o", f"base-directory={dest_path}",
            "-o", 'directory=["{author[username]}"]'
        ]
        if os.path.exists(cookies_deviantart_path):
            cmd.extend(["--cookies", cookies_deviantart_path])
        cmd.append(url)

    elif category == "civitai":
        info_cmd = [
            sys.executable, "-u", "-m", "gallery_dl",
            "-j", url
        ]
        
        uploader = "Unknown_User"
        model_name = "Unknown_Model"

        try:
            info_result = subprocess.run(info_cmd, capture_output=True, text=True, check=True)
            raw_data = json.loads(info_result.stdout)
            
            # gallery_dl gibt eine verschachtelte Liste aus: [[2, meta_dict], [3, url, meta_dict]]
            meta = {}
            if isinstance(raw_data, list):
                for entry in raw_data:
                    if isinstance(entry, list):
                        for item in entry:
                            if isinstance(item, dict) and "category" in item:
                                meta = item
                                break
                    if meta:
                        break

            if meta:
                # 1. Uploader auslesen
                user_obj = meta.get("user")
                if isinstance(user_obj, dict):
                    uploader = user_obj.get("username") or uploader
                elif isinstance(user_obj, str):
                    uploader = user_obj

                if uploader == "Unknown_User":
                    uploader = meta.get("username") or meta.get("author") or uploader

                # 2. Modellname / Post / ID auslesen
                model_obj = meta.get("model")
                if isinstance(model_obj, dict):
                    model_name = model_obj.get("name") or model_name

                if model_name == "Unknown_Model":
                    file_obj = meta.get("file") if isinstance(meta.get("file"), dict) else {}
                    post_id = file_obj.get("postId")
                    
                    if meta.get("modelName"):
                        model_name = meta.get("modelName")
                    elif meta.get("title"):
                        model_name = meta.get("title")
                    elif post_id:
                        model_name = f"Post_{post_id}"
                    elif meta.get("id"):
                        model_name = f"Image_{meta.get('id')}"

        except Exception:
            pass

        folder_name = sanitize_folder_name(f"{uploader}____{model_name}")
        target_dir = os.path.join(dest_path, folder_name)

        cmd = [
            sys.executable, "-u", "-m", "gallery_dl",
            "-o", f"base-directory={target_dir}",
            "-o", 'directory=[]',
            url
        ]

    elif category == "bilibili":
        link = resolve_bilibili_short_link(url)
        if BILIBILI_GALLERY_RE.search(link):
            # gallery-dl's default directory starts with the category, so
            # with downloads/ as the base this lands in downloads/bilibili/.
            cmd = [sys.executable, "-u", "-m", "gallery_dl",
                   "-o", f"base-directory={DOWNLOADS_DIR}", link]
            if os.path.exists(cookies_bilibili_path):
                cmd.extend(["--cookies", cookies_bilibili_path])
        else:
            # Same shape as a YouTube video download: one folder per uploader,
            # the id in the name, and an archive so a re-run or a user's video
            # list only fetches what is missing.
            cmd = [
                sys.executable, "-u", "-m", "yt_dlp",
                "--newline", "--continue",
                "--download-archive", os.path.join(dest_path, ".yt_dlp_archive.txt"),
                "--embed-metadata", "--no-write-comments",
                "-P", dest_path,
                "-o", "%(uploader,uploader_id|Unknown_Uploader)s/%(title)s [%(id)s].%(ext)s",
            ]
            if os.path.exists(cookies_bilibili_path):
                cmd.extend(["--cookies", cookies_bilibili_path])
            cmd.append(link)

    else:
        cmd = [sys.executable, "-u", "-m", "gallery_dl", "--directory", dest_path, url]

    media_path = os.path.relpath(target_dir, DOWNLOADS_DIR).replace(os.sep, "/")

    with task_lock:
        if task_id in active_tasks:
            active_tasks[task_id]["status"] = "Running"
            mark_task_started(active_tasks[task_id])
            active_tasks[task_id]["progress"] = "Initiating process..."
            active_tasks[task_id]["media_path"] = media_path

    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"

    try:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1, env=env, cwd=dest_path
        )
        with task_lock:
            running_processes[task_id] = proc

        stall_detector = RateLimitStallDetector(dest_path)
        stalled_on_rate_limit = False
        skips = {}

        for line in iter(proc.stdout.readline, ''):
            clean_line = line.strip()
            if not clean_line:
                continue

            if stall_detector.observe(clean_line):
                stalled_on_rate_limit = True
            skip = youtube_skip_reason(clean_line) if category == "youtube" else None

            with task_lock:
                if task_id not in active_tasks:
                    break
                active_tasks[task_id]["logs"].append(clean_line)
                if skip:
                    # Counted from the stream: the log keeps only its last
                    # MAX_LOG_LINES lines.
                    skips[skip] = skips.get(skip, 0) + 1
                    active_tasks[task_id]["skipped"] = dict(skips)
                if "%" in clean_line:
                    match = re.search(r'(\d{1,3}\.\d%)', clean_line)
                    if match:
                        active_tasks[task_id]["progress"] = f"Downloading: {match.group(1)}"
                else:
                    active_tasks[task_id]["progress"] = clean_line[:60]
                parse_progress_metrics(category, clean_line)

            if stalled_on_rate_limit:
                print(f"[Warning] {category} task {task_id} rate-limited with no progress "
                      f"between waits - stopping it: {url}")
                with task_lock:
                    if task_id in active_tasks:
                        active_tasks[task_id]["status"] = "Failed"
                        active_tasks[task_id]["completed_at"] = time.time()
                        # Nothing is lost by stopping: already-downloaded
                        # files are skipped on a re-run, so retrying later
                        # picks up exactly where this left off.
                        active_tasks[task_id]["progress"] = (
                            "Rate limited with no progress - stopped, retry later")
                proc.terminate()
                break

        proc.wait()

        task_started_at, finished_ok = None, False
        with task_lock:
            running_processes.pop(task_id, None)
            # A stall stop has already written its own status and message;
            # don't overwrite it with the kill's exit code. Same for a
            # shutdown, which has already re-queued this task to resume on
            # the next start - "Failed, exited code -9" would undo that.
            if (task_id in active_tasks and not stalled_on_rate_limit
                    and not SHUTTING_DOWN.is_set()
                    and active_tasks[task_id]["status"] != "Terminated"):
                task_started_at = active_tasks[task_id].get("started_at") or active_tasks[task_id].get("created_at")
                status, progress = download_outcome(category, proc.returncode, skips,
                                                    active_tasks[task_id].get("cookies_signed_out"))
                active_tasks[task_id]["status"] = status
                active_tasks[task_id]["completed_at"] = time.time()
                active_tasks[task_id]["progress"] = progress
                finished_ok = status == "Completed"
                summary = youtube_skip_summary(skips, active_tasks[task_id].get("cookies_signed_out"))
                if summary:
                    # Closes the log too - the ERROR lines are what someone
                    # opening "View Logs" sees.
                    active_tasks[task_id]["logs"].append(f"[media-dl] {summary}")

        if finished_ok and task_started_at:
            try:
                new_paths = scan_new_media_since(target_dir, task_started_at)
                if new_paths:
                    recent_media_store.record_recent_media(BASE_DIR, new_paths)
                    # Feeds "Downloaded (24h)" without anyone having to walk
                    # the library for it. These inodes were just read by
                    # scan_new_media_since, so the stats come from cache.
                    produced = 0
                    for item in new_paths:
                        full = resolve_media_path(item.get("root") or DEFAULT_ROOT_ID,
                                                  item.get("path") or "")
                        try:
                            produced += os.stat(full).st_size if full else 0
                        except OSError:
                            pass
                    record_download_volume(produced)
                    # A finished download is the main way the library grows,
                    # and until now nothing told the search index about it -
                    # only the short TTL did, by re-walking every root. Saying
                    # so explicitly is what lets that TTL be long.
                    invalidate_media_caches()
                    # Posts that are one picture cut into pieces, joined in
                    # the background (post_stitch.py).
                    # Then, once the pieces are joined (a joined picture is a
                    # picture too), the same picture twice is looked for
                    # (image_dedup.py).
                    dedup_next = ((lambda: image_dedup_after_download(new_paths))
                                  if image_dedup_enabled() else None)
                    if category == "twitter" and twitter_stitch_enabled():
                        stitch_after_download(new_paths, then=dedup_next)
                    elif dedup_next:
                        dedup_next()
                    # User, 2026-10-18: repeating intros/outros are looked for
                    # in the folders that just got a new video (video_trim.py).
                    _video_trim.enqueue_paths(new_paths)
            except Exception as e:
                print(f"[Warning] Failed to update recent-media list for task {task_id}: {str(e)}")

        # Not gated on the exit code: a cancelled or rate-limited YouTube
        # Music run leaves finished videos behind, and leaving them
        # unconverted in youtube/ is how a stopped playlist used to end up
        # with nothing in youtube_music/ at all - permanently, since a retry
        # skips them via the download archive.
        if category == "youtube":
            if dual:
                merge_download_archive(run_archive_path, youtube_archive_path)
            maybe_spawn_youtube_music_extraction(url, info_data, target_dir, downloaded_ids, keep_video=dual)

    except Exception as e:
        with task_lock:
            running_processes.pop(task_id, None)
            if task_id in active_tasks:
                active_tasks[task_id]["status"] = "Failed"
                active_tasks[task_id]["completed_at"] = time.time()
                active_tasks[task_id]["progress"] = str(e)

# -------------------------------------------------------------------
# Queues & State Persistence
# -------------------------------------------------------------------

def get_completion_time(task):
    return task.get("completed_at") or task.get("created_at") or 0

def cleanup_completed_tasks():
    """Löscht alte Tasks. Muss innerhalb eines task_lock-Blocks aufgerufen werden!"""
    now = time.time()
    completed_tasks = [
        t for t in active_tasks.values() 
        if t.get("status") in ("Completed", "Terminated", "Failed")
    ]
    completed_tasks.sort(key=get_completion_time)
    
    to_delete = set()
    for task in completed_tasks:
        task_time = get_completion_time(task)
        if task_time == 0 or (now - task_time > MAX_COMPLETED_AGE_SECONDS):
            to_delete.add(task["id"])

    remaining = [t for t in completed_tasks if t["id"] not in to_delete]
    if len(remaining) > MAX_COMPLETED_TASKS:
        overflow_count = len(remaining) - MAX_COMPLETED_TASKS
        for task in remaining[:overflow_count]:
            to_delete.add(task["id"])

    for task_id in to_delete:
        del active_tasks[task_id]

def save_state():
    """Bereinigt und speichert den Zustand atomar ab."""
    with task_lock:
        cleanup_completed_tasks()
        serializable_data = []
        for task in active_tasks.values():
            task_copy = task.copy()
            if isinstance(task_copy.get("logs"), deque):
                task_copy["logs"] = list(task_copy["logs"])
            serializable_data.append(task_copy)

    temp_file = TASKS_STATE_FILE + ".tmp"
    try:
        with open(temp_file, "w", encoding="utf-8") as f:
            json.dump(serializable_data, f, indent=2, ensure_ascii=False)
        os.replace(temp_file, TASKS_STATE_FILE)
    except Exception as e:
        print(f"[Error] Fehlschlag beim Speichern von {TASKS_STATE_FILE}: {str(e)}")

def load_state():
    """Stellt gespeicherte Tasks wieder her."""
    if not os.path.exists(TASKS_STATE_FILE):
        return

    try:
        with open(TASKS_STATE_FILE, "r", encoding="utf-8") as f:
            tasks_list = json.load(f)
    except Exception as e:
        print(f"[Warning] Failed to load {TASKS_STATE_FILE}: {str(e)}")
        return

    with task_lock:
        for task in tasks_list:
            task_id = task.get("id")
            category = task.get("category")
            status = task.get("status")

            if not task_id or not category:
                continue

            if "logs" in task and isinstance(task["logs"], list):
                task["logs"] = deque(task["logs"], maxlen=MAX_LOG_LINES)
            else:
                task["logs"] = deque(maxlen=MAX_LOG_LINES)

            active_tasks[task_id] = task
            if status in ("Queued", "Running"):
                task["status"] = "Queued"
                task["progress"] = "Queued (Recovered)"
                if category in pipelines:
                    pipelines[category].put(task_id)

# -------------------------------------------------------------------
# Pipeline Usage Tracking (persisted, lock-enforced access)
# -------------------------------------------------------------------

def _read_usage_unlocked():
    if not os.path.exists(USAGE_FILE):
        return {}
    with open(USAGE_FILE, "r", encoding="utf-8") as f:
        return json.load(f)

def _write_usage_unlocked(data):
    temp_file = USAGE_FILE + ".tmp"
    with open(temp_file, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(temp_file, USAGE_FILE)

def _prune_usage(data):
    """Entfernt Tageseintraege, die aelter als USAGE_RETENTION_DAYS sind. Reine Funktion."""
    cutoff = date.today() - timedelta(days=USAGE_RETENTION_DAYS - 1)
    pruned = {}
    for cat, days in data.items():
        kept_days = {
            day_str: count for day_str, count in days.items()
            if _safe_parse_date(day_str) and _safe_parse_date(day_str) >= cutoff
        }
        if kept_days:
            pruned[cat] = kept_days
    return pruned

def _safe_parse_date(day_str):
    try:
        return date.fromisoformat(day_str)
    except (ValueError, TypeError):
        return None

def record_pipeline_usage(category):
    """Einziger Weg, die Nutzung einer Pipeline zu erhoehen."""
    today_str = date.today().isoformat()
    with usage_lock:
        try:
            data = _read_usage_unlocked()
        except Exception as e:
            print(f"[Warning] Failed to read {USAGE_FILE}: {str(e)}")
            data = {}
        data.setdefault(category, {})
        data[category][today_str] = data[category].get(today_str, 0) + 1
        data = _prune_usage(data)
        try:
            _write_usage_unlocked(data)
        except Exception as e:
            print(f"[Warning] Failed to write {USAGE_FILE}: {str(e)}")

def get_weekly_usage():
    """Einziger Weg, die Nutzung der letzten USAGE_RETENTION_DAYS Tage zu lesen."""
    with usage_lock:
        try:
            data = _read_usage_unlocked()
        except Exception as e:
            print(f"[Warning] Failed to read {USAGE_FILE}: {str(e)}")
            data = {}
        pruned = _prune_usage(data)
        if pruned != data:
            try:
                _write_usage_unlocked(pruned)
            except Exception as e:
                print(f"[Warning] Failed to write {USAGE_FILE}: {str(e)}")
        return {
            cat: sum(pruned.get(cat, {}).values())
            for cat in ALL_CATEGORIES
        }

# -------------------------------------------------------------------
# System Stats (CPU/RAM/Network/Disk) — sampled in the background so the
# polled /api/system-stats endpoint just reads a cached snapshot.
# -------------------------------------------------------------------

def count_items_last_minutes(minutes):
    """Muss innerhalb eines task_lock-Blocks aufgerufen werden!"""
    cutoff = time.time() - minutes * 60
    while item_download_events and item_download_events[0] < cutoff:
        item_download_events.popleft()
    return len(item_download_events)

# --- "Downloaded (24h)" ----------------------------------------------------
# This used to be a full os.walk of DOWNLOADS_DIR with a stat() per file, run
# by the stats sampler EVERY 60 SECONDS, forever, whether or not anyone had
# the page open. At ~370k files that is 370k seeks a minute; on the spinning
# disks this library is moving to, one pass would not finish before the next
# began, and the disks would never idle.
#
# It is now a rolling event log instead: a finished task appends (when, how
# many bytes it produced), and the tick just sums the last 24 hours in memory.
# Steady-state cost: zero filesystem calls.
DOWNLOAD_VOLUME_FILE = os.path.join(BASE_DIR, "download_volume.json")
DOWNLOAD_VOLUME_WINDOW = 24 * 3600
_download_volume_lock = threading.Lock()
_download_volume = None          # [[ts, bytes], ...]; None until first load

def _prune_download_volume(events, now=None):
    cutoff = (now or time.time()) - DOWNLOAD_VOLUME_WINDOW
    return [e for e in events if e[0] >= cutoff]

def _seed_download_volume():
    """First-run seed, from the recent-media list rather than the library.

    That list is the app's own record of what arrived lately, capped at 3000
    paths - so this costs at most 3000 stats ONCE, instead of 400k every
    minute.

    It is a LOWER BOUND, not the true figure, and measured to be so: a busy
    day here is ~11,800 files, which overflows the 3000 cap, and on 2026-09-02
    the seed gave 3.0GB against a true 17.5GB. It only matters for the first
    24h after a restart, because every task completion after that records its
    exact byte count; from then on the number is right. Getting the seed exact
    would mean walking the whole library, which is the thing this replaced.

    (The metric it replaced was not a baseline worth matching: that walk
    filtered on st_mtime, and gallery-dl backdates twitter files to the
    tweet's own date - see file_added_time - so it reported 4.9GB for the same
    17.5GB day. This uses arrival time, like the rest of the app.)"""
    events = []
    try:
        if not recent_media_store.recent_media_list_exists(BASE_DIR):
            return events
        cutoff = time.time() - DOWNLOAD_VOLUME_WINDOW
        for item in recent_media_store.read_recent_media_paths(BASE_DIR):
            root_id, rel_path = _normalize_recent_item(item)
            full = resolve_media_path(root_id, rel_path)
            if not full:
                continue
            try:
                st = os.stat(full)
            except OSError:
                continue
            added = file_added_time(st)
            if added >= cutoff:
                events.append([added, st.st_size])
        # Say so rather than quietly reporting a low number: a full recent
        # list means older arrivals fell off the end and are not counted.
        if len(events) >= recent_media_store.max_recent_media_items(BASE_DIR):
            print("[Info] 24h download volume seeded from a full recent-media "
                  "list, so it is a lower bound until new tasks complete.")
    except Exception as e:
        print(f"[Warning] Could not seed the 24h download volume: {e}")
    return events

def _load_download_volume():
    """Caller must hold _download_volume_lock."""
    global _download_volume
    if _download_volume is not None:
        return _download_volume
    events = []
    try:
        if os.path.exists(DOWNLOAD_VOLUME_FILE):
            with open(DOWNLOAD_VOLUME_FILE, "r", encoding="utf-8") as f:
                raw = json.load(f)
            events = [[float(t), int(b)] for t, b in raw
                      if isinstance(t, (int, float)) and isinstance(b, int)]
        else:
            events = _seed_download_volume()
    except Exception as e:
        print(f"[Warning] Could not read {DOWNLOAD_VOLUME_FILE}: {e}")
        events = []
    _download_volume = _prune_download_volume(events)
    return _download_volume

def _save_download_volume_unlocked():
    tmp = DOWNLOAD_VOLUME_FILE + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(_download_volume, f)
        os.replace(tmp, DOWNLOAD_VOLUME_FILE)
    except OSError as e:
        print(f"[Warning] Could not write {DOWNLOAD_VOLUME_FILE}: {e}")

def record_download_volume(byte_count, when=None):
    """Notes that a finished task produced byte_count bytes."""
    if not byte_count:
        return
    global _download_volume
    with _download_volume_lock:
        events = _load_download_volume()
        events.append([when or time.time(), int(byte_count)])
        _download_volume = _prune_download_volume(events)
        _save_download_volume_unlocked()

def compute_downloaded_bytes_since(seconds):
    """Bytes downloaded in the last `seconds`. Reads no files at all."""
    global _download_volume
    with _download_volume_lock:
        events = _load_download_volume()
        # Pruning on read as well keeps the number decaying when nothing is
        # being downloaded, rather than freezing at the last recorded value.
        _download_volume = events = _prune_download_volume(events)
        cutoff = time.time() - seconds
        return sum(b for t, b in events if t >= cutoff)

NET_SUBSAMPLE_SECONDS = 0.25
NET_SUBSAMPLES_PER_TICK = 4  # 4 * 0.25s = ~1s per reported tick, matching the frontend's poll interval

def stats_sampler_loop():
    psutil.cpu_percent(interval=None)  # prime the internal baseline
    last_net = psutil.net_io_counters()
    last_net_time = time.time()
    tick = 0

    while True:
        # A single delta over a whole 1-2s window averages away short
        # bursts (e.g. yt-dlp briefly hitting 100+ MB/s). Sub-sampling
        # every 250ms and keeping the peak sub-interval rate per tick
        # makes the dashboard reflect real spikes instead of smoothing
        # them out, while still only updating/publishing once per tick.
        peak_sent_mbps = 0.0
        peak_recv_mbps = 0.0
        for _ in range(NET_SUBSAMPLES_PER_TICK):
            time.sleep(NET_SUBSAMPLE_SECONDS)
            net = psutil.net_io_counters()
            now = time.time()
            dt = max(0.001, now - last_net_time)
            sent_mbps = (net.bytes_sent - last_net.bytes_sent) / (1024 ** 2) / dt
            recv_mbps = (net.bytes_recv - last_net.bytes_recv) / (1024 ** 2) / dt
            peak_sent_mbps = max(peak_sent_mbps, sent_mbps)
            peak_recv_mbps = max(peak_recv_mbps, recv_mbps)
            last_net, last_net_time = net, now

        tick += 1

        cpu = psutil.cpu_percent(interval=None)
        vm = psutil.virtual_memory()
        disk = psutil.disk_usage(DOWNLOADS_DIR)

        with task_lock:
            items_60 = count_items_last_minutes(60)

        update = {
            "cpu_percent": round(cpu, 1),
            "ram_percent": round(vm.percent, 1),
            "ram_used_gb": round(vm.used / (1024 ** 3), 2),
            "ram_total_gb": round(vm.total / (1024 ** 3), 2),
            "net_sent_mbps": round(peak_sent_mbps, 3),
            "net_recv_mbps": round(peak_recv_mbps, 3),
            "disk_free_gb": round(disk.free / (1024 ** 3), 2),
            "disk_total_gb": round(disk.total / (1024 ** 3), 2),
            "disk_used_percent": round(disk.percent, 1),
            "items_last_60min": items_60
        }

        if tick % 60 == 0:  # cheap now (an in-memory sum), but it only changes
            try:            # when a download finishes, so once a minute is plenty
                update["downloaded_bytes_24h"] = compute_downloaded_bytes_since(24 * 3600)
            except Exception as e:
                print(f"[Warning] Failed to compute 24h download volume: {str(e)}")

        with system_stats_lock:
            SYSTEM_STATS.update(update)

def worker_fallback(task_id, url, category):
    with task_lock:
        if task_id in active_tasks:
            active_tasks[task_id]["status"] = "Running"
            mark_task_started(active_tasks[task_id])
    save_state()
    execute_download_task(task_id, url, category)
    save_state()

def worker_loop(category):
    q = pipelines[category]
    while True:
        task_id = q.get()
        if task_id is None:
            break
        
        with task_lock:
            task = active_tasks.get(task_id)
            if task and task.get("status") != "Terminated":
                task["status"] = "Running"
                mark_task_started(task)
            else:
                task = None

        save_state()

        if task:
            # A task that raises must not take the whole pipeline with it:
            # an uncaught exception ends this thread, and the category's queue
            # then fills up forever without a word (2026-09-25: a NameError in
            # the first recovered YouTube task stalled YouTube for two days).
            try:
                execute_download_task(task["id"], task["url"], task["category"])
            except Exception as exc:
                traceback.print_exc()
                with task_lock:
                    t = active_tasks.get(task_id)
                    if t and t.get("status") in ("Queued", "Running"):
                        t["status"] = "Failed"
                        t["completed_at"] = time.time()
                        t["progress"] = f"Failed: {exc}"
                        t["logs"].append(f"[Error] {type(exc).__name__}: {exc}")
            save_state()
            wait_s = CONFIG.get("pipeline_wait_seconds", {}).get(category, 0)
            if wait_s > 0:
                time.sleep(wait_s)
        else:
            save_state()

        q.task_done()

# State laden: fills the pipeline queues. The workers that drain them start
# at the very END of this module - see start_pipeline_workers().
load_state()

threading.Thread(target=stats_sampler_loop, daemon=True).start()

def handle_shutdown_signal(signum, frame):
    """Kill in-flight download subprocesses on server shutdown so they don't
    keep running as orphans and race with the requeued task after restart
    (load_state() marks Running/Queued tasks as "Queued (Recovered)").

    The requeue depends on those tasks still reading as Running/Queued in
    tasks.json, and that was a race: killing the subprocess makes the
    worker's `proc.wait()` return a non-zero code, which it writes as
    "Failed" - a status load_state() does NOT requeue. Whether a restart
    resumed a download or silently dropped it came down to which thread got
    there first. So mark them Queued and persist that BEFORE the kill, and
    have the worker leave the status alone once SHUTTING_DOWN is set."""
    SHUTTING_DOWN.set()
    requeued = 0
    with task_lock:
        for task in active_tasks.values():
            if task.get("status") in ("Running", "Queued"):
                task["status"] = "Queued"
                task["progress"] = "Queued (server restarting)"
                requeued += 1
    if requeued:
        # Called outside the lock - save_state takes it itself.
        save_state()
        print(f"[Info] Shutting down: {requeued} download(s) queued to resume on restart.")

    with task_lock:
        procs = list(running_processes.values())
    if procs:
        print(f"[Info] Shutting down: terminating {len(procs)} active download process(es)...")
    for proc in procs:
        try:
            proc.kill()
        except Exception:
            pass
    sys.exit(0)

signal.signal(signal.SIGTERM, handle_shutdown_signal)
signal.signal(signal.SIGINT, handle_shutdown_signal)

# -------------------------------------------------------------------
# Routes & Endpoints
# -------------------------------------------------------------------

@app.route("/login", methods=["POST"])
def login():
    """Retired. Identity is Authelia's; this endpoint only exists to give an
    old client a clear answer instead of a confusing 404."""
    return jsonify({"success": False,
                    "message": "This server now uses single sign-on. "
                               "Visit the site in a browser to sign in."}), 410


@app.route("/logout", methods=["GET"])
def logout():
    # Ending the Authelia session is what "log out" means now -- clearing a
    # local cookie would leave the user signed in at the identity provider and
    # they would be let straight back in.
    if not AUTH_URL:
        return redirect("/")
    return redirect(AUTH_URL + "/logout?rd=" + quote((PUBLIC_URL or "") + "/", safe=""))

@app.route("/api/download", methods=["POST"])
@require_admin
def trigger_download():
    data = request.get_json(silent=True) or {}
    url = data.get("url", "").strip()

    if not url:
        return jsonify({"error": "URL parameter is required"}), 400
    link, dual = split_dual_link(url)
    if dual:
        if not get_sso_user():
            return jsonify({"error": "yt-dual/ links are for the web page, not the API"}), 400
        if classify_url(link) != "youtube":
            return jsonify({"error": "yt-dual/ works with YouTube links only"}), 400
        url = DUAL_PREFIX + link
    try:
        task_id, category = queue_download(url)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify({"success": True, "task_id": task_id, "category": category})


# "yt-dual/<link>": a YouTube video or playlist kept as music AND as video
# (user, 2026-09-23: "How can I download a playlist as both music and
# videos?" - then: "let us not enable dual download via API and instead only
# via the webpage, maybe by manually modifying the link"). The prefix is kept
# in the task's own url, so the dashboard shows it and its Re-run button and
# a restart's recovery bring it back as the same kind of download;
# execute_download_task takes it off before anything reads the link. Only a
# request from the web page (an SSO session) may start one - an API key is
# refused, as asked.
DUAL_PREFIX = "yt-dual/"
DUAL_LINK_RE = re.compile(r"^\s*yt-dual(?:\s*[:/]\s*|\s+)(\S.*)$", re.IGNORECASE)


def split_dual_link(url):
    """(the link, True) for "yt-dual/<link>" - "yt-dual:" and a link without
    https:// as well - and (url, False) for anything else."""
    m = DUAL_LINK_RE.match(url or "")
    if not m:
        return url, False
    link = m.group(1).strip()
    if not re.match(r"^[a-z][a-z0-9+.-]*://", link, re.IGNORECASE):
        link = "https://" + link
    return link, True


_PLAIN_URL_RE = re.compile(
    r"""^(?:https?://[^\s/?#<>"'`\x5c]+[^\s<>"'`\x5c]*|@[\w.\-]{1,100}|[A-Za-z0-9][A-Za-z0-9.\-]*\.[A-Za-z]{2,}(?:[/?#][^\s<>"'`\x5c]*)?)$""")


def download_url_is_plain(url):
    """A link is handed to yt-dlp / gallery-dl as one command-line argument, so anything starting with "-"
    would be read as an OPTION (yt-dlp --exec runs a program). classify_url only looks for a site name
    anywhere in the text, so it cannot be the guard: accept http(s) links, "@handle" and bare "site.tld/..."
    without whitespace or control characters, nothing else."""
    return (isinstance(url, str) and 0 < len(url) <= 2000 and not url.startswith("-")
            and _PLAIN_URL_RE.match(url) is not None and not any(ord(c) < 32 or ord(c) == 127 for c in url))


def queue_download(url):
    """Queues a download of `url` exactly as the dashboard does: (task id,
    category), or ValueError for a site nothing here downloads. The music
    page's playlist sync goes through here too."""
    if not download_url_is_plain(url[len(DUAL_PREFIX):] if url.startswith(DUAL_PREFIX) else url):
        raise ValueError("Rejected: not a plain web address")
    category = classify_url(url)
    if category == "unknown":
        raise ValueError("Rejected: Unsupported website URL format")

    task_id = secrets.token_hex(8)

    with task_lock:
        active_tasks[task_id] = {
            "id": task_id,
            "url": url,
            "category": category,
            "status": "Queued",
            "progress": "0%",
            "logs": deque(maxlen=MAX_LOG_LINES),
            "created_at": time.time(),
            "started_at": None,
            "completed_at": None,
            "media_path": None
        }

    save_state()
    record_pipeline_usage(category)

    if category in pipelines:
        pipelines[category].put(task_id)
    else:
        threading.Thread(target=worker_fallback, args=(task_id, url, category), daemon=True).start()
    return task_id, category

@app.route("/api/tasks", methods=["GET"])
@require_admin
def get_tasks():
    tasks_response = []
    with task_lock:
        for task in active_tasks.values():
            task_copy = task.copy()
            if isinstance(task_copy.get("logs"), deque):
                task_copy["logs"] = list(task_copy["logs"])
            tasks_response.append(task_copy)
            
    return jsonify(tasks_response)

@app.route("/api/pipes", methods=["GET"])
@require_admin
def get_pipes():
    max_display = CONFIG.get("max_displayed_tasks_per_pipe", 5)
    weekly_usage = get_weekly_usage()

    out = {}
    with task_lock:
        for cat, q in pipelines.items():
            stats = pipeline_stats[cat]
            elapsed = max(1, time.time() - stats["last_time"])

            current_task = None
            for t in active_tasks.values():
                if t["category"] == cat and t["status"] == "Running":
                    current_task = {"id": t["id"], "url": t["url"], "progress": t["progress"], "started_at": t.get("started_at")}
                    break

            remaining_slots = max(0, max_display - (1 if current_task else 0))
            upcoming_tasks = []
            for task_id in list(q.queue)[:remaining_slots]:
                t = active_tasks.get(task_id)
                if t:
                    upcoming_tasks.append({"id": t["id"], "url": t["url"], "status": t["status"], "created_at": t.get("created_at")})

            out[cat] = {
                "queued": q.qsize(),
                "rate_mbps": round(stats["rate_mbps"], 2),
                "items_per_hr": round(stats["items_downloaded"] * 3600 / elapsed, 1),
                "total_mb": round(stats["session_total_mb"], 2),
                "current_task": current_task,
                "upcoming_tasks": upcoming_tasks,
                "weekly_usage": weekly_usage.get(cat, 0)
            }
    return jsonify(out)

@app.route("/api/system-stats", methods=["GET"])
@require_admin
def get_system_stats():
    with system_stats_lock:
        return jsonify(dict(SYSTEM_STATS))

@app.route("/api/prefs", methods=["GET"])
def get_prefs():
    user = get_authenticated_user()
    if not user:
        return jsonify({"error": "Unauthorized"}), 401
    return jsonify(user.get("prefs") or dict(DEFAULT_PREFS))

@app.route("/api/prefs", methods=["POST"])
def set_prefs():
    user = get_authenticated_user()
    if not user:
        return jsonify({"error": "Unauthorized"}), 401

    data = request.get_json(silent=True) or {}
    updates = {}
    if "advanced_mode" in data:
        updates["advanced_mode"] = bool(data["advanced_mode"])

    if not updates:
        return jsonify({"error": "No valid preference fields provided"}), 400

    updated_user = update_user_prefs(user["username"], updates)
    if not updated_user:
        return jsonify({"error": "User not found"}), 404

    return jsonify(updated_user.get("prefs") or dict(DEFAULT_PREFS))

@app.route("/api/task/<task_id>/terminate", methods=["POST"])
@require_admin
def terminate_task(task_id):
    with task_lock:
        task = active_tasks.get(task_id)
        if not task:
            return jsonify({"error": "Not found"}), 404
        task["status"] = "Terminated"
        task["completed_at"] = time.time()
        proc = running_processes.get(task_id)
        if proc:
            proc.kill()

    save_state()
    return jsonify({"success": True})

@app.route("/api/task/<task_id>/prioritize", methods=["POST"])
@require_admin
def prioritize_task(task_id):
    with task_lock:
        task = active_tasks.get(task_id)
        if not task or task["status"] != "Queued":
            return jsonify({"error": "Cannot prioritize non-queued task"}), 400
        cat = task["category"]
        q = pipelines[cat]
        items = []
        while not q.empty():
            items.append(q.get_nowait())
        items = [i for i in items if i != task_id]
        items.insert(0, task_id)
        for i in items:
            q.put(i)

    return jsonify({"success": True})

@app.route("/api/pipe/<category>/purge", methods=["POST"])
@require_admin
def purge_pipe(category):
    if category not in pipelines:
        return jsonify({"error": "Invalid pipe"}), 404

    with task_lock:
        q = pipelines[category]
        while not q.empty():
            try:
                tid = q.get_nowait()
                if tid in active_tasks:
                    active_tasks[tid]["status"] = "Terminated"
                    active_tasks[tid]["completed_at"] = time.time()
            except queue.Empty:
                break

    save_state()
    return jsonify({"success": True})

@app.route("/api/me", methods=["GET"])
def get_me():
    user = get_authenticated_user()
    if not user:
        return jsonify({"error": "Unauthorized"}), 401
    # Groups come straight from Authelia when the request arrived through the
    # proxy; the shared user widget lists them under the account name.
    groups = [g.strip() for g in
              (request.headers.get("Remote-Groups") or "").split(",") if g.strip()]
    return jsonify({"username": user["username"], "user": user["username"],
                    "role": user.get("role", "admin"), "groups": groups})

@app.route("/api/users", methods=["GET"])
@require_admin
def list_users():
    users = load_users()
    return jsonify([
        {"username": u["username"], "role": u.get("role", "admin")}
        for u in users
    ])

# -------------------------------------------------------------------
# Media roots - "downloads" is just the built-in one. Admins can add
# further folders from anywhere on the server, each with its own
# guest-visibility flag. Everything that resolves a media path takes a
# (root_id, relative path) pair; a missing/blank root means the built-in
# downloads root, which keeps every path already stored in shares.json
# and recent_media.json valid without migration.
# -------------------------------------------------------------------

roots_lock = threading.Lock()

def _read_roots_unlocked():
    if not os.path.exists(ROOTS_FILE):
        return []
    with open(ROOTS_FILE, "r", encoding="utf-8") as f:
        return json.load(f)

def _write_roots_unlocked(roots):
    temp_file = ROOTS_FILE + ".tmp"
    with open(temp_file, "w", encoding="utf-8") as f:
        json.dump(roots, f, indent=4, ensure_ascii=False)
    os.replace(temp_file, ROOTS_FILE)

def builtin_root():
    """The downloads root. Its guest visibility is persisted in roots.json
    like any other root's; everything else about it is fixed."""
    visible = True
    try:
        for r in load_extra_roots():
            if r["id"] == DEFAULT_ROOT_ID:
                visible = bool(r.get("guest_visible", True))
    except Exception:
        pass
    return {
        "id": DEFAULT_ROOT_ID,
        "name": "Downloads",
        "path": DOWNLOADS_DIR,
        "guest_visible": visible,
        "builtin": True
    }

def load_extra_roots():
    with roots_lock:
        try:
            return _read_roots_unlocked()
        except Exception as e:
            print(f"[Warning] Failed to load {ROOTS_FILE}: {str(e)}")
            return []

def get_all_roots():
    """Built-in downloads root first, then admin-added ones."""
    extras = [r for r in load_extra_roots() if r.get("id") != DEFAULT_ROOT_ID]
    return [builtin_root()] + extras

def get_root(root_id):
    root_id = root_id or DEFAULT_ROOT_ID
    for r in get_all_roots():
        if r["id"] == root_id:
            return r
    return None

def roots_for_user(user):
    """Guests only ever see roots explicitly marked guest_visible."""
    roots = get_all_roots()
    if user and not is_admin(user):
        return [r for r in roots if r.get("guest_visible")]
    return roots

def root_visible_to(user, root_id):
    return any(r["id"] == (root_id or DEFAULT_ROOT_ID) for r in roots_for_user(user))

def resolve_media_path(root_id, rel_path):
    """Resolves (root, path) to an absolute path, or None if the root is
    unknown or the path would escape it (traversal guard). Never checks
    permissions - callers do that via root_visible_to()."""
    root = get_root(root_id)
    if not root:
        return None
    base = os.path.abspath(root["path"])
    rel_path = (rel_path or "").strip("/").replace("\\", "/")
    target = os.path.abspath(os.path.join(base, rel_path))
    if target == base or target.startswith(base + os.sep):
        return target
    return None

def cover_version(share):
    """Cache key for a share's cover URL, for the same reason as
    thumb_version: uploading a new custom thumbnail replaces the image behind
    an unchanged URL, which a cached copy would hide for an hour. The stored
    filename is regenerated on every upload, so it doubles as the key."""
    return quote(str(share.get("thumbnail") or "none"), safe="")

def thumb_version(entry):
    """Cache key for a file's thumbnail URL.

    Thumbnails are served with a day of caching because a grid asks for
    hundreds of them, but the URL is derived from the path alone - so a
    regenerated thumbnail (the file was replaced, or the way it's generated
    changed) would stay invisible until that day was up, with clearing the
    browser cache as the only cure. Folding the source's mtime into the URL
    keeps the long caching and still updates the moment the file does."""
    return int(entry.get("mtime") or 0)

# yt-dlp writes "<name>-poster.jpg" beside the video it belongs to. It is a
# companion to that video, not a picture in its own right, and in this library
# 19,352 of 19,353 of them sit next to their video - listing both means every
# video folder shows each item twice.
#
# The sibling test is deliberately name-only: every caller already holds the
# directory's file names (from scandir or os.walk), so hiding these costs no
# extra stat calls. That matters because the library is moving onto spinning
# disks, where a per-file existence check is a seek.
POSTER_STEM_SUFFIX = "-poster"

def poster_companion_names(filenames):
    """Of `filenames` (all from ONE directory), those that are only a poster
    for a video sitting next to them. A poster whose video is gone is NOT
    hidden - it is then the only thing left representing that item."""
    video_stems = {
        os.path.splitext(n)[0].lower() for n in filenames
        if os.path.splitext(n)[1].lower() in MEDIA_EXTENSIONS["video"]
    }
    if not video_stems:
        return frozenset()
    hidden = set()
    for name in filenames:
        stem, ext = os.path.splitext(name)
        if ext.lower() not in MEDIA_EXTENSIONS["image"]:
            continue
        stem = stem.lower()
        if (stem.endswith(POSTER_STEM_SUFFIX)
                and stem[:-len(POSTER_STEM_SUFFIX)] in video_stems):
            hidden.add(name)
    return frozenset(hidden)

def _is_hidden_or_partial(filename):
    # .js here is not script: it's the ugoira frame-delay table PixivUtil2
    # writes beside an animation archive, which is metadata, not media.
    #
    # .nfo is Jellyfin's metadata sidecar and "<name>-poster.jpg" the cover it
    # writes beside an item: both DESCRIBE an item rather than being one, and
    # they turned up in search results next to the song or video they belong
    # to (user, 2026-09-20: "hide .nfo and *-poster"). Hidden wherever a
    # listing is built - search, browsing, shares, folder counts, the recent
    # feed. A video's thumbnail is taken from the video itself, so nothing
    # loses its picture. Unlike poster_companion_names this does not ask
    # whether the video is still there: a poster on its own is still a poster,
    # and in this library exactly one of 19,353 has no video beside it.
    name = filename.lower()
    if name.startswith(".") or name.endswith((".part", ".ytdl", ".lock", ".js", ".nfo")):
        return True
    # One stream of an interrupted yt-dlp download, "<title> [<id>].f30280.m4a"
    # (user, 2026-10-13: listed as an "Unknown artist" song). The merged file
    # replaces it; until then it is a fragment, not media.
    if _YTDLP_FRAGMENT_NAME.search(filename):
        return True
    stem, ext = os.path.splitext(name)
    return ext in MEDIA_EXTENSIONS["image"] and stem.endswith(POSTER_STEM_SUFFIX)

def _entry_media_type(rel_dir, filename, root_id):
    """Media type for a listing entry. Only archives need the file itself
    resolved (to tell a pixiv animation from an ordinary zip) - everything
    else is decided by extension alone, which keeps large listings cheap."""
    kind = classify_media_type(filename)
    if kind != "other" or not filename.lower().endswith(".zip"):
        return kind
    rel_file_path = "/".join(p for p in (rel_dir, filename) if p)
    full = resolve_media_path(root_id, rel_file_path)
    return "video" if full and is_ugoira_zip(full) else kind

def _media_file_entry(rel_dir, filename, stat, root_id=DEFAULT_ROOT_ID):
    rel_file_path = "/".join(p for p in (rel_dir, filename) if p)
    return {
        "name": filename,
        "size": stat.st_size,
        "mtime": stat.st_mtime,
        "type": _entry_media_type(rel_dir, filename, root_id),
        # Served through an endpoint rather than a static path: roots other
        # than "downloads" live outside web_files entirely, and every read
        # has to be checked against the caller's root visibility anyway.
        "url": f"/api/media/file?root={quote(root_id, safe='')}&path={quote(rel_file_path, safe='')}",
        "path": rel_file_path,
        "root": root_id,
        # mtime is the content's own date (downloaders stamp the source's
        # timestamp onto it); added is when it actually landed here. Both
        # are shown in the info panel, and they routinely differ by years.
        "added": file_added_time(stat)
    }

@app.route("/api/roots", methods=["GET"])
def list_roots():
    user = get_authenticated_user()
    if not user:
        return jsonify({"error": "Unauthorized"}), 401
    return jsonify([
        {
            "id": r["id"],
            "name": r["name"],
            # Only admins get to see where a root actually lives on disk.
            "path": r["path"] if is_admin(user) else None,
            "guest_visible": bool(r.get("guest_visible")),
            "builtin": bool(r.get("builtin")),
            "exists": os.path.isdir(r["path"])
        }
        for r in roots_for_user(user)
    ])

@app.route("/api/roots", methods=["POST"])
@require_admin
def create_root():
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    path = (data.get("path") or "").strip()

    if not name or not path:
        return jsonify({"error": "name and path are required"}), 400

    path = os.path.abspath(path)
    if not os.path.isdir(path):
        return jsonify({"error": "That path is not an existing directory on the server"}), 400

    with roots_lock:
        roots = _read_roots_unlocked()
        if any(os.path.abspath(r["path"]) == path for r in roots) or path == os.path.abspath(DOWNLOADS_DIR):
            return jsonify({"error": "That folder is already a root"}), 400
        new_root = {
            "id": secrets.token_hex(8),
            "name": name,
            "path": path,
            # Hidden from guests until an admin opts in - a newly added
            # folder shouldn't become public just by existing.
            "guest_visible": bool(data.get("guest_visible", False))
        }
        roots.append(new_root)
        _write_roots_unlocked(roots)

    invalidate_media_caches()
    return jsonify(new_root)

@app.route("/api/roots/<root_id>", methods=["PATCH"])
@require_admin
def update_root(root_id):
    data = request.get_json(silent=True) or {}

    with roots_lock:
        roots = _read_roots_unlocked()
        entry = next((r for r in roots if r["id"] == root_id), None)

        # The built-in downloads root has no roots.json entry until one of
        # its settings is changed - create a settings-only stub for it.
        if entry is None and root_id == DEFAULT_ROOT_ID:
            entry = {"id": DEFAULT_ROOT_ID, "name": "Downloads", "path": DOWNLOADS_DIR, "guest_visible": True}
            roots.append(entry)
        if entry is None:
            return jsonify({"error": "Not found"}), 404

        if "guest_visible" in data:
            entry["guest_visible"] = bool(data["guest_visible"])
        if "name" in data and root_id != DEFAULT_ROOT_ID:
            new_name = (data.get("name") or "").strip()
            if new_name:
                entry["name"] = new_name
        _write_roots_unlocked(roots)

    invalidate_media_caches()
    return jsonify({"success": True})

@app.route("/api/roots/<root_id>", methods=["DELETE"])
@require_admin
def delete_root(root_id):
    if root_id == DEFAULT_ROOT_ID:
        return jsonify({"error": "The built-in downloads root cannot be removed"}), 400

    with roots_lock:
        roots = _read_roots_unlocked()
        remaining = [r for r in roots if r["id"] != root_id]
        if len(remaining) == len(roots):
            return jsonify({"error": "Not found"}), 404
        _write_roots_unlocked(remaining)

    invalidate_media_caches()
    # Only the reference is dropped - nothing on disk is touched.
    return jsonify({"success": True})

def invalidate_media_caches():
    """Roots changing invalidates both cached listings, which are keyed to
    the set of roots that existed when they were built."""
    with _media_index_lock:
        _media_index_cache["index"] = None
    with _recent_media_page_lock:
        _recent_media_page_cache["entries"] = None
    with _folder_stats_lock:
        _folder_stats_cache.clear()

import steam_ingest
steam_ingest.register(app, get_authenticated_user, is_admin, DOWNLOADS_DIR, invalidate_media_caches)

# -------------------------------------------------------------------
# Deleting media: the trash, and the records that decide whether a
# deleted file can come back
# -------------------------------------------------------------------
#
# Every downloader keeps its own memory of what it already fetched, so a
# repeated run doesn't re-download the world. That memory is what makes
# deletion ambiguous: removing the file alone leaves the record behind, and
# the next run of the same URL refuses to fetch it again ("already
# recorded"). So a delete has to say which of two things it means:
#
#   release - delete it AND forget it, so it can be downloaded again
#   ban     - delete it and make sure it never comes back
#
# The memories involved, and what each mode has to do to them:
#
#   youtube / youtube music   downloads/youtube/.yt_dlp_archive.txt
#                             ban: leave the line.  release: drop the line.
#   pixiv                     PixivUtil2/db.sqlite, pixiv_master_image
#                             ban: leave the rows.  release: drop the rows.
#   gallery-dl sources        no record of any kind exists
#                             ban: leave a 0-byte tombstone at the original
#                             path, which gallery-dl's skip-if-exists treats
#                             as "already have it".  release: nothing to do.
#
# So for youtube and pixiv a ban is the ABSENCE of action - the record is
# already there, it is why the file was downloaded - and release is the
# active step. For gallery-dl it is the other way round, because nothing
# was ever recorded; that is exactly why a deleted tweet image always came
# back before.
#
# Nothing is unlinked. Files move to <root>/.trash/<batch>/, which browsing
# skips (it ignores any name starting with a dot), so a wrong selection is
# recoverable until the trash is emptied.

DELETE_RELEASE = "release"   # delete, and allow a future re-download
DELETE_BAN = "ban"           # delete, and keep it out for good

TRASH_DIR_NAME = ".trash"

# Sources fetched by gallery-dl, which keeps no archive here. A tombstone is
# the only ban available for these, and the only one that works for files
# downloaded before any of this existed.
GALLERY_DL_CATEGORIES = {"twitter", "danbooru", "civitai", "bluesky",
                         "patreon", "deviantart", "discord", "instagram"}

# --- youtube -------------------------------------------------------

YOUTUBE_ARCHIVE_PATH = os.path.join(DOWNLOADS_DIR, "youtube", ".yt_dlp_archive.txt")
MUSIC_EXTRACTOR_LOG = os.path.join(BASE_DIR, "youtube_music_extractor.log")
_YOUTUBE_ID_IN_NAME = re.compile(r"\[([A-Za-z0-9_-]{11})\]")

# Containers whose tags mutagen cannot read, so the id has to come back out
# through ffprobe. Videos are here because --embed-metadata now stamps them
# too, and a .webm or .mkv is exactly the case where the filename may have
# been changed but the tag survives.
_FFPROBE_TAG_EXTS = (".mka", ".mkv", ".webm", ".mp4", ".m4v", ".mov", ".avi")

def _youtube_id_from_mka(full_path):
    """The video id out of a container's own tags, read with ffprobe.
    yt-dlp writes the source URL into PURL/COMMENT/DESCRIPTION the same way
    for audio and video; only the way to read it back differs."""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format_tags",
             "-of", "json", full_path],
            capture_output=True, text=True, timeout=30)
        tags = (json.loads(out.stdout or "{}").get("format") or {}).get("tags") or {}
    except Exception:
        return None
    for key, val in tags.items():
        if key.lower() not in ("purl", "comment", "description", "woas", "url"):
            continue
        m = (re.search(r"[?&]v=([A-Za-z0-9_-]{11})", str(val))
             or _YOUTUBE_ID_IN_NAME.search(str(val)))
        if m:
            return m.group(1)
    return None

def _youtube_id_from_tags(full_path):
    """The video id a downloaded file carries in its own tags. Files written
    before this was stamped have none, which is what the log fallback below
    is for."""
    ext = os.path.splitext(full_path)[1].lower()
    if ext in _FFPROBE_TAG_EXTS:
        return _youtube_id_from_mka(full_path)
    if ext not in (".opus", ".ogg", ".m4a", ".mp3", ".flac"):
        return None
    try:
        import mutagen
        tags = mutagen.File(full_path)
        if not tags:
            return None
        for key in ("purl", "PURL", "comment", "\xa9cmt", "WOAS", "description"):
            val = tags.get(key)
            if not val:
                continue
            text = val[0] if isinstance(val, list) else val
            m = (re.search(r"[?&]v=([A-Za-z0-9_-]{11})", str(text))
                 or _YOUTUBE_ID_IN_NAME.search(str(text)))
            if m:
                return m.group(1)
    except Exception:
        pass
    return None

_music_log_cache = {"mtime": 0.0, "map": {}}
_music_log_lock = threading.Lock()

def _music_log_map():
    """basename -> video id, parsed from the extractor log. That log is the
    only surviving record for audio extracted before ids were embedded in
    the tags: the source video (whose filename carried the id) is deleted
    once the audio exists. It is a few KB and append-only, so reparsing it
    when it grows is cheap."""
    try:
        mtime = os.path.getmtime(MUSIC_EXTRACTOR_LOG)
    except OSError:
        return {}
    with _music_log_lock:
        if mtime == _music_log_cache["mtime"]:
            return _music_log_cache["map"]
    try:
        with open(MUSIC_EXTRACTOR_LOG, "r", encoding="utf-8", errors="replace") as f:
            text = f.read()
    except OSError:
        return {}
    mapping = {}
    for vid, block in re.findall(
            r"^--- .+? \| .*? \(([A-Za-z0-9_-]{5,})\) ---$(.*?)(?=^--- |\Z)",
            text, re.M | re.S):
        saved = re.search(r"^\[youtube-music\] Saved: (.+)$", block, re.M)
        if saved:
            mapping[os.path.basename(saved.group(1).strip())] = vid
    with _music_log_lock:
        _music_log_cache["mtime"] = mtime
        _music_log_cache["map"] = mapping
    return mapping

def youtube_id_for(full_path):
    """The video id behind one downloaded file, from whichever record still
    has it: the filename yt-dlp wrote, the audio file's own tags, or the
    extractor log naming what each audio file came from."""
    name = os.path.basename(full_path)
    m = _YOUTUBE_ID_IN_NAME.search(name)
    if m:
        return m.group(1)
    vid = _youtube_id_from_tags(full_path)
    if vid:
        return vid
    return _music_log_map().get(name)

_youtube_archive_lock = threading.Lock()

def youtube_archive_edit(video_ids, add):
    """Adds or removes 'youtube <id>' lines. Rewrites the file whole, under
    a lock and through a temp file: a partial write here would strip
    thousands of entries and re-download the entire library."""
    ids = {v for v in video_ids if v}
    if not ids:
        return 0
    with _youtube_archive_lock:
        lines = []
        if os.path.exists(YOUTUBE_ARCHIVE_PATH):
            with open(YOUTUBE_ARCHIVE_PATH, "r", encoding="utf-8") as f:
                lines = f.read().splitlines()
        present = {l.strip().split()[-1] for l in lines if l.strip()}
        if add:
            new = [f"youtube {v}" for v in sorted(ids - present)]
            if not new:
                return 0
            out, changed = lines + new, len(new)
        else:
            out = [l for l in lines
                   if not (l.strip().startswith("youtube ")
                           and l.strip().split()[-1] in ids)]
            changed = len(lines) - len(out)
            if not changed:
                return 0
        os.makedirs(os.path.dirname(YOUTUBE_ARCHIVE_PATH), exist_ok=True)
        tmp = YOUTUBE_ARCHIVE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write("\n".join(out) + ("\n" if out else ""))
        os.replace(tmp, YOUTUBE_ARCHIVE_PATH)
        return changed

# --- pixiv ---------------------------------------------------------

PIXIV_DB_PATH = os.path.join(BASE_DIR, "PixivUtil2", "db.sqlite")
_PIXIV_ID_IN_NAME = re.compile(r"^(\d+)_p\d+")

def pixiv_image_id_for(full_path):
    m = _PIXIV_ID_IN_NAME.match(os.path.basename(full_path))
    return m.group(1) if m else None

def pixiv_db_forget(image_ids):
    """Drops PixivUtil2's memory of these images so a later run of the same
    artist fetches them again. Only the release direction exists: a ban
    needs no action, because the row is already there."""
    ids = [i for i in image_ids if i]
    if not ids or not os.path.exists(PIXIV_DB_PATH):
        return 0
    conn = sqlite3.connect(PIXIV_DB_PATH)
    try:
        for table in ("pixiv_master_image", "pixiv_manga_image", "pixiv_ai_info"):
            try:
                conn.executemany(f"DELETE FROM {table} WHERE image_id = ?",
                                 [(i,) for i in ids])
            except sqlite3.Error:
                continue          # table absent in this schema version
        removed = conn.total_changes
        conn.commit()
        return removed
    finally:
        conn.close()

# --- what a delete means for one path ------------------------------

def download_category_of(root_id, rel_path):
    """Which downloader produced a file, from where it sits in the built-in
    downloads tree. Files in an admin-added library have no downloader
    behind them, so there is no record to release or keep."""
    if (root_id or DEFAULT_ROOT_ID) != DEFAULT_ROOT_ID:
        return None
    head = (rel_path or "").strip("/").split("/")[0]
    return head or None

def collect_deletable_files(full_path):
    """Every file a delete would remove: the file itself, or everything
    under a folder."""
    if os.path.isfile(full_path):
        return [full_path]
    out = []
    for base, _dirs, files in os.walk(full_path):
        for name in files:
            out.append(os.path.join(base, name))
    return out

def plan_record_changes(root_id, rel_path, full_path):
    """Which records the files under this path correspond to. Computed
    BEFORE anything moves: the filename is one of the id sources, and for an
    extracted audio file so are its own tags."""
    category = download_category_of(root_id, rel_path)
    plan = {"category": category, "root": root_id, "youtube": set(), "pixiv": set(),
            "tombstone": [], "unresolved": 0, "files": 0}
    for f in collect_deletable_files(full_path):
        # Counted whether or not a downloader is behind it: the caller
        # reports this back as "n files deleted", which is true of an added
        # library too - only the record lookup below is downloads-only.
        plan["files"] += 1
        if not category:
            continue
        if category in ("youtube", "youtube_music"):
            vid = youtube_id_for(f)
            if vid:
                plan["youtube"].add(vid)
            else:
                plan["unresolved"] += 1
        elif category == "pixiv":
            pid = pixiv_image_id_for(f)
            if pid:
                plan["pixiv"].add(pid)
            else:
                plan["unresolved"] += 1
        elif category in GALLERY_DL_CATEGORIES:
            plan["tombstone"].append(f)
    return plan

# Files known to be EMPTY, {root_id: {rel_path}}. Folder listings drop a
# 0-byte file by its size, but the search index stats nothing - a stat per
# file per rebuild is minutes on the spinning disks when the inodes are cold -
# so every ban tombstone (33,907 of them on 2026-09-18) was a search hit.
# The index leaves out what is listed here instead. A tombstone is recorded
# when it is written, forgotten when a restore replaces it, and any empty file
# a listing meets (build_entries_for) is recorded too; check_empty_files()
# re-stats the list in the background at startup so a path that holds a real
# file again comes back.
EMPTY_FILES_STORE = os.path.join(BASE_DIR, "empty_files.json")
_empty_files = None
_empty_files_lock = threading.Lock()


def _empty_files_loaded():
    """The registry, read from disk once. Call with _empty_files_lock held."""
    global _empty_files
    if _empty_files is None:
        try:
            with open(EMPTY_FILES_STORE, encoding="utf-8") as fh:
                raw = json.load(fh) or {}
            _empty_files = {r: set(p) for r, p in raw.items() if isinstance(p, list)}
        except (OSError, ValueError):
            _empty_files = {}
    return _empty_files


def _save_empty_files():
    """Call with _empty_files_lock held."""
    tmp = EMPTY_FILES_STORE + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({r: sorted(p) for r, p in _empty_files.items() if p}, fh)
        os.replace(tmp, EMPTY_FILES_STORE)
    except OSError as exc:
        print(f"[Warning] empty-file registry not saved: {exc}")


def note_empty_files(pairs, empty=True):
    """Records (root_id, rel_path) pairs as empty, or with empty=False forgets
    them. Returns how many changed."""
    changed = 0
    with _empty_files_lock:
        known = _empty_files_loaded()
        for root_id, rel in pairs:
            paths = known.setdefault(root_id, set())
            if empty and rel not in paths:
                paths.add(rel)
                changed += 1
            elif not empty and rel in paths:
                paths.discard(rel)
                changed += 1
        if changed:
            _save_empty_files()
    return changed


def empty_files_snapshot():
    with _empty_files_lock:
        return {r: set(p) for r, p in _empty_files_loaded().items()}


def check_empty_files():
    """Forgets every registered path that is no longer an empty file - put back
    by hand, or gone - and rebuilds the index if that changed anything. A stat
    per registered path, so it runs in the background."""
    stale = []
    for root_id, paths in empty_files_snapshot().items():
        base = (get_root(root_id) or {}).get("path")
        if not base:
            continue
        for rel in paths:
            try:
                if os.path.getsize(os.path.join(base, rel)) == 0:
                    continue
            except OSError:
                pass
            stale.append((root_id, rel))
    if stale and note_empty_files(stale, empty=False):
        print(f"[Info] {len(stale)} path(s) left the empty-file registry")
        invalidate_media_caches()


def _write_tombstone(path):
    """A 0-byte stand-in at a deleted file's original path. gallery-dl skips
    a download whose target already exists, so this is what keeps a deleted
    tweet image from simply coming back on the next run of that account.
    Listings hide empty files, so it stays invisible in the browser."""
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb"):
            pass
        return True
    except OSError:
        return False

def apply_record_changes(plan, mode):
    """Releases or bans, per the plan. Returns short human-readable notes."""
    notes = []
    if mode == DELETE_RELEASE:
        n = youtube_archive_edit(plan["youtube"], add=False)
        if n:
            notes.append(f"{n} freed in the yt-dlp archive")
        n = pixiv_db_forget(plan["pixiv"])
        if n:
            notes.append(f"{n} PixivUtil2 rows dropped")
    else:
        written = [f for f in plan["tombstone"] if _write_tombstone(f)]
        n = len(written)
        base = (get_root(plan.get("root")) or {}).get("path")
        if base:
            # For the empty-file registry: search must not list them.
            # trash_resolved records a whole delete at once.
            plan["tombstones_written"] = [
                (plan["root"], os.path.relpath(f, base).replace(os.sep, "/")) for f in written]
        if n:
            notes.append(f"{n} tombstone{'' if n == 1 else 's'} left so it "
                         f"is not downloaded again")
    return notes

# --- the trash -----------------------------------------------------

def trash_resolved(resolved, mode, user, reason=None):
    """Move already-validated (root, rel, full) triples to the trash.

    Split out of delete_media so the review queue can accept a suggestion
    through exactly this path rather than a second implementation of it: the
    record changes that decide whether a file may be downloaded again are the
    subtle part, and there must be only one copy of them.

    `reason` (optional) is stored in the batch manifest and shown in the trash
    list; the character blacklist uses it to say why a batch exists.

    Returns (batch, deleted, notes, failed).
    """
    batch = time.strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(3)
    deleted, notes, failed = [], [], []
    per_root = {}
    tombstones = []

    for root_id, rel_path, full in resolved:
        # The plan must be computed while the files still exist: ids come
        # from filenames and, for extracted audio, from the file's own tags.
        plan = plan_record_changes(root_id, rel_path, full)
        try:
            move_to_trash(root_id, rel_path, full, batch)
        except (OSError, shutil.Error) as e:
            failed.append({"path": rel_path, "error": str(e)})
            continue
        deleted.append({"root": root_id, "path": rel_path,
                        "files": plan["files"], "category": plan["category"]})
        notes.extend(apply_record_changes(plan, mode))
        tombstones.extend(plan.get("tombstones_written") or ())
        per_root.setdefault(root_id, []).append(rel_path)
    if tombstones:
        note_empty_files(tombstones)

    # A trashed file must stop being counted in the character pools and the
    # people tiles at once - those read the AI index directly and never see
    # the filesystem, so nothing else would tell them.
    ai_index_trashed([(d["root"], d["path"]) for d in deleted], True)

    for root_id, paths in per_root.items():
        manifest = {
            "batch": batch,
            "mode": mode,
            "deleted_at": time.time(),
            "deleted_by": user["username"],
            "items": paths,
        }
        if reason:
            manifest["reason"] = reason
        write_batch_manifest(root_id, batch, manifest)

    invalidate_media_caches()
    return batch, deleted, notes, failed


def ai_index_trashed(items, trashed=True):
    """Tell the AI index that these files went to the trash, or came back.

    Best-effort on purpose: the file has already moved by the time this runs,
    and an index that cannot be opened must not turn a completed delete into
    an error. The worst case is a stale row, which is what this whole function
    exists to reduce.
    """
    if not items:
        return
    try:
        media_ai.set_trashed(media_ai.connect(BASE_DIR), items, trashed)
        discovery_service().invalidate()
    except Exception as exc:
        print(f"[Warning] AI index not updated for trash: {exc}")


def ai_index_forget(items):
    """The purge case: the bytes are gone, so the captions, tags and faces
    describing them are worth nothing. Tags and faces cascade."""
    if not items:
        return
    try:
        media_ai.forget(media_ai.connect(BASE_DIR), items)
    except Exception as exc:
        print(f"[Warning] AI index not purged: {exc}")


def trash_root_for(root_id):
    root = get_root(root_id)
    return os.path.join(root["path"], TRASH_DIR_NAME) if root else None

def _unique_target(path):
    """A free path next to `path`, so restoring or trashing twice never
    silently overwrites the earlier copy."""
    if not os.path.exists(path):
        return path
    stem, ext = os.path.splitext(path)
    for i in range(2, 10000):
        candidate = f"{stem} ({i}){ext}"
        if not os.path.exists(candidate):
            return candidate
    return f"{stem} ({secrets.token_hex(4)}){ext}"

def move_to_trash(root_id, rel_path, full_path, batch):
    """Moves one file or folder into <root>/.trash/<batch>/, keeping its
    original relative path so a restore knows where it came from."""
    base = trash_root_for(root_id)
    if not base:
        return None
    dest = os.path.join(base, batch, rel_path.replace("/", os.sep))
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    dest = _unique_target(dest)
    shutil.move(full_path, dest)
    return dest

def _batch_manifest_path(root_id, batch):
    base = trash_root_for(root_id)
    return os.path.join(base, batch, "_manifest.json") if base else None

def write_batch_manifest(root_id, batch, data):
    path = _batch_manifest_path(root_id, batch)
    if not path:
        return
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
    except OSError:
        pass

def list_trash_batches(root_id):
    """Newest first. A batch with no manifest is still listed - it can be
    restored from what is actually in the folder."""
    base = trash_root_for(root_id)
    out = []
    if not base or not os.path.isdir(base):
        return out
    for name in os.listdir(base):
        folder = os.path.join(base, name)
        if not os.path.isdir(folder):
            continue
        manifest = {}
        try:
            with open(os.path.join(folder, "_manifest.json"), encoding="utf-8") as f:
                manifest = json.load(f)
        except (OSError, ValueError):
            pass
        size = 0
        count = 0
        for b, _d, fs in os.walk(folder):
            for fn in fs:
                if fn == "_manifest.json":
                    continue
                count += 1
                try:
                    size += os.path.getsize(os.path.join(b, fn))
                except OSError:
                    pass
        out.append({
            "batch": name,
            "root": root_id,
            "deleted_at": manifest.get("deleted_at"),
            "mode": manifest.get("mode"),
            "deleted_by": manifest.get("deleted_by"),
            "reason": manifest.get("reason"),
            "items": manifest.get("items") or [],
            "file_count": count,
            "size": size,
        })
    out.sort(key=lambda b: b.get("deleted_at") or 0, reverse=True)
    return out

@app.route("/api/media/delete", methods=["POST"])
@require_admin
def delete_media():
    """Deletes files and/or folders. Body:

        {"items": [{"root": "...", "path": "..."}, ...],
         "mode": "release" | "ban"}

    "release" also forgets the download record, so the same URL will fetch
    the file again. "ban" makes sure it cannot come back. Nothing is
    unlinked - everything moves to <root>/.trash/<batch>/ - so a mistaken
    selection is recoverable until the trash is emptied.

    Admin-only: this is the one destructive operation in the app, and guests
    are read-only everywhere else."""
    data = request.get_json(silent=True) or {}
    items = data.get("items") or []
    mode = (data.get("mode") or "").strip().lower()
    user = get_authenticated_user()

    if mode not in (DELETE_RELEASE, DELETE_BAN):
        return jsonify({"error": "mode must be 'release' or 'ban'"}), 400
    if not isinstance(items, list) or not items:
        return jsonify({"error": "items is required"}), 400
    if len(items) > 2000:
        return jsonify({"error": "Too many items in one delete (max 2000)"}), 400

    # Resolve and authorise everything BEFORE moving anything: a batch that
    # half-applies because item 40 was invalid is far worse than one that
    # refuses outright.
    resolved = []
    for item in items:
        root_id = (item.get("root") or DEFAULT_ROOT_ID).strip()
        rel_path = (item.get("path") or "").strip("/").replace("\\", "/")
        if not rel_path:
            return jsonify({"error": "every item needs a path"}), 400
        if not root_visible_to(user, root_id):
            return jsonify({"error": f"Forbidden: {rel_path}"}), 403
        full = resolve_media_path(root_id, rel_path)
        if not full:
            return jsonify({"error": f"Invalid path: {rel_path}"}), 400
        if not os.path.exists(full):
            return jsonify({"error": f"Not found: {rel_path}"}), 404
        # The trash lives inside the root; deleting it through this endpoint
        # would recurse it into itself.
        if rel_path.split("/")[0] == TRASH_DIR_NAME:
            return jsonify({"error": "The trash cannot be deleted from here"}), 400
        resolved.append((root_id, rel_path, full))

    batch, deleted, notes, failed = trash_resolved(resolved, mode, user)

    # Collapse the per-item notes into one line per kind of record.
    summary = {}
    for note in notes:
        count, _, label = note.partition(" ")
        try:
            summary[label] = summary.get(label, 0) + int(count)
        except ValueError:
            summary[note] = 0
    return jsonify({
        "batch": batch,
        "mode": mode,
        "deleted": deleted,
        "failed": failed,
        "records": [f"{n} {label}" if n else label for label, n in summary.items()],
    })

# -------------------------------------------------------------------
# Manual upload (media.html "Upload"; server_patch_upload.py)
# -------------------------------------------------------------------
#
# Owner, 2026-11-01: "a place to manually upload files to specific locations".
# Admin only. POST /api/media/upload takes a multipart body with the fields
# root and path (the target folder, created when missing) and then, in this
# order for every file:
#
#     [cid]  [rel]  [mtime]  [size]   files
#
# cid   a client-chosen id, stable across retries: a file the server already
#       saved under that id is not saved again (a batch that broke off after
#       three files and is sent again must not leave "name (2)" copies)
# rel   the sub-folder under path the file belongs in (a dropped folder)
# mtime the file's own modified time in ms, stamped on it the way the
#       downloaders stamp the source's date (the "Created" date of a file)
# size  the byte count the browser says it sends; a shorter file is refused
#
# The body is NOT parsed by Flask: request.files would spool every file to
# the system temp directory first (a 20 GiB request in /tmp), so the body is
# read straight off the socket with werkzeug's multipart decoder and written
# into a hidden temp file in the target folder (".upload-<hex>.part" - every
# listing, the indexer and the dedup walk skip dot-names and *.part), then
# moved into place once complete. Nothing is overwritten: a taken name becomes
# "name (2).ext". The one exception is a 0-byte file at the very path - what a
# ban leaves behind - which is replaced exactly as a trash restore replaces it.
#
# What happens to a saved file afterwards is _upload_after_save: the same calls
# execute_download_task makes after a finished task (recent list, volume,
# cache reset, Twitter stitch, video trim), plus the two things a download
# gets lazily (the dimensions queue) and the exact-copy check, which here only
# REPORTS: an uploaded file is never trashed or linked by the upload itself.
UPLOAD_MAX_REQUEST_BYTES = 20 * 1024 ** 3     # one request's Content-Length
UPLOAD_MIN_FREE_BYTES = 5 * 1024 ** 3         # refuse when less than this would remain
UPLOAD_MAX_FILES_PER_REQUEST = 2000
UPLOAD_READ_CHUNK = 1024 * 1024
UPLOAD_FREE_CHECK_EVERY = 256 * 1024 ** 2     # bytes written between free-space checks
UPLOAD_DISCARD_LIMIT = 2 * 1024 ** 3          # an early refusal reads at most this much of the body first
UPLOAD_NAME_BYTES = 200                       # leaves room for " (99)" and any filesystem's 255
UPLOAD_SEEN_MAX = 5000
# What the server already treats as media (MEDIA_EXTENSIONS), less what is refused on purpose:
# an .svg can carry script and would be served from this origin.
UPLOAD_REFUSED_EXTS = frozenset({".svg"})
UPLOAD_ALLOWED_EXTS = frozenset(e for exts in MEDIA_EXTENSIONS.values() for e in exts) - UPLOAD_REFUSED_EXTS
UPLOAD_TEMP_PREFIX = ".upload-"
UPLOAD_TEMP_SUFFIX = ".part"
UPLOAD_SYSTEM_DIRS = frozenset({"lost+found", "system volume information", "$recycle.bin",
                                "@eadir", "#recycle", "#snapshot"})
_UPLOAD_BAD_CHARS = re.compile(r'[\x00-\x1f\x7f<>:"|?*\\/]')
_UPLOAD_RESERVED = re.compile(r"^(?:con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?$", re.IGNORECASE)
_upload_place_lock = threading.Lock()
_upload_seen = {}                             # (username, cid) -> the saved result, oldest first
_upload_seen_lock = threading.Lock()


def _upload_log(text):
    """print(), but a name the console's encoding cannot show must not fail an upload that already succeeded."""
    try:
        print(text, flush=True)
    except Exception:
        print(text.encode("ascii", "replace").decode("ascii"), flush=True)


class _UploadStop(Exception):
    """One file cannot go on (disk nearly full, ...); the message goes to its result."""


def _upload_clean_name(raw):
    """One path component made safe, keeping its wording (unicode, spaces, @).
    None when nothing usable is left. No directories, no control characters, no
    hidden names (leading dots), none of the characters sanitize_folder_name also
    drops (they are replaced by "_" here so the words stay apart), no Windows
    device names and no trailing dot or space (the library may be shared by SMB)."""
    name = unicodedata.normalize("NFC", str(raw or "")).replace("\\", "/").rsplit("/", 1)[-1]
    name = _UPLOAD_BAD_CHARS.sub("_", name).strip().lstrip(".").strip().rstrip(". ")
    if not name:
        return None
    if _UPLOAD_RESERVED.match(name):
        name = "_" + name
    stem, ext = os.path.splitext(name)
    while stem and len((stem + ext).encode("utf-8")) > UPLOAD_NAME_BYTES:
        stem = stem[:-1]
    return (stem + ext) if stem else None


def _upload_clean_dir(raw, strict):
    """A relative folder path as a list of safe components. strict (a typed
    destination): anything that would have to be changed is an error. Not strict
    (the sub-folders of a dropped folder): components are cleaned like file names.
    ValueError with a sentence for the user otherwise."""
    parts = []
    for seg in str(raw or "").replace("\\", "/").split("/"):
        seg = seg.strip()
        if seg in ("", "."):
            continue
        if seg == "..":
            raise ValueError("'..' is not allowed in a folder path")
        if seg.startswith("."):
            raise ValueError(f"hidden folders are not allowed ('{seg}')")
        if seg.lower() in UPLOAD_SYSTEM_DIRS:
            raise ValueError(f"'{seg}' is a system folder")
        cleaned = _upload_clean_name(seg)
        if cleaned is None or (strict and cleaned != unicodedata.normalize("NFC", seg)):
            raise ValueError(f"'{seg}' is not a valid folder name")
        parts.append(cleaned)
    return parts


def _upload_disk_free(path):
    """Free bytes on the volume that holds `path` (or its nearest existing parent)."""
    probe = path
    while probe and not os.path.exists(probe):
        parent = os.path.dirname(probe)
        if parent == probe:
            break
        probe = parent
    try:
        return shutil.disk_usage(probe).free
    except OSError:
        return None


def _upload_destination(root_id, raw_path, user):
    """-> (root, base dir on disk, relative parts). ValueError with a sentence."""
    root_id = (root_id or DEFAULT_ROOT_ID).strip()
    if not root_visible_to(user, root_id):
        raise ValueError("Unknown library")
    root = get_root(root_id)
    if not root or not os.path.isdir(root["path"]):
        raise ValueError("That library's folder does not exist on the server")
    parts = _upload_clean_dir(raw_path, strict=True)
    base = resolve_media_path(root_id, "/".join(parts))
    if not base:
        raise ValueError("Invalid path")
    probe = base
    while not os.path.exists(probe):
        probe = os.path.dirname(probe)
    if not os.path.isdir(probe):
        raise ValueError("Part of that path is a file, not a folder")
    if not os.access(probe, os.W_OK):
        raise ValueError("That folder is read-only for the server")
    return root, base, parts


def _upload_place(tmp, dirpath, name):
    """Moves a finished temp file to dirpath/name, or "name (2).ext" when that is
    taken; never overwrites. -> (final path, replaced a 0-byte stand-in)."""
    stem, ext = os.path.splitext(name)
    with _upload_place_lock:
        for i in range(1, 10000):
            target = os.path.join(dirpath, name if i == 1 else f"{stem} ({i}){ext}")
            try:
                existing = os.lstat(target)
            except FileNotFoundError:
                existing = None
            if existing is not None:
                if i == 1 and existing.st_size == 0 and os.path.isfile(target):
                    os.replace(tmp, target)       # the ban's stand-in, as a trash restore does
                    return target, True
                continue
            try:
                os.link(tmp, target)              # atomic, and refuses to overwrite
                try:
                    os.unlink(tmp)
                except OSError:
                    pass                          # the hidden temp name stays: harmless
                return target, False
            except FileExistsError:
                continue
            except OSError:                       # a filesystem without hard links
                os.replace(tmp, target)
                return target, False
    raise OSError("no free file name")


def _upload_discard(stream, limit=UPLOAD_DISCARD_LIMIT):
    """Reads and drops (up to `limit` bytes of) what is left of the body, so a client
    that is still sending can read the answer instead of a reset connection."""
    left = limit
    try:
        while left > 0:
            chunk = stream.read(min(UPLOAD_READ_CHUNK, left))
            if not chunk:
                return
            left -= len(chunk)
    except Exception:
        pass


def _upload_refuse(status, message, **extra):
    resp = jsonify(dict(error=message, **extra))
    resp.status_code = status
    resp.headers["Connection"] = "close"          # the body was not read: the connection cannot be reused
    return resp


def _upload_exact_copies(saved):
    """{path: [other names]} for uploaded pictures that are byte-identical to another
    picture - the rule and the calls of _idd_download_pass (same scope: pictures under
    the downloader folders of the main library, same size scan, same hasher, same
    plan), but only the plan is read: nothing is trashed, linked or tombstoned."""
    found = {}
    if not image_dedup_enabled():
        return found
    wanted = sorted({s["path"] for s in saved
                     if s["root"] == DEFAULT_ROOT_ID
                     and s["path"].split("/")[0] in image_dedup.CATEGORIES
                     and _idd_accept(os.path.basename(s["path"]))})
    if not wanted:
        return found
    store = _idd_store()
    entries = {}
    for folder in sorted({r.rsplit("/", 1)[0] for r in wanted}):
        listed = image_dedup.scan_folder(DOWNLOADS_DIR, folder, _idd_accept)
        store.replace_folder(folder, listed)
        entries.update((e.rel, e) for e in listed)
    new = {r for r in wanted if r in entries}
    for size in {entries[r].size for r in new}:
        for c in store.with_size(size):
            if c.rel in entries:
                continue
            try:
                s = os.lstat(os.path.join(DOWNLOADS_DIR, *c.rel.split("/")))
            except OSError:
                continue
            if s.st_size == c.size:
                entries[c.rel] = image_dedup.Entry(c.rel, s.st_size, s.st_mtime_ns, s.st_dev, s.st_ino, s.st_nlink)
    hasher = image_dedup.Hasher(store, DOWNLOADS_DIR)
    plan = image_dedup.build_plan(list(entries.values()), hasher, store.exempt_all())
    for g in plan["groups"]:
        names = ({g["canon"].rel} | {e.rel for e in g["links"]} | {e.rel for e in g["trash_links"]}
                 | {e.rel for e, _keeper in g["trash"]})
        for rel in names & new:
            found[rel] = sorted(names - {rel})
    if found:
        # A later library pass must not trash a file the owner uploaded on purpose.
        image_dedup_exempt([(DEFAULT_ROOT_ID, rel) for rel in found])
    return found


def _upload_after_save(saved, who):
    """What a finished download gets (execute_download_task), for files an upload
    saved: [{"root","path","size"}, ...]. Every step on its own, so one failing
    leaves the others. -> {step: "ok" | "skipped: why" | "failed: why"}, and
    "duplicates" {path: [other names]}."""
    steps = {}
    if not saved:
        return steps
    new_paths = [{"root": s["root"], "path": s["path"]} for s in reversed(saved)]     # newest first

    def run(name, fn):
        try:
            out = fn()
            steps[name] = out if isinstance(out, str) else "ok"
        except Exception as exc:
            steps[name] = f"failed: {exc}"
            _upload_log(f"[Warning] upload: step '{name}' failed: {exc}")

    # 1-3: as in execute_download_task, in the same order.
    run("recent", lambda: recent_media_store.record_recent_media(BASE_DIR, new_paths))
    run("volume", lambda: record_download_volume(sum(s["size"] for s in saved)))
    run("caches", invalidate_media_caches)       # search index, recent page, folder stats, music catalogue (follows the index)
    # 4: a download waits for a listing to queue these; an upload says so at once.
    def dims():
        items = []
        for s in saved:
            if media_dimensions.is_video_name(s["path"]) or media_dimensions.is_image_name(s["path"]):
                full = resolve_media_path(s["root"], s["path"])
                if full:
                    items.append((media_dimensions.key_for(s["root"], s["path"]), full))
        return f"queued {queue_dimensions(items)}" if items else "skipped: no pictures or videos"
    run("dimensions", dims)
    # 5: Twitter posts cut into pieces are joined (post_stitch.py); the function only acts on
    # twitter/<account>/<id>_<n> names in the main library, so it is called for those alone.
    def stitch():
        mine = [p for p in new_paths if p["root"] == DEFAULT_ROOT_ID and p["path"].startswith("twitter/")]
        if not mine:
            return "skipped: no twitter/ files"
        if not twitter_stitch_enabled():
            return "skipped: switched off in settings"
        stitch_after_download(mine)
    run("stitch", stitch)
    # 6: exact copies - looked for like a download's, reported instead of trashed.
    def dedup():
        found = _upload_exact_copies(saved)
        steps["duplicates"] = found
        return f"{len(found)} exact copies found" if found else "ok"
    run("exact_copies", dedup)
    # 7: repeating intros and outros are looked for in folders that got a new video (video_trim.py).
    run("video_trim", lambda: _video_trim.enqueue_paths(new_paths))
    return steps


# -------------------------------------------------------------------
# Delete a whole account (server_patch_account.py)
# -------------------------------------------------------------------
ACCOUNT_SOURCES = {"twitter", "pixiv", "instagram", "bluesky", "deviantart", "patreon", "civitai",
                   "bilibili", "youtube"}


def _account_target(root_id, rel_path):
    """(source, name, full) for <source>/<account> in the downloads root, else (None, message, None)."""
    root_id = (root_id or DEFAULT_ROOT_ID).strip()
    rel = (rel_path or "").strip("/").replace("\\", "/")
    parts = rel.split("/")
    if root_id != DEFAULT_ROOT_ID or len(parts) != 2 or parts[0] not in ACCOUNT_SOURCES or not parts[1]:
        return None, "That folder is not an account (expected <source>/<account> in the downloads library)", None
    full = resolve_media_path(root_id, rel)
    if not full or not os.path.isdir(full):
        return None, "Account folder not found", None
    return parts[0], parts[1], full


def _account_profile_handles(source, name):
    bare = name.lstrip("@")
    handles = {name, bare, "@" + bare}
    if source == "pixiv" and name.endswith(")") and "(" in name:
        handles.add(name[name.rindex("(") + 1:-1])
    return sorted(handles)


def _account_stats(full):
    files = collect_deletable_files(full)
    size = 0
    for f in files:
        try:
            size += os.path.getsize(f)
        except OSError:
            pass
    return len(files), size


@app.route("/api/media/account/preview", methods=["GET"])
@require_admin
def account_preview():
    source, name, full = _account_target(request.args.get("root"), request.args.get("path"))
    if source is None:
        return jsonify({"error": name}), 400
    files, size = _account_stats(full)
    profile = None
    try:
        conn = media_ai.connect(BASE_DIR)
        handles = _account_profile_handles(source, name)
        row = conn.execute("SELECT kind FROM profiles WHERE source = ? AND handle IN (%s)" % ",".join("?" * len(handles)),
                           (source, *handles)).fetchone()
        profile = row["kind"] if row else None
    except Exception as exc:
        print(f"[Warning] account profile lookup failed: {exc}")
    return jsonify({"source": source, "name": name, "files": files, "bytes": size, "profile": profile})


@app.route("/api/media/account/delete", methods=["POST"])
@require_admin
def account_delete():
    data = request.get_json(silent=True) or {}
    if data.get("confirm") is not True:
        return jsonify({"error": "confirm is required"}), 400
    mode = (data.get("mode") or "").strip().lower()
    if mode not in (DELETE_RELEASE, DELETE_BAN):
        return jsonify({"error": "mode must be 'release' or 'ban'"}), 400
    root_id = (data.get("root") or DEFAULT_ROOT_ID).strip()
    source, name, full = _account_target(root_id, data.get("path"))
    if source is None:
        return jsonify({"error": name}), 400
    files, _size = _account_stats(full)
    expected = data.get("expected_files")
    if isinstance(expected, int) and expected != files:
        return jsonify({"error": f"The account holds {files} files now, {expected} were previewed.",
                        "code": "count_changed", "files": files}), 409
    user = get_authenticated_user()
    rel = f"{source}/{name}"
    batch, deleted, notes, failed = trash_resolved([(root_id, rel, full)], mode, user,
                                                   reason=f"Account deleted: {source}/{name}")
    if failed:
        return jsonify({"error": failed[0].get("error") or "The account could not be moved", "failed": failed}), 500
    removed_profiles = 0
    try:
        conn = media_ai.connect(BASE_DIR)
        handles = _account_profile_handles(source, name)
        cur = conn.execute("DELETE FROM profiles WHERE source = ? AND handle IN (%s)" % ",".join("?" * len(handles)),
                           (source, *handles))
        removed_profiles = cur.rowcount
        conn.commit()
    except Exception as exc:
        print(f"[Warning] account profile not removed: {exc}")
    return jsonify({"batch": batch, "mode": mode, "files": files, "records": notes, "profile_removed": bool(removed_profiles)})


@app.route("/api/media/upload/limits", methods=["GET"])
@require_admin
def media_upload_limits():
    """What an upload may be, and the room left, so the page can say so before anything is sent."""
    user = get_authenticated_user()
    root_id = (request.args.get("root") or DEFAULT_ROOT_ID).strip()
    if not root_visible_to(user, root_id):
        return jsonify({"error": "Unknown library"}), 404
    root = get_root(root_id)
    target = resolve_media_path(root_id, request.args.get("path", "")) or (root or {}).get("path")
    free = _upload_disk_free(target) if target else None
    probe = target
    while probe and not os.path.exists(probe):
        probe = os.path.dirname(probe)
    kinds = {kind: sorted(e for e in exts if e in UPLOAD_ALLOWED_EXTS) for kind, exts in MEDIA_EXTENSIONS.items()}
    return jsonify({
        "root": root_id,
        "exists": bool(root and os.path.isdir(root["path"])),
        "writable": bool(probe and os.access(probe, os.W_OK)),
        "max_request_bytes": UPLOAD_MAX_REQUEST_BYTES,
        "min_free_bytes": UPLOAD_MIN_FREE_BYTES,
        "free_bytes": free,
        "max_files_per_request": UPLOAD_MAX_FILES_PER_REQUEST,
        "extensions": kinds,
        "refused_extensions": sorted(UPLOAD_REFUSED_EXTS),
        # Files saved under these folders of the main library get the downloader's automatic
        # rules too (Twitter stitch, the profile review queue): the page says so.
        "downloader_folders": sorted(set(ALL_CATEGORIES) | {"youtube_music"}),
    })


@app.route("/api/media/upload", methods=["POST"])
@require_admin
def media_upload():
    """Saves uploaded files. Multipart, fields in the order described above this block.
    200 with {"results": [{name, saved_as, path, size, ok, error, notes}, ...], ...} even when
    some files failed; 4xx/5xx only when nothing could be started."""
    try:
        from werkzeug.sansio.multipart import Data, Epilogue, Field, File, MultipartDecoder, NeedData
    except ImportError:
        return _upload_refuse(500, "This server's werkzeug is too old for streaming uploads (needs 2.0+)")
    user = get_authenticated_user()
    who = (user or {}).get("username") or "?"
    boundary = request.mimetype_params.get("boundary")
    if request.mimetype != "multipart/form-data" or not boundary:
        return _upload_refuse(400, "Expected a multipart/form-data upload")
    length = request.content_length
    if length is None:
        return _upload_refuse(411, "Content-Length is required")
    if length > UPLOAD_MAX_REQUEST_BYTES:
        return _upload_refuse(413, f"Too large: {length} bytes in one request, the limit is {UPLOAD_MAX_REQUEST_BYTES}",
                              max_request_bytes=UPLOAD_MAX_REQUEST_BYTES)
    stream = request.stream
    decoder = MultipartDecoder(boundary.encode("ascii", "ignore"))

    fields, per_file = {}, {}
    dest = None                    # (root, base dir, parts), once the first file arrives
    dest_error = None
    field = None                   # [name, bytearray] while a plain field is being read
    cur = None                     # the file being read
    results, saved = [], []
    made_dirs = set()
    stop_all = None                # a reason that refuses every file still to come (disk nearly full)
    interrupted = None

    def finish_file(c):
        """The last byte of a file arrived: verify, stamp, place."""
        res = c["result"]
        if c["fh"] is not None:
            c["fh"].close()
            c["fh"] = None
        if c["written"] == 0:
            raise _UploadStop("The file is empty")      # a 0-byte file is what the library reads as a ban's stand-in
        if c["expect"] is not None and c["written"] != c["expect"]:
            raise _UploadStop(f"Received {c['written']} of {c['expect']} bytes")
        if c["mtime"]:
            try:
                os.utime(c["tmp"], (time.time(), c["mtime"]))
            except OSError:
                pass
        final, replaced = _upload_place(c["tmp"], c["dir"], c["name"])
        c["tmp"] = None
        rel = "/".join(p for p in c["parts"] + [os.path.basename(final)] if p)
        res.update(ok=True, saved_as=os.path.basename(final), path=rel, size=c["written"])
        if os.path.basename(final) != c["name"]:
            res["notes"].append("name taken: saved as " + os.path.basename(final))
        if c["name"] != c["asked"]:
            res["notes"].append("name changed to be safe: " + c["name"])
        if replaced:
            res["notes"].append("replaced an empty stand-in left by a ban")
            note_empty_files([(c["root"], rel)], empty=False)
        saved.append({"root": c["root"], "path": rel, "size": c["written"]})
        if c["cid"]:
            with _upload_seen_lock:
                _upload_seen[(who, c["cid"])] = {"saved_as": res["saved_as"], "path": rel, "size": c["written"]}
                while len(_upload_seen) > UPLOAD_SEEN_MAX:
                    _upload_seen.pop(next(iter(_upload_seen)))

    def abandon(c, message):
        """This file failed: drop its temp file, say why."""
        if c["fh"] is not None:
            try:
                c["fh"].close()
            except OSError:
                pass
            c["fh"] = None
        if c["tmp"]:
            try:
                os.unlink(c["tmp"])
            except OSError:
                pass
            c["tmp"] = None
        if not c["result"]["error"] and not c["result"]["ok"]:
            c["result"]["error"] = message
        c["discard"] = True

    def begin_file(event):
        nonlocal dest, dest_error, stop_all
        asked = event.filename or ""
        res = {"name": asked, "saved_as": None, "path": None, "size": 0, "ok": False, "error": None, "notes": []}
        results.append(res)
        meta = dict(per_file)
        per_file.clear()
        c = {"result": res, "fh": None, "tmp": None, "discard": True, "written": 0, "since_check": 0,
             "asked": asked, "name": None, "dir": None, "parts": [], "root": None,
             "cid": (meta.get("cid") or "").strip()[:80], "expect": None, "mtime": None}
        try:
            c["expect"] = int(meta["size"]) if meta.get("size", "").strip() else None
        except ValueError:
            c["expect"] = None
        try:
            ms = float(meta.get("mtime") or 0)
            if 315532800000 < ms < (time.time() + 86400) * 1000:       # 1980 .. tomorrow
                c["mtime"] = ms / 1000.0
        except ValueError:
            pass
        if event.name != "files":
            res["error"] = f"Unexpected form part '{event.name}'"
            return c
        if len(results) > UPLOAD_MAX_FILES_PER_REQUEST:
            res["error"] = f"More than {UPLOAD_MAX_FILES_PER_REQUEST} files in one request"
            return c
        if dest is None and dest_error is None:
            try:
                dest = _upload_destination(fields.get("root"), fields.get("path"), user)
            except ValueError as exc:
                dest_error = str(exc)
        if dest_error:
            res["error"] = dest_error
            return c
        if stop_all:
            res["error"] = stop_all
            return c
        if c["cid"]:
            with _upload_seen_lock:
                before = _upload_seen.get((who, c["cid"]))
            if before:
                res.update(ok=True, saved_as=before["saved_as"], path=before["path"], size=before["size"])
                res["notes"].append("already uploaded earlier: not saved again")
                return c
        name = _upload_clean_name(asked)
        if not name:
            res["error"] = "The file name is not usable"
            return c
        ext = os.path.splitext(name)[1].lower()
        if ext in UPLOAD_REFUSED_EXTS:
            res["error"] = f"{ext} files are not accepted (they can carry script)"
            return c
        if ext not in UPLOAD_ALLOWED_EXTS:
            res["error"] = f"The library does not list {ext or 'files without an extension'}: not accepted"
            return c
        root, base, parts = dest
        try:
            sub = _upload_clean_dir(meta.get("rel"), strict=False)
        except ValueError as exc:
            res["error"] = str(exc)
            return c
        free = _upload_disk_free(base)
        if free is not None and free < UPLOAD_MIN_FREE_BYTES:
            stop_all = f"Not enough room: {free} bytes free, at least {UPLOAD_MIN_FREE_BYTES} must stay free"
            res["error"] = stop_all
            return c
        folder = os.path.join(base, *sub) if sub else base
        try:
            if folder not in made_dirs:
                os.makedirs(folder, exist_ok=True)
                made_dirs.add(folder)
            tmp = os.path.join(folder, UPLOAD_TEMP_PREFIX + secrets.token_hex(8) + UPLOAD_TEMP_SUFFIX)
            c["fh"] = open(tmp, "wb")
            c["tmp"] = tmp
        except OSError as exc:
            res["error"] = f"Could not write there: {exc.strerror or exc}"
            return c
        c.update(name=name, dir=folder, parts=parts + sub, root=root["id"], discard=False)
        return c

    try:
        eof = False
        while not eof:
            chunk = stream.read(UPLOAD_READ_CHUNK)
            eof = not chunk
            decoder.receive_data(None if eof else chunk)
            event = decoder.next_event()
            while not isinstance(event, (Epilogue, NeedData)):
                if isinstance(event, Field):
                    field = [event.name, bytearray()]
                elif isinstance(event, File):
                    cur = begin_file(event)
                elif isinstance(event, Data):
                    if field is not None:
                        if len(field[1]) < 65536:
                            field[1].extend(event.data[:65536 - len(field[1])])
                        if not event.more_data:
                            value = bytes(field[1]).decode("utf-8", "replace")
                            if field[0] in ("cid", "rel", "mtime", "size"):
                                per_file[field[0]] = value
                            else:
                                fields[field[0]] = value
                            field = None
                    elif cur is not None:
                        if not cur["discard"] and event.data:
                            try:
                                cur["fh"].write(event.data)
                                cur["written"] += len(event.data)
                                cur["since_check"] += len(event.data)
                                if cur["since_check"] >= UPLOAD_FREE_CHECK_EVERY:
                                    cur["since_check"] = 0
                                    free = _upload_disk_free(cur["dir"])
                                    if free is not None and free < UPLOAD_MIN_FREE_BYTES:
                                        stop_all = f"The disk is nearly full ({free} bytes free): stopped"
                                        raise _UploadStop(stop_all)
                            except _UploadStop as exc:
                                abandon(cur, str(exc))          # the rest of this file is read and dropped
                            except OSError as exc:
                                abandon(cur, f"Write failed: {exc.strerror or exc}")
                        if not event.more_data:
                            finished, cur = cur, None
                            try:
                                if not finished["discard"]:
                                    finish_file(finished)
                            except _UploadStop as exc:
                                abandon(finished, str(exc))
                            except OSError as exc:
                                abandon(finished, f"Could not save: {exc.strerror or exc}")
                event = decoder.next_event()
    except Exception as exc:
        interrupted = f"{type(exc).__name__}: {exc}"
        if cur is not None:
            abandon(cur, "The upload was interrupted before this file arrived completely")
            cur = None
        _upload_log(f"[Warning] upload by {who} interrupted: {interrupted}")

    if cur is not None:
        abandon(cur, "The upload ended before this file arrived completely")
    if not results and not interrupted:
        return _upload_refuse(400, "No files in the request")

    pipeline = {}
    try:
        pipeline = _upload_after_save(saved, who)
    except Exception as exc:
        _upload_log(f"[Warning] upload: post-save pipeline failed: {exc}")
        pipeline = {"error": str(exc)}
    duplicates = pipeline.pop("duplicates", {}) if isinstance(pipeline, dict) else {}
    for res in results:
        dup = duplicates.get(res.get("path") or "")
        if dup:
            res["duplicate_of"] = dup[:5]
            res["notes"].append("identical to " + ", ".join(dup[:3]) + (" and more" if len(dup) > 3 else "")
                                + " - kept, nothing was removed")

    bytes_saved = sum(s["size"] for s in saved)
    failed = sum(1 for r in results if not r["ok"])
    root_out = dest[0]["id"] if dest else (fields.get("root") or DEFAULT_ROOT_ID)
    path_out = "/".join(dest[2]) if dest else ""
    where = f"{root_out}:{path_out}"
    _upload_log(f"[Info] upload: {who} -> {where}: {len(saved)} saved ({bytes_saved} bytes), {failed} failed"
          + (f", interrupted ({interrupted})" if interrupted else ""))
    for res in results[:50]:
        if res["ok"]:
            _upload_log(f"[Info] upload:   {res['path']} ({res['size']} bytes)" + (" - " + "; ".join(res["notes"]) if res["notes"] else ""))
        else:
            _upload_log(f"[Info] upload:   refused {res['name']!r}: {res['error']}")
    return jsonify({
        "ok": not failed and not interrupted,
        "root": root_out,
        "path": path_out,
        "saved": len(saved),
        "failed": failed,
        "bytes": bytes_saved,
        "interrupted": interrupted,
        "results": results,
        "pipeline": pipeline,
    })


@app.route("/api/media/trash", methods=["GET"])
@require_admin
def list_trash():
    user = get_authenticated_user()
    batches = []
    for r in roots_for_user(user):
        batches.extend(list_trash_batches(r["id"]))
    batches.sort(key=lambda b: b.get("deleted_at") or 0, reverse=True)
    return jsonify(batches)

@app.route("/api/media/trash/<batch>/restore", methods=["POST"])
@require_admin
def restore_trash(batch):
    """Puts a batch back where it came from. A tombstone left by a ban is
    removed on the way, otherwise the restored file would land beside its
    own 0-byte stand-in."""
    user = get_authenticated_user()
    data = request.get_json(silent=True) or {}
    root_id = (data.get("root") or DEFAULT_ROOT_ID).strip()
    if not root_visible_to(user, root_id):
        return jsonify({"error": "Forbidden"}), 403
    if not re.match(r"^[0-9]{8}-[0-9]{6}-[0-9a-f]{6}$", batch or ""):
        return jsonify({"error": "Invalid batch"}), 400

    base = trash_root_for(root_id)
    folder = os.path.join(base, batch) if base else None
    if not folder or not os.path.isdir(folder):
        return jsonify({"error": "Not found"}), 404

    root = get_root(root_id)
    # Read before anything moves: a restore of a character-blacklist batch has to
    # exempt the files it brings back (see charbl_exempt).
    try:
        with open(_batch_manifest_path(root_id, batch), encoding="utf-8") as fh:
            batch_reason = (json.load(fh) or {}).get("reason") or ""
    except (OSError, ValueError):
        batch_reason = ""
    # `items` restores only those files and leaves the rest in the trash. One
    # review Apply writes hundreds of discards into a single batch, so rescuing
    # one file that slipped past must not bring back every other discard.
    only = data.get("items")
    if only is not None:
        if not isinstance(only, list) or not only:
            return jsonify({"error": "items must be a non-empty list"}), 400
        only = {str(p).replace("\\", "/").lstrip("/") for p in only}
    restored, failed = [], []
    for b, _dirs, files in os.walk(folder):
        for name in files:
            if name == "_manifest.json":
                continue
            src = os.path.join(b, name)
            rel = os.path.relpath(src, folder)
            if only is not None and rel.replace(os.sep, "/") not in only:
                continue
            dest = os.path.join(root["path"], rel)
            try:
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                # A ban wrote an empty stand-in here; drop it so the real
                # file goes back to its own name rather than beside it.
                if os.path.isfile(dest) and os.path.getsize(dest) == 0:
                    os.remove(dest)
                shutil.move(src, _unique_target(dest))
                restored.append(rel.replace(os.sep, "/"))
            except (OSError, shutil.Error) as e:
                failed.append({"path": rel.replace(os.sep, "/"), "error": str(e)})

    if only is not None:
        missing = sorted(only - set(restored) - {f["path"] for f in failed})
        failed.extend({"path": p, "error": "not in this batch"} for p in missing)
        if restored:
            # The manifest must stop listing what left, or a later restore or
            # purge of this batch would act on files that are no longer in it.
            try:
                with open(_batch_manifest_path(root_id, batch), encoding="utf-8") as fh:
                    manifest = json.load(fh) or {}
                gone = set(restored)
                manifest["items"] = [i for i in manifest.get("items") or [] if i not in gone]
                write_batch_manifest(root_id, batch, manifest)
            except (OSError, ValueError) as e:
                failed.append({"path": "_manifest.json", "error": str(e)})
        if not any(n != "_manifest.json" for _b, _d, fs in os.walk(folder) for n in fs):
            shutil.rmtree(folder, ignore_errors=True)
    elif not failed:
        shutil.rmtree(folder, ignore_errors=True)
    # Restoring brings the AI work back with the file - it was kept precisely
    # so a restore would not cost another pass over the GPU. A file that came
    # back under a different name (something already sat at its old one) will
    # not match and stays marked; the indexer picks it up as new, which is the
    # right answer for a path that genuinely changed.
    ai_index_trashed([(root_id, rel) for rel in restored], False)
    if restored and str(batch_reason).startswith(CHARBL_REASON):
        # User, 2026-10-18: files restored from a blacklist batch must stay.
        charbl_exempt([(root_id, rel) for rel in restored])
    if restored and str(batch_reason).startswith(IMGDEDUP_REASON):
        # The same for a name the picture de-duplication trashed.
        image_dedup_exempt([(root_id, rel) for rel in restored])
    # The tombstones those files replaced are gone.
    note_empty_files([(root_id, rel) for rel in restored], empty=False)
    invalidate_media_caches()
    return jsonify({"restored": restored, "failed": failed})

@app.route("/api/media/trash/<batch>", methods=["DELETE"])
@require_admin
def purge_trash(batch):
    """Empties one batch for good. The record changes a delete made are NOT
    undone here - purging is about disk space, and re-deciding whether
    something may be downloaded again is a separate act."""
    user = get_authenticated_user()
    root_id = (request.args.get("root") or DEFAULT_ROOT_ID).strip()
    if not root_visible_to(user, root_id):
        return jsonify({"error": "Forbidden"}), 403
    if not re.match(r"^[0-9]{8}-[0-9]{6}-[0-9a-f]{6}$", batch or ""):
        return jsonify({"error": "Invalid batch"}), 400
    if not purge_trash_batch(root_id, batch):
        return jsonify({"error": "Not found"}), 404
    return jsonify({"success": True})


def purge_trash_batch(root_id, batch, sync_vr=True):
    """Empties one batch for good; False when it does not exist."""
    base = trash_root_for(root_id)
    folder = os.path.join(base, batch) if base else None
    if not folder or not os.path.isdir(folder):
        return False
    # Read the paths back out of the batch BEFORE the tree goes: the manifest
    # is what says where these files used to live, and after the rmtree there
    # is nothing left to ask.
    gone = []
    manifest = os.path.join(folder, "_manifest.json")
    try:
        with open(manifest, encoding="utf-8") as fh:
            gone = [(root_id, rel) for rel in (json.load(fh) or {}).get("items", [])]
    except (OSError, ValueError):
        pass
    shutil.rmtree(folder, ignore_errors=True)
    ai_index_forget(gone)
    return True


TRASH_BATCH_RE = re.compile(r"^([0-9]{8}-[0-9]{6})-[0-9a-f]{6}$")
TRASH_PURGE_CHECK_S = 3600


def trash_batch_time(root_id, batch):
    """When a batch was trashed: its manifest's deleted_at, else the time in
    its name (local time, as the name was written). None if neither reads."""
    try:
        with open(_batch_manifest_path(root_id, batch), encoding="utf-8") as fh:
            stamp = (json.load(fh) or {}).get("deleted_at")
        if isinstance(stamp, (int, float)) and stamp > 0:
            return float(stamp)
    except (OSError, ValueError, TypeError):
        pass
    m = TRASH_BATCH_RE.match(batch)
    try:
        return time.mktime(time.strptime(m.group(1), "%Y%m%d-%H%M%S")) if m else None
    except ValueError:
        return None


def purge_old_trash(now=None):
    """Empties every trash batch older than trash_purge_days, in every root.
    Only what a delete already decided goes: the download records (archive
    lines, ban tombstones) were written when the files were trashed and are
    not touched, so a banned file still does not come back."""
    days = int(CONFIG.get("trash_purge_days") or 0)
    if days <= 0:
        return 0
    cutoff = (now or time.time()) - days * 86400
    due = []
    for root in get_all_roots():
        base = trash_root_for(root["id"])
        try:
            names = os.listdir(base) if base else []
        except OSError:
            continue
        for name in names:
            if not TRASH_BATCH_RE.match(name) or not os.path.isdir(os.path.join(base, name)):
                continue
            stamp = trash_batch_time(root["id"], name)
            if stamp is not None and stamp < cutoff:
                due.append((root["id"], name))
    if not due:
        return 0
    purged = sum(1 for root_id, name in due if purge_trash_batch(root_id, name, sync_vr=False))
    if purged:
        print(f"[trash] emptied {purged} batch(es) older than {days} days", flush=True)
        invalidate_media_caches()
    return purged


def _trash_purge_loop():
    time.sleep(120)             # let startup settle first
    while True:
        try:
            purge_old_trash()
        except Exception as exc:
            print(f"[Warning] trash purge: {exc}", flush=True)
        time.sleep(TRASH_PURGE_CHECK_S)


# ---- video dimension filters ------------------------------------------------
# Dimensions come from media_dimensions.json, which backfill_dimensions.py
# writes (videos and, since 2026-09-11, pictures). Held in memory because every filtered listing
# reads it; re-read when the file changes so a backfill running alongside the
# server becomes visible without a restart.
_dimensions_cache = {"data": {}, "checked_at": 0.0, "stamp": None, "loaded": False}
_dimensions_lock = threading.Lock()
_dimensions_load_lock = threading.Lock()
DIMENSIONS_RECHECK_S = 20.0


def media_dimensions_map():
    """The dimensions store, re-checked every 20 s and re-read when it changed.

    One load at a time, and never the empty map of a load still in progress.
    The old version marked the store checked BEFORE parsing its 40 MB, so every
    request arriving during the first parse after a restart got {} - a live
    share with a resolution filter then resolved to no files at all and cached
    that for a minute. Once something has loaded, a reload in progress serves
    the previous map rather than making requests wait for the parse.
    """
    with _dimensions_lock:
        if _dimensions_cache["loaded"] and (
                time.time() - _dimensions_cache["checked_at"] < DIMENSIONS_RECHECK_S
                or _dimensions_load_lock.locked()):
            return _dimensions_cache["data"]
    with _dimensions_load_lock:
        with _dimensions_lock:
            if (_dimensions_cache["loaded"]
                    and time.time() - _dimensions_cache["checked_at"] < DIMENSIONS_RECHECK_S):
                return _dimensions_cache["data"]
        try:
            st = os.stat(media_dimensions.store_path(BASE_DIR))
            # Size, not mtime: this filesystem repeats st_mtime_ns across
            # consecutive writes, so an mtime stamp would miss a rewrite.
            stamp = (st.st_size, st.st_ino)
        except OSError:
            stamp = None
        with _dimensions_lock:
            if _dimensions_cache["loaded"] and stamp == _dimensions_cache["stamp"]:
                _dimensions_cache["checked_at"] = time.time()
                return _dimensions_cache["data"]
        data = media_dimensions.load(BASE_DIR)
        with _dimensions_lock:
            _dimensions_cache.update(data=data, stamp=stamp, loaded=True,
                                     checked_at=time.time())
        return data


# ---- content level filter ----------------------------------------------------
# Safe / Suggestive / Explicit from the AI index: WD14's rating, lifted by
# confident nudity and sex tags (media_ai.content_levels says why). Three
# buckets over four levels, and several can be picked at once - Safe plus
# Suggestive is what "safe for work" means. A file the tagger has not rated
# matches none of them: a filter that let unknown files through as safe could
# not be trusted.
CONTENT_BUCKETS = {"safe": (0,), "suggestive": (1,), "explicit": (2, 3)}
CONTENT_OPTIONS = [
    {"key": "safe", "label": "Safe", "hint": "nothing suggestive"},
    {"key": "suggestive", "label": "Suggestive",
     "hint": "swimsuits, underwear, cleavage - no nudity"},
    {"key": "explicit", "label": "Explicit", "hint": "nudity or sex"},
]
CONTENT_BUCKET_OF = {level: key for key, levels in CONTENT_BUCKETS.items() for level in levels}
# Held in memory because every filtered listing asks. Building it is a pass
# over the tag table (seconds), so a stale map is answered at once while a
# background thread rebuilds it, at most this often. Only newly tagged files
# change it, and a file the tagger has not reached is unrated either way.
CONTENT_REFRESH_S = 600
_content_cache = {"levels": None, "counts": None, "built_at": 0.0}
_content_lock = threading.Lock()
_content_build_lock = threading.Lock()


def parse_content(raw):
    """The requested content buckets as a set of levels, or None for no
    content filter. Comma-separated like kind; unknown words are ignored, and
    nothing but unknown words is no filter - which a live share refuses."""
    if not raw:
        return None
    levels = set()
    for part in str(raw).split(","):
        levels.update(CONTENT_BUCKETS.get(part.strip().lower(), ()))
    return frozenset(levels) or None


def content_matches(levels, content, root_id, path):
    return levels.get((root_id, path)) in content


def _build_content_levels():
    try:
        levels = media_ai.content_levels(media_ai.connect(BASE_DIR))
    except Exception as exc:
        print(f"[Warning] content levels not built: {exc}")
        return None
    counts = dict.fromkeys(CONTENT_BUCKETS, 0)
    for level in levels.values():
        counts[CONTENT_BUCKET_OF[level]] += 1
    with _content_lock:
        _content_cache.update(levels=levels, counts=counts, built_at=time.time())
    return levels


def _refresh_content_levels():
    try:
        _build_content_levels()
    finally:
        _content_build_lock.release()


def content_levels_map(wait=True):
    """{(root, path): level 0-3} for every rated file.

    A map older than CONTENT_REFRESH_S is answered at once while a background
    thread rebuilds it. Before the first build, `wait` decides: a filtered
    listing waits for it (an empty map would hide everything), the filter
    panel's counts do not - they get None, and the build is started.
    """
    with _content_lock:
        levels, built_at = _content_cache["levels"], _content_cache["built_at"]
    if levels is not None and time.time() - built_at < CONTENT_REFRESH_S:
        return levels
    if levels is not None or not wait:
        if _content_build_lock.acquire(blocking=False):
            try:
                threading.Thread(target=_refresh_content_levels, daemon=True).start()
            except Exception:
                _content_build_lock.release()
        return levels
    with _content_build_lock:
        with _content_lock:
            if _content_cache["levels"] is not None:
                return _content_cache["levels"]
        levels = _build_content_levels()
    return levels if levels is not None else {}


def content_counts():
    """Files per bucket for the filter panel, or None while the first map is
    still being built - asking starts that build without waiting for it."""
    content_levels_map(wait=False)
    with _content_lock:
        counts = _content_cache["counts"]
    return dict(counts) if counts else None


def content_info(root_id, path):
    """The info panel's content row: the bucket, the level and what decided
    it, read fresh for this one file rather than from the map. None when the
    index cannot be read, which leaves the row out."""
    try:
        level, evidence = media_ai.content_evidence(media_ai.connect(BASE_DIR), root_id, path)
    except Exception as exc:
        print(f"[Warning] content level not read: {exc}")
        return None
    return {"bucket": CONTENT_BUCKET_OF.get(level), "level": level, "evidence": evidence[:4]}


# What the search box is asking about. Filename is the original behaviour and
# stays the default: it needs no index, it is instant, and it is what someone
# typing an artist's name means. The rest read what the vision models saw and
# are only as complete as the index behind them - which is why the client is
# told the coverage rather than left to guess why a search came back thin.
SEARCH_MODES = ("filename", "ocr", "context", "character", "all")
# The media_ai.SEARCH_COLUMNS key each AI search type reads.
AI_SEARCH_COLUMNS = {"ocr": "ocr", "context": "context", "character": "tags", "all": "ai"}


def ai_search_paths(query, mode, allowed_roots):
    """(root, path) pairs from the AI index for the non-filename modes.

    Returns a SET, because "all" unions it with the filename hits and a file
    matching both must appear once. Roots are filtered here rather than after
    paging: a guest must not be able to learn that a file exists in a root
    they cannot see by counting results.
    """
    if mode == "filename":
        return set()
    column_mode = AI_SEARCH_COLUMNS.get(mode)
    if not column_mode:
        return set()
    try:
        conn = media_ai.connect(BASE_DIR)
        hits = media_ai.search(conn, query, column_mode)
    except Exception as exc:
        # A missing or busy index must not take the search box down with it;
        # filename results still come back and the mode simply finds nothing.
        print(f"[Warning] AI search unavailable: {exc}")
        return set()
    return {(r, p) for r, p in hits if r in allowed_roots}


def parse_media_filters(args):
    """The active filters from a request, or all-None if none are set.

    Three resolution families that share one set of numbers and differ only
    in the edge they measure, plus a frame rate floor and ceiling - see
    media_dimensions.matches() - and the content level (parse_content).
    """
    orientation = (args.get("orientation") or "").strip().lower()
    if orientation not in ("horizontal", "vertical", "square"):
        orientation = None

    def pick(name, table):
        raw = (args.get(name) or "").strip().lower()
        return table.get(raw)

    std = media_dimensions.STANDARDS
    # New filters are APPENDED: QUERY_SHARE_FILTER_KEYS is zipped against this
    # order.
    return (orientation, pick("min_hres", std), pick("min_vres", std),
            pick("min_res", std), parse_kinds(args.get("kind")),
            pick("min_fps", media_dimensions.MIN_FRAME_RATES),
            pick("max_fps", media_dimensions.MAX_FRAME_RATES),
            parse_content(args.get("content")))


def favorites_filter(args, user):
    """{root id: set of paths} - the viewer's own favourites - when the request asks
    for "In favorites" (fav=1), else None. Kept out of parse_media_filters: it is
    per viewer, so a live share (no viewer) can never carry it."""
    if (args.get("fav") or "").strip().lower() not in ("1", "true", "yes") or not user:
        return None
    return {root: set(paths) for root, paths in user_favorites(user["username"]).items()}


def three_d_filter(args):
    """The finished conversions when the request asks for 3D only (stereo=1), else
    None. Kept out of parse_media_filters like the favourites: the conversions
    change all the time, and a query share stores those filters."""
    if (args.get("stereo") or "").strip().lower() not in ("1", "true", "yes"):
        return None
    return stereo3d_service().ready_paths()


def is_three_d(ready, root_id, rel_path):
    """Converted already, or stereo by nature."""
    return (root_id, rel_path) in ready or stereo3d.is_native_3d(rel_path)


def filters_active(filters):
    return any(f for f in filters)


def apply_media_filters(entries, filters, root_id=None):
    """Keeps only entries whose dimensions and content level pass.

    Anything without known dimensions is dropped: audio always, since it has
    no pixels, and any picture or video the backfill has not measured yet -
    "probably fine" would make the filter untrustworthy. Pictures count since
    2026-09-11; they used to be left out as "a question about video". A content
    filter drops files the tagger has not rated on the same principle. Folders are the
    exception and are never passed through here - they are how you navigate,
    and hiding them would strand you in a filtered folder with no way out.
    """
    if not filters_active(filters):
        return entries
    orientation, min_w, min_h, min_any, kinds, min_fps, max_fps, content = filters
    # A kind filter on its own asks nothing about pixels, so it must not drag
    # in the "unknown dimensions are dropped" rule below - that would hide
    # every image the backfill has not reached yet from "show me images".
    dims_needed = any((orientation, min_w, min_h, min_any, min_fps, max_fps))
    dims_map = media_dimensions_map() if dims_needed else {}
    levels = content_levels_map() if content else None
    out = []
    for entry in entries:
        name = entry.get("name") or os.path.basename(entry.get("path") or "")
        if not kind_matches(name, kinds):
            continue
        rid = entry.get("root") or root_id or DEFAULT_ROOT_ID
        if content and not content_matches(levels, content, rid, entry.get("path") or ""):
            continue
        if not dims_needed:
            out.append(entry)
            continue
        dims = dims_map.get(media_dimensions.key_for(rid, entry.get("path") or ""))
        if media_dimensions.matches(dims, orientation, min_w, min_h, min_any,
                                    min_fps, max_fps):
            entry["width"], entry["height"] = dims[0], dims[1]
            if media_dimensions.frame_rate_of(dims):
                entry["fps"] = media_dimensions.frame_rate_of(dims)
            out.append(entry)
    return out


def attach_media_dimensions(entries, root_id=None):
    """Width, height and a video's duration on file entries, from the
    dimensions store.

    The gallery lays tiles out by aspect ratio before a single thumbnail has
    loaded, and a duration badge must not cost an ffprobe per tile. A file the
    store has not measured is left without; the page takes the thumbnail's own
    shape for it once that loads. Folders are skipped.
    """
    if not entries:
        return entries
    dims_map = media_dimensions_map()
    unmeasured = []
    for entry in entries:
        if entry.get("is_dir"):
            continue
        rid = entry.get("root") or root_id or DEFAULT_ROOT_ID
        path = entry.get("path") or ""
        key = media_dimensions.key_for(rid, path)
        dims = dims_map.get(key)
        # Never measured, measured before videos carried a duration, or
        # measured when the file had another size (replaced): measure it now,
        # in the background (queue_dimensions).
        if (not dims or media_dimensions.needs_video_fields(path, dims)
                or (entry.get("size") and dims[2] != entry["size"])):
            if media_dimensions.is_video_name(path) or media_dimensions.is_image_name(path):
                full = resolve_media_path(rid, path)
                if full:
                    unmeasured.append((key, full))
        if not dims:
            continue
        entry["width"], entry["height"] = dims[0], dims[1]
        duration = media_dimensions.duration_of(dims)
        if duration:
            entry["duration"] = duration
    if unmeasured:
        queue_dimensions(unmeasured)
    return entries


# A file the dimensions store has not measured used to wait for the nightly
# backfill for its duration badge and its shape: a download finished after
# 04:00 went a whole day without, and so did anything copied in by hand
# (2026-09-18: two re-encoded videos showed no length). Listings now queue
# such files and one background thread measures them - an ffprobe per video,
# a header read per picture - a batch at a time, writing the store (~40 MB)
# once per batch. The next listing shows them.
DIMS_QUEUE_MAX = 5000
DIMS_BATCH = 100
_dims_queue = {}                 # store key -> full path, in arrival order
_dims_failed = set()             # not measurable (not asked again until a restart)
_dims_queue_lock = threading.Lock()
_dims_worker = {"running": False}


def queue_dimensions(items):
    """Queues (store key, full path) pairs for measuring. Returns how many were new."""
    added = 0
    with _dims_queue_lock:
        for key, full in items:
            if key in _dims_queue or key in _dims_failed or len(_dims_queue) >= DIMS_QUEUE_MAX:
                continue
            _dims_queue[key] = full
            added += 1
        if added and not _dims_worker["running"]:
            _dims_worker["running"] = True
            threading.Thread(target=_measure_dimensions_loop, daemon=True,
                             name="dimensions-measure").start()
    return added


def _measure_dimensions_loop():
    while True:
        with _dims_queue_lock:
            batch = []
            for key in list(_dims_queue)[:DIMS_BATCH]:
                batch.append((key, _dims_queue.pop(key)))
            if not batch:
                _dims_worker["running"] = False
                return
        updates = {}
        for key, full in batch:
            try:
                entry = media_dimensions.entry_for(full)
            except Exception:
                entry = None
            if entry:
                updates[key] = entry
            else:
                with _dims_queue_lock:
                    _dims_failed.add(key)
        if updates:
            try:
                media_dimensions.merge(BASE_DIR, updates)
                with _dimensions_lock:
                    _dimensions_cache["checked_at"] = 0.0    # re-read on the next ask
            except Exception as exc:
                print(f"[Warning] dimensions not saved: {exc}", flush=True)


# --- Favourites ---------------------------------------------------------------
# Per account: {"<username>": {"<root id>": {"<path>": <favourited at>}}} in
# favorites.json, apart from users.json - that one is read on every request and
# a favourites list only grows. Kept in memory after the first read; a change
# rewrites the file whole (small) with tmp + replace, under the lock.
FAVORITES_FILE = os.path.join(BASE_DIR, "favorites.json")
_favorites_lock = threading.Lock()
_favorites = {"loaded": False, "data": {}}
# Functions called after every change: fn(username, root_id, rel_path, on, favourited_at). A list
# the 3D code registers into (below), so set_favorite itself depends on nothing else - the harnesses
# that load it out of the AST (stitch_check, favorites_check) neither need nor pull in the 3D side.
_favorite_listeners = []


def _favorites_data():
    """Caller holds _favorites_lock."""
    if not _favorites["loaded"]:
        try:
            with open(FAVORITES_FILE, encoding="utf-8") as fh:
                _favorites["data"] = json.load(fh) or {}
        except (OSError, ValueError):
            _favorites["data"] = {}
        _favorites["loaded"] = True
    return _favorites["data"]


def user_favorites(username):
    """{root id: {path: favourited at}} for one account - a copy."""
    with _favorites_lock:
        return {root: dict(paths) for root, paths in (_favorites_data().get(username) or {}).items()}


def set_favorite(username, root_id, rel_path, on):
    """Adds or removes one favourite; returns how many the account has. Every way a
    favourite comes or goes passes through here, which is where an admin's favourite
    video or picture reaches the 3D queue (a _favorite_listeners entry, after the lock)."""
    stamp = None
    with _favorites_lock:
        data = _favorites_data()
        mine = data.setdefault(username, {})
        paths = mine.setdefault(root_id, {})
        if on:
            stamp = paths.setdefault(rel_path, time.time())
        else:
            paths.pop(rel_path, None)
        if not paths:
            mine.pop(root_id, None)
        count = sum(len(p) for p in mine.values())
        if not mine:
            data.pop(username, None)
        _atomic_json_write(FAVORITES_FILE, data)
    for listener in _favorite_listeners:
        listener(username, root_id, rel_path, on, stamp)
    return count


def attach_favorites(entries, user):
    """ "favorite": true on the file entries the viewer has favourited. Only in
    routes with a signed-in viewer: the share routes build the same entries for
    anonymous visitors, who have no favourites."""
    favorites = user_favorites(user["username"]) if user else {}
    if not favorites:
        return entries
    for e in entries:
        if not e.get("is_dir") and e.get("path") in favorites.get(e.get("root") or DEFAULT_ROOT_ID, {}):
            e["favorite"] = True
    return entries


@app.route("/api/media/favorite", methods=["POST"])
def media_favorite():
    """Body {root, path, favorite: true | false}. Every signed-in account has its
    own list, guests too, but only for files in a library it may see. A file
    that is gone can still be unfavourited, never favourited."""
    user = get_authenticated_user()
    if not user:
        return jsonify({"error": "Unauthorized"}), 401
    data = request.get_json(silent=True) or {}
    root_id = data.get("root") or DEFAULT_ROOT_ID
    rel_path = data.get("path")
    on = data.get("favorite")
    if not isinstance(root_id, str) or not isinstance(rel_path, str) or not isinstance(on, bool):
        return jsonify({"error": "root, path and favorite (true or false) are required"}), 400
    root_id = root_id.strip()
    rel_path = rel_path.strip("/").replace("\\", "/")
    if not rel_path:
        return jsonify({"error": "path is required"}), 400
    if not root_visible_to(user, root_id):
        return jsonify({"error": "Forbidden"}), 403
    if on:
        full = resolve_media_path(root_id, rel_path)
        if not full or not os.path.isfile(full):
            return jsonify({"error": "Not found"}), 404
    count = set_favorite(user["username"], root_id, rel_path, on)
    return jsonify({"favorite": on, "count": count})


@app.route("/api/media/browse", methods=["GET"])
def browse_media():
    user = get_authenticated_user()
    if not user:
        return jsonify({"error": "Unauthorized"}), 401

    root_id = request.args.get("root", "").strip()
    rel_path = request.args.get("path", "").strip("/").replace("\\", "/")

    # No root given and no path: the top level, which lists the roots
    # themselves rather than any one folder's contents. With a single root
    # configured that's just noise, so descend straight into it.
    if not root_id:
        visible = roots_for_user(user)
        if len(visible) > 1:
            return jsonify({
                "root": "",
                "path": "",
                "is_root_listing": True,
                "roots": [
                    {"id": r["id"], "name": r["name"], "guest_visible": bool(r.get("guest_visible"))}
                    for r in visible
                ],
                "dirs": [],
                "files": []
            })
        root_id = visible[0]["id"] if visible else DEFAULT_ROOT_ID

    if not root_visible_to(user, root_id):
        return jsonify({"error": "Forbidden"}), 403

    target_dir = resolve_media_path(root_id, rel_path)
    if not target_dir:
        return jsonify({"error": "Invalid path"}), 400
    if not os.path.isdir(target_dir):
        return jsonify({"error": "Not found"}), 404

    dirs, files = [], []
    candidates = []
    try:
        with os.scandir(target_dir) as it:
            for entry in it:
                if entry.name.startswith("."):
                    continue
                if entry.is_dir():
                    dirs.append(entry.name)
                elif entry.is_file() and not _is_hidden_or_partial(entry.name):
                    candidates.append(entry)
        # Which posters to hide can only be decided once the whole directory
        # has been read, so the stat() calls happen in a second pass - and
        # not at all for the entries dropped here.
        hidden = poster_companion_names([e.name for e in candidates])
        for entry in candidates:
            if entry.name in hidden:
                continue
            stat = entry.stat()
            # 0 bytes means a tombstone left by a banned delete
            # (or a download that produced nothing) - either way
            # there is no media here to show.
            if stat.st_size == 0:
                continue
            files.append(_media_file_entry(rel_path, entry.name, stat, root_id))
    except OSError as e:
        return jsonify({"error": str(e)}), 500

    dirs.sort(key=str.lower)
    files.sort(key=lambda f: f["name"].lower())

    filters = parse_media_filters(request.args)
    favs = favorites_filter(request.args, user)
    total_files = len(files)
    files = apply_media_filters(files, filters, root_id)
    if favs is not None:
        mine = favs.get(root_id, set())
        files = [f for f in files if f.get("path") in mine]
    three_d = three_d_filter(request.args)
    if three_d is not None:
        files = [f for f in files if is_three_d(three_d, root_id, f.get("path"))]
    attach_media_dimensions(files, root_id)
    attach_favorites(files, user)

    root = get_root(root_id)
    return jsonify({
        "root": root_id,
        "root_name": root["name"] if root else root_id,
        "path": rel_path,
        "is_root_listing": False,
        "multi_root": len(roots_for_user(user)) > 1,
        # So the page can say "3 of 120" rather than leaving someone to
        # wonder whether a folder is empty or just filtered.
        "filtered": filters_active(filters) or favs is not None or three_d is not None,
        "total_files": total_files,
        "dirs": dirs,
        # Parsed alongside the plain names rather than replacing them: the
        # public share viewer reads "dirs" as strings and predates this.
        "dir_info": [dict(name=d, **parse_folder_identity(d)) for d in dirs],
        "files": files
    })

def parse_folder_identity(name):
    """Splits a source folder name into the creator's display name and the
    account id it was filed under, so the library can be sorted by either.
    The convention is per-source: pixiv writes "Artist (12345678)", civitai
    "Creator____Post_123", twitter uses the bare "@handle" (there is no
    display name on disk, which is why sorting twitter by artist falls back
    to the handle), and youtube records only the channel's display name."""
    stripped = (name or "").strip()
    if stripped.startswith("@"):
        return {"artist": None, "id": stripped.lstrip("@")}
    numeric_suffix = re.match(r"^(.*?)\s*\((\d+)\)$", stripped)
    if numeric_suffix:
        return {"artist": numeric_suffix.group(1).strip() or None,
                "id": numeric_suffix.group(2)}
    if "____" in stripped:
        left, _, right = stripped.partition("____")
        return {"artist": left.strip() or None, "id": right.strip() or None}
    return {"artist": stripped or None, "id": None}

# Folder totals are a subtree walk, which is far too expensive to do on
# every listing - they're computed only when something actually sorts by
# them, and reused for a while afterwards.
FOLDER_STATS_CACHE_SECONDS = 300
_folder_stats_cache = {}
_folder_stats_lock = threading.Lock()

def folder_subtree_stats(dir_path):
    """(total bytes, newest arrival time, file count) for everything under
    dir_path. Uses the arrival time rather than mtime for the same reason
    the recent feed does - see file_added_time."""
    total = 0
    latest = 0.0
    count = 0
    for root, _dirs, files in os.walk(dir_path):
        for fname in files:
            if _is_hidden_or_partial(fname):
                continue
            try:
                st = os.stat(os.path.join(root, fname))
            except OSError:
                continue
            total += st.st_size
            count += 1
            added = file_added_time(st)
            if added > latest:
                latest = added
    return total, latest, count

@app.route("/api/media/folder-stats", methods=["GET"])
def media_folder_stats():
    """Size and last-arrival for each immediate subfolder of one folder."""
    user = get_authenticated_user()
    if not user:
        return jsonify({"error": "Unauthorized"}), 401

    root_id = request.args.get("root", "").strip() or DEFAULT_ROOT_ID
    if not root_visible_to(user, root_id):
        return jsonify({"error": "Forbidden"}), 403

    rel_path = request.args.get("path", "").strip("/").replace("\\", "/")
    target_dir = resolve_media_path(root_id, rel_path)
    if not target_dir:
        return jsonify({"error": "Invalid path"}), 400
    if not os.path.isdir(target_dir):
        return jsonify({"error": "Not found"}), 404

    cache_key = (root_id, rel_path)
    with _folder_stats_lock:
        cached = _folder_stats_cache.get(cache_key)
        if cached and time.time() - cached[0] < FOLDER_STATS_CACHE_SECONDS:
            return jsonify({"root": root_id, "path": rel_path, "folders": cached[1]})

    folders = {}
    try:
        with os.scandir(target_dir) as it:
            for entry in it:
                if entry.name.startswith(".") or not entry.is_dir():
                    continue
                size, latest, count = folder_subtree_stats(entry.path)
                folders[entry.name] = {"size": size, "latest": latest, "files": count}
    except OSError as e:
        return jsonify({"error": str(e)}), 500

    with _folder_stats_lock:
        _folder_stats_cache[cache_key] = (time.time(), folders)

    return jsonify({"root": root_id, "path": rel_path, "folders": folders})

def is_browser_navigation():
    """True when this request is a person opening the URL in a tab (rather
    than fetch()/<img> pulling a subresource), so it can be answered with a
    login page instead of a bare status code."""
    if request.headers.get("Sec-Fetch-Mode") == "navigate":
        return True
    return "text/html" in request.headers.get("Accept", "")

def login_redirect_for(target_url):
    # Caddy normally redirects an unauthenticated browser to Authelia before it
    # reaches this app; this is the fallback for a request that bypassed the
    # proxy or arrived without an API key.
    if not AUTH_URL:
        return Response("Sign in required: the reverse proxy did not send an identity.", 401, mimetype="text/plain")
    return redirect(AUTH_URL + "/?rd=" + quote(target_url, safe=""))

# Where a twitter, pixiv or instagram download came from, rebuilt from the place the
# downloader put it - no extra read of the file and no lookup:
#   twitter/@<handle>/<tweet id>_<n>.<ext>               (gallery-dl)
#   pixiv/<artist> (<member id>)/<image id>_p<n> - <title>  (PixivUtil2)
#   instagram/@<handle>/<post id>_<n>_<shortcode>.<ext>  (gallery-dl)
# The info panel links the post from the file name and the account from the
# id. Paths that do not fit the pattern simply get no links.
_TWITTER_POST_IN_NAME = re.compile(r"^(\d{5,})_")
_PIXIV_ARTIST_FOLDER = re.compile(r"^(.*) \((\d+)\)$")
_INSTAGRAM_POST_IN_NAME = re.compile(r"^\d+_\d+_([A-Za-z0-9_-]+)\.")


def source_links_for(root_id, rel_path):
    parts = (rel_path or "").strip("/").split("/")
    if len(parts) != 3 or download_category_of(root_id, rel_path) not in ("twitter", "pixiv", "instagram"):
        return None
    site, folder, name = parts
    if site == "instagram":
        handle = folder[1:] if folder.startswith("@") else ""
        post = _INSTAGRAM_POST_IN_NAME.match(name)
        if not re.fullmatch(r"[A-Za-z0-9._]{1,30}", handle):
            return None
        out = {"site": "instagram", "artist_name": "@" + handle, "artist_id": "@" + handle,
               "artist_url": f"https://www.instagram.com/{handle}/"}
        if post:
            out["post_url"] = f"https://www.instagram.com/p/{post.group(1)}/"
        return out
    if site == "twitter":
        handle = folder[1:] if folder.startswith("@") else ""
        post = _TWITTER_POST_IN_NAME.match(name)
        if not handle or not re.fullmatch(r"[A-Za-z0-9_]{1,50}", handle):
            return None
        out = {"site": "twitter", "artist_name": "@" + handle, "artist_id": "@" + handle,
               "artist_url": f"https://x.com/{handle}"}
        if post:
            out["post_url"] = f"https://x.com/{handle}/status/{post.group(1)}"
        return out
    artist = _PIXIV_ARTIST_FOLDER.match(folder)
    if not artist or folder.startswith("FANBOX "):
        return None
    out = {"site": "pixiv", "artist_name": artist.group(1), "artist_id": artist.group(2),
           "artist_url": f"https://www.pixiv.net/users/{artist.group(2)}"}
    image = pixiv_image_id_for(name)
    if image:
        out["post_url"] = f"https://www.pixiv.net/artworks/{image}"
    return out


@app.route("/api/media/info", methods=["GET"])
def media_info():
    """Everything the preview's info panel shows for one file."""
    user = get_authenticated_user()
    if not user:
        return jsonify({"error": "Unauthorized"}), 401

    root_id = request.args.get("root", "").strip() or DEFAULT_ROOT_ID
    if not root_visible_to(user, root_id):
        return jsonify({"error": "Forbidden"}), 403

    rel_path = (request.args.get("path") or "").strip("/").replace("\\", "/")
    file_path = resolve_media_path(root_id, rel_path)
    if not file_path:
        return jsonify({"error": "Invalid path"}), 400
    if not os.path.isfile(file_path):
        return jsonify({"error": "Not found"}), 404

    stat = os.stat(file_path)
    root = get_root(root_id)
    info = {
        "name": os.path.basename(rel_path),
        "path": rel_path,
        "root": root_id,
        "root_name": root["name"] if root else root_id,
        "folder": os.path.dirname(rel_path),
        "type": classify_media_path(file_path),
        "size": stat.st_size,
        "modified": stat.st_mtime,
        "added": file_added_time(stat),
        # No tagging system exists yet - the key is here so the panel can
        # show the row without the frontend inventing the concept.
        "tags": [],
    }
    info.update(probe_media_details(file_path))
    source = source_links_for(root_id, rel_path)
    if source:
        info["source"] = source
    if set(kinds_of(info["name"])) & {"image", "video"}:
        content = content_info(root_id, rel_path)
        if content is not None:
            info["content"] = content
    if "audio" in kinds_of(info["name"]):
        genres = genre_info(root_id, rel_path)
        if genres is not None:
            info["ai_genres"] = genres
    return jsonify(info)


def genre_info(root_id, rel_path):
    """What the genre pass heard in one track, for the info panel:
    {"done": analysed yet, "styles": [[Discogs label, p]], "on_map": has an
    embedding}. None when the AI store cannot be read."""
    try:
        conn = media_ai.connect(BASE_DIR)
        row = conn.execute("SELECT id, genre_model FROM media WHERE root = ? AND path = ?",
                           (root_id, rel_path)).fetchone()
        if not row:
            return {"done": False, "styles": [], "on_map": False}
        styles = [[r[0], round(r[1] or 0.0, 3)] for r in conn.execute(
            "SELECT tag, p FROM media_tags WHERE media_id = ? AND category = 'genre'"
            " ORDER BY p DESC", (row[0],))]
        on_map = conn.execute("SELECT 1 FROM audio_embeddings WHERE media_id = ?",
                              (row[0],)).fetchone() is not None
        return {"done": bool(row[1]), "styles": styles, "on_map": on_map}
    except Exception as exc:
        print(f"[Warning] genre info for {rel_path}: {exc}")
        return None


# ---------------------------------------------------------------------------
# Genre map (genres.html): every analysed track, placed so that tracks the
# genre model hears alike sit together. genre_map.py lays it out in a child
# process - seconds to minutes of O(n^2) numpy that has no place in a request
# thread or in this process's memory - and the route serves what it wrote,
# starting a rebuild when the store has moved on (KSF-042).
# ---------------------------------------------------------------------------
GENRE_MAP_REBUILD_S = 600          # at most one build started per 10 minutes
GENRE_MAP_THREADS = "4"            # BLAS threads for the build: it shares the VM
_genre_map = {"proc": None, "started": 0.0, "finished": 0.0, "exit": None,
              "last_line": "", "data": None, "mtime": None, "idsum": 0}
_genre_map_lock = threading.Lock()


def genre_map_building():
    proc = _genre_map["proc"]
    return proc is not None and proc.poll() is None


def genre_map_loaded():
    """The last map genre_map.py wrote, parsed once per write."""
    try:
        mtime = os.path.getmtime(genre_map.map_path(BASE_DIR))
    except OSError:
        return None
    if _genre_map["mtime"] != mtime:
        data = genre_map.load_map(BASE_DIR)
        _genre_map.update(data=data, mtime=mtime,
                          idsum=sum(t["id"] for t in data["tracks"]) if data else 0)
    return _genre_map["data"]


def start_genre_map_build():
    """genre_map.py in the background. False while one runs, or when one
    started under GENRE_MAP_REBUILD_S ago - during a backfill the store moves
    on every few seconds, and the map need not chase it."""
    with _genre_map_lock:
        if genre_map_building() or time.time() - _genre_map["started"] < GENRE_MAP_REBUILD_S:
            return False
        env = dict(os.environ, OPENBLAS_NUM_THREADS=GENRE_MAP_THREADS,
                   OMP_NUM_THREADS=GENRE_MAP_THREADS, MKL_NUM_THREADS=GENRE_MAP_THREADS)
        try:
            proc = subprocess.Popen(
                [sys.executable, "-u", os.path.join(BASE_DIR, "genre_map.py"), BASE_DIR],
                cwd=BASE_DIR, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        except OSError as exc:
            _genre_map.update(started=time.time(), exit=-1, last_line=str(exc)[:300])
            return False
        _genre_map.update(proc=proc, started=time.time(), exit=None, last_line="")

    def watch():
        for line in proc.stdout:
            if line.strip():
                _genre_map["last_line"] = line.strip()[:300]
        _genre_map.update(exit=proc.wait(), finished=time.time())
        if _genre_map["exit"]:
            print(f"[Warning] genre map build failed ({_genre_map['exit']}): {_genre_map['last_line']}")

    threading.Thread(target=watch, daemon=True, name="genre-map").start()
    return True


def genre_map_counts(conn):
    """(tracks with an embedding, sum of their media ids): the map is behind
    when either differs from what it was built from - a count alone would
    miss one track trashed while another arrived."""
    return conn.execute(
        "SELECT COUNT(*), COALESCE(SUM(e.media_id), 0) FROM audio_embeddings e"
        " JOIN media m ON m.id = e.media_id WHERE m.trashed IS NULL").fetchone()


def genre_map_behind(data, count, idsum):
    return count > 0 and (data is None or data.get("count") != count or _genre_map["idsum"] != idsum)


@app.route("/api/media/genre-map", methods=["GET"])
def media_genre_map():
    """The map for genres.html: every track with an embedding in a root the
    user may see, as rows in the order of "fields". A map behind the store is
    served as it is while a rebuild runs; the page polls and glides over."""
    user = get_authenticated_user()
    if not user:
        return jsonify({"error": "Unauthorized"}), 401
    try:
        conn = media_ai.connect(BASE_DIR)
        count, idsum = genre_map_counts(conn)
        music, analysed = conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(genre_model IS NOT NULL), 0) FROM media"
            " WHERE kind = 'audio' AND trashed IS NULL").fetchone()
        trashed = {r[0] for r in conn.execute(
            "SELECT id FROM media WHERE kind = 'audio' AND trashed IS NOT NULL")}
    except Exception as exc:
        print(f"[Warning] genre map unavailable: {exc}")
        return jsonify({"error": "The AI index is not available"}), 503

    data = genre_map_loaded()
    stale = genre_map_behind(data, count, idsum)
    if stale:
        start_genre_map_build()

    allowed = {r["id"] for r in roots_for_user(user)}
    root_index = {}
    rows = []
    for t in (data or {}).get("tracks", []):
        if t["root"] not in allowed or t["id"] in trashed:
            continue
        ri = root_index.setdefault(t["root"], len(root_index))
        rows.append([t["id"], ri, t["path"], t["size"], t["g"], t["s"], t["p2"], t["p3"]])
    failed = _genre_map["exit"] not in (None, 0) and not genre_map_building()
    return jsonify({
        "fields": ["id", "root", "path", "size", "genre", "styles", "xy", "xyz"],
        "tracks": rows,
        "parents": (data or {}).get("parents", []),
        "styles": (data or {}).get("styles", []),
        "roots": [{"id": rid, "name": (get_root(rid) or {}).get("name", rid)} for rid in root_index],
        "built": (data or {}).get("built"),
        "method": (data or {}).get("method"),
        "model": (data or {}).get("model"),
        "building": genre_map_building(),
        "stale": stale,
        # The build's own last line for an admin; it can name server paths.
        "error": (_genre_map["last_line"] if is_admin(user) else "the layout could not be built") if failed else None,
        "music": music,
        "analysed": analysed,
    })

@app.route("/api/media/file", methods=["GET"])
def media_file():
    """Serves the bytes of one media file. Roots other than the built-in
    downloads one live outside web_files, so they can't be reached by the
    static route - and this also enforces per-root guest visibility."""
    user = get_authenticated_user()
    if not user:
        # Someone opening a shared media link in a browser without a
        # session used to be served the login page, because these files
        # went through serve_static. Since they moved to this endpoint a
        # bare 401 was a dead end - send navigations to the login page and
        # back to the file afterwards. Subresource fetches still get 401.
        if is_browser_navigation():
            return login_redirect_for(request.full_path)
        return "401 Unauthorized", 401

    root_id = request.args.get("root", "").strip() or DEFAULT_ROOT_ID
    if not root_visible_to(user, root_id):
        return "403 Forbidden", 403

    file_path = resolve_media_path(root_id, request.args.get("path", ""))
    if not file_path or not os.path.isfile(file_path):
        return "404 Not Found", 404

    return send_media_file(file_path)

# Full-library index for search. Deliberately just (root_id, rel_path)
# string pairs - NOT entry dicts. This library runs to ~300k files, where
# building a dict-per-file (with its long percent-encoded URL) cost ~1.5GB
# of resident memory and several seconds per rebuild, which is what made
# search feel broken. Filtering only needs the path string; the handful of
# entries on the returned page get stat'ed and expanded afterward.
_media_index_cache = {"index": None, "computed_at": 0.0}
_media_index_lock = threading.Lock()
# Held for the whole walk, so callers that find the index stale wait for ONE
# build instead of each walking the library themselves.
_media_index_build_lock = threading.Lock()
# Rebuilding this walks every root in full. On SSDs 60s was merely wasteful;
# on spinning disks a walk of ~300k files is minutes of seeking, and at 60s a
# single person searching would keep the disks busy permanently. Everything
# that changes the library now calls invalidate_media_caches() explicitly -
# downloads finishing, deletes, roots changing - so this is only a backstop
# for changes made outside the app (files copied in over Samba, say).
MEDIA_INDEX_CACHE_SECONDS = 900

def link_key(dir_path, fname):
    """(device, inode) when this name is one of SEVERAL for the same file,
    else None. A song is stored once, under its artist, and hard-linked into
    every playlist that holds it, so the library has one file under several
    names - and search matches names. This is what says two of them are one
    file, so it can be shown once (one_row_per_file).

    Only audio is stat'ed. Hard links are something only the music store
    makes, and the walk exists precisely to avoid a stat per file: at ~4.5k
    songs against ~550k files this costs under 1% of the rebuild."""
    if classify_media_type(fname) != "audio":
        return None
    try:
        st = os.stat(os.path.join(dir_path, fname))
    except OSError:
        return None
    return (st.st_dev, st.st_ino) if st.st_nlink > 1 else None


def one_row_per_file(rows):
    """The same rows with every file appearing once, keeping the first name -
    after ranking, that is the name that matched best. Rows without a link key
    (every ordinary file) pass straight through."""
    seen = set()
    out = []
    for row in rows:
        key = row[3] if len(row) > 3 else None
        if key is not None:
            if key in seen:
                continue
            seen.add(key)
        out.append(row)
    return out


def library_file_count(rows):
    """Files, not names: a song in three playlists is one file, not four."""
    return sum(1 for row in one_row_per_file(rows) if not row[2])


def _fresh_media_index():
    with _media_index_lock:
        cached = _media_index_cache["index"]
        if cached is not None and time.time() - _media_index_cache["computed_at"] < MEDIA_INDEX_CACHE_SECONDS:
            return cached
    return None


def _refresh_media_index():
    """One walk on its own thread, for get_media_index's stale answer."""
    try:
        with _media_index_build_lock:
            if _fresh_media_index() is None:
                _walk_media_index()
    except Exception as exc:
        print(f"[Warning] media index refresh failed: {exc}", flush=True)
    finally:
        with _media_index_lock:
            _media_index_cache["refreshing"] = False


def get_media_index():
    cached = _fresh_media_index()
    if cached is not None:
        return cached
    # An expired index is answered at once and rebuilt behind it: nothing else
    # keeps it fresh, so otherwise the first search after 15 quiet minutes sat
    # through a walk of the whole library, and so did a VR wall paging down.
    # A file that has gone since is dropped when its page is expanded. Only an
    # index that was invalidated (None) or never built makes the caller wait.
    with _media_index_lock:
        stale = _media_index_cache["index"]
        start = stale is not None and not _media_index_cache.get("refreshing")
        if start:
            _media_index_cache["refreshing"] = True
    if start:
        threading.Thread(target=_refresh_media_index, daemon=True,
                         name="media-index-refresh").start()
    if stale is not None:
        return stale
    # One walk at a time. Every request that found the cache stale used to walk
    # the whole library itself; a share page fires its info, items, cover and
    # dozens of thumbnails at once, and on 2026-09-13 that took over a minute to
    # a first byte - dozens of 550k-entry walks contending for the GIL.
    with _media_index_build_lock:
        cached = _fresh_media_index()
        if cached is not None:
            return cached
        return _walk_media_index()


def _walk_media_index():
    index = []
    empties = empty_files_snapshot()
    for media_root in get_all_roots():
        base = media_root["path"]
        if not os.path.isdir(base):
            continue
        root_id = media_root["id"]
        empty_here = empties.get(root_id, ())
        for root, dirs, files in os.walk(base):
            # Never INTO a dot-folder. The trash is <root>/.trash, and leaving
            # only its name out while still walking it put every trashed file
            # and folder in the search results (37k files, 2026-09-18).
            dirs[:] = [d for d in dirs if not d.startswith(".")]
            rel_dir = os.path.relpath(root, base).replace(os.sep, "/")
            if rel_dir == ".":
                rel_dir = ""
            # Folders are indexed too. Searching for an artist used to return
            # their files but never the folder holding them, which is usually
            # the thing you actually wanted to open.
            for dname in dirs:
                index.append((root_id,
                              "/".join(p for p in (rel_dir, dname) if p), True))
            # os.walk already handed us the directory's whole file list, so
            # the poster check is free here.
            hidden = poster_companion_names(files)
            for fname in files:
                if _is_hidden_or_partial(fname) or fname in hidden:
                    continue
                rel = "/".join(p for p in (rel_dir, fname) if p)
                # No stat() here - 300k stat calls per rebuild for data that
                # substring matching never looks at. Which files are empty
                # (ban tombstones) comes from the registry instead.
                if rel in empty_here:
                    continue
                index.append((root_id, rel, False, link_key(root, fname)))

    with _media_index_lock:
        _media_index_cache["index"] = index
        _media_index_cache["computed_at"] = time.time()
    return index

def build_entries_for(pairs):
    """Expands index rows into full entry dicts, skipping any that have since
    disappeared. Only ever called on a single page's worth.

    Rows are (root_id, rel_path) or (root_id, rel_path, is_dir); the two-part
    form is still accepted because the recent feed and the pool code build
    pairs of their own."""
    entries = []
    empty = []
    for row in pairs:
        root_id, rel_path = row[0], row[1]
        is_dir = row[2] if len(row) > 2 else False
        full = resolve_media_path(root_id, rel_path)
        if not full:
            continue
        try:
            stat = os.stat(full)
        except OSError:
            continue
        if not is_dir and stat.st_size == 0:
            # A ban tombstone, or a download that produced nothing: no media
            # to show, as in a folder listing. One the registry did not know
            # yet is recorded, so the next index build leaves it out and the
            # search's total stops counting it.
            empty.append((root_id, rel_path))
            continue
        rel_dir, name = os.path.split(rel_path)
        if is_dir:
            # A folder has no size, type or url worth reporting; what the page
            # needs is enough to draw a folder tile and navigate into it.
            entries.append({
                "name": name, "path": rel_path, "root": root_id,
                "is_dir": True, "type": "folder",
                "mtime": stat.st_mtime, "added": file_added_time(stat),
            })
        else:
            entries.append(_media_file_entry(rel_dir, name, stat, root_id))
    if empty:
        note_empty_files(empty)
    return entries

# Recently-downloaded feed: served from a maintained list (recent_media.json,
# via recent_media_store) instead of walking the whole downloads tree on
# every request. New entries are appended by execute_download_task() right
# after a task finishes (see scan_new_media_since below) and by
# youtube_music_extractor.py for its own independently-produced files.
# Entries are validated against disk here and self-pruned if missing -
# cached briefly so paging through the lazy-load feed is cheap.
_recent_media_page_cache = {"entries": None, "computed_at": 0.0}
_recent_media_page_lock = threading.Lock()
RECENT_MEDIA_PAGE_CACHE_SECONDS = 5

def file_added_time(stat_result):
    """When a file actually landed on this disk. NOT mtime: downloaders
    backdate that to the source's own timestamp (gallery_dl stamps a
    tweet's original date, so a twitter image fetched today can carry an
    mtime from 2023), which makes fresh downloads look ancient. ctime is
    set by the filesystem on write and can't be backdated that way."""
    return max(stat_result.st_ctime, 0)

def bootstrap_recent_media_list():
    """Seeds recent_media.json from the existing library the first time it's
    needed. Without this the feed would sit empty on an install that already
    has a full downloads tree, only filling as new downloads land - looking
    broken rather than merely new."""
    limit = recent_media_store.max_recent_media_items(BASE_DIR)
    # heapq over (added_time, root, path) tuples: at ~300k files, keeping
    # only the newest `limit` costs a bounded amount of memory instead of
    # materializing an entry dict for the entire library.
    newest = []
    for media_root in get_all_roots():
        base = media_root["path"]
        if not os.path.isdir(base):
            continue
        for root, dirs, files in os.walk(base):
            dirs[:] = [d for d in dirs if not d.startswith(".")]   # the trash
            rel_dir = os.path.relpath(root, base).replace(os.sep, "/")
            if rel_dir == ".":
                rel_dir = ""
            hidden = poster_companion_names(files)
            for fname in files:
                if _is_hidden_or_partial(fname) or fname in hidden:
                    continue
                try:
                    stat = os.stat(os.path.join(root, fname))
                except OSError:
                    continue
                if stat.st_size == 0:                               # a tombstone
                    continue
                rel = "/".join(p for p in (rel_dir, fname) if p)
                item = (file_added_time(stat), media_root["id"], rel)
                if len(newest) < limit:
                    heapq.heappush(newest, item)
                elif item[0] > newest[0][0]:
                    heapq.heapreplace(newest, item)

    newest.sort(key=lambda t: t[0], reverse=True)
    paths = [{"root": r, "path": p} for _t, r, p in newest]
    recent_media_store.write_recent_media_paths(BASE_DIR, paths)
    print(f"[Info] Seeded recent-media list with {len(paths)} existing file(s)")
    return paths

def _normalize_recent_item(item):
    """Entries are {"root": ..., "path": ...}; a bare string is a pre-roots
    entry and belongs to the built-in downloads root."""
    if isinstance(item, str):
        return DEFAULT_ROOT_ID, item
    return item.get("root") or DEFAULT_ROOT_ID, item.get("path") or ""

def _recent_item_is_poster_companion(full_path):
    """True for a recent entry that is only a poster for a video next to it.

    Unlike every other caller this one has no directory listing to work from
    - the recent list is a flat list of paths - so it has to look. It is kept
    cheap on purpose: only names actually ending in "-poster.<image>" look at
    the disk at all, and then at most once per video extension. Entries
    recorded from now on are filtered before they ever reach the list (see
    scan_new_media_since), so this only pays for the backlog."""
    stem, ext = os.path.splitext(full_path)
    if ext.lower() not in MEDIA_EXTENSIONS["image"]:
        return False
    if not stem.lower().endswith(POSTER_STEM_SUFFIX):
        return False
    base = stem[:-len(POSTER_STEM_SUFFIX)]
    return any(os.path.exists(base + v) for v in MEDIA_EXTENSIONS["video"])

def get_recent_media_entries(needed=None):
    """Entries for the recent strip, newest first.

    `needed` is how many the caller will actually show. Building every entry
    means one stat() per file, and the list holds up to 3000 - which on a
    spinning disk is 3000 seeks to render a strip of forty. So the work stops
    once `needed` valid entries exist, and the rest of the list is left
    untouched until someone pages into it."""
    with _recent_media_page_lock:
        cached = _recent_media_page_cache["entries"]
        if (cached is not None
                and time.time() - _recent_media_page_cache["computed_at"] < RECENT_MEDIA_PAGE_CACHE_SECONDS
                and (needed is None or len(cached) >= needed
                     or _recent_media_page_cache.get("complete"))):
            return cached

    if not recent_media_store.recent_media_list_exists(BASE_DIR):
        paths = bootstrap_recent_media_list()
    else:
        paths = recent_media_store.read_recent_media_paths(BASE_DIR)
    entries = []
    valid_paths = []
    missing = False
    stopped_early = False
    for item in paths:
        # Everything past what the caller needs stays on the list untouched:
        # it is neither validated nor pruned here, which is why `missing`
        # rewrites are only allowed on a complete pass below.
        if needed is not None and len(entries) >= needed:
            stopped_early = True
            valid_paths.append(item)
            continue
        root_id, rel_path = _normalize_recent_item(item)
        full = resolve_media_path(root_id, rel_path)
        try:
            stat = os.stat(full) if full else None
        except OSError:
            stat = None
        if stat is None or stat.st_size == 0:
            # Gone, or a ban's 0-byte tombstone (a deleted or joined Twitter
            # piece) - hidden here as every listing hides it.
            missing = True
            continue
        if full and _recent_item_is_poster_companion(full):
            # Drop it from the stored list too, so this costs nothing next time.
            missing = True
            continue
        valid_paths.append({"root": root_id, "path": rel_path})
        rel_dir, fname = os.path.split(rel_path)
        entries.append(_media_file_entry(rel_dir, fname, stat, root_id))

    # Pruning is safe either way: entries past the cut-off were copied to
    # valid_paths untouched, so a partial pass only ever removes files it
    # actually looked at and confirmed gone.
    if missing:
        recent_media_store.write_recent_media_paths(BASE_DIR, valid_paths)

    with _recent_media_page_lock:
        _recent_media_page_cache["entries"] = entries
        _recent_media_page_cache["computed_at"] = time.time()
        # A partial build must not be served to a caller that wants more, and
        # its length is not the library's total.
        _recent_media_page_cache["complete"] = not stopped_early
        _recent_media_page_cache["total"] = (len(entries) if not stopped_early
                                             else len(paths))
    return entries

def scan_new_media_since(target_dir, since_ts):
    """Returns DOWNLOADS_DIR-relative paths of files under target_dir that
    were written at/after since_ts - i.e. what a just-finished task actually
    produced. Scoped to a single task's own output folder rather than the
    whole library, so this stays cheap even though it's a walk.

    Uses the filesystem write time (see file_added_time), not mtime: gallery_dl
    backdates twitter images to the tweet's own date, so an mtime filter threw
    away every freshly downloaded file and they never reached the recent feed."""
    found = []
    seen_files = set()
    for root, _dirs, files in os.walk(target_dir):
        hidden = poster_companion_names(files)
        for fname in files:
            if _is_hidden_or_partial(fname) or fname in hidden:
                continue
            full = os.path.join(root, fname)
            try:
                st = os.stat(full)
                added = file_added_time(st)
            except OSError:
                continue
            if st.st_nlink > 1:
                # The song and its name in the playlist are one file; the
                # feed says "downloaded" once.
                if (st.st_dev, st.st_ino) in seen_files:
                    continue
                seen_files.add((st.st_dev, st.st_ino))
            if added >= since_ts:
                rel = os.path.relpath(full, DOWNLOADS_DIR).replace(os.sep, "/")
                found.append((added, {"root": DEFAULT_ROOT_ID, "path": rel}))
    found.sort(key=lambda t: t[0], reverse=True)
    return [p for _added, p in found]

@app.route("/api/media/recent", methods=["GET"])
def recent_media():
    user = get_authenticated_user()
    if not user:
        return jsonify({"error": "Unauthorized"}), 401

    try:
        offset = max(0, int(request.args.get("offset", 0)))
    except ValueError:
        offset = 0
    try:
        limit = min(100, max(1, int(request.args.get("limit", 40))))
    except ValueError:
        limit = 40

    filters = parse_media_filters(request.args)
    favs = favorites_filter(request.args, user)
    three_d = three_d_filter(request.args)
    filtered = filters_active(filters) or favs is not None or three_d is not None
    # Filter before paging, including matches beyond the first unfiltered page.
    # Keep the cheap partial build when no filters are active.
    entries = get_recent_media_entries(needed=None if filtered else offset + limit)
    if filtered:
        entries = apply_media_filters([dict(e) for e in entries], filters)
        if favs is not None:
            entries = [e for e in entries
                       if e.get("path") in favs.get(e.get("root") or DEFAULT_ROOT_ID, set())]
        if three_d is not None:
            entries = [e for e in entries
                       if is_three_d(three_d, e.get("root") or DEFAULT_ROOT_ID, e.get("path"))]
        complete, total = True, len(entries)
    else:
        with _recent_media_page_lock:
            complete = _recent_media_page_cache.get("complete", True)
            total = _recent_media_page_cache.get("total", len(entries))
    # Copies: the entries are the cached recent list's own dicts.
    page = attach_favorites(attach_media_dimensions([dict(e) for e in entries[offset:offset + limit]]), user)

    return jsonify({
        "items": page,
        "offset": offset,
        "limit": limit,
        # On a partial build this is the list length, an upper bound: the
        # entries beyond the page have not been checked for existence yet.
        "total": total,
        # Stopping early means the list had at least one more item to give.
        "has_more": (not complete) or offset + limit < len(entries)
    })

_everything_cache = {"index": None, "rows": {}}


def everything_rows(index_rows, allowed_roots):
    """Every file row a caller may see, in path order - the "*" search.

    Path order keeps a site and an account together, which is how the library
    is laid out; newest first would need a stat of every file. Sorted once per
    index build and per set of visible roots, because every page of results
    asks again and this is the whole library. Callers must not mutate it.

    A song hard-linked into several playlists is one entry here, the first in
    path order - listing "every file" must not list one file four times.
    """
    key = frozenset(allowed_roots)
    if _everything_cache["index"] is not index_rows:
        _everything_cache["index"] = index_rows
        _everything_cache["rows"] = {}
    rows = _everything_cache["rows"].get(key)
    if rows is None:
        rows = one_row_per_file(sorted((r for r in index_rows if r[0] in key and not r[2]),
                                       key=lambda r: (r[0], r[1].lower())))
        _everything_cache["rows"][key] = rows
    return rows


def _search_rank(row, query):
    """Sort key for one search hit. Lower sorts first.

    Two rules, in this order:

      1. An exact name match wins outright. Searching for a folder by its full
         name should put that folder at the top, not on page four behind every
         file whose path happens to contain it.
      2. Otherwise folders come before files. A folder is a place you can go;
         a file is one item out of what may be thousands inside it, so the
         folder is nearly always the more useful answer to a vague query.

    Within a tier: a name that STARTS with the query beats one that merely
    contains it, then shortest path (the shallowest, most general hit), then
    the path itself so the order never wobbles between identical queries.
    """
    rel = row[1]
    is_dir = row[2] if len(row) > 2 else False
    name = rel.rsplit("/", 1)[-1].lower()
    # For a file, "exact" means the name without its extension: searching
    # "sunset" should treat sunset.jpg as an exact hit, not a partial one.
    stem = name if is_dir else name.rsplit(".", 1)[0]
    exact = name == query or stem == query
    return (0 if exact else 1,
            0 if is_dir else 1,
            0 if name.startswith(query) else 1,
            len(rel),
            rel.lower())


def narrow_to_words(pairs, query, mode):
    """The (root, path) pairs among `pairs` that also match the search box's
    words in the chosen search type - a character, a person or the favourites
    narrowed to "beach". Filename is a substring (or * wildcard) of the path,
    the AI types read the index, and "all" is both."""
    words = query.replace("*", " ").strip()
    if not words:
        return set(pairs)
    keep = set()
    if mode in ("filename", "all"):
        needle = query.strip().lower()
        if "*" in needle:
            pattern = re.compile(".*".join(re.escape(part) for part in needle.split("*")))
            keep |= {p for p in pairs if pattern.search(p[1].lower())}
        else:
            keep |= {p for p in pairs if needle in p[1].lower()}
    column_mode = AI_SEARCH_COLUMNS.get(mode)
    if column_mode:
        try:
            conn = media_ai.connect(BASE_DIR)
            ids = media_ai.ids_for_paths(conn, pairs)
            keep |= {ids[i] for i in media_ai.search_ids(conn, words, column_mode, ids)}
        except Exception as exc:
            print(f"[Warning] narrowing by words failed: {exc}")
    return keep


def _timeline_kinds():
    wanted = [k for k in (request.args.get("type") or "image,video").split(",") if k in ("image", "video")]
    return wanted or ["image", "video"]


@app.route("/api/media/timeline/buckets", methods=["GET"])
def timeline_buckets():
    user = get_authenticated_user()
    if not user:
        return jsonify({"error": "Unauthorized"}), 401
    gran = "year" if request.args.get("granularity") == "year" else "month"
    allowed = sorted(r["id"] for r in roots_for_user(user))
    rows = media_dates.buckets(BASE_DIR, allowed, _timeline_kinds(), gran)
    return jsonify({"buckets": rows, "total": sum(b["count"] for b in rows),
                    "granularity": gran, "index": media_dates.status()})


@app.route("/api/media/timeline", methods=["GET"])
def timeline_media():
    user = get_authenticated_user()
    if not user:
        return jsonify({"error": "Unauthorized"}), 401
    span = media_dates.bucket_range((request.args.get("bucket") or "").strip())
    if not span:
        return jsonify({"error": "bucket must be YYYY or YYYY-MM"}), 400
    try:
        offset = max(0, int(request.args.get("offset", 0)))
    except ValueError:
        offset = 0
    try:
        limit = min(200, max(1, int(request.args.get("limit", 60))))
    except ValueError:
        limit = 60
    allowed = sorted(r["id"] for r in roots_for_user(user))
    kinds = _timeline_kinds()
    filters = parse_media_filters(request.args)
    filtered = filters_active(filters)

    def entry(row):
        rel_dir, name = os.path.split(row["path"])
        return {"name": name, "size": row["size"], "mtime": row["ts"], "type": row["kind"],
                "url": f"/api/media/file?root={quote(row['root'], safe='')}&path={quote(row['path'], safe='')}",
                "path": row["path"], "root": row["root"], "added": row["ts"]}

    gone = []

    def alive(rows):
        out = []
        for row in rows:
            full = resolve_media_path(row["root"], row["path"])
            if full and os.path.isfile(full):
                out.append(row)
            else:
                gone.append((row["root"], row["path"]))
        return out

    if filtered:
        rows = alive(media_dates.rows_in_range(BASE_DIR, allowed, kinds, span[0], span[1]))
        entries = apply_media_filters([entry(r) for r in rows], filters)
        total = len(entries)
        page = entries[offset:offset + limit]
    else:
        total = sum(1 for _ in media_dates.rows_in_range(BASE_DIR, allowed, kinds, span[0], span[1]))
        rows = alive(media_dates.rows_in_range(BASE_DIR, allowed, kinds, span[0], span[1], offset, limit))
        page = [entry(r) for r in rows]
    media_dates.forget(BASE_DIR, gone)
    page = attach_favorites(attach_media_dimensions(page), user)
    return jsonify({"items": page, "offset": offset, "limit": limit, "total": total,
                    "has_more": offset + limit < total})


@app.route("/api/media/places", methods=["GET"])
def places_points():
    user = get_authenticated_user()
    if not user:
        return jsonify({"error": "Unauthorized"}), 401
    allowed = sorted(r["id"] for r in roots_for_user(user))
    rows = media_places.points(BASE_DIR, allowed)
    items, gone = [], []
    for row in rows:
        full = resolve_media_path(row["root"], row["path"])
        if not full or not os.path.isfile(full):
            gone.append((row["root"], row["path"]))
            continue
        name = os.path.basename(row["path"])
        items.append({"name": name, "size": row["size"], "mtime": row["ts"], "type": "image",
                      "url": f"/api/media/file?root={quote(row['root'], safe='')}&path={quote(row['path'], safe='')}",
                      "path": row["path"], "root": row["root"], "added": row["ts"],
                      "lat": round(row["lat"], 6), "lon": round(row["lon"], 6)})
    media_places.forget(BASE_DIR, gone)
    items = attach_favorites(attach_media_dimensions(items), user)
    return jsonify({"items": items, "total": len(items), "index": media_places.status()})


@app.route("/api/media/places/rescan", methods=["POST"])
@require_admin
def places_rescan():
    media_places.request_rescan()
    return jsonify({"success": True, "index": media_places.status()})


@app.route("/api/media/timeline/rescan", methods=["POST"])
@require_admin
def timeline_rescan():
    media_dates.request_rescan()
    return jsonify({"success": True, "index": media_dates.status()})


@app.route("/api/media/search", methods=["GET"])
def search_media():
    user = get_authenticated_user()
    if not user:
        return jsonify({"error": "Unauthorized"}), 401

    query = request.args.get("q", "").strip().lower()
    # A tag or person browse has no words in the box - the subject IS the
    # query.
    favorites_view = request.args.get("favorites") == "1"
    if not query and not (request.args.get("tag") or "").strip()             and not (request.args.get("person") or "").strip() and not favorites_view:
        return jsonify({"items": [], "offset": 0, "limit": 0, "total": 0, "has_more": False})

    try:
        offset = max(0, int(request.args.get("offset", 0)))
    except ValueError:
        offset = 0
    try:
        limit = min(100, max(1, int(request.args.get("limit", 40))))
    except ValueError:
        limit = 40

    mode = (request.args.get("mode") or "filename").strip().lower()
    if mode not in SEARCH_MODES:
        mode = "filename"

    # Substring match over the lightweight path index, restricted to roots
    # the caller may see so a guest can't surface files from a hidden root
    # by searching. Only the requested page is expanded into full entries.
    allowed_roots = {r["id"] for r in roots_for_user(user)}
    index_rows = get_media_index()

    subject = ((request.args.get("tag") or "").strip()
               or (request.args.get("person") or "").strip()
               or ("favorites" if favorites_view else ""))
    # "*" on its own lists every file, whatever the search type: it is how the
    # filters get applied to the whole library ("every vertical 60 fps video").
    # Files only - folders rank first and ~1.5k of them would fill the opening
    # pages. A * among other characters is a wildcard over the path in the
    # filename search ("genshin*.mp4").
    everything = not subject and "*" in query and not query.replace("*", "").strip()
    wildcard = None
    if not everything and "*" in query:
        wildcard = re.compile(".*".join(re.escape(part) for part in query.split("*")))
    if everything:
        matches = everything_rows(index_rows, allowed_roots)
    elif mode in ("filename", "all") and not subject:
        if wildcard:
            matches = [row for row in index_rows
                       if row[0] in allowed_roots and wildcard.search(row[1].lower())]
        else:
            matches = [row for row in index_rows
                       if row[0] in allowed_roots and query in row[1].lower()]
    else:
        matches = []

    # An exact tag beats the text search for a pool: clicking "hatsune_miku"
    # must mean that tag and not every caption containing the word miku, and
    # the tag vocabulary is fixed so there is nothing to guess.
    # Comma-separated, because a character pool is several tags: browsing
    # "Artoria Pendragon" means all six of her costume tags at once. No tag in
    # the danbooru vocabulary contains a comma (checked: 0 of 7,820 general
    # and 2,121 character tags), so the separator needs no escaping.
    exact_tag = [t for t in (request.args.get("tag") or "").split(",") if t.strip()]
    if exact_tag:
        try:
            conn = media_ai.connect(BASE_DIR)
            # No cap: the default 5,000 cut the biggest pools short without a
            # word on the page (Artoria Pendragon has 5,255 files).
            wanted = {(r, p) for r, p in media_ai.files_for_tag(conn, exact_tag, limit=10_000_000)
                      if r in allowed_roots}
        except Exception as exc:
            print(f"[Warning] tag lookup failed: {exc}")
            wanted = set()
        matches = [row for row in index_rows if (row[0], row[1]) in wanted]

    elif (request.args.get("person") or "").strip():
        # Every file this face cluster appears in. Same shape as a tag browse
        # deliberately: to the person looking, "show me her" and "show me this
        # character" are the same gesture.
        try:
            cluster_id = int(request.args["person"])
            conn = media_ai.connect(BASE_DIR)
            wanted = {(r["root"], r["path"]) for r in conn.execute(
                "SELECT DISTINCT m.root, m.path FROM faces f"
                " JOIN media m ON m.id = f.media_id"
                " WHERE f.cluster_id = ? AND m.trashed IS NULL",
                (cluster_id,)) if r["root"] in allowed_roots}
        except Exception as exc:
            print(f"[Warning] person lookup failed: {exc}")
            wanted = set()
        matches = [row for row in index_rows if (row[0], row[1]) in wanted]

    elif favorites_view:
        # The viewer's own favourites, newest first, through the same path as a
        # person: filters, paging and the preview's arrow keys come for free. A
        # favourite whose file is gone (deleted, moved) is not listed.
        order = sorted(((ts, r, p) for r, paths in user_favorites(user["username"]).items()
                        if r in allowed_roots for p, ts in paths.items()), reverse=True)
        position = {(r, p): i for i, (_ts, r, p) in enumerate(order)}
        matches = sorted((row for row in index_rows if not row[2] and (row[0], row[1]) in position),
                         key=lambda row: position[(row[0], row[1])])

    elif mode != "filename" and not everything:
        # The AI index knows paths, not index rows, and only the rows carry
        # the is_dir flag the ranking and the folder rules need. So the hits
        # are turned back into rows via a lookup built once. It already
        # matches word prefixes, so a wildcard's * is simply left out.
        wanted = ai_search_paths(query.replace("*", " ").strip(), mode, allowed_roots)
        if wanted:
            already = {(r[0], r[1]) for r in matches}
            for row in index_rows:
                key = (row[0], row[1])
                if key in wanted and key not in already:
                    matches.append(row)

    # Words beside a character, a person or the favourites narrow that subject
    # instead of being ignored (user, 2026-09-14: "search for a character or
    # person AND specify image context"). A lone "*" narrows nothing. Only when
    # the page says so (narrow=1): a page loaded before this change still sends
    # the subject's NAME as q, and narrowing by that would empty a person.
    if subject and request.args.get("narrow") == "1" and query.replace("*", " ").strip():
        keep = narrow_to_words({(row[0], row[1]) for row in matches}, query, mode)
        matches = [row for row in matches if (row[0], row[1]) in keep]

    if not everything and not favorites_view:   # those two are already in their order
        # A wildcard ranks by its longest literal piece: "exact name" and
        # "name starts with" mean nothing for the pattern itself.
        rank_query = max(query.split("*"), key=len) if wildcard else query
        matches.sort(key=lambda row: _search_rank(row, rank_query))

    if not everything:                          # everything_rows is collapsed once, cached
        # One song, one result. A song is stored once and hard-linked into
        # every playlist that holds it, so searching its title used to find
        # it once per playlist. Ranking runs first, so the name kept is the
        # one that matched best.
        matches = one_row_per_file(matches)

    filters = parse_media_filters(request.args)
    favs = favorites_filter(request.args, user)
    if favs is not None:
        # Like the filters below: on the index rows, before the page is cut. Folder
        # rows go too - a folder is never a favourite.
        matches = [row for row in matches if not row[2] and row[1] in favs.get(row[0], ())]
    three_d = three_d_filter(request.args)
    if three_d is not None:
        matches = [row for row in matches if not row[2] and is_three_d(three_d, row[0], row[1])]
    if filters_active(filters):
        # Filtering has to happen on the INDEX ROWS, before the page is cut:
        # paging first and filtering after would hand back a short page and a
        # total that counts files the caller will never be shown. Folder rows
        # stay - they carry no dimensions and are how you navigate.
        orientation, min_w, min_h, min_any, kinds, min_fps, max_fps, content = filters
        dims_needed = any((orientation, min_w, min_h, min_any, min_fps, max_fps))
        dims_map = media_dimensions_map() if dims_needed else {}
        levels = content_levels_map() if content else None
        kept = []
        for row in matches:
            if row[2]:                      # a folder row
                continue
            if not kind_matches(row[1].rsplit("/", 1)[-1], kinds):
                continue
            if content and not content_matches(levels, content, row[0], row[1]):
                continue
            if not dims_needed:
                kept.append(row)
                continue
            dims = dims_map.get(media_dimensions.key_for(row[0], row[1]))
            if media_dimensions.matches(dims, orientation, min_w, min_h, min_any,
                                        min_fps, max_fps):
                kept.append(row)
        matches = kept

    page = build_entries_for(matches[offset:offset + limit])
    if filters_active(filters):
        page = apply_media_filters(page, filters)
    attach_media_dimensions(page)
    attach_favorites(page, user)

    return jsonify({
        "items": page,
        "offset": offset,
        "limit": limit,
        "total": len(matches),
        "has_more": offset + limit < len(matches),
        "filtered": filters_active(filters) or favs is not None or three_d is not None,
        "mode": mode,
        "everything": everything,
    })

def ai_coverage():
    """How much of the library each vision pass has reached. Zeroes are a
    perfectly good answer here - they mean the backfill has not run."""
    try:
        return media_ai.coverage(media_ai.connect(BASE_DIR))
    except Exception:
        return {"files": 0, "captioned": 0, "ocr": 0, "tagged": 0,
                "faces": 0, "people": 0}


FACE_THUMB_DIR = preview_location("faces", os.path.join(BASE_DIR, "media_face_cache"))
FACE_THUMB_PX = 200


@app.route("/api/media/face/<int:face_id>/thumb", methods=["GET"])
def media_face_thumb(face_id):
    """A square crop around one face.

    Cropped on the server, not with CSS: a grid of 200 people would otherwise
    pull 200 full-size originals off a spinning disk to show 200 thumbnails of
    a head. Cached on disk by face id, because a face's box never changes -
    re-detection replaces the row and gets a new id.
    """
    user = get_authenticated_user()
    if not user:
        return "401 Unauthorized", 401

    cached = os.path.join(FACE_THUMB_DIR, f"{face_id}.jpg")
    if os.path.isfile(cached):
        resp = send_file(cached, mimetype="image/jpeg", conditional=True)
        resp.headers["Cache-Control"] = "private, max-age=86400"
        return resp

    conn = media_ai.connect(BASE_DIR)
    row = conn.execute(
        "SELECT f.bbox, f.frame_ts, f.frame_w, m.root, m.path, m.kind"
        " FROM faces f JOIN media m ON m.id = f.media_id"
        " WHERE f.id = ?", (face_id,)).fetchone()
    if row is None:
        return "404 Not Found", 404
    if not root_visible_to(user, row["root"]):
        return "403 Forbidden", 403
    path = resolve_media_path(row["root"], row["path"])
    if not path or not os.path.isfile(path):
        return "404 Not Found", 404

    try:
        box = json.loads(row["bbox"] or "[]")
        x, y, w, h = (float(v) for v in box)
    except (ValueError, TypeError):
        return "404 Not Found", 404

    # ffmpeg, not Pillow: every other thumbnail in this app is made with
    # ffmpeg, it is already a hard dependency, and it reads the formats this
    # library actually contains (webp, avif, animated gif) without adding a
    # second imaging stack that would have to be kept in step.
    side = max(w, h) * 1.6
    cx, cy = x + w / 2, y + h / 2
    box = max(16, int(side))
    # ffmpeg slides an out-of-bounds crop back inside the frame but never
    # shrinks one: a square wider or taller than the picture fails outright
    # ("Invalid too big ... size"), and 1.6x a large face easily passes the
    # short side (a 1377 px face in a 2050 px wide picture asks for 2202). So
    # the square is capped at the short side INSIDE the filter, where iw/ih are
    # the frame really being cropped - after the video pre-scale below - and
    # re-centred from ow/oh so a capped square still frames the face. ffmpeg's
    # min() takes exactly two arguments, hence the nesting.
    crop = (f"crop=w='min({box},min(iw,ih))':h='min({box},min(iw,ih))'"
            f":x='clip({cx:.1f}-ow/2,0,iw-ow)':y='clip({cy:.1f}-oh/2,0,ih-oh)'"
            f":exact=0")
    os.makedirs(FACE_THUMB_DIR, exist_ok=True)
    tmp = cached + ".part"
    try:
        # A video face was detected in a sampled frame that the indexer had
        # already scaled down, so the crop must reproduce BOTH the seek and
        # that scale - otherwise the box is in one coordinate space and the
        # picture in another, and ffmpeg refuses a crop bigger than the frame.
        seek, pre = [], ""
        if row["frame_ts"] is not None:
            seek = ["-ss", f"{float(row['frame_ts']):.2f}"]
        if row["frame_w"]:
            pre = f"scale='min({int(row['frame_w'])},iw)':-2,"
        proc = subprocess.run(
            ["ffmpeg", "-v", "error", "-y", *seek, "-i", path, "-frames:v", "1",
             "-vf", f"{pre}{crop},"
                    f"scale={FACE_THUMB_PX}:{FACE_THUMB_PX}:force_original_aspect_ratio=decrease",
             # -f is REQUIRED because tmp ends in .part: ffmpeg infers the
             # muxer from the extension and cannot guess from ".part". This
             # exact mistake was already made once here, with the .mka remux.
             "-q:v", "3", "-f", "image2", tmp],
            capture_output=True, timeout=60)
        if proc.returncode != 0 or not os.path.getsize(tmp):
            print(f"[Warning] face crop {face_id}: "
                  f"{proc.stderr.decode('utf-8', 'replace')[:160]}")
            return "404 Not Found", 404
        os.replace(tmp, cached)
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"[Warning] face crop failed for {face_id}: {exc}")
        return "404 Not Found", 404

    resp = send_file(cached, mimetype="image/jpeg", conditional=True)
    resp.headers["Cache-Control"] = "private, max-age=86400"
    return resp


_indexing_cache = {"at": 0.0, "data": None}
INDEXING_CACHE_S = 5.0


# ---------------------------------------------------------------------------
# The review queue
# ---------------------------------------------------------------------------
# Every route here PROPOSES. The sweep writes rows and stops; accepting one is
# a separate request from a person, and it then goes down the same trash-and-
# ban path as deleting by hand. Nothing in this file deletes on its own.

@app.route("/api/media/profiles", methods=["GET"])
@require_admin
def media_profiles():
    """Accounts, what they look like, and what they have been marked as."""
    try:
        conn = media_ai.connect(BASE_DIR)
        shapes = media_review.profile_shapes(conn)
    except Exception as exc:
        print(f"[Warning] profiles unavailable: {exc}")
        return jsonify({"items": []})
    items = sorted(shapes.values(), key=lambda p: -p["files"])
    return jsonify({"items": items[:1000], "total": len(items)})


@app.route("/api/media/profiles/<source>/<handle>", methods=["POST"])
@require_admin
def media_profile_mark(source, handle):
    """Mark an account, or clear the mark and fall back to what the files say."""
    body = request.get_json(silent=True) or {}
    kind = (body.get("kind") or "").strip().lower()
    if kind not in ("", media_review.KIND_PHOTO, media_review.KIND_ANIME,
                    media_review.KIND_MIXED):
        return jsonify({"error": "unknown kind"}), 400
    # Absent means "leave it alone", so marking a kind does not silently reset
    # exemptions set earlier, and setting an exemption does not clear the kind.
    raw_exempt = body.get("exempt")
    if raw_exempt is None and body.get("allow_sketch") is not None:
        # The older single-flag form, still accepted.
        raw_exempt = ["sketch"] if body.get("allow_sketch") else []
    conn = media_ai.connect(BASE_DIR)

    row = conn.execute("SELECT kind, exempt FROM profiles"
                       " WHERE source=? AND handle=?", (source, handle)).fetchone()
    now_kind = kind or (row["kind"] if row else "")
    if raw_exempt is None:
        now_exempt = media_review.parse_exempt(row["exempt"] if row else "")
    else:
        if isinstance(raw_exempt, str):
            raw_exempt = raw_exempt.split(",")
        unknown = [str(x) for x in raw_exempt
                   if str(x).strip() not in media_review.EXEMPT_KEYS]
        if unknown:
            return jsonify({"error": "unknown exemption(s): %s" % ", ".join(unknown),
                            "known": list(media_review.EXEMPT_KEYS)}), 400
        now_exempt = media_review.parse_exempt(",".join(str(x) for x in raw_exempt))

    # The row exists to carry EITHER a kind or an exemption. Deleting it when
    # the kind is cleared would throw the exemptions away with it.
    if not now_kind and not now_exempt:
        conn.execute("DELETE FROM profiles WHERE source=? AND handle=?",
                     (source, handle))
    else:
        conn.execute(
            "INSERT INTO profiles (source, handle, kind, exempt, updated_at)"
            " VALUES (?, ?, ?, ?, ?) ON CONFLICT(source, handle)"
            " DO UPDATE SET kind=excluded.kind, exempt=excluded.exempt,"
            " updated_at=excluded.updated_at",
            (source, handle, now_kind, ",".join(sorted(now_exempt)), time.time()))
    conn.commit()
    return jsonify({"ok": True, "source": source, "handle": handle,
                    "kind": now_kind or None, "exempt": sorted(now_exempt)})


_review_sweep = {"running": False, "at": 0.0, "result": None}
_review_auto = {"running": False, "started": 0.0, "finished": 0.0, "total": 0,
                "done": 0, "trashed": 0, "missing": 0, "failed": 0,
                "batches": [], "by_reason": {}, "error": None}
AUTO_TRASH_CHUNK = 500


def auto_trash_pending():
    """Trash every pending suggestion of a rule the user trusts to act alone.

    See media_review.AUTO_TRASH_REASONS for which rules and why. It goes
    through trash_resolved - the same trash, ban and tombstone path as a review
    discard - so every file stays restorable, and each batch manifest names the
    rule instead of a person. No LABEL is written: a label is a person's
    judgement, and counting the rule's own actions as confirmations would let
    its precision grade itself. Chunks of AUTO_TRASH_CHUNK keep one trash batch
    to a size a restore can still be reasoned about.
    """
    reasons = auto_trash_reasons()
    st = _review_auto
    st.update({"running": True, "started": time.time(), "finished": 0.0, "total": 0,
               "done": 0, "trashed": 0, "missing": 0, "failed": 0,
               "batches": [], "by_reason": {}, "error": None})
    try:
        conn = media_ai.connect(BASE_DIR)
        holes = ",".join("?" * len(reasons))
        rows = conn.execute(
            "SELECT s.media_id, s.reason, m.root, m.path FROM suggestions s"
            " JOIN media m ON m.id = s.media_id"
            " WHERE s.state = 'pending' AND m.trashed IS NULL"
            f" AND s.reason IN ({holes})", list(reasons)).fetchall()
        st["total"] = len(rows)
        # One rule per trash batch, named in its manifest, so undoing what one
        # rule did is a list of batches rather than a search through mixed ones.
        work = []
        for reason in reasons:
            mine = [r for r in rows if r["reason"] == reason]
            work += [(reason, mine[i:i + AUTO_TRASH_CHUNK])
                     for i in range(0, len(mine), AUTO_TRASH_CHUNK)]
        for reason, chunk in work:
            user = {"username": "auto: " + reason}
            resolved, ids = [], {}
            for r in chunk:
                full = resolve_media_path(r["root"], r["path"])
                if not full or not os.path.exists(full):
                    st["missing"] += 1
                    continue
                resolved.append((r["root"], r["path"], full))
                ids[(r["root"], r["path"])] = r["media_id"]
            if resolved:
                batch, deleted, _notes, failed = trash_resolved(resolved, DELETE_BAN, user)
                st["batches"].append(batch)
                st["trashed"] += len(deleted)
                st["by_reason"][reason] = st["by_reason"].get(reason, 0) + len(deleted)
                st["failed"] += len(failed)
                now = time.time()
                conn.executemany(
                    "UPDATE suggestions SET state='deleted', decided_at=?"
                    " WHERE media_id=? AND state='pending'",
                    [(now, ids[(d["root"], d["path"])]) for d in deleted])
                conn.commit()
            st["done"] += len(chunk)
    except Exception as exc:
        st["error"] = str(exc)
        print(f"[Warning] auto-trash failed: {exc}")
    finally:
        st["running"] = False
        st["finished"] = time.time()


def start_auto_trash():
    """Start the pass in the background; False if one is running or no rule is trusted."""
    if _review_auto["running"] or not auto_trash_reasons():
        return False
    _review_auto["running"] = True        # claimed before the thread exists
    threading.Thread(target=auto_trash_pending, daemon=True, name="auto-trash").start()
    return True


@app.route("/api/media/review/auto", methods=["GET", "POST"])
@require_admin
def media_review_auto():
    """Status of the automatic trash pass; POST starts one now."""
    started = start_auto_trash() if request.method == "POST" else False
    return jsonify(dict(_review_auto, started=started,
                        reasons=list(auto_trash_reasons())))


# --- Twitter posts cut into pieces ------------------------------------------
# post_stitch.py decides which pictures of a post are one picture and joins
# them; this is what happens around it (user, 2026-09-23: replace the pieces,
# for new downloads and the library already there). The pieces go to the
# trash with a BAN: gallery-dl skips a file that exists, so the 0-byte
# tombstone is what keeps the next run of that account from fetching them
# again. Favourites and share links that named a piece name the joined
# picture instead. One pass at a time (_stitch_lock): the library pass and a
# finished download never work on the same post at once.
#
# Threads, not processes: server.py runs as __main__, and a spawned worker
# would import it again - config.json rewritten, the app built - in every
# child. Pillow and numpy let go of the GIL while decoding, and the work is
# disk-bound anyway; each worker thread lowers its own priority.
STITCH_CHUNK = 100            # posts measured per round
STITCH_BATCH = 200            # joined pictures per trash batch - joins are rare (~0.4% of
                              # posts), so a batch per round would litter the trash page
STITCH_WORKERS = 4
_stitch_lock = threading.Lock()
_stitch = {"running": False, "scope": None, "started": 0.0, "finished": 0.0, "posts": 0,
           "done": 0, "joined": 0, "pieces": 0, "failed": 0, "batches": [], "error": None}
# The last finished pass of each scope: a download finishing right after the
# library pass starts a pass of its own, which resets _stitch - and the library
# pass's result was lost the first time it ran.
_stitch_last = {}


def twitter_stitch_enabled():
    return bool((CONFIG.get("twitter_stitch") or {}).get("enabled", True))


def _carry_to_joined(moves):
    """moves: {piece path: joined path}, both in the downloads root. Every
    account that favourited a piece gets the joined picture as a favourite;
    a share that listed a piece lists the joined picture instead, once."""
    if not moves:
        return
    root = DEFAULT_ROOT_ID
    with _favorites_lock:
        fans = [(user, set(roots.get(root) or ())) for user, roots in _favorites_data().items()]
    for user, paths in fans:
        for joined in {moves[p] for p in paths if p in moves}:
            set_favorite(user, root, joined, True)
    with shares_lock:
        shares = _read_shares_unlocked()
        changed = False
        for share in shares:
            if share.get("kind") == "file" and share_root_of(share) == root and share.get("path") in moves:
                share["path"] = moves[share["path"]]
                changed = True
            elif share.get("kind") == "pool":
                items, seen, hit = [], set(), False
                for item in share.get("items", []):
                    if item.get("kind") == "file" and share_root_of(item) == root and item.get("path") in moves:
                        item = dict(item, path=moves[item["path"]])
                        hit = True
                    key = (share_root_of(item), item.get("path"))
                    if key not in seen:
                        seen.add(key)
                        items.append(item)
                if hit:
                    share["items"] = items
                    changed = True
        if changed:
            _write_shares_unlocked(shares)


def stitch_twitter_posts(posts, who, recent=False):
    """Joins what belongs together in these posts and trashes the pieces.

    posts: [(author folder, tweet id, [(n, name), ...])] (post_stitch.posts_in).
    who names the trash batches; recent puts the joined pictures in the
    Recently downloaded strip - for a new download, not the library pass.

    A joined picture waits under a hidden name until its pieces are in the
    trash, then takes its own: a stop in between leaves the pieces and a
    hidden file the next pass overwrites - never the pieces AND the joined
    picture in the library, which a later pass would take for pieces
    restored on purpose and leave."""
    from concurrent.futures import ThreadPoolExecutor
    st = _stitch
    pending = []                          # [(author folder, group)] waiting for the trash

    def rel(path):
        return os.path.relpath(path, DOWNLOADS_DIR).replace(os.sep, "/")

    def flush():
        resolved = [(DEFAULT_ROOT_ID, rel(os.path.join(folder, name)), os.path.join(folder, name))
                    for folder, group in pending for name in group["pieces"]]
        batch, deleted, _notes, failed = trash_resolved(resolved, DELETE_BAN, {"username": who})
        st["batches"].append(batch)
        st["pieces"] += len(deleted)
        st["failed"] += len(failed)
        gone = {d["path"] for d in deleted}
        moves, made = {}, []
        for folder, group in pending:
            part = os.path.join(folder, post_stitch.part_name(group["out"]))
            pieces = [rel(os.path.join(folder, name)) for name in group["pieces"]]
            if not any(p in gone for p in pieces):
                os.remove(part)           # nothing moved: the post stays as it was
                continue
            os.replace(part, os.path.join(folder, group["out"]))
            joined = rel(os.path.join(folder, group["out"]))
            made.append(joined)
            moves.update((p, joined) for p in pieces)
        pending.clear()
        _carry_to_joined(moves)
        st["joined"] += len(made)
        if made and recent:
            recent_media_store.record_recent_media(
                BASE_DIR, [{"root": DEFAULT_ROOT_ID, "path": p} for p in made])

    with ThreadPoolExecutor(max_workers=STITCH_WORKERS, initializer=post_stitch.lower_priority,
                            thread_name_prefix="stitch") as pool:
        for start in range(0, len(posts), STITCH_CHUNK):
            chunk = posts[start:start + STITCH_CHUNK]
            results = pool.map(post_stitch.stitch_post, [p[0] for p in chunk],
                               [p[1] for p in chunk], [p[2] for p in chunk])
            for (folder, tid, _items), res in zip(chunk, results):
                if isinstance(res, dict):
                    st["failed"] += 1
                    print(f"[Warning] stitch {folder} {tid}: {res['error']}", flush=True)
                    continue
                # A picture already joined before is left be: pieces that are
                # back beside it were restored from the trash on purpose.
                pending.extend((folder, group) for group in res if not group.get("existing"))
            st["done"] += len(chunk)
            if len(pending) >= STITCH_BATCH:
                flush()
    if pending:
        flush()


def run_stitch(scope, find_posts, who, recent):
    st = _stitch
    with _stitch_lock:
        st.update(running=True, scope=scope, started=time.time(), finished=0.0, posts=0, done=0,
                  joined=0, pieces=0, failed=0, batches=[], error=None)
        try:
            posts = find_posts()
            st["posts"] = len(posts)
            stitch_twitter_posts(posts, who, recent)
        except Exception as exc:
            st["error"] = str(exc)
            print(f"[Warning] stitch ({scope}) failed: {exc}", flush=True)
        finally:
            st["running"] = False
            st["finished"] = time.time()
            _stitch_last[scope] = dict(st, batches=list(st["batches"]))


def _library_twitter_posts():
    base = os.path.join(DOWNLOADS_DIR, "twitter")
    out = []
    for author in sorted(os.listdir(base)):
        folder = os.path.join(base, author)
        if author.startswith(".") or not os.path.isdir(folder):
            continue
        out += [(folder, tid, items) for tid, items in sorted(post_stitch.posts_in(folder).items())]
    return out


def start_stitch_library():
    """The whole library, once, in the background; False if a pass is running."""
    if _stitch["running"]:
        return False
    _stitch["running"] = True             # claimed before the thread exists
    threading.Thread(target=run_stitch, args=("library", _library_twitter_posts, "auto: stitch (library)", False),
                     daemon=True, name="stitch-library").start()
    return True


def stitch_after_download(new_paths, then=None):
    """The posts a finished Twitter download brought, in the background. `then`
    (optional) runs when the pass is over - or at once when there is nothing to
    join - so a step that has to see the joined pictures comes after them."""
    wanted = {}
    for p in new_paths:
        rel = p.get("path") or ""
        m = post_stitch.PIECE_RE.match(os.path.basename(rel))
        if m and rel.startswith("twitter/"):
            wanted.setdefault(os.path.dirname(os.path.join(DOWNLOADS_DIR, rel)), set()).add(m.group(1))
    if not wanted:
        if then:
            then()
        return

    def find_posts():
        out = []
        for folder, tids in wanted.items():
            found = post_stitch.posts_in(folder)
            out += [(folder, tid, found[tid]) for tid in sorted(tids) if tid in found]
        return out

    def stitch_then():
        run_stitch("download", find_posts, "auto: stitch", True)
        if then:
            then()

    threading.Thread(target=stitch_then, daemon=True, name="stitch-download").start()


@app.route("/api/twitter/stitch", methods=["GET", "POST"])
@require_admin
def twitter_stitch():
    """Status of the current or last stitch pass, and the last finished pass of
    each scope ("library", "download"); POST starts one over the whole library."""
    started = start_stitch_library() if request.method == "POST" else False
    return jsonify(dict(_stitch, started=started, enabled=twitter_stitch_enabled(), last=_stitch_last))


# --- Exact copies of a picture -------------------------------------------------
# image_dedup.py decides which pictures are byte-identical and what to do with
# them; this is what happens around it (user, 2026-10-27): the same picture in
# DIFFERENT folders becomes one file with several names (hard links - every name
# stays where it is), and a folder keeps ONE name per picture - the oldest; the
# other names go to the trash with a BAN (a tombstone, so gallery-dl does not
# fetch the re-posted picture again). Favourites and share links that named a
# trashed name name the one that stays. Out of scope: look-alikes.
#
# A name that is trashed is first made a link to the file that stays, so the
# trash holds a name, not a second copy of the bytes - the space comes back at
# once, and a restore brings back a name of the same picture.
#
# A restore from such a batch exempts the names it brings back: a later pass
# leaves them be (as the character blacklist does). Same-post copies (123_1 and
# 123_2 identical) are never trashed against each other - they may be the pieces
# of one cut picture (post_stitch.py) or a post that meant it.
#
# Library pass: POST /api/images/dedup - a DRY RUN unless the body says
# {"dry_run": false}; it walks the library once (a stat per picture), reads only
# files whose size another file shares, one thread at low priority, and remembers
# every hash per inode in image_dedup.sqlite. After a download only the folders
# it wrote to and the files of the same size elsewhere are looked at.
IMGDEDUP_REASON = "image dedup: exact copy in the same folder"   # the manifest `reason` starts with this
IMGDEDUP_BATCH = 500          # names per trash batch
IMGDEDUP_CHUNK = 50           # groups per locked round of changes
_idd_pass_lock = threading.Lock()      # one library pass at a time
_idd_apply_lock = threading.Lock()     # one round of changes at a time (library pass and downloads)
_idd_store_lock = threading.Lock()
_idd_store_obj = None
_IDD_COUNTERS = {"files": 0, "groups": 0, "plan_trash": 0, "plan_links": 0, "saved_bytes": 0,
                 "trashed": 0, "linked": 0, "refused": 0, "failed": 0, "done_groups": 0}
_idd = dict(_IDD_COUNTERS, batches=[], running=False, scope=None, dry_run=True, phase="", started=0.0,
            finished=0.0, stop=False, error=None, report=None)
_idd_dl = dict(_IDD_COUNTERS, batches=[], running=0, scope="download", finished=0.0, error=None)
_idd_last = {}


def image_dedup_enabled():
    return bool((CONFIG.get("image_dedup") or {}).get("enabled", True))


def _idd_store():
    global _idd_store_obj
    with _idd_store_lock:
        if _idd_store_obj is None:
            _idd_store_obj = image_dedup.Store(os.path.join(BASE_DIR, "image_dedup.sqlite"))
        return _idd_store_obj


def _idd_accept(name):
    return "image" in kinds_of(name) and not _is_hidden_or_partial(name)


def image_dedup_exempt(pairs):
    """Names restored from a dedup batch: never trashed by a later pass."""
    rels = [rel for root_id, rel in pairs if (root_id or DEFAULT_ROOT_ID) == DEFAULT_ROOT_ID]
    if rels:
        try:
            _idd_store().exempt_add(rels)
        except Exception as exc:
            print(f"[Warning] image dedup: restored names not exempted: {exc}", flush=True)


def image_dedup_apply(groups, who, st):
    """Carries out plan groups (image_dedup.build_plan): every name that is not on
    the file that stays is linked to it (verified byte for byte first), then the
    extra names of a folder are trashed with a ban. Anything that no longer is
    what the plan saw is refused and left as it was."""
    base = DOWNLOADS_DIR
    pending = []                                # [(trashed rel, keeper rel)]
    touched = set()

    def flush():
        if not pending:
            return
        resolved = [(DEFAULT_ROOT_ID, rel, os.path.join(base, *rel.split("/"))) for rel, _k in pending]
        batch, deleted, _notes, failed = trash_resolved(
            resolved, DELETE_BAN, {"username": who}, reason=IMGDEDUP_REASON)
        st["batches"].append(batch)
        st["trashed"] += len(deleted)
        st["failed"] += len(failed)
        gone = {d["path"] for d in deleted}
        _carry_to_joined({rel: keeper for rel, keeper in pending if rel in gone})
        pending.clear()

    for start in range(0, len(groups), IMGDEDUP_CHUNK):
        if st.get("stop"):
            break
        with _idd_apply_lock:
            ai_pairs = []
            for g in groups[start:start + IMGDEDUP_CHUNK]:
                canon = g["canon"]
                refused = set()
                for e in {x.rel: x for x in g["links"] + g["trash_links"]}.values():
                    try:
                        if image_dedup.relink(base, canon, e):
                            st["linked"] += 1
                            touched.add(e.rel.rsplit("/", 1)[0])
                            ai_pairs.append((e.rel, f"{canon.dev}:{canon.ino}"))
                    except image_dedup.Refused as exc:
                        refused.add(e.rel)
                        st["refused"] += 1
                        print(f"[Info] image dedup: {e.rel} left as it was: {exc}", flush=True)
                for e, keeper in g["trash"]:
                    if e.rel not in refused:
                        pending.append((e.rel, keeper.rel))
                        touched.add(e.rel.rsplit("/", 1)[0])
                st["done_groups"] += 1
            if ai_pairs:
                try:
                    image_dedup.set_ai_inodes(media_ai.connect(BASE_DIR), DEFAULT_ROOT_ID, ai_pairs)
                except Exception as exc:
                    print(f"[Warning] image dedup: AI index inodes not updated: {exc}", flush=True)
            if len(pending) >= IMGDEDUP_BATCH:
                flush()
    with _idd_apply_lock:
        flush()
        store = _idd_store()
        for folder in touched:                  # the stored walk shows what the folders are now
            store.replace_folder(folder, image_dedup.scan_folder(base, folder, _idd_accept))
    if touched:
        invalidate_media_caches()


def _idd_report(st, plan, hasher):
    """The plan's numbers, into the status and as a report dict."""
    stats = plan["stats"]
    st.update(groups=stats.get("groups", 0), plan_trash=stats.get("trash_names", 0),
              plan_links=stats.get("link_names", 0), saved_bytes=stats.get("saved_bytes", 0))
    return dict(stats, hashed_files=hasher.files, hashed_bytes=hasher.bytes, quick_files=hasher.quick_files,
                hash_cached=hasher.cached,
                hash_errors=hasher.errors, examples=plan["examples"])


def run_image_dedup_library(dry_run):
    st = _idd
    with _idd_pass_lock:
        st.update(_IDD_COUNTERS, batches=[], running=True, scope="library", dry_run=dry_run, phase="walking",
                  started=time.time(), finished=0.0, stop=False, error=None, report=None)
        try:
            store = _idd_store()
            walk_stats = {}
            entries = []
            for e in image_dedup.walk(DOWNLOADS_DIR, accept=_idd_accept, cancel=lambda: st["stop"], stats=walk_stats):
                entries.append(e)
                st["files"] = len(entries)
            if st["stop"]:
                raise RuntimeError("stopped while walking")
            store.replace_files(entries)
            st["phase"] = "hashing"
            hasher = image_dedup.Hasher(store, DOWNLOADS_DIR, cancel=lambda: st["stop"])
            plan = image_dedup.build_plan(entries, hasher, store.exempt_all(), cancel=lambda: st["stop"])
            if st["stop"]:
                raise RuntimeError("stopped while hashing")
            report = _idd_report(st, plan, hasher)
            report.update(images=len(entries), image_bytes=sum(e.size for e in entries),
                          tombstones=walk_stats.get("tombstones", 0), stale_temps=len(walk_stats.get("temps", [])),
                          seconds=round(time.time() - st["started"], 1))
            st["report"] = report
            if not dry_run:
                st["phase"] = "applying"
                with _idd_apply_lock:
                    image_dedup.sweep_temps(DOWNLOADS_DIR, walk_stats.get("temps", []))
                image_dedup_apply(plan["groups"], "auto: image dedup (library)", st)
                if not st["stop"]:
                    store.prune_hashes({(e.dev, e.ino) for e in entries})
            st["phase"] = "done"
        except Exception as exc:
            st["error"] = str(exc)
            print(f"[Warning] image dedup (library) failed: {exc}", flush=True)
        finally:
            st["running"] = False
            st["finished"] = time.time()
            _idd_last["library-dry" if dry_run else "library"] = dict(st, batches=list(st["batches"]))


def _idd_library_thread(dry_run):
    image_dedup.lower_priority()
    run_image_dedup_library(dry_run)


def start_image_dedup_library(dry_run=True):
    """The whole library, once, in the background; False if a pass is running."""
    if _idd["running"]:
        return False
    _idd["running"] = True                    # claimed before the thread exists
    threading.Thread(target=_idd_library_thread, args=(dry_run,), daemon=True, name="imgdedup-library").start()
    return True


def _idd_download_pass(wanted):
    image_dedup.lower_priority()
    st = _idd_dl
    st["running"] += 1
    try:
        store = _idd_store()
        entries = {}
        for folder in sorted({r.rsplit("/", 1)[0] for r in wanted}):
            found = image_dedup.scan_folder(DOWNLOADS_DIR, folder, _idd_accept)
            store.replace_folder(folder, found)
            entries.update((e.rel, e) for e in found)
        new = {r for r in wanted if r in entries}
        for size in {entries[r].size for r in new}:
            for c in store.with_size(size):         # the same size in other folders, from the last library walk
                if c.rel in entries:
                    continue
                try:
                    s = os.lstat(os.path.join(DOWNLOADS_DIR, *c.rel.split("/")))
                except OSError:
                    continue
                if s.st_size == c.size:
                    entries[c.rel] = image_dedup.Entry(c.rel, s.st_size, s.st_mtime_ns, s.st_dev, s.st_ino, s.st_nlink)
        hasher = image_dedup.Hasher(store, DOWNLOADS_DIR)
        plan = image_dedup.build_plan(list(entries.values()), hasher, store.exempt_all())
        mine = [g for g in plan["groups"]
                if new & ({g["canon"].rel} | {e.rel for e in g["links"]} | {e.rel for e, _k in g["trash"]})]
        if mine:
            image_dedup_apply(mine, "auto: image dedup", st)
    except Exception as exc:
        st["error"] = str(exc)
        print(f"[Warning] image dedup (download) failed: {exc}", flush=True)
    finally:
        st["running"] -= 1
        st["finished"] = time.time()
        _idd_last["download"] = dict(st, batches=list(st["batches"]))


def image_dedup_after_download(new_paths):
    """The pictures a finished download brought: another name for the same picture
    in their folder is trashed (the oldest stays), a copy in another folder
    becomes a link. In the background."""
    wanted = sorted({p.get("path") or "" for p in new_paths
                     if (p.get("root") or DEFAULT_ROOT_ID) == DEFAULT_ROOT_ID
                     and (p.get("path") or "").split("/")[0] in image_dedup.CATEGORIES
                     and _idd_accept(os.path.basename(p.get("path") or ""))})
    if wanted:
        threading.Thread(target=_idd_download_pass, args=(wanted,), daemon=True, name="imgdedup-download").start()


@app.route("/api/images/dedup", methods=["GET", "POST"])
@require_admin
def image_dedup_status():
    """GET: the status of the library pass, the last finished pass of each scope
    ("library", "library-dry", "download") and, in "report", the numbers of the
    last library plan. POST starts a library pass - a dry run that changes nothing
    unless the body is {"dry_run": false}; {"stop": true} asks a running pass to
    stop."""
    data = request.get_json(silent=True) or {}
    started = False
    if request.method == "POST":
        if data.get("stop"):
            _idd["stop"] = True
        else:
            started = start_image_dedup_library(dry_run=data.get("dry_run") is not False)
    return jsonify(dict(_idd, started=started, enabled=image_dedup_enabled(), last=_idd_last, download=_idd_dl))


@app.route("/api/media/review/scan", methods=["POST"])
@require_admin
def media_review_scan():
    """Re-read every tagged file and refresh the queue.

    Synchronous: it is a few seconds over a few thousand rows, and a
    background job here would need its own progress channel to say something
    the caller already knows.
    """
    if _review_sweep["running"]:
        return jsonify({"error": "a scan is already running"}), 409
    _review_sweep["running"] = True
    try:
        conn = media_ai.connect(BASE_DIR)
        result = media_review.sweep(conn)
    except Exception as exc:
        print(f"[Warning] review sweep failed: {exc}")
        return jsonify({"error": str(exc)}), 500
    finally:
        _review_sweep["running"] = False
    _review_sweep.update({"at": time.time(), "result": result})
    # Rules the user trusts to act alone are applied straight after the scan,
    # in the background - trashing thousands of files takes minutes.
    result = dict(result, auto_trash_started=start_auto_trash())
    return jsonify(result)


@app.route("/api/media/review", methods=["GET"])
@require_admin
def media_review_queue():
    """What the sweep thinks does not belong, worst first."""
    user = get_authenticated_user()
    try:
        limit = min(500, max(1, int(request.args.get("limit", 100))))
    except ValueError:
        limit = 100
    state = (request.args.get("state") or "pending").strip().lower()
    if state not in ("pending", "kept", "deleted"):
        state = "pending"

    # KEYSET paging, not OFFSET. The queue shrinks while it is being worked -
    # deciding a batch removes those rows from `pending` - so the Nth row is
    # not the same row it was a moment ago, and an offset would step straight
    # over as many suggestions as had just been decided. Resuming from the last
    # row actually seen cannot skip anything, however much the list moved.
    after_score = request.args.get("after_score")
    after_id = request.args.get("after_id")
    # One rule at a time when the person asks for it: working through one kind
    # of suggestion in a row is quicker, and the answers more consistent, than
    # a queue that mixes food photos with sketches.
    reason = (request.args.get("reason") or "").strip()
    where = "s.state = ? AND m.trashed IS NULL"
    args = [state]
    if reason:
        where += " AND s.reason = ?"
        args.append(reason)
    if after_score is not None and after_id is not None:
        try:
            # Same tuple as the sort: score descending, then media_id ascending
            # to break ties, so "after" means strictly later in that order.
            args += [float(after_score), float(after_score), int(after_id)]
            where += " AND (COALESCE(s.score, 0) < ?" \
                     " OR (COALESCE(s.score, 0) = ? AND s.media_id > ?))"
        except (TypeError, ValueError):
            pass
    args.append(limit)

    conn = media_ai.connect(BASE_DIR)
    rows = conn.execute(
        "SELECT s.media_id, s.reason, s.detail, s.score, m.root, m.path"
        " FROM suggestions s JOIN media m ON m.id = s.media_id"
        f" WHERE {where}"
        " ORDER BY COALESCE(s.score, 0) DESC, s.media_id LIMIT ?", args).fetchall()

    allowed = {r["id"] for r in roots_for_user(user)}
    wanted = [(r["root"], r["path"], False) for r in rows if r["root"] in allowed]
    entries = {(e["root"], e["path"]): e for e in build_entries_for(wanted)}

    items = []
    for r in rows:
        entry = entries.get((r["root"], r["path"]))
        if not entry:
            continue
        items.append({**entry, "media_id": r["media_id"], "reason": r["reason"],
                      "detail": r["detail"], "score": r["score"],
                      "profile": (media_review.profile_of(r["path"]) or (None, None))[1]})

    counts = {r["state"]: r["n"] for r in conn.execute(
        "SELECT state, COUNT(*) AS n FROM suggestions GROUP BY state")}
    # Per-rule counts for the picker, over what the queue can actually show (a
    # trashed file is not in it). First page only: paging does not change
    # which rules have something waiting.
    reasons = None
    if after_id is None:
        reasons = [{"reason": r["reason"], "n": r["n"]} for r in conn.execute(
            "SELECT s.reason AS reason, COUNT(*) AS n FROM suggestions s"
            " JOIN media m ON m.id = s.media_id"
            " WHERE s.state = 'pending' AND m.trashed IS NULL"
            " GROUP BY s.reason ORDER BY n DESC")]
    try:
        stats = media_review.label_stats(conn)
    except Exception as exc:
        print(f"[Warning] label stats failed: {exc}")
        stats = {}
    last = rows[-1] if rows else None
    in_view = None
    if reasons is not None:
        in_view = sum(r["n"] for r in reasons if not reason or r["reason"] == reason)
    return jsonify({"items": items, "counts": counts, "state": state,
                    "reason": reason or None, "reasons": reasons,
                    "pending_in_view": in_view,
                    "stats": stats,
                    "has_more": len(rows) == limit,
                    "next_score": (last["score"] if last else None) or 0,
                    "next_id": last["media_id"] if last else None})


@app.route("/api/media/review/decide", methods=["POST"])
@require_admin
def media_review_decide():
    """Keep or delete reviewed files. Deleting goes through the ordinary
    trash, including the ban that keeps a file from coming back."""
    user = get_authenticated_user()
    body = request.get_json(silent=True) or {}
    ids = [int(i) for i in (body.get("media_ids") or []) if str(i).isdigit()]
    decision = (body.get("decision") or "").strip().lower()
    mode = DELETE_BAN if body.get("mode", "ban") == "ban" else DELETE_RELEASE
    if not ids:
        return jsonify({"error": "nothing to decide on"}), 400
    if decision not in ("keep", "keep_anyway", "delete"):
        return jsonify(
            {"error": "decision must be keep, keep_anyway or delete"}), 400

    conn = media_ai.connect(BASE_DIR)
    holes = ",".join("?" * len(ids))
    rows = conn.execute(
        f"SELECT id, root, path FROM media WHERE id IN ({holes})", ids).fetchall()

    # A durable label for every decision, in both branches. `suggestions` is
    # queue state - it tracks what still needs looking at - while `labels` is
    # the record of what was judged, and that has to outlive the file.
    shapes = {}
    try:
        shapes = media_review.profile_shapes(conn)
    except Exception:
        pass

    def label_all(verdict, acted, only=None):
        for r in rows:
            if only is not None and (r["root"], r["path"]) not in only:
                continue
            try:
                media_review.record_label(
                    conn, r["id"], r["path"], r["root"], "review", verdict,
                    acted=acted, by=user["username"], shapes=shapes)
            except Exception as exc:
                print(f"[Warning] label not recorded for {r['path']}: {exc}")

    if decision in ("keep", "keep_anyway"):
        # Both leave the file alone; they differ in what they say about the
        # RULE. "keep" means the suggestion was wrong. "keep_anyway" means it
        # was right and the file is wanted regardless - which is a correct
        # flag, and must not be recorded as a false positive or the next
        # classifier learns to stop finding these.
        label_all(media_review.VERDICT_BELONGS if decision == "keep"
                  else media_review.VERDICT_NOT, "kept")
        conn.executemany(
            "UPDATE suggestions SET state='kept', decided_at=? WHERE media_id=?",
            [(time.time(), i) for i in ids])
        conn.commit()
        return jsonify({"kept": len(ids), "decision": decision,
                        "stats": media_review.label_stats(conn)})

    resolved, failed = [], []
    for r in rows:
        if not root_visible_to(user, r["root"]):
            failed.append({"path": r["path"], "error": "forbidden"})
            continue
        full = resolve_media_path(r["root"], r["path"])
        if not full or not os.path.exists(full):
            failed.append({"path": r["path"], "error": "not found"})
            continue
        resolved.append((r["root"], r["path"], full))

    done_paths = {(r[0], r[1]) for r in resolved}
    # BEFORE the files move: record_label reads the file's tags, and the row
    # is about to be marked trashed.
    label_all(media_review.VERDICT_NOT, "deleted", only=done_paths)

    batch, deleted, notes, more_failed = trash_resolved(resolved, mode, user)
    failed.extend(more_failed)
    done = {(d["root"], d["path"]) for d in deleted}
    conn.executemany(
        "UPDATE suggestions SET state='deleted', decided_at=? WHERE media_id=?",
        [(time.time(), r["id"]) for r in rows if (r["root"], r["path"]) in done])
    conn.commit()
    return jsonify({"batch": batch, "deleted": len(deleted), "failed": failed,
                    "mode": mode})


@app.route("/api/media/training", methods=["GET"])
@require_admin
def media_training_queue():
    """A random sample to judge blind.

    The response deliberately does NOT say whether the rules flagged a file,
    what reason they gave or what they scored. A label cast while looking at
    the machine's opinion is a vote on that opinion, and the whole point of
    this queue is a judgement the rules did not influence.
    """
    user = get_authenticated_user()
    try:
        limit = min(100, max(1, int(request.args.get("limit", 24))))
    except ValueError:
        limit = 24

    conn = media_ai.connect(BASE_DIR)
    try:
        rows = media_review.sample_for_training(conn, limit)
    except Exception as exc:
        print(f"[Warning] training sample failed: {exc}")
        rows = []

    allowed = {r["id"] for r in roots_for_user(user)}
    wanted = [(r["root"], r["path"], False) for r in rows if r["root"] in allowed]
    entries = {(e["root"], e["path"]): e for e in build_entries_for(wanted)}

    # Other files from the same account, as the reference to judge against.
    # Fetched per profile rather than per file so a batch of 24 costs one
    # query per distinct account, and the same strip is reused across a
    # profile's several files in the sample.
    wanted_profiles = {}
    for r in rows:
        who = media_review.profile_of(r["path"])
        if who:
            wanted_profiles.setdefault(who, []).append(r["path"])
    siblings = {}
    for who, own in wanted_profiles.items():
        prefix = f"{who[0]}/{who[1]}/"
        try:
            near = conn.execute(
                "SELECT root, path FROM media WHERE path LIKE ? AND trashed IS NULL"
                " ORDER BY RANDOM() LIMIT 12", (prefix + "%",)).fetchall()
        except Exception:
            near = []
        picked = [(n["root"], n["path"], False) for n in near
                  if n["path"] not in own and n["root"] in allowed][:4]
        siblings[who] = [{"root": e["root"], "path": e["path"],
                          "mtime": e["mtime"], "type": e.get("type")}
                         for e in build_entries_for(picked)]

    items = []
    for r in rows:
        entry = entries.get((r["root"], r["path"]))
        if not entry:
            continue
        who = media_review.profile_of(r["path"])
        items.append({**entry, "media_id": r["id"],
                      "profile": who[1] if who else None,
                      "siblings": siblings.get(who, [])})

    try:
        stats = media_review.label_stats(conn)
    except Exception as exc:
        print(f"[Warning] label stats failed: {exc}")
        stats = {}
    return jsonify({"items": items, "stats": stats})


@app.route("/api/media/training/decide", methods=["POST"])
@require_admin
def media_training_decide():
    """Record a blind judgement. This queue LABELS; it does not delete.

    A file judged not to belong is left exactly where it is - the training
    sample has to stay a random sample of the library, and quietly deleting
    from it would make the next sample a sample of what survived previous
    rounds of judgement.
    """
    user = get_authenticated_user()
    body = request.get_json(silent=True) or {}

    # A batch, because the page stages answers and sends them together - one
    # request per click made a slip permanent before it was visible. The
    # single-answer form is still accepted.
    batch = body.get("decisions")
    if batch is None:
        batch = [{"media_id": body.get("media_id"),
                  "verdict": body.get("verdict")}]
    if not isinstance(batch, list) or not batch:
        return jsonify({"error": "nothing to record"}), 400

    verdicts = (media_review.VERDICT_BELONGS, media_review.VERDICT_NOT,
                media_review.VERDICT_UNSURE)
    wanted = {}
    for item in batch:
        verdict = (str(item.get("verdict") or "")).strip().lower()
        if verdict not in verdicts:
            return jsonify({"error": f"unknown verdict: {verdict}"}), 400
        try:
            wanted[int(item.get("media_id"))] = verdict
        except (TypeError, ValueError):
            return jsonify({"error": "media_id required"}), 400

    conn = media_ai.connect(BASE_DIR)
    holes = ",".join("?" * len(wanted))
    rows = conn.execute(
        f"SELECT id, root, path FROM media WHERE id IN ({holes})",
        list(wanted)).fetchall()
    if not rows:
        return jsonify({"error": "no such file"}), 404
    try:
        shapes = media_review.profile_shapes(conn)
    except Exception:
        shapes = {}

    recorded, refused = 0, []
    for row in rows:
        if not root_visible_to(user, row["root"]):
            refused.append(row["path"])
            continue
        media_review.record_label(conn, row["id"], row["path"], row["root"],
                                  "training", wanted[row["id"]], acted="none",
                                  by=user["username"], shapes=shapes)
        recorded += 1
    if refused:
        return jsonify({"error": "Forbidden", "paths": refused}), 403
    return jsonify({"ok": True, "recorded": recorded,
                    "stats": media_review.label_stats(conn)})


@app.route("/api/media/labels/mine", methods=["GET"])
@require_admin
def media_labels_mine():
    """Your own answers, for checking. Suspicious ones first, then newest.

    The suspicion check is internal-consistency only - the same post answered
    two ways, near-identical pictures answered two ways, a lone odd answer in
    an otherwise consistent account. Deliberately NOT "the rules disagree with
    you": the rules are what these labels measure, and grading the labels by
    them would be circular.
    """
    user = get_authenticated_user()
    # Disagreement is the default because it is the only view with a job:
    # the rest of the panel just lists answers back, while this one is where
    # the rules are wrong and where a learned rule gets its training signal.
    verdict = (request.args.get("verdict") or "disagree").strip().lower()
    if verdict not in ("belongs", "not_belongs", "unsure", "all", "disagree"):
        verdict = "disagree"
    try:
        limit = min(300, max(1, int(request.args.get("limit", 60))))
    except ValueError:
        limit = 60

    conn = media_ai.connect(BASE_DIR)
    try:
        suspects = {x["label_id"]: x for x in media_review.suspect_labels(conn, 300)}
    except Exception as exc:
        print(f"[Warning] suspect labels failed: {exc}")
        suspects = {}

    sql = ("SELECT id, root, path, profile, verdict, tags, decided_at FROM labels"
           " WHERE queue = 'training'")
    args = []
    if verdict not in ("all", "disagree"):
        sql += " AND verdict = ?"
        args.append(verdict)
    elif verdict == "disagree":
        sql += " AND verdict != 'unsure'"
    sql += " ORDER BY decided_at DESC"
    if verdict != "disagree":
        sql += " LIMIT ?"
        args.append(limit)
    rows = conn.execute(sql, args).fetchall()

    # What the tagger would say about each, judged on the snapshot taken when
    # the answer was given but with today's rules.
    try:
        shapes = media_review.profile_shapes(conn)
    except Exception:
        shapes = {}
    opinions = {}
    for r in rows:
        try:
            snapshot = json.loads(r["tags"] or "{}")
        except (ValueError, TypeError):
            snapshot = {}
        who = media_review.profile_of(r["path"])
        kind = (shapes.get(who) or {}).get("kind") if who else None
        opinions[r["id"]] = media_review.judge(r["path"], snapshot, kind)

    if verdict == "disagree":
        # A disagreement in either direction: something the rules missed, or
        # something they flagged that the user kept.
        rows = [r for r in rows
                if bool(opinions.get(r["id"]))
                != (r["verdict"] == media_review.VERDICT_NOT)][:limit]

    allowed = {r["id"] for r in roots_for_user(user)}
    wanted = [(r["root"], r["path"], False) for r in rows if r["root"] in allowed]
    entries = {(e["root"], e["path"]): e for e in build_entries_for(wanted)}

    # The account strip again - the same reference the answer was given
    # against, so a re-check is made on the same evidence.
    strips = {}
    for r in rows:
        who = media_review.profile_of(r["path"])
        if not who or who in strips:
            continue
        try:
            near = conn.execute(
                "SELECT root, path FROM media WHERE path LIKE ? AND trashed IS NULL"
                " ORDER BY RANDOM() LIMIT 10", (f"{who[0]}/{who[1]}/%",)).fetchall()
        except Exception:
            near = []
        picked = [(n["root"], n["path"], False) for n in near
                  if n["path"] != r["path"] and n["root"] in allowed][:4]
        strips[who] = [{"root": e["root"], "path": e["path"], "mtime": e["mtime"]}
                       for e in build_entries_for(picked)]

    items = []
    for r in rows:
        entry = entries.get((r["root"], r["path"]))
        if not entry:
            continue
        who = media_review.profile_of(r["path"])
        sus = suspects.get(r["id"])
        rule = opinions.get(r["id"])
        if rule and r["verdict"] != media_review.VERDICT_NOT:
            disagreement = "the tagger flagged this (%s) and you kept it" % rule[0]
        elif not rule and r["verdict"] == media_review.VERDICT_NOT:
            disagreement = "the tagger saw nothing wrong with this"
        else:
            disagreement = None
        items.append({**entry, "label_id": r["id"], "verdict": r["verdict"],
                      "profile": r["profile"], "siblings": strips.get(who, []),
                      "suspect": sus["why"] if sus else None,
                      "suspect_weight": sus["weight"] if sus else 0,
                      "rule_said": rule[0] if rule else None,
                      "decided_at": r["decided_at"],
                      "disagreement": disagreement})
    if verdict == "disagree":
        # The rules' false positives first: they are far rarer than the misses
        # and each one is a rule to reconsider, where a miss is just another
        # example for the pile. Suspicion weight is irrelevant here - it grades
        # answers against each other, not against the rules.
        items.sort(key=lambda i: (0 if i.get("rule_said") else 1,
                                  -i.get("decided_at", 0)))
    else:
        # Suspicious first, then most recent - a slip is likeliest in what was
        # just done, and likelier still in what the checks picked out.
        items.sort(key=lambda i: -i["suspect_weight"])

    counts = {r["verdict"]: r["n"] for r in conn.execute(
        "SELECT verdict, COUNT(*) AS n FROM labels WHERE queue='training'"
        " GROUP BY verdict")}

    # How many disagreements exist in total, counted over every label rather
    # than this page, so the header can say it without a second request.
    missed = kept = 0
    try:
        for r in conn.execute("SELECT id, path, verdict, tags FROM labels"
                              " WHERE queue='training' AND verdict != 'unsure'"):
            try:
                snap = json.loads(r["tags"] or "{}")
            except (ValueError, TypeError):
                snap = {}
            who = media_review.profile_of(r["path"])
            kind = (shapes.get(who) or {}).get("kind") if who else None
            flagged = bool(media_review.judge(r["path"], snap, kind))
            if r["verdict"] == media_review.VERDICT_NOT and not flagged:
                missed += 1
            elif r["verdict"] != media_review.VERDICT_NOT and flagged:
                kept += 1
    except Exception as exc:
        print(f"[Warning] disagreement count failed: {exc}")

    return jsonify({"items": items, "counts": counts, "verdict": verdict,
                    "suspect_total": len(suspects),
                    "missed": missed, "kept_over_rule": kept})


@app.route("/api/media/labels/<int:label_id>", methods=["POST"])
@require_admin
def media_label_change(label_id):
    """Change an answer, or withdraw it entirely."""
    body = request.get_json(silent=True) or {}
    conn = media_ai.connect(BASE_DIR)
    if body.get("delete"):
        # Withdrawn rather than corrected: the file goes back into the pool to
        # be asked about again, which is the right outcome for a misclick on
        # something the user could not actually judge.
        conn.execute("DELETE FROM labels WHERE id = ?", (label_id,))
        conn.commit()
        return jsonify({"ok": True, "deleted": label_id})

    verdict = (body.get("verdict") or "").strip().lower()
    if verdict not in (media_review.VERDICT_BELONGS, media_review.VERDICT_NOT,
                       media_review.VERDICT_UNSURE):
        return jsonify({"error": "unknown verdict"}), 400
    conn.execute("UPDATE labels SET verdict = ?, decided_at = ? WHERE id = ?",
                 (verdict, time.time(), label_id))
    conn.commit()
    return jsonify({"ok": True, "id": label_id, "verdict": verdict,
                    "stats": media_review.label_stats(conn)})


@app.route("/api/media/labels/stats", methods=["GET"])
@require_admin
def media_label_stats():
    """How the rules are actually doing, measured against the blind labels."""
    return jsonify(media_review.label_stats(media_ai.connect(BASE_DIR)))


@app.route("/api/media/labels/export", methods=["GET"])
@require_admin
def media_label_export():
    """The whole label set as JSON lines - a training corpus you can take away.

    Each line is self-contained: path, profile, the tag vector as it stood
    when the judgement was made, what the rules said at the time, and the
    verdict. Nothing here needs the database it came from.
    """
    conn = media_ai.connect(BASE_DIR)
    def stream():
        for r in conn.execute(
                "SELECT queue, root, path, profile, profile_kind, reason, score,"
                " flagged, verdict, acted, tags, decided_at, decided_by"
                " FROM labels ORDER BY decided_at"):
            row = dict(r)
            row["tags"] = json.loads(row["tags"] or "{}")
            yield json.dumps(row, ensure_ascii=False) + "\n"
    return Response(stream(), mimetype="application/x-ndjson", headers={
        "Content-Disposition": 'attachment; filename="media-labels.jsonl"'})


@app.route("/api/media/indexing", methods=["GET"])
def media_indexing_status():
    """How far the vision passes have got, and what the worker is doing.

    Cached for a few seconds: the counts are aggregates over a table with a
    row per library file, and the page that shows this polls every second.
    """
    if not get_authenticated_user():
        return jsonify({"error": "Unauthorized"}), 401
    now = time.time()
    if _indexing_cache["data"] and now - _indexing_cache["at"] < INDEXING_CACHE_S:
        return jsonify(_indexing_cache["data"])

    try:
        conn = media_ai.connect(BASE_DIR)
        cov = media_ai.coverage(conn)
        faces_files = conn.execute(
            "SELECT COUNT(DISTINCT media_id) AS n FROM faces").fetchone()["n"] or 0
    except Exception as exc:
        print(f"[Warning] indexing status: {exc}")
        cov, faces_files = {}, 0

    # What the indexer is currently doing, read from its own config rather
    # than guessed - it is the thing that decides.
    task, running = None, False
    try:
        with open(os.path.join(BASE_DIR, "indexer_config.json"), encoding="utf-8") as fh:
            task = (json.load(fh) or {}).get("tasks")
    except (OSError, ValueError):
        pass
    # NOT `systemctl --user`: this process is a SYSTEM unit, which has no
    # XDG_RUNTIME_DIR or DBUS_SESSION_BUS_ADDRESS and therefore cannot see a
    # user unit at all - it answered "stopped" while the indexer was visibly
    # working. The indexer reports itself instead.
    beat = {}
    try:
        with open(os.path.join(media_ai.store_dir(BASE_DIR),
                               "indexer_status.json"), encoding="utf-8") as fh:
            beat = json.load(fh) or {}
    except (OSError, ValueError):
        beat = {}
    # Stale means stopped: a heartbeat older than this is a crashed or killed
    # sweep, and reporting it as running would be worse than saying nothing.
    # Alive if it beat recently, OR it announced how long it would be quiet:
    # an idle indexer sleeps ten minutes between looks for new files, and
    # reading that silence as "stopped" is what the panel used to do.
    now_s = time.time()
    alive = bool(beat) and (now_s - beat.get("at", 0) < 180
                            or now_s < (beat.get("until") or 0) + 180)
    left = max(0, (beat.get("outstanding") or 0) - (beat.get("done") or 0)
               - (beat.get("failed") or 0))
    if not alive:
        state = "stopped"
    elif beat.get("state") == "waiting-for-window":
        state = "waiting"
    elif beat.get("state") in ("idle", "sweep-finished") and not left:
        state = "idle"
    else:
        state = "indexing"
    running = state == "indexing"
    if beat.get("task"):
        task = beat["task"]

    worker = {"reachable": False}
    try:
        cfg_path = os.path.join(BASE_DIR, "indexer_config.json")
        with open(cfg_path, encoding="utf-8") as fh:
            base = (json.load(fh) or {}).get("worker") or DEFAULT_WORKER_URL
        with urllib.request.urlopen(base.rstrip("/") + "/v1/status", timeout=3) as r:
            worker = {"reachable": True, **json.load(r)}
    except Exception:
        pass

    # Which pass the "time left" applies to - the one actually running.
    done_key = {"caption": "captioned", "caption,ocr": "captioned",
                "ocr": "ocr", "tags": "tagged", "faces": "faces_files"}.get(
                    (task or "").strip())
    data = {
        "library_files": library_file_count(get_media_index()),
        "faces_files": faces_files,
        "current_task": task,
        "running": running,
        "state": state,
        # Work still queued in the sweep that is running - the only honest
        # basis for "time left". The gap between the file count and the done
        # count is not work: most of it is music, tombstones and trash.
        "left": left if state == "indexing" else 0,
        "sweep": beat,
        "worker": worker,
        **cov,
    }
    data["current_done"] = data.get(done_key, 0) if done_key else 0
    # What the bars are measured against: every file a pass can process. The
    # store counts the files it has seen; the running sweep may also know new
    # ones it has not stored yet. Its outstanding count, though, is mostly
    # STORED files still waiting for this pass, so adding it to the store's
    # count counted those twice - 859,530 shown for ~485k files during the face
    # pass (2026-09-14). The pass's own done stamps plus what it has left is
    # the whole library as that pass sees it; the larger of the two wins.
    stamp = {"caption": "caption_done", "caption,ocr": "caption_done", "ocr": "ocr_done",
             "tags": "tags_done", "faces": "faces_done"}.get((task or "").strip())
    pass_total = (data.get(stamp) or 0) + data["left"] if stamp else 0
    data["indexable"] = max(data.get("indexable") or 0, pass_total)
    _indexing_cache.update({"at": now, "data": data})
    return jsonify(data)


# Search by image: private query jobs; uploads are never saved as library media.
_image_search_instance = None
_image_search_lock = threading.Lock()


def _image_search_analyze(blob):
    base, token = vision_worker_address()
    boundary = secrets.token_hex(16).encode()
    body = (b"--" + boundary + b'\r\nContent-Disposition: form-data; name="file"; filename="reference.jpg"\r\n'
            b'Content-Type: image/jpeg\r\n\r\n' + blob + b"\r\n")
    for name, value in (("tasks", "caption,tags,faces"), ("urgent", "1")):
        body += (b"--" + boundary + b'\r\nContent-Disposition: form-data; name="' + name.encode()
                 + b'"\r\n\r\n' + value.encode() + b"\r\n")
    body += b"--" + boundary + b"--\r\n"
    req = urllib.request.Request(base.rstrip("/") + "/v1/analyze", data=body, method="POST")
    req.add_header("Content-Type", "multipart/form-data; boundary=" + boundary.decode())
    if token:
        req.add_header("Authorization", "Bearer " + token)
    with urllib.request.urlopen(req, timeout=180) as response:
        data = json.loads(response.read())
    if data.get("failed") or not data.get("results"):
        raise RuntimeError("The vision worker could not analyse this reference.")
    return data["results"][0]


def image_search_service():
    global _image_search_instance
    with _image_search_lock:
        if _image_search_instance is None:
            _image_search_instance = image_search.ImageSearch(
                media_ai.store_dir(BASE_DIR), lambda: media_ai.connect(BASE_DIR),
                get_media_index, resolve_media_path, _image_search_analyze)
        return _image_search_instance


@app.route("/api/media/image-search", methods=["POST"])
def start_image_search():
    user = get_authenticated_user()
    if not user:
        return jsonify({"error": "Unauthorized"}), 401
    if request.content_length and request.content_length > image_search.MAX_UPLOAD + 1024 * 1024:
        return jsonify({"error": "Choose an image up to 8 MB."}), 413
    source = None
    if request.is_json:
        data = request.get_json(silent=True) or {}
        if not isinstance(data, dict):
            return jsonify({"error": "Invalid reference image request."}), 400
        root = str(data.get("root") or DEFAULT_ROOT_ID).strip()
        path = str(data.get("path") or "").strip("/").replace("\\", "/")
        if not root_visible_to(user, root):
            return jsonify({"error": "Forbidden"}), 403
        if (root, path) not in listed_media_paths():
            return jsonify({"error": "Not found"}), 404
        full = resolve_media_path(root, path)
        if not full or not os.path.isfile(full) or classify_media_path(full) != "image":
            return jsonify({"error": "Choose a picture from the library."}), 400
        if os.path.getsize(full) > image_search.MAX_UPLOAD:
            # Existing high-resolution pictures may exceed the upload limit; use their normal thumbnail.
            thumbnail = get_file_thumbnail(full)
            if not thumbnail:
                return jsonify({"error": "Could not read this picture."}), 400
            blob = thumbnail[1]
        else:
            with open(full, "rb") as fh:
                blob = fh.read(image_search.MAX_UPLOAD + 1)
        source = (root, path)
    else:
        upload = request.files.get("image")
        if upload is None:
            return jsonify({"error": "Choose a reference image."}), 400
        blob = upload.read(image_search.MAX_UPLOAD + 1)
    try:
        job = image_search_service().start(user["username"], {r["id"] for r in roots_for_user(user)}, blob, source)
    except (ValueError, OSError, image_search.Image.DecompressionBombError, image_search.Image.DecompressionBombWarning):
        return jsonify({"error": "Choose a valid image up to 8 MB and 40 megapixels."}), 400
    except RuntimeError as exc:
        return jsonify({"error": str(exc)}), 429
    return jsonify({"id": job, "state": "working"}), 202


@app.route("/api/media/image-search/<job_id>", methods=["GET"])
def image_search_results(job_id):
    user = get_authenticated_user()
    if not user:
        return jsonify({"error": "Unauthorized"}), 401
    allowed = {r["id"] for r in roots_for_user(user)}
    job = image_search_service().get(job_id, user["username"], allowed)
    if job is None:
        return jsonify({"error": "This image search expired. Choose the reference again."}), 404
    allowed = {r["id"] for r in roots_for_user(user)}
    if job["source"] and job["source"][0] not in allowed:
        return jsonify({"error": "Not found"}), 404
    try:
        offset = max(0, int(request.args.get("offset") or 0))
        limit = min(100, max(1, int(request.args.get("limit") or 40)))
    except ValueError:
        return jsonify({"error": "Invalid page."}), 400
    category = request.args.get("match") or "all"
    if category not in ("all", "visual", "description", "characters", "faces"):
        return jsonify({"error": "Invalid match type."}), 400
    live = listed_media_paths()
    matches = [(pair, signals) for pair, signals in job.pop("scores").items() if pair[0] in allowed and pair in live]
    counts = {kind: sum(kind in signals for pair, signals in matches) for kind in ("visual", "description", "characters", "faces")}
    if category != "all":
        matches = [(pair, signals) for pair, signals in matches if category in signals]
    matches.sort(key=lambda row: (-(row[1].get(category, 0) if category != "all" else max(row[1].values()) + .04 * (len(row[1])-1)), row[0]))
    page = matches[offset:offset + limit]
    scores = dict(page)
    entries = attach_favorites(attach_media_dimensions(build_entries_for([pair for pair, signals in page])), user)
    for entry in entries:
        signals = scores.get((entry["root"], entry["path"]), {})
        entry["match_types"] = list(signals)
    job.pop("source", None)
    return jsonify({**job, "items": entries, "total": len(matches), "counts": counts,
                    "offset": offset, "has_more": offset + limit < len(matches)})


_discovery_instance = None
_discovery_lock = threading.Lock()

def discovery_service():
    global _discovery_instance
    with _discovery_lock:
        if _discovery_instance is None:
            _discovery_instance = discovery_catalogue.Catalogue(os.path.join(media_ai.store_dir(BASE_DIR), 'discovery_cache'))
        return _discovery_instance

def discovery_characters_build():
    conn = media_ai.connect(BASE_DIR)
    rows = media_ai.tag_pools(conn, limit=1000000)
    # Cover selection and file metadata belong in the background snapshot.
    content_levels_map()
    allowed = {r['id'] for r in get_all_roots()}
    rows = use_safe_covers(conn, rows, allowed)
    for row in rows:
        row['label'] = tag_label(row['tag'], tag_series(row['tag']))
        row['series'] = tag_series(row['tag'])
        row['_prepared'] = character_pool_item(row, allowed)
    return rows

def discovery_people_build():
    conn = media_ai.connect(BASE_DIR)
    roots = {}
    auto = media_faces.auto_names(conn, roots=roots)
    return {'auto': auto, 'real': media_faces.realness(conn), 'roots':{str(k):sorted(v) for k,v in roots.items()}}

def discovery_live_cover(conn, cover):
    if not cover: return None
    return cover if conn.execute('SELECT 1 FROM media location CROSS JOIN media live INDEXED BY media_trashed ON live.id=location.id WHERE location.root=? AND location.path=? AND live.trashed IS NULL', (cover['root'], cover['path'])).fetchone() else None

@app.route('/api/media/discovery/<kind>', methods=['GET'])
def media_discovery(kind):
    user = get_authenticated_user()
    if not user: return jsonify({'error':'Unauthorized'}), 401
    if kind not in ('characters','people'): return jsonify({'error':'Not found'}), 404
    try:
        offset = max(0, int(request.args.get('offset') or 0))
        limit = min(200, max(1, int(request.args.get('limit') or 40)))
        min_size = max(1, int(request.args.get('min_size') or media_faces.MIN_CLUSTER_SIZE))
    except ValueError: return jsonify({'error':'Invalid page'}), 400
    query = (request.args.get('q') or '').strip()[:300]
    allowed = {r['id'] for r in roots_for_user(user)}
    service = discovery_service()
    rows = service.get(kind, discovery_characters_build if kind == 'characters' else discovery_people_build)
    if rows is None:
        return jsonify({'items':[], 'total':0, 'offset':offset, 'next_offset':offset, 'has_more':True, 'loading':True, **service.status(kind)})
    conn = media_ai.connect(BASE_DIR)
    items = []
    if kind == 'characters':
        # A character being blacklisted is left out at once, before the sweep
        # has trashed her files and the snapshot has been rebuilt.
        rows = charbl_filter_pools(rows, *charbl_hidden(conn))
        rows = [r for r in rows if discovery_catalogue.character_matches(r, query)]
        info = discovery_catalogue.page_info(rows, offset, limit)
        page = copy.deepcopy(rows[offset:offset+limit])
        for row in page:
            prepared = row.get('_prepared')
            covers = ([prepared.get('cover')] + [v.get('cover') for v in prepared.get('variants',[])]) if prepared else []
            if prepared and all(not cover or cover['root'] in allowed for cover in covers):
                item = prepared
            else:
                row = use_safe_covers(conn, [row], allowed)[0]
                item = character_pool_item(row, allowed)
            item['cover'] = discovery_live_cover(conn, item['cover'])
            for variant in item['variants']:
                variant['cover'] = discovery_live_cover(conn, variant['cover'])
            items.append(item)
    else:
        auto, real = rows['auto'], rows['real']
        show = request.args.get('show') or 'real'
        if show not in ('all','real','hidden'): show = 'real'
        records = [dict(r) for r in conn.execute('SELECT id,name,face_count,COALESCE(hidden,0) AS hidden FROM clusters WHERE merged_into IS NULL AND face_count>=? ORDER BY face_count DESC,id', (min_size,))]
        hidden_total = sum(bool(r['hidden']) for r in conn.execute('SELECT COALESCE(hidden,0) AS hidden FROM clusters WHERE merged_into IS NULL'))
        named = request.args.get('named') in ('1','true')
        records = [r for r in records if allowed.intersection(rows['roots'].get(str(r['id']),[]))
                   and bool(r['hidden']) == (show == 'hidden')
                   and (not named or r['name'] is not None)
                   and (show != 'real' or real.get(str(r['id']),real.get(r['id'],{})).get('real') is not False)
                   and discovery_catalogue.people_matches(r,auto.get(str(r['id']),auto.get(r['id'],{})),query)]
        info = discovery_catalogue.page_info(records, offset, limit)
        levels = content_levels_map(wait=False)
        for record in records[offset:offset+limit]:
            cid = record['id']
            row = conn.execute('SELECT c.id,c.name,c.cover_face,f.bbox,m.root,m.path, (SELECT COUNT(DISTINCT f2.media_id) FROM faces f2 JOIN media m2 INDEXED BY media_trashed ON m2.id=f2.media_id WHERE f2.cluster_id=c.id AND m2.trashed IS NULL) AS file_count FROM clusters c LEFT JOIN faces f ON f.id=c.cover_face LEFT JOIN media m ON m.id=f.media_id WHERE c.id=?', (cid,)).fetchone()
            if not row or not row['file_count']: continue
            cover = {'root':row['root'],'path':row['path'],'bbox':row['bbox'],'face_id':row['cover_face']} if row['root'] in allowed and row['path'] else None
            cover = discovery_live_cover(conn, cover)
            face_id = cover['face_id'] if cover else None
            if levels and not (cover and cover_rank(levels,cover['root'],cover['path']) == 0):
                pick = safe_person_cover(conn,cid,levels,allowed)
                if pick:
                    _,face_id,bbox,root,path = pick
                    cover = {'root':root,'path':path,'bbox':bbox,'face_id':face_id}
            guess = auto.get(str(cid),auto.get(cid,{}));judged = real.get(str(cid),real.get(cid,{}))
            items.append({'id':cid,'name':row['name'],'count':row['file_count'],'cover':cover,'face_id':face_id,
                          'auto_name':guess.get('name'),'auto_share':guess.get('share'),'auto_accounts':guess.get('accounts',[]),
                          'real':judged.get('real'),'judged':judged.get('judged',0)})
        info['hidden_total'] = hidden_total
    return jsonify({'items':items,'loading':False,**info,**service.status(kind)})


@app.route("/api/media/people", methods=["GET"])
def media_people():
    """The face clusters, biggest first - the Immich-shaped grid.

    Clusters below MIN_CLUSTER_SIZE are hidden by default: a library this size
    produces thousands of one-off strangers in the background of a photo, and
    a grid of those is worse than no grid.
    """
    if not get_authenticated_user():
        return jsonify({"error": "Unauthorized"}), 401
    try:
        limit = min(1000, max(1, int(request.args.get("limit", 200))))
    except ValueError:
        limit = 200
    try:
        min_size = max(1, int(request.args.get("min_size",
                                               media_faces.MIN_CLUSTER_SIZE)))
    except ValueError:
        min_size = media_faces.MIN_CLUSTER_SIZE
    named_only = request.args.get("named") in ("1", "true")
    # real | all | hidden. Default `real`, which also means "and everything
    # not yet judged" - see media_faces.realness.
    show = (request.args.get("show") or "real").strip().lower()
    if show not in ("real", "all", "hidden"):
        show = "real"

    try:
        conn = media_ai.connect(BASE_DIR)
        # file_count, not face_count. A video contributes one face per
        # sampled frame, so a person in a single clip can carry four faces -
        # and the tile said "4 photos" while opening it showed one. What the
        # tile promises has to be what the click delivers, so both are now
        # the number of distinct FILES, counted live rather than read from
        # the stored aggregate (which a merge or a re-sweep can outdate).
        sql = ("SELECT c.id, c.name, c.cover_face,"
               " (SELECT COUNT(DISTINCT f2.media_id) FROM faces f2"
               "  JOIN media m2 ON m2.id = f2.media_id"
               "  WHERE f2.cluster_id = c.id AND m2.trashed IS NULL) AS file_count,"
               " f.media_id, f.bbox, m.root, m.path"
               " FROM clusters c"
               " LEFT JOIN faces f ON f.id = c.cover_face"
               " LEFT JOIN media m ON m.id = f.media_id"
               " WHERE c.merged_into IS NULL AND c.face_count >= ?")
        args = [min_size]
        # Hidden is a decision, so it is a filter in SQL rather than something
        # sifted out afterwards - the limit must apply to what is shown.
        sql += " AND COALESCE(c.hidden, 0) = ?"
        args.append(1 if show == "hidden" else 0)
        if named_only:
            sql += " AND c.name IS NOT NULL"
        # Ordered by the stored aggregate because ordering by the live
        # subquery would compute it for every cluster in the table; the two
        # only disagree on how videos are weighted, which does not reshuffle
        # a grid sorted by size.
        sql += " ORDER BY c.face_count DESC LIMIT ?"
        args.append(limit)
        rows = conn.execute(sql, args).fetchall()
    except Exception as exc:
        print(f"[Warning] people unavailable: {exc}")
        rows = []

    # A face that is some twitter account's main face arrives named after every
    # such account (media_faces.account_main_faces). Computed, not stored: a
    # stored name could not be told apart from one the user typed, and this
    # one has to keep up as the sweep finds more files.
    try:
        auto = media_faces.auto_names(conn)
    except Exception as exc:
        print(f"[Warning] auto names unavailable: {exc}")
        auto = {}
    try:
        real = media_faces.realness(conn)
    except Exception as exc:
        print(f"[Warning] realness unavailable: {exc}")
        real = {}

    allowed = {r["id"] for r in roots_for_user(get_authenticated_user())}
    levels = content_levels_map(wait=False)
    items = []
    for r in rows:
        cover = None
        face_id = r["cover_face"]
        if r["root"] in allowed and r["path"]:
            cover = {"root": r["root"], "path": r["path"], "bbox": r["bbox"],
                     "face_id": r["cover_face"]}
        # A cluster whose every file is in the trash is not a person any
        # more; it would otherwise sit in the grid reading "0 photos".
        if not r["file_count"]:
            continue
        judged = real.get(r["id"]) or {}
        # False means judged and drawn. None means not judged yet, and is
        # shown - the face pass is thousands of files ahead of the tag pass.
        if show == "real" and judged.get("real") is False:
            continue
        # A Safe cover where the person has one (see cover_rank). The stored
        # cover is kept when it already is; most are, so most tiles cost no
        # query at all.
        if levels and not (cover and cover_rank(levels, r["root"], r["path"]) == 0):
            try:
                pick = safe_person_cover(conn, r["id"], levels, allowed)
            except Exception as exc:
                print(f"[Warning] safe cover for person {r['id']}: {exc}")
                pick = None
            if pick:
                _rank, face_id, bbox, root, path = pick
                cover = {"root": root, "path": path, "bbox": bbox, "face_id": face_id}
        guess = auto.get(r["id"])
        items.append({"id": r["id"], "name": r["name"],
                      "count": r["file_count"], "cover": cover,
                      "face_id": face_id,
                      # A name the user typed always wins; the guess rides
                      # alongside so the page can show it as a guess.
                      "auto_name": guess["name"] if guess else None,
                      "auto_share": guess["share"] if guess else None,
                      "auto_accounts": guess["accounts"] if guess else [],
                      "real": judged.get("real"),
                      "judged": judged.get("judged", 0)})
    hidden_total = conn.execute(
        "SELECT COUNT(*) AS n FROM clusters"
        " WHERE merged_into IS NULL AND COALESCE(hidden, 0) = 1").fetchone()["n"]
    return jsonify({"items": items, "min_size": min_size, "show": show,
                    "hidden_total": hidden_total})


@app.route("/api/media/people/<int:cluster_id>", methods=["POST"])
@require_admin
def media_person_update(cluster_id):
    """Name a person, or merge one into another.

    Merge rewrites the losing cluster's faces rather than deleting them, and
    leaves a `merged_into` marker so a re-cluster can tell that these two were
    judged the same person - which is information the embeddings alone did not
    have.
    """
    body = request.get_json(silent=True) or {}
    conn = media_ai.connect(BASE_DIR)

    if "name" in body:
        name = (body.get("name") or "").strip()[:120] or None
        conn.execute("UPDATE clusters SET name=?, updated_at=? WHERE id=?",
                     (name, time.time(), cluster_id))
        conn.commit()
        return jsonify({"ok": True, "id": cluster_id, "name": name})

    if "hidden" in body:
        conn.execute("UPDATE clusters SET hidden=?, updated_at=? WHERE id=?",
                     (1 if body["hidden"] else 0, time.time(), cluster_id))
        conn.commit()
        return jsonify({"ok": True, "id": cluster_id,
                        "hidden": bool(body["hidden"])})

    if "merge_into" in body:
        target = int(body["merge_into"])
        if target == cluster_id:
            return jsonify({"error": "cannot merge a person into themselves"}), 400
        row = conn.execute("SELECT id FROM clusters WHERE id=?", (target,)).fetchone()
        if row is None:
            return jsonify({"error": "no such person"}), 404
        conn.execute("UPDATE faces SET cluster_id=? WHERE cluster_id=?",
                     (target, cluster_id))
        # The surviving cluster keeps whichever name exists - a named cluster
        # merged into an unnamed one would otherwise lose the name.
        src = conn.execute("SELECT name FROM clusters WHERE id=?",
                           (cluster_id,)).fetchone()
        dst = conn.execute("SELECT name FROM clusters WHERE id=?",
                           (target,)).fetchone()
        if src and src["name"] and not (dst and dst["name"]):
            conn.execute("UPDATE clusters SET name=? WHERE id=?",
                         (src["name"], target))
        conn.execute(
            "UPDATE clusters SET face_count = (SELECT COUNT(*) FROM faces"
            " WHERE cluster_id=?), updated_at=? WHERE id=?",
            (target, time.time(), target))
        conn.execute("UPDATE clusters SET merged_into=?, face_count=0,"
                     " updated_at=? WHERE id=?",
                     (target, time.time(), cluster_id))
        conn.commit()
        discovery_service().invalidate()
        return jsonify({"ok": True, "merged": cluster_id, "into": target})

    return jsonify({"error": "send a name, a hidden, or a merge_into"}), 400


@app.route("/api/media/people/<int:cluster_id>/files", methods=["GET"])
def media_person_files(cluster_id):
    """Everything this person appears in."""
    user = get_authenticated_user()
    if not user:
        return jsonify({"error": "Unauthorized"}), 401
    allowed = {r["id"] for r in roots_for_user(user)}
    conn = media_ai.connect(BASE_DIR)
    rows = conn.execute(
        "SELECT DISTINCT m.root, m.path FROM faces f JOIN media m ON m.id = f.media_id"
        " WHERE f.cluster_id = ? AND m.trashed IS NULL LIMIT 2000",
        (cluster_id,)).fetchall()
    wanted = {(r["root"], r["path"]) for r in rows if r["root"] in allowed}
    entries = attach_favorites(build_entries_for([(r, p, False) for r, p in wanted]), user)
    return jsonify({"items": entries, "total": len(entries)})


@app.route("/api/media/people/quality", methods=["GET"])
def media_people_quality():
    """The purity check, exposed.

    A cluster drawn mostly from twitter should come overwhelmingly from ONE
    profile, because an account is one person posting pictures of themselves.
    Low purity means two people were merged - something Immich cannot know and
    we can, because we know where every file came from.
    """
    if not get_authenticated_user():
        return jsonify({"error": "Unauthorized"}), 401
    conn = media_ai.connect(BASE_DIR)
    ids, media_ids, paths, embs = media_faces.load_faces(conn)
    labels = [r["cluster_id"] if r["cluster_id"] is not None else -1
              for r in conn.execute(
                  "SELECT f.cluster_id FROM faces f JOIN media m ON m.id=f.media_id"
                  " WHERE f.embedding IS NOT NULL ORDER BY f.quality DESC")]
    return jsonify(media_faces.measure_purity(paths, labels))


# `jeanne_d\'arc_alter_(avenger)_(fate)` -> ["avenger", "fate"]. Nested
# brackets do not occur in the danbooru vocabulary, so a non-greedy group is
# enough and a parser would be ceremony.
TAG_QUALIFIER_RE = re.compile(r"\(([^()]*)\)")


# ---- Safe covers -------------------------------------------------------------
# The character and people circles show a Safe picture whenever the subject has
# one (user, 2026-09-18: "preview images for character and people circles
# should, if at all possible, always be Safe rated images"). They used to show
# the tagger's surest picture and the best face whatever the rating: 37 of the
# top 300 character covers and 5 of the top 200 people were Explicit, while all
# but one of them had a Safe picture to show.
#
# Best first: Safe, Suggestive, not rated yet, Explicit - and within one rank
# the old order (tag confidence, face quality). Chosen per request by walking a
# subject's pictures best-first (the media_tags_character_best / faces_cluster_quality
# indexes) and stopping at the first Safe one, which is nearly always among the
# first few. The STORED cover face is left alone: re-clustering carries names
# over by it (media_faces.persist).

def cover_rank(levels, root_id, path):
    level = levels.get((root_id, path))
    if level is None:
        return 2
    return 0 if level == 0 else 1 if level == 1 else 3


def safe_tag_cover(conn, tag, levels, allowed):
    """(rank, p, root, path) of the best cover for one tag, or None."""
    best = None
    for p, root, path in conn.execute(
            "SELECT t.p, m.root, m.path FROM media_tags t JOIN media m ON m.id = t.media_id"
            " WHERE t.category = 'character' AND t.tag = ? AND m.trashed IS NULL"
            " ORDER BY t.p DESC", (tag,)):
        if root not in allowed:
            continue
        rank = cover_rank(levels, root, path)
        if best is None or rank < best[0]:
            best = (rank, p or 0, root, path)
            if rank == 0:
                break
    return best


def use_safe_covers(conn, pools, allowed):
    """Gives every pool and variant its Safe cover (see above). Before the
    first content map exists - the first half minute after a restart - the
    covers stay as tag_pools chose them."""
    levels = content_levels_map(wait=False)
    if not levels:
        return pools
    for pool in pools:
        tags = [v["tag"] for v in pool["variants"]] or [pool["tag"]]
        try:
            picks = {t: safe_tag_cover(conn, t, levels, allowed) for t in tags}
        except Exception as exc:        # the old cover is still a cover
            print(f"[Warning] safe cover for {pool['tag']}: {exc}")
            continue
        for v in pool["variants"]:
            pick = picks.get(v["tag"])
            if pick:
                v["cover"] = (pick[2], pick[3])
        found = [p for p in picks.values() if p]
        if found:
            best = min(found, key=lambda p: (p[0], -p[1]))
            pool["cover"] = (best[2], best[3])
    return pools


def safe_person_cover(conn, cluster_id, levels, allowed):
    """(rank, face_id, bbox, root, path) of a person's best cover face, or None."""
    best = None
    for face_id, bbox, root, path in conn.execute(
            "SELECT f.id, f.bbox, m.root, m.path FROM faces f JOIN media m ON m.id = f.media_id"
            " WHERE f.cluster_id = ? AND m.trashed IS NULL ORDER BY f.quality DESC",
            (cluster_id,)):
        if root not in allowed:
            continue
        rank = cover_rank(levels, root, path)
        if best is None or rank < best[0]:
            best = (rank, face_id, bbox, root, path)
            if rank == 0:
                break
    return best


def character_pool_cover(cover_root, cover_path, allowed):
    """A cover from a root this caller cannot see is dropped rather than the
    whole pool: the count still tells them the character exists, which they
    could learn from any file's tags anyway."""
    if cover_root not in allowed or not cover_path:
        return None
    full = resolve_media_path(cover_root, cover_path)
    if not full or not os.path.isfile(full):
        return None
    try:
        mtime = int(os.stat(full).st_mtime)
    except OSError:
        mtime = 0
    return {"root": cover_root, "path": cover_path, "mtime": mtime}


def character_pool_item(pool, allowed):
    """One tile for the characters grid, or one pool for the variant strip."""
    series = tag_series(pool["tag"])
    return {
        "tag": pool["tag"], "count": pool["count"],
        # danbooru writes `houshou_marine_(1st_costume)`, which reads badly in
        # a grid; the series is pulled out to its own line.
        "label": tag_label(pool["tag"], series),
        "series": series,
        "cover": character_pool_cover(*pool["cover"], allowed),
        # The costume/class/ascension spellings of the same character. Empty
        # for the overwhelming majority of pools; where it is not, the client
        # offers them as a strip of circles above the results and can browse
        # the whole pool or any single variant.
        "variants": [{"tag": v["tag"], "count": v["count"],
                      "label": variant_label(v["tag"], pool["tag"], series),
                      "cover": character_pool_cover(*v["cover"], allowed)}
                     for v in pool["variants"]],
    }


def tag_series(tag):
    """The series a danbooru character tag ends in, or None.

    The LAST bracket group, not everything between the first and the last.
    Reading from the first open bracket to the last close one turned
    `jeanne_d\'arc_alter_(avenger)_(fate)` into the series "avenger) (fate",
    which the grid printed under her name and then could not strip off the
    label either. 96 tags in the library carry more than one group.

    Only a pool KEY should be passed here. A costume sits in the last
    position too (`hoshimachi_suisei_(1st_costume)`) and would be read as a
    series - but the key of a group is by construction its least qualified
    member, so a costume can never be in that position on a key.
    """
    if not tag.endswith(")"):
        return None
    groups = TAG_QUALIFIER_RE.findall(tag)
    return groups[-1].replace("_", " ") if groups else None


def tag_name(tag):
    """The bare character name, with every bracket group removed."""
    cut = tag.find("_(")
    return (tag if cut < 0 else tag[:cut]).replace("_", " ")


def tag_label(tag, series=None):
    """Name plus anything qualifying it other than the series shown beside it."""
    extra = tag_qualifiers(tag, series)
    return tag_name(tag) + (" (" + ", ".join(extra) + ")" if extra else "")


def tag_qualifiers(tag, series=None):
    """The bracket groups that are not the pool's series.

    The series is the pool's, not the tag's own: inside the
    artoria_pendragon_(fate) pool, `artoria_pendragon_(lancer)_(fate)` is
    distinguished by "lancer" alone, and repeating "fate" on every circle
    would say nothing.
    """
    groups = [g.replace("_", " ") for g in TAG_QUALIFIER_RE.findall(tag)]
    if series and groups and groups[-1] == series:
        groups.pop()
    return groups


def variant_label(tag, key, series=None):
    """What distinguishes one variant from the rest of its pool.

    "lancer" for `artoria_pendragon_(lancer)_(fate)`, "alter swimsuit rider,
    second ascension" where there are two qualifiers, and "Default" for the
    pool key itself, which has nothing left over once the name and series are
    removed - an empty caption under a circle would just look broken.
    """
    extra = tag_qualifiers(tag, series)
    if tag == key or not extra:
        return "Default"
    return ", ".join(extra)


@app.route("/api/media/characters", methods=["GET"])
def media_characters():
    """Characters found in the library, most-seen first.

    Doubles as the type-ahead source: `q` filters the fixed danbooru
    vocabulary, so what comes back is guaranteed to match something. A free
    text captioner could not offer this - you would be completing against
    spellings that may not exist.
    """
    if not get_authenticated_user():
        return jsonify({"error": "Unauthorized"}), 401
    try:
        limit = min(2000, max(1, int(request.args.get("limit", 300))))
    except ValueError:
        limit = 300
    query = (request.args.get("q") or "").strip().lower()
    category = (request.args.get("category") or "character").strip().lower()
    if category not in ("character", "general", "rating"):
        category = "character"
    allowed = {r["id"] for r in roots_for_user(get_authenticated_user())}
    try:
        conn = media_ai.connect(BASE_DIR)
        hidden_keys, hidden_tags = (charbl_hidden(conn) if category == "character"
                                    else (set(), set()))
        if hidden_keys or hidden_tags:
            # A character being blacklisted vanishes at once, before the sweep
            # has moved her files (2026-10-18): ask for extra pools to fill the
            # page, and the total loses the ones left out.
            wanted = min(2000, limit + len(hidden_keys | hidden_tags))
            rows = media_ai.tag_pools(conn, category=category, query=query or None,
                                      limit=wanted)
            total = media_ai.tag_pool_total(conn, category=category, query=query or None)
            kept = charbl_filter_pools(rows, hidden_keys, hidden_tags)
            if len(rows) < wanted:
                total -= len(rows) - len(kept)
            rows = kept[:limit]
        else:
            rows = media_ai.tag_pools(conn, category=category, query=query or None,
                                      limit=limit)
            total = media_ai.tag_pool_total(conn, category=category,
                                            query=query or None)
        if category == "character":
            rows = use_safe_covers(conn, rows, allowed)
    except Exception as exc:
        print(f"[Warning] character list unavailable: {exc}")
        rows, total = [], 0

    items = [character_pool_item(pool, allowed) for pool in rows]
    # `total` is the whole vocabulary, not this page of it. The client used to
    # count what it received, which meant its own limit looked like an answer.
    return jsonify({"category": category, "items": items, "total": total,
                    "limit": limit})


@app.route("/api/media/characters/pool", methods=["GET"])
def media_character_pool():
    """One character pool, by its key, with its variants.

    The grid hands the client everything it needs, so this is not on the
    normal path - it is what makes a character URL survive a reload and the
    Back button, when the grid it was clicked in was never built.
    """
    if not get_authenticated_user():
        return jsonify({"error": "Unauthorized"}), 401
    key = (request.args.get("tag") or "").strip()
    if not key:
        return jsonify({"error": "tag required"}), 400
    allowed = {r["id"] for r in roots_for_user(get_authenticated_user())}
    try:
        conn = media_ai.connect(BASE_DIR)
        rows = media_ai.tag_pools(conn, category="character", limit=1, keys=[key])
        rows = charbl_filter_pools(rows, *charbl_hidden(conn))
        rows = use_safe_covers(conn, rows, allowed)
    except Exception as exc:
        print(f"[Warning] character pool unavailable: {exc}")
        rows = []
    if not rows:
        return jsonify({"error": "not found"}), 404
    return jsonify(character_pool_item(rows[0], allowed))


# ---- Character blacklist -----------------------------------------------------
# User, 2026-10-18: "I need a blacklist of characters." One global list, managed
# by admins, because trashing removes the files for everybody. A listed
# character's media goes to the trash with a ban (trash_resolved, restorable for
# trash_purge_days), the files the tagger finds later are trashed the same way
# by a sweep every ten minutes, and the characters grid leaves the pool out
# straight away while a sweep is waiting or running. Removing an entry restores
# nothing; the trash does that. A batch restored from the trash exempts its files
# (restored files must not be trashed again by the next sweep) until an admin
# clears the exemption.
#
# Stored in character_blacklist.json next to server.py:
#   {"entries": [{"tag", "include_variants", "added_by", "added_at", "min_p"?}],
#    "exemptions": [{"root", "path", "at"}]}

CHARBL_FILE = os.path.join(BASE_DIR, "character_blacklist.json")
CHARBL_REASON = "character blacklist"          # the manifest `reason` starts with this
CHARBL_SWEEP_INTERVAL_S = 600
CHARBL_DEFAULT_MIN_P = 0.35
CHARBL_DEFAULT_MAX_PER_SWEEP = 5000
CHARBL_GROWTH_LIMIT = 1.10
_charbl_lock = threading.Lock()                # the file
_charbl_state_lock = threading.Lock()          # the sweep state
_charbl_state = {"pending": False, "running": False, "worker": False, "last": None}


def charbl_settings():
    """(min_p, max_per_sweep) from config.json's character_blacklist block.

    The default minimum confidence is the floor the tagger itself stores
    character tags at: the characters grid, its counts and the pool pages list
    every stored tag with no confidence limit of their own, so a character's
    count there is exactly the number of files over this floor (0.35 on the
    real index, measured 2026-10-18). A higher min_p, here or per entry, trashes
    fewer files and leaves the doubtful ones.
    """
    block = CONFIG.get("character_blacklist")
    block = block if isinstance(block, dict) else {}
    try:
        min_p = float(block.get("min_p", CHARBL_DEFAULT_MIN_P))
    except (TypeError, ValueError):
        min_p = CHARBL_DEFAULT_MIN_P
    try:
        cap = int(block.get("max_per_sweep", CHARBL_DEFAULT_MAX_PER_SWEEP))
    except (TypeError, ValueError):
        cap = CHARBL_DEFAULT_MAX_PER_SWEEP
    return min(max(min_p, 0.0), 1.0), max(1, cap)


def charbl_load():
    try:
        with open(CHARBL_FILE, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    entries = [e for e in data.get("entries") or [] if isinstance(e, dict) and e.get("tag")]
    exemptions = [x for x in data.get("exemptions") or []
                  if isinstance(x, dict) and x.get("root") and x.get("path")]
    return {"entries": entries, "exemptions": exemptions}


def charbl_update(change):
    """Applies change(data) under the lock and writes the file atomically.
    Returns whatever change returns."""
    with _charbl_lock:
        data = charbl_load()
        result = change(data)
        _atomic_json_write(CHARBL_FILE, data, indent=2)
        return result


def charbl_exempt(pairs):
    """Exempts (root, path) pairs from the sweep. Called by the trash restore."""
    pairs = list(dict.fromkeys(pairs))
    if not pairs:
        return 0
    now = time.time()

    def add(data):
        have = {(x["root"], x["path"]) for x in data["exemptions"]}
        fresh = [(r, p) for r, p in pairs if (r, p) not in have]
        data["exemptions"].extend({"root": r, "path": p, "at": now} for r, p in fresh)
        return len(fresh)
    return charbl_update(add)


def charbl_tags(conn, tag, include_variants):
    """The tags one entry covers: the tag itself, and with include_variants
    every tag of its pool (the grouping media_ai.tag_pools shows), key first."""
    if not include_variants:
        return [tag]
    variants = media_ai.variant_map(conn)
    key = variants.get(tag, tag)
    members = sorted(t for t, k in variants.items() if k == key and t != key)
    return list(dict.fromkeys([key, tag] + members))


def charbl_blocked_tags(conn, entries):
    """Every character tag the list covers, variants included."""
    blocked = set()
    for e in entries:
        blocked.update(charbl_tags(conn, e["tag"], bool(e.get("include_variants"))))
    return blocked


def charbl_rows(conn, entry, exempt, tags=None, blocked=None, shared_out=None):
    """[(root, path, p)] of the live files one entry matches, surest first:
    carrying a covered tag at p >= the entry's min_p (else the configured
    one), not trashed, not exempt, not under .trash.

    User, 2026-10-18: "Trash only if all characters in the image are blocked."
    A file that also shows a character NOT on the list (any character tag at
    p >= the configured min_p outside `blocked`) is kept; it stays in that other
    character's pool. `blocked` defaults to every tag the saved list covers
    plus this entry's; with `shared_out` (a list) the kept shared files are
    appended to it as (root, path) for the preview's count."""
    min_p = entry.get("min_p")
    if not isinstance(min_p, (int, float)) or isinstance(min_p, bool):
        min_p = charbl_settings()[0]
    tags = tags or charbl_tags(conn, entry["tag"], bool(entry.get("include_variants")))
    if blocked is None:
        blocked = charbl_blocked_tags(conn, charbl_load()["entries"])
    # A costume or variant tag of a blocked character is the same character,
    # not "another" one, even for an entry without include_variants.
    variants = media_ai.variant_map(conn)
    keys = {variants.get(t, t) for t in set(blocked) | set(tags)}
    blocked = sorted(set(blocked) | set(tags) | keys
                     | {t for t, k in variants.items() if k in keys})
    others = (" AND EXISTS (SELECT 1 FROM media_tags o WHERE o.media_id = m.id"
              " AND o.category = 'character' AND o.p >= ? AND o.tag NOT IN ("
              + ",".join("?" * len(blocked)) + "))")
    rows = conn.execute(
        "SELECT m.root AS root, m.path AS path, MAX(t.p) AS p,"
        " CASE WHEN 1" + others + " THEN 1 ELSE 0 END AS shared FROM media_tags t"
        " JOIN media m ON m.id = t.media_id"
        " WHERE t.category = 'character' AND t.tag IN (" + ",".join("?" * len(tags)) + ")"
        " AND t.p >= ? AND m.trashed IS NULL GROUP BY m.id ORDER BY p DESC",
        [float(charbl_settings()[0])] + blocked + list(tags) + [float(min_p)]).fetchall()
    out = []
    for r in rows:
        if (r["root"], r["path"]) in exempt or r["path"].split("/")[0] == TRASH_DIR_NAME:
            continue
        if r["shared"]:
            if shared_out is not None:
                shared_out.append((r["root"], r["path"]))
            continue
        out.append((r["root"], r["path"], r["p"]))
    return out


def charbl_exempt_set(data):
    return {(x["root"], x["path"]) for x in data["exemptions"]}


def charbl_status():
    with _charbl_state_lock:
        return {"pending": _charbl_state["pending"], "running": _charbl_state["running"],
                "last": dict(_charbl_state["last"]) if _charbl_state["last"] else None}


def charbl_sweep_once():
    """One sweep: trash everything the list matches, up to max_per_sweep, as ONE
    trash batch through trash_resolved in ban mode. Reads the AI index only."""
    started = time.time()
    cap = charbl_settings()[1]
    data = charbl_load()
    exempt = charbl_exempt_set(data)
    summary = {"at": started, "trashed": 0, "failed": 0, "matched": 0, "deferred": 0,
               "missing": 0, "batch": None, "reason": None, "error": None, "by_tag": {}}
    try:
        conn = media_ai.connect(BASE_DIR)
        resolved, seen, per_tag = [], set(), {}
        blocked = charbl_blocked_tags(conn, data["entries"])
        for entry in data["entries"]:
            for root, path, _p in charbl_rows(conn, entry, exempt, blocked=blocked):
                if (root, path) in seen:
                    continue
                seen.add((root, path))
                summary["matched"] += 1
                full = resolve_media_path(root, path)
                if not full or not os.path.exists(full):
                    summary["missing"] += 1          # in the index, gone from the disk
                    continue
                if len(resolved) >= cap:
                    summary["deferred"] += 1         # the next sweep
                    continue
                resolved.append((root, path, full))
                per_tag[(root, path)] = entry["tag"]
        if resolved:
            tags = list(dict.fromkeys(per_tag.values()))
            reason = f"{CHARBL_REASON}: " + ", ".join(tags[:5]) + (
                f" (+{len(tags) - 5} more)" if len(tags) > 5 else "")
            batch, deleted, _notes, failed = trash_resolved(
                resolved, DELETE_BAN, {"username": "auto: " + CHARBL_REASON}, reason=reason)
            summary.update(trashed=len(deleted), failed=len(failed), batch=batch, reason=reason)
            for item in deleted:
                tag = per_tag.get((item["root"], item["path"]))
                summary["by_tag"][tag] = summary["by_tag"].get(tag, 0) + 1
    except Exception as exc:
        summary["error"] = str(exc)
        print(f"[Warning] character blacklist sweep failed: {exc}")
    summary["duration_s"] = round(time.time() - started, 2)
    return summary


def _charbl_worker():
    """Runs sweeps until none is requested; a request that arrives during a sweep
    gets a sweep of its own afterwards, so an entry added mid-sweep is not missed."""
    while True:
        with _charbl_state_lock:
            if not _charbl_state["pending"]:
                _charbl_state["running"] = False
                _charbl_state["worker"] = False
                return
            _charbl_state["pending"] = False
            _charbl_state["running"] = True
        summary = charbl_sweep_once()
        with _charbl_state_lock:
            _charbl_state["last"] = summary


def charbl_request_sweep():
    """Asks for a sweep in the background and returns at once."""
    with _charbl_state_lock:
        _charbl_state["pending"] = True
        if _charbl_state["worker"]:
            return
        _charbl_state["worker"] = True
    threading.Thread(target=_charbl_worker, daemon=True, name="character-blacklist").start()


def _charbl_sweep_loop():
    """Every ten minutes, for the files the tagger has tagged since. Nothing
    runs while the list is empty."""
    time.sleep(90)
    while True:
        try:
            if charbl_load()["entries"]:
                charbl_request_sweep()
        except Exception as exc:
            print(f"[Warning] character blacklist loop: {exc}")
        time.sleep(CHARBL_SWEEP_INTERVAL_S)


def charbl_hidden(conn):
    """(pool keys, tags) the character grids always leave out. It used to apply
    only while a sweep ran (afterwards the files were gone), but since shared
    pictures are kept (2026-10-18) a blacklisted character still has files in
    the index and her pool would come back."""
    entries = charbl_load()["entries"]
    if not entries:
        return set(), set()
    keys, tags = set(), set()
    variants = media_ai.variant_map(conn)
    for entry in entries:
        covered = charbl_tags(conn, entry["tag"], bool(entry.get("include_variants")))
        tags.update(covered)
        if entry.get("include_variants"):
            keys.add(variants.get(entry["tag"], entry["tag"]))
    return keys, tags


def charbl_filter_pools(pools, keys, tags):
    out = []
    for pool in pools:
        if pool["tag"] in keys:
            continue
        kept = [v for v in pool["variants"] if v["tag"] not in tags]
        if pool["tag"] in tags and not kept:
            continue
        if len(kept) != len(pool["variants"]):
            pool = dict(pool, variants=kept if len(kept) > 1 else [])
            # The characters grid reads the discovery snapshot, whose rows carry
            # the finished tile as well.
            prepared = pool.get("_prepared")
            if prepared:
                left = [v for v in prepared["variants"] if v["tag"] not in tags]
                pool["_prepared"] = dict(prepared, variants=left if len(left) > 1 else [])
        out.append(pool)
    return out


def _charbl_entry_json(conn, entry, exempt, blocked):
    tags = charbl_tags(conn, entry["tag"], bool(entry.get("include_variants")))
    shared = []
    matches = len(charbl_rows(conn, entry, exempt, tags, blocked=blocked, shared_out=shared))
    return {"tag": entry["tag"], "include_variants": bool(entry.get("include_variants")),
            "added_by": entry.get("added_by"), "added_at": entry.get("added_at"),
            "min_p": entry.get("min_p"), "variants": tags,
            "matches": matches, "kept_shared": len(shared)}


def _charbl_body_entry(conn, body):
    """(entry, error) from a request body; the tag must be a character the index knows."""
    tag = str(body.get("tag") or "").strip()
    if not tag:
        return None, "tag required"
    if not conn.execute("SELECT 1 FROM media_tags WHERE category = 'character' AND tag = ? LIMIT 1",
                        (tag,)).fetchone():
        return None, f"unknown character tag: {tag}"
    entry = {"tag": tag, "include_variants": bool(body.get("include_variants", True))}
    if body.get("min_p") is not None:
        try:
            value = float(body["min_p"])
        except (TypeError, ValueError):
            return None, "min_p must be a number"
        if not 0 <= value <= 1:
            return None, "min_p must be between 0 and 1"
        entry["min_p"] = value
    return entry, None


@app.route("/api/media/characters/blacklist", methods=["GET"])
@require_admin
def character_blacklist_list():
    """The entries with a live match count each, the exemptions and the last
    sweep's status."""
    data = charbl_load()
    exempt = charbl_exempt_set(data)
    conn = media_ai.connect(BASE_DIR)
    min_p, cap = charbl_settings()
    blocked = charbl_blocked_tags(conn, data["entries"])
    return jsonify({
        "entries": [_charbl_entry_json(conn, e, exempt, blocked) for e in data["entries"]],
        "exemptions": data["exemptions"],
        "status": charbl_status(),
        "min_p": min_p, "max_per_sweep": cap,
    })


@app.route("/api/media/characters/blacklist/preview", methods=["POST"])
@require_admin
def character_blacklist_preview():
    """What adding {tag, include_variants, min_p?} would trash, without changing
    anything: the count, the variant tags covered and a few sample paths."""
    body = request.get_json(silent=True) or {}
    conn = media_ai.connect(BASE_DIR)
    entry, error = _charbl_body_entry(conn, body)
    if error:
        return jsonify({"error": error}), 400
    data = charbl_load()
    tags = charbl_tags(conn, entry["tag"], entry["include_variants"])
    shared = []
    rows = charbl_rows(conn, entry, charbl_exempt_set(data), tags,
                       blocked=charbl_blocked_tags(conn, data["entries"]), shared_out=shared)
    return jsonify({
        "tag": entry["tag"], "include_variants": entry["include_variants"],
        "min_p": entry.get("min_p", charbl_settings()[0]),
        "count": len(rows), "kept_shared": len(shared), "variants": tags,
        "samples": [{"root": r, "path": p, "p": q} for r, p, q in rows[:5]],
        "already_listed": any(e["tag"] == entry["tag"] for e in data["entries"]),
    })


@app.route("/api/media/characters/blacklist", methods=["POST"])
@require_admin
def character_blacklist_add():
    """Adds an entry and sweeps in the background. Needs confirm: true and the
    count the preview gave; 409 if more than 10 % more files match now."""
    body = request.get_json(silent=True) or {}
    if body.get("confirm") is not True:
        return jsonify({"error": "confirm: true is required"}), 400
    expected = body.get("expected_count")
    if isinstance(expected, bool) or not isinstance(expected, int) or expected < 0:
        return jsonify({"error": "expected_count (from the preview) is required"}), 400
    conn = media_ai.connect(BASE_DIR)
    entry, error = _charbl_body_entry(conn, body)
    if error:
        return jsonify({"error": error}), 400
    user = get_authenticated_user()
    data = charbl_load()
    if any(e["tag"] == entry["tag"] for e in data["entries"]):
        return jsonify({"error": "already on the list", "code": "exists"}), 409
    count = len(charbl_rows(conn, entry, charbl_exempt_set(data)))
    if count > expected * CHARBL_GROWTH_LIMIT:
        return jsonify({"error": f"{count} files match now, {expected} were previewed; preview again",
                        "code": "count_grew", "count": count, "expected_count": expected}), 409
    entry.update(added_by=user["username"], added_at=time.time())

    def add(stored):
        stored["entries"].append(entry)
    charbl_update(add)
    # Pending before the response goes out, so the very next grid request
    # already leaves the character out.
    charbl_request_sweep()
    return jsonify({"added": entry, "count": count, "status": charbl_status()})


@app.route("/api/media/characters/blacklist", methods=["DELETE"])
@require_admin
def character_blacklist_remove():
    """Removes an entry (?tag=). Restores nothing; the trash page does that."""
    tag = (request.args.get("tag") or "").strip()

    def drop(data):
        before = len(data["entries"])
        data["entries"] = [e for e in data["entries"] if e["tag"] != tag]
        return before - len(data["entries"])
    if not tag or not charbl_update(drop):
        return jsonify({"error": "not on the list"}), 404
    return jsonify({"removed": tag})


@app.route("/api/media/characters/blacklist/exemptions", methods=["DELETE"])
@require_admin
def character_blacklist_clear_exemption():
    """Clears one exemption (?root=&path=) or all of them (?all=1)."""
    root = (request.args.get("root") or "").strip()
    path = (request.args.get("path") or "").strip()
    everything = request.args.get("all") == "1"

    def drop(data):
        if everything:
            before, data["exemptions"] = len(data["exemptions"]), []
            return before
        before = len(data["exemptions"])
        data["exemptions"] = [x for x in data["exemptions"]
                              if not (x["root"] == root and x["path"] == path)]
        return before - len(data["exemptions"])
    cleared = charbl_update(drop) if (everything or (root and path)) else 0
    if not cleared:
        return jsonify({"error": "no such exemption"}), 404
    return jsonify({"cleared": cleared})


_fps_coverage = {"data": None, "value": (0, 0)}


def frame_rate_coverage(dims):
    """(videos with a frame rate, videos measured) - counted once per load of
    the dimensions store, which is ~475k entries."""
    if _fps_coverage["data"] is not dims:
        videos = [v for k, v in dims.items()
                  if k[k.rfind("."):].lower() in media_dimensions.VIDEO_EXTS]
        _fps_coverage["value"] = (sum(1 for v in videos if len(v) > 3), len(videos))
        _fps_coverage["data"] = dims
    return _fps_coverage["value"]


@app.route("/api/media/filters", methods=["GET"])
def media_filter_options():
    """What the filter panel offers, and how much of the library can answer.

    The coverage figure is not decoration: until the backfill has been over a
    file, that file cannot match any filter, and a panel that did not say so
    would look like it was hiding results at random.
    """
    if not get_authenticated_user():
        return jsonify({"error": "Unauthorized"}), 401
    return jsonify(cached_filter_options())


# The panel's options are the same for everybody, and the coverage figures in them
# cost seconds (3.4 s for the AI index's counts on 2026-10-13, 54 s while the
# library index was being rebuilt) - the panel stayed empty for all of that. So
# they are cached and refreshed in the background: a request only ever waits for
# the very first build.
FILTER_OPTIONS_MAX_AGE_S = 120
_filter_options = {"data": None, "at": 0.0, "building": False}
_filter_options_lock = threading.Lock()


def _rebuild_filter_options():
    try:
        data = build_filter_options()
        with _filter_options_lock:
            # Content counts are None until the first level map exists: retry soon.
            age_bonus = 0 if data.get("content_counts") is not None else FILTER_OPTIONS_MAX_AGE_S - 15
            _filter_options.update(data=data, at=time.time() - age_bonus)
    finally:
        with _filter_options_lock:
            _filter_options["building"] = False


def cached_filter_options():
    with _filter_options_lock:
        data, at = _filter_options["data"], _filter_options["at"]
        stale = data is None or time.time() - at > FILTER_OPTIONS_MAX_AGE_S
        start = stale and data is not None and not _filter_options["building"]
        if start:
            _filter_options["building"] = True
    if data is None:
        data = build_filter_options()
        with _filter_options_lock:
            if _filter_options["data"] is None:
                _filter_options.update(data=data, at=time.time())
        return data
    if start:
        threading.Thread(target=_rebuild_filter_options, daemon=True, name="filter-options").start()
    return data


def static_filter_options():
    """The panel's buttons: fixed lists, the same on every load. Delivered inside
    media.html (serve_static), so the panel is complete on first paint; only the
    coverage figures below come from /api/media/filters, whenever they are ready."""
    return {
        "standards": [
            {"key": k, "label": media_dimensions.STANDARD_LABELS[k],
             "long_edge": v, "short_edge": round(v * 9 / 16)}
            for k, v in media_dimensions.STANDARDS.items()
        ],
        "kinds": [
            {"key": "video", "label": "Videos",
             "hint": "moving pictures, GIFs included"},
            {"key": "image", "label": "Images",
             "hint": "still pictures, GIFs included"},
            {"key": "music", "label": "Music", "hint": "audio files"},
        ],
        "orientations": ["horizontal", "vertical", "square"],
        # Several at once.
        "contents": CONTENT_OPTIONS,
        # Same numbers, three edges: width, height, or either.
        "families": [
            {"key": "min_hres", "label": "Horizontal", "edge": "width"},
            {"key": "min_vres", "label": "Vertical", "edge": "height"},
            {"key": "min_res", "label": "Either", "edge": "longest side"},
        ],
        # A floor and a ceiling on the same principle, so together they make
        # a band. Only videos have a rate: pictures, GIFs and music never match.
        "frame_rates": {
            "min": [{"key": k, "fps": v} for k, v in media_dimensions.MIN_FRAME_RATES.items()],
            "max": [{"key": k, "fps": v} for k, v in media_dimensions.MAX_FRAME_RATES.items()],
        },
        "modes": list(SEARCH_MODES),
    }


def build_filter_options():
    """static_filter_options plus how much of the library can answer - the part
    that costs seconds."""
    dims = media_dimensions_map()
    fps_known, videos_known = frame_rate_coverage(dims)
    return {
        **static_filter_options(),
        # None until the first level map has been built, which this starts.
        "content_counts": content_counts(),
        "known": len(dims),
        # Videos measured before frame rates were stored have none until the
        # backfill has been over them again; said, so the filter is not
        # mistaken for one that loses videos at random.
        "fps_known": fps_known,
        "videos_known": videos_known,
        # What each search mode can currently answer over. A mode whose index
        # is empty is offered but labelled, rather than silently returning
        # nothing and looking broken.
        "ai": ai_coverage(),
        # The denominator for "how much is indexed". Taken from the path index
        # rather than the AI store, because the AI store only knows the files
        # it has already seen - measuring progress against that would read
        # 100% from the first batch onwards.
        "library_files": library_file_count(get_media_index()),
    }


# -------------------------------------------------------------------
# Live upscaling
# -------------------------------------------------------------------
# An image someone keeps looking at is upscaled on the vision worker
# (Real-ESRGAN, vision/upscaler.py) until a zoom of `zoom_factor` on their
# screen stays sharp, and the page swaps it in. One job at a time, newest
# first - the picture on screen now matters more than one flicked past a
# minute ago. Results are cached ON THE MEDIA DISK, not /data2: /data2 is a
# thin pool, and write-and-delete churn is exactly what filled it and froze
# this VM in August. Evicted `cache_days` after the last view.

UPSCALE_EXTS = {".jpg", ".jpeg", ".jfif", ".png", ".webp", ".bmp"}
# What Florence's caption says about the MEDIUM. Measured 2026-09-14 on 32,755
# captioned files against photos WD14 calls realistic and pixiv/danbooru
# drawings (vault KSF-019). "anime" is in 19% of photo captions ("a cosplay
# costume, likely inspired by the popular anime series") against 11% of
# drawings', and "painting" (one hanging on the wall) in 0.6% of each - neither
# names a medium. Florence calls drawings "animated" (72%, photos 0.5%) and
# photos of cosplayers "cosplay" (35%, drawings 0.2%). Checked in this order: a
# photo word, an artwork word, then a costume or a photo mentioned in passing,
# which drawings have as well ("an animated woman in a pirate costume").
UPSCALE_CAPTION_PHOTO = re.compile(r"\b(cosplay\w*|selfie|photograph\w*|real[- ]life|wigs?|photo of)\b", re.I)
UPSCALE_CAPTION_ART = re.compile(
    r"\b(animated|animation|illustration\w*|drawing|drawn|artwork|digital art|digital painting|cartoon\w*"
    r"|manga|comic|sketch|chibi|pixel art)\b", re.I)
UPSCALE_CAPTION_COSTUME = re.compile(r"\b(costumes?|photos?)\b", re.I)
UPSCALE_QUEUE_MAX = 8
_upscale_cv = threading.Condition()
_upscale_index = {"loaded": False, "entries": {}, "dirty": False, "dir": None}
_upscale_jobs = {}          # key -> {"status", "error", "at", "spec"}
_upscale_queue = []         # keys waiting, newest last


def upscale_settings():
    merged = json.loads(json.dumps(DEFAULT_CONFIG["live_upscale"]))
    merged.update(CONFIG.get("live_upscale") or {})
    return merged


def upscale_cache_dir():
    wanted = upscale_settings().get("cache_dir") or cache_location("upscale", os.path.join(
        os.path.dirname(os.path.realpath(DOWNLOADS_DIR)), "ksf-web-dl-upscale-cache"))
    cached = _upscale_index["dir"]
    # On the fallback the wanted place is tried again every ten minutes, so
    # creating it (on /mnt/media that needs root) takes effect without a restart.
    if cached and cached[0] == wanted and (cached[1] == wanted or time.time() - cached[2] < 600):
        return cached[1]
    path = wanted
    try:
        os.makedirs(path, exist_ok=True)
        if not os.access(path, os.W_OK):
            raise OSError("not writable")
    except OSError as exc:
        path = os.path.join(BASE_DIR, "media_upscale_cache")
        os.makedirs(path, exist_ok=True)
        if not cached or cached[1] != path:
            print(f"[Warning] live upscale cache {wanted} unusable ({exc}); using {path} until it is",
                  flush=True)
    if cached and cached[1] != path:
        _upscale_index["loaded"] = False        # another directory, another index
    _upscale_index["dir"] = (wanted, path, time.time())
    return path


def _upscale_entries():
    """The cache index. Caller holds _upscale_cv."""
    if not _upscale_index["loaded"]:
        try:
            with open(os.path.join(upscale_cache_dir(), "index.json"), encoding="utf-8") as fh:
                _upscale_index["entries"] = json.load(fh) or {}
        except (OSError, ValueError):
            _upscale_index["entries"] = {}
        _upscale_index["loaded"] = True
    return _upscale_index["entries"]


def _save_upscale_index():
    """Caller holds _upscale_cv."""
    path = os.path.join(upscale_cache_dir(), "index.json")
    with open(path + ".tmp", "w", encoding="utf-8") as fh:
        json.dump(_upscale_index["entries"], fh, separators=(",", ":"))
    os.replace(path + ".tmp", path)
    _upscale_index["dirty"] = False


def _touch_upscale(key):
    """The cached entry, marked as looked at now - the eviction clock. None for
    a cover's copy over "Largest song cover": a plan at the box can have the
    old copy's key (2x of a 2560 x 1440 cover came back 4x), and it is made
    again under that key within the box."""
    cap = cover_size_cap(upscale_settings())
    with _upscale_cv:
        entry = _upscale_entries().get(key)
        if not entry or _over_cover_cap(entry, cap):
            return None
        entry["last_viewed"] = time.time()
        _upscale_index["dirty"] = True
        return dict(entry)


# The photo upscalers the vision worker offers (vision/upscaler.py MODEL_SPECS),
# in the order Settings lists them: the user's shortlist from a side-by-side on
# their own photos, 2026-09-14 (vault KSF-019). Artwork keeps the anime model,
# which was fine; "general" (Real-ESRGAN x2plus) was the photo model before and
# wiped skin texture.
UPSCALE_PHOTO_MODELS = {
    "nomoswebphoto-realplksr": "4xNomosWebPhoto RealPLKSR",
    "nmkd-siax": "4x NMKD Siax CX",
    "ultrasharp": "4x-UltraSharp",
    "nomos2-otf-esrgan": "4xNomos2 otf ESRGAN",
}
UPSCALE_MODEL_LABELS = {**UPSCALE_PHOTO_MODELS, "anime": "Real-ESRGAN anime 6B",
                        "general": "Real-ESRGAN x2plus"}


def photo_upscale_model(cfg):
    """The photo upscaler Settings names, or the default for a name the list
    does not offer."""
    model = cfg.get("photo_model")
    return model if model in UPSCALE_PHOTO_MODELS else DEFAULT_CONFIG["live_upscale"]["photo_model"]


def upscale_model_from(tags, caption, face, cfg):
    """(model, why) from what the vision models saw - tags as {tag: p}, the
    caption, the best face score: "anime" for artwork, the photo upscaler
    Settings names for photos, cosplay and renders. why is None when nothing
    decided and default_model applied.

    In order: photo tags; the caption naming a photo, then artwork, then a
    costume or a photo in passing (UPSCALE_CAPTION_*); a face detected at least
    face_min_score surely; anime tags. Photo tags come first: WD14 tags a cosplay
    photo "1girl" just like a drawing, but only calls photographs and 3D renders
    "realistic". It leaves many cosplay photos without a photo tag, and for
    those "1girl" - or the word "anime" in the caption - used to pick the anime
    model (vault KSF-019).
    """
    photo = photo_upscale_model(cfg)
    min_p = float(cfg.get("tag_min_confidence") or 0.35)
    for tag in cfg.get("photo_tags") or []:
        if tags.get(tag, 0) >= min_p:
            return photo, f"tag {tag}"
    if UPSCALE_CAPTION_PHOTO.search(caption):
        return photo, "caption describes a photo"
    if UPSCALE_CAPTION_ART.search(caption):
        return "anime", "caption describes artwork"
    if UPSCALE_CAPTION_COSTUME.search(caption):
        return photo, "caption mentions a costume or a photo"
    min_face = float(cfg.get("face_min_score") or 0.8)
    if min_face < 1 and face >= min_face:
        return photo, f"face detected at {face:.2f}"
    for tag in cfg.get("anime_tags") or []:
        if tags.get(tag, 0) >= min_p:
            return "anime", f"tag {tag}"
    return ("anime" if cfg.get("default_model") == "anime" else photo), None


def film_grain_hint(root_id, rel_path, cfg=None):
    """True when the tagger called this picture film_grain. Only a hint for the
    vision worker, which still measures the grain itself: the tag lowers how
    much it needs to see (JPEG leaves little of subtle grain), and a tagged
    picture whose grain JPEG erased is still left alone."""
    cfg = cfg or upscale_settings()
    try:
        conn = media_ai.connect(BASE_DIR)
        row = conn.execute(
            "SELECT t.p FROM media m JOIN media_tags t ON t.media_id = m.id"
            " WHERE m.root = ? AND m.path = ? AND t.tag = 'film_grain'", (root_id, rel_path)).fetchone()
    except Exception:
        return False
    return bool(row) and (row["p"] or 0.0) >= float(cfg.get("tag_min_confidence") or 0.35)


def choose_upscale_model(root_id, rel_path):
    """(model, why) for a picture, by upscale_model_from's rules on what the
    AI index holds for it."""
    cfg = upscale_settings()
    tags, caption, face = {}, "", 0.0
    try:
        conn = media_ai.connect(BASE_DIR)
        row = conn.execute("SELECT id, caption FROM media WHERE root=? AND path=?",
                           (root_id, rel_path)).fetchone()
        if row:
            caption = row["caption"] or ""
            tags = {r["tag"]: (r["p"] or 0.0) for r in conn.execute(
                "SELECT tag, p FROM media_tags WHERE media_id=?", (row["id"],))}
            face = conn.execute("SELECT MAX(det_score) AS s FROM faces WHERE media_id=?",
                                (row["id"],)).fetchone()["s"] or 0.0
    except Exception as exc:
        print(f"[Warning] upscale model choice: {exc}")
    model, reason = upscale_model_from(tags, caption, face, cfg)
    return model, reason or ("not indexed yet" if not tags and not caption and not face else "no deciding tag")


# THE MODEL FOR ONE FILE, SET BY HAND. The rules answer from what the vision
# models saw, and for some files there is nothing there to read differently:
# twitter/@_m_3030/2092241371868635209_1.jpg is a cosplay photograph that
# Florence captions "The image is animated, giving it a dynamic feel" (an
# artwork word), that WD14 gives no photo tag at all and tags "1girl" 0.99, and
# whose one face scores 0.67 against the 0.8 the face rule needs - three
# independent reasons for the anime model and not one signal for the right one
# (vault KSF-019, second report). This is the escape hatch for those: it wins
# over the whole chain, and the choices made here are the labelled data a real
# photo-vs-drawing classifier will be measured against.
#
# Kept beside the AI index, not in it: the index holds what the models really
# said, and a person disagreeing with the conclusion does not make the caption
# wrong. Same reasoning as upscale_covers.json above.
#
# The KIND is stored ("photo" or "anime"), not the model name, so a file set to
# "photo" follows live_upscale.photo_model when that is changed in Settings -
# the user picked "a photograph", not "4xNomosWebPhoto".
UPSCALE_MODEL_STORE = os.path.join(BASE_DIR, "upscale_models.json")
UPSCALE_MODEL_STORE_MAX = 20000
UPSCALE_MODEL_KINDS = ("photo", "anime")
_model_lock = threading.Lock()
_model_overrides = {"loaded": False, "entries": {}}     # root -> path -> {kind, by, at}


def _model_store():
    """Caller holds _model_lock."""
    if not _model_overrides["loaded"]:
        try:
            with open(UPSCALE_MODEL_STORE, encoding="utf-8") as fh:
                loaded = json.load(fh)
            _model_overrides["entries"] = loaded if isinstance(loaded, dict) else {}
        except (OSError, ValueError):
            _model_overrides["entries"] = {}
        _model_overrides["loaded"] = True
    return _model_overrides["entries"]


def _save_model_store():
    """Caller holds _model_lock."""
    entries = _model_overrides["entries"]
    total = sum(len(paths) for paths in entries.values())
    if total > UPSCALE_MODEL_STORE_MAX:
        # Oldest first: what someone set today is what they still care about.
        flat = sorted((e.get("at") or 0, root, path)
                      for root, paths in entries.items() for path, e in paths.items())
        for _, root, path in flat[:total - UPSCALE_MODEL_STORE_MAX]:
            entries[root].pop(path, None)
    for root in [r for r, paths in entries.items() if not paths]:
        entries.pop(root, None)
    try:
        with open(UPSCALE_MODEL_STORE + ".tmp", "w", encoding="utf-8") as fh:
            json.dump(entries, fh, separators=(",", ":"))
        os.replace(UPSCALE_MODEL_STORE + ".tmp", UPSCALE_MODEL_STORE)
    except OSError as exc:
        print(f"[Warning] live upscale: {UPSCALE_MODEL_STORE} not saved: {exc}", flush=True)


def upscale_model_override(root_id, rel_path):
    """{kind, by, at} for a file whose model was set by hand, or None."""
    with _model_lock:
        entry = (_model_store().get(root_id) or {}).get(rel_path)
        return dict(entry) if isinstance(entry, dict) else None


def set_upscale_model_override(root_id, rel_path, kind, by):
    """kind "photo" or "anime" to set, None to go back to the rules."""
    with _model_lock:
        entries = _model_store()
        if kind is None:
            (entries.get(root_id) or {}).pop(rel_path, None)
        else:
            entries.setdefault(root_id, {})[rel_path] = {"kind": kind, "by": by, "at": time.time()}
        _save_model_store()
    return upscale_model_override(root_id, rel_path)


# Music covers. An audio file's preview shows the cover embedded in it (the
# picture /api/media/thumbnail serves, get_embedded_cover), and that cover is
# upscaled by the same rules as a picture file: the same wait, zoom, limits,
# cache and model rules. The AI index holds no audio, so the question the model
# rules answer from it - photo or artwork? - is asked of the cover itself: the
# vision worker analyses it once (caption, tags, faces, as the indexer does for
# a picture) when the page first asks for the upscale, and the answer is kept
# in upscale_covers.json - not in the index, where it would make songs turn up
# in picture searches (vault KSF-019).
UPSCALE_COVER_STORE = os.path.join(BASE_DIR, "upscale_covers.json")
UPSCALE_COVER_STORE_MAX = 5000
_cover_lock = threading.Lock()
_cover_sizes = {}          # (full path, size, mtime_ns) -> (w, h), or None for no readable cover
_cover_analysis = {"loaded": False, "entries": {}}
_cover_failures = {}       # store key -> (at, error); not asked again for ten minutes
_cover_busy = set()        # store keys being analysed right now


def upscale_cover_bytes(full):
    cover = get_embedded_cover(full)
    return cover[1] if cover and cover[1] else None


def upscale_cover_size(full, st):
    """(width, height) of the cover in an audio file, or None when it has none.
    Kept per version of the file: the page asks every two seconds while a job
    runs, and reading a cover out means parsing the file."""
    key = (full, st.st_size, st.st_mtime_ns)
    with _cover_lock:
        if key in _cover_sizes:
            return _cover_sizes[key]
    blob = upscale_cover_bytes(full)
    size = media_dimensions.probe_image(io.BytesIO(blob)) if blob else None
    with _cover_lock:
        if len(_cover_sizes) > 2000:
            _cover_sizes.clear()
        _cover_sizes[key] = size
    return size


def _cover_store():
    """What the worker saw in each cover analysed so far. Caller holds _cover_lock."""
    if not _cover_analysis["loaded"]:
        try:
            with open(UPSCALE_COVER_STORE, encoding="utf-8") as fh:
                _cover_analysis["entries"] = json.load(fh) or {}
        except (OSError, ValueError):
            _cover_analysis["entries"] = {}
        _cover_analysis["loaded"] = True
    return _cover_analysis["entries"]


def _cover_known(store_key, st, cfg):
    """(model, why) from a stored analysis of this version of the file, or from a
    failure less than ten minutes old (the default model, and why); None when
    the worker has yet to be asked. Caller holds _cover_lock."""
    entry = _cover_store().get(store_key)
    if entry and entry.get("size") == st.st_size and entry.get("mtime_ns") == st.st_mtime_ns:
        model, reason = upscale_model_from(entry.get("tags") or {}, entry.get("caption") or "",
                                           float(entry.get("face") or 0), cfg)
        return model, "cover: " + (reason or "nothing in it decides")
    failed = _cover_failures.get(store_key)
    if failed and time.time() - failed[0] < 600:
        return upscale_model_from({}, "", 0.0, cfg)[0], f"cover not analysed: {failed[1]}"
    return None


def analyse_cover(blob):
    """(caption, {tag: p}, best face score) for one picture from the vision
    worker's /v1/analyze - the passes the indexer asks for, less OCR. Sent as
    urgent: someone is looking at it, and allow_urgent_anytime in the worker's
    config lets that through outside its processing window."""
    base, token = vision_worker_address()
    boundary = secrets.token_hex(16).encode()
    body = (b"--" + boundary + b'\r\nContent-Disposition: form-data; name="file"; filename="cover"\r\n'
            b"Content-Type: application/octet-stream\r\n\r\n" + blob + b"\r\n")
    for name, value in (("tasks", "caption,tags,faces"), ("urgent", "1")):
        body += (b"--" + boundary + b'\r\nContent-Disposition: form-data; name="' + name.encode()
                 + b'"\r\n\r\n' + value.encode() + b"\r\n")
    body += b"--" + boundary + b"--\r\n"
    req = urllib.request.Request(base.rstrip("/") + "/v1/analyze", data=body, method="POST")
    req.add_header("Content-Type", "multipart/form-data; boundary=" + boundary.decode())
    if token:
        req.add_header("Authorization", "Bearer " + token)
    try:
        # Up to a minute when the worker has unloaded its models and loads them first.
        with urllib.request.urlopen(req, timeout=180) as r:
            data = json.loads(r.read()) or {}
    except urllib.error.HTTPError as exc:
        text = exc.read()[:1000].decode("utf-8", "replace")
        try:
            detail = json.loads(text) or {}
        except ValueError:
            detail = {}
        raise RuntimeError(f"the vision worker refused it (HTTP {exc.code}): "
                           f"{detail.get('error') or text[:200] or exc.reason}")
    except TimeoutError:
        raise RuntimeError("the vision worker did not answer within 3 minutes")
    except urllib.error.URLError as exc:
        raise RuntimeError(f"the vision worker could not be reached ({exc.reason})")
    if data.get("failed"):
        raise RuntimeError(f"the vision worker could not read the cover: {data['failed'][0].get('error')}")
    result = (data.get("results") or [{}])[0]
    tags = {}
    for entries in (result.get("tags") or {}).values():
        for e in entries or []:
            tags[e["tag"]] = max(tags.get(e["tag"], 0.0), float(e.get("p") or 0))
    face = max((float(f.get("det_score") or 0) for f in result.get("faces") or []), default=0.0)
    return (result.get("caption") or "").strip(), tags, face


def _analyse_cover_now(store_key, rel_path, full, st):
    try:
        blob = upscale_cover_bytes(full)
        if not blob:
            raise RuntimeError("the cover could not be read out of the file")
        caption, tags, face = analyse_cover(blob)
    except Exception as exc:
        print(f"[Warning] live upscale: cover of {rel_path} not analysed: {exc}", flush=True)
        with _cover_lock:
            _cover_failures[store_key] = (time.time(), str(exc)[:200])
        return
    with _cover_lock:
        store = _cover_store()
        store[store_key] = {"size": st.st_size, "mtime_ns": st.st_mtime_ns, "caption": caption,
                            "tags": {t: round(p, 3) for t, p in tags.items()},
                            "face": round(face, 4), "at": round(time.time())}
        _cover_failures.pop(store_key, None)
        if len(store) > UPSCALE_COVER_STORE_MAX:
            for old in sorted(store, key=lambda k: store[k].get("at") or 0)[:len(store) - UPSCALE_COVER_STORE_MAX]:
                store.pop(old, None)
        try:
            with open(UPSCALE_COVER_STORE + ".tmp", "w", encoding="utf-8") as fh:
                json.dump(store, fh, separators=(",", ":"))
            os.replace(UPSCALE_COVER_STORE + ".tmp", UPSCALE_COVER_STORE)
        except OSError as exc:
            print(f"[Warning] live upscale: {UPSCALE_COVER_STORE} not saved: {exc}", flush=True)


def cover_upscale_model(root_id, rel_path, full, st, analyse):
    """(model, why) for an audio file's cover, by upscale_model_from's rules on
    what the vision worker saw in it. Before the cover is analysed: (None, a
    status for the page) - "none", as nothing can be cached under a model not
    chosen yet, or "working" while another request is asking. With analyse this
    call asks the worker itself, which takes seconds, up to a minute while the
    worker loads its models."""
    store_key = root_id + "\0" + rel_path
    cfg = upscale_settings()
    with _cover_lock:
        known = _cover_known(store_key, st, cfg)
        busy = store_key in _cover_busy
        mine = known is None and analyse and not busy
        if mine:
            _cover_busy.add(store_key)
    if mine:
        try:
            _analyse_cover_now(store_key, rel_path, full, st)
        finally:
            with _cover_lock:
                _cover_busy.discard(store_key)
                known = _cover_known(store_key, st, cfg)
    if known is None:
        return None, ("working" if busy else "none")
    return known


# Soft covers: covers larger than they are sharp. A visualiser channel's 4K
# upload is often a smaller render stretched to 4K, and its cover - a frame of
# it - holds no more than that: "not needed" by its size, soft on the screen
# (user, 2026-09-27: "All music visualizations by MHM and Spirit that are 4K
# are still not sharp. They should also be fed into the upscaler. Geoxor can be
# treated normally, though. There should be a list of folders where the song
# covers are treated as if they were not high enough resolution."). A cover in
# one of live_upscale.soft_cover_folders counts as at most soft_cover_height
# pixels on its short side: it is shrunk to that before the GPU pass and
# upscaled from there like any small cover (vault KSF-060). A model fed the
# 4K frame itself would see soft edges spread over several pixels and keep
# them; at the size the picture really has, they are one pixel wide and it
# draws them sharp.
#
# Soft covers, found by measuring (user, 2026-09-28: "You may also
# automatically detect when a cover is basically a 4K image that is at most as
# sharp as 1440p ... The info panel must list when this happened."). A cover
# of 4K or more outside the list is measured once per file version
# (cover_edge_steepness): on its strong edges, the share of the edge's
# contrast its steepest single pixel step holds, at 4K. A sharp 4K cover
# limited to 1080p measures about 0.40, to 1440p 0.47; real Geoxor covers
# 0.42-0.77 (median 0.59), MHM and Spirit 0.29-0.54 (median 0.40). At or below
# soft_cover_auto_edge (0.40: no Geoxor cover, 58% of MHM/Spirit, and on a
# contact sheet only visibly blurred covers from other channels) the cover is
# soft, and shrinking it to 1080p first loses nothing it had. What did NOT
# separate them (vault KSF-060): halving and restoring (PSNR 43 / 45 / 41 dB),
# the energy above 1440p, the spectrum's fall into the noise floor, and how
# much edge steepness a trip through 1440p costs - MHM covers keep fine
# texture above 1440p while their edges are soft, so the soft part is the
# drawing, not a stretch from a smaller size.
#
# Every cover since 2026-09-28 (user: "Ok, apply the measured upscaling
# approach to ALL covers for now."): soft_cover_auto_min_long, default 0,
# instead of 4K only (3800 brings that back). The measurement is in pixels of
# the size it is taken at - the cover fitted into 3840 x 2160, smaller ones as
# they are - so 0.40 means "no sharper than half that size" at every size.
# Half is only how far the measurement vouches for; how far a soft cover IS
# shrunk is soft_cover_height on its short side, like a listed cover (user,
# 2026-09-28, after 1080p covers went to 960 x 540: "Shrinking 1080p by half
# seems like it would loose most of the detail. If somehow possibly, don't
# shrink under 1440p, even if the result is way bigger than needed."). So
# with 1440: a soft 4K cover is taken as 2560 x 1440 and made 2x (5120 x
# 2880 for a 4K screen), a 1440p or 1080p cover is never shrunk, and never
# measured either. The half size still binds if soft_cover_height is set
# lower. Never smaller than max_factor can bring back to the screen, and not
# at all when that leaves more than SOFT_AUTO_MAX_SCALE of the file: a
# shrink that small is not worth the measurement.
_soft_names = {"pair": (None, {})}         # (the catalogue it was built from, {(root, path): all its names})
SOFT_AUTO_MAX_SCALE = 0.8
SOFT_EDGE_STORE = os.path.join(BASE_DIR, "upscale_cover_edges.json")
SOFT_EDGE_STORE_MAX = 20000
_soft_edge_lock = threading.Lock()         # one measurement at a time; guards the store
_soft_edges = {"loaded": False, "entries": {}}


def _soft_folder_rules(cfg):
    """[(root id or None, folder)] from soft_cover_folders. An entry is a folder
    as the details panel names it - "Downloads / youtube_music/MHM" - or a bare
    path, which counts in every root."""
    return named_folder_rules(cfg.get("soft_cover_folders"))


def named_folder_rules(entries):
    """[(root id or None, folder)] from a settings list of folders, each named as
    the details panel names it - "Downloads / youtube_music/MHM" -, as the media
    page's breadcrumb shows it - "Libraries / Downloads / youtube_music / MHM" -
    or a bare path, which counts in every root."""
    roots = {(r.get("name") or "").strip().casefold(): r["id"] for r in get_all_roots() if r.get("name")}
    rules = []
    for entry in entries or []:
        text = unicodedata.normalize("NFC", str(entry)).strip()
        root_id = None
        # " / " is only ever a separator: a folder name cannot hold a slash.
        parts = [part.strip() for part in text.split(" / ")]
        if len(parts) > 2 and parts[0].casefold() == "libraries" and parts[1].casefold() in roots:
            parts = parts[1:]           # the breadcrumb's own first step
        if len(parts) > 1 and parts[0].casefold() in roots:
            root_id, parts = roots[parts[0].casefold()], parts[1:]
        folder = "/".join(parts).strip().strip("/")
        if folder:
            rules.append((root_id, folder))
    return rules


def _song_names(root_id, rel_path):
    """Every (root, path) the music catalogue knows this file by - its home
    under the channel and the hard links playlists hold of it - so a song
    played from a playlist is in its channel's folder too. Only the name
    asked for while there is no catalogue, or for a file that is not a song
    there. Never starts a catalogue build."""
    with _music_lock:
        catalog = _music["catalog"]
    if catalog is None:
        return {(root_id, rel_path)}
    built_from, names = _soft_names["pair"]
    if built_from is not catalog:
        names = {}
        for s in catalog.get("songs") or []:
            group = {(s["root"], s["path"])} | {tuple(n) for n in s.get("names") or []}
            if len(group) > 1:
                for n in group:
                    names[n] = group
        _soft_names["pair"] = (catalog, names)     # one assignment: never a new catalogue with old names
    return names.get((root_id, rel_path)) or {(root_id, rel_path)}


def soft_cover_folder(root_id, rel_path, cfg):
    """The soft_cover_folders entry that holds this audio file under any of its
    names - its folder or one above it - or None."""
    rules = _soft_folder_rules(cfg)
    if not rules:
        return None
    for root, path in sorted(_song_names(root_id, rel_path)):
        path = unicodedata.normalize("NFC", path)
        for rule_root, folder in rules:
            if (rule_root is None or rule_root == root) and path.startswith(folder + "/"):
                return folder
    return None


def soft_cover_blob(blob, size):
    """The cover shrunk to `size` (Lanczos), as PNG so the shrinking is the only
    loss: what the GPU pass of a soft cover starts from."""
    from PIL import Image
    img = Image.open(io.BytesIO(blob))
    img.load()
    if img.mode not in ("RGB", "RGBA"):
        img = img.convert("RGBA" if "A" in img.getbands() or "transparency" in img.info else "RGB")
    out = io.BytesIO()
    img.resize((int(size[0]), int(size[1])), Image.LANCZOS).save(out, "PNG", compress_level=1)
    return out.getvalue()


def _max_filter(a, r):
    """The maximum over the (2r+1)-square around each pixel (separable)."""
    import numpy as np
    out = a.copy()
    for d in range(1, r + 1):
        np.maximum(out[:, d:], a[:, :-d], out=out[:, d:])
        np.maximum(out[:, :-d], a[:, d:], out=out[:, :-d])
    rows = out.copy()
    for d in range(1, r + 1):
        np.maximum(out[d:, :], rows[:-d, :], out=out[d:, :])
        np.maximum(out[:-d, :], rows[d:, :], out=out[:-d, :])
    return out


def edge_steepness(blob):
    """How steep a picture's edges are as a 3840 x 2160 screen shows it: over
    the pixels near a strong edge (a 7 x 7 window spanning more than 80 grey
    levels), the median of the window's steepest single pixel step over its
    span. 1.0 is a hard one-pixel step. None when there are too few strong
    edges to tell (dark or flat covers) - never taken as soft."""
    import numpy as np
    from PIL import Image
    grey = Image.open(io.BytesIO(blob)).convert("L")
    s = min(3840 / grey.size[0], 2160 / grey.size[1], 1.0)
    if s < 0.999:
        grey = grey.resize((max(1, round(grey.size[0] * s)), max(1, round(grey.size[1] * s))), Image.LANCZOS)
    a = np.asarray(grey, dtype=np.float32)
    span = _max_filter(a, 3) + _max_filter(-a, 3)
    strong = span > 80
    if int(strong.sum()) < 2000:
        return None
    step = np.zeros_like(a)
    step[:, :-1] = np.abs(np.diff(a, axis=1))
    step[:-1, :] = np.maximum(step[:-1, :], np.abs(np.diff(a, axis=0)))
    return float(np.median(_max_filter(step, 3)[strong] / span[strong]))


_UNMEASURED = object()


def _soft_edge_store():
    """Caller holds _soft_edge_lock."""
    if not _soft_edges["loaded"]:
        try:
            with open(SOFT_EDGE_STORE, encoding="utf-8") as fh:
                _soft_edges["entries"] = json.load(fh) or {}
        except (OSError, ValueError):
            _soft_edges["entries"] = {}
        _soft_edges["loaded"] = True
    return _soft_edges["entries"]


def cover_edge_steepness(root_id, rel_path, full, st, measure):
    """edge_steepness of this version of an audio file's cover, measured once
    and kept in upscale_cover_edges.json (None: too few edges to tell). Not
    measured yet and `measure` off - the page's check on opening, which has to
    answer at once - gives _UNMEASURED."""
    store_key = root_id + "\0" + rel_path

    def known():
        entry = _soft_edge_store().get(store_key)
        if entry and entry.get("size") == st.st_size and entry.get("mtime_ns") == st.st_mtime_ns:
            return entry.get("edge")
        return _UNMEASURED

    with _soft_edge_lock:
        value = known()
    if value is not _UNMEASURED or not measure:
        return value
    # Under the lock: two asks for one cover measure it once. Half a second of
    # CPU on a 4K cover, then never again for this version of the file.
    with _soft_edge_lock:
        value = known()
        if value is not _UNMEASURED:
            return value
        try:
            blob = upscale_cover_bytes(full)
            value = edge_steepness(blob) if blob else None
        except Exception as exc:
            # Not stored: the next ask tries again. None is "not soft".
            print(f"[Warning] live upscale: cover of {rel_path} not measured: {exc}", flush=True)
            return None
        store = _soft_edge_store()
        store[store_key] = {"size": st.st_size, "mtime_ns": st.st_mtime_ns,
                            "edge": None if value is None else round(value, 4), "at": round(time.time())}
        if len(store) > SOFT_EDGE_STORE_MAX:
            for old in sorted(store, key=lambda k: store[k].get("at") or 0)[:len(store) - SOFT_EDGE_STORE_MAX]:
                store.pop(old, None)
        try:
            with open(SOFT_EDGE_STORE + ".tmp", "w", encoding="utf-8") as fh:
                json.dump(store, fh, separators=(",", ":"))
            os.replace(SOFT_EDGE_STORE + ".tmp", SOFT_EDGE_STORE)
        except OSError as exc:
            print(f"[Warning] live upscale: {SOFT_EDGE_STORE} not saved: {exc}", flush=True)
    return value


_worker_upscale_limit = {"px": None, "at": 0.0}
_worker_upscale_limit_lock = threading.Lock()


def worker_upscale_limit_px(refresh=False):
    """The vision worker's own ceiling on an upscale's output (upscale_max_output_px
    in its /v1/status), cached five minutes; None when it cannot be asked. Plans
    stay within the lower of this and the "Largest result" setting - the two used
    to be independent, so a 256 MP setting planned jobs a 64 MP worker refused
    with HTTP 413. The lock is held across the request: one call at a time."""
    with _worker_upscale_limit_lock:
        cached = _worker_upscale_limit
        if not refresh and cached["at"] and time.time() - cached["at"] < 300:
            return cached["px"]
        base, token = vision_worker_address()
        req = urllib.request.Request(base.rstrip("/") + "/v1/status")
        if token:
            req.add_header("Authorization", "Bearer " + token)
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                px = int((json.loads(r.read()) or {}).get("upscale_max_output_px") or 0) or None
        except (OSError, ValueError):
            px = cached["px"]              # keep what was known; the worker may be restarting
        cached.update(px=px, at=time.time())
        return px


def note_worker_upscale_limit(px):
    with _worker_upscale_limit_lock:
        _worker_upscale_limit.update(px=px, at=time.time())


# Under this much room a megapixel limit leaves nothing worth a GPU pass, and the
# file is shown as it is.
UPSCALE_MIN_SCALE = 1.1
COVER_SIZE_CHOICES = ("3840x2160", "5120x2880", "7680x4320", "15360x8640", "none")


def cover_size_cap(cfg):
    """(width, height) no song cover's upscaled copy may exceed, or None.
    User, 2026-09-28: "cap the size of song cover upscale files that are cached
    to 8K (7680 x 4320) after the removal of the black bars. Downscaling after
    upscaling is allowed." - "the cap should be a setting". A box, not
    megapixels: a square or portrait cover stops at its height, as it would on
    that screen. Black bars are to be cut out of the cover file itself (the
    border cropping), so once that has run the size planned from is the
    picture without them."""
    raw = str(cfg.get("cover_max_size", "7680x4320") or "").lower().replace("×", "x").replace(" ", "")
    w, _, h = raw.partition("x")
    try:
        w, h = int(w), int(h)
    except ValueError:
        return None
    return (w, h) if w > 0 and h > 0 else None


def _over_cover_cap(entry, cap):
    out = entry.get("out") or [0, 0]
    return bool(cap and entry.get("cover") and (out[0] > cap[0] or out[1] > cap[1]))


def plan_upscale(root_id, rel_path, need_w, need_h, analyse=False):
    """(job spec, None), or (None, a status for the page). An audio file is
    planned for its cover ("Music covers" above). analyse lets this call have
    the worker analyse a cover the model choice has not seen yet: the page's
    ask after view_seconds does, its check on opening does not."""
    cfg = upscale_settings()
    if not cfg.get("enabled"):
        return None, {"status": "disabled"}
    full = resolve_media_path(root_id, rel_path)
    if not full or not os.path.isfile(full):
        return None, {"status": "missing"}
    cover = classify_media_type(full) == "audio"
    if not cover and os.path.splitext(full)[1].lower() not in UPSCALE_EXTS:
        return None, {"status": "unsupported"}
    st = os.stat(full)
    dims = None if cover else media_dimensions_map().get(media_dimensions.key_for(root_id, rel_path))
    if cover:
        width, height = upscale_cover_size(full, st) or (0, 0)
    elif dims and len(dims) > 2 and dims[2] == st.st_size:
        width, height = dims[0], dims[1]
    else:
        width, height = media_dimensions.probe_image(full) or (0, 0)
    if not width or not height:
        return None, {"status": "unsupported"}
    # A soft cover is planned as the smaller picture it really is: `width` and
    # `height` are that size from here on, and _run_upscale sends it shrunk.
    soft = None
    if cover:
        short = int(cfg.get("soft_cover_height") or 0)
        folder = soft_cover_folder(root_id, rel_path, cfg) if short else None
        if folder:
            # Shrunk to soft_cover_height when larger. A listed cover at or
            # under it is soft at its own size, and upscaled all the same: the
            # folder says its covers are never sharp enough (user, 2026-09-28,
            # of a 2560 x 1440 MHM cover "not needed" on a 1440p screen: "This
            # should not have happened").
            s = min(1.0, short / min(width, height))
            soft = {"height": min(short, min(width, height)), "folder": folder, "file": [width, height],
                    "size": [max(1, round(width * s)), max(1, round(height * s))]}
        elif cfg.get("soft_cover_auto", True) and max(width, height) >= int(cfg.get("soft_cover_auto_min_long") or 0):
            # soft_cover_height on the short side, like a listed cover; never
            # below half the size edge_steepness measures at (fitted into 3840
            # x 2160), nor smaller than max_factor brings back to the screen.
            fit = min(3840 / width, 2160 / height, 1.0)
            s = max(fit / 2, short / min(width, height),
                    max(need_w / width, need_h / height) / max(2, int(cfg.get("max_factor") or 4)))
            size = [max(1, round(width * s)), max(1, round(height * s))]
            # On every screen: a soft cover is upscaled even where its soft
            # size already fills it (below), so the answer depends on it.
            if s <= SOFT_AUTO_MAX_SCALE:
                edge = cover_edge_steepness(root_id, rel_path, full, st, measure=analyse)
                if edge is _UNMEASURED:
                    return None, {"status": "none", "unmeasured": True}
                limit = float(cfg.get("soft_cover_auto_edge") or 0)
                if edge is not None and edge <= limit:
                    soft = {"height": min(size), "folder": None, "file": [width, height], "size": size,
                            "auto": {"edge": round(edge, 3), "limit": round(limit, 3)}}
        if soft:
            width, height = soft["size"]
    # A soft cover is never "not needed": it goes through the upscaler on
    # every screen, 2x at least from its soft size, however much bigger than
    # the screen that is (user, 2026-09-28, after a listed 4K cover taken as
    # 2560 x 1440 came out "not needed" on a 1440p screen: "don't shrink under
    # 1440p, even if the result is way bigger than needed" / "This should not
    # have happened").
    if not soft and width >= need_w and height >= need_h:
        return None, {"status": "not_needed", "width": width, "height": height, **({"soft": soft} if soft else {})}
    ratio = max(need_w / width, need_h / height)
    # The smallest whole factor that reaches the zoom - never below 2, since a
    # result barely larger than the original is not worth a GPU pass - up to
    # max_factor. Whole factors keep one cached copy good for many screens.
    # 1e-9: a ratio of 2.0000000001 out of float division is still 2x.
    needed = max(2, math.ceil(ratio - 1e-9))
    wanted = min(needed, max(2, int(cfg.get("max_factor") or 4)))
    # The lower of the "Largest result" setting and the vision worker's own
    # ceiling - and which one it was, because the details panel names it.
    setting_px = float(cfg.get("max_output_megapixels") or 64) * 1e6
    worker_px = worker_upscale_limit_px()
    max_px, limit_by = setting_px, "setting"
    if worker_px and worker_px < setting_px:
        max_px, limit_by = float(worker_px), "worker"
    area = width * height
    limited = None
    if area * wanted * wanted <= max_px:
        factor, target = wanted, (width * wanted, height * wanted)
        if wanted < needed:
            limited = {"needed": needed, "by": "max_factor"}
    else:
        # Bound by megapixels: the LARGEST picture within the limit, at whatever
        # scale that is. The limit caps what is served; it is never a reason to
        # serve less (a whole factor below it) or nothing. Only a file already
        # about as large as the limit is left as it is.
        scale = math.sqrt(max_px / area)
        if scale < UPSCALE_MIN_SCALE:
            return None, {"status": "too_large", "width": width, "height": height,
                          "original_mp": round(area / 1e6, 1),
                          "limit_mp": round(max_px / 1e6, 1), "limit_by": limit_by}
        target = (int(width * scale), int(height * scale))
        factor = math.ceil(scale)
        limited = {"needed": needed, "by": limit_by, "limit_mp": round(max_px / 1e6, 1)}
    max_scale = max(2, int(cfg.get("max_factor") or 4))
    # A song cover's copy fits "Largest song cover": the model still runs its
    # whole factor, the worker shrinks the pass into the box - this target, and
    # max_scale for a pass that overshoots it (a 4x model on a 2560 x 1440
    # cover came back 10240 x 5760: 68 such copies in the cache on 2026-09-28).
    cap = cover_size_cap(cfg) if cover else None
    if cap:
        cap_scale = min(cap[0] / width, cap[1] / height)
        max_scale = min(max_scale, cap_scale)
        if target[0] > cap[0] or target[1] > cap[1]:
            if cap_scale < UPSCALE_MIN_SCALE:
                return None, {"status": "too_large", "width": width, "height": height,
                              "original_mp": round(area / 1e6, 1), "limit_by": "cover_size",
                              "limit_size": list(cap)}
            target = (min(cap[0], int(width * cap_scale)), min(cap[1], int(height * cap_scale)))
            factor = math.ceil(cap_scale - 1e-9)
            limited = {"needed": needed, "by": "cover_size", "size": list(cap)}
    if cover:
        model, reason = cover_upscale_model(root_id, rel_path, full, st, analyse)
        if not model:
            return None, {"status": reason}
    else:
        model, reason = choose_upscale_model(root_id, rel_path)
    # Set by hand for this one file: it wins over every rule, covers included.
    # The model is in the cache key, so flipping back and forth costs one GPU
    # pass each way and then never again - the other copy is still cached.
    override = upscale_model_override(root_id, rel_path)
    if override:
        model = "anime" if override.get("kind") == "anime" else photo_upscale_model(cfg)
        reason = f"set by hand ({override.get('by') or 'unknown'})"
    # Film grain is put back on artwork only: the photo models were chosen for
    # how they handle a photo's own noise, and a camera's noise is not a texture
    # someone laid there on purpose.
    grain = model == "anime" and bool(cfg.get("restore_grain", True))
    # A whole factor keys as it always did ("3"); a size at a limit keys by that
    # size, so the two can never be mistaken for each other. Grain is part of
    # the key: a copy made without it is made again, once, when next viewed -
    # and so is one made with an older way of drawing it (GRAIN_VERSION).
    size_key = str(factor) if target == (width * factor, height * factor) else f"{target[0]}x{target[1]}"
    # A soft cover's copy is another picture than one upscaled from the whole
    # cover: its own key, and the lookups keep the two apart (cached_upscale_of).
    raw = "\0".join((root_id, rel_path, str(st.st_size), str(st.st_mtime_ns), size_key,
                     model + (f"+grain{GRAIN_VERSION}" if grain else "") + (f"+soft{soft['height']}" if soft else "")))
    return {"key": hashlib.sha256(raw.encode("utf-8")).hexdigest()[:40], "root": root_id,
            "path": rel_path, "full": full, "factor": factor, "model": model,
            "grain": grain, "grain_hint": grain and film_grain_hint(root_id, rel_path, cfg),
            "grain_version": GRAIN_VERSION if grain else None,
            "reason": reason, "src": [width, height], "limited": limited,
            "override": override.get("kind") if override else None,
            "target": list(target), "scale": round(target[0] / width, 2), "cover": cover,
            "soft": soft, "version": upscale_version(st),
            # The bounds a pass that overshoots the target may be served within.
            "max_px": int(max_px), "max_scale": max_scale}, None


def vision_worker_address():
    """(base URL, token) of the vision worker - the indexer's own setting."""
    try:
        with open(os.path.join(BASE_DIR, "indexer_config.json"), encoding="utf-8") as fh:
            cfg = json.load(fh) or {}
    except (OSError, ValueError):
        cfg = {}
    return (cfg.get("worker") or DEFAULT_WORKER_URL), (cfg.get("token") or "")


GRAIN_INFO_KEYS = ("added", "why", "sigma", "kept", "added_sigma", "mono", "size", "fine", "hinted", "seconds")

# How the vision worker draws film grain (vision/grain.py). Bump it with every
# change to what grain.py draws: a copy made with grain under an older version
# is then made again when next viewed, instead of waiting for Settings > Clear
# cache. 2: grain drawn twice as fine as the original's pixels (user,
# 2026-09-22: "The grain should be higher frequency" - "2x finer A looks good").
GRAIN_VERSION = 2


def parse_grain_header(raw):
    """What the vision worker did about film grain (its X-Upscale-Grain
    header), reduced to the fields the details panel shows - or None when it
    was not asked or sent nothing readable. A worker from before grain.py
    sends no header at all."""
    if not raw:
        return None
    try:
        info = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(info, dict):
        return None
    out = {}
    for key in GRAIN_INFO_KEYS:
        value = info.get(key)
        if isinstance(value, bool) or (key in ("added", "hinted") and value is not None):
            out[key] = bool(value)
        elif isinstance(value, (int, float)):
            out[key] = round(float(value), 3)
        elif isinstance(value, str):
            out[key] = value[:200]
    return out if "added" in out else None


class UpscaleLimitError(RuntimeError):
    """The worker refused an output over its own ceiling (HTTP 413)."""

    def __init__(self, message, max_output_px):
        super().__init__(message)
        self.max_output_px = max_output_px


def _run_upscale(spec):
    cfg = upscale_settings()
    base, token = vision_worker_address()
    if spec.get("cover"):
        blob = upscale_cover_bytes(spec["full"])
        if not blob:
            raise RuntimeError("the cover could not be read out of the file")
        # A listed cover no larger than soft_cover_height goes as it is.
        if spec.get("soft") and list(spec["soft"].get("file") or []) != list(spec["src"]):
            blob = soft_cover_blob(blob, spec["src"])
    else:
        with open(spec["full"], "rb") as fh:
            blob = fh.read()
    boundary = secrets.token_hex(16).encode()
    body = (b"--" + boundary + b'\r\nContent-Disposition: form-data; name="file"; filename="image"\r\n'
            b"Content-Type: application/octet-stream\r\n\r\n" + blob + b"\r\n")
    target = spec.get("target") or [spec["src"][0] * spec["factor"], spec["src"][1] * spec["factor"]]
    # keep_larger: when the model passes overshoot the target, the worker hands
    # the larger picture back instead of shrinking it (within max_px / max_scale).
    for name, value in (("model", spec["model"]), ("factor", str(spec["factor"])),
                        ("width", str(target[0])), ("height", str(target[1])),
                        ("quality", str(cfg.get("quality") or 92)), ("keep_larger", "1"),
                        ("max_px", str(spec.get("max_px") or 0)), ("max_scale", str(spec.get("max_scale") or 0)),
                        ("grain", "1" if spec.get("grain") else "0"),
                        ("grain_hint", "1" if spec.get("grain_hint") else "0")):
        body += (b"--" + boundary + b'\r\nContent-Disposition: form-data; name="' + name.encode()
                 + b'"\r\n\r\n' + value.encode() + b"\r\n")
    body += b"--" + boundary + b"--\r\n"
    req = urllib.request.Request(base.rstrip("/") + "/v1/upscale", data=body, method="POST")
    req.add_header("Content-Type", "multipart/form-data; boundary=" + boundary.decode())
    if token:
        req.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(req, timeout=600) as r:
            data = r.read()
            ctype = (r.headers.get("Content-Type") or "image/webp").split(";")[0]
            size = r.headers.get("X-Upscale-Size") or "0x0"
            seconds = float(r.headers.get("X-Upscale-Seconds") or 0)
            grain_info = parse_grain_header(r.headers.get("X-Upscale-Grain"))
    except urllib.error.HTTPError as exc:
        # What the worker said, as words for the details panel - not its JSON.
        body = exc.read()[:1000].decode("utf-8", "replace")
        try:
            detail = json.loads(body) or {}
        except ValueError:
            detail = {}
        if exc.code == 413 and detail.get("max_output_px"):
            raise UpscaleLimitError(detail.get("error") or "over the worker's limit", int(detail["max_output_px"]))
        raise RuntimeError(f"the vision worker refused it (HTTP {exc.code}): "
                           f"{detail.get('error') or body[:200] or exc.reason}")
    except TimeoutError:
        raise RuntimeError("the vision worker did not answer within 10 minutes")
    except urllib.error.URLError as exc:
        raise RuntimeError(f"the vision worker could not be reached ({exc.reason})")
    ext = {"image/webp": ".webp", "image/png": ".png", "image/jpeg": ".jpg"}.get(ctype, ".webp")
    name = spec["key"] + ext
    target = os.path.join(upscale_cache_dir(), name)
    with open(target + ".part", "wb") as fh:
        fh.write(data)
    os.replace(target + ".part", target)
    out_w, _, out_h = size.partition("x")
    now = time.time()
    return {"root": spec["root"], "path": spec["path"], "factor": spec["factor"],
            "model": spec["model"], "reason": spec["reason"], "file": name, "bytes": len(data),
            "src": spec["src"], "out": [int(out_w or 0), int(out_h or 0)], "limited": spec.get("limited"),
            # The scale actually served, which is more than planned when a pass overshot.
            "scale": (round(int(out_w) / spec["src"][0], 2) if out_w and spec["src"][0] else spec.get("scale")),
            "seconds": round(seconds, 2), "created": now, "last_viewed": now,
            # Asked, whatever the worker answered: that is what the cache
            # lookup matches on (a worker without grain.py sends no answer).
            **({"grain_asked": True, "grain_version": spec.get("grain_version") or 1} if spec.get("grain") else {}),
            **({"grain": grain_info} if grain_info else {}),
            **({"version": spec["version"]} if spec.get("version") else {}),
            **({"cover": True} if spec.get("cover") else {}),
            **({"soft": spec["soft"]} if spec.get("soft") else {})}


def _upscale_worker_loop():
    while True:
        with _upscale_cv:
            while not _upscale_queue:
                _upscale_cv.wait()
            key = _upscale_queue.pop()               # newest first
            job = _upscale_jobs.get(key)
            if not job or job["status"] != "queued":
                continue
            job["status"], job["at"] = "working", time.time()
            spec = job["spec"]
        try:
            entry = _run_upscale(spec)
            with _upscale_cv:
                _upscale_entries()[key] = entry
                _save_upscale_index()
                _upscale_jobs.pop(key, None)
        except UpscaleLimitError as exc:
            # The worker's ceiling is lower than what was planned with (it changed
            # since it was read): remember it and drop the job, so the next poll
            # plans within it at once instead of showing a failure for ten minutes.
            print(f"[Info] live upscale of {spec['path']} re-planned: {exc}", flush=True)
            note_worker_upscale_limit(exc.max_output_px)
            with _upscale_cv:
                _upscale_jobs.pop(key, None)
        except Exception as exc:
            print(f"[Warning] live upscale failed for {spec['path']}: {exc}", flush=True)
            with _upscale_cv:
                _upscale_jobs[key] = {"status": "failed", "error": str(exc)[:300],
                                      "at": time.time(), "spec": spec}


def upscale_evict_once():
    """Drops what nobody has looked at for cache_days, then the least recently
    viewed until the cache fits cache_max_gb. Returns how many went."""
    cfg = upscale_settings()
    cache = upscale_cache_dir()
    now = time.time()
    max_age = float(cfg.get("cache_days") or 7) * 86400
    max_bytes = float(cfg.get("cache_max_gb") or 20) * 1024 ** 3
    cap = cover_size_cap(cfg)
    removed = 0
    with _upscale_cv:
        entries = _upscale_entries()
        for key, entry in list(entries.items()):
            path = os.path.join(cache, entry.get("file") or "")
            # A cover's copy over "Largest song cover" is never shown again
            # (cached_upscale_of), so it only takes room.
            if (now - float(entry.get("last_viewed") or 0) > max_age or not os.path.isfile(path)
                    or _over_cover_cap(entry, cap)):
                try:
                    os.remove(path)
                except OSError:
                    pass
                entries.pop(key, None)
                removed += 1
        total = sum(int(e.get("bytes") or 0) for e in entries.values())
        for key, entry in sorted(entries.items(), key=lambda kv: kv[1].get("last_viewed") or 0):
            if total <= max_bytes:
                break
            try:
                os.remove(os.path.join(cache, entry["file"]))
            except OSError:
                pass
            total -= int(entry.get("bytes") or 0)
            entries.pop(key, None)
            removed += 1
        if removed or _upscale_index["dirty"]:
            _save_upscale_index()
        known = {e.get("file") for e in entries.values()}
    # A file with no index entry: a crash between writing it and the index.
    for name in os.listdir(cache):
        full = os.path.join(cache, name)
        if name.startswith("index.json") or name in known:
            continue
        try:
            if now - os.path.getmtime(full) > 86400:
                os.remove(full)
                removed += 1
        except OSError:
            pass
    return removed


def _upscale_maintenance_loop():
    """Saves the last-viewed clock every five minutes, evicts hourly."""
    ticks = 0
    while True:
        try:
            if ticks % 12 == 0:
                gone = upscale_evict_once()
                if gone:
                    print(f"[Info] live upscale cache: evicted {gone}", flush=True)
            elif _upscale_index["dirty"]:
                with _upscale_cv:
                    _save_upscale_index()
        except Exception as exc:
            print(f"[Warning] live upscale maintenance: {exc}", flush=True)
        ticks += 1
        time.sleep(300)


@app.route("/api/media/upscale/config", methods=["GET"])
def media_upscale_config():
    """What the preview needs to decide when to ask."""
    if not get_authenticated_user():
        return jsonify({"error": "Unauthorized"}), 401
    cfg = upscale_settings()
    # The labels name the model in the details panel and on the chip.
    return jsonify({**{k: cfg.get(k) for k in ("enabled", "view_seconds", "zoom_factor")},
                    # Which of the photo models "set to photograph" means today.
                    "photo_model": photo_upscale_model(cfg),
                    "model_labels": UPSCALE_MODEL_LABELS})


@app.route("/api/media/upscale/model", methods=["POST"])
def media_upscale_model():
    """Body {root, path, model: "photo" | "anime" | null}: the model for ONE
    file when the rules got it wrong, null to go back to the rules. The page
    then asks for the upscale again; the copy made under the old model stays
    cached, so going back is instant.

    Anyone who may see the library may set it - the same reach as the rest of
    the upscale API - and who set it is kept, because it is one shared answer
    for everybody rather than a per-account preference like a favourite."""
    user = get_authenticated_user()
    if not user:
        return jsonify({"error": "Unauthorized"}), 401
    data = request.get_json(silent=True) or {}
    root_id = (data.get("root") or DEFAULT_ROOT_ID).strip()
    rel_path = (data.get("path") or "").strip("/").replace("\\", "/")
    kind = data.get("model")
    if kind is not None and kind not in UPSCALE_MODEL_KINDS:
        return jsonify({"error": 'model must be "photo", "anime" or null'}), 400
    if not rel_path or not root_visible_to(user, root_id):
        return jsonify({"error": "Forbidden"}), 403
    set_upscale_model_override(root_id, rel_path, kind, user["username"])
    model = None if kind is None else ("anime" if kind == "anime" else photo_upscale_model(upscale_settings()))
    print(f"upscale model for {root_id}/{rel_path} set to {kind or 'automatic'} by {user['username']}",
          flush=True)
    return jsonify({"override": kind, "model": model,
                    "label": UPSCALE_MODEL_LABELS.get(model) if model else None})


@app.route("/api/media/upscale", methods=["POST"])
def media_upscale():
    """Body: {root, path, need_w, need_h, check_only}. need_* is the pixel size
    the image must reach to stay sharp at the zoom factor on the viewer's
    screen. check_only answers from the cache without starting a job - the
    page asks that the moment an image opens, so something upscaled on an
    earlier visit is swapped in without waiting again."""
    user = get_authenticated_user()
    if not user:
        return jsonify({"error": "Unauthorized"}), 401
    data = request.get_json(silent=True) or {}
    root_id = (data.get("root") or DEFAULT_ROOT_ID).strip()
    rel_path = (data.get("path") or "").strip("/").replace("\\", "/")
    if not rel_path or not root_visible_to(user, root_id):
        return jsonify({"error": "Forbidden"}), 403
    answer, code = upscale_answer(root_id, rel_path, data, lambda key: f"/api/media/upscaled?key={key}")
    return jsonify(answer), code


def _ready_answer(key, entry, url_for):
    # The override is read fresh rather than taken from the entry: a copy made
    # before the model was set by hand is still the right copy whenever the
    # hand-picked model is the one the rules chose anyway, and the panel must
    # still show that it is set.
    #
    # The url names the copy's making, not only its key: the key stays the same
    # when a copy is made again (Clear cache, or a worker change such as film
    # grain), and the browser keeps a fetched copy for a day - it showed the
    # old grainless picture under a panel describing the new one (2026-09-22).
    return {"status": "ready", "url": f"{url_for(key)}&v={int(entry.get('created') or 0)}",
            # .get: an entry written before those keys existed still answers,
            # with no override rather than a 500 for the picture on screen.
            "override": (upscale_model_override(entry.get("root"), entry.get("path")) or {}).get("kind"),
            "factor": entry["factor"], "model": entry["model"],
            "reason": entry.get("reason"), "width": entry["out"][0],
            "height": entry["out"][1], "seconds": entry.get("seconds"),
            "created": entry.get("created"), "limited": entry.get("limited"),
            "ext": os.path.splitext(entry.get("file") or "")[1],
            "scale": entry.get("scale") or entry["factor"], "cover": bool(entry.get("cover")),
            # What the worker did about film grain, when it was asked (artwork).
            "grain": entry.get("grain"),
            # A soft cover: the size it was taken as, and why ("Soft covers").
            "soft": entry.get("soft")}


def upscale_version(st):
    """The source file as it is now: a cached copy of any other version is
    of another picture."""
    return f"{st.st_size}:{st.st_mtime_ns}"


def cached_upscale_of(root_id, rel_path, want=None, model=None, grain=None, soft=None):
    """(key, entry) of the best cached copy of this file as it is now, or None.

    A copy is looked up by the exact plan - factor and model - so the same
    picture opened at another window size, on a phone or on a screen with
    another pixel density, found nothing and waited out view_seconds while a
    copy sat in the cache (user, 2026-09-18: "Upscaled media should always be
    fetched instantly if it is already in cache"). Any copy of the same file
    version will do, whatever its factor: the smallest that covers `want`
    (the planned output size), else the largest there is.

    With a `model` - the plan named one - only copies made with that model
    count. A different model means Settings (or new tags) now want another
    picture, and serving the old one instantly would mean the new one is
    never made. Without a plan (not needed at this size, a cover not analysed
    yet) any copy counts. `grain` the same way: a plan that puts film grain back
    takes only copies made that way (and one without, only copies made
    without), or a copy from before grain.py would be served forever. A copy
    with grain also has to be of the current GRAIN_VERSION; one without the
    mark is from version 1. `soft` too (the soft_cover_height a plan shrinks
    the cover to, 0 for none): a copy upscaled from the whole cover is not the
    one a soft cover's plan wants, however large - or the 2560 x 1440 covers
    upscaled before their folder was listed would be served forever.

    A copy made since 2026-09-18 carries the version it was made from. An
    older one is accepted only if its key, recomputed with the file's CURRENT
    size and mtime and its own factor (or size, if made at a limit) and model,
    is its key - which rejects a copy of a file replaced since.
    """
    full = resolve_media_path(root_id, rel_path)
    try:
        st = os.stat(full) if full else None
    except OSError:
        st = None
    if st is None:
        return None
    version = upscale_version(st)
    # Without a plan any copy may be shown - but one made the way the settings
    # make copies now comes first. Otherwise an old copy without film grain
    # and the new one with it tie on size, the older wins, and a cover looked
    # at from the cache kept showing the grainless one (user, 2026-09-22: "at
    # least for song covers, the upscale is not re-done").
    cfg = upscale_settings()
    prefer_grain = bool(cfg.get("restore_grain", True)) if grain is None else None
    # A cover's copy larger than "Largest song cover" is never shown: it is made
    # again within it (and the cache sweep drops it, upscale_evict_once).
    cap = cover_size_cap(cfg)
    best = None
    with _upscale_cv:
        for key, e in _upscale_entries().items():
            if e.get("root") != root_id or e.get("path") != rel_path:
                continue
            if _over_cover_cap(e, cap):
                continue
            if model and e.get("model") != model:
                continue
            with_grain = bool(e.get("grain_asked"))
            grain_now = with_grain and int(e.get("grain_version") or 1) == GRAIN_VERSION
            if grain is not None and (with_grain != bool(grain) or (grain and not grain_now)):
                continue
            if soft is not None and int((e.get("soft") or {}).get("height") or 0) != soft:
                continue
            if e.get("version"):
                if e["version"] != version:
                    continue
            else:
                out = e.get("out") or [0, 0]
                # Keyed by its whole factor, or by its size when made at a limit
                # (plan_upscale) - which the copy came out at: all 58 copies in
                # the cache on 2026-09-18 matched one or the other.
                if not any(hashlib.sha256("\0".join((root_id, rel_path, str(st.st_size), str(st.st_mtime_ns),
                                                     size_key, str(e.get("model")))).encode("utf-8"))
                           .hexdigest()[:40] == key
                           for size_key in (str(e.get("factor")), f"{out[0]}x{out[1]}")):
                    continue
            out = e.get("out") or [0, 0]
            area = out[0] * out[1]
            covers = bool(want) and out[0] >= want[0] and out[1] >= want[1]
            current_way = prefer_grain is None or (grain_now if prefer_grain else not with_grain)
            rank = (covers, current_way, -area if covers else area)
            if best is None or rank > best[0]:
                best = (rank, key)
    if best is None:
        return None
    entry = _touch_upscale(best[1])
    return (best[1], entry) if entry else None


def upscale_answer(root_id, rel_path, data, url_for):
    """(answer, HTTP status) to one ask for an upscale, from the library
    (media_upscale) or a share page (public_share_upscale), once the caller has
    checked the file is theirs to see: plan it, answer from the cache, or queue
    it. url_for(key) is where that page fetches a finished copy."""
    try:
        need_w = max(1, int(data.get("need_w") or 0))
        need_h = max(1, int(data.get("need_h") or 0))
    except (TypeError, ValueError):
        return {"error": "need_w and need_h must be numbers"}, 400

    spec, status = plan_upscale(root_id, rel_path, need_w, need_h, analyse=not data.get("check_only"))
    if status:
        # Not planned this time - no need at this size, over a limit, a cover
        # not analysed yet - but a copy already made is still shown at once.
        # Off, missing and unsupported mean no copy at all. So is a cover not
        # measured yet ("Soft covers, found by measuring"): a copy made before
        # every cover was measured may be one upscaled from the soft full size,
        # and once shown the page never asks - the cover would never be
        # measured. The page asks at once for a song; that ask measures, and a
        # cover found sharp gets its old copy back from the cache.
        if status.pop("unmeasured", False):
            return status, 200
        if status.get("status") not in ("disabled", "missing", "unsupported"):
            hit = cached_upscale_of(root_id, rel_path)
            if hit:
                return _ready_answer(hit[0], hit[1], url_for), 200
        return status, 200
    key = spec["key"]
    entry = _touch_upscale(key)
    if entry:
        return _ready_answer(key, entry, url_for), 200
    hit = cached_upscale_of(root_id, rel_path, want=spec.get("target"), model=spec.get("model"),
                            grain=spec.get("grain"), soft=(spec.get("soft") or {}).get("height") or 0)
    if hit:
        return _ready_answer(hit[0], hit[1], url_for), 200
    with _upscale_cv:
        job = _upscale_jobs.get(key)
        if job and job["status"] == "failed" and time.time() - job["at"] < 600:
            return {"status": "failed", "error": job["error"]}, 200
        if data.get("check_only"):
            return {"status": job["status"] if job else "none", "override": spec.get("override"),
                    "factor": spec["factor"], "model": spec["model"]}, 200
        if not job or job["status"] == "failed":
            job = {"status": "queued", "error": None, "at": time.time(), "spec": spec}
            _upscale_jobs[key] = job
            _upscale_queue.append(key)
            while len(_upscale_queue) > UPSCALE_QUEUE_MAX:
                dropped = _upscale_queue.pop(0)
                if (_upscale_jobs.get(dropped) or {}).get("status") == "queued":
                    _upscale_jobs.pop(dropped, None)
            _upscale_cv.notify()
        elif job["status"] == "queued" and key in _upscale_queue:
            _upscale_queue.remove(key)             # asked again: to the front
            _upscale_queue.append(key)
    return {"status": job["status"], "factor": spec["factor"], "scale": spec.get("scale"),
            "override": spec.get("override"),
            "model": spec["model"], "reason": spec["reason"]}, 200


@app.route("/api/media/upscaled", methods=["GET"])
def media_upscaled_file():
    user = get_authenticated_user()
    if not user:
        return jsonify({"error": "Unauthorized"}), 401
    key = (request.args.get("key") or "").strip()
    if not re.fullmatch(r"[0-9a-f]{40}", key):
        return jsonify({"error": "bad key"}), 400
    entry = _touch_upscale(key)
    if not entry:
        return jsonify({"error": "Not found"}), 404
    if not root_visible_to(user, entry["root"]):
        return jsonify({"error": "Forbidden"}), 403
    path = os.path.join(upscale_cache_dir(), entry["file"])
    if not os.path.isfile(path):
        return jsonify({"error": "Not found"}), 404
    # ?download=1 is the menu's "Download upscaled", named after the original.
    as_attachment = request.args.get("download") in ("1", "true")
    name = None
    if as_attachment:
        base = os.path.splitext(os.path.basename(entry["path"]))[0] + (".cover" if entry.get("cover") else "")
        label = f"{float(entry.get('scale') or entry['factor']):.2f}".rstrip("0").rstrip(".")
        name = f"{base}.upscaled-{label}x{os.path.splitext(entry['file'])[1]}"
    resp = send_file(path, conditional=True, as_attachment=as_attachment, download_name=name)
    resp.headers["Cache-Control"] = "private, max-age=86400"
    return resp


@app.route("/api/media/upscale/stats", methods=["GET"])
def media_upscale_stats():
    user = get_authenticated_user()
    if not user:
        return jsonify({"error": "Unauthorized"}), 401
    if not is_admin(user):
        return jsonify({"error": "Forbidden"}), 403
    with _upscale_cv:
        entries = list(_upscale_entries().values())
        jobs = [{"path": j["spec"]["path"], "status": j["status"], "model": j["spec"]["model"],
                 "factor": j["spec"]["factor"], "error": j.get("error")} for j in _upscale_jobs.values()]
    models = {}
    for e in entries:
        models[e.get("model")] = models.get(e.get("model"), 0) + 1
    seconds = [e.get("seconds") or 0 for e in entries]
    return jsonify({
        "cache_dir": upscale_cache_dir(), "entries": len(entries),
        "bytes": sum(int(e.get("bytes") or 0) for e in entries), "models": models,
        "avg_seconds": round(sum(seconds) / len(seconds), 2) if seconds else None,
        "jobs": jobs, "settings": upscale_settings(),
        "worker_max_output_mp": round((worker_upscale_limit_px() or 0) / 1e6) or None,
    })


@app.route("/api/media/upscale/clear", methods=["POST"])
@require_admin
def media_upscale_clear():
    """Empties the live-upscale cache (settings page)."""
    cache = upscale_cache_dir()
    removed = 0
    with _upscale_cv:
        entries = _upscale_entries()
        for entry in entries.values():
            try:
                os.remove(os.path.join(cache, entry.get("file") or ""))
                removed += 1
            except OSError:
                pass
        entries.clear()
        _save_upscale_index()
    return jsonify({"removed": removed})


# -------------------------------------------------------------------
# Frame interpolation in the video player
#
# The vision worker generates the in-between frames with Lossless Scaling
# frame generation (vision/vidinterp/vidinterp-gpu) and streams fragmented
# MP4. It cannot reach this host, so the video is uploaded to it once, keyed by
# root, path, size and mtime; every later stream (a seek is a new stream from
# that position) reuses the upload. The player asks for the screen's refresh
# rate as the output rate (?o=) and the bandwidth cap from its own setting.
# Output at exactly the screen's rate, not a whole multiple of the video's: a
# 25 fps video at 4x = 100 fps on a 120 Hz screen shows every fifth frame twice
# (judder), at 120 fps (4.8x) every frame equally long. Browsers composite at
# the screen's full rate, so a variable-refresh screen never comes down to the
# video's rate - the rate has to be matched here (KSF-029).
# -------------------------------------------------------------------

def interp_settings():
    merged = json.loads(json.dumps(DEFAULT_CONFIG["interpolation"]))
    merged.update(CONFIG.get("interpolation") or {})
    return merged


_interp_worker = {"at": 0.0, "status": None}
_interp_upload_locks = {}
_interp_upload_locks_guard = threading.Lock()


def _interp_worker_request(path, method="GET", data=None, headers=None):
    base, token = vision_worker_address()
    req = urllib.request.Request(base.rstrip("/") + path, data=data, method=method)
    if token:
        req.add_header("Authorization", "Bearer " + token)
    for name, value in (headers or {}).items():
        req.add_header(name, value)
    return req


def interp_worker_status(key=None):
    """The worker's /v1/interp/status (cached a minute unless a key is asked
    about); None when the worker cannot be reached."""
    if key is None and _interp_worker["status"] is not None and time.time() - _interp_worker["at"] < 60:
        return _interp_worker["status"]
    try:
        with urllib.request.urlopen(_interp_worker_request(
                "/v1/interp/status" + (f"?key={key}" if key else "")), timeout=5) as r:
            status = json.load(r)
    except (OSError, ValueError):
        status = None
    if key is None:
        _interp_worker.update(at=time.time(), status=status)
    return status


def interp_ensure_uploaded(key, file_path):
    """Uploads the video to the worker unless it already has it. One upload per
    key at a time: a seek right after opening must not send the file twice."""
    with _interp_upload_locks_guard:
        lock = _interp_upload_locks.setdefault(key, threading.Lock())
    with lock:
        status = interp_worker_status(key)
        if status is None:
            raise RuntimeError("the vision worker could not be reached")
        if status.get("cached"):
            return
        size = os.path.getsize(file_path)
        with open(file_path, "rb") as fh:
            req = _interp_worker_request(f"/v1/interp/source/{key}", method="PUT", data=fh,
                                         headers={"Content-Length": str(size),
                                                  "Content-Type": "application/octet-stream"})
            with urllib.request.urlopen(req, timeout=900) as r:
                r.read()


INTERP_MAX_RATIO = 16      # output / video frame rate
INTERP_MIN_RATIO = 1.1     # below this the original plays: in-between frames would barely move
INTERP_OUT_FPS = (10.0, 480.0)


def interp_output_rate(cfg, fps, args):
    """The output rate a player asked for: ?o=<frames a second> (the screen's
    rate), or the older ?m=<multiplier> of a player loaded before exact rates.
    -> (variant prefix, worker parameter, output fps). The variant is m<k> when
    the rate is a whole 2-8 times the video's - the name the segment cache had
    before, so what was made then still plays - else f<rate>. ValueError: a
    bad parameter; LookupError: no rate is left for this video."""
    max_out = float(cfg.get("max_output_fps") or 240)
    if args.get("o"):
        out = float(args.get("o"))
        if not math.isfinite(out):
            raise ValueError("bad o")
        # The server's frame-rate ceiling wins over what the screen asked for.
        out = round(min(out, max_out, INTERP_OUT_FPS[1], fps * INTERP_MAX_RATIO if fps else math.inf), 3)
        if out < INTERP_OUT_FPS[0] or (fps and out < fps * INTERP_MIN_RATIO):
            raise LookupError("this video is already at the highest frame rate allowed")
        k = round(out / fps) if fps else 0
        if fps and 2 <= k <= 8 and abs(out - fps * k) < 1e-6 * out:
            return f"m{k}", f"mult={k}", out
        return f"f{out:g}", f"fps={out:g}", out
    mult = int(args.get("m") or 2)
    if fps:
        mult = min(mult, int(max_out // fps))
    if mult < 2:
        raise LookupError("this video is already at the highest frame rate allowed")
    mult = min(mult, 8)
    return f"m{mult}", f"mult={mult}", (fps * mult if fps else 0)


@app.route("/api/media/interp/config", methods=["GET"])
def media_interp_config():
    """What the player's settings menu offers, and whether it can work now."""
    if not get_authenticated_user():
        return jsonify({"error": "Unauthorized"}), 401
    cfg = interp_settings()
    status = interp_worker_status() if cfg.get("enabled") else None
    if not cfg.get("enabled"):
        reason = "turned off on the server"
    elif status is None:
        reason = "the vision worker cannot be reached"
    elif not status.get("available"):
        reason = "not installed on the vision worker (" + ", ".join(status.get("missing") or []) + ")"
    else:
        reason = ""
    return jsonify({"enabled": bool(cfg.get("enabled")), "available": not reason, "reason": reason,
                    "bitrates_mbps": cfg.get("bitrates_mbps") or [0],
                    "default_mbps": cfg.get("default_mbps", 20),
                    "max_output_fps": cfg.get("max_output_fps") or 240,
                    "engine": "Lossless Scaling frame generation (LSFG 3.1)"})


@app.route("/api/media/interp/stream", methods=["GET"])
def media_interp_stream():
    """?root=&path=&t=<start seconds>&o=<output fps>&mbps=<cap, 0 = none>&loop=1
    (or m=<multiplier> instead of o=).
    Fragmented MP4 from t on; the player makes a new request to seek. With
    loop=1 the stream never ends: it wraps to the start, as the player loops."""
    user = get_authenticated_user()
    if not user:
        return "401 Unauthorized", 401
    cfg = interp_settings()
    if not cfg.get("enabled"):
        return jsonify({"error": "frame interpolation is turned off on the server"}), 403
    root_id = request.args.get("root", "").strip() or DEFAULT_ROOT_ID
    if not root_visible_to(user, root_id):
        return "403 Forbidden", 403
    rel_path = (request.args.get("path") or "").strip("/").replace("\\", "/")
    file_path = resolve_media_path(root_id, rel_path)
    if not file_path or not os.path.isfile(file_path):
        return "404 Not Found", 404
    if classify_media_path(file_path) != "video":
        return jsonify({"error": "not a video"}), 400
    fps = probe_media_details(file_path).get("fps") or 0
    try:
        start = max(0.0, float(request.args.get("t") or 0))
        mbps = max(0.0, min(1000.0, float(request.args.get("mbps") or 0)))
        _, rate_param, out_fps = interp_output_rate(cfg, fps, request.args)
    except ValueError:
        return jsonify({"error": "bad parameters"}), 400
    except LookupError as exc:
        return jsonify({"error": str(exc)}), 400

    stat = os.stat(file_path)
    key = hashlib.sha256(f"{root_id}\n{rel_path}\n{stat.st_size}\n{int(stat.st_mtime)}".encode()).hexdigest()[:32]
    try:
        interp_ensure_uploaded(key, file_path)
        upstream = urllib.request.urlopen(_interp_worker_request(
            f"/v1/interp/stream/{key}?start={start:.3f}&{rate_param}&mbps={mbps:g}"
            f"&max_height={int(cfg.get('max_height') or 2160)}"
            f"&loop={'1' if request.args.get('loop') == '1' else '0'}"), timeout=60)
    except urllib.error.HTTPError as exc:
        body = exc.read()[:1000].decode("utf-8", "replace")
        try:
            detail = json.loads(body) or {}
        except ValueError:
            detail = {}
        return jsonify({"error": detail.get("error") or f"the vision worker refused (HTTP {exc.code})",
                        "busy": bool(detail.get("busy"))}), (503 if exc.code == 503 else 502)
    except (OSError, RuntimeError) as exc:
        return jsonify({"error": f"frame interpolation failed: {exc}"}), 502

    def relay():
        # Closing the upstream when the browser goes away (a seek, the next
        # file, a closed tab) is what stops the encoder on the worker.
        try:
            while True:
                chunk = upstream.read(1 << 16)
                if not chunk:
                    break
                yield chunk
        finally:
            upstream.close()

    return Response(relay(), mimetype="video/mp4",
                    headers={"Cache-Control": "no-store", "X-Interp-Fps": f"{out_fps:g}",
                             "X-Accel-Buffering": "no"})


# The player's segment requests come every second or two; probing the file's
# frame rate each time would cost an ffprobe per request. (path, mtime) -> fps.
_interp_fps_cache = {}
INTERP_SEG_NAME = re.compile(r"^(init\.mp4|meta\.json|end\.json|seg_\d{5}\.m4s)$")


@app.route("/api/media/interp/seg", methods=["GET"])
def media_interp_segment():
    """?root=&path=&o=&mbps=&name=<init.mp4|meta.json|end.json|seg_NNNNN.m4s>[&from=<segment>]
    (o = output fps; or m = multiplier).
    One file of the vision worker's cache of interpolated segments (Smooth motion
    in the player, js/interp.js). Segments already made come at once; one being
    made is waited for by the worker, which answers 503 + Retry-After when it
    takes longer. The source is uploaded on first use, and again if the worker
    has evicted it."""
    user = get_authenticated_user()
    if not user:
        return "401 Unauthorized", 401
    cfg = interp_settings()
    if not cfg.get("enabled"):
        return jsonify({"error": "frame interpolation is turned off on the server"}), 403
    root_id = request.args.get("root", "").strip() or DEFAULT_ROOT_ID
    if not root_visible_to(user, root_id):
        return "403 Forbidden", 403
    rel_path = (request.args.get("path") or "").strip("/").replace("\\", "/")
    file_path = resolve_media_path(root_id, rel_path)
    if not file_path or not os.path.isfile(file_path):
        return "404 Not Found", 404
    name = request.args.get("name") or ""
    if not INTERP_SEG_NAME.match(name):
        return jsonify({"error": "bad name"}), 400
    try:
        mbps = max(0.0, min(1000.0, float(request.args.get("mbps") or 0)))
        from_seg = max(0, int(request.args.get("from") or 0))
    except ValueError:
        return jsonify({"error": "bad parameters"}), 400
    stat = os.stat(file_path)
    cache_key = (file_path, stat.st_mtime)
    if cache_key not in _interp_fps_cache:
        if classify_media_path(file_path) != "video":
            return jsonify({"error": "not a video"}), 400
        _interp_fps_cache[cache_key] = probe_media_details(file_path).get("fps") or 0
    fps = _interp_fps_cache[cache_key]
    try:
        rate_variant, _, _ = interp_output_rate(cfg, fps, request.args)
    except ValueError:
        return jsonify({"error": "bad parameters"}), 400
    except LookupError as exc:
        return jsonify({"error": str(exc)}), 400
    key = hashlib.sha256(f"{root_id}\n{rel_path}\n{stat.st_size}\n{int(stat.st_mtime)}".encode()).hexdigest()[:32]
    variant = f"{rate_variant}_h{int(cfg.get('max_height') or 2160)}_r{mbps:g}"
    path = f"/v1/interp/seg/{key}/{variant}/{name}" + (f"?from={from_seg}" if name == "meta.json" else "")

    try:
        if name == "meta.json":
            interp_ensure_uploaded(key, file_path)
        try:
            data, ctype = _interp_seg_fetch_raw(path)
        except urllib.error.HTTPError as exc:
            if exc.code != 404 or not exc.upload:
                raise
            interp_ensure_uploaded(key, file_path)      # evicted on the worker: send it again
            data, ctype = _interp_seg_fetch_raw(path)
    except urllib.error.HTTPError as exc:
        headers = {"Retry-After": "1"} if exc.code == 503 else {}
        return jsonify({"error": str(exc.reason), "pending": exc.code == 503}), exc.code, headers
    except (OSError, RuntimeError) as exc:
        return jsonify({"error": f"frame interpolation failed: {exc}"}), 502
    return Response(data, mimetype=ctype.split(";")[0], headers={
        # Segments never change once made; meta.json can still become end.json-exact.
        "Cache-Control": "no-store" if name.endswith(".json") else "private, max-age=86400",
    })


def _interp_seg_fetch_raw(path):
    """One raw request against the worker's segment cache (`path` = the
    /v1/interp/seg/... URL). -> (bytes, content-type). On an error response,
    raises urllib.error.HTTPError with .reason replaced by the worker's own
    JSON error message (when it sent one) and an .upload attribute mirroring
    its "upload": true (the source was evicted and has to be sent again)."""
    try:
        with urllib.request.urlopen(_interp_worker_request(path), timeout=45) as r:
            return r.read(), r.headers.get("Content-Type") or "application/octet-stream"
    except urllib.error.HTTPError as exc:
        try:
            detail = json.loads(exc.read()[:1000].decode("utf-8", "replace")) or {}
        except ValueError:
            detail = {}
        wrapped = urllib.error.HTTPError(exc.url, exc.code, detail.get("error") or exc.reason,
                                         exc.headers, None)
        wrapped.upload = bool(detail.get("upload"))
        raise wrapped from None


def _interp_seg_fetch_wait(path, key, file_path, deadline):
    """Like _interp_seg_fetch_raw, but for a caller that (unlike the player)
    cannot itself retry: waits out a "still being made" 503 up to `deadline`
    (a time.time() value - the worker answers 503 only after its own
    SEG_WAIT_S, so this rarely loops more than once or twice), and re-uploads
    the source once if the worker answers 404 "upload": true (evicted).
    Raises urllib.error.HTTPError on a 503 past the deadline or any other
    error status."""
    uploaded = False
    while True:
        try:
            return _interp_seg_fetch_raw(path)
        except urllib.error.HTTPError as exc:
            if exc.code == 404 and exc.upload and not uploaded:
                interp_ensure_uploaded(key, file_path)
                uploaded = True
                continue
            if exc.code == 503 and time.time() < deadline:
                time.sleep(0.5)
                continue
            raise


INTERP_DOWNLOAD_SEG_TIMEOUT_S = 120   # per segment, since the last one arrived


def content_disposition_header(filename, as_attachment=True):
    """Content-Disposition for a filename that may hold non-ASCII characters
    (see send_media_file's own comment above for why this has to be
    `attachment`, not the <a download> attribute alone, when download=1 is
    asked for). filename* is RFC 5987 (what Flask's send_file also emits for
    a non-ASCII download_name); filename is an ASCII fallback for a client
    that does not understand it."""
    disposition = "attachment" if as_attachment else "inline"
    ascii_name = filename.encode("ascii", "replace").decode("ascii").replace('"', "'")
    return f'{disposition}; filename="{ascii_name}"; filename*=UTF-8\'\'{quote(filename, safe="")}'


def interp_open_rendition(key, variant, file_path):
    """Uploads the source if the worker lacks it and asks for the rendition the way the player
    does (meta.json?from=0, then init.mp4), waiting out "still being made". -> (the segment count
    meta.json estimates, init.mp4's bytes). Raises what _interp_seg_fetch_wait raises."""
    interp_ensure_uploaded(key, file_path)
    meta_raw, _ = _interp_seg_fetch_wait(
        f"/v1/interp/seg/{key}/{variant}/meta.json?from=0", key, file_path,
        time.time() + INTERP_DOWNLOAD_SEG_TIMEOUT_S)
    total = int(json.loads(meta_raw)["segments"])   # an estimate until end.json exists
    init_data, _ = _interp_seg_fetch_wait(
        f"/v1/interp/seg/{key}/{variant}/init.mp4", key, file_path,
        time.time() + INTERP_DOWNLOAD_SEG_TIMEOUT_S)
    return total, init_data


def interp_segment_stream(key, variant, file_path, rel_path, total, limit=None, strict=False,
                          should_stop=None):
    """Yields the bytes of segment 0, 1, ... of a rendition, each generated the way the player
    would ask for it. `total` is meta.json's estimate, which can be one off EITHER way: ending at
    it would silently drop the last segment when it runs short, so where it runs out (or a segment
    is refused) the loop asks end.json.
    limit: stop after that many segments (a partial rendition made again after a cut).
    strict: where the download only prints and ends the stream, raise (the redo job then backs off).
    should_stop(): checked before each segment; ends the stream early."""
    def exact_total(i):
        """The real segment count from end.json, which the worker writes once a
        run reaches the end; None if it does not exist within the timeout.
        ?from=i: a request that finds nothing sets the worker's cursor, and
        without it that would be segment 0 - dragging the job back to the start."""
        deadline = time.time() + INTERP_DOWNLOAD_SEG_TIMEOUT_S
        while True:
            try:
                raw, _ = _interp_seg_fetch_raw(f"/v1/interp/seg/{key}/{variant}/end.json?from={i}")
                return int(json.loads(raw)["last_segment"]) + 1
            except urllib.error.HTTPError as exc:
                if exc.code != 404 or exc.upload or time.time() > deadline:
                    return None
            time.sleep(0.5)

    n, exact = total, False
    i = 0
    while True:
        if limit is not None and i >= limit:
            return
        if should_stop is not None and should_stop():
            return
        if i >= n:
            if exact:
                return
            found = exact_total(i)
            if found is None:
                print(f"[interp-download] {rel_path}: no end.json after segment {i}", flush=True)
                if strict:
                    raise RuntimeError(f"no end.json after segment {i}")
                return
            n, exact = found, True
            continue
        seg_path = f"/v1/interp/seg/{key}/{variant}/seg_{i:05d}.m4s"
        try:
            data, _ = _interp_seg_fetch_wait(seg_path, key, file_path,
                                             time.time() + INTERP_DOWNLOAD_SEG_TIMEOUT_S)
        except urllib.error.HTTPError as exc:
            if exc.code == 404 and not exact:
                n = i          # past the estimate's real end: settle it with end.json
                continue
            print(f"[interp-download] {rel_path}: stopped at segment {i}/{n} ({exc.reason})",
                  flush=True)
            if strict:
                raise
            return
        except (OSError, RuntimeError) as exc:
            print(f"[interp-download] {rel_path}: stopped at segment {i}/{n} ({exc})", flush=True)
            if strict:
                raise
            return
        yield data
        i += 1


@app.route("/api/media/interp/download", methods=["GET"])
def media_interp_download():
    """?root=&path=&o=&mbps=[&download=1] (o = output fps; or m = multiplier
    - same meaning as /api/media/interp/seg's).
    The whole Smooth motion rendition as one file: init.mp4 followed by every
    segment of the worker's segment cache, in order - concatenated that way
    they are one valid fragmented MP4 (contiguous timestamps, see "Frame
    interpolation in the video player" above). A segment not made yet is
    generated the way the player would (meta.json with ?from=0), waiting out
    a 503 instead of leaving that to the browser (js/interp.js's own
    fetchSegmentFile can't be reused here - this runs on the server).
    (Context menu "Download smooth motion", user request 2026-09-27: "Allow me
    to download the upscaled videos from the right-click menu" - there is no
    real video upscaling here, this is what the player calls Smooth motion.)
    """
    user = get_authenticated_user()
    if not user:
        return "401 Unauthorized", 401
    cfg = interp_settings()
    if not cfg.get("enabled"):
        return jsonify({"error": "frame interpolation is turned off on the server"}), 403
    root_id = request.args.get("root", "").strip() or DEFAULT_ROOT_ID
    if not root_visible_to(user, root_id):
        return "403 Forbidden", 403
    rel_path = (request.args.get("path") or "").strip("/").replace("\\", "/")
    file_path = resolve_media_path(root_id, rel_path)
    if not file_path or not os.path.isfile(file_path):
        return "404 Not Found", 404
    if classify_media_path(file_path) != "video":
        return jsonify({"error": "not a video"}), 400
    fps = probe_media_details(file_path).get("fps") or 0
    try:
        mbps = max(0.0, min(1000.0, float(request.args.get("mbps") or 0)))
        rate_variant, _, out_fps = interp_output_rate(cfg, fps, request.args)
    except ValueError:
        return jsonify({"error": "bad parameters"}), 400
    except LookupError as exc:
        return jsonify({"error": str(exc)}), 400

    stat = os.stat(file_path)
    key = hashlib.sha256(f"{root_id}\n{rel_path}\n{stat.st_size}\n{int(stat.st_mtime)}".encode()).hexdigest()[:32]
    variant = f"{rate_variant}_h{int(cfg.get('max_height') or 2160)}_r{mbps:g}"

    # Everything that can fail outright (the worker unreachable, no room to even
    # start) happens before the response starts, so it can still be a JSON error;
    # once streaming begins only the connection itself can say a segment failed.
    try:
        total, init_data = interp_open_rendition(key, variant, file_path)
    except urllib.error.HTTPError as exc:
        headers = {"Retry-After": "1"} if exc.code == 503 else {}
        return jsonify({"error": str(exc.reason), "pending": exc.code == 503}), exc.code, headers
    except (OSError, RuntimeError, ValueError, KeyError) as exc:
        return jsonify({"error": f"frame interpolation failed: {exc}"}), 502

    def stream():
        yield init_data
        yield from interp_segment_stream(key, variant, file_path, rel_path, total)

    stem = os.path.splitext(os.path.basename(rel_path))[0]
    name = f"{stem}.smooth-{out_fps:g}fps.mp4"
    as_attachment = request.args.get("download") in ("1", "true")
    return Response(stream(), mimetype="video/mp4", headers={
        "Cache-Control": "no-store", "X-Accel-Buffering": "no",
        "Content-Disposition": content_disposition_header(name, as_attachment),
    })


# -------------------------------------------------------------------
# 2D -> 3D conversion (stereo3d.py): the queue, the 3D cache and /api/media/3d/*
# -------------------------------------------------------------------
# Nothing converts by itself: "Convert to 3D" (admins) and, later, favourites put
# files in the queue; a dispatcher thread hands them to the vision worker's
# /v1/stereo/* routes one job at a time and pulls the results into a cache of
# its own (stereo3d.cache_dir, evicted least recently watched first). The logic
# lives in stereo3d.py; this is its settings, routes and threads.

STEREO3D_QUEUE_FILE = os.path.join(BASE_DIR, "stereo3d_queue.json")
_stereo3d = {"svc": None, "admins": (0.0, frozenset())}
_stereo3d_lock = threading.Lock()


def stereo3d_settings():
    merged = json.loads(json.dumps(DEFAULT_CONFIG["stereo3d"]))
    merged.update(CONFIG.get("stereo3d") or {})
    return merged


def _stereo3d_admin_names():
    """Names of the admin accounts, read at most once a minute."""
    at, names = _stereo3d["admins"]
    if time.time() - at > 60:
        names = frozenset(u["username"] for u in load_users() if is_admin(u))
        _stereo3d["admins"] = (time.time(), names)
    return names


def stereo3d_is_favourite(root_id, rel_path):
    """True when any admin has the file favourited - the favourites that count for the 3D cache."""
    admins = _stereo3d_admin_names()
    root_id = root_id or DEFAULT_ROOT_ID
    with _favorites_lock:
        for name, roots in _favorites_data().items():
            if name in admins and rel_path in (roots.get(root_id) or {}):
                return True
    return False


_stereo3d_pin = {"entries": None, "at": 0.0, "rules": []}


def stereo3d_permanent_rules():
    """named_folder_rules of stereo3d.permanent_folders, re-read at most once a minute
    (eviction asks once per cache entry)."""
    entries = tuple(stereo3d_settings().get("permanent_folders") or ())
    if entries != _stereo3d_pin["entries"] or time.time() - _stereo3d_pin["at"] > 60:
        _stereo3d_pin.update(entries=entries, at=time.time(), rules=named_folder_rules(entries))
    return _stereo3d_pin["rules"]


def stereo3d_in_permanent_folder(root_id, rel_path):
    root_id = root_id or DEFAULT_ROOT_ID
    rel_path = (rel_path or "").strip("/")
    for rid, folder in stereo3d_permanent_rules():
        if (rid is None or rid == root_id) and (rel_path == folder or rel_path.startswith(folder + "/")):
            return True
    return False


def stereo3d_permanent_files():
    """(mtime, root, path) of every video and picture in the permanent folders; dot folders
    (the trash) are skipped."""
    out, seen = [], set()
    roots = {r["id"]: r["path"] for r in get_all_roots()}
    for rid, folder in stereo3d_permanent_rules():
        for root_id in ([rid] if rid else list(roots)):
            base = roots.get(root_id)
            start = resolve_media_path(root_id, folder) if base else None
            if not start or not os.path.isdir(start):
                continue
            for dirpath, dirs, files in os.walk(start):
                dirs[:] = [d for d in dirs if not d.startswith(".")]
                for name in files:
                    if name.startswith(".") or stereo3d.kind_of(name) is None:
                        continue
                    full = os.path.join(dirpath, name)
                    rel = os.path.relpath(full, base).replace(os.sep, "/")
                    if (root_id, rel) in seen:
                        continue
                    seen.add((root_id, rel))
                    try:
                        if os.path.getsize(full) > 0:      # not a ban tombstone
                            out.append((os.path.getmtime(full), root_id, rel))
                    except OSError:
                        pass
    return out


def stereo3d_service():
    with _stereo3d_lock:
        if _stereo3d["svc"] is None:
            # Never the app's own disk: /data2 is a thin pool that filled up once.
            # A permanent folder counts as a favourite (queued in that lane, never pulled out by an
            # unfavourite) and is pinned: eviction never touches it.
            _stereo3d["svc"] = stereo3d.Service(
                STEREO3D_QUEUE_FILE, stereo3d_settings, resolve_media_path, vision_worker_address,
                lambda r, p: stereo3d_in_permanent_folder(r, p) or stereo3d_is_favourite(r, p),
                forbidden_dirs=(BASE_DIR, "/data2"), pinned_fn=stereo3d_in_permanent_folder)
        return _stereo3d["svc"]


# --- Favourites -> 3D --------------------------------------------------------
# An ADMIN's favourite video or picture is queued in the favourites lane (behind
# every "Convert to 3D" request, newest first); removing it takes a still-waiting
# item back out unless another admin keeps it. Audio, folders and everybody else's
# favourites are ignored. Favourites that already exist are queued by a backfill
# (stereo3d.favorites_backfill_days, 7 by default, 0 = off) at start-up and when
# one of its settings changes; it is idempotent, and a file the cache evicted for
# space stays out until the cache grows (stereo3d.py, Service.backfill).

def stereo3d_favorite_changed(username, root_id, rel_path, on, favourited_at=None):
    """Called by set_favorite after its lock is released. Never raises: favouriting must
    work whatever the 3D side is doing."""
    try:
        if stereo3d.kind_of(rel_path) is None:
            return
        if username not in _stereo3d_admin_names():
            return
        svc = stereo3d_service()
        if on:
            svc.favourite_added(root_id, rel_path, username, favourited_at)
        else:
            svc.favourite_removed(root_id, rel_path)
    except Exception as exc:
        print(f"[Warning] 3D favourites hook: {exc}", flush=True)


_favorite_listeners.append(stereo3d_favorite_changed)


def stereo3d_admin_favourites():
    """(favourited at, root, path) of every favourite of every admin account."""
    admins = frozenset(u["username"] for u in load_users() if is_admin(u))
    with _favorites_lock:
        return [(ts, root, path) for name, roots in _favorites_data().items() if name in admins
                for root, paths in roots.items() for path, ts in paths.items()]


def _stereo3d_backfill_signature():
    cfg = stereo3d_settings()
    return (bool(cfg.get("enabled")), bool(cfg.get("favorites_auto")), cfg.get("favorites_backfill_days"),
            cfg.get("cache_max_gb"))


_stereo3d_backfill_wake = threading.Event()


def _stereo3d_backfill_loop():
    """Runs the backfill once after start-up and again whenever its settings change (the
    settings page wakes it; otherwise it looks every 30 seconds) - never as a tight loop."""
    time.sleep(45)
    last = None
    pin_last, pin_at = None, 0.0
    while True:
        try:
            pin_sig = (tuple(stereo3d_settings().get("permanent_folders") or ()),
                       bool(stereo3d_settings().get("enabled")))
            changed = pin_sig != pin_last
            if not pin_sig[0] and changed:
                # The last permanent folder came off the list: no scan runs, so prune the queue here
                # (user 2026-10-30: the removed Series folder's episodes still sat in the queue).
                pin_last = pin_sig
                n = stereo3d_service().drop_unpinned()
                if n:
                    print(f"[Info] stereo3d: permanent folders emptied: {n} waiting item(s) left the queue", flush=True)
            if pin_sig[0] and (changed or time.time() - pin_at > 3600):
                pin_last, pin_at = pin_sig, time.time()
                res = stereo3d_service().pin_scan(stereo3d_permanent_files())
                if res.get("status") == "ok" and (res["queued"] or changed):
                    print(f"[Info] stereo3d: permanent folders: {res['files']} files, {res['queued']} queued, "
                          f"{res['cached']} cached, {res['duplicate']} already waiting", flush=True)
        except Exception as exc:
            print(f"[Warning] 3D permanent folders: {exc}", flush=True)
        try:
            sig = _stereo3d_backfill_signature()
            if sig != last:
                res = stereo3d_service().backfill(stereo3d_admin_favourites())
                last = sig
                if res.get("status") == "ok":
                    print(f"[Info] stereo3d: favourites backfill, last {res['days']:g} days: {res['window']} files, "
                          f"{res['queued']} queued, {res['cached']} cached, {res['duplicate']} already waiting, "
                          f"{res['evicted']} evicted for space", flush=True)
        except Exception as exc:
            print(f"[Warning] 3D favourites backfill: {exc}", flush=True)
        _stereo3d_backfill_wake.wait(30)
        _stereo3d_backfill_wake.clear()


def _stereo3d_loop():
    """Ten seconds a tick while something is queued or running, a minute otherwise."""
    while True:
        delay = 60
        try:
            svc = stereo3d_service()
            svc.step()
            delay = 10 if svc.busy() else 60
        except Exception as exc:
            print(f"[Warning] 3D conversion dispatcher: {exc}", flush=True)
            delay = 30
        time.sleep(delay)


def _stereo3d_maintenance_loop():
    """Saves the watched clock every five minutes, runs the cache upkeep hourly."""
    ticks = 0
    while True:
        try:
            svc = stereo3d_service()
            if ticks % 12 == 0:
                svc.maintenance()
            else:
                svc.flush()
        except Exception as exc:
            print(f"[Warning] 3D cache maintenance: {exc}", flush=True)
        ticks += 1
        time.sleep(300)


def _stereo3d_clean_path(root_id, rel_path):
    if not isinstance(root_id, str) or not isinstance(rel_path, str):
        return None, None
    rel_path = rel_path.strip("/").replace("\\", "/")
    return (root_id.strip() or DEFAULT_ROOT_ID), (rel_path or None)


@app.route("/api/media/3d/list", methods=["GET"])
def media_3d_list():
    """Every finished conversion in a library the viewer may see, newest first - what a
    3D player needs: stream URLs for the side-by-side and the depth map (range requests)."""
    user = get_authenticated_user()
    if not user:
        return jsonify({"error": "Unauthorized"}), 401
    mine = user_favorites(user["username"])
    rows = stereo3d_service().list_entries(
        lambda root: root_visible_to(user, root),
        lambda root, path: path in mine.get(root or DEFAULT_ROOT_ID, {}))
    return jsonify({"enabled": bool(stereo3d_settings().get("enabled")), "items": rows})


@app.route("/api/media/3d/status", methods=["GET"])
def media_3d_status():
    """?root=&path= : none | queued (position) | converting (phase, progress, eta) | held | failed | done."""
    user = get_authenticated_user()
    if not user:
        return jsonify({"error": "Unauthorized"}), 401
    root_id, rel_path = _stereo3d_clean_path(request.args.get("root", ""), request.args.get("path", ""))
    if not rel_path:
        return jsonify({"error": "path is required"}), 400
    if not root_visible_to(user, root_id):
        return jsonify({"error": "Forbidden"}), 403
    return jsonify(stereo3d_service().status_for(root_id, rel_path))


_STEREO3D_VIEW_RATE_PER_MIN = 30
_stereo3d_view_hits = {}
_stereo3d_view_lock = threading.Lock()


def _stereo3d_view_retry_after(username, now=None):
    """Per-user limit of view requests per minute: 0 = allowed (and counted), else seconds to wait."""
    now = time.time() if now is None else now
    with _stereo3d_view_lock:
        hits = [t for t in _stereo3d_view_hits.get(username, ()) if now - t < 60]
        if len(hits) >= _STEREO3D_VIEW_RATE_PER_MIN:
            _stereo3d_view_hits[username] = hits
            return max(1, int(math.ceil(60 - (now - hits[0]))))
        hits.append(now)
        _stereo3d_view_hits[username] = hits
        if len(_stereo3d_view_hits) > 500:
            for name in [n for n, h in _stereo3d_view_hits.items() if not h or now - h[-1] >= 60]:
                del _stereo3d_view_hits[name]
        return 0


@app.route("/api/media/3d/view", methods=["POST"])
def media_3d_view():
    """Body {root, path, leave?}: the VR app opened (or left) a picture/video. Any signed-in role that can
    see the root. Opening one without a finished 3D version queues it in the viewing lane (ahead of manual
    and favourites); leave:true demotes it to the end of the manual lane unless it already started.
    -> the same answer as /api/media/3d/status."""
    user = get_authenticated_user()
    if not user:
        return jsonify({"error": "Unauthorized"}), 401
    cfg = stereo3d_settings()
    if not cfg.get("enabled", True):
        return jsonify({"error": "3D conversion is turned off"}), 409
    if not cfg.get("view_auto", True):
        return jsonify({"error": "Automatic 3D conversion on view is turned off"}), 403
    if cfg.get("view_auto_admin_only", False) and not is_admin(user):
        return jsonify({"error": "Forbidden"}), 403
    data = request.get_json(silent=True)
    data = data if isinstance(data, dict) else {}
    root_id, rel_path = _stereo3d_clean_path(data.get("root") or DEFAULT_ROOT_ID, data.get("path"))
    if not rel_path:
        return jsonify({"error": "path is required"}), 400
    if not root_visible_to(user, root_id):
        return jsonify({"error": "Forbidden"}), 403
    if stereo3d.kind_of(rel_path) is None:
        return jsonify({"error": "only videos and pictures can be made 3D"}), 400
    if stereo3d.is_native_3d(rel_path):
        # Already stereo: a 2D -> 3D conversion would only cost GPU hours on both eyes as one picture.
        status = stereo3d_service().status_for(root_id, rel_path)
        return jsonify(status if status.get("state") == "done" else {"state": "unsupported"})
    wait = _stereo3d_view_retry_after(user["username"])
    if wait:
        resp = jsonify({"error": "Too many requests", "retry_after_s": wait})
        resp.status_code = 429
        resp.headers["Retry-After"] = str(wait)
        return resp
    svc = stereo3d_service()
    if data.get("leave"):
        svc.leave(root_id, rel_path, user["username"])
    else:
        svc.view(root_id, rel_path, user["username"])
    return jsonify(svc.status_for(root_id, rel_path))


@app.route("/api/media/3d/convert", methods=["POST"])
@require_admin
def media_3d_convert():
    """Body {items: [{root, path}, ...]}: queues them in the manual lane, in the order given.
    A favourite already waiting moves up to the manual lane."""
    data = request.get_json(silent=True) or {}
    items = data.get("items")
    if not isinstance(items, list) or not items or len(items) > 500:
        return jsonify({"error": "items: 1 to 500 of {root, path}"}), 400
    clean = []
    for it in items:
        root_id, rel_path = (None, None)
        if isinstance(it, dict):
            root_id, rel_path = _stereo3d_clean_path(it.get("root") or DEFAULT_ROOT_ID, it.get("path"))
        if not rel_path:
            return jsonify({"error": "every item needs a root and a path"}), 400
        clean.append({"root": root_id, "path": rel_path})
    user = get_authenticated_user()
    results = stereo3d_service().request_many(clean, stereo3d.LANE_MANUAL, user["username"])
    counts = {}
    for r in results:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    return jsonify({"results": results, "counts": counts})


@app.route("/api/media/3d/queue", methods=["DELETE"])
@require_admin
def media_3d_queue_delete():
    """?key=<key> (or body {key}) drops one item, a running job included; ?all=1 empties the queue."""
    data = request.get_json(silent=True) or {}
    key = request.args.get("key") or data.get("key")
    svc = stereo3d_service()
    if request.args.get("all") in ("1", "true") or data.get("all") is True:
        return jsonify({"removed": svc.clear_queue()})
    if not isinstance(key, str) or not key:
        return jsonify({"error": "key or all=1 is required"}), 400
    return jsonify({"removed": 1 if svc.cancel(key) else 0})


@app.route("/api/media/3d/retry", methods=["POST"])
@require_admin
def media_3d_retry():
    """Body {key} puts one failed item back in the queue, {all: true} every failed one."""
    data = request.get_json(silent=True) or {}
    key = data.get("key")
    if not data.get("all") and not isinstance(key, str):
        return jsonify({"error": "key or all is required"}), 400
    return jsonify({"retried": stereo3d_service().retry(None if data.get("all") else key)})


@app.route("/api/media/3d/file", methods=["GET"])
def media_3d_file():
    """?key=&name=sbs.mp4|depth.mp4|sbs.jpg|depth.png|meta.json - range requests work. A play
    start (no Range, or from byte 0) is what the cache's watched clock counts."""
    user = get_authenticated_user()
    if not user:
        return jsonify({"error": "Unauthorized"}), 401
    name = request.args.get("name", "")
    if name not in stereo3d.FILE_NAMES:
        return jsonify({"error": "unknown file"}), 400
    svc = stereo3d_service()
    folder, entry = svc.entry_files(request.args.get("key", ""))
    if not entry or name not in stereo3d.RESULT_NAMES[entry["kind"]]:
        return jsonify({"error": "Not found"}), 404
    if not root_visible_to(user, entry.get("root")):
        return jsonify({"error": "Forbidden"}), 403
    if name.startswith("sbs."):
        svc.note_play(request.args["key"], request.headers.get("Range"))
    mime = {"mp4": "video/mp4", "jpg": "image/jpeg", "png": "image/png", "json": "application/json"}[name.rsplit(".", 1)[1]]
    resp = send_file(os.path.join(folder, name), mimetype=mime, conditional=True, max_age=0)
    if request.args.get("download") in ("1", "true"):
        resp.headers["Content-Disposition"] = content_disposition_header(stereo3d.download_name(entry, name), True)
    return resp


@app.route("/api/settings/3d", methods=["GET"])
@require_admin
def settings_3d():
    """The 3D card on the settings page: queue with progress, the worker's answer, cache size."""
    return jsonify(stereo3d_service().overview())


@app.route("/api/settings/3d/cache", methods=["DELETE"])
@require_admin
def settings_3d_cache_clear():
    return jsonify({"removed": stereo3d_service().clear_cache()})


# -------------------------------------------------------------------
# Settings page (web_files/settings.html)
# -------------------------------------------------------------------
# The page renders its forms from SETTINGS_SCHEMA, and every save is checked
# against it HERE - the browser's min/max is a convenience, not a rule. A key
# with dots lives in a nested config.json dict; a key starting "indexer."
# lives in indexer_config.json, which the indexer re-reads every rotation.

INDEXER_CONFIG_FILE = os.path.join(BASE_DIR, "indexer_config.json")
INDEXER_PASSES = ("tags", "faces", "caption,ocr")
INDEXER_DEFAULTS = {"pipeline": ["tags"], "pass_budget_s": 1800, "batch_size": 8, "prep_workers": 6}
_settings_write_lock = threading.Lock()

SETTINGS_SCHEMA = [
    {"id": "general", "title": "General", "fields": [
        {"key": "max_recent_media_items", "type": "int", "min": 100, "max": 50000,
         "label": "Recent downloads kept",
         "help": "Entries the Recently downloaded strip can page through. Costs memory, not disk reads."},
        {"key": "max_displayed_tasks_per_pipe", "type": "int", "min": 1, "max": 50,
         "label": "Tasks listed per pipeline",
         "help": "How many queued tasks each pipeline card shows on the dashboard."},
        {"key": "trash_purge_days", "type": "int", "min": 0, "max": 3650, "unit": "days",
         "label": "Empty the trash after",
         "help": "Deleted files - by hand, by a review or by auto-trash - stay restorable this long, "
                 "then go for good. Checked every hour. 0 = never."},
    ]},
    {"id": "shares", "title": "Shares", "fields": [
        {"key": "shares.default_expiry_days", "type": "int", "min": 0, "max": 3650, "unit": "days",
         "label": "New links expire after",
         "help": "0 = never. Existing links keep their own date - change those in the list below."},
    ]},
    {"id": "downloads", "title": "Download behaviour", "fields": [
        {"key": "pipeline_wait_seconds", "type": "map_int", "keys": list(ALL_CATEGORIES),
         "min": 0, "max": 3600, "unit": "s", "label": "Pause between items, per site",
         "help": "Waited after each downloaded item - the main lever against rate limits."},
        {"key": "youtube_playlist_sleep.min_seconds", "type": "int", "min": 0, "max": 3600, "unit": "s",
         "label": "YouTube playlist pause - shortest"},
        {"key": "youtube_playlist_sleep.max_seconds", "type": "int", "min": 0, "max": 7200, "unit": "s",
         "label": "YouTube playlist pause - longest",
         "help": "A random pause in this range between the videos of a playlist or channel. Single videos never wait."},
        {"key": "youtube_playlist_sleep.threshold_count", "type": "int", "min": 0, "max": 10000,
         "label": "Pauses begin after this many videos"},
        {"key": "youtube_playlist_sleep.window_minutes", "type": "int", "min": 1, "max": 1440, "unit": "min",
         "label": "counted over this window"},
        {"key": "twitter_stitch.enabled", "type": "bool",
         "label": "Join Twitter posts cut into pieces", "danger": True,
         "help": "When a post's pictures are one picture cut up - stacked, side by side or a 2x2 grid - "
                 "they are joined after the download and the pieces go to the trash with a ban "
                 "(restorable from the trash). Favourites and share links move to the joined picture."},
        {"key": "image_dedup.enabled", "type": "bool",
         "label": "Keep one copy of a picture saved twice", "danger": True,
         "help": "After each download, a picture that is byte-for-byte the same as one already in the "
                 "library is stored once: a copy in another folder becomes a link to the same file (every "
                 "name stays where it is), and a second name for it in the same folder - an account that "
                 "posted it again - goes to the trash with a ban, the oldest staying (restorable; a "
                 "restored name is left alone from then on). Look-alikes (resized, re-compressed) are not "
                 "touched. The whole library is covered by a pass started from the admin API "
                 "(POST /api/images/dedup, a dry run unless told otherwise)."},
        {"key": "song_merge.enabled", "type": "bool",
         "label": "Keep one copy of a song downloaded twice", "danger": True,
         "help": "When the library holds a song twice - an upload titled \"Artist - Song\" and the "
                 "YouTube Music track \"Song\", or one title twice - and both are the same recording "
                 "(compared by sound, so remixes and edits stay), the file with the higher audio "
                 "bitrate stays under the title without the artist in it. The other goes to the trash "
                 "(restorable) and its names in playlists become names of the one that stays."},
    ]},
    {"id": "image_processing", "title": "Image processing", "fields": [
        {"key": "indexer.pipeline", "type": "passes", "options": list(INDEXER_PASSES),
         "label": "Vision passes, in rotation order",
         "help": "Each pass runs for its time slice, then the next. New downloads get every pass within one rotation."},
        {"key": "indexer.pass_budget_s", "type": "int", "min": 60, "max": 86400, "unit": "s",
         "label": "Time slice per pass"},
        {"key": "indexer.batch_size", "type": "int", "min": 1, "max": 64,
         "label": "Files per worker request"},
        {"key": "indexer.prep_workers", "type": "int", "min": 1, "max": 32,
         "label": "Files read in parallel",
         "help": "Reading files and pulling video frames for a batch. More helps a slow disk only up to a point."},
        {"key": "auto_trash_reasons", "type": "multi", "options": list(media_review.RULES),
         "label": "Rules that trash without review", "danger": True,
         "help": "Pending suggestions of these rules go straight to the trash with a ban (restorable from the trash). Every other rule waits in the review queue."},
        {"key": "dimensions_backfill.enabled", "type": "bool",
         "label": "Measure new downloads every night",
         "help": "Dimensions, frame rate and video length for the filters and the gallery. Otherwise new files are measured only when run by hand."},
        {"key": "dimensions_backfill.hour", "type": "int", "min": 0, "max": 23, "unit": "h",
         "label": "at this hour (server time)"},
    ]},
    {"id": "video_trim", "title": "Intro & outro cutting", "fields": [
        {"key": "video_trim.enabled", "type": "bool",
         "label": "Look for repeating intros and outros",
         "help": "Within one folder (a channel, a playlist), videos that open or end with the same "
                 "footage are found in the background. Nothing is cut until you approve that intro or "
                 "outro below; the cut is a stream copy, so the video is not re-encoded."},
        {"key": "video_trim.folders", "type": "lines",
         "label": "Folders to scan",
         "help": "One root ID:folder per line, including subfolders. Example: downloads:youtube or downloads:youtube/Akashi. Empty = scan no folders."},
        {"key": "video_trim.after_download", "type": "bool",
         "label": "Look after every video download",
         "help": "The folder of a new video is checked, and approved intros and outros are cut from it."},
        {"key": "video_trim.daily_time", "type": "time",
         "label": "Daily pass at",
         "help": "Server time, HH:MM. Folders that changed since the last pass are checked. Empty = no daily pass."},
        {"key": "video_trim.scan_seconds", "type": "int", "min": 30, "max": 600, "unit": "s",
         "label": "Length looked at, at each end"},
        {"key": "video_trim.min_videos", "type": "int", "min": 2, "max": 50,
         "label": "Videos that must share a segment"},
        {"key": "video_trim.min_seconds", "type": "float", "min": 1, "max": 30, "step": 0.5, "unit": "s",
         "label": "Shortest segment"},
        {"key": "video_trim.max_seconds", "type": "float", "min": 5, "max": 300, "step": 1, "unit": "s",
         "label": "Longest segment"},
        {"key": "video_trim.max_mean_distance", "type": "float", "min": 0, "max": 16, "step": 0.5,
         "label": "How alike on average",
         "help": "Bits (of 64) in which two frames of the segment may differ, on average. Lower = stricter."},
        {"key": "video_trim.max_frame_distance", "type": "int", "min": 1, "max": 32,
         "label": "How alike at worst",
         "help": "Bits (of 64) in which any single frame may differ."},
        {"key": "video_trim.max_keyframe_gap_s", "type": "float", "min": 0, "max": 10, "step": 0.1, "unit": "s",
         "label": "Furthest keyframe after an intro",
         "help": "An intro can only be cut at a keyframe. A video without one this close after the intro "
                 "is skipped - it is never re-encoded."},
    ]},
    {"id": "song_covers", "title": "Song covers", "fields": [
        {"key": "song_covers.enabled", "type": "bool",
         "label": "Remake covers that show the wrong frame",
         "help": "A song's cover was the biggest picture the download could find - usually a frame ten "
                 "seconds in, which on many re-upload channels is still the intro and elsewhere a fade to "
                 "black. This fetches each video again, reads its timeline and takes the cover from a "
                 "moment chosen for that video. Weeks of work at the rate below; it carries on by itself "
                 "across restarts and never runs while a download does. This repairs a one-off "
                 "mistake, so it is meant to be removed once the list below is empty and a scan "
                 "finds nothing more to fix."},
        {"key": "song_covers.scope", "type": "select",
         "options": ["black", "channels", "all"],
         "labels": {"black": "Only covers that are black",
                    "channels": "Black covers and the channels below",
                    "all": "Every song in the library"},
         "label": "Which songs"},
        {"key": "song_covers.channels", "type": "lines",
         "label": "Channels with an intro",
         "help": "One per line; a folder name in youtube_music/ counts when it starts with this."},
        {"key": "song_covers.songs_per_hour", "type": "int", "min": 1, "max": 240, "unit": "/h",
         "label": "Songs per hour",
         "help": "Each song costs one small download of the whole video (360p) and one of a few seconds "
                 "at full size - about 8 MB. Low is the point: this shares YouTube with the downloads."},
        {"key": "song_covers.frame_height", "type": "int", "min": 360, "max": 2160, "unit": "px",
         "label": "Tallest frame to fetch"},
        {"key": "song_covers.min_gain", "type": "float", "min": 0, "max": 1, "step": 0.01,
         "label": "Improvement a new frame needs",
         "help": "How much better the new frame has to look (contrast and brightness, 0-1) before it "
                 "replaces the cover. A black cover is replaced whatever this says."},
        {"key": "song_covers.rescan_days", "type": "int", "min": 1, "max": 90, "unit": "days",
         "label": "Look for new songs to fix every"},
    ]},
    {"id": "live_upscale", "title": "Live upscaling", "fields": [
        {"key": "live_upscale.enabled", "type": "bool", "label": "Upscale images you keep looking at"},
        {"key": "live_upscale.shares", "type": "bool", "label": "Also on shared links",
         "help": "Visitors of a share link get upscaled pictures and covers too, on the same GPU queue. "
                 "Needs the setting above."},
        {"key": "live_upscale.view_seconds", "type": "float", "min": 1, "max": 120, "step": 0.5, "unit": "s",
         "label": "Looking time before upscaling"},
        {"key": "live_upscale.zoom_factor", "type": "float", "min": 1, "max": 4, "step": 0.5, "unit": "x",
         "label": "Zoom that must stay sharp",
         "help": "Upscaled until this much zoom on the viewer's screen shows no enlarged pixels."},
        {"key": "live_upscale.max_factor", "type": "int", "min": 2, "unit": "x",
         "label": "Largest upscale factor",
         "help": "Any whole number from 2. Each image gets the smallest factor that keeps the zoom sharp, "
                 "up to this - and never a result larger than Largest result below."},
        {"key": "live_upscale.max_output_megapixels", "type": "int", "min": 4, "max": 256, "unit": "MP",
         "label": "Largest result",
         "help": "Upscaled pictures never pass this, nor the vision worker's own ceiling (shown under Cache "
                 "below). When the zoom would need more, the largest picture within the lower of the two is made."},
        {"key": "live_upscale.cache_days", "type": "int", "min": 1, "max": 365, "unit": "days",
         "label": "Evict when not viewed for"},
        {"key": "live_upscale.cache_max_gb", "type": "float", "min": 0.5, "max": 2000, "step": 0.5, "unit": "GB",
         "label": "Cache size limit"},
        {"key": "live_upscale.quality", "type": "int", "min": 60, "max": 100, "label": "WebP quality"},
        {"key": "live_upscale.photo_model", "type": "select", "options": list(UPSCALE_PHOTO_MODELS),
         "labels": UPSCALE_PHOTO_MODELS, "label": "Photo upscaler",
         "help": "For photos, cosplay and renders; artwork keeps the anime model. From the side-by-side on "
                 "your photos: RealPLKSR keeps colours closest, Siax CX is sharper and a little brighter, "
                 "UltraSharp sharper still, Nomos2 otf softer. A change applies to every picture from then "
                 "on; copies made with the previous model are not shown again and age out of the cache."},
        {"key": "live_upscale.restore_grain", "type": "bool", "label": "Keep film grain on artwork",
         "help": "The anime model paints out the film grain artists lay over a drawing. With this on, the "
                 "vision worker measures the grain on the original - strength, size, colour - and lays a "
                 "matching grain back over the upscale. Pictures without grain are left as they are. "
                 "Copies made without it are made again the next time they are viewed."},
        {"key": "live_upscale.default_model", "type": "select", "options": ["general", "anime"],
         "labels": {"general": "Photo upscaler", "anime": "Anime model"},
         "label": "Model when tags and caption decide nothing"},
        {"key": "live_upscale.tag_min_confidence", "type": "float", "min": 0.05, "max": 0.95, "step": 0.05,
         "label": "Tag confidence needed"},
        {"key": "live_upscale.face_min_score", "type": "float", "min": 0.5, "max": 1, "step": 0.05,
         "label": "Face score meaning a photo",
         "help": "When no tag or caption decides, a face detected at least this surely gets the photo upscaler - "
                 "drawn faces score lower. At 0.8: 47% of photos, 2.5% of drawings. 1 = off."},
        {"key": "live_upscale.photo_tags", "type": "tags",
         "label": "Tags meaning a photo or render (photo upscaler)", "help": "Checked first."},
        {"key": "live_upscale.anime_tags", "type": "tags", "label": "Tags meaning artwork (anime model)"},
        {"key": "live_upscale.cover_max_size", "type": "select", "options": list(COVER_SIZE_CHOICES),
         "labels": {"3840x2160": "4K (3840 × 2160)", "5120x2880": "5K (5120 × 2880)",
                    "7680x4320": "8K (7680 × 4320)", "15360x8640": "16K (15360 × 8640)", "none": "No limit"},
         "label": "Largest song cover",
         "help": "An upscaled song cover is made and kept no larger than this, whatever the screen asks for: "
                 "the upscaler still runs its whole factor and the result is shrunk to fit. On the short side "
                 "too, so a square cover stops at the height. Copies over it are made again at this size "
                 "the next time the song plays, and the larger ones leave the cache."},
        {"key": "live_upscale.soft_cover_folders", "type": "lines",
         "label": "Soft covers: folders whose song covers are upscaled as if smaller",
         "help": "One folder per line, as the info panel names it (Downloads / youtube_music/MHM), or just the "
                 "path to count it in every library. For channels whose 4K uploads are stretched from a smaller "
                 "picture: a cover in one of these folders, their subfolders, or a playlist's link to a song in "
                 "one is shrunk to the size below and upscaled from there, instead of counting as sharp enough."},
        {"key": "live_upscale.soft_cover_height", "type": "int", "min": 144, "max": 4320, "unit": "px",
         "label": "Soft covers count as at most",
         "help": "On the short side: 1080 takes a 3840 × 2160 cover as 1920 × 1080. Smaller covers are left as "
                 "they are."},
        {"key": "live_upscale.soft_cover_auto", "type": "bool",
         "label": "Soft covers: also find them by measuring",
         "help": "A song cover is measured once: if its edges are no steeper than the limit below, it is taken "
                 "as the short side set above, like a cover in a soft-cover folder, and upscaled from there "
                 "(never below half its own size, counted at 4K at most). The info panel says when this "
                 "happened. Covers a soft-cover folder already shrinks, and covers too small to shrink to the "
                 "short side above, are not measured."},
        {"key": "live_upscale.soft_cover_auto_min_long", "type": "int", "min": 0, "max": 8000, "unit": "px",
         "label": "Soft covers: measure covers from",
         "help": "Long side of the cover. 0 = every cover large enough to be shrunk, 3800 = only covers of 4K "
                 "or more."},
        {"key": "live_upscale.soft_cover_auto_edge", "type": "float", "min": 0.2, "max": 0.8, "step": 0.01,
         "label": "Soft covers: measured edge steepness at most",
         "help": "How much of an edge's contrast its steepest pixel step holds, at the cover's own size (4K at "
                 "most). 0.40 = no sharper than half that size, 0.47 = two thirds. Sharp 4K covers measure about "
                 "0.45 to 0.75 (Geoxor's lowest: 0.42), MHM and Spirit covers about 0.30 to 0.50, most 1080p "
                 "covers above 0.45."},
        {"key": "live_upscale.cache_dir", "type": "text", "label": "Cache folder",
         "help": "Empty = on the media disk beside the downloads. Avoid /data2: it is a thin pool."},
    ]},
    {"id": "stereo3d", "title": "2D to 3D", "fields": [
        {"key": "stereo3d.enabled", "type": "bool", "label": "Allow 3D conversion",
         "help": "Videos and pictures become side-by-side 3D (plus a depth map) on the vision worker, one job at "
                 "a time, when its GPU is free. Nothing converts on its own: admins start it with Convert to 3D."},
        {"key": "stereo3d.cache_max_gb", "type": "int", "min": 10, "max": 5000, "unit": "GB",
         "label": "3D cache size limit",
         "help": "Over this, the least recently watched conversions go first; favourites of admins last."},
        {"key": "stereo3d.max_eye_long_side", "type": "select", "options": [1440, 2160, 2560, 3840],
         "labels": {"1440": "1440 px", "2160": "2160 px (the headset panel)", "2560": "2560 px", "3840": "3840 px"},
         "label": "Largest video, per eye (long side)",
         "help": "Videos only: a bigger source is scaled down to this on its longer side. Pictures are never "
                 "scaled, so they stay zoomable. A change re-converts a video on its next request."},
        {"key": "stereo3d.divergence", "type": "float", "min": 1, "max": 5, "step": 0.1,
         "label": "3D strength (divergence)",
         "help": "How far the eyes' views are pushed apart. A change re-converts a file on its next request."},
        {"key": "stereo3d.convergence", "type": "float", "min": 0, "max": 1, "step": 0.05,
         "label": "Screen depth (convergence)",
         "help": "Which depth sits on the screen plane. A change re-converts a file on its next request."},
        {"key": "stereo3d.mbps_1080", "type": "int", "min": 5, "max": 300, "unit": "Mbit/s",
         "label": "Video bitrate cap, 1080p eyes"},
        {"key": "stereo3d.mbps_1440", "type": "int", "min": 5, "max": 300, "unit": "Mbit/s",
         "label": "Video bitrate cap, 1440p eyes"},
        {"key": "stereo3d.mbps_2160", "type": "int", "min": 5, "max": 300, "unit": "Mbit/s",
         "label": "Video bitrate cap, 2160p eyes",
         "help": "Between these sizes the cap follows the eye's pixel count."},
        {"key": "stereo3d.favorites_auto", "type": "bool", "label": "Prepare new favourites",
         "help": "An admin's favourite video or picture is queued for conversion behind manual requests."},
        {"key": "stereo3d.favorites_backfill_days", "type": "int", "min": 0, "max": 3650, "unit": "days",
         "label": "Prepare favourites from the last",
         "help": "Existing favourites this recent are queued too, newest first. 0 = only new ones."},
        {"key": "stereo3d.permanent_folders", "type": "lines",
         "label": "Permanent 3D folders",
         "help": "One folder per line, as the info panel names it (Downloads / youtube/Some channel), or just "
                 "the path to count it in every library. Every video and picture in these folders and their "
                 "subfolders is converted in the background, newest first, behind your own requests, and is "
                 "never evicted - so the cache size above has to hold them. New files are picked up within "
                 "the hour."},
        {"key": "stereo3d.view_auto", "type": "bool", "label": "Convert what is viewed in VR",
         "help": "Opening a picture or video in the VR app that has no 3D version queues it ahead of manual "
                 "requests and favourites; the next visit is 3D. The 2D site never does this."},
        {"key": "stereo3d.view_auto_admin_only", "type": "bool", "label": "Only admins' views convert",
         "help": "Off = every signed-in user (guests included) who can see the file triggers the conversion."},
        {"key": "stereo3d.viewing_max_per_user", "type": "int", "min": 1, "max": 20,
         "label": "Items one user can have queued as viewed",
         "help": "A newer view beyond this moves that user's oldest to the manual lane (a lost leave)."},
        {"key": "stereo3d.max_attempts", "type": "int", "min": 1, "max": 10,
         "label": "Attempts before an item is marked failed"},
        {"key": "stereo3d.cache_dir", "type": "text", "label": "3D cache folder",
         "help": "Empty = ~/.cache/media-dl-3d (or $MEDIA_3D_CACHE). The folder must already exist; it is never created "
                 "and never falls back to another disk."},
    ]},
]


def _dig(data, key):
    node = data
    for part in key.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


def _settings_field(key):
    for section in SETTINGS_SCHEMA:
        for field in section["fields"]:
            if field["key"] == key:
                return field
    return None


def _read_indexer_config():
    try:
        with open(INDEXER_CONFIG_FILE, encoding="utf-8") as fh:
            return json.load(fh) or {}
    except (OSError, ValueError):
        return {}


def _atomic_json_write(path, data, indent=None):
    with open(path + ".tmp", "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=indent, ensure_ascii=False)
    os.replace(path + ".tmp", path)


def auto_trash_reasons():
    """The rules trusted to trash without review: config.json, known rules only."""
    configured = CONFIG.get("auto_trash_reasons")
    if not isinstance(configured, list):
        return tuple(media_review.AUTO_TRASH_REASONS)
    return tuple(r for r in media_review.RULES if r in configured)


def settings_values():
    indexer = dict(INDEXER_DEFAULTS, **_read_indexer_config())
    if not indexer.get("pipeline"):
        indexer["pipeline"] = [indexer.get("tasks") or "tags"]
    values = {}
    for section in SETTINGS_SCHEMA:
        for field in section["fields"]:
            key = field["key"]
            if key.startswith("indexer."):
                values[key] = indexer.get(key.split(".", 1)[1])
            elif key == "auto_trash_reasons":
                values[key] = list(auto_trash_reasons())
            else:
                value = _dig(CONFIG, key)
                values[key] = _dig(DEFAULT_CONFIG, key) if value is None else value
    return values


def _coerce_setting(field, value):
    """The value to store; raises ValueError with a message for the page."""
    kind = field["type"]

    def bounded(v):
        if "min" in field and v < field["min"]:
            raise ValueError(f"at least {field['min']}")
        if "max" in field and v > field["max"]:
            raise ValueError(f"at most {field['max']}")
        return v

    if kind == "int":
        if isinstance(value, bool):
            raise ValueError("a whole number")
        try:
            number = float(value)
        except (TypeError, ValueError):
            raise ValueError("a whole number")
        if number != int(number):
            raise ValueError("a whole number")
        return bounded(int(number))
    if kind == "float":
        if isinstance(value, bool):
            raise ValueError("a number")
        try:
            number = float(value)
        except (TypeError, ValueError):
            raise ValueError("a number")
        if number != number or number in (float("inf"), float("-inf")):
            raise ValueError("a number")
        return bounded(round(number, 3))
    if kind == "bool":
        if not isinstance(value, bool):
            raise ValueError("on or off")
        return value
    if kind == "time":
        text = value.strip() if isinstance(value, str) else None
        if text is None or (text and not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", text)):
            raise ValueError("a time as HH:MM, or empty")
        return text
    if kind == "select":
        for option in field["options"]:
            if str(option) == str(value):
                return option
        raise ValueError("one of " + ", ".join(str(o) for o in field["options"]))
    if kind == "text":
        if not isinstance(value, str) or len(value) > 500:
            raise ValueError("text of at most 500 characters")
        return value.strip()
    if kind == "tags":
        if isinstance(value, str):
            value = [p for p in re.split(r"[,\s]+", value) if p]
        if not isinstance(value, list) or len(value) > 200:
            raise ValueError("a list of tags")
        out = []
        for tag in value:
            tag = str(tag).strip()
            if not re.fullmatch(r"[\w().:'/+-]{1,80}", tag):
                raise ValueError(f"not a tag: {tag[:40]}")
            if tag not in out:
                out.append(tag)
        return out
    if kind == "lines":
        # One entry per line, because what this holds are names with spaces in
        # them (a channel folder) - nothing is split on whitespace here.
        if isinstance(value, str):
            value = value.splitlines()
        if not isinstance(value, list) or len(value) > 200:
            raise ValueError("one entry per line, at most 200 of them")
        out = []
        for item in value:
            item = str(item).strip()
            if not item:
                continue
            if len(item) > 120:
                raise ValueError(f"too long: {item[:40]}")
            if item not in out:
                out.append(item)
        return out
    if kind == "multi":
        if not isinstance(value, list):
            raise ValueError("a list")
        unknown = [v for v in value if v not in field["options"]]
        if unknown:
            raise ValueError("unknown: " + ", ".join(str(v) for v in unknown)[:200])
        return [o for o in field["options"] if o in value]
    if kind == "passes":
        if (not isinstance(value, list) or not value
                or any(v not in field["options"] for v in value) or len(set(value)) != len(value)):
            raise ValueError("at least one pass, each at most once")
        return list(value)
    if kind == "map_int":
        if not isinstance(value, dict):
            raise ValueError("a value per site")
        out = {}
        for site, raw in value.items():
            if site not in field["keys"]:
                raise ValueError(f"unknown site {site}")
            try:
                number = int(raw)
            except (TypeError, ValueError):
                raise ValueError(f"{site}: a whole number")
            if number < field["min"] or number > field["max"]:
                raise ValueError(f"{site}: {field['min']} to {field['max']}")
            out[site] = number
        return out
    raise ValueError("not editable")


@app.route("/api/settings", methods=["GET"])
@require_admin
def get_settings():
    return jsonify({"schema": SETTINGS_SCHEMA, "values": settings_values(),
                    "server_time": time.strftime("%H:%M %Z")})


@app.route("/api/settings", methods=["PUT"])
@require_admin
def put_settings():
    """Body: {"values": {key: value}}. All or nothing: one invalid value and
    nothing is written, and each problem comes back under its key."""
    incoming = (request.get_json(silent=True) or {}).get("values")
    if not isinstance(incoming, dict) or not incoming:
        return jsonify({"error": "values required"}), 400
    errors, clean = {}, {}
    for key, value in incoming.items():
        field = _settings_field(key)
        if not field:
            errors[key] = "not a setting"
            continue
        try:
            clean[key] = _coerce_setting(field, value)
        except ValueError as exc:
            errors[key] = str(exc)
    shortest = clean.get("youtube_playlist_sleep.min_seconds", _dig(CONFIG, "youtube_playlist_sleep.min_seconds"))
    longest = clean.get("youtube_playlist_sleep.max_seconds", _dig(CONFIG, "youtube_playlist_sleep.max_seconds"))
    if shortest is not None and longest is not None and shortest > longest:
        errors["youtube_playlist_sleep.max_seconds"] = "must not be shorter than the shortest pause"
    if errors:
        return jsonify({"errors": errors}), 400

    with _settings_write_lock:
        indexer_updates = {k.split(".", 1)[1]: v for k, v in clean.items() if k.startswith("indexer.")}
        if indexer_updates:
            current = _read_indexer_config()
            current.update(indexer_updates)
            if "pipeline" in indexer_updates:
                # The indexer's single-pass fallback stays meaningful.
                current["tasks"] = indexer_updates["pipeline"][0]
            _atomic_json_write(INDEXER_CONFIG_FILE, current, indent=2)
        config_updates = {k: v for k, v in clean.items() if not k.startswith("indexer.")}
        if config_updates:
            for key, value in config_updates.items():
                parts = key.split(".")
                node = CONFIG
                for part in parts[:-1]:
                    node = node.setdefault(part, {})
                if isinstance(value, dict) and isinstance(node.get(parts[-1]), dict):
                    node[parts[-1]].update(value)     # a per-site map: only the sites sent
                else:
                    node[parts[-1]] = value
            _atomic_json_write(CONFIG_FILE, CONFIG, indent=4)
    if any(k.startswith("stereo3d.") for k in clean):
        _stereo3d_backfill_wake.set()       # the favourites backfill follows the window at once
    print(f"[Info] settings changed by {get_authenticated_user()['username']}: {sorted(clean)}", flush=True)
    return jsonify({"values": settings_values()})


@app.route("/api/settings/stats", methods=["GET"])
@require_admin
def settings_stats():
    """What the Download behaviour section shows beside its settings."""
    weekly = get_weekly_usage()
    pipes, statuses = {}, {}
    with task_lock:
        for cat, q in pipelines.items():
            stats = pipeline_stats[cat]
            pipes[cat] = {"queued": q.qsize(), "rate_mbps": round(stats["rate_mbps"], 2),
                          "session_total_mb": round(stats["session_total_mb"], 1),
                          "weekly_usage": weekly.get(cat, 0)}
        for task in active_tasks.values():
            status = task.get("status") or "?"
            statuses[status] = statuses.get(status, 0) + 1
    return jsonify({"volume_1h_bytes": compute_downloaded_bytes_since(3600),
                    "volume_24h_bytes": compute_downloaded_bytes_since(24 * 3600),
                    "pipelines": pipes, "task_statuses": statuses,
                    "dimensions": dict(_dimensions_run), "youtube_cookies": youtube_cookie_status(),
                    "server_time": time.strftime("%H:%M %Z")})


@app.route("/api/downloads/cookies", methods=["GET"])
@require_admin
def download_cookie_status():
    """Sign-in state of the YouTube cookie file, for the dashboard's warning."""
    return jsonify({"youtube": youtube_cookie_status()})


@app.route("/api/settings/users", methods=["GET"])
@require_admin
def settings_users():
    """Accounts belong to Authelia; this is what media-dl knows about them."""
    return jsonify({
        "me": get_authenticated_user()["username"],
        "users": [{"username": u["username"], "role": u.get("role", "admin"),
                   "has_token": bool(u.get("api_token")), "last_seen": u.get("last_seen"),
                   "advanced_mode": bool((u.get("prefs") or {}).get("advanced_mode"))}
                  for u in load_users()],
        "groups": {"admin": sorted(SSO_ADMIN_GROUPS), "guest": sorted(SSO_GUEST_GROUPS)},
        "roots": [{"id": r["id"], "name": r["name"], "guest_visible": bool(r.get("guest_visible")),
                   "builtin": bool(r.get("builtin"))} for r in get_all_roots()],
    })


@app.route("/api/settings/users/<username>/token", methods=["GET", "POST", "DELETE"])
@require_admin
def settings_user_token(username):
    """GET shows the stored token (tokens are stored verbatim anyway, and
    whoever holds an admin session can mint or replace any of them);
    POST issues a new API token, replacing any old one; DELETE revokes it.
    For the phone shortcut, scripts and cron."""
    with users_lock:
        users = _read_users_unlocked()
        user = next((u for u in users if u["username"].lower() == username.lower()), None)
        if not user:
            return jsonify({"error": "Not found"}), 404
        if request.method == "GET":
            if not user.get("api_token"):
                return jsonify({"error": "No token issued"}), 404
            return jsonify({"username": user["username"], "token": user["api_token"]})
        if request.method == "POST":
            token = secrets.token_urlsafe(32)
            user["api_token"] = token
            _write_users_unlocked(users)
            print(f"[Info] API token issued for {user['username']} by {get_authenticated_user()['username']}", flush=True)
            return jsonify({"username": user["username"], "token": token})
        user.pop("api_token", None)
        _write_users_unlocked(users)
        print(f"[Info] API token revoked for {user['username']} by {get_authenticated_user()['username']}", flush=True)
        return jsonify({"username": user["username"], "revoked": True})


_dimensions_run = {"running": False, "started": None, "finished": None, "exit": None,
                   "last_line": "", "trigger": None}


def run_dimensions_backfill(trigger):
    """backfill_dimensions.py --kind all in the background; False if one runs.
    It is resumable and prunes stale entries, so a nightly run only measures
    what is new."""
    if _dimensions_run["running"]:
        return False
    _dimensions_run.update(running=True, started=time.time(), finished=None, exit=None,
                           last_line="", trigger=trigger)

    def work():
        try:
            proc = subprocess.Popen(
                [sys.executable, "-u", os.path.join(BASE_DIR, "backfill_dimensions.py"), "--kind", "all"],
                cwd=BASE_DIR, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            for line in proc.stdout:
                if line.strip():
                    _dimensions_run["last_line"] = line.strip()[:300]
            _dimensions_run["exit"] = proc.wait()
        except Exception as exc:
            _dimensions_run.update(exit=-1, last_line=str(exc)[:300])
        finally:
            _dimensions_run.update(running=False, finished=time.time())

    threading.Thread(target=work, daemon=True, name="dimensions-backfill").start()
    return True


def _dimensions_schedule_loop():
    last_day = None
    while True:
        try:
            cfg = CONFIG.get("dimensions_backfill") or {}
            now = time.localtime()
            today = time.strftime("%Y-%m-%d", now)
            if cfg.get("enabled") and now.tm_hour == int(cfg.get("hour", 4)) and last_day != today:
                last_day = today
                run_dimensions_backfill("nightly")
        except Exception as exc:
            print(f"[Warning] dimensions schedule: {exc}", flush=True)
        time.sleep(300)


@app.route("/api/settings/dimensions/run", methods=["POST"])
@require_admin
def settings_dimensions_run():
    started = run_dimensions_backfill("manual")
    return jsonify({"started": started, "state": dict(_dimensions_run)})


@app.route("/api/media/thumbnail", methods=["GET"])
def media_thumbnail():
    user = get_authenticated_user()
    if not user:
        return jsonify({"error": "Unauthorized"}), 401

    root_id = request.args.get("root", "").strip() or DEFAULT_ROOT_ID
    if not root_visible_to(user, root_id):
        return jsonify({"error": "Forbidden"}), 403

    file_path = resolve_media_path(root_id, request.args.get("path", ""))
    if not file_path:
        return jsonify({"error": "Invalid path"}), 400

    if not os.path.isfile(file_path):
        return jsonify({"error": "Not found"}), 404

    cover = get_file_thumbnail(file_path)

    if not cover:
        return jsonify({"error": "No embedded thumbnail"}), 404

    mime, data = cover
    resp = app.response_class(data, mimetype=mime)
    resp.headers["Cache-Control"] = "private, max-age=86400"
    return resp

def get_file_thumbnail(file_path):
    media_type = classify_media_type(file_path)
    if media_type == "audio":
        return get_embedded_cover(file_path)
    if media_type == "video":
        return get_video_frame_thumbnail(file_path)
    if media_type == "image":
        return get_image_downscaled_thumbnail(file_path)
    if is_ugoira_zip(file_path):
        return get_ugoira_thumbnail(file_path)
    return None

def _resolve_downloads_subpath(rel_path, root_id=DEFAULT_ROOT_ID):
    """Resolves rel_path against the given root (default: downloads).
    Returns the absolute path, or None if the root is unknown or the path
    would escape it (traversal guard)."""
    return resolve_media_path(root_id, rel_path)

def _scan_media_dir(target_dir, rel_path, root_id=DEFAULT_ROOT_ID):
    dirs, files = [], []
    candidates = []
    with os.scandir(target_dir) as it:
        for entry in it:
            if entry.name.startswith("."):
                continue
            if entry.is_dir():
                dirs.append(entry.name)
            elif entry.is_file() and not _is_hidden_or_partial(entry.name):
                candidates.append(entry)
    # Second pass, so a hidden poster costs no stat() - see the browse route.
    hidden = poster_companion_names([e.name for e in candidates])
    for entry in candidates:
        if entry.name not in hidden:
            files.append(_media_file_entry(rel_path, entry.name, entry.stat(), root_id))
    dirs.sort(key=str.lower)
    files.sort(key=lambda f: f["name"].lower())
    return dirs, files

# -------------------------------------------------------------------
# Public Sharing - file/folder/pool links that work with no login, gated
# only by an unguessable per-share token (like an API key). shares.json is
# the full record of every share ever created (including revoked ones),
# which doubles as the "what has/hasn't been shared" tracking log.
# -------------------------------------------------------------------

shares_lock = threading.Lock()

def _read_shares_unlocked():
    if not os.path.exists(SHARES_FILE):
        return []
    with open(SHARES_FILE, "r", encoding="utf-8") as f:
        return json.load(f)

def _write_shares_unlocked(shares):
    temp_file = SHARES_FILE + ".tmp"
    with open(temp_file, "w", encoding="utf-8") as f:
        json.dump(shares, f, indent=2, ensure_ascii=False)
    os.replace(temp_file, SHARES_FILE)

def load_shares():
    with shares_lock:
        try:
            return _read_shares_unlocked()
        except Exception as e:
            print(f"[Warning] Failed to load {SHARES_FILE}: {str(e)}")
            return []

def find_share(token):
    """The share a public URL names - never a revoked or an expired one. Every
    public route gates through here, so an expiry takes effect everywhere."""
    share = find_share_including_expired(token)
    return None if share is None or share_expired(share) else share


def find_share_including_expired(token):
    for s in load_shares():
        if secrets.compare_digest(s["id"], token) and not s.get("revoked"):
            return s
    return None


def share_expired(share, now=None):
    expires = share.get("expires_at")
    return bool(expires) and (now or time.time()) >= float(expires)


def share_status(share):
    if share.get("revoked"):
        return "revoked"
    return "expired" if share_expired(share) else "active"


def default_share_expiry(created=None):
    """expires_at for a new share, from Settings > Shares; None = never."""
    try:
        days = float((CONFIG.get("shares") or {}).get("default_expiry_days") or 0)
    except (TypeError, ValueError):
        days = 0
    return (created or time.time()) + days * 86400 if days > 0 else None

def share_public_url(share):
    return f"/share/{share['id']}"

def share_label(share):
    if share.get("label"):
        return share["label"]
    if share["kind"] == "pool":
        return "Shared Pool"
    if share["kind"] == "query":
        return "Shared " + ((share.get("query") or {}).get("type") or "selection")
    return os.path.basename(share["path"].rstrip("/")) or share["path"]

def share_hidden_name(path):
    """Files no share link shows or serves, whatever the share covers: anything
    with "Screenshot_" in its file name (phone screenshots, which land in the
    library next to the photos and can show anything that was on the screen).
    Checked where a link lists files and where it serves one, so it holds for
    links made before the rule as well as new ones."""
    name = (path or "").replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
    return "screenshot_" in name.lower()

def path_allowed_by_share(share, rel_path, root_id=None):
    """A path is in scope for a share if it IS the shared file, or falls
    inside the shared folder / one of a pool's folder items - and only
    within that entry's own media root, so a path that happens to match
    under a *different* root is still rejected."""
    rel_path = (rel_path or "").strip("/").replace("\\", "/")
    root_id = root_id or DEFAULT_ROOT_ID
    # No "." or ".." segments. "shared/../private/x.jpg" starts with "shared/",
    # and resolve_media_path keeps a path inside the ROOT, not inside the share -
    # so without this a link to one folder read, listed and thumbnailed any file
    # in the same library (fixed 2026-09-18).
    if any(part in (".", "..") for part in rel_path.split("/")):
        return False
    if share_hidden_name(rel_path):
        return False

    def within(base, base_root):
        return base_root == root_id and (rel_path == base or rel_path.startswith(base + "/"))

    if share["kind"] in ("file", "folder"):
        return within(share["path"], share_root_of(share))
    if share["kind"] == "query":
        return (root_id, rel_path) in query_share_files(share)[1]
    if share["kind"] == "pool":
        for item in share.get("items", []):
            if within(item["path"], share_root_of(item)):
                return True
    return False

def share_summary(share):
    out = {
        "id": share["id"],
        "kind": share["kind"],
        "label": share_label(share),
        "created_by": share.get("created_by"),
        "created_at": share.get("created_at"),
        "revoked": bool(share.get("revoked")),
        "revoked_at": share.get("revoked_at"),
        "url": share_public_url(share),
        "has_custom_thumbnail": bool(share.get("thumbnail")),
        "view": share.get("view") or "files",
        "cover_url": f"/api/share/{share['id']}/cover?v={cover_version(share)}"
    }
    out["expires_at"] = share.get("expires_at")
    out["expired"] = share_expired(share)
    out["status"] = share_status(share)
    if share["kind"] in ("file", "folder"):
        out["path"] = share["path"]
        out["root"] = share_root_of(share)
    elif share["kind"] == "query":
        # What the link shows - without the face ids a person link anchors
        # on, which are internal and mean nothing to a reader.
        q = share.get("query") or {}
        out["query"] = {"type": q.get("type"), "filters": q.get("filters") or {},
                        "tags": q.get("tags") or [], "text": q.get("text") or "",
                        "mode": q.get("mode") or ""}
    else:
        out["items"] = [
            {**i, "root": share_root_of(i)} for i in share.get("items", [])
        ]
    return out

def share_root_of(obj):
    """A share (or one pool item) records which media root its path is
    relative to. Anything stored before roots existed has none, and by
    definition lived in the built-in downloads root."""
    return (obj or {}).get("root") or DEFAULT_ROOT_ID

def share_media_type(share):
    """Type of a single-file share, resolved against the file on disk so an
    ugoira archive is described as the video it plays as."""
    full = resolve_media_path(share_root_of(share), share.get("path", ""))
    if full and os.path.isfile(full):
        return classify_media_path(full)
    return classify_media_type(share.get("path", ""))

def _cover_for_downloads_path(rel_path, root_id=DEFAULT_ROOT_ID):
    """Returns (file_path, generated) for a single path inside a root -
    either the file itself (if it's an image, it IS its own cover) or a
    generated cover (embedded audio art / video frame grab). None if
    neither is available."""
    full = resolve_media_path(root_id, rel_path)
    if not full or not os.path.isfile(full):
        return None
    if classify_media_path(full) == "image":
        return full, None
    cover = get_file_thumbnail(full)
    return (None, cover) if cover else None

def resolve_share_cover(share):
    """Order: a manually uploaded thumbnail wins; otherwise fall back to
    the shared file's own cover, or the first item in a folder/pool that
    has one. Returns (file_path, generated_cover) - exactly one is set -
    or None if nothing usable was found."""
    custom = share.get("thumbnail")
    if custom:
        custom_path = os.path.join(SHARE_THUMBS_DIR, custom)
        if os.path.isfile(custom_path):
            return custom_path, None

    if share["kind"] == "file":
        if share_hidden_name(share["path"]):
            return None
        return _cover_for_downloads_path(share["path"], share_root_of(share))

    if share["kind"] == "folder":
        full_dir = resolve_media_path(share_root_of(share), share["path"])
        try:
            for entry in sorted(os.scandir(full_dir), key=lambda e: e.name.lower()):
                if (entry.is_file() and not _is_hidden_or_partial(entry.name)
                        and not share_hidden_name(entry.name)):
                    rel = share["path"] + "/" + entry.name
                    result = _cover_for_downloads_path(rel, share_root_of(share))
                    if result:
                        return result
        except (OSError, TypeError):
            pass
        return None

    if share["kind"] == "query":
        for root_id, rel in query_share_files(share)[0][:20]:
            result = _cover_for_downloads_path(rel, root_id)
            if result:
                return result
        return None

    if share["kind"] == "pool":
        for item in share.get("items", []):
            if item["kind"] == "file" and not share_hidden_name(item["path"]):
                result = _cover_for_downloads_path(item["path"], share_root_of(item))
                if result:
                    return result
        return None

    return None

def _serve_cover_result(result):
    if not result:
        return jsonify({"error": "No cover available"}), 404
    file_path, generated = result
    if file_path:
        directory, filename = os.path.split(file_path)
        return send_from_directory(directory, filename)
    mime, data = generated
    resp = app.response_class(data, mimetype=mime)
    resp.headers["Cache-Control"] = "public, max-age=3600"
    return resp

MAX_SHARE_THUMB_BYTES = 8 * 1024 * 1024
SHARE_THUMB_EXTENSIONS = (".jpg", ".jpeg", ".png", ".webp", ".gif")

def share_owner_denied(user, share):
    """True when `user` may not change `share`: only admins and the account that created it may."""
    return not is_admin(user) and share.get("created_by") != user["username"]


@app.route("/api/share/<share_id>/thumbnail", methods=["POST"])
def upload_share_thumbnail(share_id):
    user = get_authenticated_user()
    if not user:
        return jsonify({"error": "Unauthorized"}), 401

    file = request.files.get("thumbnail")
    if not file or not file.filename:
        return jsonify({"error": "No file uploaded"}), 400

    ext = os.path.splitext(file.filename)[1].lower()
    if ext not in SHARE_THUMB_EXTENSIONS:
        return jsonify({"error": "Unsupported image type"}), 400

    file.seek(0, os.SEEK_END)
    size = file.tell()
    file.seek(0)
    if size > MAX_SHARE_THUMB_BYTES:
        return jsonify({"error": "Image too large (max 8MB)"}), 400

    with shares_lock:
        shares = _read_shares_unlocked()
        share = next((s for s in shares if s["id"] == share_id), None)
        if not share:
            return jsonify({"error": "Not found"}), 404

        os.makedirs(SHARE_THUMBS_DIR, exist_ok=True)
        old = share.get("thumbnail")
        filename = f"{share_id}{ext}"
        if old and old != filename:
            try:
                os.remove(os.path.join(SHARE_THUMBS_DIR, old))
            except OSError:
                pass
        file.save(os.path.join(SHARE_THUMBS_DIR, filename))
        share["thumbnail"] = filename
        _write_shares_unlocked(shares)
        result = share_summary(share)

    return jsonify(result)

@app.route("/api/share/<share_id>/thumbnail", methods=["DELETE"])
def delete_share_thumbnail(share_id):
    user = get_authenticated_user()
    if not user:
        return jsonify({"error": "Unauthorized"}), 401

    with shares_lock:
        shares = _read_shares_unlocked()
        share = next((s for s in shares if s["id"] == share_id), None)
        if not share:
            return jsonify({"error": "Not found"}), 404
        if share_owner_denied(user, share):
            return jsonify({"error": "Forbidden"}), 403
        old = share.pop("thumbnail", None)
        if old:
            try:
                os.remove(os.path.join(SHARE_THUMBS_DIR, old))
            except OSError:
                pass
        _write_shares_unlocked(shares)
        result = share_summary(share)

    return jsonify(result)

@app.route("/api/share/<share_id>/cover", methods=["GET"])
def share_cover(share_id):
    if not get_authenticated_user():
        return jsonify({"error": "Unauthorized"}), 401
    share = next((s for s in load_shares() if s["id"] == share_id), None)
    if not share:
        return jsonify({"error": "Not found"}), 404
    return _serve_cover_result(resolve_share_cover(share))

# How long a revoked share stays listed. Revoking already stops the link
# working; the entry lingers only so it's visible that it happened, and
# after a day that's just clutter. The record itself is kept - this hides
# it from the list, it does not delete it.
# --- Share views ---------------------------------------------------------------
# Counted on the share PAGE (/share/<token>), once per load. Link-preview
# crawlers - a link pasted into Discord or WhatsApp fetches the page to unfurl
# it - are counted apart, or every paste would read as a visit. Visitors are
# distinct (address, browser) pairs per day, recognised through a salted hash
# whose salt is random, changes daily and is never written: the stats file holds
# counts only, nothing that identifies a visitor.

SHARE_STATS_FILE = os.path.join(BASE_DIR, "share_stats.json")
LINK_PREVIEW_AGENTS = re.compile(
    r"bot|crawler|spider|preview|facebookexternalhit|slack|discord|whatsapp|telegram|"
    r"embedly|skypeuripreview|curl|wget|python-requests|go-http-client", re.I)
_share_stats_lock = threading.Lock()
_share_stats = {"loaded": False, "data": {}, "dirty": False, "day": None, "salt": b"", "seen": {}}


def _share_stats_data():
    """Caller holds _share_stats_lock."""
    if not _share_stats["loaded"]:
        try:
            with open(SHARE_STATS_FILE, encoding="utf-8") as fh:
                _share_stats["data"] = json.load(fh) or {}
        except (OSError, ValueError):
            _share_stats["data"] = {}
        _share_stats["loaded"] = True
    return _share_stats["data"]


def record_share_view(share_id):
    agent = request.headers.get("User-Agent") or ""
    address = request.remote_addr or ""
    if address in SSO_PROXY_IPS:
        address = (request.headers.get("X-Forwarded-For") or address).split(",")[0].strip()
    today = time.strftime("%Y-%m-%d")
    with _share_stats_lock:
        entry = _share_stats_data().setdefault(
            share_id, {"views": 0, "visitors": 0, "previews": 0, "last_viewed_at": None, "days": {}})
        if not agent or LINK_PREVIEW_AGENTS.search(agent):
            entry["previews"] = entry.get("previews", 0) + 1
        else:
            if _share_stats["day"] != today:
                _share_stats.update(day=today, salt=secrets.token_bytes(16), seen={})
            mark = hashlib.sha256(_share_stats["salt"] + address.encode() + b"|" + agent.encode()).hexdigest()[:20]
            seen = _share_stats["seen"].setdefault(share_id, set())
            if mark not in seen:
                seen.add(mark)
                entry["visitors"] = entry.get("visitors", 0) + 1
            entry["views"] = entry.get("views", 0) + 1
            entry["last_viewed_at"] = time.time()
            days = entry.setdefault("days", {})
            days[today] = days.get(today, 0) + 1
            for old in sorted(days)[:-30]:
                days.pop(old, None)
        _share_stats["dirty"] = True


def flush_share_stats():
    """Writes the counts if anything changed; True when it wrote."""
    with _share_stats_lock:
        if not _share_stats["dirty"]:
            return False
        snapshot = json.dumps(_share_stats["data"], separators=(",", ":"))
        _share_stats["dirty"] = False
    try:
        with open(SHARE_STATS_FILE + ".tmp", "w", encoding="utf-8") as fh:
            fh.write(snapshot)
        os.replace(SHARE_STATS_FILE + ".tmp", SHARE_STATS_FILE)
    except OSError:
        with _share_stats_lock:
            _share_stats["dirty"] = True        # again next round
        raise
    return True


def _share_stats_flush_loop():
    while True:
        time.sleep(60)
        try:
            flush_share_stats()
        except Exception as exc:
            print(f"[Warning] share stats not saved: {exc}", flush=True)


# --- What a share is about ------------------------------------------------------

SHARE_CATEGORY_LABELS = {"character": "Characters", "person": "People", "account": "Accounts",
                         "folder": "Folders", "pool": "Pools", "image": "Images", "video": "Videos",
                         "music": "Music", "file": "Files"}
_share_tags_cache = {}


def share_category(share):
    """Decided from the share itself - no stored field to go stale."""
    kind = share.get("kind")
    if kind == "query":
        return "person" if (share.get("query") or {}).get("type") == "person" else "character"
    if kind == "pool":
        # A music link is a pool of songs (music_share_create): the Shares page
        # has an icon and a name for music, and reading "Pool" there tells
        # nobody what the link plays.
        return "music" if share.get("view") == "music" else "pool"
    path = (share.get("path") or "").strip("/")
    if kind == "folder":
        parts = path.split("/")
        # <site>/<account> - a whole twitter, pixiv or youtube account
        return "account" if len(parts) == 2 and parts[0] in ALL_CATEGORIES else "folder"
    return {"image": "image", "video": "video", "audio": "music"}.get(classify_media_type(path), "file")


def share_detail(share):
    kind = share.get("kind")
    if kind == "pool":
        n = len(share.get("items") or [])
        if share.get("view") == "music":
            what = (share.get("music") or {}).get("kind") or ""
            of = f" from {what}" if what and what not in ("songs", "song") else ""
            return f"{n} song{'' if n == 1 else 's'}{of}"
        return f"{n} item{'' if n == 1 else 's'}"
    if kind == "query":
        q = share.get("query") or {}
        filters = ", ".join(f"{k} {v}" for k, v in sorted((q.get("filters") or {}).items()))
        what = "live character link" if q.get("type") == "character" else "live person link"
        if q.get("text"):
            what += f' narrowed to "{q["text"]}"'
        return what + (f" - {filters}" if filters else "")
    return share.get("path") or ""


# Driven from media through its UNIQUE(root, path) index: the CROSS JOIN fixes
# that join order and the unary + keeps the planner off the category and trashed
# indexes. Left to itself it walked every character tag in the library once per
# share (31 s for a list of 58).
SHARE_TAG_SQL = ("SELECT t.tag AS tag, COUNT(*) AS n FROM media m CROSS JOIN media_tags t ON t.media_id = m.id"
                 " WHERE ({clauses}) AND +m.trashed IS NULL AND +t.category = 'character'"
                 " GROUP BY t.tag ORDER BY n DESC, t.tag LIMIT 3")


def _share_tag_clauses(parts):
    """WHERE terms for [(root, path, is_folder)]: an exact file, or everything
    under a folder as the key range (path/, path0) - '0' sorts right after '/'
    - which the UNIQUE(root, path) index answers; a LIKE on that case-sensitive
    column would read every row."""
    clauses, args = [], []
    for root, path, is_folder in parts:
        path = (path or "").strip("/")
        if is_folder and not path:
            clauses.append("(m.root = ?)")
            args.append(root)
        elif is_folder:
            clauses.append("(m.root = ? AND m.path > ? AND m.path < ?)")
            args += [root, path + "/", path + "0"]
        else:
            clauses.append("(m.root = ? AND m.path = ?)")
            args += [root, path]
    return clauses, args


def _character_tag_counts(conn, parts):
    """Top character tags over [(root, path, is_folder)]."""
    clauses, args = _share_tag_clauses(parts)
    if not clauses:
        return []
    return [(r["tag"], r["n"]) for r in conn.execute(
        SHARE_TAG_SQL.format(clauses=" OR ".join(clauses)), args)]


def share_content_tags(share):
    """Who or what a share shows, from the AI index: a character link's tags, a
    person link's name, and the most frequent characters of a file, folder or
    pool. Cached ten minutes per share."""
    hit = _share_tags_cache.get(share["id"])
    if hit and time.time() - hit[0] < 600:
        return hit[1]
    tags = []
    try:
        q = share.get("query") or {}
        conn = media_ai.connect(BASE_DIR)
        if share.get("kind") == "query" and q.get("type") == "character":
            tags = [t.replace("_", " ") for t in (q.get("tags") or [])[:3]]
        elif share.get("kind") == "query":
            cluster = resolve_person_anchor(conn, q.get("faces") or [])
            row = (conn.execute("SELECT name FROM clusters WHERE id = ?", (cluster,)).fetchone()
                   if cluster is not None else None)
            tags = [row["name"]] if row and row["name"] else ["unnamed person"]
        else:
            parts = []
            if share.get("kind") in ("file", "folder"):
                parts.append((share_root_of(share), share.get("path"), share.get("kind") == "folder"))
            for item in (share.get("items") or [])[:200]:
                parts.append((share_root_of(item), item.get("path"), item.get("kind") == "folder"))
            tags = [t.replace("_", " ") for t, _n in _character_tag_counts(conn, parts)]
    except Exception as exc:
        print(f"[Warning] share content tags: {exc}")
    _share_tags_cache[share["id"]] = (time.time(), tags)
    return tags


@app.route("/api/share/manage", methods=["GET"])
@require_admin
def manage_shares():
    """Every listed share with status, category, what it shows and its views
    (Settings > Shares)."""
    with _share_stats_lock:
        stats = json.loads(json.dumps(_share_stats_data()))
    rows, counts = [], {}
    for share in sorted(load_shares(), key=lambda s: s.get("created_at", 0), reverse=True):
        if not share_listed(share):
            continue
        category = share_category(share)
        counts[category] = counts.get(category, 0) + 1
        seen = stats.get(share["id"]) or {}
        row = share_summary(share)
        row.update({
            "category": category,
            "content_tags": [] if share.get("revoked") else share_content_tags(share),
            "detail": share_detail(share),
            "views": seen.get("views", 0), "visitors": seen.get("visitors", 0),
            "previews": seen.get("previews", 0), "last_viewed_at": seen.get("last_viewed_at"),
        })
        rows.append(row)
    return jsonify({
        "now": time.time(),
        "categories": [{"key": k, "label": label, "count": counts.get(k, 0)}
                       for k, label in SHARE_CATEGORY_LABELS.items()],
        "shares": rows,
    })


def _expiry_value(value):
    """None, or a validated expiry time; raises ValueError."""
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError("expires_at must be a time or null")
    try:
        value = float(value)
    except (TypeError, ValueError):
        raise ValueError("expires_at must be a time or null")
    if value <= 0 or value > time.time() + 20 * 365 * 86400:
        raise ValueError("expires_at is out of range")
    return value


@app.route("/api/share/<share_id>", methods=["PATCH"])
def update_share(share_id):
    """Body: {label?, expires_at?}. An empty label falls back to the default
    name. Admins, or whoever created the share."""
    user = get_authenticated_user()
    if not user:
        return jsonify({"error": "Unauthorized"}), 401
    data = request.get_json(silent=True) or {}
    updates = {}
    if "label" in data:
        label = data.get("label")
        if not isinstance(label, str) or len(label.strip()) > 120:
            return jsonify({"error": "label must be text of at most 120 characters"}), 400
        updates["label"] = label.strip()
    if "expires_at" in data:
        try:
            updates["expires_at"] = _expiry_value(data.get("expires_at"))
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
    if not updates:
        return jsonify({"error": "nothing to change"}), 400
    with shares_lock:
        shares = _read_shares_unlocked()
        share = next((x for x in shares if x["id"] == share_id), None)
        if not share:
            return jsonify({"error": "Not found"}), 404
        if not is_admin(user) and share.get("created_by") != user["username"]:
            return jsonify({"error": "Forbidden"}), 403
        for key, value in updates.items():
            if key == "label" and not value:
                share.pop("label", None)
            else:
                share[key] = value
        _write_shares_unlocked(shares)
        out = share_summary(share)
    _share_tags_cache.pop(share_id, None)
    return jsonify(out)


@app.route("/api/share/bulk", methods=["POST"])
@require_admin
def bulk_shares():
    """Body: {ids: [...], action: "revoke" | "expiry", expires_at?}."""
    data = request.get_json(silent=True) or {}
    ids, action = data.get("ids"), data.get("action")
    if (not isinstance(ids, list) or not ids or len(ids) > 1000
            or not all(isinstance(i, str) for i in ids)):
        return jsonify({"error": "ids must be a list of share ids"}), 400
    if action not in ("revoke", "expiry"):
        return jsonify({"error": "action must be revoke or expiry"}), 400
    expires_at = None
    if action == "expiry":
        try:
            expires_at = _expiry_value(data.get("expires_at"))
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
    wanted, changed, now = set(ids), 0, time.time()
    with shares_lock:
        shares = _read_shares_unlocked()
        for share in shares:
            if share["id"] not in wanted:
                continue
            if action == "revoke":
                if not share.get("revoked"):
                    share["revoked"], share["revoked_at"] = True, now
                    changed += 1
            else:
                share["expires_at"] = expires_at
                changed += 1
        _write_shares_unlocked(shares)
    print(f"[Info] {action} on {changed} shares by {get_authenticated_user()['username']}", flush=True)
    return jsonify({"changed": changed})


REVOKED_SHARE_VISIBLE_SECONDS = 24 * 3600

def share_listed(share):
    if not share.get("revoked"):
        return True
    revoked_at = share.get("revoked_at")
    # Revoked before revocation times were recorded, so it can only be
    # older than the window.
    if not revoked_at:
        return False
    return time.time() - revoked_at < REVOKED_SHARE_VISIBLE_SECONDS

@app.route("/api/share", methods=["GET"])
def list_shares():
    if not get_authenticated_user():
        return jsonify({"error": "Unauthorized"}), 401
    shares = sorted(load_shares(), key=lambda s: s.get("created_at", 0), reverse=True)
    return jsonify([share_summary(s) for s in shares if share_listed(s)])

@app.route("/api/share", methods=["POST"])
def create_share():
    """Shares a single existing file or folder. Body: {"path": "..."}."""
    user = get_authenticated_user()
    if not user:
        return jsonify({"error": "Unauthorized"}), 401

    data = request.get_json(silent=True) or {}
    rel_path = (data.get("path") or "").strip("/").replace("\\", "/")
    root_id = (data.get("root") or DEFAULT_ROOT_ID).strip()
    if not rel_path:
        return jsonify({"error": "path is required"}), 400
    if not root_visible_to(user, root_id):
        return jsonify({"error": "Forbidden"}), 403

    target = _resolve_downloads_subpath(rel_path, root_id)
    if not target:
        return jsonify({"error": "Invalid path"}), 400
    if not os.path.exists(target):
        return jsonify({"error": "Not found"}), 404
    if os.path.isfile(target) and share_hidden_name(rel_path):
        return jsonify({"error": "Screenshots are not shared"}), 400

    share = {
        "id": secrets.token_urlsafe(24),
        "kind": "folder" if os.path.isdir(target) else "file",
        "path": rel_path,
        "root": root_id,
        "created_by": user["username"],
        "created_at": time.time(),
        "expires_at": default_share_expiry(),
        "revoked": False
    }

    with shares_lock:
        shares = _read_shares_unlocked()
        shares.append(share)
        _write_shares_unlocked(shares)

    return jsonify(share_summary(share))

@app.route("/api/share/pool", methods=["POST"])
def create_share_pool():
    """Creates an empty pool share. Items are added afterward via
    /api/share/<id>/items. Body: {"label": "..."} (optional)."""
    user = get_authenticated_user()
    if not user:
        return jsonify({"error": "Unauthorized"}), 401

    data = request.get_json(silent=True) or {}
    label = (data.get("label") or "").strip()

    share = {
        "id": secrets.token_urlsafe(24),
        "kind": "pool",
        "label": label,
        "items": [],
        "created_by": user["username"],
        "created_at": time.time(),
        "expires_at": default_share_expiry(),
        "revoked": False
    }

    with shares_lock:
        shares = _read_shares_unlocked()
        shares.append(share)
        _write_shares_unlocked(shares)

    return jsonify(share_summary(share))

@app.route("/api/share/<share_id>/items", methods=["POST"])
def add_share_pool_item(share_id):
    user = get_authenticated_user()
    if not user:
        return jsonify({"error": "Unauthorized"}), 401

    data = request.get_json(silent=True) or {}
    rel_path = (data.get("path") or "").strip("/").replace("\\", "/")
    root_id = (data.get("root") or DEFAULT_ROOT_ID).strip()
    if not rel_path:
        return jsonify({"error": "path is required"}), 400
    if not root_visible_to(user, root_id):
        return jsonify({"error": "Forbidden"}), 403

    target = _resolve_downloads_subpath(rel_path, root_id)
    if not target:
        return jsonify({"error": "Invalid path"}), 400
    if not os.path.exists(target):
        return jsonify({"error": "Not found"}), 404
    item_kind = "folder" if os.path.isdir(target) else "file"
    if item_kind == "file" and share_hidden_name(rel_path):
        return jsonify({"error": "Screenshots are not shared"}), 400

    with shares_lock:
        shares = _read_shares_unlocked()
        share = next((s for s in shares if s["id"] == share_id), None)
        if not share or share["kind"] != "pool":
            return jsonify({"error": "Not a pool share"}), 404
        if share_owner_denied(user, share):
            return jsonify({"error": "Forbidden"}), 403
        if not any(i["path"] == rel_path and share_root_of(i) == root_id for i in share["items"]):
            share["items"].append({"path": rel_path, "kind": item_kind, "root": root_id})
        _write_shares_unlocked(shares)
        result = share_summary(share)

    return jsonify(result)

@app.route("/api/share/<share_id>/items", methods=["DELETE"])
def remove_share_pool_item(share_id):
    user = get_authenticated_user()
    if not user:
        return jsonify({"error": "Unauthorized"}), 401

    data = request.get_json(silent=True) or {}
    rel_path = (data.get("path") or "").strip("/").replace("\\", "/")

    with shares_lock:
        shares = _read_shares_unlocked()
        share = next((s for s in shares if s["id"] == share_id), None)
        if not share or share["kind"] != "pool":
            return jsonify({"error": "Not a pool share"}), 404
        if share_owner_denied(user, share):
            return jsonify({"error": "Forbidden"}), 403
        share["items"] = [i for i in share["items"] if i["path"] != rel_path]
        _write_shares_unlocked(shares)
        result = share_summary(share)

    return jsonify(result)

@app.route("/api/share/<share_id>", methods=["DELETE"])
def revoke_share(share_id):
    user = get_authenticated_user()
    if not user:
        return jsonify({"error": "Unauthorized"}), 401

    with shares_lock:
        shares = _read_shares_unlocked()
        share = next((s for s in shares if s["id"] == share_id), None)
        if not share:
            return jsonify({"error": "Not found"}), 404
        if share_owner_denied(user, share):
            return jsonify({"error": "Forbidden"}), 403
        # Revoking again must not restart the 24h it stays listed for.
        if not share.get("revoked"):
            share["revoked"] = True
            share["revoked_at"] = time.time()
        _write_shares_unlocked(shares)

    return jsonify({"success": True})

# --- Live shares: a character or a person, with the filters that were active --

# In parse_media_filters' order - the two are zipped together.
QUERY_SHARE_FILTER_KEYS = ("orientation", "min_hres", "min_vres", "min_res", "kind",
                           "min_fps", "max_fps", "content")
QUERY_SHARE_TTL_S = 60          # how long a resolved file list is reused
PERSON_ANCHOR_FACES = 300       # faces a person link remembers the person by
PERSON_ANCHOR_MIN_SHARE = 0.3   # of those still present, how many one cluster must hold
_query_share_cache = {}
_query_share_lock = threading.Lock()
_query_share_building = {}      # share id -> lock held while its list is computed
_listed_paths_cache = {"index": None, "set": None}


def resolve_person_anchor(conn, anchors):
    """The face cluster that is this person NOW, or None.

    Cluster ids are renumbered on every regroup (media_faces.persist), so a
    link cannot keep one: after the next regroup it would open on whoever got
    that number. It keeps the faces it was made from and follows them. If no
    single cluster still holds a good part of them the link shows nothing -
    an empty link is a nuisance, a link to a different person is a disclosure.
    """
    counts, existing = {}, 0
    for i in range(0, len(anchors), 500):
        chunk = [int(a) for a in anchors[i:i + 500]]
        holes = ",".join("?" * len(chunk))
        for r in conn.execute(
                f"SELECT cluster_id AS cid, COUNT(*) AS n FROM faces"
                f" WHERE id IN ({holes}) GROUP BY cluster_id", chunk):
            existing += r["n"]
            if r["cid"] is not None:
                counts[r["cid"]] = counts.get(r["cid"], 0) + r["n"]
    if not counts or not existing:
        return None
    cid, n = max(counts.items(), key=lambda kv: kv[1])
    return cid if n >= PERSON_ANCHOR_MIN_SHARE * existing else None


def listed_media_paths():
    """{(root, path)} of every file in the path index, built once per index
    build: each live-share recompute checks membership against it, and making
    it is a pass over ~540k rows."""
    index = get_media_index()
    with _query_share_lock:
        if _listed_paths_cache["index"] is index:
            return _listed_paths_cache["set"]
    listed = {(row[0], row[1]) for row in index if not row[2]}
    with _query_share_lock:
        _listed_paths_cache["index"], _listed_paths_cache["set"] = index, listed
    return listed


def query_share_files(share):
    """([(root, path)] in display order, the same as a set) for a live share.

    Recomputed at most once a minute per share: every thumbnail and file
    request of the public page checks membership against it. The rules are
    the logged-in search's own - not trashed, present in the path index (so a
    video's poster companion stays hidden), the stored filters - plus the
    roots its creator could see when the link was made.

    Only ever computed by one thread per share. A page load asks for this
    list from the page, its info, items and cover and every thumbnail at
    once; on a cold cache each of them used to compute it in parallel - 25 s
    for a normal page load on 2026-09-13, over a minute when the path index
    had expired as well. Now the first computes and the rest wait for it, and
    once a list exists an expired one is served at once while a background
    thread refreshes it, so a visitor never waits for the recompute.
    """
    sid = share["id"]
    with _query_share_lock:
        hit = _query_share_cache.get(sid)
        build_lock = _query_share_building.setdefault(sid, threading.Lock())
    if hit:
        if (time.time() - hit[0] >= QUERY_SHARE_TTL_S
                and build_lock.acquire(blocking=False)):
            try:
                threading.Thread(target=_refresh_query_share,
                                 args=(share, build_lock), daemon=True).start()
            except Exception:
                build_lock.release()
        return hit[1], hit[2]
    with build_lock:
        with _query_share_lock:
            hit = _query_share_cache.get(sid)
        if hit:
            return hit[1], hit[2]
        return _compute_query_share(share)


def _refresh_query_share(share, build_lock):
    """The background half of query_share_files. A failure keeps serving the
    list it already had."""
    try:
        _compute_query_share(share)
    except Exception as exc:
        print(f"[Warning] live share not refreshed: {exc}")
    finally:
        build_lock.release()


def _compute_query_share(share):
    now = time.time()
    q = share.get("query") or {}
    pairs = []
    try:
        conn = media_ai.connect(BASE_DIR)
        if q.get("type") == "character" and q.get("tags"):
            pairs = media_ai.files_for_tag(conn, q["tags"], limit=100000)
        elif q.get("type") == "person" and q.get("faces"):
            cid = resolve_person_anchor(conn, q["faces"])
            if cid is not None:
                pairs = [(r["root"], r["path"]) for r in conn.execute(
                    "SELECT m.root AS root, m.path AS path, MAX(f.quality) AS best"
                    " FROM faces f JOIN media m ON m.id = f.media_id"
                    " WHERE f.cluster_id = ? AND m.trashed IS NULL"
                    " GROUP BY m.root, m.path ORDER BY best DESC", (cid,))]
    except Exception as exc:
        print(f"[Warning] live share not resolved: {exc}")
        pairs = []
    if q.get("text"):
        keep = narrow_to_words(set(pairs), q["text"], q.get("mode") or "filename")
        pairs = [p for p in pairs if p in keep]

    roots = set(share.get("roots") or [])
    listed = listed_media_paths()
    orientation, min_w, min_h, min_any, kinds, min_fps, max_fps, content = parse_media_filters(
        q.get("filters") or {})
    dims_needed = any((orientation, min_w, min_h, min_any, min_fps, max_fps))
    dims_map = media_dimensions_map() if dims_needed else {}
    levels = content_levels_map() if content else None
    out = []
    for root, path in pairs:
        if root not in roots or (root, path) not in listed:
            continue
        if share_hidden_name(path):
            continue
        if not kind_matches(path.rsplit("/", 1)[-1], kinds):
            continue
        if content and not content_matches(levels, content, root, path):
            continue
        if dims_needed and not media_dimensions.matches(
                dims_map.get(media_dimensions.key_for(root, path)),
                orientation, min_w, min_h, min_any, min_fps, max_fps):
            continue
        out.append((root, path))
    member = set(out)
    with _query_share_lock:
        _query_share_cache[share["id"]] = (now, out, member)
    return out, member


@app.route("/api/share/query", methods=["POST"])
def create_query_share():
    """A LIVE share of a character pool or a person, with the active filters.

    Body: {"type": "character", "tags": [...]} or {"type": "person",
    "person": <cluster id>}, plus "filters" and "label". Live rather than a
    snapshot: the link shows whatever matches when it is opened, so new
    downloads of that character appear without a new link - a fixed set is
    what a pool already is. The filters are stored with the share, a visitor
    cannot change them, and it only reaches the roots its creator could see.
    """
    user = get_authenticated_user()
    if not user:
        return jsonify({"error": "Unauthorized"}), 401
    data = request.get_json(silent=True) or {}
    qtype = (data.get("type") or "").strip()

    raw = data.get("filters") or {}
    if not isinstance(raw, dict):
        return jsonify({"error": "filters must be an object"}), 400
    filters = {k: str(v).strip() for k, v in raw.items()
               if k in QUERY_SHARE_FILTER_KEYS and v not in (None, "", False)}
    parsed = dict(zip(QUERY_SHARE_FILTER_KEYS, parse_media_filters(filters)))
    bad = [k for k in filters
           if not parsed.get(k) or (k == "kind" and None in parsed[k])]
    if bad:
        return jsonify({"error": "unknown filter value(s): " + ", ".join(bad)}), 400

    # Words narrowing the subject travel with the link, or it would show more
    # than the page it was shared from.
    text = data.get("text")
    if text is not None and not isinstance(text, str):
        return jsonify({"error": "text must be a string"}), 400
    text = (text or "").strip()
    if len(text) > 200:
        return jsonify({"error": "text is too long"}), 400
    mode = data.get("mode") or "filename"
    if not isinstance(mode, str) or mode.strip().lower() not in SEARCH_MODES:
        return jsonify({"error": "unknown search type"}), 400
    mode = mode.strip().lower()

    conn = media_ai.connect(BASE_DIR)
    query = {"type": qtype, "filters": filters}
    if text.replace("*", " ").strip():
        query["text"], query["mode"] = text, mode
    if qtype == "character":
        tags = [t.strip() for t in (data.get("tags") or [])
                if isinstance(t, str) and t.strip()][:50]
        if not tags:
            return jsonify({"error": "tags are required"}), 400
        holes = ",".join("?" * len(tags))
        if not conn.execute(f"SELECT 1 FROM media_tags WHERE tag IN ({holes}) LIMIT 1",
                            tags).fetchone():
            return jsonify({"error": "No such character"}), 404
        query["tags"] = tags
    elif qtype == "person":
        try:
            cluster_id = int(data.get("person"))
        except (TypeError, ValueError):
            return jsonify({"error": "person is required"}), 400
        anchors = [r["id"] for r in conn.execute(
            "SELECT id FROM faces WHERE cluster_id = ? ORDER BY quality DESC LIMIT ?",
            (cluster_id, PERSON_ANCHOR_FACES))]
        if not anchors:
            return jsonify({"error": "No such person"}), 404
        query["faces"] = anchors
    else:
        return jsonify({"error": "type must be character or person"}), 400

    share = {
        "id": secrets.token_urlsafe(24),
        "kind": "query",
        "label": (data.get("label") or "").strip()[:120],
        "query": query,
        "roots": sorted(r["id"] for r in roots_for_user(user)),
        "created_by": user["username"],
        "created_at": time.time(),
        "expires_at": default_share_expiry(),
        "revoked": False,
    }
    with shares_lock:
        shares = _read_shares_unlocked()
        shares.append(share)
        _write_shares_unlocked(shares)
    out = share_summary(share)
    out["total"] = len(query_share_files(share)[0])
    return jsonify(out)

# --- Public (no-login) endpoints - gated only by the share token itself ---

@app.route("/api/public/share/<token>", methods=["GET"])
def public_share_info(token):
    share = find_share(token)
    if not share:
        stale = find_share_including_expired(token)
        if stale and share_expired(stale):
            # Said as such, so the page can tell a visitor the link ran out
            # rather than that it never existed.
            return jsonify({"error": "expired"}), 410
        return jsonify({"error": "Not found"}), 404

    out = share_summary(share)
    out["cover_url"] = f"/api/public/share/{token}/cover?v={cover_version(share)}"

    def public_urls(entry, rel_path, root_id):
        q = f"path={quote(rel_path, safe='')}&root={quote(root_id, safe='')}"
        entry["url"] = f"/api/public/share/{token}/file?{q}"
        entry["thumb_url"] = f"/api/public/share/{token}/thumbnail?{q}&v={thumb_version(entry)}"
        return entry

    if share["kind"] == "file":
        share_root = share_root_of(share)
        full = resolve_media_path(share_root, share["path"])
        if not full or not os.path.isfile(full) or share_hidden_name(share["path"]):
            return jsonify({"error": "File no longer exists"}), 404
        entry = _media_file_entry(
            os.path.dirname(share["path"]), os.path.basename(share["path"]), os.stat(full), share_root
        )
        attach_media_dimensions([entry], share_root)
        out["file"] = public_urls(entry, share["path"], share_root)

    elif share["kind"] == "pool":
        items = []
        for item in share.get("items", []):
            item_root = share_root_of(item)
            full = resolve_media_path(item_root, item["path"])
            if not full:
                continue
            if item["kind"] == "file" and share_hidden_name(item["path"]):
                continue
            if item["kind"] == "file" and os.path.isfile(full):
                entry = _media_file_entry(
                    os.path.dirname(item["path"]), os.path.basename(item["path"]), os.stat(full), item_root
                )
                attach_media_dimensions([entry], item_root)
                items.append(public_urls(entry, item["path"], item_root))
            elif item["kind"] == "folder" and os.path.isdir(full):
                items.append({
                    "kind": "folder",
                    "name": os.path.basename(item["path"].rstrip("/")) or item["path"],
                    "path": item["path"],
                    "root": item_root
                })
        out["items"] = items

    elif share["kind"] == "query":
        # A live share can hold a thousand files; the page asks for them in
        # pages through /items rather than receiving them all here.
        out["total"] = len(query_share_files(share)[0])

    return jsonify(out)

@app.route("/api/public/share/<token>/items", methods=["GET"])
def public_share_items(token):
    """One page of a live share's files, with public URLs. Paged because a
    character can match a thousand files, shown as they are scrolled to."""
    share = find_share(token)
    if not share or share["kind"] != "query":
        return jsonify({"error": "Not found"}), 404
    try:
        offset = max(0, int(request.args.get("offset", 0)))
    except ValueError:
        offset = 0
    try:
        limit = min(100, max(1, int(request.args.get("limit", 60))))
    except ValueError:
        limit = 60
    files = query_share_files(share)[0]
    page = build_entries_for(files[offset:offset + limit])
    # The share viewer is a gallery too: tiles are laid out by aspect ratio.
    attach_media_dimensions(page)
    for e in page:
        root_id = e.get("root") or DEFAULT_ROOT_ID
        q = f"path={quote(e['path'], safe='')}&root={quote(root_id, safe='')}"
        e["url"] = f"/api/public/share/{token}/file?{q}"
        e["thumb_url"] = f"/api/public/share/{token}/thumbnail?{q}&v={thumb_version(e)}"
    return jsonify({"items": page, "offset": offset, "limit": limit,
                    "total": len(files), "has_more": offset + limit < len(files)})

@app.route("/api/public/share/<token>/cover", methods=["GET"])
def public_share_cover(token):
    share = find_share(token)
    if not share:
        return jsonify({"error": "Not found"}), 404
    return _serve_cover_result(resolve_share_cover(share))

@app.route("/api/public/share/<token>/browse", methods=["GET"])
def public_share_browse(token):
    """Directory listing for a folder-kind share, or for a folder item
    inside a pool share - restricted to that subtree only."""
    share = find_share(token)
    if not share:
        return jsonify({"error": "Not found"}), 404

    rel_path = request.args.get("path", "").strip("/").replace("\\", "/")
    root_id = (request.args.get("root") or "").strip() or share_root_of(share)
    if not rel_path:
        if share["kind"] == "folder":
            rel_path = share["path"]
            root_id = share_root_of(share)
        else:
            return jsonify({"error": "path is required"}), 400

    if not path_allowed_by_share(share, rel_path, root_id):
        return jsonify({"error": "Forbidden"}), 403

    target_dir = _resolve_downloads_subpath(rel_path, root_id)
    if not target_dir or not os.path.isdir(target_dir):
        return jsonify({"error": "Not found"}), 404

    try:
        dirs, files = _scan_media_dir(target_dir, rel_path, root_id)
    except OSError as e:
        return jsonify({"error": str(e)}), 500

    files = [f for f in files if not share_hidden_name(f["path"])]
    attach_media_dimensions(files, root_id)
    for f in files:
        q = f"path={quote(f['path'], safe='')}&root={quote(root_id, safe='')}"
        f["url"] = f"/api/public/share/{token}/file?{q}"
        f["thumb_url"] = f"/api/public/share/{token}/thumbnail?{q}&v={thumb_version(f)}"

    return jsonify({"path": rel_path, "root": root_id, "dirs": dirs, "files": files})

@app.route("/api/public/share/<token>/thumbnail", methods=["GET"])
def public_share_thumbnail(token):
    share = find_share(token)
    if not share:
        return jsonify({"error": "Not found"}), 404

    rel_path = request.args.get("path", "")
    root_id = (request.args.get("root") or "").strip() or share_root_of(share)
    if not path_allowed_by_share(share, rel_path, root_id):
        return jsonify({"error": "Forbidden"}), 403

    file_path = _resolve_downloads_subpath(rel_path, root_id)
    if not file_path or not os.path.isfile(file_path):
        return jsonify({"error": "Not found"}), 404

    cover = get_file_thumbnail(file_path)
    if not cover:
        return jsonify({"error": "No embedded thumbnail"}), 404

    mime, data = cover
    resp = app.response_class(data, mimetype=mime)
    resp.headers["Cache-Control"] = "public, max-age=86400"
    return resp

@app.route("/api/public/share/<token>/info", methods=["GET"])
def public_share_file_info(token):
    """The info panel's data for one shared file.

    Everything the logged-in panel shows EXCEPT where the file lives: no
    path, no root, no folder. Whoever holds a share link is not being shown
    around the library, so the location is dropped here on the server rather
    than merely left unrendered - the browser is never sent it at all.
    """
    share = find_share(token)
    if not share:
        return jsonify({"error": "Not found"}), 404

    rel_path = request.args.get("path", "").strip("/").replace("\\", "/")
    root_id = (request.args.get("root") or "").strip() or share_root_of(share)
    if not path_allowed_by_share(share, rel_path, root_id):
        return jsonify({"error": "Forbidden"}), 403

    file_path = _resolve_downloads_subpath(rel_path, root_id)
    if not file_path or not os.path.isfile(file_path):
        return jsonify({"error": "Not found"}), 404

    stat = os.stat(file_path)
    info = {
        "name": os.path.basename(rel_path),
        "type": classify_media_path(file_path),
        "size": stat.st_size,
        "modified": stat.st_mtime,
        "added": file_added_time(stat),
    }
    info.update(probe_media_details(file_path))
    # The post and account are public on the site itself; only the library
    # path they are derived from stays on the server.
    source = source_links_for(root_id, rel_path)
    if source:
        info["source"] = source
    # probe_media_details reports only technical/tag data, but strip the
    # location keys explicitly anyway: this is the one endpoint where a
    # future addition leaking a path would be a real disclosure, and a
    # filter that is already there cannot be forgotten.
    for key in ("path", "folder", "root", "root_name"):
        info.pop(key, None)
    return jsonify(info)

def share_upscale_enabled():
    """Live upscaling on share pages: both "Upscale images you keep looking at"
    and "Also on shared links" in Settings."""
    cfg = upscale_settings()
    return bool(cfg.get("enabled")) and bool(cfg.get("shares"))


@app.route("/api/public/share/<token>/upscale/config", methods=["GET"])
def public_share_upscale_config(token):
    """media_upscale_config for a share page."""
    if not find_share(token):
        return jsonify({"error": "Not found"}), 404
    cfg = upscale_settings()
    return jsonify({"enabled": share_upscale_enabled(), "view_seconds": cfg.get("view_seconds"),
                    "zoom_factor": cfg.get("zoom_factor"), "model_labels": UPSCALE_MODEL_LABELS})


@app.route("/api/public/share/<token>/upscale", methods=["POST"])
def public_share_upscale(token):
    """media_upscale for whoever holds a share link: only a file the share
    covers, and only while share_upscale_enabled(). The job goes on the
    library's own queue - one at a time, newest first, UPSCALE_QUEUE_MAX - so a
    visitor's picture waits its turn like anyone's, and a copy made for either
    serves both."""
    share = find_share(token)
    if not share:
        return jsonify({"error": "Not found"}), 404
    data = request.get_json(silent=True) or {}
    rel_path = (data.get("path") or "").strip("/").replace("\\", "/")
    root_id = (data.get("root") or "").strip() or share_root_of(share)
    if not rel_path or not path_allowed_by_share(share, rel_path, root_id):
        return jsonify({"error": "Forbidden"}), 403
    if not share_upscale_enabled():
        return jsonify({"status": "disabled"})
    answer, code = upscale_answer(root_id, rel_path, data,
                                  lambda key: f"/api/public/share/{token}/upscaled?key={key}")
    return jsonify(answer), code


@app.route("/api/public/share/<token>/upscaled", methods=["GET"])
def public_share_upscaled(token):
    """An upscaled copy, for a share page: only of a file the share covers.
    A key for anything else is "not found", not "forbidden" - a link holder
    learns nothing about what else has been upscaled."""
    share = find_share(token)
    if not share or not share_upscale_enabled():
        return jsonify({"error": "Not found"}), 404
    key = (request.args.get("key") or "").strip()
    if not re.fullmatch(r"[0-9a-f]{40}", key):
        return jsonify({"error": "bad key"}), 400
    entry = _touch_upscale(key)
    if not entry or not path_allowed_by_share(share, entry["path"], entry["root"]):
        return jsonify({"error": "Not found"}), 404
    path = os.path.join(upscale_cache_dir(), entry["file"])
    if not os.path.isfile(path):
        return jsonify({"error": "Not found"}), 404
    resp = send_file(path, conditional=True)
    resp.headers["Cache-Control"] = "private, max-age=86400"
    return resp


def _share_cached_source(token):
    share = find_share(token)
    if not share:
        return None, None, None, (jsonify({"error": "Not found"}), 404)
    root = (request.args.get("root") or "").strip() or share_root_of(share)
    path = (request.args.get("path") or "").strip("/").replace("\\", "/")
    if not path or not path_allowed_by_share(share, path, root):
        return None, None, None, (jsonify({"error": "Forbidden"}), 403)
    source = resolve_media_path(root, path)
    if not source or not os.path.isfile(source):
        return None, None, None, (jsonify({"error": "Not found"}), 404)
    return root, path, source, None


@app.route("/api/public/share/<token>/3d/list")
def public_share_3d_list(token):
    share = find_share(token)
    if not share:
        return jsonify({"error": "Not found"}), 404
    rows = stereo3d_service().list_entries(lambda root: True)
    rows = [row for row in rows if path_allowed_by_share(share, row["path"], row["root"])]
    for row in rows:
        for field in ("sbs_url", "depth_url"):
            row[field] = row[field].replace("/api/media/3d/file", f"/api/public/share/{token}/3d/file")
    return jsonify({"enabled": True, "items": rows})


@app.route("/api/public/share/<token>/3d/status")
def public_share_3d_status(token):
    root, path, source, error = _share_cached_source(token)
    if error is not None:
        return error
    result = stereo3d_service().status_for(root, path)
    if result.get("state") != "done":
        return jsonify({"state": "none"})
    for field in ("sbs_url", "depth_url"):
        result[field] = result[field].replace("/api/media/3d/file", f"/api/public/share/{token}/3d/file")
    return jsonify(result)


@app.route("/api/public/share/<token>/3d/file")
def public_share_3d_file(token):
    share = find_share(token)
    if not share:
        return jsonify({"error": "Not found"}), 404
    svc = stereo3d_service()
    key = request.args.get("key", "")
    folder, entry = svc.entry_files(key)
    name = request.args.get("name", "")
    if (not entry or not path_allowed_by_share(share, entry["path"], entry["root"])
            or name not in stereo3d.RESULT_NAMES[entry["kind"]]):
        return jsonify({"error": "Not found"}), 404
    if name.startswith("sbs."):
        svc.note_play(key, request.headers.get("Range"))
    response = send_file(os.path.join(folder, name), conditional=True, max_age=0)
    response.headers["Cache-Control"] = "private, no-store"
    return response


@app.route("/api/public/share/<token>/interp/config")
def public_share_interp_config(token):
    if not find_share(token):
        return jsonify({"error": "Not found"}), 404
    cfg = interp_settings()
    return jsonify({"enabled": True, "available": True, "cache_only": True,
                    "bitrates_mbps": cfg.get("bitrates_mbps") or [0],
                    "default_mbps": cfg.get("default_mbps", 20),
                    "max_output_fps": cfg.get("max_output_fps") or 240})


@app.route("/api/public/share/<token>/interp/seg")
def public_share_interp_segment(token):
    root, path, source, error = _share_cached_source(token)
    if error is not None:
        return error
    name = request.args.get("name") or ""
    if not INTERP_SEG_NAME.fullmatch(name):
        return jsonify({"error": "bad name"}), 400
    if classify_media_path(source) != "video":
        return jsonify({"error": "not a video"}), 400
    cfg = interp_settings()
    try:
        mbps = float(request.args.get("mbps") or 0)
        if not math.isfinite(mbps) or not 0 <= mbps <= 1000:
            raise ValueError("bad mbps")
        rate, _, _ = interp_output_rate(cfg, probe_media_details(source).get("fps") or 0, request.args)
    except (ValueError, LookupError, OverflowError):
        return jsonify({"error": "bad parameters"}), 400
    stat = os.stat(source)
    key = hashlib.sha256(f"{root}\n{path}\n{stat.st_size}\n{int(stat.st_mtime)}".encode()).hexdigest()[:32]
    variant = f"{rate}_h{int(cfg.get('max_height') or 2160)}_r{mbps:g}"
    try:
        data, ctype = _interp_seg_fetch_raw(f"/v1/interp/cached/{key}/{variant}/{name}")
    except urllib.error.HTTPError as exc:
        return jsonify({"error": "Cached rendition unavailable"}), exc.code
    except (OSError, RuntimeError):
        return jsonify({"error": "Cached rendition unavailable"}), 502
    return Response(data, mimetype=ctype.split(";")[0], headers={"Cache-Control": "private, no-store"})


@app.route("/api/public/share/<token>/file", methods=["GET"])
def public_share_file(token):
    share = find_share(token)
    if not share:
        return "404 Not Found", 404

    rel_path = request.args.get("path", "")
    root_id = (request.args.get("root") or "").strip() or share_root_of(share)
    if not path_allowed_by_share(share, rel_path, root_id):
        return "403 Forbidden", 403

    file_path = _resolve_downloads_subpath(rel_path, root_id)
    if not file_path or not os.path.isfile(file_path):
        return "404 Not Found", 404

    return send_media_file(file_path)

def external_base_url():
    """Scheme+host as the outside world sees it. TLS is terminated by a
    proxy in front of this app, so request.scheme alone would report http
    and produce link-preview URLs that crawlers can't use."""
    proto = request.headers.get("X-Forwarded-Proto", request.scheme)
    host = request.headers.get("X-Forwarded-Host", request.host)
    return f"{proto}://{host}"

def share_preview_meta(share, token):
    """Open Graph / Twitter Card tags describing a share.

    Link-preview crawlers (Discord's included) do not run JavaScript, and
    public_share.html is a JS shell - so without these rendered into the
    server's response there is nothing for them to read but the generic
    page title, which is why shared links unfurled blank."""
    base = external_base_url()
    label = share_label(share)
    kind = share["kind"]
    music = share.get("view") == "music"

    if music:
        description, og_type = music_share_preview(share, token)
    elif kind == "pool":
        count = len(share.get("items", []))
        description = f"{count} item{'' if count == 1 else 's'}"
    elif kind == "folder":
        description = "Shared folder"
    elif kind == "query":
        count = len(query_share_files(share)[0])
        description = f"{count} file{'' if count == 1 else 's'}"
    else:
        # Repeating the filename (already the title) tells the reader
        # nothing - say what the thing actually is instead.
        media_type = share_media_type(share)
        noun = {"image": "Image", "video": "Video", "audio": "Audio"}.get(media_type, "File")
        full = resolve_media_path(share_root_of(share), share.get("path", ""))
        try:
            size = os.path.getsize(full) if full else 0
        except OSError:
            size = 0
        if size >= 1024 ** 3:
            human = f"{size / 1024 ** 3:.1f} GB"
        elif size >= 1024 ** 2:
            human = f"{size / 1024 ** 2:.1f} MB"
        elif size:
            human = f"{size / 1024:.0f} KB"
        else:
            human = ""
        description = f"{noun} · {human}" if human else noun

    cover_url = f"{base}/api/public/share/{token}/cover"
    # Ordered dict, not a list: later entries must be able to *replace*
    # earlier ones (a video share overrides og:type), and emitting the same
    # property twice leaves crawlers to pick one arbitrarily.
    tags = {
        "og:site_name": "Music" if music else "Media Library",
        "og:type": og_type if music else "website",
        "og:title": label,
        "og:description": description,
        "og:url": f"{base}/share/{token}",
        "og:image": cover_url,
        "og:image:alt": label,
        # Without this Discord renders a small square thumbnail off to the
        # side instead of a full-width image.
        "twitter:card": "summary_large_image",
        "twitter:title": label,
        "twitter:description": description,
        "twitter:image": cover_url,
    }

    # A single shared video can play inline in the embed rather than being
    # just a link with a poster.
    if kind == "file" and share_media_type(share) == "video":
        rel = share["path"]
        q = f"path={quote(rel, safe='')}&root={quote(share_root_of(share), safe='')}"
        video_url = f"{base}/api/public/share/{token}/file?{q}"
        ext = os.path.splitext(rel)[1].lower()
        if is_ugoira_zip(resolve_media_path(share_root_of(share), rel) or ""):
            ext = ".mp4"
        tags.update({
            "og:type": "video.other",
            "og:video": video_url,
            "og:video:secure_url": video_url,
            "og:video:type": "video/mp4" if ext == ".mp4" else "video/webm",
        })

    lines = [
        # escape() with quote=True: labels are user-supplied, so a quote in
        # a share name would otherwise break out of the content attribute.
        f'<meta property="{key}" content="{html_escape(value, quote=True)}">'
        for key, value in tags.items()
    ]
    # Discord tints the embed's left border with theme-color. The music viewer
    # has one of its own (the phone's browser bar takes it), and a second tag
    # leaves each reader to pick one.
    if not music:
        lines.append('<meta name="theme-color" content="#38bdf8">')
    return "\n  ".join(lines)


# What a link preview says about a music link: the share page's own header in
# one line (user, 2026-09-23: "A music share should expose a preview to
# discord") - "Playlist · Owner · 42 songs · 2 hr 13 min", "Song · Artist ·
# 3:41" - where it had said "Media Library" and "1 item". The picture stays
# /cover: a cover chosen in Settings > Shares wins there, else the first song's.
MUSIC_SHARE_KINDS = {"playlist": "Playlist", "album": "Album", "artist": "Artist",
                     "style": "Style", "song": "Song"}
MUSIC_OG_TYPES = {"song": "music.song", "album": "music.album", "playlist": "music.playlist"}


def music_share_preview(share, token):
    """(description, og:type) of a music link, from the songs it still holds."""
    music = share.get("music") or {}
    kind = music.get("kind") or "songs"
    subtitle = (music.get("subtitle") or "").strip()
    songs = _music_share_songs(share, token)
    total = sum(s["duration"] for s in songs)
    parts = [MUSIC_SHARE_KINDS.get(kind, "")]
    if kind == "song":
        parts.append(subtitle or (songs[0]["artist"] if songs else ""))
        if total:
            h, m, s = total // 3600, total // 60 % 60, total % 60
            parts.append(f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}")
    else:
        parts.append(subtitle)
        parts.append(f"{len(songs):,} song{'' if len(songs) == 1 else 's'}")
        minutes = round(total / 60)
        if minutes:
            parts.append(f"{minutes} min" if minutes < 60 else f"{minutes // 60} hr {minutes % 60} min")
    return " · ".join(p for p in parts if p), MUSIC_OG_TYPES.get(kind, "website")

# The stylesheet and scripts the public share viewer needs to render. Kept as
# an allowlist, and served from under /api/public/ rather than the app's own
# /js and /css: everything a signed-out visitor may fetch then sits below a
# single prefix, so the proxy in front of this app grants exactly
# "^/(share/|api/public/)" instead of an enumerated list of filenames that
# silently goes stale the next time the viewer gains a script.
PUBLIC_SHARE_ASSETS = {
    "css/atrihord.css",
    "js/atrihord-user.js",
    "js/vr/discovery.js",
    'js/share-vr.js',
    'js/stereo3d.js',
    'js/vendor/three.module.min.js',
    'js/vr-logic.js',
    'js/vr/app.js',
    'js/vr/autoconvert.js',
    'js/vr/host.js',
    'js/vr/layout.js',
    'js/vr/loader.js',
    'js/vr/logic.js',
    'js/vr/nav.js',
    'js/vr/scroll.js',
    'js/vr/search.js',
    'js/vr/settings.js',
    'js/vr/sources.js',
    'js/vr/stereo-screen.js',
    'js/vr/ui.js',
    'js/vr/wall.js',
    "css/main.css",
    "css/music.css",
    "js/gallery.js",
    "js/interp.js",
    "js/music_share.js",
    "js/overlay-dismiss.js",
    "js/player.js",
    "js/public_share.js",
    "js/upscale.js",
}

@app.route("/api/public/assets/<path:relative_path>")
def public_share_asset(relative_path):
    """Static files for the public share viewer, no session required.

    Strictly the allowlist above. This is one of the few routes with no
    identity behind it, so it must never become a way to read arbitrary
    files - membership is checked against the exact set, which no amount of
    "../" can satisfy."""
    if relative_path not in PUBLIC_SHARE_ASSETS:
        return "404 Not Found", 404
    resp = send_from_directory(WEB_FILES_DIR, relative_path)
    # Same revalidate-always policy as the rest of the app's code assets, so a
    # deployed fix reaches a share viewer on their next load.
    resp.headers["Cache-Control"] = "no-cache"
    return resp

# Download of the companion app (a separate project, see README). Served from web_files/app/ when this server hosts
# it; otherwise MEDIA_BRIDGE_DOWNLOAD_URL (for example the project's GitHub releases page) is the target; with
# neither, the music page hides its "Spatial app" link instead of offering a dead one.
BRIDGE_DOWNLOAD_URL = os.environ.get("MEDIA_BRIDGE_DOWNLOAD_URL", "").strip()
BRIDGE_EXE = os.path.join(WEB_FILES_DIR, "app", "spatial-bridge", "ksf-spatial-bridge.exe")


@app.route("/app/ksf-spatial-bridge.exe", methods=["GET", "HEAD"])
def spatial_bridge_download():
    """The same exe the self-update serves (web_files/app/spatial-bridge/), as a download for the music page."""
    if not get_authenticated_user():
        return login_redirect_for(request.url)
    if os.path.isfile(BRIDGE_EXE):
        return send_from_directory(os.path.dirname(BRIDGE_EXE), "ksf-spatial-bridge.exe", as_attachment=True)
    if BRIDGE_DOWNLOAD_URL.startswith("https://"):
        return redirect(BRIDGE_DOWNLOAD_URL)
    return "404 Not Found", 404


@app.route("/app/ksf-spatial-bridge.zip", methods=["GET", "HEAD"])
def spatial_bridge_download_old_link():
    return redirect("/app/ksf-spatial-bridge.exe")


SPATIAL_BRIDGE_DIR = os.path.join(WEB_FILES_DIR, "app", "spatial-bridge")
SPATIAL_BRIDGE_FILES = {"latest.json", "ksf-spatial-bridge.exe"}


@app.route("/api/public/spatial-bridge/<name>", methods=["GET"])
def spatial_bridge_update_file(name):
    """The companion app's self-update reads latest.json (version, sha256, signature) and the exe from here.
    No login: the app cannot sign in, and it accepts nothing that is not signed with the maintainer's key.
    Exactly two file names are served, from one folder."""
    if name not in SPATIAL_BRIDGE_FILES:
        return "404 Not Found", 404
    resp = send_from_directory(SPATIAL_BRIDGE_DIR, name)
    resp.headers["Cache-Control"] = "no-cache"
    return resp


def _music_share_songs(share, token):
    """The songs of a music share, named as the music page names them: the
    catalogue is asked first (it knows that a visualiser channel is not the
    artist, and what the album is), the file's own tags only where a song has
    left the catalogue since the link was made."""
    catalog, _building = music_catalog()
    by_path = {}
    if catalog:
        for s in catalog["songs"]:
            for r, p in s.get("names", []) or [(s["root"], s["path"])]:
                by_path.setdefault((r, p), s)
    gains = ((catalog or {}).get("loudness") or {}).get("gains") or {}
    tags = music_tag_cache()
    out = []
    for item in share.get("items", []):
        root_id = share_root_of(item)
        rel = item["path"]
        full = resolve_media_path(root_id, rel)
        if not full or not os.path.isfile(full):
            continue
        song = by_path.get((root_id, rel))
        if song:
            title, artist = song["title"], song["artist"]
            album, duration, cover = song["album"], song["duration"], song["cover"]
        else:
            info = tags.get(full, os.stat(full))
            stem = os.path.splitext(os.path.basename(rel))[0]
            title = info.get("title") or stem
            artist = info.get("artist") or "Unknown artist"
            album, duration, cover = info.get("album"), info.get("duration") or 0, bool(info.get("cover"))
        q = "path=%s&root=%s" % (quote(rel, safe=""), quote(root_id, safe=""))
        out.append({
            # Shown as the music page shows it (no "Music Visualization", no emoji).
            "title": music_library.shown_title(title, artist), "artist": artist,
            "album": music_library.shown_title(album, artist) if album else "",
            "duration": round(duration or 0), "path": rel, "root": root_id,
            "url": "/api/public/share/%s/file?%s" % (token, q),
            "cover_url": ("/api/public/share/%s/music/cover?%s" % (token, q)) if cover else None,
            # dB to the common loudness, as the music page plays it (KSF-058).
            "gain": (gains.get(song["root"]) or {}).get(song["path"]) if song else None,
        })
    return out


def music_loudness_public():
    """The common loudness and an unmeasured file's gain, for a page that
    plays songs: what goes with each song's "gain"."""
    catalog, _building = music_catalog()
    loud = (catalog or {}).get("loudness") or {}
    return {"target": loud.get("target"), "unknown": loud.get("unknown", 0.0)}


@app.route("/api/public/share/<token>/music", methods=["GET"])
def public_share_music(token):
    """What the music viewer needs: the songs, in the order they were shared."""
    share = find_share(token)
    if not share:
        stale = find_share_including_expired(token)
        if stale and share_expired(stale):
            return jsonify({"error": "expired"}), 410
        return jsonify({"error": "Not found"}), 404
    if share.get("view") != "music":
        return jsonify({"error": "Not a music link"}), 404
    music = share.get("music") or {}
    return jsonify({
        "title": share_label(share), "subtitle": music.get("subtitle") or "",
        "kind": music.get("kind") or "songs", "created_at": share.get("created_at"),
        "expires_at": share.get("expires_at"), "songs": _music_share_songs(share, token),
        "loudness": music_loudness_public(),
    })


@app.route("/api/public/share/<token>/music/cover", methods=["GET"])
def public_share_music_cover(token):
    """One song's cover art. Gated by the same rule as every other public
    file route: the path must be one this share actually holds."""
    share = find_share(token)
    if not share or share.get("view") != "music":
        return jsonify({"error": "Not found"}), 404
    root_id = (request.args.get("root") or DEFAULT_ROOT_ID).strip()
    rel = (request.args.get("path") or "").strip("/").replace("\\", "/")
    if not path_allowed_by_share(share, rel, root_id):
        return jsonify({"error": "Forbidden"}), 403
    return _music_cover_response(root_id, rel, request.args.get("s"))


@app.route("/share/<token>")
def public_share_page(token):
    # The page itself is static - it reads the token from its own URL
    # client-side and drives everything through the
    # /api/public/share/<token>/* routes above. No auth check here on
    # purpose: the token itself is the access control for anyone who has
    # the link. The only server-side work is injecting link-preview tags,
    # which crawlers can't obtain any other way (they don't run JS).
    # A music link opens the music viewer, a file link the file viewer. The
    # token is read by the page itself either way, so the only decision here
    # is which page to send.
    known = find_share(token)
    name = "music_share.html" if (known or {}).get("view") == "music" else "public_share.html"
    try:
        with open(os.path.join(WEB_FILES_DIR, name), "r", encoding="utf-8") as f:
            page = f.read()
    except OSError:
        return "404 Not Found", 404

    share = known
    if share and request.method == "GET":
        record_share_view(share["id"])
    if share:
        page = page.replace("</head>", "  " + share_preview_meta(share, token) + "\n</head>", 1)

    resp = app.response_class(page, mimetype="text/html")
    # Revoking a share must actually stop previews from resurfacing.
    resp.headers["Cache-Control"] = "no-cache"
    return resp

# -------------------------------------------------------------------
# music.example.org: the music library, YouTube Music style
# -------------------------------------------------------------------
# User, 2026-09-22: "a new web page, music.example.org, that is served by the
# media-dl server and works mostly like Youtube Music's interface does" -
# playlists, search over playlists/albums/artists/titles, re-downloading a
# playlist, channel view. The catalogue itself is music_library.py's; this part
# feeds it the library's audio files (from the path index, so trash, sidecars
# and hidden files never get in) and serves it per viewer.
#
# music.html answers "/" on a music.* host and /music.html on every host, so
# the page works before the hostname and its Caddy block exist.
import gzip as _gzip
import music_library
import music_listened
import music_loudness
import song_merge

MUSIC_TAGS_FILE = os.path.join(BASE_DIR, "music_tags.json")
MUSIC_SOURCES_FILE = os.path.join(BASE_DIR, "music_sources.json")
MUSIC_PLAYLISTS_FILE = os.path.join(BASE_DIR, "music_playlists.json")
MUSIC_LISTENING_FILE = os.path.join(BASE_DIR, "music_listening.json")
# Seconds listened per account, song and hour, for charts (music_listened.py).
MUSIC_LISTENED_FILE = os.path.join(BASE_DIR, "music_listened.sqlite")
# How loud every song and song video is, so the page plays them all at one
# loudness (music_loudness.py, vault KSF-058).
MUSIC_LOUDNESS_FILE = os.path.join(BASE_DIR, "music_loudness.json")
# ffmpeg processes measuring at once: each is one core at nice 15, beside downloads.
MUSIC_LOUDNESS_WORKERS = 2
# The most songs one report may carry: the pages report every minute, so a
# report holds the few songs of that minute.
MUSIC_LISTENED_MAX_ITEMS = 50
# The longest "where it was played from" the page may send with a play. Long
# enough for a playlist id or an artist's name, short enough that the store
# stays a store of plays.
MUSIC_CTX_MAX = 160
MUSIC_COVER_DIR = os.path.join(BASE_DIR, "music_cover_cache")
# Renditions, made once and kept: a row's thumbnail, the bar, the artwork
# Android's lock screen shows. They are a LADDER, not a limit - a request for
# more than the largest of them is served the cover inside the file itself
# (_music_cover_response), which is what the now-playing screen asks for.
MUSIC_COVER_SIZES = (96, 300, 600)
# The types a cover may be kept and served as, and the extension each is kept
# under: a cache hit for the file's own picture is one of these four names.
MUSIC_COVER_TYPES = (("jpg", "image/jpeg"), ("png", "image/png"),
                     ("webp", "image/webp"), ("gif", "image/gif"))
# Above this a cover is sent as a rendition after all. Nothing in the library
# comes close (the largest of forty sampled: 285 KB); it is here so that one
# lossless PNG cannot turn every play into a 40 MB download.
MUSIC_COVER_FULL_MAX = 12 * 1024 * 1024
# A rebuild after the path index changed waits at least this long after the
# last one started: a playlist download finishing song by song would otherwise
# rebuild it after every song.
MUSIC_REBUILD_MIN_S = 60
MUSIC_REF_RE = re.compile(r"^(v:[A-Za-z0-9_-]{11}|p:[^:\x00]{1,64}:[^\x00]{1,1000})$")
USER_PLAYLIST_ID_RE = re.compile(r"^u[0-9a-f]{11}$")

_music_lock = threading.Lock()
# again: a rebuild was asked for (force) while one was running - start it when that one ends.
_music = {"catalog": None, "index": None, "building": False, "last_start": 0.0, "error": None,
          "again": False}
_music_stores = {}


def _music_store(name, factory):
    with _music_lock:
        if name not in _music_stores:
            _music_stores[name] = factory()
        return _music_stores[name]


def music_sources():
    return _music_store("sources", lambda: music_library.SourceRegistry(MUSIC_SOURCES_FILE))


def music_playlists_store():
    return _music_store("playlists", lambda: music_library.UserPlaylists(MUSIC_PLAYLISTS_FILE))


def music_listening_store():
    return _music_store("listening", lambda: music_library.Listening(MUSIC_LISTENING_FILE))


def music_listened_store():
    return _music_store("listened", lambda: music_listened.Listened(MUSIC_LISTENED_FILE))


_music_map = {"checked": 0.0, "mtime": None, "positions": {}}


def music_positions():
    """{(root, path): [x, y, z]}: every analysed song's place on the genre
    map's 3D layout, which the radios walk (read once per map write). At most
    every GENRE_MAP_REBUILD_S this also starts a map rebuild when the map is
    behind the AI index, as the map page does: new songs must join the radios
    without anyone opening genres.html."""
    data = genre_map_loaded()
    now = time.time()
    if now - _music_map["checked"] >= GENRE_MAP_REBUILD_S:
        _music_map["checked"] = now
        try:
            if genre_map_behind(data, *genre_map_counts(media_ai.connect(BASE_DIR))):
                start_genre_map_build()
        except Exception as exc:
            print(f"[Warning] music: genre map check failed: {exc}", flush=True)
    if _music_map["mtime"] != _genre_map["mtime"]:
        _music_map["positions"] = {(t["root"], t["path"]): t["p3"]
                                   for t in (data or {}).get("tracks", []) if t.get("p3")}
        _music_map["mtime"] = _genre_map["mtime"]
    return _music_map["positions"]


def music_tag_cache():
    return _music_store("tags", lambda: music_library.TagCache(MUSIC_TAGS_FILE))


def music_loudness_cache():
    return _music_store("loudness", lambda: music_loudness.LoudnessCache(MUSIC_LOUDNESS_FILE))


def music_loudness_measurer():
    """Measures what the loudness cache is missing, in the background; every
    few hundred results (and at the end) the catalogue is built again, which
    is what hands the new gains to the page. The cache is fetched first:
    _music_store holds _music_lock (not reentrant) while it makes a store, so
    asking for another store inside the factory would deadlock."""
    cache = music_loudness_cache()
    return _music_store("loudness_measurer", lambda: music_loudness.Measurer(
        cache, workers=MUSIC_LOUDNESS_WORKERS, batch=200,
        on_batch=lambda: music_catalog(force=True)))


def _music_loudness_plan(catalog, names, roots):
    """The page's gains for every song and song video of `catalog`
    (music_loudness.plan). What is not measured yet is queued for the
    measurer and gets the typical gain meanwhile. names: the build's
    (root, path, full, stat) of every audio file."""
    stat_of = {(r, rel): (full, st) for r, rel, full, st in names}
    files = {}
    for s in catalog["songs"]:
        key = (s["root"], s["path"])
        if key in stat_of:
            files[key] = stat_of[key]
        for v in s.get("videos") or [s.get("video")]:
            if not v or (v[0], v[1]) in files or v[0] not in roots:
                continue
            full = os.path.join(roots[v[0]]["path"], v[1])
            try:
                files[(v[0], v[1])] = (full, os.stat(full))
            except OSError:
                pass
    cache = music_loudness_cache()
    measured, missing, live = {}, [], set()
    for key, (full, st) in files.items():
        live.add(cache.key(st))
        value = cache.get(st)
        if value is None:
            missing.append((full, st))
        else:
            measured[key] = value
    cache.prune(live)
    cache.save()
    if missing:
        music_loudness_measurer().want(missing)
    out = music_loudness.plan(measured)
    out["measured"], out["missing"] = len(measured), len(missing)
    return out


def music_sync_url(url):
    """The link a sync downloads: a YouTube or YouTube Music playlist or album
    link, always as music.youtube.com - only a music.youtube.com download is
    turned into songs (maybe_spawn_youtube_music_extraction). None when it is
    not a playlist link."""
    if not music_library.playlist_url_ok(url):
        return None
    return "https://music.youtube.com/playlist?list=" + music_library.LIST_ID_RE.search(url).group(1)


def record_music_source(url, tracks):
    """A playlist download says where its folder came from and in which order
    YouTube Music holds the songs - what the page orders the playlist by and
    what its Sync button downloads again."""
    folder = playlist_dir_for(tracks[0]) if tracks else None
    if not folder:
        return
    rel = os.path.relpath(folder, DOWNLOADS_DIR).replace(os.sep, "/")
    m = music_library.LIST_ID_RE.search(url or "")
    try:
        music_sources().put(f"{DEFAULT_ROOT_ID}:{rel}", url=music_sync_url(url) or url,
                            list_id=m.group(1) if m else None, title=tracks[0].get("album"),
                            owner=tracks[0].get("folder_owner"),
                            tracks=[t["id"] for t in tracks if t.get("id")], synced_at=time.time())
    except OSError as exc:
        print(f"[Warning] music: the source of {rel} was not recorded: {exc}", flush=True)


def _music_genres():
    """{(root, path): style} - each audio file's strongest style from the AI
    index (Discogs "Electronic---Phonk" shown as "Phonk")."""
    best = {}
    try:
        conn = media_ai.connect(BASE_DIR)
        for r in conn.execute("SELECT m.root AS root, m.path AS path, t.tag AS tag, t.p AS p FROM media m"
                              " JOIN media_tags t ON t.media_id = m.id"
                              " WHERE t.category = 'genre' AND m.kind = 'audio' AND m.trashed IS NULL"):
            key = (r["root"], r["path"])
            if key not in best or (r["p"] or 0) > best[key][1]:
                best[key] = (r["tag"], r["p"] or 0)
    except Exception as exc:
        print(f"[Warning] music: styles not read: {exc}", flush=True)
        return {}
    return {k: v[0].split("---")[-1] for k, v in best.items()}


def _build_music_catalog(index):
    roots = {r["id"]: r for r in get_all_roots()}
    names = []
    videos = []
    for row in index:
        if row[2] or row[0] not in roots:
            continue
        if music_library.is_video(row[1]):
            videos.append((row[0], row[1]))
            continue
        if not music_library.is_audio(row[1]):
            continue
        root = roots[row[0]]
        full = os.path.join(root["path"], row[1])
        try:
            st = os.stat(full)
        except OSError:
            continue
        names.append((row[0], row[1], full, st))
    tags = music_tag_cache()
    # What each copy of a music video measures, so the Video pill can play the
    # best of several copies of the same cut (music_library._rank_videos).
    dims = media_dimensions_map()

    def video_dims(root_id, rel):
        d = dims.get(media_dimensions.key_for(root_id, rel))
        if not d or len(d) < 2:
            return None
        return {"width": d[0], "height": d[1], "fps": media_dimensions.frame_rate_of(d),
                "duration": media_dimensions.duration_of(d)}
    catalog = music_library.build(names, tags, DEFAULT_ROOT_ID,
                                  songs_index=music_store.read_songs_index(DOWNLOADS_DIR),
                                  registry=music_sources().all(), genres=_music_genres(),
                                  root_names={rid: r.get("name") or rid for rid, r in roots.items()},
                                  videos=music_library.video_index(videos), video_dims=video_dims,
                                  renamed=song_merge.load_renamed(SONG_MERGE_FILE))
    tags.save()
    catalog["loudness"] = _music_loudness_plan(catalog, names, roots)
    return catalog


def _music_build_thread(index):
    try:
        t0 = time.time()
        catalog = _build_music_catalog(index)
        with _music_lock:
            _music["catalog"], _music["index"], _music["error"] = catalog, index, None
        maybe_merge_songs(catalog)
        loud = catalog.get("loudness") or {}
        print(f"[Info] music: catalogue built - {len(catalog['songs'])} songs, "
              f"{len(catalog['playlists'])} playlists in {time.time() - t0:.1f}s; loudness target "
              f"{loud.get('target')} LUFS, {loud.get('measured', 0)} files measured, "
              f"{loud.get('missing', 0)} waiting", flush=True)
    except Exception as exc:
        with _music_lock:
            _music["error"] = str(exc)[:300]
        print(f"[Warning] music: catalogue build failed: {exc}", flush=True)
    finally:
        with _music_lock:
            _music["building"] = False
            again, _music["again"] = _music["again"], False
        if again:
            music_catalog(force=True)


def music_catalog(force=False):
    """(catalogue or None, building). Built in the background from the path
    index, and built again when the index has been rebuilt since - a download
    finished, files moved - at most once a minute. The previous catalogue is
    served while the next is made; the first build reads every file's tags
    (~2 minutes for this library) and later ones only what is new."""
    index = get_media_index()
    with _music_lock:
        stale = _music["index"] is not index
        start = not _music["building"] and (
            _music["catalog"] is None or force
            or (stale and time.time() - _music["last_start"] >= MUSIC_REBUILD_MIN_S))
        if start:
            _music["building"], _music["last_start"] = True, time.time()
        elif force and _music["building"]:
            _music["again"] = True     # what this build has not seen yet (new loudness) needs one more
        catalog, building = _music["catalog"], _music["building"]
    if start:
        threading.Thread(target=_music_build_thread, args=(index,), daemon=True).start()
    return catalog, building


# --- One song, one title (song_merge.py) ---------------------------------------
# After each catalogue build, the pairs of songs that may be one recording held
# twice - "Geoxor & Canonblade - Sakura" by Geoxor beside "Sakura" by Geoxor &
# Canonblade - are listened to, and each that is one recording keeps its better
# file under the clean title (user, 2026-09-28: "This happens often. When such
# cases occur, keep the title that does not contain the artist name again.").
# One round at a time and never while a download runs, since the song store is
# being written then. A round that merged something builds the catalogue again,
# which starts the next round, until a round has nothing left to merge.
SONG_MERGE_FILE = os.path.join(BASE_DIR, "song_merges.json")
_song_merge_lock = threading.Lock()
_song_merge = {"running": False, "started": 0.0, "finished": 0.0, "last": None, "error": None}


def song_merge_enabled():
    return bool((CONFIG.get("song_merge") or {}).get("enabled", True))


def maybe_merge_songs(catalog):
    """Starts a round over `catalog` unless one runs, a download does, or the
    setting is off. True when it started one."""
    if not catalog or not song_merge_enabled() or downloads_busy():
        return False
    with _song_merge_lock:
        if _song_merge["running"]:
            return False
        _song_merge["running"], _song_merge["started"] = True, time.time()
    threading.Thread(target=_song_merge_round, args=(catalog,), daemon=True, name="song-merge").start()
    return True


def _shared_names():
    """{(root, path)} of every file a share link names: a merge leaves those
    names exactly as they are, because a link someone holds cannot follow."""
    out = set()
    with shares_lock:
        shares = _read_shares_unlocked()
    for share in shares if isinstance(shares, list) else []:
        if share.get("kind") == "file" and share.get("path"):
            out.add((share_root_of(share), share["path"]))
        for item in share.get("items", []) or []:
            if isinstance(item, dict) and item.get("path"):
                out.add((share_root_of(item), item["path"]))
    return out


def _song_merged(res, entry):
    """What the server keeps about files, after one merge: the trashed name
    leaves the AI index's search, a name that took the kept file's extension
    keeps its favourites, and the retitled file's cached covers go."""
    if res.get("trashed"):
        ai_index_trashed([(res["trashed"][0], res["trashed"][1])], True)
    moves = res.get("moves") or {}
    if moves:
        with _favorites_lock:
            fans = [(user, {r: set(paths) for r, paths in roots.items()})
                    for user, roots in _favorites_data().items()]
        for user, by_root in fans:
            for (r, old), (_r, new) in moves.items():
                if old in by_root.get(r, ()):
                    set_favorite(user, r, new, True)
                    set_favorite(user, r, old, False)
    if res.get("retagged") and res.get("before"):
        try:
            music_forget_cover(resolve_media_path(*entry["keep"]), res["before"])
        except OSError:
            pass


def _song_merge_round(catalog):
    report, error = None, None
    try:
        roots = {r["id"]: r["path"] for r in get_all_roots()}
        report = song_merge.run(catalog["songs"], roots, DEFAULT_ROOT_ID, DOWNLOADS_DIR, SONG_MERGE_FILE,
                                shared=_shared_names(), busy=downloads_busy,
                                record_song=music_store.record_song, on_merged=_song_merged,
                                log=lambda m: print(f"[Info] song merge: {m}", flush=True))
        if report["listened"] or report["merged"] or report["failed"]:
            print(f"[Info] song merge: {report['candidates']} pairs to look at, {report['listened']} "
                  f"listened to, {report['same']} one recording, {report['merged']} merged, "
                  f"{report['failed']} not merged" + (f" (trash batch {report['batch']})" if report["batch"] else "")
                  + (f", stopped: {report['stopped']}" if report["stopped"] else ""), flush=True)
    except Exception as exc:
        error = str(exc)[:300]
        print(f"[Warning] song merge failed: {exc}", flush=True)
    finally:
        with _song_merge_lock:
            _song_merge.update(running=False, finished=time.time(), error=error, last=report)
    if report and (report["merged"] or report.get("tidied")):
        invalidate_media_caches()
        music_catalog(force=True)


def _json_maybe_gzip(payload, status=200):
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    resp = app.response_class(body, status=status, mimetype="application/json")
    if len(body) > 4096 and "gzip" in (request.headers.get("Accept-Encoding") or ""):
        resp.set_data(_gzip.compress(body, 5))
        resp.headers["Content-Encoding"] = "gzip"
        resp.headers["Vary"] = "Accept-Encoding"
    return resp


def is_music_host():
    return (request.host or "").split(":")[0].lower().startswith("music.")


@app.route("/api/music/library", methods=["GET"])
def music_library_api():
    """Everything the page shows, for this viewer: songs in the roots they may
    see, playlists, channels, albums, their own playlists, and whether they
    may sync (download). 202 while the first build is still running."""
    user = get_authenticated_user()
    if not user:
        return jsonify({"error": "Unauthorized"}), 401
    catalog, building = music_catalog()
    if catalog is None:
        with _music_lock:
            error = _music["error"]
        return jsonify({"building": True, "error": error}), 202
    view = music_library.for_viewer(catalog, {r["id"] for r in roots_for_user(user)},
                                    music_playlists_store().of(user["username"]), can_sync=is_admin(user),
                                    positions=music_positions(),
                                    listening=music_listening_store().summary(user["username"]))
    view["building"] = building
    return _json_maybe_gzip(view)


MUSIC_SHARE_MAX = 500          # songs in one link


def _music_songs_by_ref(catalog, visible_roots):
    """{ref: song} for every song the viewer may see - the page's own refs
    ("v:<video id>" or "p:<root>:<path>"), including a song's other names, so
    a playlist's hard link finds the song it points at."""
    out = {}
    for s in catalog["songs"]:
        if s["root"] not in visible_roots:
            continue
        out.setdefault(music_library.song_ref(s), s)
        out.setdefault("p:%s:%s" % (s["root"], s["path"]), s)
        for r, p in s.get("names", []):
            out.setdefault("p:%s:%s" % (r, p), s)
    for s in catalog["songs"]:
        if s["root"] in visible_roots:
            for ref in s.get("aliases") or ():
                out.setdefault(ref, s)          # a merged song's dropped id or renamed name
    return out


@app.route("/api/music/share", methods=["POST"])
def music_share_create():
    """A link to songs, kept in media-dl's own share store as a POOL share
    (user, 2026-09-23: "consider piggy-backing media-dl's system such that the
    shared can be managed there"): Settings > Shares lists it, gives it an
    expiry, revokes it, counts its views and takes a cover for it, exactly as
    for a shared folder. `view`: "music" is the only thing that marks it, and
    it is what makes /share/<token> serve the music viewer.

    Body: {"refs": [song ref, ...], "label": "...", "kind": "playlist|album|
    artist|song|songs", "subtitle": "..."}. The ORDER of refs is the order of
    the link, which a share of a playlist needs."""
    user = get_authenticated_user()
    if not user:
        return jsonify({"error": "Unauthorized"}), 401
    data = request.get_json(silent=True) or {}
    refs = data.get("refs")
    if not isinstance(refs, list) or not refs:
        return jsonify({"error": "refs is required"}), 400
    if len(refs) > MUSIC_SHARE_MAX:
        return jsonify({"error": "A link holds at most %d songs" % MUSIC_SHARE_MAX}), 400
    catalog, _building = music_catalog()
    if catalog is None:
        return jsonify({"error": "The music library is still being read"}), 503
    by_ref = _music_songs_by_ref(catalog, {r["id"] for r in roots_for_user(user)})

    items = []
    seen = set()
    for ref in refs:
        song = by_ref.get(ref) if isinstance(ref, str) else None
        if not song:
            continue
        key = (song["root"], song["path"])
        if key in seen:
            continue
        seen.add(key)
        items.append({"path": song["path"], "kind": "file", "root": song["root"]})
    if not items:
        return jsonify({"error": "None of those songs could be found"}), 404

    kind = (data.get("kind") or "songs").strip()[:40]
    label = (data.get("label") or "").strip()[:200]
    if not label:
        label = items[0]["path"].rsplit("/", 1)[-1] if len(items) == 1 else "Shared music"
    share = {
        "id": secrets.token_urlsafe(24),
        "kind": "pool",
        "view": "music",
        "music": {"kind": kind, "subtitle": (data.get("subtitle") or "").strip()[:200]},
        "label": label,
        "items": items,
        "created_by": user["username"],
        "created_at": time.time(),
        "expires_at": default_share_expiry(),
        "revoked": False,
    }
    with shares_lock:
        shares = _read_shares_unlocked()
        shares.append(share)
        _write_shares_unlocked(shares)
    out = share_summary(share)
    out["songs"] = len(items)
    return jsonify(out)


@app.route("/api/music/cover", methods=["GET"])
def music_cover():
    """A song's embedded cover at 96, 300 or 600 px, as WebP, made once and
    kept (by file version, so every name of a song shares it). 404 when the
    song has none - remembered too, since a Matroska cover costs an ffmpeg run
    to look for."""
    user = get_authenticated_user()
    if not user:
        return jsonify({"error": "Unauthorized"}), 401
    root_id = (request.args.get("root") or DEFAULT_ROOT_ID).strip()
    if not root_visible_to(user, root_id):
        return jsonify({"error": "Forbidden"}), 403
    return _music_cover_response(root_id, request.args.get("path", ""), request.args.get("s"))


def _music_cover_response(root_id, rel_path, want_size):
    """The cover itself, once the caller has decided who may see it: a signed
    in viewer with that root, or anyone holding a share token that covers this
    song."""
    full = resolve_media_path(root_id, rel_path)
    if not full or not os.path.isfile(full):
        return jsonify({"error": "Not found"}), 404
    try:
        want = int(want_size or 300)
    except (TypeError, ValueError):
        want = 300
    # The smallest rendition that covers what was asked for - and "full", the
    # file's own picture, for anything the ladder cannot reach. The screen a
    # song is played on asks for its own size (music.js stageCoverSize), so a
    # 4K display is sent the cover the file holds instead of a 600 px copy of
    # it stretched over 8 megapixels (user, 2026-09-23).
    size = next((s for s in MUSIC_COVER_SIZES if s >= want), "full")
    st = os.stat(full)
    key = hashlib.sha1(f"{st.st_dev}:{st.st_ino}:{st.st_size}:{st.st_mtime_ns}:{size}".encode()).hexdigest()
    folder = os.path.join(MUSIC_COVER_DIR, key[:2])
    missing = os.path.join(folder, key + ".none")
    if os.path.exists(missing):
        return jsonify({"error": "No cover"}), 404
    if size == "full":
        for ext, mime in MUSIC_COVER_TYPES:
            hit = os.path.join(folder, f"{key}.{ext}")
            if os.path.isfile(hit):
                return music_cover_send(hit, mime)
    else:
        cached = os.path.join(folder, key + ".webp")
        if os.path.isfile(cached):
            return music_cover_send(cached, "image/webp")
    cover = get_embedded_cover(full)
    os.makedirs(folder, exist_ok=True)
    if not cover:
        open(missing, "w").close()
        return jsonify({"error": "No cover"}), 404
    blob = cover[1]
    if size == "full":
        mime, ext = music_cover_type(blob[:12])
        if mime and len(blob) <= MUSIC_COVER_FULL_MAX:
            out = os.path.join(folder, f"{key}.{ext}")
            with open(out + ".part", "wb") as fh:
                fh.write(blob)
            os.replace(out + ".part", out)
            return music_cover_send(out, mime)
        # A type nothing agrees on, or a picture too large to send whole: the
        # largest rendition instead, kept under the key that was asked for.
        size = MUSIC_COVER_SIZES[-1]
    cached = os.path.join(folder, key + ".webp")
    try:
        from PIL import Image
        img = Image.open(io.BytesIO(blob))
        img.draft("RGB", (size, size))
        img = img.convert("RGB")
        img.thumbnail((size, size), Image.LANCZOS)
        tmp = cached + ".part"
        img.save(tmp, "WEBP", quality=82, method=4)
        os.replace(tmp, cached)
    except Exception as exc:
        print(f"[Warning] music: cover of {full} not made: {exc}", flush=True)
        return jsonify({"error": "Unreadable cover"}), 404
    return music_cover_send(cached, "image/webp")


def _music_refs(value):
    """The song references in a request body that look like references."""
    if not isinstance(value, list):
        return None
    return [v for v in value if isinstance(v, str) and MUSIC_REF_RE.match(v)]


@app.route("/api/music/playlists", methods=["POST"])
def music_playlist_create():
    user = get_authenticated_user()
    if not user:
        return jsonify({"error": "Unauthorized"}), 401
    data = request.get_json(silent=True) or {}
    try:
        pid, pl = music_playlists_store().create(user["username"], str(data.get("name") or ""))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    songs = _music_refs(data.get("songs")) or []
    if songs:
        pl = music_playlists_store().update(user["username"], pid, add=songs)
    return jsonify({"id": pid, "playlist": pl})


@app.route("/api/music/playlists/<pid>", methods=["PATCH", "DELETE"])
def music_playlist_edit(pid):
    """Rename, reorder (songs = the whole new order), add or remove songs; or
    delete. Only the viewer's own playlists - there is no other kind here."""
    user = get_authenticated_user()
    if not user:
        return jsonify({"error": "Unauthorized"}), 401
    if not USER_PLAYLIST_ID_RE.match(pid or ""):
        return jsonify({"error": "Not found"}), 404
    store = music_playlists_store()
    if request.method == "DELETE":
        return (jsonify({"success": True}) if store.delete(user["username"], pid)
                else (jsonify({"error": "Not found"}), 404))
    data = request.get_json(silent=True) or {}
    name = data.get("name")
    if name is not None and not isinstance(name, str):
        return jsonify({"error": "name must be text"}), 400
    songs = _music_refs(data.get("songs")) if "songs" in data else None
    if "songs" in data and songs is None:
        return jsonify({"error": "songs must be a list"}), 400
    pl = store.update(user["username"], pid, name=name, songs=songs,
                      add=_music_refs(data.get("add")), remove=_music_refs(data.get("remove")))
    if pl is None:
        return jsonify({"error": "Not found"}), 404
    return jsonify({"id": pid, "playlist": pl})


def _music_radio(value):
    """The radio a request names, checked: {id, kind, key, title}, None when
    it names none, False when what it sent is not one. The id is always
    "<kind>:<key>" - the page's own id must agree."""
    if value is None:
        return None
    if not isinstance(value, dict):
        return False
    kind, key, title = value.get("kind"), value.get("key"), value.get("title")
    if kind not in music_library.RADIO_KINDS or not isinstance(key, str) or not isinstance(title, str):
        return False
    if not 0 < len(key) <= 1000 or "\x00" in key or len(title) > 200:
        return False
    if kind == "song" and not MUSIC_REF_RE.match(key):
        return False
    rid = f"{kind}:{key}"
    if value.get("id") not in (None, rid):
        return False
    return {"id": rid, "kind": kind, "key": key, "title": title.strip()}


@app.route("/api/music/played", methods=["POST"])
def music_played():
    """A song started: kept per account for the radios and the history. What
    was picked by hand ("user") seeds "Radios for you"; a radio ("radio", with
    the radio) remembers what it played, so it never plays a song twice; "ctx"
    names where the song was played from (music_library.Listening), which is
    what lets the history say "from Night drive"."""
    user = get_authenticated_user()
    if not user:
        return jsonify({"error": "Unauthorized"}), 401
    data = request.get_json(silent=True) or {}
    ref, src = data.get("ref"), data.get("src")
    if not isinstance(ref, str) or not MUSIC_REF_RE.match(ref):
        return jsonify({"error": "ref must be a song reference"}), 400
    if src not in music_library.PLAY_SOURCES:
        return jsonify({"error": "src must be one of " + ", ".join(music_library.PLAY_SOURCES)}), 400
    radio = _music_radio(data.get("radio"))
    if radio is False or (radio is None) != (src != "radio"):
        return jsonify({"error": "a radio play names its radio, and only a radio play does"}), 400
    ctx = data.get("ctx")
    if ctx is not None and (not isinstance(ctx, str) or len(ctx) > MUSIC_CTX_MAX):
        return jsonify({"error": f"ctx must be a string of at most {MUSIC_CTX_MAX} characters"}), 400
    music_listening_store().record(user["username"], ref, src, radio, ctx=ctx or None)
    return jsonify({"success": True})


# ---- time listened, for charts (music_listened.py, vault KSF-052) ------------
_music_file_songs = {"catalog": None, "map": {}}


def _music_song_for_file(catalog, root, path):
    """The song a played FILE is: its audio under any of its names (a
    playlist's hard link as well as its home under the artist), or one of its
    music videos - "the video associated with an audio track counts as the
    song" (user, 2026-09-24). None for anything else. Mapped once per
    catalogue."""
    with _music_lock:
        if _music_file_songs["catalog"] is not catalog:
            files = {}
            for s in catalog["songs"]:
                files.setdefault((s["root"], s["path"]), s)
                for r, p in s.get("names") or []:
                    files.setdefault((r, p), s)
            # Videos after every audio name: a file that is somehow both keeps
            # the song it is the audio of.
            for s in catalog["songs"]:
                for v in s.get("videos") or [s.get("video")]:
                    if v:
                        files.setdefault((v[0], v[1]), s)
            _music_file_songs.update(catalog=catalog, map=files)
        return _music_file_songs["map"].get((root, path))


@app.route("/api/music/listened", methods=["POST"])
def music_listened_add():
    """Seconds that played, from the music page (by song ref) or media-dl's
    preview (by file: a song, or its music video, counts as the song).

    Body: {"items": [{"ref": "v:...", "s": 42.5} | {"root": "...", "path": "...",
    "s": 42.5}, ...]}. Answers {"counted": [bool per item]}: false is a file
    that is not a song, or a ref no longer in the library - the preview stops
    reporting that file. 503 while the first catalogue is being built, when a
    file cannot be told from a song yet (the page keeps the time and sends it
    again)."""
    user = get_authenticated_user()
    if not user:
        return jsonify({"error": "Unauthorized"}), 401
    data = request.get_json(silent=True) or {}
    items = data.get("items")
    if not isinstance(items, list) or not items or len(items) > MUSIC_LISTENED_MAX_ITEMS:
        return jsonify({"error": f"items must be a list of 1 to {MUSIC_LISTENED_MAX_ITEMS} entries"}), 400
    catalog, _building = music_catalog()
    visible = {r["id"] for r in roots_for_user(user)}
    by_ref = _music_songs_by_ref(catalog, visible) if catalog else None
    counted, rows = [], []
    for item in items:
        seconds = item.get("s") if isinstance(item, dict) else None
        if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or not 0 < seconds < float("inf"):
            return jsonify({"error": "every item needs s, the seconds listened"}), 400
        ref, root, path = item.get("ref"), item.get("root"), item.get("path")
        song = None
        if isinstance(ref, str):
            if not MUSIC_REF_RE.match(ref):
                return jsonify({"error": "ref must be a song reference"}), 400
            if by_ref is not None:
                song = by_ref.get(ref)
                ref = music_library.song_ref(song) if song else None
        elif isinstance(root, str) and isinstance(path, str):
            if catalog is None:
                return jsonify({"error": "The music library is still being read"}), 503
            root = root or DEFAULT_ROOT_ID           # media-dl's entries may leave the built-in root blank
            song = _music_song_for_file(catalog, root, path) if root in visible else None
            ref = music_library.song_ref(song) if song and song["root"] in visible else None
        else:
            return jsonify({"error": "every item needs a ref, or a root and a path"}), 400
        counted.append(ref is not None)
        if ref is not None:
            rows.append((ref, seconds, song))
    if rows:
        music_listened_store().add(user["username"], rows)
    return jsonify({"counted": counted})


@app.route("/api/music/listened", methods=["GET"])
def music_listened_summary():
    """The viewer's own listening: the total, songs by time listened and the
    time per UTC day. ?days=N for the last N days (default: everything)."""
    user = get_authenticated_user()
    if not user:
        return jsonify({"error": "Unauthorized"}), 401
    try:
        days = float(request.args.get("days") or 0)
    except ValueError:
        days = 0
    try:
        limit = min(500, max(1, int(request.args.get("limit") or 50)))
    except ValueError:
        limit = 50
    since = time.time() - days * 86400 if days > 0 else None
    return jsonify(music_listened_store().summary(user["username"], since, limit))


# ---------------------------------------------------------------------------
# Song covers: the job that remakes a cover from a better frame (song_covers.py,
# vault KSF-046). It reads the songs, fetches videos again and writes pictures
# back, so it is admin-only from the outside; the worker itself runs from the
# server's own thread.
# ---------------------------------------------------------------------------

def song_covers_settings():
    merged = json.loads(json.dumps(song_covers.DEFAULTS))
    merged.update(CONFIG.get("song_covers") or {})
    return merged


def downloads_busy():
    """True while a download is running or waiting: the cover job stands back
    for it, both for the line and for YouTube's patience."""
    with task_lock:
        return any(t.get("status") in ("Running", "Queued") for t in active_tasks.values())


def music_cover_type(blob):
    """(mimetype, extension) read from the picture's own bytes, or (None, None)
    for something this route will not serve whole.

    Read from the bytes rather than taken from get_embedded_cover, which
    reports what the CONTAINER claims: a .mka cover comes back declared
    "image/png" whatever it is, because get_mka_cover copies the attachment's
    own bitstream into a file it happens to have named cover.png."""
    if blob[:3] == b"\xff\xd8\xff":
        return "image/jpeg", "jpg"
    if blob[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png", "png"
    if blob[:4] == b"RIFF" and blob[8:12] == b"WEBP":
        return "image/webp", "webp"
    if blob[:4] == b"GIF8":
        return "image/gif", "gif"
    return None, None


def music_cover_send(path, mime):
    resp = send_file(path, mimetype=mime)
    resp.headers["Cache-Control"] = "private, max-age=604800"
    return resp


def music_forget_cover(full, old_stat):
    """Throws away what music_cover() made of a song's old picture.

    The job puts the song's timestamp back after writing (so the library keeps
    dating it by when it was downloaded), which leaves the cache key it was
    stored under reachable - the picture inside the file is simply not that
    one any more."""
    for st in (old_stat, os.stat(full)):
        for size in MUSIC_COVER_SIZES + ("full",):
            key = hashlib.sha1(f"{st.st_dev}:{st.st_ino}:{st.st_size}:{st.st_mtime_ns}:{size}".encode()).hexdigest()
            folder = os.path.join(MUSIC_COVER_DIR, key[:2])
            for name in [key + ".webp", key + ".none"] + [f"{key}.{e}" for e, _m in MUSIC_COVER_TYPES]:
                try:
                    os.remove(os.path.join(folder, name))
                except OSError:
                    pass


_song_covers = song_covers.SongCovers(
    BASE_DIR, os.path.join(DOWNLOADS_DIR, "youtube_music"),
    song_covers_settings, downloads_busy,
    log=lambda text: print(text, flush=True), forget_cover=music_forget_cover)


@app.route("/api/music/covers", methods=["GET"])
def music_covers_status():
    """What the job has done and what is left of it."""
    user = get_authenticated_user()
    if not user:
        return jsonify({"error": "Unauthorized"}), 401
    if not is_admin(user):
        return jsonify({"error": "Forbidden"}), 403
    return jsonify(_song_covers.status())


@app.route("/api/music/covers/scan", methods=["POST"])
@require_admin
def music_covers_scan():
    """Reads every song's cover again and fills the queue. Downloads nothing,
    so it is safe to press at any time; `force` also reconsiders the songs it
    has already been through."""
    data = request.get_json(silent=True) or {}
    force = bool(data.get("force"))
    result = {}

    def work():
        result.update(_song_covers.scan(force=force))

    thread = threading.Thread(target=work, daemon=True)
    thread.start()
    thread.join(timeout=25)
    if thread.is_alive():
        return jsonify({"started": True, "running": True})
    return jsonify({"started": True, "running": False, **result})


@app.route("/api/music/covers/song", methods=["POST"])
@require_admin
def music_covers_song():
    """Puts one song at the front of the queue - the page's "improve this
    cover" (body {root, path}, the song's own path in the library)."""
    data = request.get_json(silent=True) or {}
    root_id = (data.get("root") or DEFAULT_ROOT_ID).strip()
    full = resolve_media_path(root_id, data.get("path", ""))
    store = os.path.join(DOWNLOADS_DIR, "youtube_music")
    if not full or not os.path.isfile(full):
        return jsonify({"error": "Not found"}), 404
    real = os.path.realpath(full)
    if not real.startswith(os.path.realpath(store) + os.sep):
        return jsonify({"error": "Only songs in the music store have a video to take a frame from"}), 400
    rel = os.path.relpath(real, os.path.realpath(store)).replace(os.sep, "/")
    if not _song_covers.enqueue(rel):
        return jsonify({"error": "Not found"}), 404
    return jsonify({"queued": True, "path": rel, "status": _song_covers.status()})


# ---------------------------------------------------------------------------
# Intro & outro cutting (video_trim.py). User, 2026-10-18: "Build a feature
# extremely similar video beginnings and endings are recognized and cut. Avoid
# a re-encode if possible without endangering compatibility." It rewrites video
# files, so everything here is admin-only, and nothing is cut before the user
# approves a signature.
# ---------------------------------------------------------------------------

def video_trim_settings():
    return CONFIG.get("video_trim") or {}


def video_trim_roots():
    return [{"id": r["id"], "path": r["path"]} for r in get_all_roots()]


# User, 2026-10-18: "when a video is intro/outro is cut for non-new downloads, re-do the
# interpolation and 3D work." A cut (or an undo) writes into the same inode, so size and mtime
# change and the 3D cache key and the vision worker's Smooth motion key no longer find the file.
# on_cut hands both caches the OLD version: whatever existed for it is made again for the new one
# (3D: stereo3d.Service.redo_after_cut, lane "redo"; Smooth motion: interp_redo.py). A file that
# had neither (a download that is cut right away) gets nothing.
INTERP_REDO_FILE = os.path.join(BASE_DIR, "interp_redo.json")


def interp_key_for(root_id, rel_path, size, mtime):
    return hashlib.sha256(f"{root_id}\n{rel_path}\n{size}\n{int(mtime)}".encode()).hexdigest()[:32]


def _interp_redo_variants(old_key):
    """What the vision worker's segment cache holds for one source key:
    [{variant, segments, complete}]. interp_redo.Backoff when it cannot say."""
    try:
        with urllib.request.urlopen(_interp_worker_request(f"/v1/interp/variants/{old_key}"), timeout=10) as r:
            data = json.load(r)
    except urllib.error.HTTPError as exc:
        raise interp_redo.Backoff(f"the vision worker answered {exc.code}") from None
    except (OSError, ValueError) as exc:
        raise interp_redo.Backoff(f"the vision worker could not be reached: {exc}") from None
    if not isinstance(data, list):
        raise interp_redo.Backoff("the vision worker sent no variant list")
    return [v for v in data if isinstance(v, dict) and v.get("variant")]


def _interp_redo_run(root_id, rel_path, spec, should_stop):
    """Makes one variant for the file as it is now: a complete one in full, a partial one the same
    number of leading segments. -> "done" | "skip" | "gone" | "stopped"; interp_redo.Backoff when
    the worker is away or busy (the GPU is shared with 3D, upscaling and the indexer)."""
    cfg = interp_settings()
    parsed = interp_redo.parse_variant(spec.get("variant"))
    if not cfg.get("enabled") or parsed is None:
        return "skip"
    if parsed["max_height"] != int(cfg.get("max_height") or 2160):
        return "skip"                       # made for a setting that is gone
    file_path = resolve_media_path(root_id, rel_path)
    if not file_path or not os.path.isfile(file_path):
        return "gone"
    if not spec.get("complete") and int(spec.get("segments") or 0) <= 0:
        return "skip"
    status = interp_worker_status()
    if status is None or not status.get("available"):
        raise interp_redo.Backoff("the vision worker cannot be reached or has no interpolation")
    fps = probe_media_details(file_path).get("fps") or 0
    try:
        prefix, _, _ = interp_output_rate(cfg, fps, parsed["args"])
    except (ValueError, LookupError):
        return "skip"
    if prefix != parsed["prefix"]:
        return "skip"                       # the rate ceiling changed: another rendition now
    stat = os.stat(file_path)
    key = interp_key_for(root_id, rel_path, stat.st_size, stat.st_mtime)
    variant = f"{prefix}_h{parsed['max_height']}_r{parsed['mbps']:g}"
    limit = None if spec.get("complete") else int(spec["segments"])
    try:
        total, _init = interp_open_rendition(key, variant, file_path)
        for _ in interp_segment_stream(key, variant, file_path, rel_path, total, limit=limit,
                                       strict=True, should_stop=should_stop):
            pass                            # generating is the point; the bytes are not wanted
    except urllib.error.HTTPError as exc:
        raise interp_redo.Backoff(f"the vision worker answered {exc.code} ({exc.reason})") from None
    except (OSError, RuntimeError, ValueError, KeyError) as exc:
        raise interp_redo.Backoff(f"{type(exc).__name__}: {exc}") from None
    return "stopped" if should_stop() else "done"


_interp_redo = interp_redo.InterpRedo(
    INTERP_REDO_FILE, lambda: downloads_busy(), _interp_redo_variants, _interp_redo_run,
    log=lambda text: print(text, flush=True))


def video_trim_on_cut(root_id, rel_path, old, new):
    """video_trim.py committed a cut (or an undo) of (root_id, rel_path); `old` is the version
    before. Runs on the trim thread; each half is on its own so one failing leaves the other."""
    try:
        res = stereo3d_service().redo_after_cut(root_id, rel_path, old)
        if res.get("status") == "requeued":
            print(f"[trim] 3D re-queued for {root_id}/{rel_path} ({res.get('lane')})", flush=True)
    except Exception as exc:
        print(f"[trim] 3D redo for {root_id}/{rel_path}: {type(exc).__name__}: {exc}", flush=True)
    try:
        if not interp_settings().get("enabled"):
            return
        old_key = interp_key_for(root_id, rel_path, old["size"], old["mtime"])
        try:
            variants = _interp_redo_variants(old_key)
        except interp_redo.Backoff:
            variants = None                 # ask again from the job
        job = _interp_redo.schedule(root_id, rel_path, old_key, variants)
        if job:
            print(f"[trim] Smooth motion redo queued for {root_id}/{rel_path}", flush=True)
    except Exception as exc:
        print(f"[trim] Smooth motion redo for {root_id}/{rel_path}: {type(exc).__name__}: {exc}", flush=True)


def video_trim_redo_status():
    try:
        s3d = stereo3d_service().redo_counts()
    except Exception:
        s3d = None
    return {"stereo3d": s3d, "interp": _interp_redo.status()}


_video_trim = video_trim.VideoTrim(
    BASE_DIR, video_trim_settings, downloads_busy, video_trim_roots, invalidate_media_caches,
    log=lambda text: print(text, flush=True), on_cut=video_trim_on_cut)


@app.route("/api/media/trim", methods=["GET"])
@require_admin
def video_trim_list():
    return jsonify({"status": _video_trim.status(), "signatures": _video_trim.list_signatures(),
                    "redo": video_trim_redo_status()})


@app.route("/api/media/trim/<sid>/preview", methods=["GET"])
@require_admin
def video_trim_preview(sid):
    info = _video_trim.preview(sid)
    if info is None:
        return jsonify({"error": "Not found"}), 404
    return jsonify(info)


@app.route("/api/media/trim/<sid>/frame/<int:index>", methods=["GET"])
@require_admin
def video_trim_frame(sid, index):
    path = _video_trim.frame_path(sid, index)
    if not path:
        return jsonify({"error": "Not found"}), 404
    resp = send_file(path, mimetype="image/jpeg")
    resp.headers["Cache-Control"] = "private, max-age=86400"
    return resp


def _video_trim_action(sid, fn):
    if sid not in _video_trim.state["signatures"]:
        return jsonify({"error": "Not found"}), 404
    return jsonify({"ok": True, "result": fn()})


@app.route("/api/media/trim/<sid>/approve", methods=["POST"])
@require_admin
def video_trim_approve(sid):
    return _video_trim_action(sid, lambda: _video_trim.approve(sid))


@app.route("/api/media/trim/<sid>/reject", methods=["POST"])
@require_admin
def video_trim_reject(sid):
    return _video_trim_action(sid, lambda: _video_trim.reject(sid))


@app.route("/api/media/trim/<sid>/undo", methods=["POST"])
@require_admin
def video_trim_undo(sid):
    # Written back in the background: it copies whole videos.
    return _video_trim_action(sid, lambda: _video_trim.queue_job(("undo", sid)))


@app.route("/api/media/trim/<sid>/delete-originals", methods=["POST"])
@require_admin
def video_trim_delete_originals(sid):
    return _video_trim_action(sid, lambda: _video_trim.queue_job(("delete", sid)))


@app.route("/api/media/trim/<sid>/auto-delete", methods=["POST"])
@require_admin
def video_trim_auto_delete(sid):
    value = (request.get_json(silent=True) or {}).get("value")
    if not isinstance(value, bool):
        return jsonify({"error": "value must be true or false"}), 400
    return _video_trim_action(sid, lambda: _video_trim.set_auto_delete(sid, value))


@app.route("/api/media/trim/run", methods=["POST"])
@require_admin
def video_trim_run():
    """Starts the daily pass now; `all` also looks at folders that did not change."""
    _video_trim.run_now(everything=bool((request.get_json(silent=True) or {}).get("all")))
    return jsonify({"started": True})


# --- Deleting a song, every copy of it ------------------------------------------
# User, 2026-10-14: "Add a delete button on music-dl. It should identify all
# copies of the song (hardlinks) and delete all of them", behind a dialog that
# asks again. A song is one file hard-linked into every playlist that holds it
# (vault KSF-044), so deleting one name frees nothing and leaves the song in the
# other playlists. The catalogue groups the names by inode already; each is
# stat'ed again here, so the dialog lists what is on disk now, not what the last
# build saw. The delete itself goes through the trash like /api/media/delete
# (trash_resolved): every name lands in one batch and can be restored until the
# 30-day purge.

def _music_song_copies(root_id, rel):
    """(copies, nlink) for the song that has the name (root_id, rel). copies:
    [{root, root_name, path, full}] - every name the catalogue knows that is
    still this same file on disk; nlink: the file's link count, so the caller
    can tell links the library does not know about (outside the music folders,
    or made since the last build). Raises LookupError."""
    catalog, _building = music_catalog()
    if catalog is None:
        raise LookupError("The music library is still being built")
    song = None
    for s in catalog["songs"]:
        names = [(s["root"], s["path"])] + [tuple(n) for n in s.get("names", [])]
        if (root_id, rel) in names:
            song = s
            break
    if song is None:
        raise LookupError("No such song")
    full = resolve_media_path(root_id, rel)
    try:
        st0 = os.stat(full) if full else None
    except OSError:
        st0 = None
    if st0 is None:
        raise LookupError("The song's file is gone")
    roots = {r["id"]: r for r in get_all_roots()}
    copies, seen = [], set()
    for r, p in [(song["root"], song["path"])] + [tuple(n) for n in song.get("names", [])]:
        if (r, p) in seen:
            continue
        seen.add((r, p))
        f = resolve_media_path(r, p)
        try:
            st = os.stat(f) if f else None
        except OSError:
            st = None
        if st is None or (st.st_dev, st.st_ino) != (st0.st_dev, st0.st_ino):
            continue                  # renamed or replaced since the build: not this file any more
        copies.append({"root": r, "root_name": (roots.get(r) or {}).get("name") or r, "path": p, "full": f})
    return copies, st0.st_nlink


def _music_copies_answer(copies, nlink):
    return {"copies": [{k: c[k] for k in ("root", "root_name", "path")} for c in copies],
            "nlink": nlink, "unlisted": max(0, nlink - len(copies))}


@app.route("/api/music/song/copies", methods=["POST"])
@require_admin
def music_song_copies():
    """Body {root, path}: every name of that song's file, for the delete
    dialog. Admin-only, like every delete."""
    data = request.get_json(silent=True) or {}
    root_id = (data.get("root") or DEFAULT_ROOT_ID).strip()
    rel = (data.get("path") or "").strip("/").replace("\\", "/")
    try:
        copies, nlink = _music_song_copies(root_id, rel)
    except LookupError as e:
        return jsonify({"error": str(e)}), 404
    return jsonify(_music_copies_answer(copies, nlink))


@app.route("/api/music/song/delete", methods=["POST"])
@require_admin
def music_song_delete():
    """Body {root, path, copies: ["root:path", ...], allow_redownload}: moves
    every name of the song's file to the trash. `copies` is the list the dialog
    showed; if the file has other names by now the delete is refused (409) with
    the new list, so nothing is deleted that the user did not see. With
    allow_redownload the yt-dlp archive forgets the video and a playlist sync
    fetches it again; without, it stays recorded and does not come back."""
    data = request.get_json(silent=True) or {}
    user = get_authenticated_user()
    root_id = (data.get("root") or DEFAULT_ROOT_ID).strip()
    rel = (data.get("path") or "").strip("/").replace("\\", "/")
    shown = data.get("copies")
    if not isinstance(shown, list) or not shown:
        return jsonify({"error": "copies is required"}), 400
    try:
        copies, nlink = _music_song_copies(root_id, rel)
    except LookupError as e:
        return jsonify({"error": str(e)}), 404
    if sorted(str(c) for c in shown) != sorted("%s:%s" % (c["root"], c["path"]) for c in copies):
        return jsonify(dict(_music_copies_answer(copies, nlink),
                            error="The copies of this song changed since the dialog opened")), 409
    for c in copies:
        if not root_visible_to(user, c["root"]) or c["path"].split("/")[0] == TRASH_DIR_NAME:
            return jsonify({"error": "Forbidden: %s" % c["path"]}), 403
    mode = DELETE_RELEASE if data.get("allow_redownload") else DELETE_BAN
    batch, deleted, notes, failed = trash_resolved(
        [(c["root"], c["path"], c["full"]) for c in copies], mode, user)
    # Gone from the page at once: the catalogue is only rebuilt once the path
    # index has been walked again (a minute or more), and until then the song
    # would still be listed. A rebuild replaces the catalogue, and this with it,
    # so a restore from the trash shows the song again after the next build.
    with _music_lock:
        catalog = _music["catalog"]
        if catalog is not None:
            gone = set(catalog.get("deleted") or ())
            gone.update((d["root"], d["path"]) for d in deleted)
            catalog["deleted"] = gone
    threading.Thread(target=lambda: music_catalog(force=True), daemon=True).start()
    return jsonify({"batch": batch, "mode": mode, "deleted": [{"root": d["root"], "path": d["path"]} for d in deleted],
                    "failed": failed, "records": sorted(set(notes)), "unlisted": max(0, nlink - len(copies))})


@app.route("/api/music/history", methods=["GET"])
def music_history():
    """The viewer's plays, newest first, in pages: what the history view
    lists. The library hands out only the newest few hundred plays (all the
    radios need), so everything older is read here - ?before=<time> asks for
    the page after the one the view already has."""
    user = get_authenticated_user()
    if not user:
        return jsonify({"error": "Unauthorized"}), 401
    try:
        limit = min(500, max(1, int(request.args.get("limit") or 200)))
    except ValueError:
        limit = 200
    try:
        before = float(request.args.get("before") or 0) or None
    except ValueError:
        before = None
    return jsonify(music_listening_store().history(user["username"], before, limit))


@app.route("/api/music/radio", methods=["GET"])
def music_radio_state():
    """One of the viewer's radios (?id=<kind>:<key>) with everything it
    played, to go on from where it stopped."""
    user = get_authenticated_user()
    if not user:
        return jsonify({"error": "Unauthorized"}), 401
    radio = music_listening_store().radio(user["username"], request.args.get("id") or "")
    if radio is None:
        return jsonify({"error": "Not found"}), 404
    return jsonify(radio)


# The loudness a viewer may pick for the music page (LUFS). Below -30 every
# song is a whisper; above -5 nearly every file stops at its clipping ceiling.
MUSIC_TARGET_LUFS_MIN = -30.0
MUSIC_TARGET_LUFS_MAX = 0.0


@app.route("/api/music/settings", methods=["PUT"])
def music_settings():
    """The viewer's player settings that follow them between devices, each
    optional: autoplay - go on with similar songs when what was started runs
    out; target_lufs - the loudness every file plays at (null: the library's
    own, music_loudness.choose_target)."""
    user = get_authenticated_user()
    if not user:
        return jsonify({"error": "Unauthorized"}), 401
    data = request.get_json(silent=True) or {}
    if "autoplay" not in data and "target_lufs" not in data:
        return jsonify({"error": "nothing to set"}), 400
    if "autoplay" in data and not isinstance(data["autoplay"], bool):
        return jsonify({"error": "autoplay must be true or false"}), 400
    target = data.get("target_lufs")
    if target is not None and (isinstance(target, bool) or not isinstance(target, (int, float))
                               or not MUSIC_TARGET_LUFS_MIN <= target <= MUSIC_TARGET_LUFS_MAX):
        return jsonify({"error": f"target_lufs must be null or a number from {MUSIC_TARGET_LUFS_MIN:g} "
                                 f"to {MUSIC_TARGET_LUFS_MAX:g}"}), 400
    store = music_listening_store()
    out = {}
    if "autoplay" in data:
        store.set_autoplay(user["username"], data["autoplay"])
        out["autoplay"] = data["autoplay"]
    if "target_lufs" in data:
        store.set_target_lufs(user["username"], target)
        out["target_lufs"] = target
    return jsonify(out)


@app.route("/api/music/sync", methods=["POST"])
@require_admin
def music_sync():
    """Downloads a playlist again: yt-dlp's archive skips every song already
    fetched, so only new entries download, and the songs it already stores
    are linked in afterwards (link_stored_songs) - holes filled. A playlist
    downloaded before its source was recorded needs its link once ("url");
    it is kept from then on."""
    data = request.get_json(silent=True) or {}
    catalog, _building = music_catalog()
    if catalog is None:
        return jsonify({"error": "the library is still being read - try again in a minute"}), 409
    pl = next((p for p in catalog["playlists"] if p["id"] == data.get("playlist")), None)
    if not pl or pl["source"] != "youtube":
        return jsonify({"error": "no such downloaded playlist"}), 404
    key = f"{pl['root']}:{pl['folder']}"
    given = (data.get("url") or "").strip()
    url = music_sync_url(given) if given else (music_sources().get(key) or {}).get("url")
    if given and not url:
        return jsonify({"error": "that is not a YouTube Music playlist or album link"}), 400
    if not url:
        return jsonify({"need_url": True,
                        "error": "this playlist was downloaded before its link was kept - paste it once"}), 400
    if given:
        music_sources().put(key, url=url, claimed_at=time.time())
    try:
        task_id, _category = queue_download(url)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify({"task_id": task_id, "url": url})


@app.route("/api/music/task/<task_id>", methods=["GET"])
@require_admin
def music_task(task_id):
    """How a sync is getting on, for the playlist page."""
    with task_lock:
        task = active_tasks.get(task_id)
        snap = {k: task.get(k) for k in ("id", "status", "progress", "created_at", "started_at",
                                         "completed_at", "skipped")} if task else None
    return jsonify(snap) if snap else (jsonify({"error": "Not found"}), 404)


# Guest accounts (role: "guest") may only reach the media browser - not the
# download dashboard, its JS, or any of the /api/* management endpoints.
GUEST_ALLOWED_FILES = {"media.html", "js/media.js", "js/gallery.js", "js/player.js",
                       "js/overlay-dismiss.js", "js/user-profile.js", "js/upscale.js",
                       "js/interp.js", "genres.html", "js/genres.js", "music.html", "js/music.js",
                       "js/listen_time.js", "vr.html"}


def guest_script_ok(filename):
    """Every signed-in account may load the front end's scripts (js/, vendor libraries included), so a
    page it may open - the VR viewer, the media browser - gets all of its modules without a list to
    keep in step. Scripts only: the HTML pages stay on the list above, and the data lives behind /api/*.
    Only a plain path counts: no "..", no backslash, no .bak - a prefix test alone would let
    js/../settings.html through. Nothing under web_files/js holds a secret (checked 2026-10-06)."""
    return (filename.startswith("js/") and filename.endswith(".js") and "\\" not in filename
            and os.path.normpath(filename) == filename)

# Secure static file server
@app.route("/", defaults={"filename": "index.html"})
@app.route("/<path:filename>")
def serve_static(filename):
    user = get_authenticated_user()
    # music.example.org is this same app: its front page is the music page.
    if filename == "index.html" and is_music_host():
        filename = "music.html"

    # The scripts for the public /share/<token> viewer page, which is meant to
    # work with no login at all, so its own scripts can't be gated behind one.
    anon_allowed_files = ("js/public_share.js", "js/gallery.js", "js/player.js",
                          "js/overlay-dismiss.js", "js/upscale.js")
    if not user and filename not in anon_allowed_files and not filename.startswith("css/"):
        # There is no local login page any more -- identity is Authelia's, and
        # serving a deleted file here turned every unauthenticated request into
        # a confusing 404.
        return login_redirect_for(request.url)

    if filename == "media-next.html":
        # The redesign became the media page (M6); the address it was tried at keeps working.
        return redirect("/media.html" + ("?" + request.query_string.decode("latin-1") if request.query_string else ""))

    if user and not is_admin(user):
        guest_allowed = (
            filename in GUEST_ALLOWED_FILES
            or guest_script_ok(filename)
            or filename.startswith("css/")
            or filename.startswith("downloads/")
            or filename.startswith("app/")
        )
        if not guest_allowed:
            return redirect("/media.html")

    requested_path = os.path.abspath(os.path.join(WEB_FILES_DIR, filename))

    if not requested_path.startswith(WEB_FILES_DIR + os.sep) and requested_path != WEB_FILES_DIR:
        return "403 Forbidden: Directory traversal attempt blocked.", 403

    if not os.path.exists(requested_path) or os.path.isdir(requested_path):
        return "404 Not Found", 404

    relative_path = os.path.relpath(requested_path, WEB_FILES_DIR)
    if relative_path == "media.html":
        return media_page_response(requested_path)
    return send_from_directory(WEB_FILES_DIR, relative_path)


FILTER_OPTIONS_PLACEHOLDER = "<!--FILTER_OPTIONS-->"


def media_page_response(path):
    """media.html with the filter panel's fixed options filled in, so its buttons
    do not wait for a request. "</" is escaped: the JSON sits inside a <script>."""
    with open(path, encoding="utf-8") as fh:
        html = fh.read()
    blob = json.dumps(static_filter_options(), ensure_ascii=False).replace("</", "<\\/")
    html = html.replace(FILTER_OPTIONS_PLACEHOLDER,
                        f'<script id="filterOptionsStatic" type="application/json">{blob}</script>', 1)
    resp = app.response_class(html, mimetype="text/html")
    resp.headers["Cache-Control"] = "no-cache"
    return resp

def warm_media_caches():
    """Builds the path index and loads the dimensions store at startup, in the
    background, so the first visitor after a restart - someone opening a share
    link - does not wait for a walk of the whole library."""
    try:
        t0 = time.time()
        get_media_index()
        media_dimensions_map()
        # The content map also picks the Safe covers of the character and
        # people circles; without it they show the old covers.
        content_levels_map()
        # The music page's catalogue: started here, built in its own thread
        # (the first time it reads every song's tags; later from the cache).
        music_catalog()
        discovery_service().get("characters", discovery_characters_build)
        discovery_service().get("people", discovery_people_build)
        image_search_service().start_index()
        print(f"[Info] media caches warmed in {time.time() - t0:.1f}s", flush=True)
    except Exception as exc:
        print(f"[Warning] warming media caches failed: {exc}", flush=True)
    try:
        check_empty_files()
    except Exception as exc:
        print(f"[Warning] empty-file registry not checked: {exc}", flush=True)


def start_pipeline_workers():
    """Started last, after every function in this module exists. A worker
    picks up a recovered task the moment it starts, and anything that task
    calls must already be defined: started halfway down the file, the YouTube
    worker ran into split_dual_link (defined further down) and died with a
    NameError on the first task after a restart (2026-09-25)."""
    for cat in ALL_CATEGORIES:
        threading.Thread(target=worker_loop, args=(cat,), daemon=True,
                         name=f"pipe-{cat}").start()


start_pipeline_workers()
threading.Thread(target=warm_media_caches, daemon=True).start()
threading.Thread(target=_upscale_worker_loop, daemon=True).start()
threading.Thread(target=_upscale_maintenance_loop, daemon=True).start()
threading.Thread(target=_stereo3d_loop, daemon=True, name="stereo3d").start()
threading.Thread(target=_stereo3d_maintenance_loop, daemon=True, name="stereo3d-upkeep").start()
threading.Thread(target=_stereo3d_backfill_loop, daemon=True, name="stereo3d-backfill").start()
threading.Thread(target=_dimensions_schedule_loop, daemon=True).start()
# The filter panel's first request should not be the one that waits for its options.
threading.Thread(target=cached_filter_options, daemon=True, name="filter-options-warm").start()
threading.Thread(target=_trash_purge_loop, daemon=True, name="trash-purge").start()
threading.Thread(target=_charbl_sweep_loop, daemon=True, name="character-blacklist-loop").start()
threading.Thread(target=_share_stats_flush_loop, daemon=True).start()
# Weeks of work, a few songs an hour: started here so that a restart - or a
# crash, the unit being Restart=always - simply carries on from the state file.
threading.Thread(target=media_dates.scan_loop, daemon=True, name="media-dates",
                 args=(BASE_DIR, get_all_roots, _is_hidden_or_partial, _entry_media_type,
                       poster_companion_names)).start()
threading.Thread(target=media_places.scan_loop, daemon=True, name="media-places",
                 args=(BASE_DIR, get_all_roots)).start()
_song_covers.start()
_video_trim.start()
_interp_redo.start()

@app.before_request
def reject_cross_site_writes():
    """CSRF guard. The browser's sign-in is a cookie at the proxy, so another site could make an admin's
    browser send a write. Browsers always put an Origin on a cross-site POST/PUT/PATCH/DELETE: it must be
    this site (or MEDIA_PUBLIC_URL). Requests without one (API key clients, curl) are not browsers."""
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return None
    origin = (request.headers.get("Origin") or "").strip()
    if not origin:
        return None
    from urllib.parse import urlsplit
    host = urlsplit(origin).netloc.lower()
    allowed = {(request.host or "").lower()}
    if PUBLIC_URL:
        allowed.add(urlsplit(PUBLIC_URL).netloc.lower())
    fwd = (request.headers.get("X-Forwarded-Host") or "").split(",")[0].strip().lower()
    if fwd:
        allowed.add(fwd)
    if host not in allowed:
        return jsonify({"error": "Cross-site request refused"}), 403
    return None


@app.after_request
def add_security_headers(resp):
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    resp.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
    path = request.path
    if path.startswith("/api/media/file") or (path.startswith("/api/public/share/") and path.endswith("/file")):
        # A library file (a downloaded .html or .svg, say) must never run script in this site's origin.
        resp.headers["Content-Security-Policy"] = "sandbox; default-src 'none'; img-src 'self' data:; media-src 'self'; style-src 'unsafe-inline'"
    return resp


if __name__ == "__main__":
    _bind = os.environ.get("MEDIA_BIND", "127.0.0.1")
    if SINGLE_USER and _bind not in ("127.0.0.1", "::1", "localhost"):
        sys.exit("MEDIA_SINGLE_USER switches the login off, so MEDIA_BIND must stay on 127.0.0.1 (it is %r)." % _bind)
    app.run(host=_bind, port=int(os.environ.get("MEDIA_PORT", "8000")), debug=False)
