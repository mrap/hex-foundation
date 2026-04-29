#!/usr/bin/env bash
# Wake script for boi-optimizer agent
set -euo pipefail

AGENT_DIR="${CLAUDE_PROJECT_DIR:-${HEX_DIR:-$HOME/hex}}"
source "$AGENT_DIR/.hex/scripts/env.sh"

AGENT_ID="boi-optimizer"
TRIGGER="${1:-timer.tick.6h}"
PAYLOAD="${2:-{}}"

exec "$AGENT_DIR/.hex/bin/hex" agent wake "$AGENT_ID" \
  --trigger "$TRIGGER" \
  --payload "$PAYLOAD"
