#!/usr/bin/env python3
"""Write and sign modules/index.json, the list the Settings page installs modules from (MODULE_SYSTEM_PLAN.md).

    python3 tools/publish_modules.py --key <secret key file>     # after changing a module (raise its version first)
    python3 tools/publish_modules.py --genkey <secret key file>  # once: a new signing key; prints the public key

The secret key file holds 64 hex digits (an Ed25519 seed) and must never enter the repository. Its public half is
PUBKEY_HEX in module_manager.py; installs refuse an index that this key did not sign. Then commit modules/ and push
the branch the installs read from (MEDIA_MODULES_BRANCH, default atrihord-public).
"""
import argparse
import base64
import datetime
import hashlib
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import atri_ed25519        # noqa: E402
import module_manager as mm  # noqa: E402


def read_key(path):
    with open(path, encoding="ascii") as f:
        seed = bytes.fromhex(f.read().strip())
    if len(seed) != 32:
        raise SystemExit("the key file must hold 64 hex digits")
    return seed


def build_index():
    mods = {}
    for mid, m in mm.local_sources().items():
        if "error" in m:
            raise SystemExit("module %s: %s" % (mid, m["error"]))
        folder = os.path.join(mm.SOURCE_DIR, mid)
        hashes, sizes = {}, {}
        for rel in m["files"]:
            with open(os.path.join(folder, *rel.split("/")), "rb") as f:
                data = f.read()
            if b"\r\n" in data:  # .gitattributes stores LF, so the pushed file would not match this hash
                raise SystemExit("%s/%s has CRLF line ends; convert it to LF first" % (mid, rel))
            hashes[rel] = hashlib.sha256(data).hexdigest()
            sizes[rel] = len(data)
        listed = set(m["files"])
        for root, _dirs, files in os.walk(folder):
            for name in files:
                rel = os.path.relpath(os.path.join(root, name), folder).replace(os.sep, "/")
                if rel not in listed and rel != "README.md":
                    print("warning: %s/%s is not in its module.json and will not be published" % (mid, rel))
        entry = dict(m)
        entry["sha256"], entry["sizes"] = hashes, sizes
        mods[mid] = entry
        print("%-14s %s  %d files, %d KB" % (mid, m["version"], len(m["files"]), sum(sizes.values()) // 1024))
    return {
        "format": 1,
        "generated": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "modules": mods,
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--key", help="secret key file (64 hex digits)")
    g.add_argument("--genkey", help="write a new secret key to this file and print its public key")
    a = ap.parse_args()
    if a.genkey:
        if os.path.exists(a.genkey):
            raise SystemExit("%s exists: not overwriting a signing key" % a.genkey)
        seed = os.urandom(32)
        os.makedirs(os.path.dirname(os.path.abspath(a.genkey)), exist_ok=True)
        with open(a.genkey, "w", encoding="ascii") as f:
            f.write(seed.hex() + "\n")
        print("public key: " + atri_ed25519.public_key(seed).hex())
        return
    seed = read_key(a.key)
    pub = atri_ed25519.public_key(seed).hex()
    if pub != mm.PUBKEY_HEX:
        print("warning: this key's public half (%s) is not module_manager.PUBKEY_HEX; installs will refuse it" % pub)
    raw = (json.dumps(build_index(), indent=1, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8")
    sig = base64.b64encode(atri_ed25519.sign(seed, raw)).decode("ascii")
    mm.check_index(raw, sig, pub)          # what an install will do with it
    with open(os.path.join(mm.SOURCE_DIR, "index.json"), "wb") as f:
        f.write(raw)
    with open(os.path.join(mm.SOURCE_DIR, "index.json.sig"), "w", encoding="ascii", newline="\n") as f:
        f.write(sig + "\n")
    print("wrote modules/index.json and modules/index.json.sig")


if __name__ == "__main__":
    main()
