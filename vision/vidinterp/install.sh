#!/usr/bin/env bash
# Install vidinterp (video frame interpolation with Lossless Scaling frame generation)
# on the vision host. Main tool: bin/vidinterp-gpu (all-GPU). bin/vidinterp +
# bin/vidinterp-run.sh are the older pipe version, kept for debugging.
#
#   bash install.sh [--prefix DIR] [--dll PATH] [--skip-system] [--force]
#
#   --prefix DIR     install location (default /data/ksf-vision/vidinterp)
#   --dll PATH       copy this lsfg-vk.dll into the prefix. It comes from a licensed
#                    Lossless Scaling install switched to the Steam beta branch "lsfg-vk"
#                    (Properties > Betas); the default branch does not contain it.
#   --skip-system    do not install apt packages (only check and report them)
#   --lsfg-src DIR   use this lsfg-vk checkout (must be commit LSFG_COMMIT) instead of
#                    cloning; for hosts without git or access to git.lsfg-vk.dev
#   --force          rebuild and re-download everything
#
# Idempotent: every step checks its result first and is skipped when already done.
# Needs sudo only for the apt packages; run it with a terminal (ssh -t) so sudo can ask.
#
# lsfg-vk is PATCHED here (lsfg-vk-*.patch, applied after the checkout): see the note at
# LSFG_COMMIT before updating it.
set -euo pipefail

PREFIX=/data/ksf-vision/vidinterp
DLL= SKIP_SYSTEM= FORCE= LSFG_SRC=
while (( $# )); do
    case $1 in
        --prefix) PREFIX=$2; shift ;;
        --dll) DLL=$2; shift ;;
        --skip-system) SKIP_SYSTEM=1 ;;
        --force) FORCE=1 ;;
        --lsfg-src) LSFG_SRC=$(realpath "$2"); shift ;;
        -h|--help) sed -n '2,21p' "$0"; exit 0 ;;
        *) echo "unknown option $1" >&2; exit 2 ;;
    esac
    shift
done

HERE=$(cd "$(dirname "$0")" && pwd)

# ---- pinned upstream versions (bump deliberately, together with the checksums)
# FFmpeg 8.1, not master: master's NVENC needs API 13.1 = driver >= 610, the host has 595
# (API 13.0). 8.1 has everything vidinterp-gpu uses (Vulkan decode, libplacebo, scdet_vulkan).
FFMPEG_BASE=https://github.com/BtbN/FFmpeg-Builds/releases/download/autobuild-2026-09-18-13-22
FFMPEG_STATIC=ffmpeg-n8.1.2-54-gc573a95381-linux64-gpl-8.1.tar.xz
FFMPEG_STATIC_SHA256=ade3787b873684e1428ab39d29c5850c8b4bd349c79b47ad206eb51f479e3db4
FFMPEG_SHARED=ffmpeg-n8.1.2-54-gc573a95381-linux64-gpl-shared-8.1.tar.xz
FFMPEG_SHARED_SHA256=07fd8e8578b0e4119a252043399bd877c3068c21589fbc33ab2a5cceaf2706e5
LSFG_GIT=https://git.lsfg-vk.dev/lsfg-vk.git
# WE PATCH lsfg-vk: every lsfg-vk-*.patch next to this script is applied to this commit
# (step 3b; lsfg-vk-explicit-timestamp.patch = Context::acquireAt, which vidinterp-gpu
# --fps needs). Bumping the commit can break them: the install stops if one no longer
# applies - port it (each patch file says what it does and why), never just drop it.
LSFG_COMMIT=fd8c317159ff5cc363cffa0f4d1e9568ceadf9dc
LSFG_VERSION=2.0.0.gfd8c317

step() { printf '\n==> %s\n' "$*"; }

# ---- 1. system packages
step "system packages"
DRIVER=$(cat /sys/module/nvidia/version 2>/dev/null || true)
[[ -n $DRIVER ]] || { echo "NVIDIA kernel driver not loaded" >&2; exit 1; }
BRANCH=${DRIVER%%.*}
PKGS=(git curl xz-utils g++ patch libvulkan1 libegl1 "libnvidia-encode-$BRANCH" "libnvidia-decode-$BRANCH")
MISSING=()
for p in "${PKGS[@]}"; do
    dpkg-query -W -f='${Status}' "$p" 2>/dev/null | grep -c "install ok installed" >/dev/null || MISSING+=("$p")
