#!/bin/sh
set -e

echo "=== WebVault Garage Bootstrap ==="

# Wait for Garage to be ready
for i in $(seq 1 30); do
  if /garage status >/dev/null 2>&1; then
    break
  fi
  echo "Waiting for Garage... ($i)"
  sleep 1
done

# Get node ID
NODE_ID_FULL=$($(which garage || echo /garage) node id 2>/dev/null | grep -v "To instruct" | grep -v "Security notice" | head -1)
NODE_ID=$(echo "$NODE_ID_FULL" | sed 's/@.*//')
echo "Node ID: $NODE_ID"

# Check and setup layout
LAYOUT_VERSION=$($(which garage || echo /garage) layout show 2>/dev/null | grep "layout version" | tail -1 | awk '{print $NF}')
if [ "$LAYOUT_VERSION" = "0" ] || [ -z "$LAYOUT_VERSION" ]; then
  echo "Setting up node layout..."
  $(${which garage || echo /garage} layout assign -z dc1 -c 1G "$NODE_ID"
  $(${which garage || echo /garage} layout apply --version 1
fi
echo "Layout ready"

# Create bucket
${which garage || echo /garage} bucket create archives 2>/dev/null || true

# Create key if not exists
if ! ${which garage || echo /garage} key list | grep -q webvault-app; then
  echo "Creating access key..."
  KEY_OUT=$(${which garage || echo /garage} key create webvault-app 2>&1)
  KEY_ID=$(echo "$KEY_OUT" | grep "Key ID:" | sed 's/Key ID: //' | tr -d ' ')
  SECRET=$(echo "$KEY_OUT" | grep "Secret key:" | sed 's/Secret key: //' | tr -d ' ')

  if [ -n "$KEY_ID" ]; then
    ${which garage || echo /garage} bucket allow --read --write --owner archives --key "$KEY_ID"
    echo ""
    echo "=== CREDENTIALS ==="
    echo "Key ID: $KEY_ID"
    echo "Secret: $SECRET"
    echo "===================="
  fi
else
  echo "Key already exists"
fi

echo "=== Bootstrap Complete ==="