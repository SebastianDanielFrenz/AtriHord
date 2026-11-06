#!/usr/bin/env python3
"""Downloads the pixiv artists the account follows, slowly, forever.

Two jobs, one daemon (user unit ksf-pixiv-follow.service):

  BACKFILL  once per followed artist: if the library holds 10 or fewer of their
            files, their whole gallery is downloaded. The artists go one at a
            time, with a long pause after each (at least as long as the download
            took, at least 20 minutes). The backfill takes months, not hours.
  SYNC      from then on, their new works. A daily read of pixiv's "new from
            followed artists" feed, plus a slow sweep that checks one artist
            every 15 minutes (a full round of about 4,300 artists takes about
            six weeks), queue whatever is missing. Nothing here is real-time,
            on purpose.

It downloads nothing itself. Every download is an ordinary media-dl task, via
POST /api/download with the root account's API key, so it runs in the pixiv
pipeline like a pasted URL: it shows in the UI, it survives a server restart
(which requeues it under the same id), and PixivUtil2's database keeps what
was banned out. A user's own pixiv tasks go first: nothing is submitted while
another pixiv task is queued or running.

Its own reads of pixiv (the follow list, the feed, one artist's work ids) use
PixivUtil2's session cookie, read-only from PixivUtil2/config.ini, and happen
only while no download of its own is running.

Everything it knows is in pixiv_follow_sync.json, written atomically after
every step, so a reboot or a restart continues where it stopped - including a
download that was in flight. Settings: pixiv_follow_sync.settings.json
(optional, overrides DEFAULTS key by key). A file named pixiv_follow_sync.pause
next to it pauses everything, including new submissions (a running download
finishes).

    pixiv_venv/bin/python pixiv_follow_sync.py run       # the daemon
    pixiv_venv/bin/python pixiv_follow_sync.py status    # what it is doing
    pixiv_venv/bin/python pixiv_follow_sync.py pause|resume

Design notes: vault ticket on the pixiv follow sync (project ksf_web_dl__local).
"""
import json
import os
import random
import re
import shutil
import signal
import sqlite3
import sys
import tempfile
import time
import urllib.error
import urllib.request

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(BASE_DIR, "pixiv_follow_sync.json")
SETTINGS_FILE = os.path.join(BASE_DIR, "pixiv_follow_sync.settings.json")
PAUSE_FILE = os.path.join(BASE_DIR, "pixiv_follow_sync.pause")
USERS_FILE = os.path.join(BASE_DIR, "users.json")
PIXIV_CONFIG = os.path.join(BASE_DIR, "PixivUtil2", "config.ini")
PIXIV_DB = os.path.join(BASE_DIR, "PixivUtil2", "db.sqlite")
PIXIV_DIR = os.path.join(BASE_DIR, "web_files", "downloads", "pixiv")

HOUR = 3600
DAY = 24 * HOUR
DEFAULTS = {
    "enabled": True,
    "backfill_enabled": True,
    "sync_enabled": True,
    "server_url": "http://127.0.0.1:8000",
    "api_user": "root",
    # The user's rule: more than this many files already there, no backfill.
    "backfill_threshold": 10,
    # The pause after a download = its duration x gap_ratio, clamped, then
    # stretched by a random 0..gap_jitter so the rhythm is not a clock.
    "gap_ratio": 1.0,
    "gap_jitter": 0.5,
    "backfill_gap_min_s": 20 * 60,
    "backfill_gap_max_s": 4 * HOUR,
    "sync_gap_min_s": 4 * 60,
    "sync_gap_max_s": 1 * HOUR,
    "follows_refresh_s": 7 * DAY,
    "feed_interval_s": DAY,
    "feed_max_pages": 20,
    "sweep_interval_s": 15 * 60,
    "request_gap_s": 3,
    # Free space under which nothing more is downloaded. The backfill of every
    # followed artist is larger than the disk, so this is where it stops.
    "free_floor_backfill_gb": 500,
    "free_floor_sync_gb": 150,
    "max_attempts": 3,
    "retry_after_s": 6 * HOUR,
    # A "complete" artist (next to nothing missing) is synced by artist URL,
    # which fetches everything missing; others work by work.
    "complete_missing_max": 3,
    "complete_missing_share": 0.05,
    "tick_s": 60,
}
# Sidecars PixivUtil2 writes next to a work; they are not items.
SIDECAR_EXT = {".js", ".json", ".txt", ".html", ".htm", ".ini", ".part"}
FOLDER_ID = re.compile(r"\((\d+)\)\s*$")
EVENTS_KEPT = 200


