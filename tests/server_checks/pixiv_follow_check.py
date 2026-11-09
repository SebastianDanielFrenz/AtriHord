"""pixiv_follow_sync.py: the backfill of followed artists and the sync of their
new works, against a fake pixiv, a fake media-dl task queue, a temp library
and a temp PixivUtil2 database, on a clock the check moves by hand.

Nothing real is read or written: no pixiv request, no download, no file under
/data2. The module is taken from the stage dir when it is part of the change,
else from production.

  venv/bin/python pixiv_follow_check.py /tmp/<stage>/server.py
"""
import json
import os
import shutil
import sqlite3
import sys
import tempfile

BASE = os.environ.get("ATRIHORD_DIR") or os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
STAGE = os.path.dirname(os.path.abspath(sys.argv[1])) if len(sys.argv) > 1 else BASE
HERE = os.path.dirname(os.path.abspath(__file__))
for d in (STAGE, BASE, os.path.dirname(os.path.dirname(HERE))):
    if os.path.exists(os.path.join(d, "pixiv_follow_sync.py")):
        sys.path.insert(0, d)
        break
import pixiv_follow_sync as pfs      # noqa: E402

ok = True


def check(label, cond, detail=""):
    global ok
    ok = ok and bool(cond)
    print(("PASS " if cond else "FAIL ") + label + (f"  ({detail})" if detail and not cond else ""))


tmp = tempfile.mkdtemp(prefix="pixiv_follow_check_")
PIX = os.path.join(tmp, "pixiv")
DB = os.path.join(tmp, "db.sqlite")
os.makedirs(PIX)
conn = sqlite3.connect(DB)
conn.execute("CREATE TABLE pixiv_master_image (image_id INTEGER PRIMARY KEY, member_id INTEGER, title TEXT,"
             " save_name TEXT, created_date DATE, last_update_date DATE, is_manga TEXT, caption TEXT)")
conn.commit()


def put_files(folder, n, start=0, ext=".jpg", size=10):
    d = os.path.join(PIX, folder)
    os.makedirs(d, exist_ok=True)
    for i in range(start, start + n):
        with open(os.path.join(d, f"{i}_p0 - t{ext}"), "wb") as f:
            f.write(b"x" * size)


def record(member, ids):
    conn.executemany("INSERT OR IGNORE INTO pixiv_master_image (image_id, member_id) VALUES (?, ?)",
                     [(i, int(member)) for i in ids])
    conn.commit()


class Clock:
    t = 1_000_000.0

    def __call__(self):
        return self.t


clock = Clock()


class FakePixiv:
    def __init__(self):
        self.follows = []            # [(id, name)]
        self.works = {}              # member -> set(ids)
        self.feed = []               # newest first [(work, member)]
        self.fail = None             # PixivError to raise on the next read
        self.calls = []

    def _maybe_fail(self, what):
        self.calls.append(what)
        if self.fail:
            exc, self.fail = self.fail, None
            raise exc

    def following(self, gap_s):
        self._maybe_fail("following")
        return list(self.follows)

    def work_ids(self, member):
        self._maybe_fail(("works", member))
        if member not in self.works:
            raise pfs.PixivError("gone", "user left pixiv")
        return set(self.works[member])

    def feed_page(self, page):
        self._maybe_fail(("feed", page))
        chunk = self.feed[(page - 1) * 3: page * 3]
        return chunk, page * 3 >= len(self.feed)


class FakeServer:
    def __init__(self):
        self.tasks_ = {}
        self.submitted = []
        self.down = False
        self.n = 0

    def tasks(self):
        if self.down:
            raise pfs.ServerError("connection refused")
        return [dict(t) for t in self.tasks_.values()]

    def submit(self, url):
        if self.down:
            raise pfs.ServerError("connection refused")
        self.n += 1
        tid = f"t{self.n}"
        self.tasks_[tid] = {"id": tid, "url": url, "category": "pixiv", "status": "Queued"}
        self.submitted.append(url)
        return tid

    def run(self, tid, status="Completed", took=600):
        t = self.tasks_[tid]
        t["started_at"] = clock.t
        clock.t += took
        t["status"] = status
        t["completed_at"] = clock.t


pixiv, server = FakePixiv(), FakeServer()
library = pfs.Library(PIX, DB)
free = {"gb": 2000.0}
library.free_gb = lambda: free["gb"]
STATE = os.path.join(tmp, "state.json")
SETTINGS = os.path.join(tmp, "settings.json")
PAUSE = os.path.join(tmp, "pause")


def new_sync():
    return pfs.Sync(STATE, SETTINGS, PAUSE, pixiv=pixiv, server=server, library=library,
                    clock=clock, sleep=lambda s: None)


