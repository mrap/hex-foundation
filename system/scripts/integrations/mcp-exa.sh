#!/usr/bin/env bash
# probe.sh — mcp-exa health check
set -uo pipefail

BUNDLE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CLAUDE_SETTINGS="${HOME}/.claude/settings.json"
[[ "${HEX_RUNTIME:-}" == "codex" ]] && CLAUDE_SETTINGS="${HOME}/.codex/settings.json"
SECRETS_FILE="${AGENT_DIR}/.hex/secrets/mcp-exa.env"
RESULT=0

emit_event() {
  local event="$1" status="$2" msg="$3"
  printf '{"event":"%s","status":"%s","message":"%s","ts":"%s"}\n' \
    "$event" "$status" "$msg" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" >&2
}

echo "[mcp-exa/probe] checking Exa MCP (plugin:ecc:exa)..."

# 1. Check plugin:ecc (everything-claude-code) is enabled — exa is provided via it
if [[ -f "$CLAUDE_SETTINGS" ]]; then
  if python3 -c "
import json, sys
with open('$CLAUDE_SETTINGS') as f:
    d = json.load(f)
plugins = d.get('enabledPlugins', {})
enabled = any('everything-claude-code' in k for k in plugins.keys() if plugins[k])
sys.exit(0 if enabled else 1)
" 2>/dev/null; then
    echo "[mcp-exa/probe] plugin:ecc (everything-claude-code) is enabled"
  else
    echo "[mcp-exa/probe] WARN: plugin:ecc not found/enabled in $CLAUDE_SETTINGS" >&2
    RESULT=1
  fi
else
  echo "[mcp-exa/probe] WARN: $CLAUDE_SETTINGS not found" >&2
  RESULT=1
fi

# 2. Check EXA_API_KEY is set in secrets file
if [[ -f "$SECRETS_FILE" ]]; then
  set +u
  source "$SECRETS_FILE" 2>/dev/null || true
  set -u
  if [[ -n "${EXA_API_KEY:-}" ]]; then
    echo "[mcp-exa/probe] EXA_API_KEY present (${#EXA_API_KEY} chars)"
  else
    echo "[mcp-exa/probe] WARN: EXA_API_KEY empty in $SECRETS_FILE" >&2
    RESULT=1
  fi
else
  echo "[mcp-exa/probe] WARN: secrets file not found: $SECRETS_FILE" >&2
  RESULT=1
fi

# 3. Light connectivity check (optional — failure is degraded, not hard fail)
if command -v curl &>/dev/null && [[ -n "${EXA_API_KEY:-}" ]]; then
  HTTP_STATUS="$(curl -s -o /dev/null -w "%{http_code}" \
    -X POST "https://api.exa.ai/search" \
    -H "Content-Type: application/json" \
    -H "x-api-key: ${EXA_API_KEY}" \
    -d '{"query":"hex reliability","numResults":1}' \
    --connect-timeout 5 --max-time 15 2>/dev/null || echo "000")"
  if [[ "$HTTP_STATUS" == "200" ]]; then
    echo "[mcp-exa/probe] Exa search API: HTTP $HTTP_STATUS (ok)"
  elif [[ "$HTTP_STATUS" == "401" || "$HTTP_STATUS" == "403" ]]; then
    echo "[mcp-exa/probe] WARN: Exa search API: HTTP $HTTP_STATUS (auth error)" >&2
    RESULT=1
  else
    echo "[mcp-exa/probe] WARN: Exa search API: HTTP $HTTP_STATUS (degraded)" >&2
    RESULT=1
  fi
fi

if [[ "$RESULT" -eq 0 ]]; then
  emit_event "hex.integration.mcp-exa.probe_ok" "ok" "plugin enabled + key present + api ok"
  echo "[mcp-exa/probe] OK"
else
  emit_event "hex.integration.mcp-exa.probe_fail" "fail" "one or more checks degraded"
  echo "[mcp-exa/probe] DEGRADED (exit $RESULT)" >&2
fi

exit "$RESULT"