def log(msg):
    print(time.strftime("%Y-%m-%d %H:%M:%S ") + msg, flush=True)


def load_settings(path=SETTINGS_FILE):
    s = dict(DEFAULTS)
    try:
        with open(path, encoding="utf-8") as f:
            s.update(json.load(f))
    except FileNotFoundError:
        pass
    except (OSError, ValueError) as exc:
        log(f"settings file unreadable, using defaults: {exc}")
    return s


def write_json_atomic(path, data):
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".pfs-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ------------------------------------------------------------------ pixiv

class PixivError(Exception):
    """kind: "throttled" (back off), "session" (cookie no longer signed in),
    "gone" (this artist left pixiv or is hidden), "other"."""

    def __init__(self, kind, msg):
        super().__init__(msg)
        self.kind = kind


class Pixiv:
    """The few pixiv reads the daemon needs, with PixivUtil2's own cookie and
    browser impersonation, so pixiv sees the same client either way."""

    def __init__(self, config_path=PIXIV_CONFIG):
        self.config_path = config_path
        self._session = None
        self._cookie = None

    def _cookie_now(self):
        import configparser
        cfg = configparser.RawConfigParser(strict=False)
        cfg.read(self.config_path, encoding="utf-8")
        cookie = cfg.get("Authentication", "cookie", fallback="").strip()
        imp = cfg.get("Authentication", "userAgentImpersonation", fallback="").strip() or "firefox135"
        return cookie, imp

    def _get(self, url):
        cookie, imp = self._cookie_now()
        if not cookie:
            raise PixivError("session", "no pixiv cookie in PixivUtil2/config.ini")
        if self._session is None or cookie != self._cookie:
            # PixivUtil2 writes a refreshed cookie back into config.ini; follow it.
            from curl_cffi import requests
            self._session = requests.Session(impersonate=imp)
            self._session.cookies.set("PHPSESSID", cookie, domain=".pixiv.net")
            self._cookie = cookie
        try:
            r = self._session.get(url, headers={"Referer": "https://www.pixiv.net/"}, timeout=60)
        except Exception as exc:
            raise PixivError("other", f"{url}: {exc}")
        if r.status_code in (429, 503):
            raise PixivError("throttled", f"{url}: HTTP {r.status_code}")
        if r.status_code in (401, 403) and "/following" in url:
            raise PixivError("session", f"{url}: HTTP {r.status_code}")
        try:
            data = r.json()
        except ValueError:
            # Cloudflare's challenge page is HTML: treat it as being throttled.
            raise PixivError("throttled", f"{url}: HTTP {r.status_code}, not JSON")
        if data.get("error"):
            message = str(data.get("message") or "")
            if r.status_code in (400, 404) or "/profile/all" in url:
                raise PixivError("gone", f"{url}: {message or r.status_code}")
            raise PixivError("other", f"{url}: {message or r.status_code}")
        return data.get("body") or {}

    def own_id(self):
        cookie, _ = self._cookie_now()
        uid = cookie.split("_")[0]
        if not uid.isdigit():
            raise PixivError("session", "the pixiv cookie does not name an account")
        return uid

    def following(self, gap_s):
        """[(member_id, name)] in pixiv's order (most recently followed first),
        public and private follows. Raises unless every page was read, so a
        failed read never looks like a wave of unfollows."""
        uid = self.own_id()
        out, seen = [], set()
        for rest in ("show", "hide"):
            offset = 0
            while True:
                body = self._get(f"https://www.pixiv.net/ajax/user/{uid}/following"
                                 f"?offset={offset}&limit=100&rest={rest}&lang=en")
                users = body.get("users") or []
                for u in users:
                    mid = str(u.get("userId") or "")
                    if mid.isdigit() and mid not in seen:
                        seen.add(mid)
                        out.append((mid, u.get("userName") or ""))
                offset += len(users)
                if not users or offset >= int(body.get("total") or 0):
                    break
                time.sleep(gap_s)
            time.sleep(gap_s)
        return out

    def work_ids(self, member_id):
        """Every illustration and manga id of one artist: one request."""
        body = self._get(f"https://www.pixiv.net/ajax/user/{member_id}/profile/all?lang=en")
        ids = set()
        for key in ("illusts", "manga"):
            part = body.get(key) or {}
            ids.update(int(i) for i in part if str(i).isdigit())
        return ids

    def feed_page(self, page):
        """([(work_id, member_id)], is_last) - newest first."""
        body = self._get(f"https://www.pixiv.net/ajax/follow_latest/illust?p={page}&mode=all&lang=en")
        ids = (body.get("page") or {}).get("ids") or []
        owner = {str(t.get("id")): str(t.get("userId") or "")
                 for t in (body.get("thumbnails") or {}).get("illust") or []}
        items = [(int(i), owner.get(str(i), "")) for i in ids if str(i).isdigit()]
        return items, bool((body.get("page") or {}).get("isLastPage")) or not items


