#!/usr/bin/env bash
#
# Build (if needed) and run the hwid-blocker middleware WITHOUT Docker.
#
# Usage:
#   ./run.sh                 # build if missing, then run in the foreground
#   REBUILD=1 ./run.sh       # force a rebuild first
#   BIN=/custom/path ./run.sh
#
# Configuration comes from dockerless/.env (or $ENV_FILE), then from already
# exported environment variables, then from built-in defaults.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MID_ROOT="$(dirname "$SCRIPT_DIR")"

# 1. Load env file (KEY=value lines) if present.
ENV_FILE="${ENV_FILE:-$SCRIPT_DIR/.env}"
if [[ -f "$ENV_FILE" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "$ENV_FILE"
  set +a
  echo "[hwid-blocker] loaded env from $ENV_FILE"
else
  echo "[hwid-blocker] no env file at $ENV_FILE — using process env / defaults"
fi

# 2. Built-in defaults (overridable via env).
export LISTEN_ADDR="${LISTEN_ADDR:-127.0.0.1:3020}"
export BOT_LISTEN_ADDR="${BOT_LISTEN_ADDR:-}"
export UPSTREAM_URL="${UPSTREAM_URL:-http://127.0.0.1:3010}"
export BOT_API_URL="${BOT_API_URL:-http://127.0.0.1:8080}"
export FAIL_OPEN="${FAIL_OPEN:-true}"
export STATE_FILE="${STATE_FILE:-$MID_ROOT/data/state.json}"
mkdir -p "$(dirname "$STATE_FILE")"

# 3. Build a static binary if missing (or REBUILD=1).
BIN="${BIN:-$MID_ROOT/bin/hwid-blocker}"
if [[ ! -x "$BIN" || "${REBUILD:-0}" == "1" ]]; then
  echo "[hwid-blocker] building -> $BIN"
  mkdir -p "$(dirname "$BIN")"
  (cd "$MID_ROOT" && CGO_ENABLED=0 go build -trimpath -ldflags="-s -w" -o "$BIN" .)
fi

# 4. Run in the foreground (Ctrl-C / SIGTERM for a clean shutdown).
exec "$BIN" "$@"
