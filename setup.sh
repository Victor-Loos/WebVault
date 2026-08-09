#!/usr/bin/env bash

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ENV_FILE="$SCRIPT_DIR/.env"
GARAGE_TOML="$SCRIPT_DIR/garage/garage.toml"

create_webvault_password() {
    openssl rand -base64 24 2>/dev/null | tr -d '\n' || true
}

if [ -f "$ENV_FILE" ] && grep -q "GARAGE_ACCESS_KEY=" "$ENV_FILE" && [ -f "$GARAGE_TOML" ] && ! grep -q "REPLACEME_BY_SETUP" "$GARAGE_TOML"; then
    if ! grep -q '^WEBVAULT_USERNAME=' "$ENV_FILE" || ! grep -q '^WEBVAULT_PASSWORD=' "$ENV_FILE"; then
        WEBVAULT_PASSWORD=$(create_webvault_password)
        if [ -z "$WEBVAULT_PASSWORD" ]; then
            echo "Error: Could not generate the WebVault password. Install openssl and retry."
            exit 1
        fi
        {
            echo "WEBVAULT_USERNAME=admin"
            echo "WEBVAULT_PASSWORD=$WEBVAULT_PASSWORD"
        } >> "$ENV_FILE"
        chmod 600 "$ENV_FILE"
        echo "Enabled authentication for this existing installation."
        echo "WebVault login: admin"
        echo "WebVault password: $WEBVAULT_PASSWORD"
    else
        echo "Existing credentials found."
    fi
    if ! grep -q '^GARAGE_CAPACITY=' "$ENV_FILE"; then
        echo "GARAGE_CAPACITY=${GARAGE_CAPACITY:-100G}" >> "$ENV_FILE"
    fi
    "$SCRIPT_DIR/garage/resize.sh"
    echo "Setup checks complete. Run 'docker compose up -d' to start."
    exit 0
fi

echo "Setting up WebVault for the first time..."

if [ ! -f "$GARAGE_TOML" ]; then
    if [ -f "$SCRIPT_DIR/garage/garage.toml.example" ]; then
        echo "Copying garage/garage.toml.example → garage/garage.toml"
        cp "$SCRIPT_DIR/garage/garage.toml.example" "$GARAGE_TOML"
    else
        echo "Error: garage/garage.toml not found."
        exit 1
    fi
fi

if grep -q "REPLACEME_BY_SETUP" "$GARAGE_TOML"; then
    echo "Generating rpc_secret..."
    RPC_SECRET=$(openssl rand -hex 32 2>/dev/null || od -An -tx1 -N32 /dev/urandom | tr -d ' \n')
    if [ -z "$RPC_SECRET" ] || [ ${#RPC_SECRET} -lt 64 ]; then
        echo "Error: Could not generate rpc_secret."
        exit 1
    fi
    echo "Updating garage.toml with rpc_secret..."
    cp "$GARAGE_TOML" "$GARAGE_TOML.bak"
    sed "s|REPLACEME_BY_SETUP|$RPC_SECRET|g" "$GARAGE_TOML.bak" > "$GARAGE_TOML"
    rm -f "$GARAGE_TOML.bak"
fi

echo "Starting Garage..."
docker compose up -d garage

echo "Waiting for Garage to be healthy..."
for i in $(seq 1 60); do
    if docker exec webvault-garage /garage status 2>/dev/null | grep -q "HEALTHY"; then
        echo "Garage is healthy."
        break
    fi
    echo "Waiting... ($i/60)"
    sleep 1
done

if ! docker exec webvault-garage /garage status 2>/dev/null | grep -q "HEALTHY"; then
    echo "Error: Garage did not become healthy."
    docker compose logs garage
    exit 1
fi

echo "Bootstrapping Garage..."

NODE_ID=$(docker exec webvault-garage /garage node id 2>/dev/null | grep -o '^[a-f0-9]*' | head -1)
if [ -z "$NODE_ID" ]; then
    echo "Error: Could not get node ID."
    exit 1
fi
echo "Node ID: $NODE_ID"

LAYOUT=$(docker exec webvault-garage /garage layout show 2>/dev/null)
if echo "$LAYOUT" | grep -qi "no nodes\|version: 0"; then
    echo "Setting up layout..."
    docker exec webvault-garage /garage layout assign -z dc1 -c "${GARAGE_CAPACITY:-100G}" "$NODE_ID" > /dev/null 2>&1
    sleep 2
    docker exec webvault-garage /garage layout apply --version 1 > /dev/null 2>&1
    printf '%s\n' "${GARAGE_CAPACITY:-100G}" > "$SCRIPT_DIR/garage/.layout-capacity"
    sleep 2
fi

LAYOUT=$(docker exec webvault-garage /garage layout show 2>/dev/null)
if echo "$LAYOUT" | grep -qi "no nodes\|version: 0"; then
    echo "Error: Layout not applied."
    echo "$LAYOUT"
    exit 1
fi

echo "Creating bucket..."
docker exec webvault-garage /garage bucket create archives > /dev/null 2>&1

echo "Creating access key..."
KEY_OUT=$(docker exec webvault-garage /garage key create webvault-app 2>/dev/null)

GARAGE_ACCESS_KEY=""
GARAGE_SECRET_KEY=""

while IFS= read -r line; do
    case "$line" in
        *"Key ID:"*)
            GARAGE_ACCESS_KEY=$(echo "$line" | sed 's/.*Key ID: *//' | tr -d ' \r')
            ;;
        *"Secret key:"*)
            GARAGE_SECRET_KEY=$(echo "$line" | sed 's/.*Secret key: *//' | tr -d ' \r')
            ;;
    esac
done <<< "$KEY_OUT"

if [ -z "$GARAGE_ACCESS_KEY" ] || [ -z "$GARAGE_SECRET_KEY" ]; then
    echo "Error: Failed to generate S3 access key."
    echo "Output was: $KEY_OUT"
    exit 1
fi

echo "Granting bucket permissions..."
docker exec webvault-garage /garage bucket allow --read --write --owner archives --key "$GARAGE_ACCESS_KEY" > /dev/null 2>&1

RPC_SECRET=$(grep "^rpc_secret" "$GARAGE_TOML" | sed 's/.*= *//' | tr -d '" ')
WEBVAULT_PASSWORD=$(create_webvault_password)
if [ -z "$WEBVAULT_PASSWORD" ]; then
    echo "Error: Could not generate the WebVault password. Install openssl and retry."
    exit 1
fi

echo "Writing .env..."
cat > "$ENV_FILE" <<EOF
RPC_SECRET=$RPC_SECRET
GARAGE_ACCESS_KEY=$GARAGE_ACCESS_KEY
GARAGE_SECRET_KEY=$GARAGE_SECRET_KEY
WEBVAULT_USERNAME=admin
WEBVAULT_PASSWORD=$WEBVAULT_PASSWORD
GARAGE_CAPACITY=${GARAGE_CAPACITY:-100G}
EOF

chmod 600 "$ENV_FILE"

echo "Stopping Garage for now..."
docker compose stop garage > /dev/null 2>&1

echo ""
echo "Setup complete!"
echo "WebVault login: admin"
echo "WebVault password: $WEBVAULT_PASSWORD"
echo "Store this password securely; it remains available in .env."
echo "Run 'docker compose up -d' to start all services."
