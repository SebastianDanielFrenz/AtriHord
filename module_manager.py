#!/usr/bin/env python3
"""Optional AtriHord modules: list, install, update and remove them (MODULE_SYSTEM_PLAN.md).

A module is a folder of browser files: the source lives in the repo at modules/<id>/ (with module.json); it is
INSTALLED when it is in web_files/js/modules/<id>/ (git-ignored, so `git pull` never installs, removes or
overwrites one). Modules never run code on the server. Two sources:
  - the local checkout (modules/<id>/), used by install.sh, no network;
  - the online repo (GitHub, raw files of a branch): modules/index.json lists every module with each file's
    SHA-256 and size and is signed (index.json.sig, Ed25519). The signature is checked against the public key
    below before anything is downloaded, then every file against its hash, so neither a hijacked branch nor a
    tampered download can bring code onto the server.
server.py uses this module for Settings -> Modules; install.sh and people use the CLI:

    python3 module_manager.py list [--online]
    python3 module_manager.py install <id> [<id> ...] [--online]     # default source: this checkout
    python3 module_manager.py update <id> | --all                     # from the online repo
    python3 module_manager.py remove <id>
"""
import argparse
import base64
import hashlib
import json
import os
import re
import shutil
import sys
import threading
import time
import urllib.request

import atri_ed25519

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SOURCE_DIR = os.path.join(BASE_DIR, "modules")
INSTALL_DIR = os.path.join(BASE_DIR, "web_files", "js", "modules")
PREVIOUS_DIR = os.path.join(INSTALL_DIR, ".previous")     # the version an update replaced (one per module)
REMOVED_DIR = os.path.join(INSTALL_DIR, ".removed")       # removed modules, kept until they are installed again

REPO = os.environ.get("MEDIA_MODULES_REPO", "SebastianDanielFrenz/AtriHord").strip()
BRANCH = os.environ.get("MEDIA_MODULES_BRANCH", "atrihord-public").strip()
# Where index.json and the module folders are read from; by default the raw files of REPO/BRANCH on GitHub. A mirror
# or a test server can stand in (it must serve the same signed files; the signature is what is trusted, not the host).
BASE_URL = (os.environ.get("MEDIA_MODULES_URL", "").strip()
            or "https://raw.githubusercontent.com/%s/%s/modules/" % (REPO, BRANCH))
# Public half of the maintainer's module signing key (the secret half never leaves the maintainer's machine).
# A fork that publishes its own modules sets MEDIA_MODULES_PUBKEY to its own key (64 hex digits).
PUBKEY_HEX = os.environ.get("MEDIA_MODULES_PUBKEY", "").strip() or "012d7492cda6bd9ad0cb161b9fd2e36c189e0a9cbc1afd8cd8fcc2662920b4c5"

ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")
FILE_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]*(/[A-Za-z0-9_][A-Za-z0-9_.-]*)*$")
VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")
MAX_FILES = 64
MAX_MODULE_BYTES = 8 * 1024 * 1024
MAX_INDEX_BYTES = 512 * 1024
INDEX_TTL = 600                       # seconds an online index is reused (like the bridge's release check)

_lock = threading.Lock()
_index_cache = {"at": 0.0, "index": None, "error": None}


class ModuleError(Exception):
    """Anything that stops an operation; the message is meant for the person who asked."""


# ---- manifests --------------------------------------------------------------------------------------------------
def check_manifest(m, expect_id=None):
    """Validate a module.json dict (also the entries of the online index); return it. Raises ModuleError."""
    if not isinstance(m, dict):
        raise ModuleError("module.json is not an object")
    mid = m.get("id")
    if not isinstance(mid, str) or not ID_RE.match(mid):
        raise ModuleError("bad module id %r" % (mid,))
    if expect_id and mid != expect_id:
        raise ModuleError("module.json says %r, expected %r" % (mid, expect_id))
    if not isinstance(m.get("version"), str) or not VERSION_RE.match(m["version"]):
        raise ModuleError("%s: bad version %r" % (mid, m.get("version")))
    for k in ("name", "description"):
        if not isinstance(m.get(k), str) or len(m[k]) > 300:
            raise ModuleError("%s: bad %s" % (mid, k))
    files = m.get("files")
    if not isinstance(files, list) or not files or len(files) > MAX_FILES:
        raise ModuleError("%s: bad file list" % mid)
    for f in files:
        if not isinstance(f, str) or not FILE_RE.match(f) or ".." in f.split("/"):
            raise ModuleError("%s: bad file name %r" % (mid, f))
        if not (f.endswith(".js") or f == "module.json"):
            raise ModuleError("%s: %r is not a .js file (guests may load nothing else)" % (mid, f))
    if "module.json" not in files or "module.js" not in files:
        raise ModuleError("%s: module.json and module.js must be in the file list" % mid)
    if len(set(files)) != len(files):
        raise ModuleError("%s: a file is listed twice" % mid)
    return m


