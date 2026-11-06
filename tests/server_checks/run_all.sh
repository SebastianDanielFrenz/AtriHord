#!/bin/bash
# Runs every server harness against a staged server.py, on the media VM.
#
#   bash run_all.sh <stage dir>
#
# <stage dir> holds the server.py to test, plus the modules the harnesses import
# from next to it (media_ai.py, media_faces.py, media_indexer.py, genre_map.py -
# copied from production when they are not part of the change). The harnesses load functions out of the
# staged server.py's AST and fake everything outside it, so nothing here touches
# the live library, the vision worker or the running service.
#
# live_check.py is not run: it is a read-only diagnostic on the REAL library,
# for when something looks wrong in production (see its docstring).
# vision_lock_check.py is not run either: it tests vision_worker.py, on the
# vision host, and would import the staged server.py as if it were the worker.
set -u
STAGE=$(realpath "$1")
APP=${ATRIHORD_DIR:?set ATRIHORD_DIR to the app folder}
HERE=$(cd "$(dirname "$0")" && pwd)
for m in media_ai.py media_faces.py media_indexer.py genre_map.py youtube_music_extractor.py recent_media_store.py music_library.py music_listened.py \
         music_loudness.py song_covers.py post_stitch.py song_merge.py image_dedup.py; do
    [ -f "$STAGE/$m" ] || cp -p "$APP/$m" "$STAGE/$m"
done
fail=0
for check in "$HERE"/*_check.py; do
    name=$(basename "$check")
    [ "$name" = live_check.py ] && continue
    [ "$name" = vision_lock_check.py ] && continue
    # writeguard.py refuses every write into $APP and turns it into a FAIL line:
    # a harness once rewrote the live config.json through a resolved CONFIG.
    out=$(cd "$APP" && LIVE_DIR="$APP" timeout 600 "$APP/venv/bin/python" "$HERE/writeguard.py" "$check" "$STAGE/server.py" 2>&1)
    printf '%-28s %3d pass, %d fail\n' "$name" "$(grep -c '^PASS' <<<"$out")" "$(grep -c '^FAIL' <<<"$out")"
    if ! grep -q '^ALL PASS' <<<"$out" || grep -q '^FAIL' <<<"$out"; then
        grep '^FAIL' <<<"$out" | head -5 | sed 's/^/    /'
        tail -3 <<<"$out" | sed 's/^/    /'
        fail=1
    fi
done
exit $fail
