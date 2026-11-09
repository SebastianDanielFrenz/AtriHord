"""Phone backup receiver (phone_ingest.py), through Flask's test client against a
temp "Immich Phone" tree and a stub app whose only job is to say who is calling.
Nothing outside the temp directory is touched.

  python phone_ingest_check.py [dir containing phone_ingest.py]
"""
import hashlib
import os
import shutil
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = sys.argv[1] if len(sys.argv) > 1 else os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, SRC)

import phone_ingest                                  # noqa: E402
from flask import Flask                              # noqa: E402

ok = True


def check(label, cond, detail=""):
    global ok
    ok = ok and bool(cond)
    print(("PASS " if cond else "FAIL ") + label + (f"  ({detail})" if detail and not cond else ""), flush=True)


tmp = tempfile.mkdtemp(prefix="phone_ingest_check_")
root = os.path.join(tmp, "immich-phone")
state = os.path.join(tmp, "state")
os.makedirs(root)
os.makedirs(state)

who = {"user": {"username": "root", "role": "admin"}}
resets = []


def build(dest=root, invalidate_after=0.05):
    app = Flask("check")
    ing = phone_ingest.register(app, lambda: who["user"], lambda u: u.get("role") == "admin",
                                dest, state, lambda: resets.append(time.time()), invalidate_after)
    return app.test_client(), ing


def sha(b):
    return hashlib.sha256(b).hexdigest()


def upload(c, folder, name, data, mtime=0, chunk=7):
    """The whole client dance. Returns the finish response (or begin's, when
    begin already settled it)."""
    r = c.post("/api/phone/begin", json={"folder": folder, "name": name, "size": len(data),
                                         "sha256": sha(data), "mtime": mtime})
    j = r.get_json()
    if j.get("status") != "ready":
        return r
    off = j["offset"]
    while off < len(data):
        part = data[off:off + chunk]
        r = c.put(f"/api/phone/chunk/{j['id']}?offset={off}", data=part)
        assert r.status_code == 200, r.get_json()
        off = r.get_json()["offset"]
    return c.post(f"/api/phone/finish/{j['id']}")


client, ing = build()

# --- auth ---------------------------------------------------------------
who["user"] = None
check("no credentials -> 401", client.post("/api/phone/have", json={"files": []}).status_code == 401)
who["user"] = {"username": "guest", "role": "guest"}
check("guest -> 403", client.post("/api/phone/have", json={"files": []}).status_code == 403)
check("guest cannot begin", client.post("/api/phone/begin", json={}).status_code == 403)
who["user"] = {"username": "root", "role": "admin"}
unconfigured, _ = build(dest=None)
check("no destination configured -> 503",
      unconfigured.post("/api/phone/have", json={"files": []}).status_code == 503)

# --- have: the Immich export is what a first run is compared with ---------
exp = os.path.join(root, "Camera")
os.makedirs(exp)
for n, size in (("IMG_1.jpg", 5), ("IMG_2.JPG", 6), ("IMG_3.jpg", 7), ("IMG_3 (2).jpg", 9)):
    with open(os.path.join(exp, n), "wb") as fh:
        fh.write(b"x" * size)
os.makedirs(os.path.join(root, "@3k___pnt"))
with open(os.path.join(root, "@3k___pnt", "pic.png"), "wb") as fh:
    fh.write(b"y" * 11)


def have(*files):
    r = client.post("/api/phone/have", json={"files": [
        {"folder": f, "name": n, "size": s} for f, n, s in files]})
    return r.get_json()["have"]


check("have: exact name and size", have(("Camera", "IMG_1.jpg", 5)) == [True])
check("have: same name, other size is new", have(("Camera", "IMG_1.jpg", 6)) == [False])
check("have: extension case does not matter", have(("Camera", "IMG_2.jpg", 6)) == [True])
check("have: the export's ' (2)' clash copy matches its own size",
      have(("Camera", "IMG_3.jpg", 9), ("Camera", "IMG_3.jpg", 7), ("Camera", "IMG_3.jpg", 8)) == [True, True, False])
check("have: folder with '@' and unknown folder",
      have(("@3k___pnt", "pic.png", 11), ("Nope", "pic.png", 11)) == [True, False])
check("have: junk entries are false, not errors",
      client.post("/api/phone/have", json={"files": [None, {"folder": "Camera"}, {"name": "a.jpg", "size": "5"}]})
      .get_json()["have"] == [False, False, False])
