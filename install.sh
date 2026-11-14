#!/usr/bin/env bash
# AtriHord installer: the README's quick-start steps, then the optional modules (MODULE_SYSTEM_PLAN.md).
# Safe to run again: it only adds what is missing and (re)installs the modules you pick.
#
#   ./install.sh                         interactive: asks which optional modules to install
#   ./install.sh --modules visualizer    unattended: exactly these (comma-separated), or "all" / "none" / "default"
#   ./install.sh --no-apt                skip the apt packages (python3-venv, ffmpeg, git)
#
# Modules can be added or removed later in Settings -> Server -> Modules, or with module_manager.py.
set -euo pipefail
cd "$(dirname "$0")"

MODULES="ask"
APT=1
while [ $# -gt 0 ]; do
  case "$1" in
    --modules) MODULES="${2:?--modules needs a list, all, none or default}"; shift 2 ;;
    --modules=*) MODULES="${1#*=}"; shift ;;
    --no-apt) APT=0; shift ;;
    -h|--help) sed -n '2,10p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done

say() { printf '\n== %s\n' "$*"; }

if [ "$APT" = 1 ] && command -v apt-get >/dev/null 2>&1; then
  missing=()
  dpkg -s python3-venv >/dev/null 2>&1 || missing+=(python3-venv)
  command -v ffmpeg >/dev/null 2>&1 || missing+=(ffmpeg)
  command -v git >/dev/null 2>&1 || missing+=(git)
  if [ ${#missing[@]} -gt 0 ]; then
    say "Installing system packages: ${missing[*]}"
    sudo apt-get install -y "${missing[@]}"
  fi
fi

say "Python environment (venv/)"
[ -x venv/bin/python ] || python3 -m venv venv
venv/bin/pip install --quiet -r requirements.txt
PY=venv/bin/python

# ---- optional modules ---------------------------------------------------------------------------------------------
# one line per module in modules/: id <TAB> default (1/0) <TAB> name <TAB> description
mapfile -t ROWS < <("$PY" - <<'EOF'
import module_manager as mm
for mid, m in mm.local_sources().items():
    if "error" in m:
        continue
    print("\t".join([mid, "1" if m.get("install_default") else "0", m["name"], m["description"].replace("\t", " ")]))
EOF
)

PICK=()
if [ ${#ROWS[@]} -eq 0 ]; then
  say "No optional modules in this checkout"
elif [ "$MODULES" = "ask" ] && [ ! -t 0 ]; then
  MODULES="default"                       # no terminal to ask on: the pre-ticked ones
fi

if [ ${#ROWS[@]} -gt 0 ]; then
  case "$MODULES" in
    all)     for r in "${ROWS[@]}"; do PICK+=("${r%%$'\t'*}"); done ;;
    none)    ;;
    default) for r in "${ROWS[@]}"; do IFS=$'\t' read -r id def _ _ <<<"$r"; [ "$def" = 1 ] && PICK+=("$id"); done ;;
    ask)
      if command -v whiptail >/dev/null 2>&1; then
        items=()
        for r in "${ROWS[@]}"; do IFS=$'\t' read -r id def name desc <<<"$r"; items+=("$id" "$name - $desc" "$([ "$def" = 1 ] && echo ON || echo OFF)"); done
        if out=$(whiptail --title "AtriHord" --separate-output --checklist \
              "Optional modules (space toggles, enter confirms).\nYou can change this later in Settings -> Modules." \
              20 100 10 "${items[@]}" 3>&1 1>&2 2>&3); then
          mapfile -t PICK <<<"$out"
        fi
      else
        say "Optional modules (you can change this later in Settings -> Modules)"
        for r in "${ROWS[@]}"; do
          IFS=$'\t' read -r id def name desc <<<"$r"
          hint=$([ "$def" = 1 ] && echo "Y/n" || echo "y/N")
          read -r -p "  $name - $desc [$hint] " ans </dev/tty || ans=""
          ans=${ans:-$([ "$def" = 1 ] && echo y || echo n)}
          case "$ans" in [yY]*) PICK+=("$id") ;; esac
        done
      fi ;;
    *)       IFS=',' read -r -a PICK <<<"$MODULES" ;;
  esac
  CLEAN=()
  for id in "${PICK[@]}"; do [ -n "$id" ] && CLEAN+=("$id"); done   # whiptail can leave an empty line
  PICK=("${CLEAN[@]+"${CLEAN[@]}"}")
  if [ ${#PICK[@]} -gt 0 ]; then
    say "Installing modules: ${PICK[*]}"
    "$PY" module_manager.py install "${PICK[@]}"
  else
    say "No optional modules installed"
  fi
fi

say "Done"
cat <<'EOF'
Start a private trial (no login, localhost only):
    MEDIA_SINGLE_USER=me venv/bin/python server.py      ->  http://127.0.0.1:8000/
For a real service with sign-in, Pixiv and the GPU vision worker see README.md.
EOF
