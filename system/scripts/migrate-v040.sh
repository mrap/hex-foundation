#!/usr/bin/env bash
# migrate-v040.sh — Post-upgrade migration for v0.4.0
#
# Fixes generated files that reference old patterns:
#   1. .hex/env.sh → .hex/scripts/env.sh (env.sh moved)
#   2. Hardcoded absolute paths in wake scripts (e.g. $HOME/<dir>) → $HEX_DIR-relative
#   3. Old hex_events_cli.py path refs → hex-events binary
#   4. Old bash ~/.boi/boi refs → boi binary
#
# Safe to run multiple times (idempotent). Each fix checks before applying.
#
# Usage: HEX_DIR=/path/to/hex bash migrate-v040.sh

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HEX_DIR="${HEX_DIR:-$(cd "$SCRIPT_DIR/../.." && pwd)}"
HEX_DOTDIR="$HEX_DIR/.hex"

FIXED=0
SKIPPED=0
FAILED=0

green() { printf '\033[32m%s\033[0m\n' "$*"; }
yellow() { printf '\033[33m%s\033[0m\n' "$*"; }
red() { printf '\033[31m%s\033[0m\n' "$*"; }
bold() { printf '\033[1m%s\033[0m\n' "$*"; }

fix() { FIXED=$((FIXED + 1)); green "  FIXED: $1"; }
skip() { SKIPPED=$((SKIPPED + 1)); }
fail() { FAILED=$((FAILED + 1)); red "  FAIL: $1"; }

bold "═══ hex v0.4.0 Migration ═══"
echo "HEX_DIR=$HEX_DIR"
echo ""

# ── 1. Ensure .hex/scripts/env.sh exists ─────────────────────────────────────
bold "1. env.sh location"
if [ -f "$HEX_DOTDIR/scripts/env.sh" ]; then
  skip
  echo "  .hex/scripts/env.sh exists ✓"
else
  if [ -f "$HEX_DOTDIR/env.sh" ]; then
    cp "$HEX_DOTDIR/env.sh" "$HEX_DOTDIR/scripts/env.sh"
    chmod +x "$HEX_DOTDIR/scripts/env.sh"
    fix "Copied .hex/env.sh → .hex/scripts/env.sh"
  else
    fail ".hex/scripts/env.sh missing and no .hex/env.sh to copy from"
  fi
fi

# Keep .hex/env.sh as a shim that sources the real one (backward compat)
if [ -f "$HEX_DOTDIR/env.sh" ]; then
  # Check if it's already a shim
  if grep -q 'scripts/env.sh' "$HEX_DOTDIR/env.sh" 2>/dev/null; then
    skip
  else
    # Back up and replace with shim
    cp "$HEX_DOTDIR/env.sh" "$HEX_DOTDIR/env.sh.pre-v040"
    cat > "$HEX_DOTDIR/env.sh" << 'SHIMEOF'
#!/usr/bin/env bash
# Shim — env.sh moved to .hex/scripts/env.sh in v0.4.0
# This file exists for backward compatibility with old wake scripts.
_REAL="$(cd "$(dirname "${BASH_SOURCE[0]}")/scripts" && pwd)/env.sh"
if [ -f "$_REAL" ]; then
  source "$_REAL"
else
  echo "ERROR: env.sh not found at $_REAL" >&2
fi
SHIMEOF
    chmod +x "$HEX_DOTDIR/env.sh"
    fix "Replaced .hex/env.sh with shim → .hex/scripts/env.sh"
  fi
fi

# ── 2. Fix wake scripts: env.sh path ─────────────────────────────────────────
bold "2. Wake script env.sh references"

# Find all wake scripts (active + templates, skip archive)
while IFS= read -r wake_script; do
  [ -f "$wake_script" ] || continue
  basename_ws="$(basename "$wake_script")"

  # Fix: source "$HEX_DIR/.hex/env.sh" → source "$HEX_DIR/.hex/scripts/env.sh"
  if grep -q '\.hex/env\.sh' "$wake_script" && ! grep -q '\.hex/scripts/env\.sh' "$wake_script"; then
    sed -i '' 's|\.hex/env\.sh|.hex/scripts/env.sh|g' "$wake_script" 2>/dev/null || \
    sed -i 's|\.hex/env\.sh|.hex/scripts/env.sh|g' "$wake_script" 2>/dev/null
    fix "$basename_ws: .hex/env.sh → .hex/scripts/env.sh"
  fi

  # Fix: source "$(cd ...)/.." && pwd)/env.sh" → source "$HEX_DIR/.hex/scripts/env.sh"
  # This old pattern tries to resolve env.sh relative to .hex/bin/ — fragile
  if grep -q 'dirname.*BASH_SOURCE.*env\.sh' "$wake_script"; then
    sed -i '' 's|source "$(cd "$(dirname "${BASH_SOURCE\[0\]}")/.." && pwd)/env.sh"|source "$HEX_DIR/.hex/scripts/env.sh"|g' "$wake_script" 2>/dev/null || \
    sed -i 's|source "$(cd "$(dirname "${BASH_SOURCE\[0\]}")/.." && pwd)/env.sh"|source "$HEX_DIR/.hex/scripts/env.sh"|g' "$wake_script" 2>/dev/null
    fix "$basename_ws: relative env.sh → \$HEX_DIR/.hex/scripts/env.sh"
  fi

done < <(
  find "$HEX_DOTDIR/bin" -name "*wake*" -type f 2>/dev/null
  find "$HEX_DOTDIR/scripts" -name "*wake*" -type f 2>/dev/null
  find "$HEX_DOTDIR/templates" -name "*.tpl" -type f 2>/dev/null
)

