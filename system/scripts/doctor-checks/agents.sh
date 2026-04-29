#!/usr/bin/env bash
# doctor-checks/agents.sh — Verify all hex agents are alive and waking successfully
# Agent list is discovered from charters via hex-agent list — no hardcoded IDs.

set -uo pipefail

HEX_DIR="${HEX_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
HEX_AGENT="$HEX_DIR/.hex/bin/hex"

_check_claude_on_path() {
  if command -v claude &>/dev/null; then
    _pass "claude binary found at $(command -v claude)"
    _rec 20 "claude-binary-on-path" "pass" "found"
  else
    if [[ -x "$HOME/.local/bin/claude" ]]; then
      _error "claude binary exists at ~/.local/bin/claude but not on PATH — agent wakes will fail"
      _rec 20 "claude-binary-on-path" "error" "not on PATH"
    else
      _error "claude binary not found anywhere"
      _rec 20 "claude-binary-on-path" "error" "not found"
    fi
  fi
}

_check_agent_liveness() {
  local agent_name="$1"
  local agent_dir="$HEX_DIR/projects/$agent_name"
  local log_file="$agent_dir/log.jsonl"

  if [[ ! -f "$log_file" ]]; then
    return
  fi

  local fail_count=0
  local total=0
  while IFS= read -r line; do
    total=$((total + 1))
    if echo "$line" | grep -q '"status":"failed"\|"status":"throttled"'; then
      fail_count=$((fail_count + 1))
    fi
  done < <(tail -5 "$log_file" 2>/dev/null)

  if [[ $fail_count -eq $total && $total -gt 0 ]]; then
    local err_msg=""
    [[ -f "$agent_dir/last-error.txt" ]] && err_msg=$(tail -1 "$agent_dir/last-error.txt" 2>/dev/null | head -c 120)
    _error "Agent $agent_name: last $total log entries ALL failed/throttled${err_msg:+ — $err_msg}"
    _rec 22 "agent-$agent_name-liveness" "error" "all recent entries failed"
  elif [[ $fail_count -gt 0 ]]; then
    _warn "Agent $agent_name: $fail_count/$total recent entries failed/throttled"
    _rec 22 "agent-$agent_name-liveness" "warn" "$fail_count/$total recent failures"
  else
    _pass "Agent $agent_name: healthy (last $total entries all succeeded)"
    _rec 22 "agent-$agent_name-liveness" "pass" "healthy"
  fi
}

# Run checks
_check_claude_on_path

if [[ ! -x "$HEX_AGENT" ]]; then
  _error "hex-agent binary missing — cannot discover agents for liveness checks"
  _rec 22 "agent-discovery" "error" "hex-agent binary missing"
else
  while IFS= read -r agent_id; do
    [[ -z "$agent_id" ]] && continue
    _check_agent_liveness "$agent_id"
  done < <(HEX_DIR="$HEX_DIR" "$HEX_AGENT" agent list 2>/dev/null)
fi
