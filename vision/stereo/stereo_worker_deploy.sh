#!/bin/bash
# Deploys the vision worker with the 3D conversion API (/v1/stereo/*, vision_worker.py) and the driver's
# encoder-retry (stereo/ksf/stereo_job.py). Refuses unless prod still has the base and the staged files are
# the tested ones; runs BOTH check scripts on the staged copy first; pauses the worker so the indexer's batch
# in flight finishes, swaps, restarts the user unit, proves the new worker with a real 5 s clip end to end,
# and rolls back if anything fails. Pattern of vidinterp/deploy/worker_deploy.sh.
#
# Stage these in /tmp/stereo-deploy on the worker (scp): vision_worker.py stereo_job.py fake_stereo_driver.py
# stereo_worker_check.py vision_lock_check.py   (+ stereo_worker_deploy.sh itself)
#
#   systemd-run --user --unit ksf-stereo-worker-deploy \
#       bash /tmp/stereo-deploy/stereo_worker_deploy.sh <base worker md5> <new worker md5> <new stereo_job md5> [<base stereo_job md5>]
#   (log: /tmp/stereo-deploy/stereo_worker_deploy.log)
#
# Environment: DRY_RUN=1   only the md5/compile/check steps; touches nothing (no pause, no swap)
#              SKIP_CHECKS=1   skip the two check scripts (with DRY_RUN: just md5 + compile)
#              E2E_ONLY=1   only the end-to-end clip against the RUNNING worker (re-run after exit 2)
#              STAGE=...    staging dir (default /tmp/stereo-deploy)
#              BASE=... STEREO_WORK=...   point the e2e at a staged worker (tests only)
#
# Exit codes: 0 deployed and proven; 1 refused or rolled back; 2 deployed, but the end-to-end clip could not
# be judged because the GPU gate stayed closed (indexer/Smooth motion busy) - NOT rolled back, re-run with
# E2E_ONLY=1 when the card is quiet.
#
# Side effects: media_indexer.py reads the pause as a closed window and sleeps its closed_retry_s (10 min).
# While the e2e job runs, non-urgent analyze/audio answer 503 "3D conversion running" (same effect, ~1-2 min).
# P2b: a job WAITING behind an open gate now also makes non-urgent batches answer 503 "3D conversion waiting to
# start", and only URGENT analyze/audio hold the start gate closed, so the e2e job no longer starves behind the indexer.
#
# P2b release (priority: upscale > Smooth motion > 3D > indexing), md5s - nothing is hard-coded below, pass them:
#   base worker  0d5eae31ab311af347db4ec7a85261c2   (deployed 2026-10-04 22:22)
#   new worker   3e3fa8604d8e680953d1e87324f7b448
#   stereo_job   8037f70d7c570729866eca4fb009a493   (unchanged; pass it as <new stereo_job md5>, and as the base too)
#   bash /tmp/stereo-deploy/stereo_worker_deploy.sh 0d5eae31ab311af347db4ec7a85261c2 3e3fa8604d8e680953d1e87324f7b448 #        8037f70d7c570729866eca4fb009a493 8037f70d7c570729866eca4fb009a493
#   Stage the NEW vision_worker.py and stereo_worker_check.py (md5 0f4f0a5999ba06e6db1dc6296447aa1c) as usual.
set -u
STAGE=${STAGE:-/tmp/stereo-deploy}
LOG=${LOG:-$STAGE/stereo_worker_deploy.log}
exec > "$LOG" 2>&1
APP=/data/ksf-vision
KSF=$APP/stereo/ksf
PYV=$APP/.venv/bin/python
FFBIN=$APP/ffmpeg/ffmpeg-master-latest-linux64-gpl/bin
E2E_SRC=${E2E_SRC:-$APP/stereo/bench/src/b_live_trains1080.mkv}
E2E=$STAGE/e2e
E2E_GATE_WAIT=${E2E_GATE_WAIT:-480}      # s a waiting job may sit behind a closed gate before the e2e is "inconclusive"
E2E_RUN_WAIT=${E2E_RUN_WAIT:-600}        # s from start to done
BASE=${BASE:-http://127.0.0.1:8410}          # tests may point it at a staged worker on another port
STEREO_WORK=${STEREO_WORK:-$APP/stereo-work}
BASE_MD5=${1:-}
NEW_MD5=${2:-}
JOB_MD5=${3:-}
BASE_JOB_MD5=${4:-}
TOK=$(python3 -c "import json;print(json.load(open('$APP/vision_config.json')).get('token',''))")
api() { curl -s -m 30 -H "Authorization: Bearer $TOK" "$@"; }
st() { api "$BASE$1"; }
jfield() { python3 -c "import json,sys
try:
    d=json.load(sys.stdin)
    for k in sys.argv[1].split('.'):
        d=d[k]
    print(d)
except Exception:
    print('')" "$1"; }
unpause() { api -X POST -H "Content-Type: application/json" -d '{"paused": false}' $BASE/v1/pause; echo; }
echo "== $(date) stereo worker deploy: base=$BASE_MD5 new=$NEW_MD5 job=$JOB_MD5 dry=${DRY_RUN:-0} e2e_only=${E2E_ONLY:-0}"

# ---------------------------------------------------------------- end to end
E2E_KEY=
e2e_cleanup() { [ -n "$E2E_KEY" ] && api -X DELETE "$BASE/v1/stereo/jobs/$E2E_KEY" >/dev/null; }
e2e() {
    local clip=$E2E/clip.mp4 key code j s waited=0 elapsed=0 why
    rm -rf "$E2E"; mkdir -p "$E2E"
    $FFBIN/ffmpeg -v error -y -ss 0 -t 5 -i "$E2E_SRC" -vf scale=-2:540 -r 30 -an -c:v libx264 -crf 23 -pix_fmt yuv420p "$clip" \
        || { echo "e2e: cannot cut the 5 s clip from $E2E_SRC"; return 1; }
    key=$(python3 -c "import hashlib,time;print(hashlib.sha256(('deploy-e2e-%s' % time.time()).encode()).hexdigest()[:32])")
    E2E_KEY=$key
    s=$(st /v1/stereo/status)
    [ "$(echo "$s" | jfield available)" = "True" ] || { echo "e2e: /v1/stereo/status says not available: $s"; return 1; }
    echo "e2e: status ok: $s"
    code=$(api -m 300 -o /dev/null -w '%{http_code}' -T "$clip" "$BASE/v1/stereo/source/$key?ext=mp4")
    [ "$code" = "200" ] || { echo "e2e: upload answered $code"; return 1; }
    code=$(api -o "$E2E/post.json" -w '%{http_code}' -X POST -H "Content-Type: application/json" \
           -d "{\"key\":\"$key\",\"kind\":\"video\",\"label\":\"deploy-e2e\",\"profile\":{\"max_eye_long_side\":1920}}" "$BASE/v1/stereo/jobs")
    [ "$code" = "201" ] || { echo "e2e: submit answered $code: $(cat $E2E/post.json)"; api -X DELETE "$BASE/v1/stereo/jobs/$key"; return 1; }
    echo "e2e: job $key queued"
    while :; do
        j=$(st /v1/stereo/jobs/$key)
        s=$(echo "$j" | jfield state)
        echo "e2e: $(date +%T) state=$s $(echo "$j" | jfield progress.phase) chunk=$(echo "$j" | jfield progress.chunk)/$(echo "$j" | jfield progress.chunks)"
        case "$s" in
            done) break ;;
            failed|cancelled|http404) echo "e2e: job ended $s: $j"; api -X DELETE "$BASE/v1/stereo/jobs/$key" >/dev/null; return 1 ;;
            waiting_gpu) waited=$((waited + 3)) ;;
            *) waited=0 ;;
        esac
        if [ "$waited" -ge "$E2E_GATE_WAIT" ]; then
            why=$(st /v1/stereo/status | jfield gate.reason)
            echo "e2e: INCONCLUSIVE - the job waited ${waited}s behind a closed gate ($why); the worker itself is up"
            api -X DELETE "$BASE/v1/stereo/jobs/$key" >/dev/null
            return 2
        fi
        elapsed=$((elapsed + 3))
        if [ "$elapsed" -ge $((E2E_RUN_WAIT + E2E_GATE_WAIT)) ]; then echo "e2e: timed out in state $s: $j"; api -X DELETE "$BASE/v1/stereo/jobs/$key" >/dev/null; return 1; fi
        sleep 3
    done
    echo "e2e: done: $j"
    # a resumable pull: the first bytes of the SBS file, expecting 206 and an ftyp box
    api -D "$E2E/hdr.txt" -r 0-31 -o "$E2E/head.bin" "$BASE/v1/stereo/jobs/$key/result/sbs.mp4"
    head -1 "$E2E/hdr.txt" | grep -q " 206" || { echo "e2e: Range pull did not answer 206: $(cat $E2E/hdr.txt)"; api -X DELETE "$BASE/v1/stereo/jobs/$key" >/dev/null; return 1; }
    grep -qi "^content-range: bytes 0-31/" "$E2E/hdr.txt" || { echo "e2e: no Content-Range: $(cat $E2E/hdr.txt)"; return 1; }
    [ "$(dd if="$E2E/head.bin" bs=1 skip=4 count=4 2>/dev/null)" = "ftyp" ] || { echo "e2e: sbs.mp4 does not start with an ftyp box"; return 1; }
    api -o "$E2E/sbs.mp4" "$BASE/v1/stereo/jobs/$key/result/sbs.mp4"
    api -o "$E2E/depth.mp4" "$BASE/v1/stereo/jobs/$key/result/depth.mp4"
    local fr w h rate
    fr=$($FFBIN/ffprobe -v error -select_streams v:0 -show_entries stream=width,height,r_frame_rate -of csv=p=0 "$E2E/sbs.mp4")
    echo "e2e: sbs.mp4 width,height,r_frame_rate = $fr"
    w=$(echo "$fr" | cut -d, -f1); h=$(echo "$fr" | cut -d, -f2); rate=$(echo "$fr" | cut -d, -f3)
    [ "$rate" = "120/1" ] || { echo "e2e: sbs.mp4 is not 120 fps: $rate"; return 1; }
    [ "$w" -ge $((h * 3)) ] || { echo "e2e: sbs.mp4 ${w}x${h} is not side-by-side"; return 1; }
    [ "$($FFBIN/ffprobe -v error -select_streams v:0 -show_entries stream=pix_fmt -of csv=p=0 "$E2E/depth.mp4")" = "yuv420p10le" ] \
        || { echo "e2e: depth.mp4 is not 10 bit"; return 1; }
    n=$($FFBIN/ffprobe -v error -select_streams v:0 -count_packets -show_entries stream=nb_read_packets -of csv=p=0 "$E2E/sbs.mp4")
    [ "${n:-0}" -ge 590 ] && [ "${n:-0}" -le 610 ] || { echo "e2e: ${n:-?} frames for 5 s at 120 fps"; return 1; }
    api -X DELETE "$BASE/v1/stereo/jobs/$key"; echo
    [ "$(api -o /dev/null -w '%{http_code}' $BASE/v1/stereo/jobs/$key)" = "404" ] || { echo "e2e: job still there after DELETE"; return 1; }
    [ ! -e "$STEREO_WORK/$key" ] || { echo "e2e: work dir still there after DELETE"; return 1; }
    rm -rf "$E2E"
    echo "e2e: OK ($n frames, ${w}x${h})"
    return 0
}

