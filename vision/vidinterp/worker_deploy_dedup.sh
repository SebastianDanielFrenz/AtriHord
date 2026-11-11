#!/bin/bash
# Deploys vidinterp-gpu --dedup (held-frame interpolation) and the worker that passes it:
#   vision_worker.py   interp_config() reads interp_dedup / interp_max_hold / interp_dup_threshold and
#                      adds --dedup --max-hold N --dup-threshold T to the stream and the segment command
#   vidinterp-gpu      built from vidinterp_gpu.cpp (install.sh's own g++ line)
# Refuses unless prod still has both base files and the staged ones are the tested ones; checks the staged
# binary on a synthetic animation-on-threes clip (it must hold 2 of 3 frames and still make exactly 10x the
# frames); pauses the worker, waits for the batch in flight and for any smooth-motion stream to end, swaps,
# restarts the user unit, proves the new worker end to end (upload the clip, ask for segments through
# /v1/interp/seg, check the process got --dedup and the output is smooth), and rolls back if anything fails.
# Pattern of stereo/stereo_worker_deploy.sh.
#
# Stage in /tmp/vi-dedup-deploy on the worker (scp): vision_worker.py vidinterp-gpu worker_deploy_dedup.sh
#
#   systemd-run --user --unit ksf-vi-dedup-deploy \
#       bash /tmp/vi-dedup-deploy/worker_deploy_dedup.sh <base worker md5> <new worker md5> <base bin md5> <new bin md5>
#   (log: /tmp/vi-dedup-deploy/deploy.log)
#
# Environment: DRY_RUN=1  only the md5/compile/smoke steps; touches nothing (no pause, no swap)
set -u
STAGE=${STAGE:-/tmp/vi-dedup-deploy}
LOG=${LOG:-$STAGE/deploy.log}
exec > "$LOG" 2>&1
APP=/data/ksf-vision
VI=$APP/vidinterp
BIN=$VI/bin/vidinterp-gpu
DLL=$VI/lsfg-vk.dll
FF=$VI/ffmpeg/bin
PYV=$APP/.venv/bin/python
BASE=${BASE:-http://127.0.0.1:8410}
BASE_W=${1:-}; NEW_W=${2:-}; BASE_B=${3:-}; NEW_B=${4:-}
TOK=$(python3 -c "import json;print(json.load(open('$APP/vision_config.json')).get('token',''))")
CACHE=$(python3 -c "import json;print(json.load(open('$APP/vision_config.json')).get('interp_cache_dir') or '$APP/interp-cache')")
api() { curl -s -m 30 -H "Authorization: Bearer $TOK" "$@"; }
st() { api "$BASE$1"; }
unpause() { api -X POST -H "Content-Type: application/json" -d '{"paused": false}' $BASE/v1/pause; echo; }
echo "== $(date) vidinterp dedup deploy: worker $BASE_W -> $NEW_W, bin $BASE_B -> $NEW_B, dry=${DRY_RUN:-0}"

[ -n "$BASE_W" ] && [ -n "$NEW_W" ] && [ -n "$BASE_B" ] && [ -n "$NEW_B" ] || { echo "usage: <base worker md5> <new worker md5> <base bin md5> <new bin md5>"; exit 1; }
[ "$(md5sum $APP/vision_worker.py | cut -d' ' -f1)" = "$BASE_W" ] || { echo "prod worker changed since the base - not deploying"; exit 1; }
[ "$(md5sum $BIN | cut -d' ' -f1)" = "$BASE_B" ] || { echo "prod vidinterp-gpu changed since the base - not deploying"; exit 1; }
[ "$(md5sum $STAGE/vision_worker.py | cut -d' ' -f1)" = "$NEW_W" ] || { echo "staged worker is not the tested one"; exit 1; }
[ "$(md5sum $STAGE/vidinterp-gpu | cut -d' ' -f1)" = "$NEW_B" ] || { echo "staged vidinterp-gpu is not the tested one"; exit 1; }
export PYTHONDONTWRITEBYTECODE=1
$PYV -m py_compile $STAGE/vision_worker.py || { echo "worker does not compile"; exit 1; }
echo "md5s and compile ok"

# ---------------------------------------------------------------- the staged binary on a clip with held frames
CLIP=$STAGE/on3.mp4
if [ ! -f $CLIP ]; then
    # 8 animation steps a second on a 24 fps video = every picture shown for three frames; 4 s, 36 px a step
    $FF/ffmpeg -nostdin -v error -y -f lavfi -i "color=gray:s=2400x720:r=8:d=4,noise=alls=90:allf=u,gblur=sigma=1.5,crop=1280:720:x='36*n':y=0,format=yuv420p" \
        -vf fps=24 -c:v libx264 -crf 20 -pix_fmt yuv420p $CLIP || { echo "cannot make the test clip"; exit 1; }
fi
# mean/sd of the difference between consecutive frames of a file (scdet scale), and how many are identical
motion() { $FF/ffmpeg -nostdin -v error -i "$1" -vf "scale=480:-2,scdet=threshold=100,metadata=print:file=-" -f null - 2>&1 \
    | grep mafd | sed 's/.*mafd=//' | awk 'NR>1{ if($1<0.02) z++; s+=$1; ss+=$1*$1; c++ } END{ m=s/c; printf "%d %.4f %.4f", c+1, z/c, sqrt(ss/c-m*m) }'; }
smoke() {  # <name> <extra args...>: prints "frames identical-share sd" of the output
    local name=$1; shift
    $STAGE/vidinterp-gpu -i $CLIP -o $STAGE/smoke_$name.mp4 --fps 240 -d $DLL "$@" 2> $STAGE/smoke_$name.log || { echo "smoke $name: vidinterp-gpu failed: $(tail -3 $STAGE/smoke_$name.log)"; return 1; }
    motion $STAGE/smoke_$name.mp4
}
read -r nf idn sd <<< "$(smoke dedup --dedup)"
echo "smoke --dedup:    frames=$nf identical-share=$idn sd=$sd   $(tail -1 $STAGE/smoke_dedup.log)"
read -r nf0 idn0 sd0 <<< "$(smoke plain)"
echo "smoke (no flag):  frames=$nf0 identical-share=$idn0 sd=$sd0   $(tail -1 $STAGE/smoke_plain.log)"
[ "${nf:-0}" = "960" ] && [ "${nf0:-0}" = "960" ] || { echo "smoke: expected 960 frames (24 -> 240 fps, 4 s)"; exit 1; }
python3 - "$idn" "$sd" "$idn0" "$sd0" <<'PY' || { echo "smoke: the held frames are not interpolated (or the plain run changed)"; exit 1; }
import sys
idn, sd, idn0, sd0 = map(float, sys.argv[1:5])
# --dedup: motion is even (few identical frames, small spread); without it the old behaviour (about half identical)
assert idn < 0.1 and sd < 0.25, ("dedup output is not smooth", idn, sd)
assert idn0 > 0.3 and sd0 > 0.4, ("plain output changed", idn0, sd0)
PY
echo "smoke ok"
if [ "${DRY_RUN:-0}" = "1" ]; then echo "DRY RUN: everything before the pause is fine; nothing was touched"; exit 0; fi

# ---------------------------------------------------------------- pause, wait idle, swap
api -X POST -H "Content-Type: application/json" -d '{"paused": true}' $BASE/v1/pause; echo
for i in $(seq 1 60); do
    idle=$(st /v1/status | python3 -c "import json,sys; print(json.load(sys.stdin).get('idle_for_s',0))" 2>/dev/null || echo 0)
    [ "${idle:-0}" -ge 10 ] && break
    sleep 2
done
echo "idle ${idle}s after pausing"
[ "${idle:-0}" -ge 10 ] || { echo "batch did not finish in 2 min - not deploying"; unpause; exit 1; }
# someone watching with Smooth motion: the restart would cut their stream - wait for it to end
for i in $(seq 1 60); do pgrep -f "$BIN" >/dev/null || break; sleep 2; done
pgrep -f "$BIN" >/dev/null && { echo "a smooth-motion stream is still running after 2 min - not deploying"; unpause; exit 1; }

cp -p $APP/vision_worker.py $APP/vision_worker.py.bak-dedup || { unpause; exit 1; }
cp -p $BIN $BIN.bak-dedup || { unpause; exit 1; }
rollback() {
    echo "ROLLING BACK"
    cp -p $APP/vision_worker.py.bak-dedup $APP/vision_worker.py.new && mv -f $APP/vision_worker.py.new $APP/vision_worker.py
    cp -p $BIN.bak-dedup $BIN.new && mv -f $BIN.new $BIN
    systemctl --user restart ksf-vision.service
    for i in $(seq 1 60); do
        case "$(st /v1/interp/status)" in *available*) echo "rolled back, up after ~${i}s"; return ;; esac
        sleep 1
    done
    echo "ROLLED BACK BUT THE OLD WORKER DOES NOT ANSWER - look at: systemctl --user status ksf-vision"
}
# the binary first (a flag nobody passes yet), then the worker that passes it
cp $STAGE/vidinterp-gpu $BIN.new && chmod 755 $BIN.new && mv -f $BIN.new $BIN || { echo "cannot install the binary"; rollback; exit 1; }
cp $STAGE/vision_worker.py $APP/vision_worker.py.new && mv -f $APP/vision_worker.py.new $APP/vision_worker.py || { echo "cannot install the worker"; rollback; exit 1; }
systemctl --user restart ksf-vision.service
up=0
for i in $(seq 1 60); do
    s=$(st "/v1/interp/status")
    case "$s" in *available*) up=$i; echo "interp status up after ~${i}s: $s"; break ;; esac
    sleep 1
