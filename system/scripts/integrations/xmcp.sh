#!/usr/bin/env bash
# xmcp (x-twitter) MCP integration health check
set -uo pipefail

HEX_DIR="${CLAUDE_PROJECT_DIR:-${HEX_DIR:-$HOME/hex}}"
SECRETS_FILE="${HEX_DIR}/.hex/secrets/x-api.env"
RESULT=0

# 1. Check secrets file exists and is non-empty
if [[ ! -s "$SECRETS_FILE" ]]; then
    echo "FAIL: $SECRETS_FILE missing or empty" >&2
    exit 1
fi

# 2. Verify xmcp binary or npx package is accessible
if command -v xmcp &>/dev/null; then
    echo "xmcp binary found: $(command -v xmcp)" >&2
elif timeout 15 npx --yes xmcp@latest --version 2>/dev/null; then
    echo "xmcp reachable via npx" >&2
else
    echo "WARN: xmcp binary not found via 'which' or npx; continuing" >&2
fi

# 3. Source x-api.env and check key vars are non-empty
# Supports both TWITTER_* (actual) and X_* naming conventions
# shellcheck source=/dev/null
source "$SECRETS_FILE"

CONSUMER_KEY="${X_CONSUMER_KEY:-${TWITTER_API_KEY:-}}"
ACCESS_TOKEN="${X_ACCESS_TOKEN:-${TWITTER_ACCESS_TOKEN:-}}"

if [[ -z "$CONSUMER_KEY" ]]; then
    echo "FAIL: neither X_CONSUMER_KEY nor TWITTER_API_KEY is set in x-api.env" >&2
    RESULT=1
fi

if [[ -z "$ACCESS_TOKEN" ]]; then
    echo "FAIL: neither X_ACCESS_TOKEN nor TWITTER_ACCESS_TOKEN is set in x-api.env" >&2
    RESULT=1
fi

if [[ "$RESULT" -eq 0 ]]; then
    echo "OK: xmcp env vars present; secrets file non-empty" >&2
fi

exit "$RESULT"
