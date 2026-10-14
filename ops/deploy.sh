#!/usr/bin/env bash
# Deploy a stockapp image by commit ID; revert automatically if it doesn't turn healthy.
# Usage: sudo /opt/stockapp/ops/deploy.sh <40-character commit ID>
set -euo pipefail

REPO="us-east1-docker.pkg.dev/jbs-labs/stockapp/stockapp"
COMPOSE_FILE="/opt/stockapp/ops/compose.yaml"
STATE_DIR="/var/lib/stockapp-deploy"
WAIT_SECONDS=120

if [ "$(id -u)" -ne 0 ]; then
  echo "Run with sudo." >&2
  exit 1
fi
if [ $# -ne 1 ] || ! [[ "$1" =~ ^[0-9a-f]{40}$ ]]; then
  echo "Usage: sudo $0 <40-character commit ID>" >&2
  exit 1
fi

NEW="$REPO:$1"
mkdir -p "$STATE_DIR"
OLD="$(cat "$STATE_DIR/current" 2>/dev/null || true)"

compose() {
  STOCKAPP_IMAGE="$1" docker compose -f "$COMPOSE_FILE" "${@:2}"
}

echo "== Backing up data"
/opt/stockapp/ops/backup.sh

echo "== Pulling $NEW"
docker pull "$NEW"

echo "== Starting $NEW (waiting up to ${WAIT_SECONDS}s for healthy)"
if compose "$NEW" up -d --wait --wait-timeout "$WAIT_SECONDS"; then
  if [ -n "$OLD" ] && [ "$OLD" != "$NEW" ]; then
    echo "$OLD" > "$STATE_DIR/previous"
  fi
  echo "$NEW" > "$STATE_DIR/current"
  echo "== Deploy OK: $NEW"
  exit 0
fi

echo "== New version did not become healthy. Last log lines:" >&2
compose "$NEW" logs --tail 50 >&2 || true

if [ -n "$OLD" ]; then
  echo "== Reverting to $OLD" >&2
  compose "$OLD" up -d --wait --wait-timeout "$WAIT_SECONDS"
  echo "== Reverted to $OLD" >&2
else
  echo "== No previous version to revert to; stopping the new container." >&2
  compose "$NEW" down
fi
exit 1