# ------------------------------------------------------------ media-dl

class ServerError(Exception):
    pass


class Server:
    """media-dl's own download queue, as a script sees it."""

    def __init__(self, url, api_user, users_file=USERS_FILE):
        self.url = url.rstrip("/")
        self.api_user = api_user
        self.users_file = users_file

    def _token(self):
        # Read at every call rather than copied: rotating the token in the UI
        # or with api_token.py must not break the daemon.
        with open(self.users_file, encoding="utf-8") as f:
            for u in json.load(f):
                if u.get("username") == self.api_user and u.get("api_token"):
                    return u["api_token"]
        raise ServerError(f"user {self.api_user!r} has no API token (api_token.py issue --user ...)")

    def _call(self, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.url + path, data=data, method="POST" if body is not None else "GET")
        req.add_header("X-API-Key", self._token())
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise ServerError(f"{path}: HTTP {exc.code} {exc.read()[:200]!r}")
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise ServerError(f"{path}: {exc}")

    def tasks(self):
        return self._call("/api/tasks")

    def submit(self, url):
        out = self._call("/api/download", {"url": url})
        if not out.get("task_id"):
            raise ServerError(f"no task id for {url}: {out}")
        return out["task_id"]


# ------------------------------------------------------------- library

class Library:
    """What is already here: files on disk, and PixivUtil2's own record."""

    def __init__(self, pixiv_dir=PIXIV_DIR, db_path=PIXIV_DB):
        self.pixiv_dir = pixiv_dir
        self.db_path = db_path

    def folders(self):
        """member_id -> [folder]. An artist who renamed has several
        "Name (id)" folders; all of them count."""
        out = {}
        try:
            with os.scandir(self.pixiv_dir) as it:
                for e in it:
                    m = FOLDER_ID.search(e.name)
                    if m and e.is_dir(follow_symlinks=False) and not e.name.startswith("."):
                        out.setdefault(m.group(1), []).append(e.path)
        except FileNotFoundError:
            pass
        return out

    def files_on_disk(self, member_id, folders=None):
        folders = self.folders() if folders is None else folders
        n = 0
        for d in folders.get(str(member_id), []):
            for root, dirs, files in os.walk(d):
                dirs[:] = [x for x in dirs if not x.startswith(".")]
                for name in files:
                    if name.startswith(".") or os.path.splitext(name)[1].lower() in SIDECAR_EXT:
                        continue
                    try:
                        # 0 bytes = a ban's tombstone, not an item.
                        if os.path.getsize(os.path.join(root, name)) > 0:
                            n += 1
                    except OSError:
                        pass
        return n

    def known_ids(self, member_id):
        """Work ids PixivUtil2 has a record of: downloaded, or deleted with a
        ban (the row is what keeps a banned work out)."""
        if not os.path.exists(self.db_path):
            return set()
        conn = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True, timeout=30)
        try:
            return {r[0] for r in conn.execute(
                "SELECT image_id FROM pixiv_master_image WHERE member_id = ?", (int(member_id),))}
        finally:
            conn.close()

    def free_gb(self):
        return shutil.disk_usage(self.pixiv_dir).free / 1e9


