"""Runs one harness with every write into the live app dir refused:

  venv/bin/python writeguard.py <harness>_check.py <args...>

The harnesses load server.py's functions out of its AST and resolve missing
names from it, with __file__ faked to the live server.py. A function that
mentions CONFIG then drags in `CONFIG = load_config()`, BASE_DIR comes out as
the live dir, and load_config rewrites the real config.json - it happened
(upscale_ready_check, 2026-09-27). Checking each harness by hand for that
misses the next one; this refuses the write instead (PermissionError, which
load_config and most writers catch and log) and prints a FAIL line, so
run_all.sh fails the harness and names the file.

Covered: open/io.open for writing, os.open with a write flag, os.replace,
os.rename, os.remove/unlink, os.mkdir/makedirs, os.rmdir, sqlite3.connect
without mode=ro. Python's own .pyc writes go through posix directly and pass.
"""
import atexit
import builtins
import io
import os
import runpy
import sqlite3
import sys

LIVE = os.path.realpath(os.environ.get("LIVE_DIR", "/data2/ksf_web_dl"))
hits = []


def _live(path):
    if isinstance(path, int):
        return False
    try:
        p = os.path.realpath(os.fsdecode(os.fspath(path)))
    except TypeError:
        return False
    return p == LIVE or p.startswith(LIVE + os.sep)


def _refuse(what, path):
    hits.append(f"{what} {os.fsdecode(os.fspath(path))}")
    raise PermissionError(f"writeguard: {what} {path} is in the live app dir")


class _Callable:
    """A guard as an object, not a function: Python 3.10's pathlib keeps
    `open = io.open` as a class attribute, and a plain function put there is
    bound as a method (the accessor arrives as the file name)."""
    def __init__(self, fn):
        self.fn = fn

    def __call__(self, *args, **kwargs):
        return self.fn(*args, **kwargs)


_open = builtins.open


def _only_locks(file, mode):
    """An existing *.lock opened a+ only to flock it writes nothing:
    recent_media_store takes its shared read lock that way (favorites_check
    reads the live recent list on purpose). Its writes go through a .tmp file
    and os.replace, which stay refused."""
    return mode == "a+" and os.fsdecode(os.fspath(file)).endswith(".lock") and os.path.isfile(file)


def _guarded_open(file, mode="r", *args, **kwargs):
    if any(c in mode for c in "wax+") and _live(file) and not _only_locks(file, mode):
        _refuse(f"open({mode})", file)
    return _open(file, mode, *args, **kwargs)


builtins.open = io.open = _Callable(_guarded_open)

_os_open = os.open
_WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND


def _guarded_os_open(path, flags, *args, **kwargs):
    if flags & _WRITE_FLAGS and _live(path):
        _refuse("os.open", path)
    return _os_open(path, flags, *args, **kwargs)


os.open = _Callable(_guarded_os_open)


def _guard_paths(name, count):
    real = getattr(os, name)

    def guarded(*args, **kwargs):
        for path in args[:count]:
            # makedirs(..., exist_ok=True) on a dir that is there writes nothing.
            if _live(path) and not (name in ("mkdir", "makedirs") and os.path.isdir(path)):
                _refuse(f"os.{name}", path)
        return real(*args, **kwargs)
    setattr(os, name, _Callable(guarded))


for _name, _count in (("replace", 2), ("rename", 2), ("remove", 1), ("unlink", 1), ("mkdir", 1),
                      ("makedirs", 1), ("rmdir", 1)):
    _guard_paths(_name, _count)

_connect = sqlite3.connect


def _guarded_connect(database, *args, **kwargs):
    name = os.fsdecode(database) if isinstance(database, (str, bytes, os.PathLike)) else ""
    if name.startswith("file:"):
        target, _, query = name[5:].partition("?")
        if "mode=ro" not in query and _live(target):
            _refuse("sqlite3.connect", target)
    elif name and name != ":memory:" and _live(name):
        _refuse("sqlite3.connect", name)
    return _connect(database, *args, **kwargs)


sqlite3.connect = _Callable(_guarded_connect)


@atexit.register
def _report():
    for hit in dict.fromkeys(hits):
        print(f"FAIL writeguard: the harness wrote into the live app dir: {hit}", flush=True)


if len(sys.argv) < 2:
    sys.exit(__doc__)
sys.argv = sys.argv[1:]
runpy.run_path(sys.argv[0], run_name="__main__")
