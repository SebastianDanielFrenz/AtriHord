#!/usr/bin/env bash
# LSFG throughput on this GPU: lsfg-vk-cli benchmark over resolution, multiplier,
# flow scale and performance mode, plus the peak VRAM rise of each run.
# "base fps" is the highest source frame rate the setting can keep up with.
#
#   bench.sh [seconds per run, default 5]
set -uo pipefail
HOME_DIR=${VIDINTERP_HOME:-$(cd "$(dirname "$0")/.." && pwd)}
CLI=$HOME_DIR/bin/lsfg-vk-cli
DLL=${LSFGVK_DLL_PATH:-$HOME_DIR/lsfg-vk.dll}
T=${1:-5}

mem() { nvidia-smi --query-gpu=memory.used --format=csv,noheader,nounits | head -1; }

printf '%-10s %4s %5s %5s %10s %10s %9s\n' size mult flow perf base_fps out_fps vram_MiB
for size in 1920x1080 3840x2160; do
    for m in 2 4 5 6; do
        for flow in 0.5 0.75 1.0; do
            for perf in no yes; do
                args=(benchmark -d "$DLL" -w "${size%x*}" -h "${size#*x}" -m "$m" -f "$flow" -t "$T")
                [[ $perf == yes ]] && args+=(-p)
                base=$(mem); peak=$base
                "$CLI" "${args[@]}" > /tmp/lsfg-bench.$$ 2>&1 &
                pid=$!
                while kill -0 "$pid" 2>/dev/null; do
                    m2=$(mem); (( m2 > peak )) && peak=$m2
                    sleep 0.5
                done
                wait "$pid"
                bfps=$(grep -oP 'Base FPS\s*:\s*\K[0-9.]+' /tmp/lsfg-bench.$$ || echo err)
                ofps=$(grep -oP 'Output FPS\s*:\s*\K[0-9.]+' /tmp/lsfg-bench.$$ || echo err)
                printf '%-10s %4s %5s %5s %10s %10s %9s\n' "$size" "$m" "$flow" "$perf" "$bfps" "$ofps" "$((peak - base))"
            done
        done
    done
done
rm -f /tmp/lsfg-bench.$$
