#!/bin/sh
set -eu

GARAGE_BIN="$(command -v garage 2>/dev/null || printf '%s' /garage)"

echo "=== WebVault Garage Bootstrap ==="

ready=false
for i in $(seq 1 30); do
  if "$GARAGE_BIN" status >/dev/null 2>&1; then
    ready=true
    break
  fi
  echo "Waiting for Garage... ($i)"
  sleep 1
done
if [ "$ready" != true ]; then
  echo "Garage did not become ready" >&2
  exit 1
fi

NODE_ID_FULL=$("$GARAGE_BIN" node id 2>/dev/null | grep -v "To instruct\|Security notice" | head -1)
NODE_ID=$(printf '%s\n' "$NODE_ID_FULL" | sed 's/@.*//')
if [ -z "$NODE_ID" ]; then
  echo "Could not determine Garage node ID" >&2
  exit 1
fi
echo "Node ID: $NODE_ID"

LAYOUT_VERSION=$("$GARAGE_BIN" layout show 2>/dev/null | grep "layout version" | tail -1 | awk '{print $NF}' || true)
if [ "$LAYOUT_VERSION" = "0" ] || [ -z "$LAYOUT_VERSION" ]; then
  echo "Setting up node layout..."
  "$GARAGE_BIN" layout assign -z dc1 -c 1G "$NODE_ID"
  "$GARAGE_BIN" layout apply --version 1
fi
echo "Layout ready"

"$GARAGE_BIN" bucket create archives 2>/dev/null || true

if ! "$GARAGE_BIN" key list | grep -q webvault-app; then
  echo "Creating access key..."
  KEY_OUT=$("$GARAGE_BIN" key create webvault-app 2>&1)
  KEY_ID=$(printf '%s\n' "$KEY_OUT" | grep "Key ID:" | sed 's/.*Key ID: *//' | tr -d ' \r')
  SECRET=$(printf '%s\n' "$KEY_OUT" | grep "Secret key:" | sed 's/.*Secret key: *//' | tr -d ' \r')

  if [ -z "$KEY_ID" ] || [ -z "$SECRET" ]; then
    echo "Failed to parse generated credentials" >&2
    exit 1
  fi
  "$GARAGE_BIN" bucket allow --read --write --owner archives --key "$KEY_ID"
  echo ""
  echo "=== CREDENTIALS ==="
  echo "Key ID: $KEY_ID"
  echo "Secret: $SECRET"
  echo "===================="
else
  echo "Key already exists"
fi

echo "=== Bootstrap Complete ==="
