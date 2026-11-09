"""Character blacklist. Out of a server.py's AST and the staged media_ai.py (the
directory of <server.py> goes first on the path), over a temp AI store with the
real schema (media_ai.connect) and a temp library: the real /api/media/characters
routes, trash_resolved (spied on, not replaced), the real trash restore route and
the real sweep. Nothing outside the temp directory is touched.

  venv/bin/python character_blacklist_check.py <server.py>
"""
import ast
import json
import os
import shutil
import sys
import tempfile
import threading
import time as real_time

BASE = os.environ.get("ATRIHORD_DIR") or os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SERVER = sys.argv[1]
sys.path.insert(0, BASE)
sys.path.insert(0, os.path.dirname(os.path.abspath(SERVER)))
tmp = tempfile.mkdtemp(prefix="charbl_check_")
os.environ["KSF_MEDIA_AI_DIR"] = os.path.join(tmp, "ai")
os.makedirs(os.environ["KSF_MEDIA_AI_DIR"])
import media_ai                      # noqa: E402

ok = True


def check(label, cond, detail=""):
    global ok
    ok = ok and bool(cond)
    print(("PASS " if cond else "FAIL ") + label + (f"  ({detail})" if detail and not cond else ""), flush=True)


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


def _names(code):
    out = set(code.co_names)
    for const in code.co_consts:
        if hasattr(const, "co_names"):
            out |= _names(const)
    return out


def preload(name, seen=None):
    seen = set() if seen is None else seen
    if name in seen or name not in defs:
        return
    seen.add(name)
    code = compile(ast.Module(body=[defs[name]], type_ignores=[]), SERVER, "exec")
    for used in _names(code):
        if used not in ns:
            preload(used, seen)
    if name not in ns:
        load(name)


def F(name):
    preload(name)
    return ns[name]


R = "downloads"
lib = os.path.join(tmp, "lib")
os.makedirs(lib)
ns["BASE_DIR"] = tmp
ns["DOWNLOADS_DIR"] = lib
ns["DEFAULT_ROOT_ID"] = R
ns["CONFIG"] = {"character_blacklist": {"min_p": 0.35, "max_per_sweep": 5000}}
USER = {"username": "alice", "role": "admin"}
ns["get_authenticated_user"] = lambda: USER
ns["get_root"] = lambda root_id: {"id": R, "path": lib} if (root_id or R) == R else None
ns["get_all_roots"] = lambda: [{"id": R, "path": lib}]
ns["roots_for_user"] = lambda user: [{"id": R, "path": lib}]
ns["content_levels_map"] = lambda wait=True: None       # no content map: covers stay as tag_pools chose
ns["invalidate_media_caches"] = lambda: None


class FakeDiscovery:
    """The real service caches a snapshot built in the background; a rebuild per call is the same data."""
    def invalidate(self):
        pass

    def get(self, kind, builder):
        return builder()

    def status(self, kind):
        return {}


ns["discovery_service"] = lambda: FakeDiscovery()
preload("app")
app = ns["app"]
for route_fn in ("character_blacklist_list", "character_blacklist_preview", "character_blacklist_add",
                 "character_blacklist_remove", "character_blacklist_clear_exemption", "media_characters",
                 "media_character_pool", "media_discovery", "restore_trash", "list_trash"):
    F(route_fn)

# trash_resolved is the real one; the spy only records how it was called.
real_trash = F("trash_resolved")
calls = []


def spy(resolved, mode, user, reason=None):
    calls.append({"n": len(resolved), "mode": mode, "reason": reason, "user": user.get("username")})
    return real_trash(resolved, mode, user, reason=reason)


ns["trash_resolved"] = spy
client = app.test_client()

conn = media_ai.connect(tmp)
ids = {}


def media(path, trashed=False, on_disk=True):
    full = os.path.join(lib, path)
    if on_disk:
        os.makedirs(os.path.dirname(full), exist_ok=True)
        open(full, "wb").write(b"x")
    cur = conn.execute("INSERT INTO media (root, path, size, kind, trashed) VALUES (?, ?, 1, 'image', ?)",
                       (R, path, 1.0 if trashed else None))
    ids[path] = cur.lastrowid
    return path


def tag(path, character, p):
    conn.execute("INSERT INTO media_tags (media_id, tag, category, p) VALUES (?, ?, 'character', ?)",
                 (ids[path], character, p))


def variants(key, *members):
    for t in (key,) + members:
        conn.execute("INSERT INTO tag_variants (tag, group_key, category) VALUES (?, ?, 'character')", (t, key))


