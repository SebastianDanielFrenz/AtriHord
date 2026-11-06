"""yt-dlp skip reporting and the YouTube cookie check, out of a STAGED server.py's
AST: the helpers, the cookie reader on synthetic files, the admin endpoint, and
execute_download_task end to end with yt-dlp faked (subprocess.run and Popen
replaced) in a temp BASE_DIR. Nothing under /data2 is written and no request
reaches YouTube; the real cookie file is only read (names and expiry).

  venv/bin/python ytdlp_skips_check.py /tmp/.../server.py
"""
import ast
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import types
from collections import deque

BASE = os.environ.get("ATRIHORD_DIR") or os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SERVER = sys.argv[1]
sys.path.insert(0, BASE)
tmp = tempfile.mkdtemp(prefix="ytdlp_check_")

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


ok = True


def check(label, cond, detail=""):
    global ok
    ok = ok and bool(cond)
    print(("PASS " if cond else "FAIL ") + label + (f"  ({detail})" if detail and not cond else ""))


# Everything the download path could write points into the temp dir.
ns["BASE_DIR"] = tmp
ns["WEB_FILES_DIR"] = os.path.join(BASE, "web_files")
ns["DOWNLOADS_DIR"] = os.path.join(tmp, "downloads")
ns["TASKS_STATE_FILE"] = os.path.join(tmp, "tasks.json")
os.makedirs(os.path.join(tmp, "downloads", "youtube"))
ADMIN = {"username": "alice", "role": "admin"}
GUEST = {"username": "guest1", "role": "guest"}
CURRENT = {"user": ADMIN}
ns["get_authenticated_user"] = lambda: CURRENT["user"]
for name in ("app", "youtube_skip_reason", "youtube_skip_summary", "download_outcome", "youtube_cookie_status",
             "download_cookie_status", "execute_download_task", "active_tasks", "task_lock", "running_processes",
             "SHUTTING_DOWN", "pipeline_stats", "item_download_events", "MAX_LOG_LINES", "YOUTUBE_AUTH_COOKIES"):
    load(name)
app = ns["app"]


def call(fn, path, args=()):
    with app.test_request_context(path):
        resp = resolve(lambda: ns[fn](*args))
    status = 200
    if isinstance(resp, tuple):
        resp, status = resp[0], resp[1]
    return status, resp.get_json()


# ------------------------------------------------------------------ reasons
AGE = ("ERROR: [youtube] WnL5uXVQeJ0: Sign in to confirm your age. Use --cookies-from-browser or --cookies for "
       "the authentication. See  https://github.com/yt-dlp/yt-dlp/wiki/FAQ#how-do-i-pass-cookies-to-yt-dlp  for how to manually pass cookies")
cases = [
    (AGE, "age_restricted"),
    ("ERROR: [youtube] abcdefghijk: Join this channel to get access to members-only content like this video, and other exclusive perks.", "members_only"),
    ("ERROR: [youtube] abcdefghijk: This video is available to this channel's members on level: Member (or any higher level).", "members_only"),
    ("ERROR: [youtube] abcdefghijk: Private video. Sign in if you've been granted access to this video", "private"),
    ("ERROR: [youtube] abcdefghijk: Video unavailable. This video is private", "private"),
    ("ERROR: [youtube] abcdefghijk: Video unavailable. The uploader has not made this video available in your country", "geo_blocked"),
    ("ERROR: [youtube] abcdefghijk: Premieres in 3 hours", "not_released"),
    ("ERROR: [youtube] abcdefghijk: This live event will begin in a few moments.", "not_released"),
    ("ERROR: [youtube] abcdefghijk: Video unavailable. This video has been removed by the uploader", "unavailable"),
    ("ERROR: [youtube] abcdefghijk: Video unavailable. This video is no longer available because the YouTube account associated with this video has been terminated.", "unavailable"),
    ("ERROR: [youtube] abcdefghijk: Sign in to confirm you’re not a bot. Use --cookies-from-browser or --cookies for the authentication.", "error"),
    ("ERROR: [youtube] abcdefghijk: Requested format is not available. Use --list-formats for a list of available formats", "error"),
    ("ERROR: unable to download video data: HTTP Error 403: Forbidden", "error"),
    ("ERROR: [youtube:tab] @somechannel: This channel does not have a shorts tab", "error"),
    ("WARNING: [youtube] abcdefghijk: Some formats may be missing", None),
    ("[youtube] abcdefghijk: Sign in to confirm your age (not an ERROR line)", None),
    ("[download] Destination: Some video [abcdefghijk].webm", None),
]
got = [resolve(lambda l=l: ns["youtube_skip_reason"](l)) for l, _ in cases]
bad = [(l[:60], g, want) for (l, want), g in zip(cases, got) if g != want]
check("every yt-dlp line gets the right reason", not bad, bad)

