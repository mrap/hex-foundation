#!/usr/bin/env bats
# Fixture tests for hex-doctor check_68 (hex binary version-sync).
# Simulates a stale PATH hex binary vs foundation repo Cargo.toml.

HEX_DOCTOR_SCRIPT="${BATS_TEST_DIRNAME}/../system/scripts/hex-doctor"

# ── Helpers ──────────────────────────────────────────────────────────────────

make_path_hex() {
  local version="$1"
  local bin_dir="$FAKE_HEX/path-bin"
  mkdir -p "$bin_dir"
  cat > "$bin_dir/hex" << SCRIPT
#!/bin/bash
case "\$1" in
  --version) echo "hex $version"; exit 0 ;;
  events)    exit 0 ;;
  *)         exit 0 ;;
esac
SCRIPT
  chmod +x "$bin_dir/hex"
  export PATH="$bin_dir:$PATH"
}

make_foundation_cargo_toml() {
  local version="$1"
  local foundation_dir="$FAKE_HEX/foundation"
  mkdir -p "$foundation_dir/system/harness"
  printf '[package]\nname = "hex"\nversion = "%s"\n' "$version" \
    > "$foundation_dir/system/harness/Cargo.toml"
  export HEX_FOUNDATION_DIR="$foundation_dir"
}

# ── Setup / teardown ─────────────────────────────────────────────────────────

setup() {
  FAKE_HEX=$(mktemp -d)
  mkdir -p "$FAKE_HEX/.hex/scripts/doctor-checks"
  mkdir -p "$FAKE_HEX/.hex/scripts"
  export HEX_DIR="$FAKE_HEX"
  ORIG_PATH="$PATH"
}

teardown() {
  unset HEX_DIR HEX_FOUNDATION_DIR
  export PATH="$ORIG_PATH"
  rm -rf "$FAKE_HEX"
}

# ── Tests ─────────────────────────────────────────────────────────────────────

@test "check_68: stale binary (binary != Cargo.toml) → error exit and both versions in output" {
  make_path_hex "0.5.0"
  make_foundation_cargo_toml "0.6.0"

  run bash "$HEX_DOCTOR_SCRIPT"

  [ "$status" -eq 1 ]
  echo "$output" | grep -q "hex-binary version-sync"
  echo "$output" | grep -q "0.5.0"
  echo "$output" | grep -q "0.6.0"
}

@test "check_68: matching versions → PASS in output" {
  make_path_hex "0.6.0"
  make_foundation_cargo_toml "0.6.0"

  run bash "$HEX_DOCTOR_SCRIPT"

  echo "$output" | grep -q "hex-binary version-sync: hex binary == Cargo.toml == 0.6.0"
}

@test "check_68: no foundation Cargo.toml, falls back to installed → pass when versions match" {
  make_path_hex "0.7.0"
  # Point HEX_FOUNDATION_DIR at a path that doesn't exist so check_68 falls back to installed toml
  export HEX_FOUNDATION_DIR="$FAKE_HEX/nonexistent-foundation"
  mkdir -p "$FAKE_HEX/.hex/harness"
  printf '[package]\nname = "hex"\nversion = "0.7.0"\n' \
    > "$FAKE_HEX/.hex/harness/Cargo.toml"

  run bash "$HEX_DOCTOR_SCRIPT"

  echo "$output" | grep -q "hex-binary version-sync: hex binary == Cargo.toml == 0.7.0"
}

@test "check_68: hex not on PATH → error exit" {
  make_foundation_cargo_toml "0.6.0"
  # PATH intentionally has no hex binary

  run bash "$HEX_DOCTOR_SCRIPT"

  [ "$status" -eq 1 ]
  echo "$output" | grep -q "hex-binary version-sync"
}
