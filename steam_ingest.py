"""Receive Steam Frame screenshots pushed by the headset-side watcher.

POST /api/media/ingest (multipart field "file", authenticated, admin only)
stores one image/video in <downloads>/Steam Frame/, flat, and answers with
the stored name. Re-sending the same bytes is a no-op ("duplicate"), so the
watcher can retry blindly. Registered from server.py via register().
"""
import hashlib
import os
import re
import threading

from flask import jsonify, request

FOLDER_NAME = "Steam Frame"
ALLOWED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".mp4", ".webm", ".mkv"}
MAX_BYTES = 200 * 1024 * 1024
_SAFE_CHARS = re.compile(r"[^A-Za-z0-9._ -]")
_lock = threading.Lock()


def safe_name(raw):
    """Basename only, restricted charset, no leading dots."""
    name = os.path.basename((raw or "").replace("\\", "/"))
    name = _SAFE_CHARS.sub("_", name).lstrip(". ")
    return name[:150]


def store(dest_dir, raw_name, stream):
    """Write stream into dest_dir. Returns (status, name) with status
    'stored' | 'duplicate' | 'bad-name' | 'bad-type' | 'too-large'."""
    name = safe_name(raw_name)
    stem, ext = os.path.splitext(name)
    if not stem:
        return "bad-name", None
    if ext.lower() not in ALLOWED_EXTENSIONS:
        return "bad-type", None
    data = stream.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        return "too-large", None
    digest = hashlib.sha256(data).hexdigest()
    with _lock:
        os.makedirs(dest_dir, exist_ok=True)
        # Same bytes under any name already here -> duplicate. The hash is
        # kept in a sidecar-free way: name collisions get a hash suffix, so
        # compare against the candidate files with the same stem.
        candidate = name
        n = 0
        while os.path.exists(os.path.join(dest_dir, candidate)):
            with open(os.path.join(dest_dir, candidate), "rb") as fh:
                if hashlib.sha256(fh.read()).hexdigest() == digest:
                    return "duplicate", candidate
            n += 1
            candidate = f"{stem}_{digest[:8]}{'' if n == 1 else '_' + str(n)}{ext}"
        tmp = os.path.join(dest_dir, "." + candidate + ".part")
        with open(tmp, "wb") as fh:
            fh.write(data)
        os.replace(tmp, os.path.join(dest_dir, candidate))
    return "stored", candidate


def register(app, get_user, is_admin, downloads_dir, invalidate_caches):
    @app.route("/api/media/ingest", methods=["POST"])
    def media_ingest():
        user = get_user()
        if not user:
            return jsonify({"error": "Unauthorized"}), 401
        if not is_admin(user):
            return jsonify({"error": "Forbidden"}), 403
        upload = request.files.get("file")
        if upload is None:
            return jsonify({"error": "multipart field 'file' is required"}), 400
        status, name = store(os.path.join(downloads_dir, FOLDER_NAME),
                             request.form.get("name") or upload.filename, upload.stream)
        if status == "stored":
            invalidate_caches()
        if status in ("stored", "duplicate"):
            return jsonify({"status": status, "name": name}), 201 if status == "stored" else 200
        code = 413 if status == "too-large" else 400
        return jsonify({"error": status}), code