if [ "${E2E_ONLY:-0}" = "1" ]; then
    e2e; rc=$?; [ $rc -ne 0 ] && e2e_cleanup; echo "e2e exit $rc"; exit $rc
fi

# ---------------------------------------------------------------- refuse unless it is the tested files
[ -n "$BASE_MD5" ] && [ -n "$NEW_MD5" ] && [ -n "$JOB_MD5" ] || { echo "usage: stereo_worker_deploy.sh <base worker md5> <new worker md5> <new stereo_job md5> [<base stereo_job md5>]"; exit 1; }
[ "$(md5sum $APP/vision_worker.py | cut -d' ' -f1)" = "$BASE_MD5" ] || { echo "prod worker changed since the base - not deploying"; exit 1; }
if [ -n "$BASE_JOB_MD5" ]; then
    [ "$(md5sum $KSF/stereo_job.py | cut -d' ' -f1)" = "$BASE_JOB_MD5" ] || { echo "prod stereo_job.py changed since the base - not deploying"; exit 1; }
fi
[ "$(md5sum $STAGE/vision_worker.py | cut -d' ' -f1)" = "$NEW_MD5" ] || { echo "staged worker is not the tested one"; exit 1; }
[ "$(md5sum $STAGE/stereo_job.py | cut -d' ' -f1)" = "$JOB_MD5" ] || { echo "staged stereo_job.py is not the tested one"; exit 1; }
export PYTHONDONTWRITEBYTECODE=1
$PYV -m py_compile $STAGE/vision_worker.py || { echo "worker does not compile"; exit 1; }
$APP/stereo/.venv/bin/python -m py_compile $STAGE/stereo_job.py || { echo "stereo_job.py does not compile"; exit 1; }
[ -x $FFBIN/ffmpeg ] && [ -f "$E2E_SRC" ] || { echo "no ffmpeg or e2e source clip ($E2E_SRC)"; exit 1; }
echo "md5s and compile ok"
if [ "${SKIP_CHECKS:-0}" != "1" ]; then
    # Both run against the STAGED worker in the worker venv, with a fake driver: no GPU, no service involved.
    (cd $STAGE && VISION_DIR=$APP $PYV vision_lock_check.py vision_worker.py) || { echo "vision_lock_check FAILED on the staged worker - not deploying"; exit 1; }
    (cd $STAGE && VISION_DIR=$APP $PYV stereo_worker_check.py vision_worker.py stereo_job.py) || { echo "stereo_worker_check FAILED on the staged worker - not deploying"; exit 1; }
    echo "both checks passed"
