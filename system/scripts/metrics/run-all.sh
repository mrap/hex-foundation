#!/usr/bin/env bash
# run-all.sh — Run all user-outcome metrics scripts and report PASS/FAIL.
set -uo pipefail

METRICS_DIR="$(cd "$(dirname "$0")" && pwd)"
SCRIPTS_DIR="$(dirname "$METRICS_DIR")"
SNAPSHOTS_FILE="${HOME}/.hex/audit/metric-snapshots.jsonl"
OVERALL=0

red()   { printf '\033[31m%s\033[0m\n' "$*"; }
green() { printf '\033[32m%s\033[0m\n' "$*"; }
bold()  { printf '\033[1m%s\033[0m\n' "$*"; }

run_metric() {
  local name="$1"
  local script="$2"
  if [ ! -f "$script" ]; then
    red "  MISSING: $name — script not found at $script"
    OVERALL=1
    return
  fi
  local out
  out=$(python3 "$script" 2>&1)
  local rc=$?
  if [ $rc -eq 0 ]; then
    green "  PASS: $name — $out"
  elif [ $rc -eq 2 ]; then
    red "  FAIL (threshold breached): $name — $out"
    OVERALL=1
  else
    red "  FAIL (script error rc=$rc): $name — $out"
    OVERALL=1
  fi
}

snapshot_telemetry_ratio() {
  local ratio_script="$SCRIPTS_DIR/telemetry-ratio.py"
  if [ ! -f "$ratio_script" ]; then
    red "  MISSING: telemetry-ratio snapshot — script not found at $ratio_script"
    return
  fi

  local json_out
  json_out=$(python3 "$ratio_script" --json --hours 24 2>/dev/null) || true
  if [ -z "$json_out" ]; then
    red "  WARN: telemetry-ratio produced no output — skipping snapshot"
    return
  fi

  # Extract per-surface rows and write one snapshot line per surface.
  # Uses only stdlib (json + datetime) — no pip deps.
  python3 - "$json_out" "$SNAPSHOTS_FILE" <<'PYEOF'
import json, sys, datetime, os

raw, snapshots_path = sys.argv[1], sys.argv[2]
try:
    data = json.loads(raw)
except json.JSONDecodeError as e:
    print(f"  WARN: could not parse telemetry-ratio JSON: {e}", file=sys.stderr)
    sys.exit(0)

ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
os.makedirs(os.path.dirname(snapshots_path), exist_ok=True)

lines = []
for surface in data.get("surfaces", []):
    entry = {
        "ts": ts,
        "source": "telemetry-ratio",
        "window_hours": data.get("window_hours", 24),
        "surface": surface["surface"],
        "inputs": surface["inputs"],
        "outputs": surface["outputs"],
        "ratio_pct": surface["ratio_pct"],
        "status": surface["status"],
    }
    # Carry deduplicated counts when present (boi surface uses these)
    if surface.get("unique_inputs") is not None:
        entry["unique_inputs"] = surface["unique_inputs"]
        entry["unique_outputs"] = surface["unique_outputs"]
        entry["ratio_pct"] = surface["ratio_pct"]  # already computed from uniques
    lines.append(json.dumps(entry))

tmp = snapshots_path + ".tmp"
with open(tmp, "a") as f:
    f.write("\n".join(lines) + "\n")
os.replace(tmp, snapshots_path)

boi = next((s for s in data.get("surfaces", []) if s["surface"] == "boi"), None)
if boi and boi.get("unique_inputs") is not None:
    print(f"  telemetry-ratio snapshot written: boi {boi['ratio_pct']}% ({boi['unique_inputs']} unique inputs → {boi['unique_outputs']} unique outputs) [{boi['status']}]")
else:
    print(f"  telemetry-ratio snapshot written ({len(lines)} surfaces)")
PYEOF
}

bold "══ User-Outcome Metrics ══"

run_metric "frustration-signals"      "$METRICS_DIR/frustration-signals.py"
run_metric "feedback-recurrence"      "$METRICS_DIR/feedback-recurrence.py"
run_metric "loop-waste-detection"     "$METRICS_DIR/loop-waste-detection.py"
run_metric "done-claim-verification"  "$METRICS_DIR/done-claim-verification.py"
run_metric "context-continuity"       "$METRICS_DIR/context-continuity.py"

bold ""
bold "══ Telemetry Ratio Snapshot ══"
snapshot_telemetry_ratio

echo ""
if [ $OVERALL -eq 0 ]; then
  green "Overall: PASS"
else
  red "Overall: FAIL"
fi

exit $OVERALL