done
if (( ${#MISSING[@]} )); then
    echo "missing: ${MISSING[*]}"
    if [[ $SKIP_SYSTEM ]]; then
        echo "--skip-system given; install them with: sudo apt-get install -y ${MISSING[*]}"
    else
        sudo apt-get update -qq
        sudo apt-get install -y --no-install-recommends "${MISSING[@]}"
    fi
else
    echo "all present (driver $DRIVER)"
fi
command -v g++ >/dev/null || { echo "g++ is required" >&2; exit 1; }

mkdir -p "$PREFIX"/{bin,src,build,dl}

# ---- 2. FFmpeg: static build (command-line tools) and shared build (libraries + headers
#         for vidinterp-gpu), both pinned by sha256
step "ffmpeg"
fetch_ffmpeg() {  # <tarball name> <sha256> <target dir>
    local name=$1 sha=$2 dir=$3 tarball=$PREFIX/dl/$1
    if [[ -z $FORCE && -f $dir/.sha256 && $(cat "$dir/.sha256") == "$sha" ]]; then return; fi
    if [[ ! -f $tarball ]] || ! echo "$sha  $tarball" | sha256sum -c --quiet; then
        curl -fL --retry 3 -o "$tarball.part" "$FFMPEG_BASE/$name"
        mv "$tarball.part" "$tarball"
    fi
    echo "$sha  $tarball" | sha256sum -c --quiet
    rm -rf "$dir.new"
    mkdir -p "$dir.new"
    tar -xJf "$tarball" -C "$dir.new" --strip-components=1
    rm -rf "$dir"
    mv "$dir.new" "$dir"
    echo "$sha" > "$dir/.sha256"
}
fetch_ffmpeg "$FFMPEG_STATIC" "$FFMPEG_STATIC_SHA256" "$PREFIX/ffmpeg"
fetch_ffmpeg "$FFMPEG_SHARED" "$FFMPEG_SHARED_SHA256" "$PREFIX/ffmpeg-dev"
for f in libplacebo scdet_vulkan hwupload; do
    "$PREFIX/ffmpeg/bin/ffmpeg" -hide_banner -filters 2>/dev/null | grep -c " $f " >/dev/null \
        || { echo "ffmpeg build lacks the $f filter" >&2; exit 1; }
done
"$PREFIX/ffmpeg/bin/ffmpeg" -hide_banner -version | head -1

# ---- 3. lsfg-vk source at the pinned commit
step "lsfg-vk source"
SRC=$PREFIX/src/lsfg-vk
if [[ -n $LSFG_SRC ]]; then
    # Offline source: copy it and remember which commit it stands for.
    if [[ $FORCE || $(cat "$SRC/.commit" 2>/dev/null) != "$LSFG_COMMIT" ]]; then
        rm -rf "$SRC"
        cp -a "$LSFG_SRC" "$SRC"
        echo "$LSFG_COMMIT" > "$SRC/.commit"
    fi
    echo "using $LSFG_SRC as $LSFG_COMMIT"
else
    command -v git >/dev/null || { echo "git is required (or pass --lsfg-src)" >&2; exit 1; }
    if [[ ! -d $SRC/.git ]]; then
        rm -rf "$SRC"
        git init -q "$SRC"
        git -C "$SRC" remote add origin "$LSFG_GIT"
    fi
    if [[ $FORCE || $(git -C "$SRC" rev-parse HEAD 2>/dev/null) != "$LSFG_COMMIT" ]]; then
        git -C "$SRC" fetch -q --depth 1 origin "$LSFG_COMMIT"
        git -C "$SRC" checkout -q --force FETCH_HEAD
    fi
    git -C "$SRC" log -1 --format='%h %ci %s'
fi

# ---- 3b. our patches to lsfg-vk (NOT upstream; see the note at LSFG_COMMIT)
step "lsfg-vk patches"
for p in "$HERE"/lsfg-vk-*.patch; do
    [[ -f $p ]] || continue
    if patch -d "$SRC" -p1 -R -f -s --dry-run < "$p" >/dev/null 2>&1; then
        echo "already applied: $(basename "$p")"
    elif patch -d "$SRC" -p1 -N -f -s --dry-run < "$p" >/dev/null 2>&1; then
        patch -d "$SRC" -p1 -N -f -s < "$p"
        echo "applied: $(basename "$p")"
    else
        echo "$(basename "$p") does not apply to lsfg-vk $LSFG_COMMIT - port it before building" >&2
        exit 1
    fi
done

# ---- 4. build lsfg-vk pipeline + cli, then vidinterp
step "build"
[[ $FORCE ]] && rm -rf "$PREFIX/build"
LSFGVK_VERSION=$LSFG_VERSION bash "$HERE/build_lsfg.sh" "$SRC" "$PREFIX/build/lsfg"
g++ -std=c++20 -O2 -Wall \
    -isystem "$SRC/thirdparty" -I "$SRC/lsfg-vk-pipeline/include" \
    "$HERE/vidinterp.cpp" -o "$PREFIX/build/vidinterp" \
    "$PREFIX/build/lsfg/liblsfg-vk-pipeline.a" "$PREFIX/build/lsfg/liblsfg-vk-helper.a" \
    -l:libvulkan.so.1 -ldl -lpthread
# RPATH (not RUNPATH), so FFmpeg's own library dependencies resolve from ffmpeg-dev too.
g++ -std=c++20 -O2 -Wall \
    -isystem "$SRC/thirdparty" -I "$SRC/lsfg-vk-pipeline/include" -isystem "$PREFIX/ffmpeg-dev/include" \
    "$HERE/vidinterp_gpu.cpp" -o "$PREFIX/build/vidinterp-gpu" \
    "$PREFIX/build/lsfg/liblsfg-vk-pipeline.a" "$PREFIX/build/lsfg/liblsfg-vk-helper.a" \
    -L"$PREFIX/ffmpeg-dev/lib" -Wl,--disable-new-dtags,-rpath,"$PREFIX/ffmpeg-dev/lib" \
    -lavformat -lavcodec -lavfilter -lavutil -l:libvulkan.so.1 -ldl -lpthread
install -m 755 "$PREFIX/build/vidinterp" "$PREFIX/build/vidinterp-gpu" "$PREFIX/build/lsfg/lsfg-vk-cli" "$PREFIX/bin/"
install -m 755 "$HERE/vidinterp-run.sh" "$PREFIX/bin/"
install -m 755 "$HERE/bench.sh" "$PREFIX/bin/"
echo "installed: $(ls "$PREFIX/bin" | tr '\n' ' ')"

# ---- 5. lsfg-vk.dll
step "lsfg-vk.dll"
if [[ -n $DLL ]]; then
    install -m 644 "$DLL" "$PREFIX/lsfg-vk.dll"
fi
if [[ -f $PREFIX/lsfg-vk.dll ]]; then
    echo "present ($(sha256sum "$PREFIX/lsfg-vk.dll" | cut -c1-16)...)"
else
    echo "NOT present: the lsfg engine will not run until you copy lsfg-vk.dll from a"
    echo "Lossless Scaling install to $PREFIX/lsfg-vk.dll (or rerun with --dll PATH)."
    echo "The fruc engine works without it."
fi

# ---- 6. self-check
step "self-check"
if "$PREFIX/ffmpeg/bin/ffmpeg" -hide_banner -loglevel error -f lavfi -i testsrc2=s=1280x720:r=30:d=1 \
        -c:v av1_nvenc -f null - 2>/dev/null; then
    echo "NVENC (av1_nvenc): ok"
else
    echo "NVENC: NOT usable - vidinterp-gpu falls back to libx264, far too slow for 4K"
fi
"$PREFIX/ffmpeg/bin/ffmpeg" -hide_banner -loglevel error -init_hw_device vulkan=vk:0 \
    -f lavfi -i testsrc2=s=320x240:r=24:d=1 -f null - && echo "Vulkan in ffmpeg: ok"
if [[ -f $PREFIX/lsfg-vk.dll ]]; then
    "$PREFIX/bin/lsfg-vk-cli" benchmark -d "$PREFIX/lsfg-vk.dll" -w 1920 -h 1080 -m 2 -t 3 2>&1 \
        | grep -E "Throughput|error" || true
fi
echo
echo "done. usage: LSFGVK_DLL_PATH=$PREFIX/lsfg-vk.dll $PREFIX/bin/vidinterp-gpu -i INPUT -o OUTPUT|- -m 2"