summary = lambda *a: resolve(lambda: ns["youtube_skip_summary"](*a))
check("summary: counts, order, plural, cookie note",
      summary({"error": 2, "age_restricted": 9}, True) == "11 videos skipped: 9 age-restricted, 2 other errors (YouTube cookies are signed out)",
      summary({"error": 2, "age_restricted": 9}, True))
check("summary: one video, no cookie note for a private video",
      summary({"private": 1}, True) == "1 video skipped: 1 private", summary({"private": 1}, True))
check("summary: nothing skipped is empty", summary({}, True) == "")

outcome = lambda *a: resolve(lambda: ns["download_outcome"](*a))
table = [
    (("youtube", 0, {}), ("Completed", "100% Finished")),
    (("youtube", 1, {"age_restricted": 9}, True), ("Completed", "100% Finished - 9 videos skipped: 9 age-restricted (YouTube cookies are signed out)")),
    (("youtube", 1, {"age_restricted": 9, "error": 1}), ("Failed", "Exited code 1 - 10 videos skipped: 9 age-restricted, 1 other error")),
    (("youtube", 1, {}), ("Failed", "Exited code 1")),
    (("youtube", 2, {"private": 1}), ("Failed", "Exited code 2 - 1 video skipped: 1 private")),
    (("twitter", 1, {"private": 1}), ("Failed", "Exited code 1 - 1 video skipped: 1 private")),
    (("youtube", -15, {}), ("Failed", "Exited code -15")),
]
bad = [(a, outcome(*a), want) for a, want in table if outcome(*a) != want]
check("outcome: only known one-video refusals turn exit 1 into Completed", not bad, bad)

# ------------------------------------------------------------------ cookies
FUTURE, PAST = int(time.time()) + 86400 * 300, int(time.time()) - 60


def cookie_file(name, rows, crlf=False):
    lines = ["# Netscape HTTP Cookie File"]
    for domain, cname, expires, httponly in rows:
        prefix = "#HttpOnly_" if httponly else ""
        lines.append(f"{prefix}{domain}\tTRUE\t/\tTRUE\t{expires}\t{cname}\tsecret-value-{cname}")
    path = os.path.join(tmp, name)
    with open(path, "w", newline="") as fh:
        fh.write(("\r\n" if crlf else "\n").join(lines) + "\n")
    return path


SIGNED_OUT = [(".youtube.com", "__Secure-3PSID", FUTURE, True), (".youtube.com", "__Secure-3PAPISID", FUTURE, False),
              (".youtube.com", "PREF", FUTURE, False)]
LOGIN = (".youtube.com", "LOGIN_INFO", FUTURE, True)
status = lambda p: resolve(lambda: ns["youtube_cookie_status"](p))
s0 = status(os.path.join(tmp, "missing.txt"))
check("no file: not present, not signed in", not s0["present"] and not s0["signed_in"], s0)
s1 = status(cookie_file("out.txt", SIGNED_OUT))
check("session cookies without LOGIN_INFO: present, signed OUT", s1["present"] and not s1["signed_in"]
      and not s1["has_login_info"] and s1["cookies"] == 3, s1)