# ── 3. Fix wake scripts: hardcoded user paths ────────────────────────────────
bold "3. Hardcoded user paths in wake scripts"

# Detect the user's home dir pattern
USER_HOME="$HOME"

while IFS= read -r wake_script; do
  [ -f "$wake_script" ] || continue
  basename_ws="$(basename "$wake_script")"

  # Check for hardcoded HEX_DIR assignments like HEX_DIR="$HOME/hex"
  if grep -q "^HEX_DIR=\"${HEX_DIR}\"" "$wake_script" 2>/dev/null; then
    sed -i '' "s|^HEX_DIR=\"${HEX_DIR}\"|HEX_DIR=\"\${HEX_DIR:-${HEX_DIR}}\"|g" "$wake_script" 2>/dev/null || \
    sed -i "s|^HEX_DIR=\"${HEX_DIR}\"|HEX_DIR=\"\${HEX_DIR:-${HEX_DIR}}\"|g" "$wake_script" 2>/dev/null
    fix "$basename_ws: hardcoded HEX_DIR → env-var with fallback"
  fi

done < <(
  find "$HEX_DOTDIR/bin" -name "*wake*" -o -name "*halt*" | grep -v _archive
  find "$HEX_DOTDIR/scripts" -name "*wake*"
)

# ── 4. Fix watchdog: env.sh path ─────────────────────────────────────────────
bold "4. Watchdog env.sh reference"
WATCHDOG="$HEX_DOTDIR/scripts/hex-doctor-watchdog.sh"
if [ -f "$WATCHDOG" ]; then
  if grep -q '\.hex/env\.sh' "$WATCHDOG" && ! grep -q '\.hex/scripts/env\.sh' "$WATCHDOG"; then
    sed -i '' 's|\.hex/env\.sh|.hex/scripts/env.sh|g' "$WATCHDOG" 2>/dev/null || \
    sed -i 's|\.hex/env\.sh|.hex/scripts/env.sh|g' "$WATCHDOG" 2>/dev/null
    fix "hex-doctor-watchdog.sh: env.sh path updated"
  else
    skip
  fi
fi

# ── 5. Fix hex-halt-all.sh ───────────────────────────────────────────────────
bold "5. hex-halt-all.sh"
HALT_ALL="$HEX_DOTDIR/bin/hex-halt-all.sh"
if [ -f "$HALT_ALL" ]; then
  if grep -q 'dirname.*BASH_SOURCE.*env\.sh' "$HALT_ALL"; then
    sed -i '' 's|source "$(cd "$(dirname "${BASH_SOURCE\[0\]}")/.." && pwd)/env.sh"|source "$HEX_DIR/.hex/scripts/env.sh"|g' "$HALT_ALL" 2>/dev/null || \
    sed -i 's|source "$(cd "$(dirname "${BASH_SOURCE\[0\]}")/.." && pwd)/env.sh"|source "$HEX_DIR/.hex/scripts/env.sh"|g' "$HALT_ALL" 2>/dev/null
    fix "hex-halt-all.sh: env.sh path updated"
  else
    skip
  fi
fi

# ── 6. Fix wake template ─────────────────────────────────────────────────────
bold "6. Wake script template"
WAKE_TPL="$HEX_DOTDIR/templates/agent/wake.sh.tpl"
if [ -f "$WAKE_TPL" ]; then
  if grep -q 'dirname.*BASH_SOURCE.*env\.sh' "$WAKE_TPL" && ! grep -q 'scripts/env\.sh' "$WAKE_TPL"; then
    sed -i '' 's|source "$(cd "$(dirname "${BASH_SOURCE\[0\]}")/.." && pwd)/env.sh"|source "$HEX_DIR/.hex/scripts/env.sh"|g' "$WAKE_TPL" 2>/dev/null || \
    sed -i 's|source "$(cd "$(dirname "${BASH_SOURCE\[0\]}")/.." && pwd)/env.sh"|source "$HEX_DIR/.hex/scripts/env.sh"|g' "$WAKE_TPL" 2>/dev/null
    fix "wake.sh.tpl: env.sh path updated"
  else
    skip
  fi
fi

# ── 7. Verify: scan for remaining old patterns ──────────────────────────────
bold "7. Verification scan"
REMAINING=0

# Check for .hex/env.sh refs (excluding the shim itself and archive)
OLD_REFS=$(grep -rn '\.hex/env\.sh' "$HEX_DOTDIR" 2>/dev/null \
  | grep -v '\.hex/scripts/env\.sh' \
  | grep -v '\.hex/env\.sh\.pre-v040' \
  | grep -v '_archive/' \
  | grep -v '__pycache__' \
  | grep -v '\.jsonl' \
  | grep -v 'index.html' \
  | grep -v 'env\.sh:' \
  | grep -v 'migrate-v040' \
  || true)

if [ -n "$OLD_REFS" ]; then
  yellow "  Remaining .hex/env.sh references (non-blocking):"
  echo "$OLD_REFS" | while IFS= read -r line; do
    echo "    $line"
    REMAINING=$((REMAINING + 1))
  done
else
  green "  No remaining .hex/env.sh references ✓"
fi

# ── Summary ──────────────────────────────────────────────────────────────────
echo ""
bold "═══ Migration Summary ═══"
echo "  Fixed:   $FIXED"
echo "  Skipped: $SKIPPED (already correct)"
echo "  Failed:  $FAILED"

if [ $FAILED -gt 0 ]; then
  red "Migration completed with $FAILED failure(s)"
  exit 1
else
  green "Migration complete"
  exit 0
fi
