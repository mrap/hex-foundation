#!/usr/bin/env bash
# sync-secrets.sh — load every secret in .hex/secrets/*.env into the hex runtime surfaces.
#
# Purpose: one canonical place (~/<hex-workspace>/.hex/secrets/*.env) holds every API key.
# Adding a new key is a two-step: (1) drop a new .env file in that dir, (2) run this.
#
# What this does:
#   1. `launchctl setenv KEY VAL` for every KEY=VAL line in .hex/secrets/*.env
#      → makes them visible to user-launched processes + new launchd services
#   2. Rewrites cc-connect's launchd plist EnvironmentVariables block so the
#      cc-connect daemon (and every Claude session it spawns) inherits them
#   3. Kickstarts com.cc-connect.service so the plist change takes effect
#
# Idempotent. Run anytime a key is added, updated, or removed.
# Hex-core — do not auto-mutate (Red tier).

set -euo pipefail

HEX_DIR="${CLAUDE_PROJECT_DIR:-${HEX_DIR:-$HOME/hex}}"
SECRETS_DIR="$HEX_DIR/.hex/secrets"
PLIST="$HOME/Library/LaunchAgents/com.cc-connect.service.plist"
SERVICE_LABEL="com.cc-connect.service"

if [ ! -d "$SECRETS_DIR" ]; then
  echo "ERR: $SECRETS_DIR does not exist" >&2
  exit 1
fi
if [ ! -f "$PLIST" ]; then
  echo "WARN: $PLIST missing — will only update launchctl user env, not cc-connect plist"
fi

# --- 1. Collect every KEY=VAL from secrets/*.env (skip comments + blank) ---
declare -a KEYS VALS
while IFS= read -r line; do
  [[ -z "$line" || "$line" =~ ^[[:space:]]*# ]] && continue
  if [[ "$line" =~ ^[[:space:]]*([A-Z_][A-Z0-9_]*)=(.*)$ ]]; then
    KEYS+=("${BASH_REMATCH[1]}")
    VALS+=("${BASH_REMATCH[2]}")
  fi
done < <(cat "$SECRETS_DIR"/*.env 2>/dev/null || true)

if [ ${#KEYS[@]} -eq 0 ]; then
  echo "No secrets found in $SECRETS_DIR/*.env"
  exit 0
fi

echo "Loaded ${#KEYS[@]} secret(s) from $SECRETS_DIR:"
for k in "${KEYS[@]}"; do echo "  - $k"; done

# --- 2. Export into user launchctl env ---
for i in "${!KEYS[@]}"; do
  launchctl setenv "${KEYS[$i]}" "${VALS[$i]}"
done
echo "✓ launchctl setenv applied ($(launchctl getenv FAL_KEY > /dev/null 2>&1 && echo 'FAL_KEY visible' || echo 'FAL_KEY not visible'))"

# --- 3. Rewrite cc-connect plist EnvironmentVariables block ---
if [ -f "$PLIST" ]; then
  python3 - "$PLIST" "$SECRETS_DIR" <<'PY'
import plistlib, sys, pathlib, re

plist_path, secrets_dir = sys.argv[1], sys.argv[2]
with open(plist_path, "rb") as f:
    data = plistlib.load(f)

env = dict(data.get("EnvironmentVariables", {}))
# Preserve non-secret keys cc-connect itself sets (CC_LOG_*, PATH, etc)
# by leaving anything that's not in a secrets file alone. Only ADD/UPDATE
# secrets from .hex/secrets/*.env — never delete.
added, updated = [], []
for ef in sorted(pathlib.Path(secrets_dir).glob("*.env")):
    for line in ef.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = re.match(r"^([A-Z_][A-Z0-9_]*)=(.*)$", line)
        if not m:
            continue
        k, v = m.group(1), m.group(2)
        if k not in env:
            added.append(k)
        elif env[k] != v:
            updated.append(k)
        env[k] = v

data["EnvironmentVariables"] = env
with open(plist_path, "wb") as f:
    plistlib.dump(data, f)

print(f"Plist updated: +{len(added)} added, ~{len(updated)} updated, {len(env)} total env keys")
if added: print(f"  added: {', '.join(added)}")
if updated: print(f"  updated: {', '.join(updated)}")
PY
fi

# --- 4. Restart cc-connect so plist env change takes effect ---
if launchctl list "$SERVICE_LABEL" >/dev/null 2>&1; then
  launchctl kickstart -k "gui/$(id -u)/$SERVICE_LABEL"
  echo "✓ $SERVICE_LABEL restarted"
  # Confirm
  sleep 1
  new_pid=$(launchctl list "$SERVICE_LABEL" 2>/dev/null | awk '/PID/{print $3}' | tr -d ';')
  echo "  new PID: $new_pid"
else
  echo "SKIP: $SERVICE_LABEL not loaded"
fi

echo ""
echo "Done. New cc-connect sessions now inherit all secrets from $SECRETS_DIR."
