#!/usr/bin/env bash
# Stop MongoDB to free memory. Data stays on disk.
set -euo pipefail
sudo systemctl stop mongod
echo "MongoDB is $(systemctl is-active mongod || true)."
