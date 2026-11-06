"""
Invoked by yt-dlp via --exec once per downloaded video, but only when
server.py detected a playlist/channel URL (never for single videos).

Tracks recent YouTube video-download timestamps in a small local state
file and, if more than `threshold_count` videos were downloaded within
the last `window_minutes` minutes, sleeps for a random duration in
[min_seconds, max_seconds] before letting yt-dlp continue to the next
video. This throttles long channel/playlist downloads without slowing
down short ones.
"""
import os
import sys
import json
import time
import random

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(BASE_DIR, "config.json")
STATE_FILE = os.path.join(BASE_DIR, "youtube_download_events.json")

DEFAULTS = {
    "min_seconds": 0,
    "max_seconds": 180,
    "threshold_count": 10,
    "window_minutes": 10
}

def read_settings():
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        settings = dict(DEFAULTS)
        settings.update(cfg.get("youtube_playlist_sleep", {}))
        return settings
    except Exception:
        return dict(DEFAULTS)

# If the completed file is older than this, yt-dlp didn't actually download
# it just now (archive/existing-file skip) - don't count or sleep for it.
FRESH_DOWNLOAD_MAX_AGE_SECONDS = 300

def main():
    filepath = sys.argv[1] if len(sys.argv) > 1 else None
    if filepath:
        try:
            age = time.time() - os.path.getmtime(filepath)
            if age > FRESH_DOWNLOAD_MAX_AGE_SECONDS:
                return  # skipped entry (already downloaded), not a real download
        except OSError:
            pass  # file missing/inaccessible - fall through and treat as real

    settings = read_settings()
    now = time.time()
    cutoff = now - settings["window_minutes"] * 60

    events = []
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                events = json.load(f)
        except Exception:
            events = []

    events = [t for t in events if isinstance(t, (int, float)) and t >= cutoff]
    events.append(now)

    temp_file = STATE_FILE + ".tmp"
    try:
        with open(temp_file, "w", encoding="utf-8") as f:
            json.dump(events, f)
        os.replace(temp_file, STATE_FILE)
    except Exception as e:
        print(f"[rate-limit-guard] Warning: failed to persist state: {str(e)}", flush=True)

    if len(events) > settings["threshold_count"]:
        pause = random.uniform(settings["min_seconds"], settings["max_seconds"])
        print(
            f"[rate-limit-guard] {len(events)} videos in the last "
            f"{settings['window_minutes']}min (> {settings['threshold_count']}), "
            f"sleeping {pause:.1f}s before the next video...",
            flush=True
        )
        time.sleep(pause)

if __name__ == "__main__":
    main()