done
[ "$up" -gt 0 ] || { echo "NOT UP"; rollback; exit 1; }
echo "$s" | grep -q '"available": *true' || { echo "interp says it is not available: $s"; rollback; exit 1; }
case "$(st /v1/status)" in *'"paused": false'*|*'"paused":false'*) ;; *) echo "the new worker came up paused"; rollback; exit 1 ;; esac

# ---------------------------------------------------------------- end to end through the worker's own segment route
KEY=$(python3 -c "import hashlib,time;print(hashlib.sha256(('dedup-e2e-%s' % time.time()).encode()).hexdigest()[:32])")
VARIANT=f240_h720_r0
e2e_cleanup() { rm -rf "$CACHE/$KEY" "$CACHE/$KEY.src"; }
e2e() {
    local code
    code=$(api -m 120 -o /dev/null -w '%{http_code}' -T "$CLIP" "$BASE/v1/interp/source/$KEY")
    [ "$code" = "200" ] || { echo "e2e: upload answered $code"; return 1; }
    for i in $(seq 1 90); do                                  # 503 "still being made" is asked again
        code=$(api -o $STAGE/seg0.m4s -w '%{http_code}' "$BASE/v1/interp/seg/$KEY/$VARIANT/seg_00000.m4s?from=0")
        [ "$code" = "200" ] && break
        sleep 1
    done
    [ "$code" = "200" ] || { echo "e2e: segment 0 answered $code after ~90 s: $(cat $STAGE/seg0.m4s | head -c 200)"; return 1; }
    for i in $(seq 1 60); do                                  # the job runs on through the file
        [ -f "$CACHE/$KEY/$VARIANT/end.json" ] && break
        sleep 1
    done
    [ -f "$CACHE/$KEY/$VARIANT/end.json" ] || { echo "e2e: the run did not reach the end of the clip"; return 1; }
    # the command line is gone once the run ended; the log says how many held frames it found (0 without --dedup)
    grep -Eq "held [1-9]" "$CACHE/$KEY/$VARIANT/job.log" || { echo "e2e: job.log has no 'held' count - the worker did not pass --dedup: $(tail -3 $CACHE/$KEY/$VARIANT/job.log)"; return 1; }
    local d=$CACHE/$KEY/$VARIANT
    cat $d/init.mp4 $d/seg_*.m4s > $STAGE/e2e.mp4
    read -r nf idn sd <<< "$(motion $STAGE/e2e.mp4)"
    echo "e2e: segments ($(ls $d/seg_*.m4s | wc -l)) frames=$nf identical-share=$idn sd=$sd"
    [ "$nf" = "960" ] || { echo "e2e: expected 960 frames"; return 1; }
    python3 -c "import sys; assert float('$idn') < 0.1 and float('$sd') < 0.25" || { echo "e2e: segment output is not smooth"; return 1; }
    return 0
}
e2e; rc=$?
e2e_cleanup
[ $rc -eq 0 ] || { echo "end-to-end FAILED"; rollback; exit 1; }
echo "DEPLOYED and proven end to end"
exit 0
