#!/usr/bin/env bash
# Build the lsfg-vk v2 pipeline library (plus helper, config and CLI) with plain g++.
# Needs no cmake: the four static targets are small and have no build-time codegen
# (the only shader, Mipmaps, ships precompiled as Mipmaps.h).
#
# usage: build_lsfg.sh <lsfg-vk source dir> <build dir>
set -euo pipefail

SRC=$(realpath "$1")
OUT=$(realpath -m "$2")
CXX=${CXX:-g++}
JOBS=${JOBS:-$(nproc)}
mkdir -p "$OUT/obj"

LSFGVK_VERSION=${LSFGVK_VERSION:-2.0.0-unknown}
CXXFLAGS=(-std=c++20 -O2 -fPIC -fvisibility=hidden -DLSFGVK_MANAGED=0 -DNDEBUG
    "-DLSFGVK_VERSION=\"$LSFGVK_VERSION\""
    -isystem "$SRC/thirdparty"
    -I "$SRC/lsfg-vk-helper/include" -I "$SRC/lsfg-vk-pipeline/include"
    -I "$SRC/lsfg-vk-config/include" -isystem "$SRC/lsfg-vk-pipeline/shaders"
    -fconstexpr-ops-limit=4290000000)

compile() {  # <source.cpp> -> obj path on stdout
    local src=$1 obj="$OUT/obj/$(echo "${1#$SRC/}" | tr '/' '_').o"
    if [[ ! -f $obj || $src -nt $obj ]]; then
        "$CXX" "${CXXFLAGS[@]}" -c "$src" -o "$obj"
    fi
}
export -f compile; export SRC OUT CXX

SOURCES=(
    "$SRC/lsfg-vk-helper/src/vkhelper.cpp"
    "$SRC/lsfg-vk-pipeline/src/library/dll.cpp"
    "$SRC/lsfg-vk-pipeline/src/library.cpp"
    "$SRC/lsfg-vk-pipeline/src/pipelines/v3_1.cpp"
    "$SRC/lsfg-vk-pipeline/src/pipeline.cpp"
    "$SRC/lsfg-vk-pipeline/src/lsfgvk.cpp"
    "$SRC/lsfg-vk-config/src/config.cpp"
)
CLI_SOURCES=("$SRC"/lsfg-vk-cli/src/*.cpp)

pids=()
for s in "${SOURCES[@]}" "${CLI_SOURCES[@]}"; do
    while (( $(jobs -rp | wc -l) >= JOBS )); do wait -n; done
    compile "$s" &
    pids+=($!)
done
for p in "${pids[@]}"; do wait "$p"; done

obj() { echo "$OUT/obj/$(echo "${1#$SRC/}" | tr '/' '_').o"; }

rm -f "$OUT/liblsfg-vk-helper.a" "$OUT/liblsfg-vk-pipeline.a" "$OUT/liblsfg-vk-config.a"
ar rcs "$OUT/liblsfg-vk-helper.a" "$(obj "${SOURCES[0]}")"
ar rcs "$OUT/liblsfg-vk-pipeline.a" $(for s in "${SOURCES[@]:1:5}"; do obj "$s"; done)
ar rcs "$OUT/liblsfg-vk-config.a" "$(obj "${SOURCES[6]}")"

"$CXX" -o "$OUT/lsfg-vk-cli" $(for s in "${CLI_SOURCES[@]}"; do obj "$s"; done) \
    "$OUT/liblsfg-vk-pipeline.a" "$OUT/liblsfg-vk-config.a" "$OUT/liblsfg-vk-helper.a" \
    -ldl -lpthread

echo "built: $OUT/lsfg-vk-cli and static libs in $OUT"
