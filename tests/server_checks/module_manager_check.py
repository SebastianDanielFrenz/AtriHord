"""Checks of the optional-module system (module_manager.py, atri_ed25519.py, tools/publish_modules.py).

    py -3 tests/server_checks/module_manager_check.py

No network, nothing outside a temp folder is written: a local HTTP server stands in for the online repo, serving a
copy of modules/ signed with a throwaway key. Covers: the RFC 8032 vectors, local install / update / remove, the
online install, and that a tampered file, a tampered index, a foreign key and hostile paths are all refused.
"""
import base64
import functools
import hashlib
import http.server
import json
import os
import shutil
import sys
import tempfile
import threading

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
import atri_ed25519 as ed  # noqa: E402
import module_manager as mm  # noqa: E402

ok = 0


def check(cond, what):
    global ok
    if not cond:
        raise SystemExit("FAIL: " + what)
    ok += 1


def refused(fn, what, needle=""):
    try:
        fn()
    except mm.ModuleError as e:
        check(needle.lower() in str(e).lower(), "%s (message: %s)" % (what, e))
        return
    raise SystemExit("FAIL: not refused: " + what)


# ---- Ed25519 against RFC 8032 section 7.1 ------------------------------------------------------------------------
for sk, pk, msg, sig in [
    ("9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60", "d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a", "",
     "e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e065224901555fb8821590a33bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b"),
    ("4ccd089b28ff96da9db6c346ec114e0f5b8a319f35aba624da8cf6ed4fb8a6fb", "3d4017c3e843895a92b70aa74d1b7ebc9c982ccf2ec4968cc0cd55f12af4660c", "72",
     "92a009a9f0d4cab8720e820b5f642540a2b27b5416503f8fb3762223ebdb69da085ac1e43e15996e458f3613d0f11d8c387b2eaeb4302aeeb00d291612bb0c00"),
]:
    sk, pk, msg, sig = (bytes.fromhex(x) for x in (sk, pk, msg, sig))
    check(ed.public_key(sk) == pk, "RFC public key")
    check(ed.sign(sk, msg) == sig, "RFC signature")
    check(ed.verify(pk, msg, sig), "RFC verify")
    check(not ed.verify(pk, msg + b"!", sig), "verify refuses another message")
    s = bytearray(sig)
    s[40] ^= 1
    check(not ed.verify(pk, msg, bytes(s)), "verify refuses a changed signature")

