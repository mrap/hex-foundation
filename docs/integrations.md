# Modular Integrations (v0.3.0)

hex Foundation v0.3.0 introduces the **modular integration bundle** system: every external surface a hex instance depends on (APIs, MCPs, system services, refresh flows) lives in a single directory under `integrations/<name>/` with everything it needs to be installed, probed, maintained, and uninstalled as a unit.

## What a bundle is

```
integrations/<name>/
  integration.yaml       # manifest: tier, description, owner, secrets schema, maintenance, depends_on, provides
  probe.sh               # health probe — exit 0 healthy, non-0 with stderr reason, completes in 30s
  runbook.md             # failure modes, diagnostics, auto-fix, manual-fix, last known good
  secret.env.example     # schema only; actual values live in `.hex/secrets/<name>.env`
  events/                # hex-events policy templates, compiled by `hex-integration install`
  maintenance/           # refresh.sh, rotate.sh, etc.
  lib/                   # integration-specific helpers (e.g., RSA signing)
  tests/                 # bundle-local test scripts
  README.md              # one-screen intro
```

Copy `templates/integrations/_template/` to start a new integration.

## The CLI

`system/scripts/hex-integration` is the bundle lifecycle manager. It compiles bundle event policies into `~/.hex-events/policies/<name>-<event>.yaml` with a `# generated_from:` audit header, validates secrets against the bundle schema, and creates a symlink at `.hex/scripts/integrations/<name>.sh` → bundle `probe.sh` so the existing health harness finds bundle probes without modification.

Commands:

```
hex-integration install <name>     # compile policies, validate secrets, symlink probe, write state
hex-integration uninstall <name>   # reverse cleanly (no orphan policies/symlinks/state)
hex-integration update <name>      # re-materialize after bundle edits
hex-integration list               # installed + available bundles
hex-integration validate <name>    # dry-run schema check
hex-integration status [<name>]    # pretty-print _state/<name>.json
hex-integration probe <name>       # wraps hex-integration-check.sh (q-567 harness)
hex-integration rotate <name>      # runs maintenance/rotate.sh if present
```

All commands support `--json` and emit `hex.integration.{installed,uninstalled,updated,validated,probed,rotated}.{ok,fail}` telemetry.

## Compile-step policy coupling

The hex-events daemon watches `~/.hex-events/policies/*.yaml`. Bundles live under `integrations/<name>/events/*.yaml`. `hex-integration install` shells out to `hex-events compile <bundle>` which runs full static analysis before writing any file.

**As of hex-events v0.2.0, the compile step is a real compiler** — not just templating. It runs three validator passes:

1. **Schema** — enforces canonical `rules:[{name, trigger, actions}]` shape; rejects flat `trigger:/action:` form.
2. **Producer-check** — cross-references every subscribed event against the live catalog (`hex-events list-events`); unknown subscriptions are errors.
3. **Dead-code** — duplicate rule/policy names, unknown action types, rules with no actions, rate-limit cadence mismatches.

On compile error, `install` exits non-zero and **no files land in** `~/.hex-events/policies/`. The previously-installed version (if any) stays in place untouched.

Compiled policies carry full manifest headers:

```
# generated_from: integrations/<name>/events/<stem>.yaml
# generated_at: <ISO-8601>
# compiler_version: 1.0.0
# checks_passed: schema, producer-check, dead-code
```

This gives operators an audit trail (grep `generated_from:` to find which bundle owns any installed policy) and makes uninstall atomic (remove every file with a matching `generated_from:` header).

Pre-existing non-bundle policies (no `generated_from:` header) are scanned in `--permissive` warn-only mode during the migration window.

Bundle event YAMLs use the standard hex-events policy schema — a `rules:` list with `name`, `trigger`, and `actions` per rule. Do NOT use a flat top-level `trigger:` + `action:` — the daemon rejects that shape.

## Secrets

Bundles declare schema in `integration.yaml`:

```yaml
secrets:
  required:
    - API_KEY
  optional:
    - DEBUG
```

Actual values live in the per-host store at `.hex/secrets/<name>.env` (gitignored). `hex-integration install` validates required keys are present and non-empty before compiling anything. Missing-secret error exits 3 with a pointer to `secret.env.example`.

Private key files (RSA PEMs etc.) are referenced by path in the `.env` and live alongside it, chmod 600.

## Zero-downtime migration

When migrating a live refresh policy (Slack bot token, X OAuth2) into a bundle:

1. Copy (don't move) the existing policy file into `<bundle>/events/`.
2. Run `hex-integration install`. This creates `<bundle>-<stem>.yaml` in `~/.hex-events/policies/` — the old file keeps firing.
3. Wait for telemetry evidence that the new compiled policy has fired successfully once (check `events.db` for events with source containing the bundle name).
4. Only then rename the old policy to `_deprecated-<original>.yaml`.

## Related

- Harness: `system/scripts/hex-integration-check.sh` — single-probe runner with atomic state + locks + transition events.
- Template: `templates/integrations/_template/` — copy for every new integration.
- Reference instance: the hex instance that develops the foundation. If you maintain a private hex instance alongside this foundation repo, its `projects/integrations/modular-integration-architecture.md` contains the design doc that led to this version.
