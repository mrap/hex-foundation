#!/usr/bin/env bash
# check-fleet-pulse.sh — detect agent dormancy and ghost-waking
#
# Critic revisions applied (HIGH severity, 2026-05-05):
#  - Composite liveness score replaces binary act-count threshold
#  - WARN for ghost-wake, ERROR for confirmed dormancy (zero 24h trail entries)
#  - Budget-lockout suppression: parked agents during budget cap are not dormant
#  - expected_act_rate in charter.yaml gates composite checks; absent = exempt
#  - Two-window escalation: second consecutive degraded window upgrades WARN→ERROR
#
# Usage: check-fleet-pulse.sh [--dry-run]
# Exit:  0 = all healthy, 1 = any dormant or ghost-waking agents
set -uo pipefail

SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPTS_DIR="$(cd "$SELF_DIR/.." && pwd)"
HEX_PROJECT_DIR="$(cd "$SCRIPTS_DIR/../.." && pwd)"
HEX_ALERT="$SCRIPTS_DIR/hex-alert.sh"
HEX_EMIT="$HOME/.hex-events/hex_emit.py"
PULSE_STATE="$HEX_PROJECT_DIR/.hex/audit/fleet-pulse-state.json"

DRY_RUN=0
for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY_RUN=1 ;;
    *) echo "check-fleet-pulse: unknown arg: $arg" >&2; exit 1 ;;
  esac
done

# ─── Core detection (Python) ──────────────────────────────────────────────────
result="$(python3 - "$HEX_PROJECT_DIR" "$DRY_RUN" "$PULSE_STATE" <<'PYEOF'
import sys, json, os, re, glob
from datetime import datetime, timezone, timedelta

project_root = sys.argv[1]
dry_run = sys.argv[2] == "1"
state_path = sys.argv[3]
projects_dir = os.path.join(project_root, "projects")

now = datetime.now(timezone.utc)
window_24h = now - timedelta(hours=24)

# Load two-window escalation state (tracks per-agent first-degraded timestamp)
pulse_state = {}
if os.path.exists(state_path):
    try:
        with open(state_path) as f:
            pulse_state = json.load(f)
    except Exception:
        pulse_state = {}

new_pulse_state = {}

def parse_iso(s):
    if not s:
        return None
    s = s.rstrip("Z")
    if "." in s:
        s = s[:26]
    try:
        return datetime.fromisoformat(s).replace(tzinfo=timezone.utc)
    except Exception:
        return None

def cadence_seconds_from_charter(charter):
    """Extract shortest timer cadence in seconds from wake.triggers."""
    triggers = []
    try:
        triggers = charter.get("wake", {}).get("triggers", [])
    except Exception:
        pass
    cadence = None
    for t in triggers:
        if not isinstance(t, str):
            continue
        m = re.match(r"timer\.tick\.(\d+)(h|m|s|d)?$", t)
        if m:
            n, unit = int(m.group(1)), (m.group(2) or "s")
            secs = n * {"h": 3600, "m": 60, "s": 1, "d": 86400}.get(unit, 1)
        elif t == "timer.tick.daily":
            secs = 86400
        elif t == "timer.tick.hourly":
            secs = 3600
        else:
            continue
        if cadence is None or secs < cadence:
            cadence = secs
    return cadence or 21600  # default 6h

issues = []  # list of (agent_id, severity, reason)
healthy = []

charters = glob.glob(os.path.join(projects_dir, "*/charter.yaml"))
if not charters:
    print("NO_AGENTS", flush=True)
    sys.exit(0)