s2 = status(cookie_file("in.txt", SIGNED_OUT + [LOGIN]))
check("with LOGIN_INFO (#HttpOnly_ row): signed in", s2["signed_in"] and s2["has_login_info"], s2)
s3 = status(cookie_file("expired.txt", SIGNED_OUT + [(".youtube.com", "LOGIN_INFO", PAST, True)]))
check("an expired LOGIN_INFO does not count", not s3["signed_in"], s3)
s4 = status(cookie_file("google.txt", SIGNED_OUT + [(".google.com", "LOGIN_INFO", FUTURE, True)]))
check("a LOGIN_INFO for another domain does not count", not s4["signed_in"], s4)
s5 = status(cookie_file("sapisid.txt", [LOGIN, ("www.youtube.com", "SAPISID", FUTURE, False)]))
check("LOGIN_INFO + SAPISID (either API cookie will do): signed in", s5["signed_in"], s5)
s6 = status(cookie_file("loginonly.txt", [LOGIN, (".youtube.com", "PREF", FUTURE, False)]))
check("LOGIN_INFO alone, no API cookie: signed out", not s6["signed_in"], s6)
s7 = status(cookie_file("crlf.txt", SIGNED_OUT + [LOGIN], crlf=True))
check("a CRLF file (exported on Windows) parses", s7["signed_in"], s7)
check("never a cookie value in the answer", "secret-value" not in json.dumps([s1, s2, s3, s4, s5, s6, s7]))
real = status(os.path.join(BASE, "cookies_youtube.txt"))
print("     real cookie file now: present=%s signed_in=%s login_info=%s cookies=%s" % (
    real["present"], real["signed_in"], real["has_login_info"], real["cookies"]))

st, data = call("download_cookie_status", "/api/downloads/cookies")
check("endpoint: no cookie file in BASE_DIR", st == 200 and data["youtube"]["present"] is False, (st, data))
shutil.copy(os.path.join(tmp, "out.txt"), os.path.join(tmp, "cookies_youtube.txt"))
st, data = call("download_cookie_status", "/api/downloads/cookies")
check("endpoint: signed-out file reported", st == 200 and data["youtube"]["present"] and not data["youtube"]["signed_in"], (st, data))
CURRENT["user"] = GUEST
check("endpoint: guests refused", call("download_cookie_status", "/api/downloads/cookies")[0] == 403)
CURRENT["user"] = ADMIN

# --------------------------------------------- execute_download_task, faked
INFO = {"_type": "video", "id": "abcdefghijk", "title": "Harness video", "uploader": "Harness Channel",
        "channel_id": "UCharness"}


class FakePopen:
    lines, next_returncode, last_cmd = [], 0, None

    def __init__(self, cmd, **kwargs):
        FakePopen.last_cmd = cmd
        self.stdout = io.StringIO("".join(line + "\n" for line in FakePopen.lines))
        self.returncode = None
        self.pid = 4242

    def wait(self, timeout=None):
        self.returncode = FakePopen.next_returncode
        return self.returncode

    def poll(self):
        return self.returncode

    def terminate(self):
        self.returncode = -15

    def kill(self):
        self.returncode = -9


def run_ok(cmd, **kwargs):
    return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(INFO), stderr="")


def run_age_gated(cmd, **kwargs):
    raise subprocess.CalledProcessError(1, cmd, output="", stderr=(
        "WARNING: [youtube] abcdefghijk: This video is age-restricted; some formats may be missing\n" + AGE.replace("WnL5uXVQeJ0", "abcdefghijk")))


fake = types.SimpleNamespace(PIPE=subprocess.PIPE, STDOUT=subprocess.STDOUT, DEVNULL=subprocess.DEVNULL,
                             CalledProcessError=subprocess.CalledProcessError,
                             CompletedProcess=subprocess.CompletedProcess, TimeoutExpired=subprocess.TimeoutExpired,
                             Popen=FakePopen, run=run_ok)
ns["subprocess"] = fake
URL = "https://www.youtube.com/watch?v=abcdefghijk"


def run_task(tid, lines, returncode, run=run_ok):
    FakePopen.lines, FakePopen.next_returncode = lines, returncode
    fake.run = run
    ns["active_tasks"][tid] = {"id": tid, "url": URL, "category": "youtube", "status": "Queued", "progress": "Queued",
                               "logs": deque(maxlen=ns["MAX_LOG_LINES"]), "created_at": time.time(),
                               "started_at": None, "completed_at": None, "media_path": None}
    resolve(lambda: ns["execute_download_task"](tid, URL, "youtube"))
    return ns["active_tasks"][tid]


