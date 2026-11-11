#!/bin/bash
# Gated deploy of stereo3d.py (redo_entries / redo_spool) + the one-line spool hook in server.py's
# _stereo3d_loop to the media VM. Stage dir /tmp/redo-deploy holds: stereo3d.py (new), trim_redo_check.py,
# stereo3d_check.py. server.py is built HERE from prod (CRLF kept) by inserting the hook after "svc.step()".
#   usage: redo_spool_deploy.sh <prod server.py md5> <prod stereo3d.py md5> <new stereo3d.py md5>
#   systemd-run --user --unit ksf-redo-deploy bash /tmp/redo-deploy/redo_spool_deploy.sh ...
set -u
exec > /tmp/redo-deploy/deploy.log 2>&1
APP=/data2/ksf_web_dl
ST=/tmp/redo-deploy
PY="$APP/venv/bin/python"
BASE_SRV=$1; BASE_S3D=$2; NEW_S3D=$3
cd "$APP" || exit 1
[ "$(md5sum server.py | cut -d' ' -f1)" = "$BASE_SRV" ] || { echo "prod server.py changed - not deploying"; exit 1; }
[ "$(md5sum stereo3d.py | cut -d' ' -f1)" = "$BASE_S3D" ] || { echo "prod stereo3d.py changed - not deploying"; exit 1; }
[ "$(md5sum "$ST/stereo3d.py" | cut -d' ' -f1)" = "$NEW_S3D" ] || { echo "staged stereo3d.py is not the tested one"; exit 1; }
"$PY" - <<'PYEOF' || exit 1
import re
s = open("/data2/ksf_web_dl/server.py", "rb").read()
old = b"            svc.step()\r\n            delay = 10 if svc.busy() else 60\r\n"
assert s.count(old) == 1, s.count(old)
new = (b"            svc.step()\r\n"
       b"            svc.redo_spool(os.path.join(BASE_DIR, \"stereo3d_redo_spool.json\"))\r\n"
       b"            delay = 10 if svc.busy() else 60\r\n")
open("/tmp/redo-deploy/server.py", "wb").write(s.replace(old, new))
PYEOF
"$PY" -m py_compile "$ST/server.py" "$ST/stereo3d.py" || { echo "does not compile"; exit 1; }
for m in "$APP"/*.py; do b=$(basename "$m"); [ -e "$ST/$b" ] || cp -p "$m" "$ST/$b"; done   # the modules server.py imports
for check in "$ST"/trim_redo_check.py "$ST"/stereo3d_check.py; do
    name=$(basename "$check")
    "$PY" "$check" "$ST/server.py" > "$ST/pre_$name.txt" 2>&1
    echo "--- $name: $(grep -cE '^PASS' "$ST/pre_$name.txt") pass, $(grep -cE '^FAIL' "$ST/pre_$name.txt") fail"
    # the VM has no /cache3d: "cache folder /cache3d is refused" fails the same on unchanged prod code (2026-11-11)
    bad=$(grep -E '^FAIL' "$ST/pre_$name.txt" | grep -vc "cache folder /cache3d is refused")
    [ "$bad" -eq 0 ] && grep -q "^ALL PASS\|^1 FAILED" "$ST/pre_$name.txt" || { echo "$name failed - not deploying"; tail -15 "$ST/pre_$name.txt"; exit 1; }
done
n=$(ps -eo args | grep -cE "[g]allery.dl|[y]t.dlp")
[ "$n" -eq 0 ] || { echo "downloads running ($n) - not deploying"; exit 1; }
for f in server.py stereo3d.py; do cp -p "$f" "$f.bak-redo" || exit 1; done
rollback() { for f in server.py stereo3d.py; do cp -p "$f.bak-redo" "$f.new" && mv -f "$f.new" "$f"; done; }
for f in stereo3d.py server.py; do
    cp "$ST/$f" "$f.new" && chmod 644 "$f.new" && mv -f "$f.new" "$f" || { rollback; exit 1; }
done
OLD=$(systemctl show -p MainPID --value ksf-web-dl.service)
echo "old pid $OLD"
kill -TERM "$OLD" || { rollback; exit 1; }
if ! python3 - "$OLD" <<'PYEOF'
import subprocess, sys, time, urllib.error, urllib.request
old = sys.argv[1]
for i in range(120):
    pid = subprocess.run(["systemctl", "show", "-p", "MainPID", "--value", "ksf-web-dl.service"],
                         capture_output=True, text=True).stdout.strip()
    try:
        urllib.request.urlopen("http://127.0.0.1:8000/api/media/interp/config", timeout=3); code = 200
    except urllib.error.HTTPError as e:
        code = e.code
    except Exception:
        code = None
    if pid not in ("0", old) and code in (200, 401):
        print("up: pid %s after ~%ds, config -> %s" % (pid, i, code)); sys.exit(0)
    time.sleep(1)
sys.exit(1)
PYEOF
then
    echo "NOT UP - rolling back"; rollback
    P=$(systemctl show -p MainPID --value ksf-web-dl.service); [ "$P" != "0" ] && kill -TERM "$P"
    exit 1
fi
echo "deployed"
