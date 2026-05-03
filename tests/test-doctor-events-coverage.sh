#!/usr/bin/env bash
# Test: hex-doctor check_66 absorbs hex events status parse failures.
#
# Creates a temp HEX_DIR with a stub hex binary and asserts:
#   - Broken policies  → doctor FAILs, path + error named in output
#   - Clean policies   → events check PASSes, policy count shown
#   - Daemon down      → doctor FAILs with error indicator
#   - Missing binary   → WARN only (not hard fail)
#
# No Docker needed; stubs the hex binary entirely.
set -uo pipefail

PASS=0
FAIL=0
TOTAL=0

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HEX_DOCTOR="$SCRIPT_DIR/../system/scripts/hex-doctor"

# ── Helpers ───────────────────────────────────────────────────────────────────

assert_exit() {
  local name="$1" expected="$2" actual="$3"
  TOTAL=$((TOTAL + 1))
  if [ "$actual" -eq "$expected" ]; then
    echo "  PASS: $name"
    PASS=$((PASS + 1))
  else
    echo "  FAIL: $name (exit $actual, expected $expected)"
    FAIL=$((FAIL + 1))
  fi
}

assert_contains() {
  local name="$1" pattern="$2" output="$3"
  TOTAL=$((TOTAL + 1))
  if printf '%s' "$output" | grep -q "$pattern"; then
    echo "  PASS: $name"
    PASS=$((PASS + 1))
  else
    echo "  FAIL: $name (pattern '$pattern' not found in output)"
    FAIL=$((FAIL + 1))
    printf '%s' "$output" | tail -10 | sed 's/^/    /' >&2
  fi
}

assert_not_contains() {
  local name="$1" pattern="$2" output="$3"
  TOTAL=$((TOTAL + 1))
  if ! printf '%s' "$output" | grep -q "$pattern"; then
    echo "  PASS: $name"
    PASS=$((PASS + 1))
  else
    echo "  FAIL: $name (unexpected pattern '$pattern' found in output)"
    FAIL=$((FAIL + 1))
  fi
}

run_doctor() {
  # Run hex-doctor under a fake HEX_DIR; return output via stdout, exit code via DOCTOR_EXIT.
  # Cannot use ||true here — that would mask the exit code.
  DOCTOR_EXIT=0
  DOCTOR_OUT=$(HEX_DIR="$1" bash "$HEX_DOCTOR" 2>&1) || DOCTOR_EXIT=$?
}

# ── Setup ─────────────────────────────────────────────────────────────────────

FAKE_HEX=$(mktemp -d)
trap 'rm -rf "$FAKE_HEX"' EXIT

# .hex/bin/hex             — stub binary  (replaced per test)
# .hex/scripts/doctor.sh   — no-op stub to suppress installation-skip WARN
# .hex/scripts/doctor-checks/  — triggers the check_66 block in hex-doctor
mkdir -p "$FAKE_HEX/.hex/bin"
mkdir -p "$FAKE_HEX/.hex/scripts/doctor-checks"

# Minimal doctor.sh stub: outputs the Summary line hex-doctor greps for,
# with zero errors/warnings so it doesn't pollute the global counters.
cat > "$FAKE_HEX/.hex/scripts/doctor.sh" << 'DOCEOF'
#!/bin/bash
echo ""
echo "hex-doctor-stub"
echo "  ─────────────────────────────────────────"
echo "  [PASS] stub: ok"
echo "  Summary: 1 passed, 0 warnings, 0 errors, 0 fixed"
DOCEOF
chmod +x "$FAKE_HEX/.hex/scripts/doctor.sh"

echo "=== test-doctor-events-coverage ==="
echo ""

# ── Test 1: Broken policy — parse failure detected ────────────────────────────
echo "[1] Broken policy: parse failure detection"

cat > "$FAKE_HEX/.hex/bin/hex" << 'HEXEOF'
#!/bin/bash
if [ "$1" = "events" ] && [ "$2" = "status" ]; then
  printf 'events: failed to parse "/tmp/fake-policies/broken.yaml": duplicate field `timeout` at line 5 column 9\n' >&2
  printf 'events: loaded 0 policies from "/tmp/fake-policies"\n' >&2
  printf 'hex events status\n  policies loaded:  0\n'
fi
HEXEOF
chmod +x "$FAKE_HEX/.hex/bin/hex"

run_doctor "$FAKE_HEX"
assert_exit     "exit code is 1 (errors)"                  1              "$DOCTOR_EXIT"
assert_contains "output contains ERROR"                    "ERROR"        "$DOCTOR_OUT"
assert_contains "failing policy path named in output"      "broken.yaml"  "$DOCTOR_OUT"
assert_contains "parse error message present in output"    "duplicate field" "$DOCTOR_OUT"

# ── Test 2: Multiple broken policies — all named ──────────────────────────────
echo ""
echo "[2] Multiple broken policies: all paths + errors listed"