# ---------------------------------------------------------------- sync

def fresh_state():
    return {
        "version": 1,
        "follows": {},          # member_id -> see enroll()
        "order": [],            # member ids, pixiv's follow order
        "follows_refreshed_at": 0,
        "feed_checked_at": 0,
        "feed_seen_max": 0,
        "sweep_at": 0,
        "sync_queue": {},       # member_id -> [work ids]
        "inflight": None,
        "next_submit_at": 0,
        "pixiv_backoff_until": 0,
        "pixiv_backoff_s": 0,
        "blocked": None,        # why nothing is happening, for status
        "events": [],
    }


class Sync:
    def __init__(self, state_file=STATE_FILE, settings_file=SETTINGS_FILE, pause_file=PAUSE_FILE,
                 pixiv=None, server=None, library=None, clock=time.time, sleep=time.sleep):
        self.state_file = state_file
        self.settings_file = settings_file
        self.pause_file = pause_file
        self.clock = clock
        self.sleep = sleep
        self.s = load_settings(settings_file)
        self.pixiv = pixiv or Pixiv()
        self.server = server or Server(self.s["server_url"], self.s["api_user"])
        self.library = library or Library()
        self.state = self._load()

    # --- state
    def _load(self):
        try:
            with open(self.state_file, encoding="utf-8") as f:
                st = json.load(f)
        except FileNotFoundError:
            return fresh_state()
        base = fresh_state()
        base.update(st)
        busy = (base["inflight"] or {}).get("member")
        for mid, f in base["follows"].items():
            if f.get("backfill") == "running" and mid != busy:
                f["backfill"] = "pending"
        return base

    def save(self):
        write_json_atomic(self.state_file, self.state)

    def event(self, msg):
        log(msg)
        ev = self.state["events"]
        ev.append([int(self.clock()), msg])
        del ev[:-EVENTS_KEPT]

    def block(self, why):
        if self.state.get("blocked") != why:
            self.state["blocked"] = why
            if why:
                self.event("waiting: " + why)

    # --- the loop
    def tick(self):
        self.s = load_settings(self.settings_file)
        now = self.clock()
        if self.state["inflight"]:
            self.poll_inflight(now)
            if self.state["inflight"]:
                return self.save()
        if not self.s["enabled"] or os.path.exists(self.pause_file):
            self.block("paused")
            return self.save()
        try:
            if now >= self.state["pixiv_backoff_until"]:
                # One pixiv read job per tick, and never during a download.
                if self.read_job(now):
                    self.state["pixiv_backoff_s"] = 0
                    return self.save()
        except PixivError as exc:
            self.pixiv_failed(now, exc)
            return self.save()
        self.maybe_submit(now)
        self.save()

    def pixiv_failed(self, now, exc):
        if exc.kind == "session":
            wait = DAY
            self.block("pixiv session not signed in - refresh the cookie in PixivUtil2/config.ini")
        else:
            wait = min(DAY, max(HOUR, 2 * (self.state["pixiv_backoff_s"] or HOUR // 2)))
        self.state["pixiv_backoff_s"] = wait
        self.state["pixiv_backoff_until"] = now + wait
        self.event(f"pixiv read failed ({exc.kind}: {exc}); no pixiv reads for {wait // 60} min")

    def read_job(self, now):
        st, s = self.state, self.s
        if now - st["follows_refreshed_at"] >= s["follows_refresh_s"]:
            self.refresh_follows(now)
            return True
        if not st["follows"]:
            return False
        if s["sync_enabled"] and now - st["feed_checked_at"] >= s["feed_interval_s"]:
            self.read_feed(now)
            return True
        if s["sync_enabled"] and now - st["sweep_at"] >= s["sweep_interval_s"]:
            st["sweep_at"] = now
            mid = self.next_to_sweep()
            if mid:
                self.check_artist(mid, now)
                return True
        return False

    # --- follow list
    def refresh_follows(self, now):
        st = self.state
        listed = self.pixiv.following(self.s["request_gap_s"])
        folders = self.library.folders()
        added = 0
        ids = []
        for mid, name in listed:
            ids.append(mid)
            f = st["follows"].get(mid)
            if f is None:
                st["follows"][mid] = self.enroll(mid, name, now, folders)
                added += 1
            else:
                f["name"] = name or f.get("name")
                f.pop("unfollowed_at", None)
        gone = 0
        current = set(ids)
        for mid, f in st["follows"].items():
            if mid not in current and not f.get("unfollowed_at"):
                f["unfollowed_at"] = now
                gone += 1
        st["order"] = ids
        st["follows_refreshed_at"] = now
        pending = sum(1 for m in ids if st["follows"][m]["backfill"] == "pending")
        self.event(f"follow list: {len(ids)} artists, {added} new, {gone} unfollowed; "
                   f"{pending} waiting for their backfill")

    def enroll(self, mid, name, now, folders):
        """A newly seen follow. The backfill decision needs only the disk: over
        the threshold, the artist is left alone until the sweep has examined
        them (their ids against PixivUtil2's record)."""
        n = self.library.files_on_disk(mid, folders)
        over = n > self.s["backfill_threshold"]
        return {
            "name": name,
            "followed_seen_at": now,
            "files_at_enroll": n,
            "backfill": "skipped" if over else "pending",
            # member: synced by artist URL (fetches everything missing);
            # artwork: by work URL, only ids above `baseline`.
            "mode": None if over else "member",
            "baseline": 0,
            "checked_at": 0,
            "attempts": 0,
            "retry_at": 0,
        }

    # --- examining one artist
    def check_artist(self, mid, now):
        f = self.state["follows"][mid]
        try:
            ids = self.pixiv.work_ids(mid)
        except PixivError as exc:
            if exc.kind != "gone":
                raise
            f["checked_at"] = now
            f["gone"] = str(exc)
            self.event(f"{self.label(mid)}: not available on pixiv ({exc}); skipped")
            return
        f.pop("gone", None)
        f["checked_at"] = now
        known = self.library.known_ids(mid)
        if f["mode"] is None:
            missing = ids - known
            limit = max(self.s["complete_missing_max"], int(len(ids) * self.s["complete_missing_share"]))
            if len(missing) <= limit:
                f["mode"], f["baseline"] = "member", 0
            else:
                # Already partly here and not backfilled, by the user's rule:
                # from now on, only what is new.
                f["mode"], f["baseline"] = "artwork", max(ids) if ids else 0
            self.event(f"{self.label(mid)}: {len(ids)} works on pixiv, {len(missing)} not here; "
                       f"synced by {'artist' if f['mode'] == 'member' else 'work, new ones only'}")
        if f["backfill"] == "pending":
            return
        new = sorted(i for i in ids if i > f["baseline"] and i not in known)
        if new:
            self.enqueue(mid, new)

    def next_to_sweep(self):
        """Never-examined artists over the threshold first (they have no sync
        mode yet), then whoever was checked longest ago. Artists still waiting
        for their backfill are left out: it will fetch everything anyway."""
        best, best_key = None, None
        for mid in self.state["order"]:
            f = self.state["follows"].get(mid)
            if not f or f.get("unfollowed_at") or f["backfill"] in ("pending", "running"):
                continue
            key = (f["mode"] is not None, f["checked_at"])
            if best_key is None or key < best_key:
                best, best_key = mid, key
        return best

    # --- the feed
    def read_feed(self, now):
        st = self.state
        seen = st["feed_seen_max"]
        top, queued, examine = seen, 0, 0
        for page in range(1, self.s["feed_max_pages"] + 1):
            items, last = self.pixiv.feed_page(page)
            older = False
            for wid, mid in items:
                top = max(top, wid)
                if wid <= seen:
                    older = True
                    continue
                f = st["follows"].get(mid)
                if not f or f.get("unfollowed_at") or f["backfill"] in ("pending", "running"):
                    continue
                if f["mode"] is None:
                    # Unexamined: move them to the front of the sweep.
                    f["checked_at"] = -1
                    examine += 1
                    continue
                if wid > f["baseline"] and wid not in self.library.known_ids(mid):
                    self.enqueue(mid, [wid])
                    queued += 1
            # The first read has nothing to stop at: one page is enough, the
            # sweep covers the rest.
            if last or older or not seen:
                break
            self.sleep(self.s["request_gap_s"])
        st["feed_seen_max"] = top
        st["feed_checked_at"] = now
        self.event(f"feed: {queued} new works queued" + (f", {examine} artists to examine" if examine else ""))

    def enqueue(self, mid, ids):
        q = self.state["sync_queue"].setdefault(mid, [])
        for i in ids:
            if i not in q:
                q.append(i)

    # --- submitting
    def pick(self, now):
        """((kind, member, url, work ids), None), or (None, why not). New works
        first, then the backfill in follow order (most recently followed
        first)."""
        st, s = self.state, self.s
        free = self.library.free_gb()
        why = None
        if s["sync_enabled"] and st["sync_queue"]:
            if free < s["free_floor_sync_gb"]:
                return None, f"only {free:.0f} GB free (floor for new works: {s['free_floor_sync_gb']} GB)"
            for mid, ids in st["sync_queue"].items():
                f = st["follows"].get(mid)
                if not f or f.get("unfollowed_at") or not ids or f.get("retry_at", 0) > now:
                    continue
                if f["mode"] == "member":
                    return ("sync", mid, f"https://www.pixiv.net/users/{mid}", list(ids)), None
                return ("sync", mid, f"https://www.pixiv.net/artworks/{min(ids)}", [min(ids)]), None
        if s["backfill_enabled"]:
            for mid in st["order"]:
                f = st["follows"].get(mid)
                if not f or f["backfill"] != "pending" or f.get("unfollowed_at") or f.get("retry_at", 0) > now:
                    continue
                if free < s["free_floor_backfill_gb"]:
                    return None, f"only {free:.0f} GB free (backfill floor: {s['free_floor_backfill_gb']} GB)"
                return ("backfill", mid, f"https://www.pixiv.net/users/{mid}", []), None
        return None, why

    def maybe_submit(self, now):
        st = self.state
        if now < st["next_submit_at"]:
            return
        item, why = self.pick(now)
        if not item:
            self.block(why)
            return
        try:
            others = [t for t in self.server.tasks()
                      if t.get("category") == "pixiv" and t.get("status") in ("Queued", "Running")]
        except ServerError as exc:
            self.block(f"media-dl not answering ({exc})")
            return
        if others:
            self.block("another pixiv download (not this daemon's) is queued or running")
            return
        kind, mid, url, ids = item
        try:
            task_id = self.server.submit(url)
        except ServerError as exc:
            self.block(f"media-dl refused the download ({exc})")
            return
        self.block(None)
        if kind == "backfill":
            st["follows"][mid]["backfill"] = "running"
        st["inflight"] = {"task": task_id, "kind": kind, "member": mid, "url": url, "ids": ids,
                          "submitted_at": now, "files_before": self.library.files_on_disk(mid),
                          "known_before": len(self.library.known_ids(mid))}
        self.event(f"{kind} {self.label(mid)}: submitted {url} (task {task_id})")

    def poll_inflight(self, now):
        inf = self.state["inflight"]
        try:
            tasks = self.server.tasks()
        except ServerError as exc:
            self.block(f"media-dl not answering ({exc})")
            return
        task = next((t for t in tasks if t.get("id") == inf["task"]), None)
        status = task.get("status") if task else None
        if status in ("Queued", "Running"):
            if status == "Running" and not inf.get("started_at"):
                inf["started_at"] = task.get("started_at") or now
            self.block(None)
            return
        # Finished - or already gone from the list (completed tasks age out
        # after 10 minutes, and this daemon may have been down that long).
        # Either way the library says what happened.
        self.finish(now, inf, status or "gone", task or {})

    def finish(self, now, inf, status, task):
        st, s = self.state, self.s
        mid = inf["member"]
        f = st["follows"].get(mid) or {}
        started = task.get("started_at") or inf.get("started_at") or inf["submitted_at"]
        ended = task.get("completed_at") or now
        took = max(0, ended - started)
        files = self.library.files_on_disk(mid)
        known = self.library.known_ids(mid)
        ok = status == "Completed"
        if status == "gone":
            # Aged out of the task list while this daemon was down: judge by
            # whether anything arrived.
            ok = files > inf["files_before"] or len(known) > inf.get("known_before", 0)
        if inf["kind"] == "backfill":
            if status == "Terminated":
                # Stopped by hand in the UI: that artist is not wanted in full.
                # Left to the sweep, which will sync their new works only.
                f.update(backfill="cancelled", mode=None, attempts=0, retry_at=0)
            elif ok:
                f.update(backfill="done", attempts=0, retry_at=0, backfilled_at=now,
                         checked_at=now, mode="member", baseline=0)
            else:
                self.retry_later(f, now)
                if f["attempts"] >= s["max_attempts"]:
                    # Given up; the sweep examines them like any artist that
                    # was partly here, so the new works still come.
                    f.update(backfill="failed", mode=None, attempts=0, retry_at=0)
                else:
                    f["backfill"] = "pending"
        else:
            q = [i for i in st["sync_queue"].get(mid, []) if i not in known]
            if f.get("mode") == "artwork":
                work = inf["ids"][0] if inf["ids"] else None
                if work in q and status == "Terminated":
                    q.remove(work)
                elif work in q:
                    self.retry_later(f, now)
                    if f["attempts"] >= s["max_attempts"]:
                        q.remove(work)
                        f["attempts"], f["retry_at"] = 0, 0
                        self.event(f"sync {self.label(mid)}: gave up on work {work}")
                else:
                    f["attempts"], f["retry_at"] = 0, 0
            elif q and not ok and status != "Terminated":
                self.retry_later(f, now)
                if f["attempts"] >= s["max_attempts"]:
                    # The sweep queues whatever is still missing, in weeks.
                    q = []
                    f["attempts"], f["retry_at"] = 0, 0
            else:
                # One artist run fetched whatever it could get.
                q = []
                f["attempts"], f["retry_at"] = 0, 0
            if q:
                st["sync_queue"][mid] = q
            else:
                st["sync_queue"].pop(mid, None)
        gap_min = s["backfill_gap_min_s"] if inf["kind"] == "backfill" else s["sync_gap_min_s"]
        gap_max = s["backfill_gap_max_s"] if inf["kind"] == "backfill" else s["sync_gap_max_s"]
        gap = min(gap_max, max(gap_min, took * s["gap_ratio"])) * (1 + random.random() * s["gap_jitter"])
        st["next_submit_at"] = now + gap
        st["inflight"] = None
        self.event(f"{inf['kind']} {self.label(mid)}: {status} after {took / 60:.0f} min, "
                   f"{files} files here now; next download in {gap / 60:.0f} min")

    def retry_later(self, f, now):
        f["attempts"] = f.get("attempts", 0) + 1
        f["retry_at"] = now + self.s["retry_after_s"] * f["attempts"]

    def label(self, mid):
        name = (self.state["follows"].get(mid) or {}).get("name")
        return f"{name} ({mid})" if name else mid

    # --- status
    def summary(self):
        st = self.state
        follows = [st["follows"][m] for m in st["order"] if m in st["follows"]]
        count = {}
        for f in follows:
            count[f["backfill"]] = count.get(f["backfill"], 0) + 1
        now = self.clock()
        lines = [
            f"followed artists: {len(follows)}  (list read {ago(now, st['follows_refreshed_at'])})",
            "backfill: " + ", ".join(f"{k} {v}" for k, v in sorted(count.items())),
            f"sync: {sum(len(v) for v in st['sync_queue'].values())} works queued for "
            f"{len(st['sync_queue'])} artists; feed read {ago(now, st['feed_checked_at'])}; "
            f"checked on pixiv {sum(1 for f in follows if f['checked_at'] > 0)} of "
            f"{sum(1 for f in follows if f['backfill'] not in ('pending', 'running'))} not waiting for a backfill",
        ]
        if st["inflight"]:
            i = st["inflight"]
            lines.append(f"downloading: {i['kind']} {self.label(i['member'])}, task {i['task']}, "
                         f"submitted {ago(now, i['submitted_at'])}")
        elif st["next_submit_at"] > now:
            lines.append(f"next download in {(st['next_submit_at'] - now) / 60:.0f} min")
        if os.path.exists(self.pause_file) or not self.s["enabled"]:
            lines.append("PAUSED")
        if st["blocked"]:
            lines.append("waiting: " + st["blocked"])
        if st["pixiv_backoff_until"] > now:
            lines.append(f"no pixiv reads for another {(st['pixiv_backoff_until'] - now) / 60:.0f} min")
        try:
            lines.append(f"free space: {self.library.free_gb():.0f} GB")
        except OSError:
            pass
        lines.append("recent:")
        lines += [f"  {time.strftime('%m-%d %H:%M', time.localtime(t))}  {m}" for t, m in st["events"][-15:]]
        return "\n".join(lines)


def ago(now, t):
    if not t:
        return "never"
    d = now - t
    return f"{d / 60:.0f} min ago" if d < 2 * HOUR else f"{d / HOUR:.1f} h ago" if d < 2 * DAY else f"{d / DAY:.1f} days ago"


def run(sync):
    stop = {"now": False}

    def on_signal(signum, frame):
        stop["now"] = True

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)
    log("pixiv follow sync started" + (f"; resuming task {sync.state['inflight']['task']}"
                                       if sync.state["inflight"] else ""))
    while not stop["now"]:
        try:
            sync.tick()
        except Exception as exc:          # one bad tick must not end the daemon
            log(f"tick failed: {type(exc).__name__}: {exc}")
        end = time.time() + sync.s["tick_s"]
        while not stop["now"] and time.time() < end:
            time.sleep(1)
    # The state is saved after every tick; nothing is in memory only.
    log("pixiv follow sync stopped")


def main(argv):
    cmd = argv[1] if len(argv) > 1 else "status"
    if cmd == "run":
        run(Sync())
    elif cmd == "status":
        print(Sync().summary())
    elif cmd == "pause":
        open(PAUSE_FILE, "w").close()
        print("paused (a running download finishes)")
    elif cmd == "resume":
        try:
            os.unlink(PAUSE_FILE)
        except FileNotFoundError:
            pass
        print("resumed")
    else:
        print(__doc__)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