fi
if [ "${DRY_RUN:-0}" = "1" ]; then echo "DRY RUN: everything before the pause is fine; nothing was touched"; exit 0; fi

# ---------------------------------------------------------------- pause, wait idle, swap
# The indexer sends a batch every few seconds while it works through a backlog, so the worker is never idle on
# its own. Pause it (analyze answers 503 without counting as a request; a running 3D job is stopped by the
# pause), let the batch in flight finish, then restart; the new process starts unpaused.
api -X POST -H "Content-Type: application/json" -d '{"paused": true}' $BASE/v1/pause; echo
for i in $(seq 1 60); do
    idle=$(st /v1/status | python3 -c "import json,sys; print(json.load(sys.stdin).get('idle_for_s',0))" 2>/dev/null || echo 0)
    [ "${idle:-0}" -ge 10 ] && break
    sleep 2
done
echo "idle ${idle}s after pausing"
[ "${idle:-0}" -ge 10 ] || { echo "batch did not finish in 2 min - not deploying"; unpause; exit 1; }
# A 3D job of a previous deploy (the status route is new: absent on the first deploy) must have left the card.
for i in $(seq 1 15); do
    r=$(st /v1/stereo/status | jfield running)
    [ -z "$r" ] || [ "$r" = "None" ] && break
    sleep 2
