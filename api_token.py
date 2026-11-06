#!/usr/bin/env python3
"""Mint, list and revoke web-dl API tokens.

An API token is the credential for things that cannot hold a browser session:
the phone's share-to-disk shortcut, scripts, cron. Clients send it as

    X-API-Key: <token>

which server.py checks in get_authenticated_user() alongside the session
cookie. A token carries the same role as the user it belongs to, so a token on
a guest account can browse media and nothing more.

Run it on the server, next to users.json:

    python3 api_token.py list
    python3 api_token.py issue  --user root
    python3 api_token.py revoke --user root

The token is printed once, at issue time. It is stored verbatim in users.json
(mode 0600) rather than hashed, because the server must compare it on every
request and this file is already the place the session cookies live -- adding
hashing here would protect nothing that is not already exposed.
"""
from __future__ import annotations

import argparse
import json
import os
import secrets
import sys
import tempfile
from pathlib import Path

USERS_FILE = Path(__file__).resolve().parent / "users.json"


def read_users() -> list:
    return json.loads(USERS_FILE.read_text(encoding="utf-8"))


def write_users(users: list) -> None:
    """Atomic replace, so a crash or a concurrent read never sees half a file.

    The server writes this file too, under its own lock, which this process
    cannot share. Token changes are rare and hand-run, so the practical advice
    is simply not to mint a token in the same second someone is editing users
    in the UI."""
    fd, tmp = tempfile.mkstemp(dir=str(USERS_FILE.parent), prefix=".users-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(users, fh, indent=2, ensure_ascii=False)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, USERS_FILE)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def find(users: list, username: str) -> dict | None:
    for u in users:
        if u["username"].lower() == username.lower():
            return u
    return None


def cmd_list(args) -> int:
    for u in read_users():
        tok = u.get("api_token")
        state = f"set ({str(tok)[:6]}…)" if tok else "-"
        print(f"  {u['username']:<14} role={u.get('role', 'admin'):<6} api_token={state}")
    return 0


def cmd_issue(args) -> int:
    users = read_users()
    user = find(users, args.user)
    if user is None:
        print(f"no such user: {args.user}", file=sys.stderr)
        return 1
    replacing = bool(user.get("api_token"))
    token = secrets.token_urlsafe(32)
    user["api_token"] = token
    write_users(users)
    print(f"{'Replaced' if replacing else 'Issued'} API token for "
          f"{user['username']} (role {user.get('role', 'admin')}).")
    print("Shown once -- store it now:\n")
    print(f"    {token}\n")
    print("Use it as a header:\n")
    print(f'    X-API-Key: {token}\n')
    print("Takes effect immediately; the server re-reads users.json per request.")
    return 0


def cmd_revoke(args) -> int:
    users = read_users()
    user = find(users, args.user)
    if user is None:
        print(f"no such user: {args.user}", file=sys.stderr)
        return 1
    if not user.pop("api_token", None):
        print(f"{user['username']} had no API token; nothing to do.")
        return 0
    write_users(users)
    print(f"Revoked the API token for {user['username']}. Effective immediately.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list", help="show which accounts have a token").set_defaults(fn=cmd_list)
    for name, fn, helptext in (("issue", cmd_issue, "create or replace a token"),
                               ("revoke", cmd_revoke, "delete a token")):
        sp = sub.add_parser(name, help=helptext)
        sp.add_argument("--user", required=True)
        sp.set_defaults(fn=fn)
    args = ap.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
