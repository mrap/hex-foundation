#!/usr/bin/env bash
# check-kalshi.sh — verify Kalshi integration: public exchange status + optional key check.
# Exit 0 = healthy, non-zero = unhealthy. Prints one-line status to stdout.
# Max runtime: 5 seconds.

set -uo pipefail

NAME="kalshi"
HEX_ROOT="${CLAUDE_PROJECT_DIR:-${HEX_ROOT:-$HOME/hex}}"
SECRETS_FILE="$HEX_ROOT/.hex/secrets/kalshi.env"
BASE_URL="https://api.elections.kalshi.com/trade-api/v2"

# Load secrets if present (sets KALSHI_KEY_ID, KALSHI_PRIVATE_KEY_PATH, etc.).
if [[ -f "$SECRETS_FILE" ]]; then
  # shellcheck source=/dev/null
  source "$SECRETS_FILE"
fi

# 1. Public exchange status endpoint (no auth required).
HTTP_CODE="$(curl -sf --max-time 4 -o /dev/null -w "%{http_code}" \
  -H "Accept: application/json" \
  "$BASE_URL/exchange/status" 2>/dev/null)" || HTTP_CODE="000"

if [[ "$HTTP_CODE" != "200" ]]; then
  echo "$NAME: FAIL - /exchange/status returned HTTP $HTTP_CODE (network issue or API down)"
  exit 1
fi

# 2. If private key path is configured, verify file exists and is a valid PEM.
KEY_PATH="${KALSHI_PRIVATE_KEY_PATH:-$HEX_ROOT/.hex/secrets/kalshi-private.pem}"
if [[ -n "${KALSHI_KEY_ID:-}" ]]; then
  if [[ ! -f "$KEY_PATH" ]]; then
    echo "$NAME: FAIL - KALSHI_KEY_ID set but private key not found at $KEY_PATH"
    exit 1
  fi
  if ! grep -q "BEGIN" "$KEY_PATH" 2>/dev/null; then
    echo "$NAME: FAIL - key file exists but is not valid PEM: $KEY_PATH"
    exit 1
  fi
  echo "$NAME: ok (exchange active, key present at $KEY_PATH)"
  exit 0
fi

echo "$NAME: ok (exchange active; no credentials configured — public check only)"
exit 0
