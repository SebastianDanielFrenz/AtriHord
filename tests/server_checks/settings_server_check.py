"""The settings API of a STAGED server.py, run out of its AST in a Flask test
request, against temporary copies: config.json, indexer_config.json and the
usage/volume files are copied into a private temp dir, users.json is synthetic
(no real API tokens are copied). Nothing under /data2 is written.

  venv/bin/python settings_server_check.py /tmp/.../server.py
"""
import ast
import json
import os
import shutil
import sys
import tempfile

BASE = os.environ.get("ATRIHORD_DIR") or os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SERVER = os.path.abspath(sys.argv[1])
sys.path.insert(0, BASE)
# The staged server's own modules come first: a new one (song_covers.py) is
# not on the live server yet, and a changed one must be read from the stage.
sys.path.insert(0, os.path.dirname(SERVER))

tmp = tempfile.mkdtemp(prefix="settings_check_")
for name in ("config.json", "indexer_config.json", "pipeline_usage.json", "download_volume.json"):
    if os.path.exists(os.path.join(BASE, name)):
        shutil.copy(os.path.join(BASE, name), os.path.join(tmp, name))
with open(os.path.join(tmp, "users.json"), "w") as fh:
    json.dump([{"username": "alice", "role": "admin", "prefs": {"advanced_mode": True}},
               {"username": "guest1", "role": "guest"}], fh)

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


# Every file the code under test could write points into the temp dir.
ns["CONFIG_FILE"] = os.path.join(tmp, "config.json")
ns["USERS_FILE"] = os.path.join(tmp, "users.json")
ns["INDEXER_CONFIG_FILE"] = os.path.join(tmp, "indexer_config.json")
ns["USAGE_FILE"] = os.path.join(tmp, "pipeline_usage.json")
ns["DOWNLOAD_VOLUME_FILE"] = os.path.join(tmp, "download_volume.json")
ns["get_authenticated_user"] = lambda: {"username": "alice", "role": "admin"}
for name in ("_read_roots_unlocked", "_read_users_unlocked", "_read_usage_unlocked", "app", "get_settings", "put_settings", "settings_users",
             "settings_user_token", "settings_stats", "auto_trash_reasons", "settings_dimensions_run"):
    load(name)
app = ns["app"]


def call(fn, path, method="GET", body=None, args=()):
    with app.test_request_context(path, method=method, json=body):
        resp = resolve(lambda: ns[fn](*args))
    status = 200
    if isinstance(resp, tuple):
        resp, status = resp[0], resp[1]
    return status, resp.get_json()


ok = True


def check(label, cond, detail=""):
    global ok
    ok = ok and bool(cond)
    print(("PASS " if cond else "FAIL ") + label + (f"  ({detail})" if detail else ""))


status, data = call("get_settings", "/api/settings")
ids = [s["id"] for s in data["schema"]]
#        vr_share is in the repo's server.py only until it is deployed; stereo3d (2D to 3D) is last
check("GET schema sections", status == 200 and [i for i in ids if i != "vr_share"] ==
      ["general", "shares", "downloads", "image_processing", "song_covers", "live_upscale", "stereo3d"], ids)
#        "song_covers" is the temporary cover-repair job: it goes out of this list with it
missing = [k for k, v in data["values"].items() if v is None]
check("every setting has a value", not missing, missing)
print("     sample:", {k: data["values"][k] for k in ("indexer.pipeline", "auto_trash_reasons",
                                                   "live_upscale.view_seconds", "dimensions_backfill.enabled")})
before_sites = dict(data["values"]["pipeline_wait_seconds"])

good = {"live_upscale.view_seconds": 7.5, "indexer.pass_budget_s": 900, "auto_trash_reasons": ["no subject"],
        "pipeline_wait_seconds": {"twitter": 9}, "live_upscale.photo_tags": ["realistic", "cosplay"],
        "dimensions_backfill.enabled": True, "shares.default_expiry_days": 30, "live_upscale.max_factor": 6}
