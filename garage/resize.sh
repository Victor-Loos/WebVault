#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."
ENV_CAPACITY=$(grep '^GARAGE_CAPACITY=' .env 2>/dev/null | tail -1 | cut -d= -f2- || true)
CAPACITY="${GARAGE_CAPACITY:-${ENV_CAPACITY:-100G}}"
CAPACITY_MARKER="garage/.layout-capacity"
if [[ ! "$CAPACITY" =~ ^[1-9][0-9]*([KMGT]i?B?|[KMGT])$ ]]; then
    echo "Error: GARAGE_CAPACITY must be a positive Garage size such as 100G or 2TiB." >&2
    exit 1
fi

if [ -f "$CAPACITY_MARKER" ] && [ "$(cat "$CAPACITY_MARKER")" = "$CAPACITY" ]; then
    echo "Garage layout already uses capacity $CAPACITY."
    exit 0
fi

docker compose up -d garage >/dev/null
for _ in $(seq 1 60); do
    docker exec webvault-garage /garage status >/dev/null 2>&1 && break
    sleep 1
done

NODE_ID=$(docker exec webvault-garage /garage node id 2>/dev/null | grep -o '^[a-f0-9]*' | head -1)
LAYOUT=$(docker exec webvault-garage /garage layout show 2>/dev/null)
if [ -z "$NODE_ID" ]; then
    echo "Error: Could not determine the Garage node ID." >&2
    exit 1
fi
CURRENT_VERSION=$(printf '%s\n' "$LAYOUT" | grep -i 'layout version' | tail -1 | grep -o '[0-9][0-9]*' | tail -1)
if [ -z "$CURRENT_VERSION" ]; then
    echo "Error: Could not determine the current Garage layout version." >&2
    exit 1
fi
NEXT_VERSION=$((CURRENT_VERSION + 1))

echo "Changing Garage capacity to $CAPACITY (layout $NEXT_VERSION)..."
docker exec webvault-garage /garage layout assign -z dc1 -c "$CAPACITY" "$NODE_ID"
docker exec webvault-garage /garage layout apply --version "$NEXT_VERSION"
printf '%s\n' "$CAPACITY" > "$CAPACITY_MARKER"
echo "Garage capacity updated to $CAPACITY."