check("have: more than 1000 refused",
      client.post("/api/phone/have", json={"files": [{}] * 1001}).status_code == 400)

# --- a whole upload -------------------------------------------------------
data = os.urandom(100) + b"photo"
stamp = int((time.time() - 40 * 86400) * 1000)
r = upload(client, "Camera", "IMG_new.jpg", data, mtime=stamp)
check("upload: stored, 201", r.status_code == 201 and r.get_json() == {"status": "stored", "name": "IMG_new.jpg"}, r.get_data(True))
dest = os.path.join(root, "Camera", "IMG_new.jpg")
check("upload: bytes identical", os.path.exists(dest) and open(dest, "rb").read() == data)
check("upload: mtime is the phone's", abs(os.path.getmtime(dest) - stamp / 1000) < 2)
check("upload: no part or meta left behind", os.listdir(os.path.join(root, ".incoming")) == [])
check("upload: now reported as present", have(("Camera", "IMG_new.jpg", len(data))) == [True])

# --- resume ----------------------------------------------------------------
big = os.urandom(50)
j = client.post("/api/phone/begin", json={"folder": "Videos", "name": "clip.mp4", "size": 50,
                                          "sha256": sha(big), "mtime": 0}).get_json()
check("begin: ready at 0", j["status"] == "ready" and j["offset"] == 0)
client.put(f"/api/phone/chunk/{j['id']}?offset=0", data=big[:20])
j2 = client.post("/api/phone/begin", json={"folder": "Videos", "name": "clip.mp4", "size": 50,
                                           "sha256": sha(big), "mtime": 0}).get_json()
check("resume: same id, offset 20", j2["id"] == j["id"] and j2["offset"] == 20, j2)
r = client.put(f"/api/phone/chunk/{j['id']}?offset=0", data=big[:20])
check("stale offset -> 409 carrying the real one", r.status_code == 409 and r.get_json()["offset"] == 20)
r = client.put(f"/api/phone/chunk/{j['id']}?offset=20", data=big[20:45],
               environ_overrides={"CONTENT_LENGTH": "40"})
check("short body -> 400 and the part file is untouched",
      r.status_code == 400 and r.get_json()["offset"] == 20
      and os.path.getsize(os.path.join(root, ".incoming", j["id"] + ".part")) == 20, r.get_data(True))
r = client.post(f"/api/phone/finish/{j['id']}")
check("finish too early -> 409 incomplete", r.status_code == 409 and r.get_json()["offset"] == 20)
r = client.put(f"/api/phone/chunk/{j['id']}?offset=20", data=big[20:] + b"extra")
check("more bytes than declared -> 400", r.status_code == 400)
client2, ing2 = build()                                  # a restarted server
j3 = client2.post("/api/phone/begin", json={"folder": "Videos", "name": "clip.mp4", "size": 50,
                                            "sha256": sha(big), "mtime": 0}).get_json()
check("resume survives a restart", j3["offset"] == 20)
client2.put(f"/api/phone/chunk/{j['id']}?offset=20", data=big[20:])
r = client2.post(f"/api/phone/finish/{j['id']}")
check("resumed upload completes", r.status_code == 201 and open(os.path.join(root, "Videos", "clip.mp4"), "rb").read() == big)

# --- corruption --------------------------------------------------------------
good = os.urandom(30)
j = client.post("/api/phone/begin", json={"folder": "Camera", "name": "bad.jpg", "size": 30,
                                          "sha256": sha(good), "mtime": 0}).get_json()
client.put(f"/api/phone/chunk/{j['id']}?offset=0", data=os.urandom(30))
r = client.post(f"/api/phone/finish/{j['id']}")
check("wrong content -> 422 and nothing kept",
      r.status_code == 422 and not os.path.exists(os.path.join(root, "Camera", "bad.jpg"))
      and not any(n.startswith(j["id"]) for n in os.listdir(os.path.join(root, ".incoming"))))

# --- duplicates ----------------------------------------------------------------
r = upload(client, "Elsewhere", "copy.jpg", data)
check("same bytes under another folder -> duplicate", r.get_json() == {"status": "duplicate"})
os.unlink(dest)
r = upload(client, "Camera", "IMG_new.jpg", data)
check("a deleted upload does not come back", r.get_json() == {"status": "duplicate"})

