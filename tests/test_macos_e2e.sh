#!/usr/bin/env bash
set -euo pipefail

# hex macOS E2E Test — Hermetic test using Tart macOS VMs
#
# Prerequisites:
#   brew install cirruslabs/cli/tart
#   tart clone ghcr.io/cirruslabs/macos-sequoia-base:latest hex-test-base
#
# Usage:
#   bash tests/test_macos_e2e.sh
#
# What it does:
#   1. Clones a fresh VM from the base image (instant, copy-on-write)
#   2. Copies the hex repo into the VM
#   3. Runs install.sh inside the VM
#   4. Verifies the install
#   5. Tears down the VM (clean slate)

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
VM_NAME="hex-e2e-$(date +%s)"
BASE_IMAGE="hex-test-base"
SSH_USER="admin"
SSH_PASS="admin"
VM_IP=""

cleanup() {
    echo ""
    echo "Cleaning up VM: $VM_NAME"
    tart stop "$VM_NAME" 2>/dev/null || true
    tart delete "$VM_NAME" 2>/dev/null || true
}
trap cleanup EXIT

# ── Step 1: Clone a fresh VM ──────────────────────────────────────

echo "=== hex macOS E2E Test ==="
echo ""

if ! command -v tart &>/dev/null; then
    echo "ERROR: tart not installed. Run: brew install cirruslabs/cli/tart"
    exit 1
fi

if ! tart list | grep -q "$BASE_IMAGE"; then
    echo "ERROR: Base image '$BASE_IMAGE' not found."
    echo "Run: tart clone ghcr.io/cirruslabs/macos-sequoia-base:latest $BASE_IMAGE"
    exit 1
fi

echo "[1/6] Cloning fresh VM from $BASE_IMAGE..."
tart clone "$BASE_IMAGE" "$VM_NAME"
echo "  VM: $VM_NAME"

# ── Step 2: Start VM and get IP ───────────────────────────────────

echo "[2/6] Starting VM..."
tart run --no-graphics "$VM_NAME" &
VM_PID=$!

# Wait for VM to boot and get an IP
echo "  Waiting for VM to boot..."
for i in $(seq 1 60); do
    VM_IP=$(tart ip "$VM_NAME" 2>/dev/null || true)
    if [ -n "$VM_IP" ]; then
        break
    fi
    sleep 2
done

if [ -z "$VM_IP" ]; then
    echo "  FAIL: VM did not get an IP after 120s"
    exit 1
fi
echo "  VM IP: $VM_IP"

# Wait for SSH to be ready
echo "  Waiting for SSH..."
SSH_OPTS="-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o PreferredAuthentications=password -o PubkeyAuthentication=no"
for i in $(seq 1 30); do
    if sshpass -p "$SSH_PASS" ssh $SSH_OPTS -o ConnectTimeout=2 "$SSH_USER@$VM_IP" "echo ready" 2>/dev/null | grep -q ready; then
        break
    fi
    sleep 2
done


# Helper: run command in VM via SSH
vm_run() {
    sshpass -p "$SSH_PASS" ssh $SSH_OPTS "$SSH_USER@$VM_IP" "$@"
}

vm_scp() {
    sshpass -p "$SSH_PASS" scp $SSH_OPTS -r "$@"
}

# Verify SSH works
if ! vm_run "echo 'SSH connected'" 2>/dev/null | grep -q "SSH connected"; then
    echo "  FAIL: Cannot SSH into VM"
    exit 1
fi
echo "  SSH ready"

# ── Step 3: Copy repo into VM ─────────────────────────────────────

echo "[3/6] Copying hex repo into VM..."
vm_run "mkdir -p /tmp/hex-setup"
# Pipe tar through SSH (scp breaks with sshpass on newer macOS)
tar -C "$REPO_DIR" -czf - --exclude='.git' --exclude='__pycache__' --exclude='.pytest_cache' . \
    | vm_run "tar -C /tmp/hex-setup -xzf -"
echo "  Repo copied"

# Check VM Python version
echo "  VM Python: $(vm_run 'python3 --version 2>&1' || echo 'NOT FOUND')"

# ── Step 4: Run install ───────────────────────────────────────────

echo "[4/6] Running install.sh inside VM..."
INSTALL_OUTPUT=$(vm_run "bash /tmp/hex-setup/install.sh /tmp/test-hex" 2>&1 || true)
echo "$INSTALL_OUTPUT" | tail -8
echo ""

