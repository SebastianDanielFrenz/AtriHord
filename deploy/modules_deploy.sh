#!/bin/bash
# Gated deploy of the optional-module system (MODULE_SYSTEM_PLAN.md) to the media VM:
#   new: module_manager.py, atri_ed25519.py, modules/ (local source + signed index)
#   changed: web_files/js/settings.js (Settings -> Modules), server.py (import + 3 routes under /api/modules)
# server.py is built HERE from prod (CRLF kept) by inserting the import after "import genre_map" and the routes
# block before the "Settings page" banner, so whatever else prod's server.py holds stays as it is.
# Stage dir /tmp/modules-deploy holds: module_manager.py, atri_ed25519.py, settings.js, modules/,
# tests/server_checks/module_manager_check.py, routes.py (the block to insert, LF).
#   usage: modules_deploy.sh <prod server.py md5> <prod settings.js md5>
#   systemd-run --user --unit ksf-modules-deploy bash /tmp/modules-deploy/modules_deploy.sh <md5> <md5>
set -u
exec > /tmp/modules-deploy/deploy.log 2>&1
APP=/data2/ksf_web_dl
ST=/tmp/modules-deploy
PY="$APP/venv/bin/python"
BASE_SRV=$1; BASE_SET=$2
cd "$APP" || exit 1
[ "$(md5sum server.py | cut -d' ' -f1)" = "$BASE_SRV" ] || { echo "prod server.py changed - not deploying"; exit 1; }
[ "$(md5sum web_files/js/settings.js | cut -d' ' -f1)" = "$BASE_SET" ] || { echo "prod settings.js changed - not deploying"; exit 1; }
"$PY" - <<'PYEOF' || exit 1
s = open("/data2/ksf_web_dl/server.py", "rb").read()
crlf = b"\r\n" in s
nl = b"\r\n" if crlf else b"\n"
imp = b"import genre_map" + nl
assert s.count(imp) == 1, ("import anchor", s.count(imp))
s = s.replace(imp, imp + b"import module_manager" + nl)
banner = b"# -------------------------------------------------------------------" + nl + b"# Settings page (web_files/settings.html)"
assert s.count(banner) == 1, ("banner anchor", s.count(banner))
block = open("/tmp/modules-deploy/routes.py", "rb").read().replace(b"\r\n", b"\n")
if crlf:
    block = block.replace(b"\n", b"\r\n")
s = s.replace(banner, block + nl + banner)
assert s.count(b'@app.route("/api/modules"') == 1
open("/tmp/modules-deploy/server.py", "wb").write(s)
print("server.py built, CRLF" if crlf else "server.py built, LF")
PYEOF
"$PY" -m py_compile "$ST/server.py" "$ST/module_manager.py" "$ST/atri_ed25519.py" || { echo "does not compile"; exit 1; }
"$PY" "$ST/tests/server_checks/module_manager_check.py" > "$ST/check.txt" 2>&1 || { echo "module checks failed - not deploying"; cat "$ST/check.txt"; exit 1; }
cat "$ST/check.txt"
n=$(ps -eo args | grep -cE "[g]allery.dl|[y]t.dlp")
[ "$n" -eq 0 ] || { echo "downloads running ($n) - not deploying"; exit 1; }
cp -p server.py server.py.bak-modules && cp -p web_files/js/settings.js web_files/js/settings.js.bak-modules || exit 1
rollback() {
    cp -p server.py.bak-modules server.py.new && mv -f server.py.new server.py
    cp -p web_files/js/settings.js.bak-modules web_files/js/settings.js.new && mv -f web_files/js/settings.js.new web_files/js/settings.js
}
for f in module_manager.py atri_ed25519.py; do cp "$ST/$f" "$f.new" && chmod 644 "$f.new" && mv -f "$f.new" "$f" || exit 1; done
rm -rf modules.new && cp -r "$ST/modules" modules.new && rm -rf modules && mv modules.new modules || exit 1
cp "$ST/settings.js" web_files/js/settings.js.new && chmod 644 web_files/js/settings.js.new && mv -f web_files/js/settings.js.new web_files/js/settings.js || { rollback; exit 1; }
cp "$ST/server.py" server.py.new && chmod 644 server.py.new && mv -f server.py.new server.py || { rollback; exit 1; }
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
        urllib.request.urlopen("http://127.0.0.1:8000/api/modules", timeout=3); code = 200
    except urllib.error.HTTPError as e:
        code = e.code
    except Exception:
        code = None
    if pid not in ("0", old) and code in (200, 401):
        print("up: pid %s after ~%ds, /api/modules -> %s" % (pid, i, code)); sys.exit(0)
    time.sleep(1)
sys.exit(1)
PYEOF
then
    echo "NOT UP - rolling back"; rollback
    P=$(systemctl show -p MainPID --value ksf-web-dl.service); [ "$P" != "0" ] && kill -TERM "$P"
    exit 1
fi
echo "deployed"