def read_manifest(folder, expect_id=None):
    try:
        with open(os.path.join(folder, "module.json"), encoding="utf-8") as f:
            return check_manifest(json.load(f), expect_id)
    except (OSError, ValueError) as e:
        raise ModuleError("%s: module.json unreadable (%s)" % (os.path.basename(folder), e))


def _scan(root):
    out = {}
    if not os.path.isdir(root):
        return out
    for name in sorted(os.listdir(root)):
        folder = os.path.join(root, name)
        if name.startswith(".") or not os.path.isdir(folder) or not ID_RE.match(name):
            continue
        try:
            out[name] = read_manifest(folder, name)
        except ModuleError as e:
            out[name] = {"id": name, "error": str(e)}
    return out


def installed():
    """{id: manifest} of the installed modules (a broken one carries "error")."""
    return _scan(INSTALL_DIR)


def local_sources(source_dir=None):
    """{id: manifest} of the modules in a checkout's modules/ folder."""
    return _scan(source_dir or SOURCE_DIR)


def version_tuple(v):
    try:
        return tuple(int(x) for x in v.split("."))
    except (AttributeError, ValueError):
        return (0, 0, 0)


# ---- installing -------------------------------------------------------------------------------------------------
def _swap_in(mid, staged):
    """Make the staged folder the installed module in one rename; the replaced version goes to .previous/<id>."""
    os.makedirs(PREVIOUS_DIR, exist_ok=True)
    dest = os.path.join(INSTALL_DIR, mid)
    prev = os.path.join(PREVIOUS_DIR, mid)
    if os.path.isdir(dest):
        if os.path.isdir(prev):
            shutil.rmtree(prev)
        os.replace(dest, prev)
    os.replace(staged, dest)
    removed = os.path.join(REMOVED_DIR, mid)
    if os.path.isdir(removed):
        shutil.rmtree(removed, ignore_errors=True)


def _stage(mid, blobs):
    """Write {relative path: bytes} into a fresh staging folder next to the installed modules; return its path."""
    staged = os.path.join(INSTALL_DIR, ".staging-%s-%d" % (mid, os.getpid()))
    if os.path.isdir(staged):
        shutil.rmtree(staged)
    for rel, data in blobs.items():
        path = os.path.join(staged, *rel.split("/"))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(data)
    return staged


