#!/usr/bin/env bash
# Start the BOI live status web view.
# URL: https://<tailscale-hostname>:8891 (or http://localhost:8891 without TLS)

set -uo pipefail
cd "$(dirname "$0")"

export PORT="${PORT:-8891}"
# Set CERT and KEY env vars to enable TLS (e.g., Tailscale certs)
export CERT="${CERT:-}"
export KEY="${KEY:-}"

exec python3 server.py