# --- clash ---------------------------------------------------------------------
a, b = os.urandom(21), os.urandom(22)
upload(client, "Screens", "shot.png", a)
r = upload(client, "Screens", "shot.png", b)
check("same name, other bytes -> ' (2)'", r.status_code == 201 and r.get_json()["name"] == "shot (2).png", r.get_data(True))
c3 = os.urandom(23)
r = upload(client, "Screens", "shot.png", c3)
check("third one -> ' (3)'", r.get_json()["name"] == "shot (3).png")
check("have sees every clash copy", have(("Screens", "shot.png", 23), ("Screens", "shot.png", 21)) == [True, True])

# --- refusals ---------------------------------------------------------------------
def begin(**kw):
    body = {"folder": "Camera", "name": "z.jpg", "size": 5, "sha256": sha(b"zzzzz"), "mtime": 0}
    body.update(kw)
    return client.post("/api/phone/begin", json=body)


check("script extension refused", begin(name="evil.php").status_code == 400)
check("no extension refused", begin(name="README").status_code == 400)
check("zero bytes refused", begin(size=0).status_code == 400)
check("bad hash refused", begin(sha256="abc").status_code == 400)
check("over the size cap -> 413", begin(size=phone_ingest.MAX_BYTES + 1).status_code == 413)
saved = phone_ingest.FREE_RESERVE
phone_ingest.FREE_RESERVE = 1 << 60
check("full disk -> 507", begin().status_code == 507)
phone_ingest.FREE_RESERVE = saved
saved = phone_ingest.MAX_CHUNK
j = begin(name="chunky.jpg", size=40, sha256=sha(b"c" * 40)).get_json()
phone_ingest.MAX_CHUNK = 10
check("oversized chunk -> 413", client.put(f"/api/phone/chunk/{j['id']}?offset=0", data=b"c" * 40).status_code == 413)
phone_ingest.MAX_CHUNK = saved
check("unknown id -> 404", client.put("/api/phone/chunk/" + "0" * 32 + "?offset=0", data=b"a").status_code == 404)
check("malformed id -> 404", client.post("/api/phone/finish/..%2f..%2fetc").status_code == 404)

# --- traversal ----------------------------------------------------------------------
evil = os.urandom(15)
r = upload(client, "../../outside", "../../../escape.jpg", evil)
landed = [os.path.join(d, f) for d, _, fs in os.walk(tmp) for f in fs if open(os.path.join(d, f), "rb").read() == evil]
check("traversal in folder and name stays inside the root",
      r.status_code == 201 and len(landed) == 1 and os.path.realpath(landed[0]).startswith(os.path.realpath(root) + os.sep),
      landed)
check("names are cleaned, not rejected: no component is '..' or starts with a dot",
      all(not p.startswith(".") for p in os.path.relpath(landed[0], root).split(os.sep)))
r = upload(client, "日本語 folder", "写真 (1).jpg", os.urandom(12))
check("unicode survives", r.status_code == 201 and os.path.exists(os.path.join(root, "日本語 folder", "写真 (1).jpg")), r.get_data(True))
r = upload(client, "Camera", "x" * 300 + ".jpg", os.urandom(13))
stored = r.get_json()["name"]
check("over-long name is clipped to fit a filesystem, extension kept",
      len(stored.encode()) <= 200 and stored.endswith(".jpg"))

# --- cache reset is debounced --------------------------------------------------------
# A throttle: the first upload of a burst starts the window, the rest ride along,
# and the reset lands after it. The window must outlast the burst to see that.
time.sleep(0.4)                      # let the earlier uploads' 50 ms timers fire first
resets.clear()
burst, _ = build(invalidate_after=1.0)
for i in range(5):
    upload(burst, "Burst", f"b{i}.jpg", os.urandom(10 + i))
check("nothing reset while the burst is still inside the window", len(resets) == 0, len(resets))
time.sleep(1.5)
check("five uploads in a burst -> one cache reset", len(resets) == 1, len(resets))
upload(burst, "Burst", "later.jpg", os.urandom(40))
time.sleep(1.5)
check("an upload after the window -> another reset", len(resets) == 2, len(resets))

# --- the walkers must not see the working files ----------------------------------------
j = begin(name="hidden.jpg", size=40, sha256=sha(b"h" * 40)).get_json()
visible = [n for n in os.listdir(root) if not n.startswith(".")]
check("in-progress parts live in a dot-directory", ".incoming" in os.listdir(root) and ".incoming" not in visible)

shutil.rmtree(tmp, ignore_errors=True)
print("ALL PASS" if ok else "SOME FAILED", flush=True)
sys.exit(0 if ok else 1)
