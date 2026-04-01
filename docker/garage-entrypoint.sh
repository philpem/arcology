#!/bin/sh
# Generate RPC secret and admin token on first run if they don't exist.
# Garage requires a 32-byte hex RPC secret; the admin token can be any string.

SECRET_FILE="/var/lib/garage/rpc_secret"
TOKEN_FILE="/var/lib/garage/admin_token"

if [ ! -f "$SECRET_FILE" ]; then
    # Generate 32-byte hex secret (64 chars)
    od -An -tx1 -N32 /dev/urandom | tr -d ' \n' > "$SECRET_FILE"
    echo "Generated new RPC secret"
fi

if [ ! -f "$TOKEN_FILE" ]; then
    od -An -tx1 -N32 /dev/urandom | tr -d ' \n' > "$TOKEN_FILE"
    echo "Generated new admin token"
fi

exec /garage "$@"
