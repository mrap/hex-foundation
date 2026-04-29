#!/usr/bin/env bash
# check-secrets.sh — verify key secret files exist and have non-zero size.
# Exit 0 = all required secrets present, non-zero = missing or empty.
# Max runtime: 5 seconds.

set -uo pipefail

NAME="secrets"
HEX_ROOT="${CLAUDE_PROJECT_DIR:-${HEX_ROOT:-$HOME/hex}}"
SECRETS_DIR="$HEX_ROOT/.hex/secrets"
HEX_EVENTS_DIR="${HOME}/.hex-events"

FAILED=0
MISSING=()

# Required secret files.
declare -a REQUIRED=(
  "$SECRETS_DIR/slack-bot.env"
  "$SECRETS_DIR/x-api.env"
  "$SECRETS_DIR/fal.env"
  "$SECRETS_DIR/openrouter.env"
  "$SECRETS_DIR/excalidraw.env"
  "$HEX_EVENTS_DIR/adapters/scheduler.yaml"
)

for f in "${REQUIRED[@]}"; do
  if [[ ! -s "$f" ]]; then
    MISSING+=("$f")
    FAILED=$((FAILED + 1))
  fi
done

# Optional: kalshi-private.pem — check if present, verify PEM format.
KALSHI_PEM="$SECRETS_DIR/kalshi-private.pem"
if [[ -f "$KALSHI_PEM" ]]; then
  if ! grep -q "BEGIN" "$KALSHI_PEM" 2>/dev/null; then
    MISSING+=("$KALSHI_PEM (present but not valid PEM)")
    FAILED=$((FAILED + 1))
  fi
fi

if [[ "$FAILED" -gt 0 ]]; then
  echo "$NAME: FAIL - missing/empty: ${MISSING[*]}"
  exit 1
fi

TOTAL=${#REQUIRED[@]}
echo "$NAME: ok ($TOTAL required files present and non-empty)"
exit 0
