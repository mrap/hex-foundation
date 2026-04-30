#!/usr/bin/env bash
# probe.sh — mcp-plugin-ecc health check
# Cheapest check: verify plugin:ecc is enabled (provides memory/read_graph) + GITHUB_TOKEN present
set -uo pipefail

BUNDLE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CLAUDE_SETTINGS="${HOME}/.claude/settings.json"
[[ "${HEX_RUNTIME:-}" == "codex" ]] && CLAUDE_SETTINGS="${HOME}/.codex/settings.json"
SECRETS_FILE="${AGENT_DIR}/.hex/secrets/mcp-plugin-ecc.env"
RESULT=0

emit_event() {
  local event="$1" status="$2" msg="$3"
  printf '{"event":"%s","status":"%s","message":"%s","ts":"%s"}\n' \
    "$event" "$status" "$msg" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" >&2
}

echo "[mcp-plugin-ecc/probe] checking ECC plugin (github/memory/context7/sequential-thinking)..."

# 1. Check plugin:ecc (everything-claude-code) is enabled in Claude settings
if [[ -f "$CLAUDE_SETTINGS" ]]; then
  if python3 -c "
import json, sys
with open('$CLAUDE_SETTINGS') as f:
    d = json.load(f)
plugins = d.get('enabledPlugins', {})
enabled = any('everything-claude-code' in k for k in plugins.keys() if plugins[k])
sys.exit(0 if enabled else 1)
" 2>/dev/null; then
    echo "[mcp-plugin-ecc/probe] plugin:ecc (everything-claude-code) is enabled"
  else
    echo "[mcp-plugin-ecc/probe] WARN: plugin:ecc not found/enabled in $CLAUDE_SETTINGS" >&2
    RESULT=1
  fi
else
  echo "[mcp-plugin-ecc/probe] WARN: $CLAUDE_SETTINGS not found" >&2
  RESULT=1
fi

# 2. Check GITHUB_TOKEN is set in secrets file
if [[ -f "$SECRETS_FILE" ]]; then
  set +u
  source "$SECRETS_FILE" 2>/dev/null || true
  set -u
  if [[ -n "${GITHUB_TOKEN:-}" ]]; then
    echo "[mcp-plugin-ecc/probe] GITHUB_TOKEN present (${#GITHUB_TOKEN} chars)"
  else
    echo "[mcp-plugin-ecc/probe] WARN: GITHUB_TOKEN empty in $SECRETS_FILE" >&2
    RESULT=1
  fi
else
  echo "[mcp-plugin-ecc/probe] WARN: secrets file not found: $SECRETS_FILE" >&2
  RESULT=1
fi

# 3. Light connectivity check — verify GitHub API reachable (read_graph proxy: auth check)
if command -v curl &>/dev/null && [[ -n "${GITHUB_TOKEN:-}" ]]; then
  HTTP_STATUS="$(curl -s -o /dev/null -w "%{http_code}" \
    "https://api.github.com/user" \
    -H "Authorization: Bearer ${GITHUB_TOKEN}" \
    -H "X-GitHub-Api-Version: 2022-11-28" \
    --connect-timeout 5 --max-time 15 2>/dev/null || echo "000")"
  if [[ "$HTTP_STATUS" == "200" ]]; then
    echo "[mcp-plugin-ecc/probe] GitHub API: HTTP $HTTP_STATUS (ok)"
  elif [[ "$HTTP_STATUS" == "401" || "$HTTP_STATUS" == "403" ]]; then
    echo "[mcp-plugin-ecc/probe] WARN: GitHub API: HTTP $HTTP_STATUS (auth error)" >&2
    RESULT=1
  else
    echo "[mcp-plugin-ecc/probe] WARN: GitHub API: HTTP $HTTP_STATUS (degraded)" >&2
    RESULT=1
  fi
fi

if [[ "$RESULT" -eq 0 ]]; then
  emit_event "hex.integration.mcp-plugin-ecc.probe_ok" "ok" "plugin enabled + GITHUB_TOKEN present + api ok"
  echo "[mcp-plugin-ecc/probe] OK"
else
  emit_event "hex.integration.mcp-plugin-ecc.probe_fail" "fail" "one or more checks degraded"
  echo "[mcp-plugin-ecc/probe] DEGRADED (exit $RESULT)" >&2
fi

exit "$RESULT"
