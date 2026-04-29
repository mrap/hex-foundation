#!/usr/bin/env bash
# x-oauth2-refresh.sh — Keep the xmcp OAuth2 user-context access token alive.
#
# X OAuth2 access tokens expire in 2 hours. Since xmcp reads X_OAUTH2_ACCESS_TOKEN
# from .env once at startup, we rotate the token in .env on a timer and the NEXT
# xmcp process spawn (each Claude Code session) picks up the fresh token.
#
# To force the currently-running MCP server to pick up a fresh token, Claude Code
# must be restarted (no way to signal-reload stdio MCP subprocesses).
#
# Usage: bash .hex/scripts/x-oauth2-refresh.sh
# Env file: $HOME/github.com/xdevplatform/xmcp/.env
#
# Models slack-token-refresh.sh.

set -euo pipefail

ENV_FILE="${HOME}/github.com/xdevplatform/xmcp/.env"

if [ ! -f "$ENV_FILE" ]; then
    echo "[x-oauth2-refresh] No env file at $ENV_FILE" >&2
    exit 1
fi

# Source without polluting current shell
CLIENT_ID=$(grep '^CLIENT_ID=' "$ENV_FILE" | cut -d= -f2-)
CLIENT_SECRET=$(grep '^CLIENT_SECRET=' "$ENV_FILE" | cut -d= -f2-)
REFRESH_TOKEN=$(grep '^X_OAUTH2_REFRESH_TOKEN=' "$ENV_FILE" | cut -d= -f2-)

if [ -z "${CLIENT_ID:-}" ] || [ -z "${CLIENT_SECRET:-}" ] || [ -z "${REFRESH_TOKEN:-}" ]; then
    echo "[x-oauth2-refresh] Missing CLIENT_ID / CLIENT_SECRET / X_OAUTH2_REFRESH_TOKEN in $ENV_FILE" >&2
    exit 1
fi

RESULT=$(curl -s -X POST "https://api.x.com/2/oauth2/token" \
    -u "$CLIENT_ID:$CLIENT_SECRET" \
    -H "Content-Type: application/x-www-form-urlencoded" \
    -d "grant_type=refresh_token&refresh_token=$REFRESH_TOKEN")

NEW_ACCESS=$(echo "$RESULT" | python3 -c "import json,sys; print(json.load(sys.stdin).get('access_token',''))")
NEW_REFRESH=$(echo "$RESULT" | python3 -c "import json,sys; print(json.load(sys.stdin).get('refresh_token',''))")

if [ -z "$NEW_ACCESS" ] || [ -z "$NEW_REFRESH" ]; then
    echo "[x-oauth2-refresh] FAILED: $RESULT" >&2
    exit 1
fi

python3 - "$ENV_FILE" "$NEW_ACCESS" "$NEW_REFRESH" <<'PY'
import re, sys
env_path, new_access, new_refresh = sys.argv[1], sys.argv[2], sys.argv[3]
with open(env_path) as f:
    content = f.read()
content = re.sub(r'^X_OAUTH2_ACCESS_TOKEN=.*$',  f'X_OAUTH2_ACCESS_TOKEN={new_access}',  content, flags=re.M)
content = re.sub(r'^X_OAUTH2_REFRESH_TOKEN=.*$', f'X_OAUTH2_REFRESH_TOKEN={new_refresh}', content, flags=re.M)
with open(env_path, 'w') as f:
    f.write(content)
PY

EXPIRES_IN=$(echo "$RESULT" | python3 -c "import json,sys; print(json.load(sys.stdin).get('expires_in','?'))")
echo "[x-oauth2-refresh] Token refreshed. Expires in ${EXPIRES_IN}s."
