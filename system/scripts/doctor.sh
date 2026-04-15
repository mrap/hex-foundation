#!/usr/bin/env bash
# hex doctor — Validate hex installation health.
set -euo pipefail

HEX_DIR="${HEX_DIR:-$(pwd)}"
PASS=0
FAIL=0
WARN=0

check() {
    local name="$1"; shift
    if "$@" >/dev/null 2>&1; then
        echo "  ✓ $name"
        PASS=$((PASS + 1))
    else
        echo "  ✗ $name"
        FAIL=$((FAIL + 1))
    fi
}

warn_check() {
    local name="$1"; shift
    if "$@" >/dev/null 2>&1; then
        echo "  ✓ $name"
        PASS=$((PASS + 1))
    else
        echo "  ⚠ $name"
        WARN=$((WARN + 1))
    fi
}

echo "hex doctor"
echo "=========="
echo ""

# Core files
echo "Core files:"
check "CLAUDE.md exists" test -f "$HEX_DIR/CLAUDE.md"
check "AGENTS.md exists" test -f "$HEX_DIR/AGENTS.md"
check "todo.md exists" test -f "$HEX_DIR/todo.md"
check "me/me.md exists" test -f "$HEX_DIR/me/me.md"
check "me/learnings.md exists" test -f "$HEX_DIR/me/learnings.md"
echo ""

# System files
echo "System files:"
check ".hex/ exists" test -d "$HEX_DIR/.hex"
check "memory.db exists" test -f "$HEX_DIR/.hex/memory.db"
check "version.txt exists" test -f "$HEX_DIR/.hex/version.txt"
check "memory_search.py exists" test -f "$HEX_DIR/.hex/skills/memory/scripts/memory_search.py"
check "memory_save.py exists" test -f "$HEX_DIR/.hex/skills/memory/scripts/memory_save.py"
check "memory_index.py exists" test -f "$HEX_DIR/.hex/skills/memory/scripts/memory_index.py"
check "startup.sh exists" test -f "$HEX_DIR/.hex/scripts/startup.sh"
echo ""

# Directory structure
echo "Directories:"
for dir in me me/decisions projects people evolution landings raw raw/transcripts raw/handoffs specs; do
    check "$dir/" test -d "$HEX_DIR/$dir"
done
echo ""

# Commands (in .claude/commands/)
echo "Commands:"
warn_check "hex-startup command" test -f "$HEX_DIR/.claude/commands/hex-startup.md"
warn_check "hex-checkpoint command" test -f "$HEX_DIR/.claude/commands/hex-checkpoint.md"
warn_check "hex-shutdown command" test -f "$HEX_DIR/.claude/commands/hex-shutdown.md"
echo ""

# Memory database health
echo "Memory database:"
if [ -f "$HEX_DIR/.hex/memory.db" ]; then
    TABLES=$(python3 -c "
import sqlite3
conn = sqlite3.connect('$HEX_DIR/.hex/memory.db')
tables = {r[0] for r in conn.execute(\"SELECT name FROM sqlite_master WHERE type IN ('table','view')\").fetchall()}
for t in ['memories', 'memories_fts', 'chunks', 'files', 'metadata']:
    if t in tables:
        print(f'OK:{t}')
    else:
        print(f'MISSING:{t}')
conn.close()
" 2>/dev/null)
    while IFS= read -r line; do
        if [[ "$line" == OK:* ]]; then
            TABLE="${line#OK:}"
            echo "  ✓ table: $TABLE"
            PASS=$((PASS + 1))
        elif [[ "$line" == MISSING:* ]]; then
            TABLE="${line#MISSING:}"
            echo "  ✗ table: $TABLE"
            FAIL=$((FAIL + 1))
        fi
    done <<< "$TABLES"
else
    echo "  ✗ memory.db not found"
    FAIL=$((FAIL + 1))
fi
echo ""

# CLAUDE.md zone markers
echo "CLAUDE.md zones:"
if [ -f "$HEX_DIR/CLAUDE.md" ]; then
    check "system-start marker" grep -q "hex:system-start" "$HEX_DIR/CLAUDE.md"
    check "system-end marker" grep -q "hex:system-end" "$HEX_DIR/CLAUDE.md"
    check "user-start marker" grep -q "hex:user-start" "$HEX_DIR/CLAUDE.md"
    check "user-end marker" grep -q "hex:user-end" "$HEX_DIR/CLAUDE.md"
fi
echo ""

# Companions
echo "Companions:"
warn_check "BOI installed (~/.boi)" test -d "$HOME/.boi"
warn_check "hex-events installed (~/.hex-events)" test -d "$HOME/.hex-events"
echo ""

# Install registry
echo "Registry:"
warn_check "~/.hex-install.json exists" test -f "$HOME/.hex-install.json"
echo ""

# Summary
TOTAL=$((PASS + FAIL + WARN))
echo "=========="
echo "$PASS passed, $FAIL failed, $WARN warnings ($TOTAL checks)"
if [ "$FAIL" -gt 0 ]; then
    echo "Run install.sh to fix missing components."
    exit 1
else
    echo "hex is healthy."
fi
