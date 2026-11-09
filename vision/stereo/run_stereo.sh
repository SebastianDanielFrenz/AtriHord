#!/bin/bash
# Pauses + unloads the vision worker, runs a stereo job (or any command), ALWAYS unpauses on exit.
#   run_stereo.sh job  run --work DIR --src FILE --kind video|picture [--profile JSON]
#   run_stereo.sh exec python stereo_check.py ...      (any command, e.g. the check script)
# Like bench/run_bench.sh.  Exit code = the command's.
D=/data/ksf-vision/stereo
KSF=$D/ksf
PYV=$D/.venv/bin/python
LOGD=$D/logs
mkdir -p "$LOGD"
API=http://localhost:8410
unpause() {
  curl -s -X POST -H 'Content-Type: application/json' -d '{"paused": false}' $API/v1/pause > "$LOGD/unpause_response.txt" 2>&1
  { echo "unpaused $(date)"; curl -s $API/v1/status; echo; } >> "$LOGD/run_stereo.log"
}
trap unpause EXIT
trap 'exit 143' TERM INT HUP
{ echo "== start $(date) :: $*"; echo "status before:"; curl -s $API/v1/status; echo; } >> "$LOGD/run_stereo.log"
curl -s -X POST -H 'Content-Type: application/json' -d '{"paused": true}' $API/v1/pause >> "$LOGD/run_stereo.log"; echo >> "$LOGD/run_stereo.log"
curl -s -X POST $API/v1/unload >> "$LOGD/run_stereo.log"; echo >> "$LOGD/run_stereo.log"
sleep 3
nvidia-smi --query-gpu=memory.used,memory.total --format=csv >> "$LOGD/run_stereo.log"
mode=$1; shift
case "$mode" in
  job)  "$PYV" "$KSF/stereo_job.py" "$@" & ;;
  exec) "$@" & ;;
  *) echo "usage: run_stereo.sh job|exec ARGS" >&2; exit 2 ;;
esac
child=$!
# forward TERM to the child so a SIGTERM to this script stops the job cleanly, then wait for it
trap 'kill -TERM $child 2>/dev/null; wait $child; exit $?' TERM INT HUP
wait $child
exit $?
