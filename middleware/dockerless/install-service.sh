#!/usr/bin/env bash
#
# Install hwid-blocker as a native systemd service (no Docker required).
#
# What it does:
#   1. Builds a static Go binary into $INSTALL_DIR.
#   2. Copies your env file to $ENV_DIR/hwid-blocker.env (0600).
#   3. Creates a dedicated non-root user (if it does not exist).
#   4. Installs + enables a systemd unit pointing at the binary.
#
# Tunables (env vars): INSTALL_DIR, ENV_DIR, SERVICE_USER, ENV_FILE_SRC, STATE_DIR
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MID_ROOT="$(dirname "$SCRIPT_DIR")"

INSTALL_DIR="${INSTALL_DIR:-/opt/hwid-blocker}"
ENV_DIR="${ENV_DIR:-/etc/hwid-blocker}"
SERVICE_USER="${SERVICE_USER:-hwid}"
ENV_FILE_SRC="${ENV_FILE_SRC:-$SCRIPT_DIR/.env}"
STATE_DIR="${STATE_DIR:-$INSTALL_DIR/data}"
UNIT=/etc/systemd/system/hwid-blocker.service

# 1. Build the static binary.
echo "[install] building static binary -> $INSTALL_DIR/hwid-blocker"
mkdir -p "$INSTALL_DIR"
(cd "$MID_ROOT" && CGO_ENABLED=0 go build -trimpath -ldflags="-s -w" -o "$INSTALL_DIR/hwid-blocker" .)

# 2. Env file (real tokens live here; keep it 0600).
mkdir -p "$ENV_DIR"
if [[ -f "$ENV_FILE_SRC" ]]; then
  cp "$ENV_FILE_SRC" "$ENV_DIR/hwid-blocker.env"
else
  cp "$SCRIPT_DIR/.env.example" "$ENV_DIR/hwid-blocker.env"
  echo "[install] WARN: $ENV_FILE_SRC not found; copied .env.example — edit $ENV_DIR/hwid-blocker.env!"
fi
chmod 600 "$ENV_DIR/hwid-blocker.env"

# 3. State dir + dedicated user.
mkdir -p "$STATE_DIR"
if ! id "$SERVICE_USER" >/dev/null 2>&1; then
  useradd --system --home-dir "$INSTALL_DIR" --shell /usr/sbin/nologin "$SERVICE_USER"
fi
chown -R "$SERVICE_USER":"$SERVICE_USER" "$INSTALL_DIR"
chmod +r "$INSTALL_DIR/hwid-blocker"

# 4. systemd unit (substitute placeholders).
sed -e "s|__INSTALL_DIR__|$INSTALL_DIR|g" \
    -e "s|__ENV_DIR__|$ENV_DIR|g" \
    -e "s|__USER__|$SERVICE_USER|g" \
    "$SCRIPT_DIR/hwid-blocker.service" > "$UNIT"
systemctl daemon-reload
systemctl enable hwid-blocker.service

echo "[install] done."
echo "  Start now:   systemctl start hwid-blocker"
echo "  Status:      systemctl status hwid-blocker"
echo "  Logs:        journalctl -u hwid-blocker -f"
echo "  Env file:    $ENV_DIR/hwid-blocker.env"
