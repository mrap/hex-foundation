# Personalization Audit — 2026-04-25

Comprehensive scan of `~/github.com/mrap/hex-foundation/` for hardcoded personalization,
user-specific identifiers, and machine-specific assumptions.

**Total violations: 31**

---

## Full Inventory

| File | Line | Pattern | Value | Fix |
|------|------|---------|-------|-----|
| system/commands/bet-status.md | 33 | hardcoded path | `/Users/mrap/mrap-hex` | use `HEX_DIR` env var or `$HOME` |
| system/skills/secret-intake/scripts/server.py | 12 | hardcoded path (default) | `"/Users/mrap/mrap-hex"` | use `os.environ.get("HEX_DIR", os.path.expanduser("~"))` or require env var |
| system/skills/secret-intake/SKILL.md | 88 | hardcoded path (example default) | `/Users/mrap/mrap-hex` | replace with generic `~/your-hex-dir` |
| system/scripts/consolidate.sh | 8 | hardcoded default dir | `$HOME/mrap-hex` | use `${HEX_DIR:-$HOME/hex}` |
| system/scripts/consolidate.sh | 22 | hardcoded Claude project path | `-Users-mrap-mrap-hex` | derive from `HEX_DIR` at runtime |
| system/scripts/consolidate.sh | 52 | hardcoded Claude project path | `-Users-mrap-mrap-hex` | derive from `HEX_DIR` at runtime |
| system/scripts/consolidate.sh | 53 | hardcoded Claude project path | `-Users-mrap-mrap-hex` | derive from `HEX_DIR` at runtime |
| system/scripts/release.sh | 119 | hardcoded default dir | `$HOME/mrap-hex` | use `${HEX_DIR:-$HOME/hex}` |
| system/reference/core-agents/boi-optimizer.yaml | 73 | Slack channel (personalized name) | `#from-mrap-hex` | use configurable channel `${SLACK_ESCALATION_CHANNEL:-#hex-escalations}` |
| system/reference/core-agents/boi-optimizer.yaml | 111 | Slack channel (personalized name) | `#from-mrap-hex` | use configurable channel `${SLACK_ESCALATION_CHANNEL:-#hex-escalations}` |
| system/reference/core-agents/hex-ops.yaml | 70 | Slack channel (personalized name) | `#from-mrap-hex` | use configurable channel `${SLACK_ESCALATION_CHANNEL:-#hex-escalations}` |
| system/reference/core-agents/fleet-lead.yaml | 79 | Slack channel (personalized name) | `#from-mrap-hex` | use configurable channel `${SLACK_ESCALATION_CHANNEL:-#hex-escalations}` |
| system/reference/core-agents/hex-autonomy.yaml | 75 | Slack channel (personalized name) | `#from-mrap-hex` | use configurable channel `${SLACK_ESCALATION_CHANNEL:-#hex-escalations}` |
| system/reference/core-agents/cos.yaml | 71 | Slack channel (personalized name) | `#from-mrap-hex` | use configurable channel `${SLACK_ESCALATION_CHANNEL:-#hex-escalations}` |
| system/scripts/hex-router/router.py | 5–9 | Tailscale hostname | `mac-mini.tailbd5748.ts.net` | use `${HEX_ROUTER_HOST:-localhost}` env var |
| system/scripts/hex-router/router.py | 81 | Tailscale reference in UI | `mac-mini` | use generic wording or env-driven hostname |
| system/skills/secret-intake/scripts/start.sh | 11 | Tailscale hostname | `mac-mini.tailbd5748.ts.net` | use `${HEX_HOST:-localhost}` env var |
| system/skills/secret-intake/scripts/start.sh | 22 | Tailscale hostname | `mac-mini.tailbd5748.ts.net` | use `${HEX_HOST:-localhost}` env var |
| system/skills/secret-intake/SKILL.md | 27 | Tailscale URL in docs | `mac-mini.tailbd5748.ts.net` | replace with `<your-tailscale-hostname>` |
| system/skills/secret-intake/SKILL.md | 77 | Tailscale URL in docs | `mac-mini.tailbd5748.ts.net` | replace with `<your-tailscale-hostname>` |
| system/skills/secret-intake/SKILL.md | 79 | Tailscale URL in docs | `mac-mini.tailbd5748.ts.net` | replace with `<your-tailscale-hostname>` |
| system/scripts/pulse/server.py | 2 | hardcoded port | `8896` (comment) | update comment to reflect env var |
| system/scripts/pulse/server.py | 22 | hardcoded port | `PORT = 8896` | `PORT = int(os.environ.get("HEX_PULSE_PORT", 8896))` |
| system/scripts/hex-router/router.py | 30 | hardcoded port (routing table) | `8889` | document as configurable or use env var |
| system/scripts/hex-router/router.py | 31 | hardcoded port (routing table) | `8889` | document as configurable or use env var |
| system/scripts/hex-router/router.py | 32 | hardcoded port (routing table) | `8889` | document as configurable or use env var |
| system/scripts/hex-router/router.py | 34 | hardcoded port (routing table) | `8896` | document as configurable or use env var |
| system/scripts/hex-router/router.py | 35 | hardcoded port (routing table) | `8895` | document as configurable or use env var |
| system/scripts/hex-router/router.py | 36 | hardcoded port (routing table) | `8897` | document as configurable or use env var |
| system/scripts/hex-router/router.py | 37 | hardcoded port (routing table) | `8891` | document as configurable or use env var |
| system/scripts/hex-router/router.py | 38 | hardcoded port (routing table) | `8890` | document as configurable or use env var |
| tests/eval/autoresearch.py | 268 | Ollama model name | `gemma4:e4b` (default) | already uses env var `OLLAMA_MODEL` — **CLEAN** (kept as note) |

