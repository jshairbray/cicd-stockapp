#!/usr/bin/env bash
# Start MongoDB and refresh its copy of the stock data from SQLite.
set -euo pipefail

APP_DIR=/opt/stockapp
DATA_DIR=/var/lib/stockapp

echo "Starting MongoDB..."
sudo systemctl start mongod

echo -n "Waiting for MongoDB"
for i in $(seq 1 60); do
  if mongosh --quiet --eval "db.runCommand({ ping: 1 }).ok" >/dev/null 2>&1; then
    echo " ready."
    break
  fi
  echo -n "."
  sleep 2
done

if ! mongosh --quiet --eval "db.runCommand({ ping: 1 }).ok" >/dev/null 2>&1; then
  echo
  echo "MongoDB did not start. Check: sudo tail -20 /var/log/mongodb/mongod.log"
  exit 1
fi

echo "Copying SQLite into MongoDB (database: stocks)..."
sudo -u stockapp STOCKAPP_DATA_DIR="$DATA_DIR" \
  "$APP_DIR/venv/bin/python3" "$APP_DIR/ops/sqlite_to_mongo.py"

echo "Done. Connect with: mongosh stocks"