# ---- a sandbox: installs go into a temp folder ------------------------------------------------------------------
tmp = tempfile.mkdtemp(prefix="modcheck-")
mm.INSTALL_DIR = os.path.join(tmp, "installed")
mm.PREVIOUS_DIR = os.path.join(mm.INSTALL_DIR, ".previous")
mm.REMOVED_DIR = os.path.join(mm.INSTALL_DIR, ".removed")
try:
    # local install / reinstall / remove
    m = mm.install_local("visualizer")
    check(os.path.isfile(os.path.join(mm.INSTALL_DIR, "visualizer", "vendor", "butterchurn.min.js")), "local install copies the files")
    check(mm.installed()["visualizer"]["version"] == m["version"], "installed() reads the manifest")
    mm.install_local("visualizer")
    check(os.path.isdir(os.path.join(mm.PREVIOUS_DIR, "visualizer")), "a reinstall keeps the replaced copy in .previous")
    mm.remove("visualizer")
    check("visualizer" not in mm.installed() and os.path.isdir(os.path.join(mm.REMOVED_DIR, "visualizer")), "remove moves it aside")
    refused(lambda: mm.remove("visualizer"), "removing what is not installed", "not installed")
    refused(lambda: mm.install_local("../etc"), "a path as module id", "bad module id")

    # manifest rules
    base = mm.read_manifest(os.path.join(mm.SOURCE_DIR, "visualizer"))
    for bad, needle in [
        (dict(base, files=base["files"] + ["../../server.py"]), "bad file name"),
        (dict(base, files=base["files"] + ["/etc/passwd"]), "bad file name"),
        (dict(base, files=base["files"] + ["evil.html"]), "not a .js file"),
        (dict(base, files=[f for f in base["files"] if f != "module.js"]), "must be in the file list"),
        (dict(base, version="1.0"), "bad version"),
    ]:
        refused(lambda b=bad: mm.check_manifest(b), "manifest: " + needle, needle)

    # ---- the online repo: a copy of modules/ signed with a throwaway key, served locally -------------------------
    repo = os.path.join(tmp, "repo")
    shutil.copytree(mm.SOURCE_DIR, os.path.join(repo, "modules"))
    seed = os.urandom(32)
    pub = ed.public_key(seed).hex()

    def publish(index):
        raw = (json.dumps(index, indent=1, sort_keys=True) + "\n").encode()
        with open(os.path.join(repo, "modules", "index.json"), "wb") as f:
            f.write(raw)
        with open(os.path.join(repo, "modules", "index.json.sig"), "w") as f:
            f.write(base64.b64encode(ed.sign(seed, raw)).decode())
        return raw

    entry = dict(base)
    entry["sha256"], entry["sizes"] = {}, {}
    for rel in base["files"]:
        data = open(os.path.join(repo, "modules", "visualizer", *rel.split("/")), "rb").read()
        entry["sha256"][rel] = hashlib.sha256(data).hexdigest()
        entry["sizes"][rel] = len(data)
    good = {"format": 1, "generated": "test", "modules": {"visualizer": entry}}
    publish(good)

    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=repo)
    handler.log_message = lambda *a: None
    http.server.SimpleHTTPRequestHandler.log_message = lambda *a: None
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    mm.BASE_URL = "http://127.0.0.1:%d/modules/" % srv.server_address[1]
    mm.PUBKEY_HEX = pub

    idx = mm.online_index(force=True)
    check("visualizer" in idx["modules"], "signed index accepted")
    got = mm.install_online("visualizer")
    check(got["version"] == base["version"] and mm.installed()["visualizer"]["version"] == base["version"], "online install")
    ov = mm.overview()
    check(ov["modules"][0]["installed"] == base["version"] and not ov["modules"][0]["update"], "overview: installed, no update")

    # a newer version online shows as an update
    newer = json.loads(json.dumps(good))
    newer["modules"]["visualizer"]["version"] = "9.0.0"
    publish(newer)
    mm.online_index(force=True)
    check(mm.overview()["modules"][0]["update"], "overview marks a newer online version as an update")
    publish(good)

    # a file changed after signing -> refused, nothing replaced
    vf = os.path.join(repo, "modules", "visualizer", "visualizer.js")
    orig = open(vf, "rb").read()
    open(vf, "ab").write(b"\n// evil\n")
    before = open(os.path.join(mm.INSTALL_DIR, "visualizer", "visualizer.js"), "rb").read()
    refused(lambda: mm.install_online("visualizer", index=mm.online_index(force=True)), "a tampered file", "does not match")
    check(open(os.path.join(mm.INSTALL_DIR, "visualizer", "visualizer.js"), "rb").read() == before, "a refused update leaves the installed copy alone")
    open(vf, "wb").write(orig)

    # the index changed after signing -> refused
    raw = publish(good)
    with open(os.path.join(repo, "modules", "index.json"), "wb") as f:
        f.write(raw.replace(b'"format": 1', b'"format": 1 '))
    refused(lambda: mm.online_index(force=True), "an index changed after signing", "not signed")

    # signed by another key -> refused
    publish(good)
    mm.PUBKEY_HEX = ed.public_key(os.urandom(32)).hex()
    refused(lambda: mm.online_index(force=True), "an index signed by a foreign key", "not signed")
    mm.PUBKEY_HEX = pub

    # a signed index with a hostile path is still refused
    hostile = json.loads(json.dumps(good))
    e = hostile["modules"]["visualizer"]
    e["files"].append("../../../server.py")
    e["sha256"]["../../../server.py"] = "0" * 64
    e["sizes"]["../../../server.py"] = 1
    publish(hostile)
    refused(lambda: mm.online_index(force=True), "a signed index with a hostile path", "bad file name")

    srv.shutdown()

    # the real published index (if present) is signed by the key in module_manager.py
    real = os.path.join(mm.SOURCE_DIR, "index.json")
    if os.path.isfile(real):
        import importlib
        fresh = importlib.reload(mm)
        raw = open(real, "rb").read()
        sig = open(real + ".sig").read()
        fresh.check_index(raw, sig)
        check(True, "modules/index.json verifies with the embedded public key")
        for mid, ent in json.loads(raw)["modules"].items():
            for rel in ent["files"]:
                data = open(os.path.join(fresh.SOURCE_DIR, mid, *rel.split("/")), "rb").read()
                check(hashlib.sha256(data).hexdigest() == ent["sha256"][rel], "index is current for %s/%s (run tools/publish_modules.py)" % (mid, rel))
finally:
    shutil.rmtree(tmp, ignore_errors=True)

print("module_manager checks: %d passed" % ok)
