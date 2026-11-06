#!/usr/bin/env bash
# Interpolate a video file (or URL) to <multiplier> x its frame rate.
#
#   vidinterp-run.sh -i INPUT -o OUTPUT [options]
#
#   -i INPUT          file or http(s) URL (ffmpeg seeks with Range requests)
#   -o OUTPUT         .mp4/.mkv file, or "-" for fragmented MP4 on stdout (streaming)
#   -m N              multiplier (default 4)
#   -e lsfg|fruc      engine: LSFG 3.1 via vidinterp (default) or ffmpeg fruc_vulkan
#                     (fruc needs an FFmpeg master build via FFMPEG=; the installed 8.1 lacks it)
#   -s SECONDS        start position
#   -t SECONDS        duration
#   --max-height H    downscale above this height before interpolating (default: none)
#   --flow F          lsfg flow scale 0.25-1.0 (default 0.75)
#   --perf            lsfg performance mode
#   --dedup           lsfg: collapse held frames (animation on twos/threes)
#   --scene T         scene-cut threshold on the ffmpeg scdet scale, 0 = off (default 10)
#   --fruc-perf P     fruc: slow|medium|fast (default fast)
#   --fruc-grid G     fruc: 0 (auto) |1|2|4 (default 4; 8 fails on the RTX 5060)
#   --encoder E       auto|nvenc|x264|svtav1 (default auto: nvenc if the driver has it)
#   --quality Q       encoder quality: nvenc cq / x264 crf / svtav1 crf (default 23/18/30)
#
# This is the pipe version (raw frames through the CPU), kept for debugging and low
# resolutions. For production use bin/vidinterp-gpu.
# Environment: VIDINTERP_HOME (default: the install prefix above bin/), LSFGVK_DLL_PATH.
set -euo pipefail

HOME_DIR=${VIDINTERP_HOME:-$(cd "$(dirname "$0")/.." && pwd)}  # installed as <prefix>/bin/
FF=${FFMPEG:-$HOME_DIR/ffmpeg/bin/ffmpeg}
FP=${FFPROBE:-$HOME_DIR/ffmpeg/bin/ffprobe}
VI=${VIDINTERP:-$HOME_DIR/bin/vidinterp}
export LSFGVK_DLL_PATH=${LSFGVK_DLL_PATH:-$HOME_DIR/lsfg-vk.dll}

IN= OUT= M=4 ENGINE=lsfg SS= DUR= MAXH=0 FLOW=0.75 PERF= DEDUP= SCENE=10
FRUC_PERF=fast FRUC_GRID=4 ENC=auto Q=
while (( $# )); do
    case $1 in
        -i) IN=$2; shift ;;
        -o) OUT=$2; shift ;;
        -m) M=$2; shift ;;
        -e) ENGINE=$2; shift ;;
        -s) SS=$2; shift ;;
        -t) DUR=$2; shift ;;
        --max-height) MAXH=$2; shift ;;
        --flow) FLOW=$2; shift ;;
        --perf) PERF=1 ;;
        --dedup) DEDUP=1 ;;
        --scene) SCENE=$2; shift ;;
        --fruc-perf) FRUC_PERF=$2; shift ;;
        --fruc-grid) FRUC_GRID=$2; shift ;;
        --encoder) ENC=$2; shift ;;
        --quality) Q=$2; shift ;;
        -h|--help) sed -n '2,/^# Environment/p' "$0"; exit 0 ;;
        *) echo "unknown option $1" >&2; exit 2 ;;
    esac
    shift
done
[[ -n $IN && -n $OUT ]] || { sed -n '2,/^# Environment/p' "$0" >&2; exit 2; }

# ---- probe the source
probe() { "$FP" -v error -select_streams v:0 -show_entries "stream=$1" -of default=nw=1:nk=1 "$IN" | head -1; }
W=$(probe width); H=$(probe height); RATE=$(probe r_frame_rate)
MATRIX=$(probe color_space); TRC=$(probe color_transfer); PIXFMT=$(probe pix_fmt)
[[ $MATRIX == unknown || -z $MATRIX ]] && MATRIX=$([[ $H -ge 720 ]] && echo bt709 || echo smpte170m)
case $TRC in smpte2084|arib-std-b67) HDR=1 ;; *) HDR= ;; esac
AUDIO=$("$FP" -v error -select_streams a:0 -show_entries stream=codec_name -of default=nw=1:nk=1 "$IN" | head -1 || true)

NUM=${RATE%/*}; DEN=${RATE#*/}; [[ $DEN == "$RATE" ]] && DEN=1
OUT_RATE="$((NUM * M))/$DEN"
if (( MAXH > 0 && H > MAXH )); then
    OW=$(( (W * MAXH / H + 1) / 2 * 2 )); OH=$MAXH
else
    OW=$W; OH=$H
fi
echo "vidinterp-run: ${W}x${H} @ $RATE ($PIXFMT, $MATRIX${HDR:+, HDR}) -> ${OW}x${OH} @ $OUT_RATE, engine $ENGINE" >&2

