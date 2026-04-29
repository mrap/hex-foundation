#!/usr/bin/env bash
# excalidraw MCP integration health check
set -uo pipefail

HEX_DIR="${CLAUDE_PROJECT_DIR:-${HEX_DIR:-$HOME/hex}}"
SECRETS_FILE="${HEX_DIR}/.hex/secrets/excalidraw.env"
RESULT=0

# 1. Check secrets file exists and is non-empty
if [[ ! -s "$SECRETS_FILE" ]]; then
    echo "FAIL: $SECRETS_FILE missing or empty" >&2
    exit 1
fi
echo "OK: $SECRETS_FILE exists and is non-empty" >&2

# 2. Source excalidraw.env and check key variable is non-empty
# Supports EXCALIDRAW_API_KEY (actual) and EXCALIDRAW_SERVER_URL (alternate naming)
# shellcheck source=/dev/null
source "$SECRETS_FILE"

API_KEY="${EXCALIDRAW_API_KEY:-}"
SERVER_URL="${EXCALIDRAW_SERVER_URL:-}"

if [[ -z "$API_KEY" && -z "$SERVER_URL" ]]; then
    echo "FAIL: neither EXCALIDRAW_API_KEY nor EXCALIDRAW_SERVER_URL is set in excalidraw.env" >&2
    RESULT=1
else
    echo "OK: Excalidraw credentials present (API_KEY=${API_KEY:+set}, SERVER_URL=${SERVER_URL:+set})" >&2
fi

if [[ "$RESULT" -eq 0 ]]; then
    echo "OK: excalidraw-mcp env checks passed" >&2
fi

exit "$RESULT"