status, data = call("put_settings", "/api/settings", "PUT", {"values": good})
check("PUT valid values", status == 200, data if status != 200 else "")
saved = json.load(open(os.path.join(tmp, "config.json")))
indexer = json.load(open(os.path.join(tmp, "indexer_config.json")))
check("config.json written", saved["live_upscale"]["view_seconds"] == 7.5 and saved["auto_trash_reasons"] == ["no subject"]
      and saved["dimensions_backfill"]["enabled"] is True and saved["shares"]["default_expiry_days"] == 30
      and saved["live_upscale"]["max_factor"] == 6)
sites_after = saved["pipeline_wait_seconds"]
check("per-site map merged, other sites kept", sites_after.get("twitter") == 9
      and all(sites_after.get(k) == v for k, v in before_sites.items() if k != "twitter"), sites_after)
check("indexer_config.json written, worker untouched", indexer.get("pass_budget_s") == 900 and indexer.get("worker"),
      {k: indexer.get(k) for k in ("pass_budget_s", "worker", "pipeline")})
check("auto_trash_reasons() follows config", tuple(resolve(lambda: ns["auto_trash_reasons"]())) == ("no subject",))

snapshot = open(os.path.join(tmp, "config.json")).read()
bad = {"live_upscale.view_seconds": 0, "indexer.pipeline": ["tags", "tags"], "youtube_playlist_sleep.min_seconds": 500,
       "youtube_playlist_sleep.max_seconds": 100, "bogus": 1, "live_upscale.photo_tags": ["ok_tag", "bad tag<b>"],
       "max_recent_media_items": "lots", "live_upscale.max_factor": 1, "auto_trash_reasons": ["delete everything"],
       "shares.default_expiry_days": -1}
status, data = call("put_settings", "/api/settings", "PUT", {"values": bad})
errors = (data or {}).get("errors", {})
check("PUT invalid values rejected", status == 400, status)
expected = {"live_upscale.view_seconds", "indexer.pipeline", "youtube_playlist_sleep.max_seconds", "bogus",
            "live_upscale.photo_tags", "max_recent_media_items", "live_upscale.max_factor", "auto_trash_reasons",
            "shares.default_expiry_days"}
check("each bad value named", expected <= set(errors), sorted(set(expected) - set(errors)))
for key in sorted(errors):
    print("     %-38s %s" % (key, errors[key]))
check("nothing written on rejection (all or nothing)", open(os.path.join(tmp, "config.json")).read() == snapshot)

status, data = call("settings_users", "/api/settings/users")
check("GET users", status == 200 and [u["username"] for u in data["users"]] == ["alice", "guest1"]
      and data["groups"]["admin"] and data["roots"], (status, data))
status, data = call("settings_user_token", "/api/settings/users/guest1/token", "POST", args=("guest1",))
users = json.load(open(os.path.join(tmp, "users.json")))
check("POST token issues one", status == 200 and len(data.get("token", "")) > 30
      and users[1].get("api_token") == data.get("token"))
status, data = call("settings_user_token", "/api/settings/users/guest1/token", "DELETE", args=("guest1",))
users = json.load(open(os.path.join(tmp, "users.json")))
check("DELETE token revokes it", status == 200 and "api_token" not in users[1])
status, _ = call("settings_user_token", "/api/settings/users/nobody/token", "POST", args=("nobody",))
check("unknown user is 404", status == 404)

status, data = call("settings_stats", "/api/settings/stats")
check("GET stats", status == 200 and "pipelines" in data and "volume_24h_bytes" in data and "dimensions" in data
      and "signed_in" in (data.get("youtube_cookies") or {}),
      data if status != 200 else sorted(data))

shutil.rmtree(tmp, ignore_errors=True)
print("ALL PASS" if ok else "SOME CHECKS FAILED")