for charter_path in sorted(charters):
    proj_dir = os.path.dirname(charter_path)
    agent_id = os.path.basename(proj_dir)

    # Skip archived projects
    if "_archive" in proj_dir:
        continue

    # Load charter
    try:
        import re as _re
        with open(charter_path) as f:
            charter_text = f.read()
        # Minimal YAML parser for the fields we need (avoid pyyaml dependency)
        charter = {}
        # id / agent_id
        m = re.search(r"^id:\s*(.+)$", charter_text, re.MULTILINE)
        if m:
            charter["id"] = m.group(1).strip()
        # expected_act_rate
        m = re.search(r"^expected_act_rate:\s*(.+)$", charter_text, re.MULTILINE)
        if m:
            try:
                charter["expected_act_rate"] = float(m.group(1).strip())
            except ValueError:
                pass
        # wake triggers
        charter["wake"] = {"triggers": []}
        for t in re.findall(r"^\s*-\s*(timer\.tick\.[^\s]+)", charter_text, re.MULTILINE):
            charter["wake"]["triggers"].append(t)
    except Exception as e:
        continue

    # Load state.json
    state_path_agent = os.path.join(proj_dir, "state.json")
    if not os.path.exists(state_path_agent):
        continue
    try:
        with open(state_path_agent) as f:
            state = json.load(f)
    except Exception:
        continue

    last_wake_str = state.get("last_wake")
    last_wake = parse_iso(last_wake_str)
    trail = state.get("trail", [])
    cost = state.get("cost", {})
    period = cost.get("current_period", {})

    # ── Budget-lockout suppression ────────────────────────────────────────────
    budget_usd = period.get("budget_usd", 0) or 0
    spent_usd = period.get("spent_usd", 0) or 0
    last_trail_type = trail[-1].get("type", "") if trail else ""
    budget_parked = (
        budget_usd > 0
        and spent_usd > budget_usd * 0.9
        and last_trail_type == "park"
    )
    if budget_parked:
        healthy.append(f"{agent_id}: budget-parked (spent={spent_usd:.2f}/{budget_usd:.2f}) — suppressed")
        continue

    cadence_secs = cadence_seconds_from_charter(charter)

    # ── Dormancy check (no wake in 2× cadence) ───────────────────────────────
    dormant = False
    if last_wake is None:
        dormant = True
        dormancy_reason = "no last_wake recorded"
    else:
        age_secs = (now - last_wake).total_seconds()
        threshold_secs = cadence_secs * 2
        if age_secs > threshold_secs:
            dormant = True
            dormancy_reason = (
                f"no wake in {age_secs/3600:.1f}h "
                f"(cadence={cadence_secs//3600}h, threshold={threshold_secs//3600}h)"
            )

    # ── Trail entries in last 24h ─────────────────────────────────────────────
    trail_24h = [
        e for e in trail
        if parse_iso(e.get("ts")) and parse_iso(e.get("ts")) >= window_24h
    ]
    has_any_trail_24h = len(trail_24h) > 0

    # Confirmed dormancy: no trail entries at all in 24h (even if last_wake is recent)
    if not has_any_trail_24h and not dormant:
        # last_wake is recent but no trail entries — possible state.json desync
        # Only flag if we have ≥ 1 wake in 24h window implied by last_wake
        if last_wake and (now - last_wake).total_seconds() < 86400:
            dormant = True
            dormancy_reason = "zero trail entries in last 24h despite recent last_wake (possible trail desync)"

    if dormant:
        severity = "ERROR"
        reason = dormancy_reason
        prev = pulse_state.get(agent_id, {})
        if prev.get("issue") == "dormant" and prev.get("since"):
            prev_since = parse_iso(prev["since"])
            if prev_since and (now - prev_since).total_seconds() > 3600:
                severity = "CRITICAL"
        new_pulse_state[agent_id] = {"issue": "dormant", "since": prev.get("since") or now.isoformat()}
        issues.append((agent_id, severity, f"dormant: {reason}"))
        continue

    # ── Composite liveness score (only if expected_act_rate set) ─────────────
    expected_act_rate = charter.get("expected_act_rate")
    if expected_act_rate is not None and has_any_trail_24h:
        acts_24h = sum(1 for e in trail_24h if e.get("type") == "act")
        messages_24h = sum(1 for e in trail_24h if e.get("type") == "message_sent")
        rich_24h = sum(
            1 for e in trail_24h
            if e.get("type") in ("find", "decide", "verify")
        )
        liveness_score = acts_24h * 1.0 + messages_24h * 0.5 + rich_24h * 0.3

        non_park_wakes = sum(1 for e in trail_24h if e.get("type") not in ("park", "observe"))
        threshold = expected_act_rate * 0.1

        if liveness_score < threshold and non_park_wakes >= 4:
            prev = pulse_state.get(agent_id, {})
            severity = "WARN"
            if prev.get("issue") == "ghost-waking" and prev.get("since"):
                prev_since = parse_iso(prev["since"])
                if prev_since and (now - prev_since).total_seconds() > 3600:
                    severity = "ERROR"  # two consecutive windows
            reason = (
                f"ghost-waking: liveness_score={liveness_score:.2f} < threshold={threshold:.2f} "
                f"(expected_act_rate={expected_act_rate}, acts={acts_24h}, "
                f"msgs={messages_24h}, rich={rich_24h}), non_park_wakes={non_park_wakes}"
            )
            new_pulse_state[agent_id] = {
                "issue": "ghost-waking",
                "since": prev.get("since") or now.isoformat()
            }
            issues.append((agent_id, severity, reason))
            continue

    # All checks passed
    healthy.append(agent_id)
    # Clear any prior degraded state
    if agent_id in pulse_state:
        pass  # don't carry over — agent is healthy now