# The function catches its own exceptions, NameErrors from on-demand loading
# included - so warm up until a run no longer fails on a missing name.
for _ in range(40):
    t = run_task("warm", ["[download] 100.0% of 1.00MiB"], 0)
    m = re.search(r"name '(\w+)' is not defined", str(t.get("progress")))
    if not m:
        break
    load(m.group(1))
print("     warm-up run:", t["status"], "-", t["progress"])

DOWNLOAD = ["[youtube] abcdefghijk: Downloading webpage", "[download] Destination: Harness video [abcdefghijk].webm",
            "[download] 100.0% of 1.00MiB in 00:00:01"]
signed_out_line = lambda t: any(str(l).startswith("[media-dl] YouTube cookies are signed out") for l in t["logs"])

t = run_task("a", DOWNLOAD + [AGE], 1)
check("run with an age-restricted video and signed-out cookies: Completed, skip named",
      t["status"] == "Completed" and t["progress"] == "100% Finished - 1 video skipped: 1 age-restricted (YouTube cookies are signed out)"
      and t.get("skipped") == {"age_restricted": 1} and t.get("cookies_signed_out") is True, (t["status"], t["progress"], t.get("skipped")))
check("the signed-out note is in the task log, and the cookies were still passed",
      signed_out_line(t) and "--cookies" in FakePopen.last_cmd)
check("the log closes with the skip summary",
      list(t["logs"])[-1] == "[media-dl] 1 video skipped: 1 age-restricted (YouTube cookies are signed out)", list(t["logs"])[-2:])

t = run_task("b", DOWNLOAD + [AGE, "ERROR: unable to download video data: HTTP Error 403: Forbidden"], 1)
check("an unexplained error as well: Failed, both counted",
      t["status"] == "Failed" and t["progress"] == "Exited code 1 - 2 videos skipped: 1 age-restricted, 1 other error (YouTube cookies are signed out)"
      and t.get("skipped") == {"age_restricted": 1, "error": 1}, (t["status"], t["progress"], t.get("skipped")))

t = run_task("c", [], 0, run=run_age_gated)
check("an age-gated single video failing at metadata says why",
      t["status"] == "Failed" and t["progress"].startswith("Failed to extract metadata: [youtube] abcdefghijk: Sign in to confirm your age")
      and t["progress"].endswith("(YouTube cookies are signed out)") and t.get("skipped") == {"age_restricted": 1}
      and any(str(l).startswith("ERROR: [youtube] abcdefghijk") for l in t["logs"]), (t["status"], t["progress"][:120]))

shutil.copy(os.path.join(tmp, "in.txt"), os.path.join(tmp, "cookies_youtube.txt"))
t = run_task("d", DOWNLOAD, 0)
check("signed-in cookies, clean run: 100% Finished, no note, no skips",
      t["status"] == "Completed" and t["progress"] == "100% Finished" and t.get("cookies_signed_out") is False
      and not signed_out_line(t) and not t.get("skipped"), (t["status"], t["progress"], t.get("skipped")))

os.remove(os.path.join(tmp, "cookies_youtube.txt"))
t = run_task("e", DOWNLOAD + [AGE], 1)
check("no cookie file: skip named without blaming cookies that do not exist",
      t["status"] == "Completed" and t["progress"] == "100% Finished - 1 video skipped: 1 age-restricted"
      and t.get("cookies_signed_out") is False and "--cookies" not in FakePopen.last_cmd, (t["status"], t["progress"]))

big = DOWNLOAD + [AGE] * 700
t = run_task("f", big, 1)
check("counted from the stream, not the 500-line log", t.get("skipped") == {"age_restricted": 700}
      and len(t["logs"]) == ns["MAX_LOG_LINES"], (t.get("skipped"), len(t["logs"])))

check("task stays JSON-serialisable for tasks.json", json.dumps({**t, "logs": list(t["logs"])}) is not None)

shutil.rmtree(tmp, ignore_errors=True)
print("ALL PASS" if ok else "SOME CHECKS FAILED")
