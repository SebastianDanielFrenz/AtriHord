#!/bin/bash
# P2 pre-flight (run on the worker by hand, not part of the deploy): the STAGED worker on port 8499 with its
# own work dir and the REAL stereo driver/venv/vidinterp, driven by stereo_worker_deploy.sh's e2e function
# (5 s clip -> done -> Range pull -> ffprobe -> DELETE). The real service is paused for the duration (trap ->
# always unpaused) so the card is quiet, and is otherwise not touched.
#   bash stage_real_e2e.sh            (STAGE defaults to /data/ksf-vision/stage-stereo)
set -u
STAGE=${STAGE:-/data/ksf-vision/stage-stereo}
APP=/data/ksf-vision
PORT=8499
WORK=$STAGE/work-real
rm -rf "$WORK"; mkdir -p "$WORK"
cat > $STAGE/real_config.json <<EOF
{"token": "", "windows": [], "reserve_mb": 1100, "idle_unload_s": 900,
 "stereo_work_dir": "$WORK", "stereo_min_free_gb": 20, "stereo_idle_s": 5, "stereo_interp_idle_s": 5}
EOF
unpause() { curl -s -m 5 -X POST -H 'Content-Type: application/json' -d '{"paused": false}' http://127.0.0.1:8410/v1/pause >/dev/null; }
cleanup() { [ -n "${WPID:-}" ] && kill -TERM $WPID 2>/dev/null; sleep 1; pkill -TERM -f "stereo_job.py run --work $WORK" 2>/dev/null; unpause; echo "cleanup done (real worker unpaused)"; }
trap cleanup EXIT
trap 'exit 143' TERM INT HUP
curl -s -m 5 -X POST -H 'Content-Type: application/json' -d '{"paused": true}' http://127.0.0.1:8410/v1/pause; echo
curl -s -m 30 -X POST http://127.0.0.1:8410/v1/unload; echo
sleep 3
cd $STAGE
# the same environment the unit gives the real worker (the driver must not inherit it)
LD=$(systemctl --user show ksf-vision.service -p Environment | tr ' ' '\n' | grep '^LD_LIBRARY_PATH=' | head -1 | cut -d= -f2-)
env KSF_VISION_PORT=$PORT KSF_VISION_CONFIG=$STAGE/real_config.json HF_HOME=$APP/hf INSIGHTFACE_HOME=$APP/insightface \
    LD_LIBRARY_PATH="$LD" PYTHONDONTWRITEBYTECODE=1 \
    $APP/.venv/bin/python $STAGE/vision_worker.py > $STAGE/real_worker.log 2>&1 &
WPID=$!
for i in $(seq 1 30); do
    curl -s -m 2 http://127.0.0.1:$PORT/v1/stereo/status | grep -q available && break
    sleep 1
done
curl -s -m 5 http://127.0.0.1:$PORT/v1/stereo/status; echo
BASE=http://127.0.0.1:$PORT STEREO_WORK=$WORK STAGE=$STAGE LOG=$STAGE/real_e2e.log E2E_ONLY=1 E2E_GATE_WAIT=120 \
    bash $STAGE/stereo_worker_deploy.sh
rc=$?
echo "e2e rc=$rc"
cat $STAGE/real_e2e.log
echo "--- staged worker log"
tail -20 $STAGE/real_worker.log
exit $rc
