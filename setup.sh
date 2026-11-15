#!/usr/bin/env bash
# One-time setup for cycling-reels; --editor sets up an isolated macOS editor checkout.
# Safe to re-run: it only installs what's missing.
set -euo pipefail
cd "$(dirname "$0")"
ROOT="$(pwd)"

say()  { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
fail() { printf '\n\033[31mSetup stopped: %s\033[0m\n' "$*" >&2; exit 1; }

EDITOR_ONLY=0
case "${1:-}" in
  --editor) EDITOR_ONLY=1 ;;
  --help|-h)
    echo "Usage: bash setup.sh [--editor]"
    echo "  --editor  Local editor dependencies only; no ingest folders, project config or Whisper download."
    exit 0 ;;
  "") ;;
  *) fail "usage: bash setup.sh [--editor]" ;;
esac
[ "$#" -le 1 ] || fail "usage: bash setup.sh [--editor]"

if [ "$EDITOR_ONLY" -eq 1 ]; then
  if [ "${REELS_EDITOR_RUNTIME:-0}" != 1 ] && [ -x "$ROOT/editor/reels-editor" ]; then
    exec "$ROOT/editor/reels-editor" setup
  fi
  [ "$(uname -s)" = Darwin ] || fail "the native editor requires macOS."
  command -v xcodebuild >/dev/null 2>&1 && xcodebuild -version >/dev/null 2>&1 \
    || fail "install/select full Xcode before setting up the native app."
  command -v sandbox-exec >/dev/null 2>&1 || fail "sandbox-exec is required; renderer code never runs unsandboxed."
  [ ! -L .venv ] && [ ! -L remotion/node_modules ] \
    || fail "use independent .venv and remotion/node_modules directories, not links to another checkout."
  export PIP_CACHE_DIR="$ROOT/.cache/editor/setup/pip"
  export npm_config_cache="$ROOT/.cache/editor/setup/npm"
  export XDG_CACHE_HOME="$ROOT/.cache/editor/setup/xdg"
fi

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
  if [ "$EDITOR_ONLY" -eq 1 ]; then
    fail "missing ${need_brew[*]}. Install the system tools separately, then retry --editor; this mode does not change global packages."
  fi
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
if [ "$EDITOR_ONLY" -eq 1 ]; then
  ( cd remotion && npm ci --no-audit --no-fund --loglevel=error )
else
  ( cd remotion && npm install --no-audit --no-fund --loglevel=error )
fi

warn() { printf '\033[33m%s\033[0m\n' "$*" >&2; }

say "Chrome Headless Shell for rendering"
if ! ( cd remotion && npx --no-install remotion browser ensure ); then
  if [ "$EDITOR_ONLY" -eq 1 ]; then
    fail "the private rendering browser could not be installed. Retry editor setup when the network is available."
  fi
  warn "Couldn't download it now — Remotion will retry on the first render (or run ./reels doctor --fix)."
fi

if [ "$EDITOR_ONLY" -eq 1 ]; then
  say "Checking and building the protected editor preview"
  npm --prefix remotion run typecheck
  npm --prefix remotion run build:editor-preview
  chmod +x reels
  if ! command -v claude >/dev/null 2>&1; then
    warn "Claude Code is optional for manual editing. Install it and sign in locally for chat and semantic search."
  fi
  cat <<'EOF'

Editor dependencies ready in this checkout.
The integrated editor installer will finish the native build and private snapshot.
For manual development, see editor/docs/USAGE.md in the source checkout.
No source checkout was selected, project config created, or footage ingested.
EOF
  exit 0
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
