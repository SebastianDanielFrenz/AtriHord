#!/usr/bin/env python3
"""Push new Steam screenshots to media-dl (POST /api/media/ingest).

Stdlib only. Config via environment:
  MEDIADL_URL   base URL, e.g. https://media.example.com   (required)
  MEDIADL_KEY   admin user's X-API-Key                      (required)
  STEAM_DIRS    os.pathsep-separated glob roots (default: Steam userdata dirs)
  STATE_FILE    sent-list (default ~/.local/state/media-dl-screenshot-sync.json)
  INTERVAL      seconds between scans (default 30; 0 = scan once and exit)
Uploads are idempotent server-side, so a lost state file only costs bandwidth.
"""
import glob
import json
import os
import sys
import time
import urllib.error
import urllib.request
import uuid

HOME = os.path.expanduser("~")
DEFAULT_PATTERNS = [
    os.path.join(HOME, ".local/share/Steam/userdata/*/760/remote/*/screenshots/*"),
    os.path.join(HOME, ".steam/steam/userdata/*/760/remote/*/screenshots/*"),
]
EXTS = {".jpg", ".jpeg", ".png", ".webp", ".mp4", ".webm", ".mkv"}
SETTLE_SECONDS = 3


def find_files(patterns):
    seen, out = set(), []
    for pat in patterns:
        for path in glob.glob(pat):
            real = os.path.realpath(path)
            if real in seen or not os.path.isfile(real):
                continue
            if os.path.splitext(real)[1].lower() not in EXTS:
                continue
            seen.add(real)
            out.append(real)
    return sorted(out)


def upload_name(path):
    # .../remote/<appid>/screenshots/<file>  ->  <appid>_<file>
    parts = path.replace("\\", "/").split("/")
    appid = parts[-3] if len(parts) >= 3 else "0"
    return f"{appid}_{parts[-1]}"


def post_file(base_url, key, path, name):
    boundary = uuid.uuid4().hex
    with open(path, "rb") as fh:
        body = fh.read()
    head = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"name\"\r\n\r\n{name}\r\n"
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{name}\"\r\n"
            "Content-Type: application/octet-stream\r\n\r\n").encode()
    payload = head + body + f"\r\n--{boundary}--\r\n".encode()
    req = urllib.request.Request(base_url.rstrip("/") + "/api/media/ingest", data=payload, method="POST")
    req.add_header("X-API-Key", key)
    req.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
    with urllib.request.urlopen(req, timeout=120) as resp:
        return json.load(resp)


def load_state(path):
    try:
        with open(path) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def save_state(path, state):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(state, fh)
    os.replace(tmp, path)


def scan_once(patterns, base_url, key, state_path, now=None):
    now = now or time.time()
    state = load_state(state_path)
    sent = 0
    for path in find_files(patterns):
        st = os.stat(path)
        sig = f"{st.st_size}:{int(st.st_mtime)}"
        if state.get(path) == sig or now - st.st_mtime < SETTLE_SECONDS:
            continue
        try:
            post_file(base_url, key, path, upload_name(path))
        except urllib.error.HTTPError as exc:
            if exc.code in (400, 413):          # permanent rejection: don't retry forever
                state[path] = sig
                print(f"rejected {path}: HTTP {exc.code}", file=sys.stderr)
            else:
                print(f"failed {path}: HTTP {exc.code}", file=sys.stderr)
            continue
        except (urllib.error.URLError, OSError) as exc:
            print(f"offline, will retry: {exc}", file=sys.stderr)
            break
        state[path] = sig
        sent += 1
        print(f"sent {path}")
    save_state(state_path, state)
    return sent


def main():
    base_url, key = os.environ.get("MEDIADL_URL"), os.environ.get("MEDIADL_KEY")
    if not base_url or not key:
        sys.exit("MEDIADL_URL and MEDIADL_KEY are required")
    dirs = os.environ.get("STEAM_DIRS")
    patterns = dirs.split(os.pathsep) if dirs else DEFAULT_PATTERNS
    state_path = os.environ.get("STATE_FILE", os.path.join(HOME, ".local/state/media-dl-screenshot-sync.json"))
    interval = float(os.environ.get("INTERVAL", "30"))
    while True:
        scan_once(patterns, base_url, key, state_path)
        if interval <= 0:
            return
        time.sleep(interval)


if __name__ == "__main__":
    main()
