#!/usr/bin/env bash
# =============================================================================
#  ContextOS installer for macOS and Linux
#
#  One line:
#    curl -fsSL https://raw.githubusercontent.com/nilanshpratapanand/ContextManager-Context-OS/main/install.sh | bash
#  Or download this file and run:   bash install.sh
#
#  Options:   -y            accept every default, never prompt
#             --dir PATH    install somewhere other than ~/ContextOS
#             --no-launch   don't offer to start ContextOS at the end
#  Env:       CONTEXTOS_DIR   same as --dir
#             CONTEXTOS_REPO  git URL or local path to install from (forks)
#
#  Re-running it updates an existing install. Your .env keys and chats are kept.
# =============================================================================
set -euo pipefail

REPO_URL="${CONTEXTOS_REPO:-https://github.com/nilanshpratapanand/ContextManager-Context-OS}"
TARBALL="https://github.com/nilanshpratapanand/ContextManager-Context-OS/archive/refs/heads/main.tar.gz"
DIR="${CONTEXTOS_DIR:-$HOME/ContextOS}"
YES=0
LAUNCH=1

while [ $# -gt 0 ]; do
  case "$1" in
    -y|--yes)    YES=1 ;;
    --dir)       DIR="$2"; shift ;;
    --no-launch) LAUNCH=0 ;;
    -h|--help)   sed -n '2,17p' "$0"; exit 0 ;;
    *) echo "Unknown option: $1" >&2; exit 2 ;;
  esac
  shift
done

if [ -t 1 ]; then B=$'\e[1m'; G=$'\e[32m'; Y=$'\e[33m'; R=$'\e[31m'; D=$'\e[2m'; N=$'\e[0m'
else B=""; G=""; Y=""; R=""; D=""; N=""; fi
step() { printf '\n%s==> %s%s\n' "$B" "$1" "$N"; }
ok()   { printf '    %s✓%s %s\n' "$G" "$N" "$1"; }
warn() { printf '    %s!%s %s\n' "$Y" "$N" "$1"; }
die()  { printf '\n%sError:%s %s\n' "$R" "$N" "$1" >&2; exit 1; }

# Prompts read from the terminal even when this script arrives through a pipe.
ask() {  # ask "Question?" default(y|n)
  local q="$1" def="$2" ans=""
  if [ "$YES" = 1 ]; then [ "$def" = y ]; return; fi
  if [ -r /dev/tty ]; then
    printf '    %s [%s] ' "$q" "$([ "$def" = y ] && echo Y/n || echo y/N)" > /dev/tty
    read -r ans < /dev/tty || ans=""
  fi
  ans="${ans:-$def}"
  case "$ans" in [Yy]*) return 0 ;; *) return 1 ;; esac
}

printf '%s\n' "${B}ContextOS installer${N}  ${D}context stays, models are disposable${N}"
printf '%s\n' "${D}Installing to: $DIR${N}"

# ------------------------------------------------------------------- python
step "Checking for Python 3.9 or newer"
find_python() {
  for c in python3 python; do
    if command -v "$c" >/dev/null 2>&1 &&
       "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' 2>/dev/null; then
      echo "$c"; return 0
    fi
  done
  return 1
}

install_python() {
  if [ "$(uname)" = "Darwin" ]; then
    command -v brew >/dev/null 2>&1 || die "Install Homebrew (https://brew.sh) or Python from https://www.python.org/downloads/ and run this again."
    brew install python
  elif command -v apt-get >/dev/null 2>&1; then
    sudo apt-get update -y && sudo apt-get install -y python3 python3-venv python3-pip
  elif command -v dnf >/dev/null 2>&1; then
    sudo dnf install -y python3 python3-pip
  elif command -v pacman >/dev/null 2>&1; then
    sudo pacman -S --noconfirm python python-pip
  elif command -v zypper >/dev/null 2>&1; then
    sudo zypper install -y python3 python3-pip
  else
    die "Couldn't find a package manager. Install Python 3.9+ from https://www.python.org/downloads/ and run this again."
  fi
}

PY="$(find_python || true)"
if [ -z "$PY" ]; then
  warn "Python 3.9+ is not installed."
  ask "Install it now with your system package manager?" y || die "Python 3.9+ is required."
  install_python
  PY="$(find_python || true)"
  [ -n "$PY" ] || die "Python still not found after installing. Open a new terminal and run this again."