# ── Persist state for two-window escalation ───────────────────────────────────
if not dry_run:
    try:
        tmp = state_path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(new_pulse_state, f, indent=2)
        os.replace(tmp, state_path)
    except Exception as e:
        print(f"STATE_WRITE_ERROR:{e}", file=sys.stderr, flush=True)

# ── Output results ────────────────────────────────────────────────────────────
output = {
    "issues": [{"id": a, "severity": s, "reason": r} for a, s, r in issues],
    "healthy": healthy,
    "checked": len(charters),
}
print(json.dumps(output))
PYEOF
)"

if [[ -z "$result" ]]; then
  echo "check-fleet-pulse: INTERNAL ERROR — python analysis produced no output" >&2
  if [[ $DRY_RUN -eq 0 && -x "$HEX_ALERT" ]]; then
    "$HEX_ALERT" ERROR "fleet-pulse" "check-fleet-pulse.sh: analysis script failed silently" 2>/dev/null || true
  fi
  exit 1
fi

if [[ "$result" == "NO_AGENTS" ]]; then
  echo "check-fleet-pulse: no agents found in $HEX_PROJECT_DIR/projects" >&2
  exit 0
fi

# Parse results
issues_count="$(python3 -c "import json,sys; d=json.loads(sys.stdin.read()); print(len(d['issues']))" <<< "$result" 2>/dev/null || echo 0)"
healthy_count="$(python3 -c "import json,sys; d=json.loads(sys.stdin.read()); print(len(d['healthy']))" <<< "$result" 2>/dev/null || echo 0)"
checked_count="$(python3 -c "import json,sys; d=json.loads(sys.stdin.read()); print(d['checked'])" <<< "$result" 2>/dev/null || echo 0)"

if [[ $DRY_RUN -eq 1 ]]; then
  echo "check-fleet-pulse [DRY-RUN]: checked=$checked_count healthy=$healthy_count issues=$issues_count"
  if [[ "$issues_count" -gt 0 ]]; then
    python3 -c "
import json,sys
d=json.loads(sys.stdin.read())
for i in d['issues']:
    print(f'  [{i[\"severity\"]}] {i[\"id\"]}: {i[\"reason\"]}')
" <<< "$result"
  fi
  exit 0
fi

# Real run: fire alerts and emit events
python3 - "$result" "$HEX_ALERT" "$HEX_EMIT" <<'PYEOF'
import json, sys, subprocess, os

data = json.loads(sys.argv[1])
hex_alert = sys.argv[2]
hex_emit = sys.argv[3]

for issue in data["issues"]:
    agent_id = issue["id"]
    severity = issue["severity"]
    reason = issue["reason"]
    print(f"check-fleet-pulse: [{severity}] {agent_id}: {reason}", flush=True)

    # hex-alert
    if os.path.exists(hex_alert) and os.access(hex_alert, os.X_OK):
        subprocess.run(
            [hex_alert, severity, "fleet-pulse", f"agent {agent_id}: {reason}"],
            timeout=15, check=False
        )

    # hex-event
    payload = json.dumps({
        "agent_id": agent_id,
        "severity": severity,
        "reason": reason,
        "ts": __import__("datetime").datetime.utcnow().isoformat() + "Z",
    })
    if os.path.exists(hex_emit):
        subprocess.run(
            ["python3", hex_emit, "hex.fleet.agent.dormant", payload, "hex:fleet-pulse"],
            timeout=15, check=False
        )

for h in data["healthy"]:
    print(f"check-fleet-pulse: ok  {h}", flush=True)

print(f"check-fleet-pulse: summary checked={data['checked']} healthy={len(data['healthy'])} issues={len(data['issues'])}", flush=True)
PYEOF

if [[ "$issues_count" -gt 0 ]]; then
  exit 1
fi
exit 0