def live(path):
    return os.path.isfile(os.path.join(lib, path)) and os.path.getsize(os.path.join(lib, path)) > 0


def trashed(path):
    return conn.execute("SELECT trashed FROM media WHERE root = ? AND path = ?", (R, path)).fetchone()[0] is not None


def wait_sweep(timeout=30):
    end = real_time.time() + timeout
    while real_time.time() < end:
        st = ns["charbl_status"]()
        if not st["pending"] and not st["running"] and not ns["_charbl_state"]["worker"]:
            return st
        real_time.sleep(0.05)
    raise RuntimeError("sweep did not finish")


def api(method, url, body=None):
    rv = client.open(url, method=method, json=body)
    return rv.status_code, (rv.get_json(silent=True) or {})


# ---- the library --------------------------------------------------------------
# miku has a costume tag; a4 carries both (one file, counted once).
media("twitter/a1.jpg"); tag("twitter/a1.jpg", "miku", .9)
media("twitter/a2.jpg"); tag("twitter/a2.jpg", "miku_(summer)", .8)
media("twitter/a3.jpg"); tag("twitter/a3.jpg", "miku", .30)                 # below the 0.35 floor
media("twitter/a4.jpg"); tag("twitter/a4.jpg", "miku", .6); tag("twitter/a4.jpg", "miku_(summer)", .5)
media("twitter/a5.jpg"); tag("twitter/a5.jpg", "rin", .9)                    # another character
media("twitter/a6.jpg", trashed=True, on_disk=False); tag("twitter/a6.jpg", "miku", .9)   # already trashed
media(".trash/old/a7.jpg"); tag(".trash/old/a7.jpg", "miku", .9)             # under .trash
media("twitter/a8.jpg", on_disk=False); tag("twitter/a8.jpg", "miku", .9)    # in the index, gone from the disk
variants("miku", "miku_(summer)")
# asuka: ten files for the 409 case. cap: eight for max_per_sweep.
for i in range(10):
    p = media(f"asuka/s{i}.jpg"); tag(p, "asuka", .9)
for i in range(8):
    p = media(f"cap/c{i}.jpg"); tag(p, "cap", .9)
conn.commit()