fi
ok "$("$PY" --version 2>&1) ($(command -v "$PY"))"

# ---------------------------------------------------------------- download
step "Getting ContextOS"
mkdir -p "$DIR"
if command -v git >/dev/null 2>&1; then
  if [ -d "$DIR/.git" ]; then
    git -C "$DIR" pull --ff-only --quiet && ok "Updated the existing copy (git pull)"
  elif [ -z "$(ls -A "$DIR" 2>/dev/null)" ]; then
    git clone --depth 1 --quiet "$REPO_URL" "$DIR" && ok "Downloaded with git"
  else
    TMP="$(mktemp -d)"
    git clone --depth 1 --quiet "$REPO_URL" "$TMP/src"
    rm -rf "$TMP/src/.git"
    cp -R "$TMP/src/." "$DIR/" && rm -rf "$TMP"
    ok "Updated the files in $DIR (your .env and chats are untouched)"
  fi
else
  if command -v curl >/dev/null 2>&1; then curl -fsSL "$TARBALL" | tar -xz -C "$DIR" --strip-components=1
  elif command -v wget >/dev/null 2>&1; then wget -qO- "$TARBALL" | tar -xz -C "$DIR" --strip-components=1
  else die "Need git, curl or wget to download ContextOS."
  fi
  ok "Downloaded the source archive"
fi
[ -f "$DIR/contextos/server.py" ] || die "The download looks incomplete: $DIR/contextos/server.py is missing."
cd "$DIR"
chmod +x run.sh install.sh 2>/dev/null || true

# ------------------------------------------------------------ environment
step "Setting up a private Python environment (.venv)"
if [ ! -x .venv/bin/python ] && [ ! -x .venv/Scripts/python.exe ]; then
  if ! "$PY" -m venv .venv 2>/dev/null; then
    rm -rf .venv
    warn "Python's venv module is missing (common on Debian/Ubuntu)."
    if command -v apt-get >/dev/null 2>&1 && ask "Install python3-venv with apt?" y; then
      sudo apt-get install -y python3-venv && "$PY" -m venv .venv
    else
      die "Install python3-venv (or your distro's equivalent) and run this again."
    fi
  fi
fi
VPY=".venv/bin/python"; [ -x "$VPY" ] || VPY=".venv/Scripts/python.exe"
ok "Environment ready: $DIR/.venv"

step "Installing requirements"
"$VPY" -m pip install --upgrade --quiet pip >/dev/null 2>&1 || true
if "$VPY" -m pip install --quiet -r requirements.txt; then
  ok "Installed requirements.txt"
else
  warn "Optional packages didn't install. ContextOS still works (token counts are estimated)."
fi

# -------------------------------------------------------------- settings
step "Settings"
if [ -f .env ]; then
  ok ".env already exists - your API keys were kept"
else
  cp .env.example .env
  ok "Created .env from .env.example - add at least one free API key to it"
fi

# ------------------------------------------------------------- self-test
step "Running the self-test"
if "$VPY" tests/test_contextos.py >/tmp/contextos-test.log 2>&1; then
  ok "$(tail -n 1 /tmp/contextos-test.log)"
else
  warn "Some tests failed - see /tmp/contextos-test.log. The app may still run."
fi

# ------------------------------------------------------------------ done
cat <<EOF

${G}${B}ContextOS is installed.${N}

  Folder:      $DIR
  Start it:    ${B}$DIR/run.sh${N}                (opens http://127.0.0.1:8000)
  No keys yet: ${B}$DIR/run.sh --offline${N}      (simulated replies)
  API keys:    in the app, click the key icon (bottom left) - or edit ${B}$DIR/.env${N}
  Check keys:  ${B}$DIR/run.sh check${N}

EOF

if [ "$LAUNCH" = 1 ] && [ "$YES" = 0 ] && ask "Start ContextOS now?" y; then
  if grep -Eq '^[A-Z_]+_API_KEY=.+' .env; then exec ./run.sh
  else warn "No API keys in .env yet - starting with simulated replies."; exec ./run.sh --offline
  fi
fi
