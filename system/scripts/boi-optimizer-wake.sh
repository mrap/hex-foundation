#!/usr/bin/env bash
# Wake script for boi-optimizer agent
set -euo pipefail

HEX_DIR="${CLAUDE_PROJECT_DIR:-${HEX_DIR:-$HOME/hex}}"
source "$HEX_DIR/.hex/scripts/env.sh"

AGENT_ID="boi-optimizer"
TRIGGER="${1:-timer.tick.6h}"
PAYLOAD="${2:-{}}"

exec "$HEX_DIR/.hex/bin/hex" agent wake "$AGENT_ID" \
  --trigger "$TRIGGER" \
  --payload "$PAYLOAD"