# ---- encoder
if [[ $ENC == auto ]]; then
    if ldconfig -p 2>/dev/null | grep -c libnvidia-encode.so.1 >/dev/null; then ENC=nvenc; else ENC=x264; fi
fi
TAGS=(-colorspace "$MATRIX" -color_primaries "$([[ $HDR ]] && echo bt2020 || echo bt709)"
      -color_trc "$([[ $HDR ]] && echo "$TRC" || echo bt709)" -color_range tv)
case $ENC in
    nvenc)  VENC=(-c:v hevc_nvenc -preset p5 -tune hq -rc vbr -cq "${Q:-23}" -b:v 0 -tag:v hvc1
                  -pix_fmt "$([[ $HDR ]] && echo p010le || echo yuv420p)") ;;
    x264)   VENC=(-c:v libx264 -preset veryfast -crf "${Q:-18}" -pix_fmt yuv420p) ;;
    svtav1) VENC=(-c:v libsvtav1 -preset 8 -crf "${Q:-30}"
                  -pix_fmt "$([[ $HDR ]] && echo yuv420p10le || echo yuv420p)") ;;
    *) echo "unknown encoder $ENC" >&2; exit 2 ;;
esac
case $AUDIO in
    aac|mp3|opus|flac) AENC=(-c:a copy) ;;
    "") AENC=() ;;
    *) AENC=(-c:a aac -b:a 192k) ;;
esac
if [[ $OUT == - ]]; then
    MUX=(-f mp4 -movflags frag_keyframe+empty_moov+default_base_moof -)
else
    MUX=(-movflags +faststart "$OUT")
    [[ $OUT == *.mkv ]] && MUX=("$OUT")
fi
SEEK=(); [[ -n $SS ]] && SEEK=(-ss "$SS")
LIMIT=(); [[ -n $DUR ]] && LIMIT=(-t "$DUR")
AMAP=(); [[ -n $AUDIO ]] && AMAP=(-map "1:a:0?")

if [[ $ENGINE == fruc ]]; then
    "$FF" -hide_banner -filters 2>/dev/null | grep -c fruc_vulkan >/dev/null \
        || { echo "this ffmpeg has no fruc_vulkan (set FFMPEG= to a master build)" >&2; exit 1; }
    VF="fruc_vulkan=fps=$OUT_RATE:perf=$FRUC_PERF:grid=$FRUC_GRID"
    (( OW != W )) && VF="scale_vulkan=w=$OW:h=$OH,$VF"
    exec "$FF" -nostdin -hide_banner -loglevel error -stats \
        -init_hw_device vulkan=vk:0 -filter_hw_device vk -hwaccel vulkan -hwaccel_output_format vulkan \
        "${SEEK[@]}" "${LIMIT[@]}" -i "$IN" \
        "${SEEK[@]}" "${LIMIT[@]}" -i "$IN" \
        -map 0:v:0 "${AMAP[@]}" -vf "$VF,hwdownload,format=nv12" \
        "${VENC[@]}" "${TAGS[@]}" "${AENC[@]}" "${MUX[@]}"
fi

[[ $ENGINE == lsfg ]] || { echo "unknown engine $ENGINE" >&2; exit 2; }
[[ -f $LSFGVK_DLL_PATH ]] || { echo "lsfg-vk.dll not found at $LSFGVK_DLL_PATH" >&2; exit 1; }

# 10-bit sources keep 10 bits through LSFG (A2B10G10R10); everything else is RGBA8.
if [[ $HDR || $PIXFMT == *10* ]]; then RAW=x2bgr10le; DEPTH=10; else RAW=rgba; DEPTH=8; fi
VI_ARGS=(-W "$OW" -H "$OH" -m "$M" -f "$FLOW" --depth "$DEPTH" --scene "$SCENE")
[[ $PERF ]] && VI_ARGS+=(-p)
[[ $DEDUP ]] && VI_ARGS+=(--dedup)

# Constant frame rate into vidinterp, because it counts frames, not timestamps.
DEC_VF="fps=$RATE,scale=w=$OW:h=$OH:flags=bicubic:in_color_matrix=$MATRIX:in_range=auto,format=$RAW"
ENC_VF="scale=out_color_matrix=$MATRIX:out_range=tv"

set +e
"$FF" -nostdin -hide_banner -loglevel error -threads 0 -hwaccel auto \
        "${SEEK[@]}" "${LIMIT[@]}" -i "$IN" -map 0:v:0 -vf "$DEC_VF" -f rawvideo - \
    | "$VI" "${VI_ARGS[@]}" \
    | "$FF" -hide_banner -loglevel error -stats \
        -f rawvideo -pix_fmt "$RAW" -s "${OW}x${OH}" -framerate "$OUT_RATE" -i - \
        "${SEEK[@]}" "${LIMIT[@]}" -i "$IN" \
        -map 0:v "${AMAP[@]}" -vf "$ENC_VF" "${VENC[@]}" "${TAGS[@]}" "${AENC[@]}" "${MUX[@]}"
rc=("${PIPESTATUS[@]}")
set -e
for i in 0 1 2; do
    if (( rc[i] != 0 )); then echo "vidinterp-run: stage $i failed (exit ${rc[i]})" >&2; exit 1; fi
done