try:
    # ---- preview ----------------------------------------------------------------
    st, pv = api("POST", "/api/media/characters/blacklist/preview", {"tag": "miku", "include_variants": True})
    check("preview: count = a1, a2, a4, a8 (variants included, below-threshold / trashed / .trash left out)",
          st == 200 and pv["count"] == 4, pv)
    check("preview: variant tags covered", pv.get("variants") == ["miku", "miku_(summer)"], pv.get("variants"))
    check("preview: sample paths, surest first", 0 < len(pv.get("samples", [])) <= 5
          and pv["samples"][0]["p"] >= pv["samples"][-1]["p"], pv.get("samples"))
    st, pv1 = api("POST", "/api/media/characters/blacklist/preview", {"tag": "miku", "include_variants": False})
    check("preview without variants: a1, a4, a8", pv1["count"] == 3 and pv1["variants"] == ["miku"], pv1)
    st, pv2 = api("POST", "/api/media/characters/blacklist/preview", {"tag": "miku", "min_p": 0.85})
    check("preview with a per-entry min_p 0.85: only a1 and a8", pv2["count"] == 2, pv2)
    st, bad = api("POST", "/api/media/characters/blacklist/preview", {"tag": "no_such_character"})
    check("preview of an unknown tag is refused", st == 400, bad)
    check("preview changed nothing", all(live(f"twitter/a{i}.jpg") for i in (1, 2, 3, 4, 5))
          and not ns["charbl_load"]()["entries"] and not calls)

    # ---- add: confirmation is required ------------------------------------------
    st, r = api("POST", "/api/media/characters/blacklist", {"tag": "miku", "expected_count": 4})
    check("add without confirm is refused", st == 400, r)
    st, r = api("POST", "/api/media/characters/blacklist", {"tag": "miku", "confirm": True})
    check("add without the previewed count is refused", st == 400, r)
    check("... and nothing happened", live("twitter/a1.jpg") and not ns["charbl_load"]()["entries"])

    # ---- add: the sweep ----------------------------------------------------------
    t0 = real_time.time()
    st, r = api("POST", "/api/media/characters/blacklist",
                {"tag": "miku", "include_variants": True, "confirm": True, "expected_count": 4})
    returned_in = real_time.time() - t0
    check("add is accepted and returns at once (the sweep is a background job)",
          st == 200 and returned_in < 5, (st, r, returned_in))
    last = wait_sweep()["last"]
    check("sweep: a1, a2, a4 trashed, a3 (below threshold) and a5 (rin) stay",
          not live("twitter/a1.jpg") and not live("twitter/a2.jpg") and not live("twitter/a4.jpg")
          and live("twitter/a3.jpg") and live("twitter/a5.jpg"), last)
    check("sweep: the index marks them trashed", trashed("twitter/a1.jpg") and trashed("twitter/a2.jpg")
          and trashed("twitter/a4.jpg") and not trashed("twitter/a3.jpg"))
    check("sweep: a8 (in the index, gone from the disk) is skipped, not an error",
          last["missing"] == 1 and last["trashed"] == 3 and not last["error"], last)
    check("trash_resolved was called once, in ban mode, with the reason",
          len(calls) == 1 and calls[0]["mode"] == "ban" and calls[0]["n"] == 3
          and calls[0]["reason"] == "character blacklist: miku", calls)
    batch = last["batch"]
    manifest = json.load(open(os.path.join(lib, ".trash", batch, "_manifest.json")))
    check("the manifest records the reason and the ban", manifest.get("reason") == "character blacklist: miku"
          and manifest.get("mode") == "ban" and len(manifest["items"]) == 3, manifest)
    check("ban mode: a tombstone stands where each file was (the downloaders skip it)",
          all(os.path.isfile(os.path.join(lib, p)) and os.path.getsize(os.path.join(lib, p)) == 0
              for p in ("twitter/a1.jpg", "twitter/a2.jpg", "twitter/a4.jpg")))
    st, listing = api("GET", "/api/media/trash")
    check("the trash list shows the reason", any(b.get("reason") == "character blacklist: miku" for b in listing), listing)
    check("the .trash row of the index was not touched (it never reached the sweep)", not trashed(".trash/old/a7.jpg"))

    st, lst = api("GET", "/api/media/characters/blacklist")
    check("GET lists the entry with who and when, and a live count of 0 now",
          st == 200 and lst["entries"][0]["tag"] == "miku" and lst["entries"][0]["added_by"] == "alice"
          and lst["entries"][0]["matches"] == 1 and lst["status"]["last"]["trashed"] == 3, lst)
    # (1 = a8, which the index still lists as live although its file is gone)

    # ---- restore exempts ---------------------------------------------------------
    st, r = api("POST", f"/api/media/trash/{batch}/restore", {"root": R})
    check("restore through the existing trash route", st == 200 and len(r["restored"]) == 3, r)
    ex = ns["charbl_load"]()["exemptions"]
    check("the restored (root, path) pairs are exempt",
          {(x["root"], x["path"]) for x in ex} == {(R, "twitter/a1.jpg"), (R, "twitter/a2.jpg"), (R, "twitter/a4.jpg")}, ex)
    check("restored files are real again and live in the index",
          live("twitter/a1.jpg") and not trashed("twitter/a1.jpg"))
    ns["charbl_request_sweep"]()
    last = wait_sweep()["last"]
    check("the next sweep leaves the exempt files alone", last["trashed"] == 0 and live("twitter/a1.jpg")
          and live("twitter/a2.jpg") and live("twitter/a4.jpg") and len(calls) == 1, (last, calls))
    st, gone = api("GET", "/api/media/characters/blacklist")
    check("GET lists the exemptions", len(gone["exemptions"]) == 3)
    st, r = api("DELETE", f"/api/media/characters/blacklist/exemptions?root={R}&path=twitter/a2.jpg")
    check("an admin can clear one exemption", st == 200 and r["cleared"] == 1)
    st, r = api("DELETE", f"/api/media/characters/blacklist/exemptions?root={R}&path=twitter/a2.jpg")
    check("... clearing it twice is a 404", st == 404)
    ns["charbl_request_sweep"]()
    last = wait_sweep()["last"]
    check("the next sweep trashes only the file whose exemption was cleared",
          last["trashed"] == 1 and not live("twitter/a2.jpg") and live("twitter/a1.jpg") and live("twitter/a4.jpg"), last)
    st, r = api("DELETE", f"/api/media/characters/blacklist/exemptions?all=1")
    check("clear all exemptions", st == 200 and not ns["charbl_load"]()["exemptions"])

    # ---- the periodic sweep ------------------------------------------------------
    # The loop itself, with only sleep replaced: it must wait, then ask for a sweep
    # every interval. a9 is tagged after the add, as the tagger would.
    media("twitter/a9.jpg"); tag("twitter/a9.jpg", "miku_(summer)", .7)
    conn.commit()
    sleeps = []

    class Clock:
        def __getattr__(self, name):
            return getattr(real_time, name)

        def sleep(self, seconds):
            sleeps.append(seconds)
            if len(sleeps) >= 3:
                raise SystemExit
            real_time.sleep(0.02)
    ns["time"] = Clock()
    th = threading.Thread(target=F("_charbl_sweep_loop"), daemon=True)
    th.start()
    th.join(10)
    last = None
    try:
        last = wait_sweep()["last"]
    finally:
        ns["time"] = real_time
    check("the periodic loop waits, then repeats every 10 minutes", sleeps[:3] == [90, 600, 600], sleeps)
    check("a newly tagged file (a9, a costume tag) is caught by the periodic sweep",
          not live("twitter/a9.jpg") and trashed("twitter/a9.jpg"), last)

    # ---- removing an entry -------------------------------------------------------
    st, r = api("DELETE", "/api/media/characters/blacklist?tag=miku")
    check("remove the entry", st == 200 and not ns["charbl_load"]()["entries"])
    st, r = api("DELETE", "/api/media/characters/blacklist?tag=miku")
    check("... twice is a 404", st == 404)
    before = len(calls)
    media("twitter/b1.jpg"); tag("twitter/b1.jpg", "miku", .9)
    conn.commit()
    ns["charbl_request_sweep"]()
    wait_sweep()
    check("after removal, new files are not trashed", live("twitter/b1.jpg") and len(calls) == before)
    check("removal restored nothing (a9 is still in the trash)", trashed("twitter/a9.jpg") and not live("twitter/a9.jpg"))

    # ---- 409 on growth -----------------------------------------------------------
    st, pa = api("POST", "/api/media/characters/blacklist/preview", {"tag": "asuka"})
    check("asuka previews 10", pa["count"] == 10, pa)
    for i in (10, 11):
        p = media(f"asuka/s{i}.jpg"); tag(p, "asuka", .9)
    conn.commit()
    st, r = api("POST", "/api/media/characters/blacklist", {"tag": "asuka", "confirm": True, "expected_count": 10})
    check("12 files against a preview of 10 (more than 10 % more): 409, nothing added",
          st == 409 and r.get("code") == "count_grew" and r["count"] == 12
          and not ns["charbl_load"]()["entries"] and live("asuka/s0.jpg"), (st, r))
    st, pa = api("POST", "/api/media/characters/blacklist/preview", {"tag": "asuka"})
    p = media("asuka/s12.jpg"); tag(p, "asuka", .9)
    conn.commit()
    st, r = api("POST", "/api/media/characters/blacklist", {"tag": "asuka", "confirm": True, "expected_count": pa["count"]})
    check("13 files against a preview of 12 (8 % more): accepted", st == 200 and pa["count"] == 12, (st, r, pa))
    wait_sweep()
    check("... and all 13 are trashed", sum(1 for i in range(13) if trashed(f"asuka/s{i}.jpg")) == 13)
    st, r = api("POST", "/api/media/characters/blacklist", {"tag": "asuka", "confirm": True, "expected_count": 13})
    check("adding the same character twice is refused", st == 409 and r.get("code") == "exists", r)

    # ---- max_per_sweep -----------------------------------------------------------
    ns["CONFIG"]["character_blacklist"]["max_per_sweep"] = 3
    st, pc = api("POST", "/api/media/characters/blacklist/preview", {"tag": "cap"})
    before = len(calls)
    st, r = api("POST", "/api/media/characters/blacklist", {"tag": "cap", "confirm": True, "expected_count": pc["count"]})
    last = wait_sweep()["last"]
    check("max_per_sweep 3: the first sweep trashes 3 in one batch and leaves 5 for the next",
          last["trashed"] == 3 and last["deferred"] == 5 and len(calls) == before + 1
          and sum(trashed(f"cap/c{i}.jpg") for i in range(8)) == 3, last)
    ns["charbl_request_sweep"]()
    last = wait_sweep()["last"]
    check("... the second takes 3 more", last["trashed"] == 3 and last["deferred"] == 2, last)
    ns["charbl_request_sweep"]()
    last = wait_sweep()["last"]
    check("... the third the last 2, one batch per sweep",
          last["trashed"] == 2 and last["deferred"] == 0 and len(calls) == before + 3
          and sum(trashed(f"cap/c{i}.jpg") for i in range(8)) == 8, last)
    ns["CONFIG"]["character_blacklist"]["max_per_sweep"] = 5000

    # ---- the grid and the type-ahead hide blacklisted pools while a sweep is pending ----
    # hide_me has a costume tag; keep_me has one too and is not listed. Entries are written to
    # the store directly, so no sweep trashes anything: the pool is still live in the index.
    media("grid/h1.jpg"); tag("grid/h1.jpg", "hide_me", .9)
    media("grid/h2.jpg"); tag("grid/h2.jpg", "hide_me_(dress)", .9)
    media("grid/k1.jpg"); tag("grid/k1.jpg", "keep_me", .9)
    media("grid/k2.jpg"); tag("grid/k2.jpg", "keep_me_(dress)", .9)
    media("grid/k3.jpg"); tag("grid/k3.jpg", "keep_me", .8); tag("grid/k3.jpg", "keep_me_(dress)", .8)
    variants("hide_me", "hide_me_(dress)")
    variants("keep_me", "keep_me_(dress)")
    conn.commit()
    ns["charbl_update"](lambda d: d["entries"].extend([
        {"tag": "hide_me", "include_variants": True, "added_by": "alice", "added_at": 1},
        {"tag": "keep_me_(dress)", "include_variants": False, "added_by": "alice", "added_at": 1}]))

    def pools(url):
        st, out = api("GET", url)
        return {p["tag"]: p for p in out.get("items", [])}, out

    def variant_tags(p):
        return [v["tag"] for v in p["variants"]]

    ns["_charbl_state"].update(pending=False, running=False)
    grid, out = pools("/api/media/characters?limit=300")
    check("no sweep waiting: the pools are listed as they are", "hide_me" in grid and "keep_me" in grid, list(grid))
    total_before = out["total"]
    ns["_charbl_state"]["pending"] = True
    grid, out = pools("/api/media/characters?limit=300")
    check("sweep pending: the grid leaves the blacklisted pool (and her variants) out", "hide_me" not in grid
          and not any(t.startswith("hide_me") for t in grid), list(grid))
    check("... a variant-only entry removes just that variant from its pool",
          "keep_me" in grid and variant_tags(grid["keep_me"]) == [] , grid.get("keep_me"))
    check("... the total follows", out["total"] == total_before - 1, (out["total"], total_before))
    grid, _ = pools("/api/media/characters?limit=8&q=hide")
    check("... the type-ahead agrees", "hide_me" not in grid, list(grid))
    grid, _ = pools("/api/media/characters?limit=8&q=keep")
    check("... and still offers the others", "keep_me" in grid, list(grid))
    st, one = api("GET", "/api/media/characters/pool?tag=hide_me")
    check("... the single-pool route says not found", st == 404, one)
    st, disc = api("GET", "/api/media/discovery/characters?limit=200")
    names = [i["tag"] for i in disc.get("items", [])]
    check("... the discovery grid (what the Characters page reads) leaves her out too",
          st == 200 and "hide_me" not in names and "keep_me" in names, (st, names))
    ns["_charbl_state"].update(pending=False, running=True)
    grid, _ = pools("/api/media/characters?limit=300")
    check("sweep running: hidden as well", "hide_me" not in grid)
    ns["_charbl_state"].update(pending=False, running=False)
    grid, _ = pools("/api/media/characters?limit=300")
    check("sweep done: nothing is hidden by the list any more (files are gone by then, or live)", "hide_me" in grid)

    # ---- the store ----------------------------------------------------------------
    data = json.load(open(os.path.join(tmp, "character_blacklist.json")))
    check("character_blacklist.json holds entries and exemptions, written atomically (no .tmp left)",
          set(data) == {"entries", "exemptions"} and not os.path.exists(os.path.join(tmp, "character_blacklist.json.tmp")))
    st, r = api("POST", "/api/media/characters/blacklist/preview", {"tag": "miku"})
    ns["get_authenticated_user"] = lambda: {"username": "guest", "role": "guest"}
    st, r = api("GET", "/api/media/characters/blacklist")
    check("a non-admin is refused (403)", st == 403, st)
finally:
    shutil.rmtree(tmp, ignore_errors=True)
print("ALL PASS" if ok else "SOME CHECKS FAILED")
sys.exit(0 if ok else 1)