def tick(sync, n=1, step=60):
    for _ in range(n):
        sync.tick()
        clock.t += step


def last_task():
    return f"t{server.n}"


def next_backfill(limit=2000):
    """Moves time on until the daemon submits a backfill. Sync downloads it
    submits on the way (new works go first) are completed as they come."""
    for _ in range(limit):
        clock.t = max(clock.t + 600, sync.state["next_submit_at"] + 1)
        sync.tick()
        inf = sync.state["inflight"]
        if not inf:
            continue
        if inf["kind"] == "backfill" and inf["task"] == last_task() and                 server.tasks_[inf["task"]]["status"] == "Queued":
            return server.submitted[-1]
        if inf["kind"] == "sync":
            mid = inf["member"]
            url = inf["url"]
            record(mid, [int(url.rsplit("/", 1)[1])] if "/artworks/" in url else pixiv.works[mid])
            server.run(inf["task"], took=40)
    return None


# ------------------------------------------------------------- enrolment
# 101: nothing here. 102: 11 files, over the rule. 103: exactly 10, not over.
# 104: renamed once, 6 + 6 files in two folders = 12, over. 105: 8 real files
# plus sidecars and ban tombstones, which are not items. 106: unfollowed later.
put_files("B (102)", 11)
put_files("C (103)", 10)
put_files("D old (104)", 6)
put_files("D new (104)", 6, start=100)
put_files("E (105)", 8)
put_files("E (105)", 5, start=50, ext=".js")
put_files("E (105)", 3, start=70, size=0)
pixiv.follows = [("101", "A"), ("102", "B"), ("103", "C"), ("104", "D new"), ("105", "E"), ("106", "F")]
pixiv.works = {"101": {1, 2, 3}, "102": set(range(1000, 1040)), "103": set(range(2000, 2010)),
               "104": set(range(3000, 3012)), "105": set(range(4000, 4008)), "106": {5000}}
record("102", range(1000, 1011))           # 11 of 40 here: partial
record("104", range(3000, 3011))           # 11 of 12 here: complete

sync = new_sync()
tick(sync)          # reads the follow list
st = sync.state
check("the follow list is read first", pixiv.calls[:1] == ["following"], pixiv.calls)
check("all six enrolled, in pixiv's order", st["order"] == ["101", "102", "103", "104", "105", "106"])
b = {m: st["follows"][m]["backfill"] for m in st["order"]}
check("no files: backfill", b["101"] == "pending", b)
check("11 files: over the rule, no backfill", b["102"] == "skipped", b)
check("exactly 10 files: not over, backfill", b["103"] == "pending", b)
check("two folders of a renamed artist add up (12): no backfill", b["104"] == "skipped", b)
check("sidecars and 0-byte tombstones are not items (8): backfill", b["105"] == "pending", b)
check("nothing submitted in the same tick as a pixiv read", server.submitted == [])
# 106 is held back for the aged-out case further down.
st["follows"]["106"]["backfill"] = "skipped"

# ------------------------------------------------------------- backfill
tick(sync)          # feed (first read, one page) - nothing in it yet
tick(sync)          # sweep one artist
check("the sweep examines an over-the-rule artist first",
      ("works", "102") in pixiv.calls or ("works", "104") in pixiv.calls, pixiv.calls)
server.tasks_["mine"] = {"id": "mine", "url": "https://www.pixiv.net/users/9", "category": "pixiv",
                         "status": "Running"}
tick(sync, 3)
check("a pixiv download of the user's own goes first", "users/101" not in " ".join(server.submitted),
      server.submitted)
check("... and status says why", "another pixiv download" in (sync.state["blocked"] or ""), sync.state["blocked"])
server.tasks_["mine"]["status"] = "Completed"
while not server.submitted or "users/101" not in server.submitted[-1]:
    tick(sync)
    if clock.t > 1_100_000:
        break
check("the first pending artist in follow order is submitted", server.submitted[-1].endswith("/users/101"),
      server.submitted)
check("... marked running", st["follows"]["101"]["backfill"] == "running")
n_before = len(server.submitted)
tick(sync, 5)
check("one download at a time: nothing more while it is queued", len(server.submitted) == n_before)

# A restart while it runs: a new daemon from the same state file.
tid = last_task()
sync = new_sync()
st = sync.state
check("a restart remembers the download in flight", (st["inflight"] or {}).get("task") == tid)
check("... and the artist stays running", st["follows"]["101"]["backfill"] == "running")
put_files("A (101)", 3)
record("101", [1, 2, 3])
server.run(tid, took=1800)
tick(sync)
check("completed: done", st["follows"]["101"]["backfill"] == "done", st["follows"]["101"])
check("... then synced by artist URL", st["follows"]["101"]["mode"] == "member")
gap = st["next_submit_at"] - clock.t
check("the pause after it is at least as long as the download (30 min)", gap >= 1800 - 60, gap)
check("... and at most max x (1 + jitter)", gap <= 4 * 3600 * 1.5, gap)
n_before = len(server.submitted)
tick(sync, 20)
check("nothing is submitted inside the pause", len(server.submitted) == n_before)