done

cp -p $APP/vision_worker.py $APP/vision_worker.py.bak-stereo || { unpause; exit 1; }
cp -p $KSF/stereo_job.py $KSF/stereo_job.py.bak-stereo || { unpause; exit 1; }
rollback() {
    echo "ROLLING BACK"
    cp -p $APP/vision_worker.py.bak-stereo $APP/vision_worker.py.new && mv -f $APP/vision_worker.py.new $APP/vision_worker.py
    cp -p $KSF/stereo_job.py.bak-stereo $KSF/stereo_job.py.new && mv -f $KSF/stereo_job.py.new $KSF/stereo_job.py
    systemctl --user restart ksf-vision.service
    for i in $(seq 1 60); do
        case "$(st /v1/interp/status)" in *available*) echo "rolled back, up after ~${i}s"; return ;; esac
        sleep 1
    done
    echo "ROLLED BACK BUT THE OLD WORKER DOES NOT ANSWER - look at: systemctl --user status ksf-vision"
}
# the driver first (a file nobody runs yet), then the worker
cp $STAGE/stereo_job.py $KSF/stereo_job.py.new && mv -f $KSF/stereo_job.py.new $KSF/stereo_job.py || { echo "cannot install stereo_job.py"; rollback; exit 1; }
cp $STAGE/vision_worker.py $APP/vision_worker.py.new && mv -f $APP/vision_worker.py.new $APP/vision_worker.py || { echo "cannot install the worker"; rollback; exit 1; }
systemctl --user restart ksf-vision.service
up=0
for i in $(seq 1 60); do
    s=$(st "/v1/interp/status")
    case "$s" in *available*) up=$i; echo "interp status up after ~${i}s: $s"; break ;; esac
    sleep 1
done
[ "$up" -gt 0 ] || { echo "NOT UP"; rollback; exit 1; }
s=$(st /v1/stereo/status)
case "$s" in *'"available"'*) echo "stereo status up: $s" ;; *) echo "/v1/stereo/status does not answer: $s"; rollback; exit 1 ;; esac
[ "$(echo "$s" | jfield available)" = "True" ] || { echo "stereo says it is not available: $s"; rollback; exit 1; }
# the other existing routes the media-dl side uses
[ "$(st /v1/status | jfield paused)" = "False" ] || { echo "the new worker came up paused"; rollback; exit 1; }

# ---------------------------------------------------------------- end to end on the new worker
e2e; rc=$?
[ $rc -ne 0 ] && e2e_cleanup
case $rc in
    0) echo "DEPLOYED and proven end to end"; exit 0 ;;
    2) echo "DEPLOYED; end-to-end clip inconclusive (gate closed). Re-run later: E2E_ONLY=1 bash $0"; exit 2 ;;
    *) echo "end-to-end clip FAILED"; rollback; exit 1 ;;
esac
