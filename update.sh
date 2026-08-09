#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"
# Also migrates older installations to authentication-by-default.
./setup.sh
echo "Pulling the configured Browsertrix and Garage images..."
docker compose pull garage crawler
echo "Rebuilding the orchestrator with pinned Python dependencies..."
docker compose build --pull --no-cache orchestrator worker
echo "Applying updates..."
docker compose up -d --remove-orphans
echo "WebVault is up to date."