cat > "$FAKE_HEX/.hex/bin/hex" << 'HEXEOF'
#!/bin/bash
if [ "$1" = "events" ] && [ "$2" = "status" ]; then
  printf 'events: failed to parse "/hex/policies/boi-daemon-watchdog.yaml": duplicate field `timeout` at line 19 column 9\n' >&2
  printf 'events: failed to parse "/hex/policies/goal-alignment.yaml": missing field `field` at line 21 column 9\n' >&2
  printf 'events: failed to parse "/hex/policies/hex-v2-exp-agent.yaml": duplicate field `timeout` at line 26 column 9\n' >&2
  printf 'events: loaded 124 policies from "/hex/policies"\n' >&2
  printf 'hex events status\n  policies loaded:  124\n'
fi
HEXEOF
chmod +x "$FAKE_HEX/.hex/bin/hex"

run_doctor "$FAKE_HEX"
assert_exit     "exit code is 1 (errors)"                          1                       "$DOCTOR_EXIT"
assert_contains "first failing policy named"                        "boi-daemon-watchdog"   "$DOCTOR_OUT"
assert_contains "second failing policy named"                       "goal-alignment"        "$DOCTOR_OUT"
assert_contains "third failing policy named"                        "hex-v2-exp-agent"      "$DOCTOR_OUT"
assert_contains "first error message present"                       "duplicate field"       "$DOCTOR_OUT"
assert_contains "second error message present (missing field)"      "missing field"         "$DOCTOR_OUT"

# ── Test 3: Happy path — clean policies → events check PASSES ─────────────────
echo ""
echo "[3] Happy path: all policies valid"

cat > "$FAKE_HEX/.hex/bin/hex" << 'HEXEOF'
#!/bin/bash
if [ "$1" = "events" ] && [ "$2" = "status" ]; then
  printf 'events: loaded 5 policies from "/tmp/fake-policies"\n' >&2
  printf 'hex events status\n  policies loaded:  5\n'
fi
HEXEOF
chmod +x "$FAKE_HEX/.hex/bin/hex"

run_doctor "$FAKE_HEX"
# exit 2 = warnings from unrelated checks (e.g. version-sync Cargo.toml missing in fake env)
# exit 0 = all pass; either is acceptable — must NOT be 1 (errors)
TOTAL=$((TOTAL + 1))
if [ "$DOCTOR_EXIT" -ne 1 ]; then
  echo "  PASS: exit code is not 1 (no errors from events check)"
  PASS=$((PASS + 1))
else
  echo "  FAIL: exit code is 1 (unexpected error in happy path)"
  FAIL=$((FAIL + 1))
fi
assert_contains     "events check PASS in output"          "PASS"        "$DOCTOR_OUT"
assert_contains     "policy count shown in output"         "5 policies"  "$DOCTOR_OUT"
assert_not_contains "no ERROR in output"                   "ERROR"       "$DOCTOR_OUT"

# ── Test 4: Hex binary missing → WARN (not hard fail) ────────────────────────
echo ""
echo "[4] Missing hex binary: warns but does not hard-error"

rm -f "$FAKE_HEX/.hex/bin/hex"

run_doctor "$FAKE_HEX"
assert_contains     "output contains WARN for missing binary"  "WARN"   "$DOCTOR_OUT"
assert_not_contains "no ERROR for missing binary"              "ERROR"  "$DOCTOR_OUT"
# exit 2 = warnings only (acceptable); 0 = all pass also acceptable if WARN is suppressed
# Either way: must NOT be 1 (errors)
TOTAL=$((TOTAL + 1))
if [ "$DOCTOR_EXIT" -ne 1 ]; then
  echo "  PASS: exit code is not 1 (no hard error for missing binary)"
  PASS=$((PASS + 1))
else
  echo "  FAIL: exit code is 1 (unexpected hard error for missing binary)"
  FAIL=$((FAIL + 1))
fi

# Restore hex binary for test 5
cat > "$FAKE_HEX/.hex/bin/hex" << 'HEXEOF'
#!/bin/bash
if [ "$1" = "events" ] && [ "$2" = "status" ]; then
  printf 'daemon not running\n' >&2
  exit 1
fi
HEXEOF
chmod +x "$FAKE_HEX/.hex/bin/hex"

# ── Test 5: Non-zero exit from hex binary (daemon down) ───────────────────────
echo ""
echo "[5] Daemon down: hex events status exits non-zero"

run_doctor "$FAKE_HEX"
assert_exit     "exit code is 1 (errors)"                   1       "$DOCTOR_EXIT"
assert_contains "ERROR in output for failed hex command"    "ERROR"  "$DOCTOR_OUT"

# ── Summary ───────────────────────────────────────────────────────────────────
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  Results: $PASS passed, $FAIL failed, $TOTAL total"
echo ""

if [ "$FAIL" -eq 0 ]; then
  echo "  All tests PASS"
  exit 0
else
  echo "  $FAIL test(s) FAILED"
  exit 1
fi