def install_local(mid, source_dir=None):
    """Install (or reinstall) a module from a checkout's modules/<id>/. Returns its manifest."""
    if not ID_RE.match(mid or ""):
        raise ModuleError("bad module id %r" % (mid,))
    src = os.path.join(source_dir or SOURCE_DIR, mid)
    m = read_manifest(src, mid)
    blobs, total = {}, 0
    for rel in m["files"]:
        try:
            with open(os.path.join(src, *rel.split("/")), "rb") as f:
                blobs[rel] = f.read(MAX_MODULE_BYTES + 1)
        except OSError as e:
            raise ModuleError("%s: %s is missing (%s)" % (mid, rel, e))
        total += len(blobs[rel])
        if total > MAX_MODULE_BYTES:
            raise ModuleError("%s: larger than %d MB" % (mid, MAX_MODULE_BYTES // 2 ** 20))
    with _lock:
        os.makedirs(INSTALL_DIR, exist_ok=True)
        _swap_in(mid, _stage(mid, blobs))
    return m


def remove(mid):
    """Uninstall: the folder moves to .removed/<id> (installing again clears it)."""
    if not ID_RE.match(mid or ""):
        raise ModuleError("bad module id %r" % (mid,))
    dest = os.path.join(INSTALL_DIR, mid)
    with _lock:
        if not os.path.isdir(dest):
            raise ModuleError("%s is not installed" % mid)
        os.makedirs(REMOVED_DIR, exist_ok=True)
        target = os.path.join(REMOVED_DIR, mid)
        if os.path.isdir(target):
            shutil.rmtree(target)
        os.replace(dest, target)


# ---- the online repo --------------------------------------------------------------------------------------------
def raw_url(path):
    return BASE_URL.rstrip("/") + "/" + path


def _get(url, limit, timeout=20, too_big=None):
    req = urllib.request.Request(url, headers={"User-Agent": "atrihord-modules", "Cache-Control": "no-cache"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = r.read(limit + 1)
    if len(data) > limit:
        raise ModuleError(too_big or "%s is larger than allowed" % url)
    return data


def check_index(raw, sig_text, pubkey_hex=None):
    """Verify the signature of the raw index bytes and validate its content; return the parsed index."""
    key = pubkey_hex or PUBKEY_HEX
    if not re.fullmatch(r"[0-9a-f]{64}", key or ""):
        raise ModuleError("no module signing key is configured (MEDIA_MODULES_PUBKEY)")
    try:
        sig = base64.b64decode(sig_text.strip(), validate=True)
    except ValueError:
        raise ModuleError("the index signature is not base64")
    if not atri_ed25519.verify(bytes.fromhex(key), raw, sig):
        raise ModuleError("the module index is NOT signed by the maintainer's key: nothing was installed")
    try:
        idx = json.loads(raw.decode("utf-8"))
    except ValueError:
        raise ModuleError("the module index is not JSON")
    if not isinstance(idx, dict) or idx.get("format") != 1 or not isinstance(idx.get("modules"), dict):
        raise ModuleError("unknown module index format")
    for mid, m in idx["modules"].items():
        check_manifest(m, mid)
        hashes = m.get("sha256")
        sizes = m.get("sizes")
        if not isinstance(hashes, dict) or not isinstance(sizes, dict) or set(hashes) != set(m["files"]) or set(sizes) != set(m["files"]):
            raise ModuleError("%s: the index lacks hashes or sizes" % mid)
        for f in m["files"]:
            if not re.fullmatch(r"[0-9a-f]{64}", str(hashes[f])) or not isinstance(sizes[f], int) or not 0 <= sizes[f] <= MAX_MODULE_BYTES:
                raise ModuleError("%s: bad hash or size for %s" % (mid, f))
        if sum(sizes.values()) > MAX_MODULE_BYTES:
            raise ModuleError("%s: larger than %d MB" % (mid, MAX_MODULE_BYTES // 2 ** 20))
    return idx


def online_index(force=False):
    """The verified online index (cached for INDEX_TTL seconds). Raises ModuleError when it cannot be had."""
    now = time.time()
    if not force and _index_cache["index"] is not None and now - _index_cache["at"] < INDEX_TTL:
        return _index_cache["index"]
    try:
        raw = _get(raw_url("index.json"), MAX_INDEX_BYTES)
        sig = _get(raw_url("index.json.sig"), 1024).decode("ascii", "replace")
        idx = check_index(raw, sig)
    except ModuleError as e:
        _index_cache.update(at=now, error=str(e))
        raise
    except Exception as e:                       # network, HTTP errors
        _index_cache.update(at=now, error="the online repo could not be reached (%s)" % e)
        raise ModuleError(_index_cache["error"])
    _index_cache.update(at=now, index=idx, error=None)
    return idx


def install_online(mid, index=None):
    """Install or update a module from the online repo. Every file must match the signed index."""
    if not ID_RE.match(mid or ""):
        raise ModuleError("bad module id %r" % (mid,))
    idx = index or online_index()
    m = idx["modules"].get(mid)
    if not m:
        raise ModuleError("%s is not in the online repo" % mid)
    blobs = {}
    for attempt in (0, 1):
        try:
            for rel in m["files"]:
                data = _get(raw_url("%s/%s" % (mid, rel)), m["sizes"][rel], timeout=60,
                            too_big="%s/%s does not match the signed index (larger)" % (mid, rel))
                if len(data) != m["sizes"][rel] or hashlib.sha256(data).hexdigest() != m["sha256"][rel]:
                    raise ModuleError("%s/%s does not match the signed index" % (mid, rel))
                blobs[rel] = data
            break
        except ModuleError:
            # the branch may have moved between reading the index and the files: read both once more
            if attempt or index is not None:
                raise
            idx = online_index(force=True)
            m = idx["modules"].get(mid)
            if not m:
                raise ModuleError("%s is no longer in the online repo" % mid)
            blobs = {}
        except Exception as e:
            raise ModuleError("download of %s failed (%s)" % (mid, e))
    with _lock:
        os.makedirs(INSTALL_DIR, exist_ok=True)
        _swap_in(mid, _stage(mid, blobs))
    return {k: m[k] for k in ("id", "name", "version", "description", "files")}


def overview(online=True):
    """What Settings -> Modules shows: every installed and every available module, merged by id."""
    inst = installed()
    idx, error = None, None
    if online:
        try:
            idx = online_index()
        except ModuleError as e:
            error = str(e)
    avail = (idx or {}).get("modules", {})
    local = {k: v for k, v in local_sources().items() if "error" not in v}
    rows = []
    for mid in sorted(set(inst) | set(avail) | set(local)):
        i, a, lo = inst.get(mid), avail.get(mid), local.get(mid)
        src = i if i and "error" not in i else (a or lo or i)
        rows.append({
            "id": mid,
            "name": src.get("name", mid),
            "description": src.get("description", ""),
            "installed": (i or {}).get("version") if i else None,
            "broken": (i or {}).get("error"),
            "available": a["version"] if a else None,
            "local": lo["version"] if lo else None,           # in this server's checkout (modules/)
            "size": sum(a["sizes"].values()) if a else None,
            "update": bool(i and a and "error" not in i and version_tuple(a["version"]) > version_tuple(i["version"])),
        })
    return {"modules": rows, "online_error": error, "repo": REPO, "branch": BRANCH}


# ---- CLI --------------------------------------------------------------------------------------------------------
def _main(argv):
    ap = argparse.ArgumentParser(prog="module_manager.py", description="Optional AtriHord modules.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("list", help="installed modules (and with --online the ones in the online repo)")
    p.add_argument("--online", action="store_true")
    p.add_argument("--json", action="store_true", help="machine-readable (install.sh reads it)")
    p = sub.add_parser("install", help="install modules (default: from this checkout)")
    p.add_argument("ids", nargs="+")
    p.add_argument("--online", action="store_true", help="from the online repo instead of this checkout")
    p.add_argument("--from", dest="source", default=None, help="another checkout's modules/ folder")
    p = sub.add_parser("update", help="update from the online repo")
    p.add_argument("ids", nargs="*")
    p.add_argument("--all", action="store_true")
    p = sub.add_parser("remove", help="uninstall (kept in web_files/js/modules/.removed/)")
    p.add_argument("ids", nargs="+")
    a = ap.parse_args(argv)
    try:
        if a.cmd == "list":
            if a.json:
                local = local_sources()
                print(json.dumps({"installed": installed(), "local": local}, indent=1))
                return 0
            ov = overview(online=a.online)
            for mid, m in local_sources().items():
                if not any(r["id"] == mid for r in ov["modules"]):
                    ov["modules"].append({"id": mid, "name": m.get("name", mid), "installed": None, "available": None,
                                          "description": m.get("description", ""), "update": False, "broken": m.get("error")})
            for r in ov["modules"]:
                state = ("installed %s" % r["installed"]) if r["installed"] else "not installed"
                if r.get("broken"):
                    state = "BROKEN: " + r["broken"]
                extra = " (online: %s%s)" % (r["available"], ", update available" if r["update"] else "") if r.get("available") else ""
                print("%-14s %-34s %s%s" % (r["id"], r["name"], state, extra))
            if ov["online_error"]:
                print("online repo: " + ov["online_error"], file=sys.stderr)
            return 0
        if a.cmd == "install":
            for mid in a.ids:
                m = install_online(mid) if a.online else install_local(mid, a.source)
                print("installed %s %s" % (mid, m["version"]))
            return 0
        if a.cmd == "update":
            ids = [r["id"] for r in overview()["modules"] if r["update"]] if a.all else a.ids
            if not ids:
                print("nothing to update")
            for mid in ids:
                m = install_online(mid)
                print("updated %s to %s" % (mid, m["version"]))
            return 0
        if a.cmd == "remove":
            for mid in a.ids:
                remove(mid)
                print("removed %s" % mid)
            return 0
    except ModuleError as e:
        print("error: %s" % e, file=sys.stderr)
        return 1
    return 2


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
