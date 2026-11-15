#!/usr/bin/env bash
# One-time setup for cycling-reels; --editor also builds and installs the native Reels Editor app.
# Safe to re-run: it only installs what's missing.
set -euo pipefail
cd "$(dirname "$0")"
ROOT="$(pwd)"

say()  { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
fail() { printf '\n\033[31mSetup stopped: %s\033[0m\n' "$*" >&2; exit 1; }

case "${1:-}" in
  --editor)
    # Builds the app from this folder and installs ~/Applications/Reels Editor.app
    # (it also installs the Python/Remotion dependencies).
    exec "$ROOT/editor/reels-editor" setup ;;
  --help|-h)
    echo "Usage: bash setup.sh [--editor]"
    echo "  --editor  Build and install the native Reels Editor app for this folder."
    exit 0 ;;
  "") ;;
  *) fail "usage: bash setup.sh [--editor]" ;;
esac
[ "$#" -le 1 ] || fail "usage: bash setup.sh [--editor]"

# --- system tools -----------------------------------------------------------
need_brew=()
command -v ffmpeg >/dev/null 2>&1 || need_brew+=(ffmpeg)
command -v node   >/dev/null 2>&1 || need_brew+=(node)

pick_python() {
  for cand in python3.12 python3.13 python3.11 python3.10 python3; do
    if command -v "$cand" >/dev/null 2>&1; then
      if "$cand" -c 'import sys; raise SystemExit(0 if (3,10) <= sys.version_info[:2] <= (3,13) else 1)' 2>/dev/null; then
        echo "$cand"; return 0
      fi
    fi
  done
  return 1
}
PY="$(pick_python || true)"
[ -z "$PY" ] && need_brew+=(python@3.12)

if [ ${#need_brew[@]} -gt 0 ]; then
  if command -v brew >/dev/null 2>&1; then
    say "Installing with Homebrew: ${need_brew[*]}"
    brew install "${need_brew[@]}"
  else
    fail "missing ${need_brew[*]}. Install Homebrew from https://brew.sh, then run: brew install ${need_brew[*]}"
  fi
  [ -z "$PY" ] && PY="$(pick_python || true)"
  [ -z "$PY" ] && [ -x "$(brew --prefix 2>/dev/null)/bin/python3.12" ] && PY="$(brew --prefix)/bin/python3.12"
fi
[ -z "$PY" ] && fail "couldn't find Python 3.10–3.13 (brew install python@3.12)"

node_major="$(node -p 'process.versions.node.split(".")[0]')"
[ "$node_major" -ge 18 ] || fail "Node $node_major is too old — brew upgrade node (need 18+)"

# --- Python environment -----------------------------------------------------
say "Python environment (.venv) with $("$PY" --version)"
if [ ! -x .venv/bin/python ]; then
  "$PY" -m venv .venv
fi
.venv/bin/python -m pip install --quiet --upgrade pip
.venv/bin/python -m pip install --quiet -r requirements.txt

# --- Remotion template ------------------------------------------------------
say "Remotion template"
( cd remotion && npm install --no-audit --no-fund --loglevel=error )

warn() { printf '\033[33m%s\033[0m\n' "$*" >&2; }

say "Chrome Headless Shell for rendering"
if ! ( cd remotion && npx --no-install remotion browser ensure ); then
  warn "Couldn't download it now — Remotion will retry on the first render (or run ./reels doctor --fix)."
fi

# --- Whisper model ----------------------------------------------------------
say "Whisper speech-to-text model (one-time download, ~500 MB for small.en)"
if ! REELS_HOME="$ROOT" PYTHONPATH="$ROOT/scripts" .venv/bin/python - <<'EOF'
import sys
from reels.config import Project
from reels.transcribe import load_model
try:
    load_model(Project())
except Exception as e:  # network hiccups etc. — it retries on first use
    print(f"download failed: {type(e).__name__}: {str(e)[:200]}", file=sys.stderr)
    raise SystemExit(1)
print("model ready")
EOF
then
  warn "Couldn't download the Whisper model now — it downloads on the first ./reels transcribe (or run ./reels doctor --fix)."
fi

chmod +x reels
mkdir -p inbox library plans out music exhibits
if [ ! -f config.json ]; then
  cp config.example.json config.json
  echo "Created config.json — put your Instagram handle in it."
fi

say "Checking everything"
./reels doctor || true

cat <<'EOF'

Setup finished. Next:
  1. Put your Instagram handle in config.json  ("brand": {"handle": "@you"})
  2. Optional: run ./reels selftest  (renders a test reel incl. a 360 shot, a few minutes)
  3. Drop ride footage into inbox/, open this folder in Claude Code and say:
       "process my new footage and plan this week's reels"
EOF