---

## Notes

- `tests/test_env_resolution.sh` lines 119–121: references `/Users/mrap` only in a test that asserts the value is NOT present — **legitimate test fixture, not a violation**.
- `tests/test_env_resolution.sh` lines 104–112: `/opt/homebrew/bin` references are guarded by existence check `if [ -d "/opt/homebrew/bin" ]` — **legitimate cross-platform pattern, not a violation**.
- `tests/eval/build_tart_image.sh` lines 92–103: `/opt/homebrew` references are inside a macOS VM build script — **in-scope for macOS VM use, acceptable but should note Linux limitation in docs**.
- `system/scripts/env.sh` line 44: `_add_to_path "/opt/homebrew/bin"` — should verify this is guarded before adding.
- `system/scripts/migrate-v040.sh` lines 6, 108, 115: references `/Users/mrap` only in comments explaining what the migration detects/fixes — **acceptable**.
- `tests/eval/autoresearch.py:268`: `OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "gemma4:e4b")` — already env-var-driven; the default `gemma4:e4b` is user-installed but the fallback pattern is correct. Noted for future: default should be a more widely available model.
- `system/scripts/upgrade.sh` LaunchAgent references: macOS-specific but guarded with `if [[ "$(uname)" == "Darwin" ]]` — should verify the guard is present.

---

## Priority Fixes (scripts — highest blast radius)

1. `system/scripts/consolidate.sh` — 4 violations, hardcoded Claude project path derived from Mike's workspace
2. `system/skills/secret-intake/scripts/server.py` — hardcoded `/Users/mrap/mrap-hex` default
3. `system/skills/secret-intake/scripts/start.sh` — Tailscale hostname hardcoded
4. `system/scripts/release.sh` — hardcoded `mrap-hex` default
5. `system/commands/bet-status.md` — hardcoded absolute path
6. `system/scripts/pulse/server.py` — hardcoded port (low severity, already usable)
7. `system/reference/core-agents/*.yaml` — Slack channel name (6 files)
8. `system/scripts/hex-router/router.py` — Tailscale hostname + ports