# ---------------------------------------------------- failures and cancels
url = next_backfill()
check("the next pending artist is 103", (url or "").endswith("/users/103"), url)
server.run(last_task(), status="Failed", took=60)
tick(sync)
f = st["follows"]["103"]
check("failed: back to pending, retried later", f["backfill"] == "pending" and f["attempts"] == 1
      and f["retry_at"] > clock.t, f)
url = next_backfill()
check("while 103 waits for its retry, the next artist goes", (url or "").endswith("/users/105"), url)
server.run(last_task(), status="Terminated", took=30)
tick(sync)
check("stopped by hand: cancelled, not retried", st["follows"]["105"]["backfill"] == "cancelled"
      and st["follows"]["105"]["mode"] is None, st["follows"]["105"])

for attempt in (2, 3):
    url = next_backfill()
    check(f"103 retried (attempt {attempt}), not before its retry time",
          (url or "").endswith("/users/103") and clock.t >= st["follows"]["103"]["retry_at"], url)
    server.run(last_task(), status="Failed", took=60)
    tick(sync)
f = st["follows"]["103"]
check("three failures: given up, left to the sweep", f["backfill"] == "failed" and f["mode"] is None, f)

# A task that aged out of the list while the daemon was down, with files.
st["follows"]["106"]["backfill"] = "pending"
url = next_backfill()
check("106 submitted", (url or "").endswith("/users/106"), url)
put_files("F (106)", 1, start=5000)
record("106", [5000])
del server.tasks_[last_task()]
tick(sync)
check("gone from the task list, but files arrived: done", st["follows"]["106"]["backfill"] == "done",
      st["follows"]["106"])

# --------------------------------------------------------------- sweep
f102, f104 = st["follows"]["102"], st["follows"]["104"]
for _ in range(10):
    clock.t += 16 * 60
    tick(sync)
check("partly here (29 of 40 missing): synced by work, above the newest id",
      f102["mode"] == "artwork" and f102["baseline"] == 1039, f102)
check("... and the old missing works are not queued", "102" not in st["sync_queue"], st["sync_queue"])
check("next to complete (1 of 12 missing): synced by artist URL", f104["mode"] == "member", f104)
check("the cancelled artist is examined too", st["follows"]["105"]["mode"] is not None, st["follows"]["105"])

# ------------------------------------------------------------ the feed
pixiv.works["102"] |= {1100, 1101}
# Work ids grow across all of pixiv, as the real ones do.
pixiv.works["101"] |= {1102}
pixiv.feed = [(1102, "101"), (1101, "102"), (1100, "102"), (1039, "102"), (1038, "102"), (1037, "102")]
st["feed_checked_at"] = 0
st["feed_seen_max"] = 1039
st["next_submit_at"] = clock.t + 10 ** 6       # hold submissions to look at the queue
tick(sync)
check("the feed queues new works above the baseline", sorted(st["sync_queue"].get("102", [])) == [1100, 1101],
      st["sync_queue"])
check("... and a new work of a backfilled artist", st["sync_queue"].get("101") == [1102], st["sync_queue"])
check("... and stops at the page with what it saw last time", ("feed", 3) not in pixiv.calls, pixiv.calls)
check("the feed remembers the newest id", st["feed_seen_max"] == 1102)

st["next_submit_at"] = 0
free["gb"] = 300.0          # under the backfill floor, over the sync floor
n_before = len(server.submitted)
for _ in range(5):          # a due sweep read takes a tick first
    tick(sync)
    if len(server.submitted) > n_before:
        break
check("new works go before the backfill", len(server.submitted) == n_before + 1
      and server.submitted[-1] in ("https://www.pixiv.net/artworks/1100", "https://www.pixiv.net/users/101"),
      server.submitted[-1:])
tick(sync)
first = server.submitted[-1]
if first.endswith("/artworks/1100"):
    record("102", [1100])
else:
    record("101", [1102])
server.run(last_task(), took=40)
tick(sync)
check("a synced work leaves the queue", (1100 not in st["sync_queue"].get("102", [])) if first.endswith("1100")
      else "101" not in st["sync_queue"], st["sync_queue"])
check("... after a short pause (sync minimum 4 min)", 240 - 60 <= st["next_submit_at"] - clock.t <= 3600 * 1.5)

