#!/usr/bin/env bash
# exa MCP integration health check
set -uo pipefail

RESULT=0
HEX_DIR="${CLAUDE_PROJECT_DIR:-${HEX_DIR:-$HOME/hex}}"
EXA_ENV_FILE="${HEX_DIR}/.hex/secrets/exa.env"

# 1. Check for EXA_API_KEY in environment
EXA_KEY="${EXA_API_KEY:-}"

# If not in env, check the exa secrets file if it exists
if [[ -z "$EXA_KEY" && -s "$EXA_ENV_FILE" ]]; then
    # shellcheck source=/dev/null
    source "$EXA_ENV_FILE"
    EXA_KEY="${EXA_API_KEY:-}"
fi

if [[ -z "$EXA_KEY" ]]; then
    echo "FAIL: EXA_API_KEY not found in environment or $EXA_ENV_FILE" >&2
    RESULT=1
else
    echo "OK: EXA_API_KEY is set" >&2
fi

# 2. Verify exa MCP is configured in plugin or Claude settings
EXA_CONFIGURED=0
    if [[ -f "$mcp_file" ]] && grep -q '"exa"' "$mcp_file" 2>/dev/null; then
        echo "OK: exa MCP configured in $mcp_file" >&2
        EXA_CONFIGURED=1
        break
    fi
done

# Also check .claude.json
if [[ "$EXA_CONFIGURED" -eq 0 ]]; then
    if [[ -f "${HOME}/.claude.json" ]] && grep -q '"exa"' "${HOME}/.claude.json" 2>/dev/null; then
        echo "OK: exa MCP configured in .claude.json" >&2
        EXA_CONFIGURED=1
    fi
fi

if [[ "$EXA_CONFIGURED" -eq 0 ]]; then
    echo "WARN: exa MCP not found in Claude settings or ECC plugin config" >&2
    # Warn only — the plugin system may inject it dynamically; not a hard failure
fi

if [[ "$RESULT" -eq 0 ]]; then
    echo "OK: exa-mcp healthy" >&2
    exit 0
else
    exit 1
fi
