#!/usr/bin/env bash
# Manual backup of the stockapp SQLite database to Cloud Storage.
# Usage on the VM:  sudo /opt/stockapp/ops/backup.sh [manual|predeploy]
# Needs only roles/storage.objectCreator on the bucket (create-only upload).
set -euo pipefail

BUCKET="jbs-labs-cicd-stockapp-backups"
DB="/var/lib/stockapp/stock_reports.db"
APP_USER="stockapp"
KIND="${1:-manual}"

# --- Checks ---------------------------------------------------------------
case "$KIND" in
  manual|predeploy) ;;
  *) echo "usage: $0 [manual|predeploy]" >&2; exit 2 ;;
esac
[ "$(id -u)" -eq 0 ] || { echo "Please run with sudo." >&2; exit 1; }
[ -f "$DB" ] || { echo "Database not found: $DB" >&2; exit 1; }

# --- Private work folder, deleted automatically at the end ----------------
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT
chown "$APP_USER" "$WORK"
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
COPY="$WORK/stock_reports-$STAMP.db"

# --- 1. Safe copy (SQLite backup API, source opened read-only) -------------
sudo -u "$APP_USER" python3 - "$DB" "$COPY" << 'PY'
import sqlite3, sys
src = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True)
dst = sqlite3.connect(sys.argv[2])
src.backup(dst)
dst.close(); src.close()
PY

# --- 2. Check the copy is healthy -----------------------------------------
RESULT=$(python3 - "$COPY" << 'PY'
import sqlite3, sys
print(sqlite3.connect(sys.argv[1]).execute("PRAGMA integrity_check").fetchone()[0])
PY
)
[ "$RESULT" = "ok" ] || { echo "Integrity check failed: $RESULT" >&2; exit 1; }

# --- 3. Short-lived token from the metadata server (no key file) ----------
TOKEN=$(curl -sS --fail -H "Metadata-Flavor: Google" \
  "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token" \
  | python3 -c 'import sys, json; print(json.load(sys.stdin)["access_token"])')

# --- 4. Upload, create-only (ifGenerationMatch=0 = never overwrite) -------
NAME="$KIND/$(basename "$COPY")"
ENC=$(python3 -c 'import sys, urllib.parse; print(urllib.parse.quote(sys.argv[1], safe=""))' "$NAME")
HTTP=$(curl -sS -o "$WORK/response.json" -w '%{http_code}' -X POST \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/octet-stream" \
  --data-binary @"$COPY" \
  "https://storage.googleapis.com/upload/storage/v1/b/$BUCKET/o?uploadType=media&ifGenerationMatch=0&name=$ENC")
if [ "$HTTP" != "200" ]; then
  echo "Upload failed (HTTP $HTTP):" >&2
  cat "$WORK/response.json" >&2
  exit 1
fi

echo "Backup OK: gs://$BUCKET/$NAME ($(stat -c %s "$COPY") bytes)"