# Drain the sync queue, then the disk floor holds the backfill.
for _ in range(400):
    if not st["sync_queue"] and not st["inflight"]:
        break
    clock.t = max(clock.t, st["next_submit_at"]) + 1
    tick(sync)
    if st["inflight"]:
        url = st["inflight"]["url"]
        mid = st["inflight"]["member"]
        record(mid, [int(url.rsplit("/", 1)[1])] if "/artworks/" in url else pixiv.works[mid])
        server.run(st["inflight"]["task"], took=40)
check("the sync queue drains", not st["sync_queue"], st["sync_queue"])
pixiv.follows.append(("107", "G"))
pixiv.works["107"] = {7000}
st["follows_refreshed_at"] = 0
tick(sync)
check("a new follow is enrolled at the next list read", st["follows"].get("107", {}).get("backfill") == "pending")
clock.t = st["next_submit_at"] + 1
n_before = len(server.submitted)
tick(sync, 3)
check("under the backfill floor: no backfill", len(server.submitted) == n_before, server.submitted[-1:])
check("... and status says so", "backfill floor" in (st["blocked"] or ""), st["blocked"])
free["gb"] = 2000.0

# ------------------------------------------------------------ pausing
open(PAUSE, "w").close()
tick(sync, 2)
check("the pause file stops submissions", len(server.submitted) == n_before and st["blocked"] == "paused")
os.unlink(PAUSE)
json.dump({"backfill_enabled": False}, open(SETTINGS, "w"))
tick(sync, 2)
check("settings can switch the backfill off", len(server.submitted) == n_before)
os.unlink(SETTINGS)
for _ in range(5):          # a pixiv read that is due takes a tick first
    tick(sync)
    if server.submitted[-1].endswith("/users/107"):
        break
check("... and on again", server.submitted[-1].endswith("/users/107"), server.submitted[-1:])
server.run(last_task(), took=60)
tick(sync)

# ------------------------------------------------------ pixiv trouble
st["feed_checked_at"] = 0
pixiv.fail = pfs.PixivError("throttled", "HTTP 429")
tick(sync)
wait1 = st["pixiv_backoff_until"] - clock.t
check("throttled: no pixiv reads for an hour", 3500 <= wait1 <= 3600, wait1)
calls = len(pixiv.calls)
tick(sync, 30)
check("... none at all in that hour", len(pixiv.calls) == calls, pixiv.calls[calls:])
clock.t = st["pixiv_backoff_until"] + 1
pixiv.fail = pfs.PixivError("throttled", "HTTP 429")
tick(sync)
check("throttled again: two hours", 7100 <= st["pixiv_backoff_until"] - clock.t <= 7200)
clock.t = st["pixiv_backoff_until"] + 1
tick(sync)
check("a good read resets the back-off", st["pixiv_backoff_s"] == 0)
st["follows_refreshed_at"] = 0
pixiv.fail = pfs.PixivError("session", "HTTP 401")
tick(sync)
check("signed out: said plainly", "cookie" in (st["blocked"] or ""), st["blocked"])
check("a failed list read unfollows nobody", not any(f.get("unfollowed_at") for f in st["follows"].values()))
clock.t = st["pixiv_backoff_until"] + 1
pixiv.follows = [x for x in pixiv.follows if x[0] != "106"]
tick(sync)
check("a complete list read marks an unfollow", st["follows"]["106"].get("unfollowed_at"))
pixiv.works["106"] |= {5001}
st["sync_queue"]["106"] = [5001]
st["next_submit_at"] = 0
n_before = len(server.submitted)
tick(sync, 2)
check("an unfollowed artist is not downloaded", len(server.submitted) == n_before, server.submitted[-1:])
st["sync_queue"].pop("106")

# ---------------------------------------------------- media-dl trouble
st["follows"]["103"].update(backfill="pending", retry_at=0, attempts=0)
st["next_submit_at"] = 0
server.down = True
tick(sync, 3)
check("media-dl down: waits, says so", "not answering" in (st["blocked"] or "") and not st["inflight"],
      st["blocked"])
server.down = False
st["follows"]["103"]["backfill"] = "failed"

# ------------------------------------------------------------ state file
sync.save()
data = json.load(open(STATE, encoding="utf-8"))
check("the state file is plain JSON with every artist", len(data["follows"]) == 7)
check("no temp files left behind", not [n for n in os.listdir(tmp) if n.startswith(".pfs-")])
check("status renders", "followed artists: 6" in new_sync().summary(), new_sync().summary()[:200])

shutil.rmtree(tmp, ignore_errors=True)
print("ALL PASS" if ok else "SOME CHECKS FAILED")