# ── Step 5: Verify ────────────────────────────────────────────────

echo "[5/6] Running verification..."
PASS=0
FAIL=0

run_check() {
    local name="$1"
    shift
    if vm_run "$@" >/dev/null 2>&1; then
        echo "  PASS: $name"
        PASS=$((PASS + 1))
    else
        echo "  FAIL: $name"
        FAIL=$((FAIL + 1))
    fi
}

# Directory structure
run_check "CLAUDE.md exists" "test -f /tmp/test-hex/CLAUDE.md"
run_check "AGENTS.md exists" "test -f /tmp/test-hex/AGENTS.md"
run_check "me/me.md exists" "test -f /tmp/test-hex/me/me.md"
run_check "memory.db exists" "test -f /tmp/test-hex/.hex/memory.db"
run_check "todo.md exists" "test -f /tmp/test-hex/todo.md"
run_check "projects/ exists" "test -d /tmp/test-hex/projects"
run_check "evolution/ exists" "test -d /tmp/test-hex/evolution"

# Onboarding trigger
run_check "Onboarding placeholder" "grep -q 'Your name here' /tmp/test-hex/me/me.md"

# Zone markers
run_check "CLAUDE.md system-start" "grep -q 'hex:system-start' /tmp/test-hex/CLAUDE.md"
run_check "CLAUDE.md user-start" "grep -q 'hex:user-start' /tmp/test-hex/CLAUDE.md"

# Memory round-trip
vm_run "cd /tmp/test-hex && python3 .hex/skills/memory/scripts/memory_save.py 'macos test sentinel' --tags 'e2e'" >/dev/null 2>&1
SEARCH_OUT=$(vm_run "cd /tmp/test-hex && python3 .hex/skills/memory/scripts/memory_search.py 'macos sentinel' --compact" 2>&1)
if echo "$SEARCH_OUT" | grep -q "sentinel"; then
    echo "  PASS: Memory save + search round-trip"
    PASS=$((PASS + 1))
else
    echo "  FAIL: Memory save + search round-trip"
    echo "  Output: $SEARCH_OUT"
    FAIL=$((FAIL + 1))
fi

# Memory index
vm_run "cd /tmp/test-hex && python3 .hex/skills/memory/scripts/memory_index.py" >/dev/null 2>&1
INDEX_STATS=$(vm_run "cd /tmp/test-hex && python3 .hex/skills/memory/scripts/memory_index.py --stats" 2>&1)
if echo "$INDEX_STATS" | grep -q "Files indexed:"; then
    echo "  PASS: Memory index + stats"
    PASS=$((PASS + 1))
else
    echo "  FAIL: Memory index + stats"
    FAIL=$((FAIL + 1))
fi

# Install registry
run_check "~/.hex-install.json exists" "test -f ~/.hex-install.json"

# Re-install guard
REINSTALL_OUT=$(vm_run "bash /tmp/hex-setup/install.sh /tmp/test-hex" 2>&1 || true)
if echo "$REINSTALL_OUT" | grep -q "already exists"; then
    echo "  PASS: Re-install blocked"
    PASS=$((PASS + 1))
else
    echo "  FAIL: Re-install blocked"
    FAIL=$((FAIL + 1))
fi

# No personal refs
if vm_run "grep -qi 'mike\|rapadas\|whitney\|hermes\|nanoclaw\|mrap' /tmp/test-hex/CLAUDE.md" 2>/dev/null; then
    echo "  FAIL: Personal references in CLAUDE.md"
    FAIL=$((FAIL + 1))
else
    echo "  PASS: No personal references"
    PASS=$((PASS + 1))
fi

# macOS-specific: verify native Python works
PYVER=$(vm_run "python3 --version" 2>&1)
echo "  INFO: VM Python version: $PYVER"

# ── Step 6: Results ───────────────────────────────────────────────

TOTAL=$((PASS + FAIL))
echo ""
echo "[6/6] Results"
echo "========================================="
echo " $PASS passed, $FAIL failed ($TOTAL total)"
echo " Platform: macOS VM via Tart"
echo "========================================="

if [ "$FAIL" -gt 0 ]; then
    exit 1
fi
echo ""
echo "=== ALL macOS TESTS PASSED ==="
