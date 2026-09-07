# Phone alerts — severity-aware delivery rail (task T2cf094yz)

**Spec:** S6bg793ev · **Task:** T2cf094yz · **Date:** 2026-09-06 · **Base:** develop 8b5a26bf

## What this task built

The off-machine delivery rail in `system/harness/src/alert.rs`, plus its config
loader, template, and the full test set from scope item 5. Task Tbnve3dk9 wires
the three severity classes at their existing call sites; this task only makes the
rail exist and be correct.

### Delivery model

Every alert, unchanged from before, still does three things first, in order and
always: a loud `ALERT [key] title: msg` line on stderr, a telemetry row, and a
macOS banner (gated `cfg(all(macos, not(test)))`). Per-key dedupe (6h stamp
file) is untouched and runs before anything else — a suppressed key delivers
nothing, exactly as today.

On top of that, when `$HEX_DIR/.hex/config/alerts.toml` is present:

- **ntfy push** (`curl` with a `Priority:` header). Every alert pushes. The
  default class pushes at `default` priority; the three named classes push at
  `urgent`. Push **bodies are generic** — `[key] title` only, built by the pure
  `push_body()` function. Never the message, a path, or personal data (the push
  service is third-party).
- **gws/gmail email** (second rail). Only the three named classes, and only when
  an operator address is configured. Email is first-party (the operator's own
  mailbox), so it may carry the full message detail.

Config **absent** = exactly the pre-existing Mac-only behavior, plus **one loud
line per process lifetime** noting the rail is unconfigured (guarded by a
process-global `AtomicBool`; `take_unconfigured_warning()` returns true exactly
once).

### The class parameter without breaking call sites

`AlertClass` is an enum with a `Default` variant and a `Default` impl. Rust has
no default arguments, so "gains a class parameter … every existing call site
compiles unchanged" is realized by keeping the two historical entrypoints intact
and adding two severity-aware siblings:

| Function | Signature | Role |
|---|---|---|
| `notify(key, title, msg)` | unchanged | delegates at `AlertClass::Default` |
| `notify_at(hex_dir, key, title, msg)` | unchanged | delegates at `AlertClass::Default` |
| `notify_with_class(key, title, msg, class)` | new | resolves `HEX_DIR`, delegates |
| `notify_at_with_class(hex_dir, key, title, msg, class)` | new | the real impl |

`notify_at` is itself `pub` with six external callers (applier, supervise,
nightly_full_liveness, …); keeping its arity is why the sibling approach was
required rather than adding a parameter in place.

### Config loader safety

`load_config(hex_dir: &Path)` reads `hex_dir.join(".hex/config/alerts.toml")`
and **nothing else** — it never consults `HEX_DIR` or the home directory.
`HEX_DIR` is resolved once, in `notify_with_class`. This is deliberate: the
`llm_config` loader falls back to `~/hex` when `HEX_DIR` is unset, which for an
alert rail would mean an unset-env run reading the operator's *real* config and
firing real pushes. That fallback is not copied here. Absent file → `None`
(Mac-only fallback). Malformed file → loud stderr + `None` (best-effort; a bad
config never fails the calling worker, S6).

### Hourly collapse cap

At most `max_pushes_per_hour` human-facing pushes per rolling hour across all
keys. State lives in `.hex/run/alerts/push-window.json` (`window_start`, `sent`,
`suppressed`, `collapse_sent`). `push_gate()`:

- under the cap → `Send` (real push);
- first overflow of the window → `Collapse { suppressed }`, sends exactly one
  summary push naming the suppressed count;
- subsequent overflows → `Suppress` (no push).

**Accounting decision (pinned):** the window emits at most `max` human-facing
pushes plus one collapse notice — the collapse is a rate-limit notice, not a
human-facing alert push, so "at most `max_pushes_per_hour` human-facing pushes"
holds. With `max = 2`, four distinct alerts produce exactly three pushes: two
real + one collapse.

**Collapse count is a floor.** Emission is eager (at first overflow) because the
call is synchronous — there is no background flusher, so the only timely *single*
push is emitted the moment the cap is first exceeded. It therefore names the
count suppressed *so far* (`"N+ suppressed"`), never a total it will outlive.

**Why the collapse is S6-compliant (load-bearing):** stderr and the telemetry
row fire for *every* alert, including every suppressed one, because they happen
before the push rail. Only the third-party push collapses; nothing is silent.

### Testability seams

- `push_body()` / `collapse_body()` are pure and `cfg`-independent, so the
  generic-body invariant is asserted against the exact value production sends,
  not against a test-only sink. (`cfg(test)` is active only for this lib target's
  own unit tests; the bin and integration targets link the real curl/gws arms.)
- Under `cfg(test)`, `deliver_push`/`deliver_email` record to `test_sink`
  instead of shelling out, and `warn()` records the loud line — so deliveries and
  every failure path can be asserted without network or process spawning.
- All alert tests serialize on the crate's single `HEX_DIR` lock
  (`telemetry::test_support::lock_env`) and reset the process-global sink +
  once-only flag up front, because those are process-global.

## Test set (scope item 5) — all in `alert.rs` `mod tests`

| Scope requirement | Test |
|---|---|
| config precedence | `config_precedence_file_value_beats_builtin_default` |
| absent-config fallback | `absent_config_falls_back_and_delivers_no_rail` |
| one loud line per process | `unconfigured_warning_is_once_per_process` |
| generic-body invariant | `push_body_contains_no_path_email_or_personal_tokens` |
| three classes send both rails | `email_classes_send_both_rails` |
| default class push only | `default_class_sends_push_only_at_normal_priority` |
| hourly cap collapses + reports | `hourly_cap_collapses_and_reports` |
| every failure path loud | `missing_rails_fail_loudly`, `malformed_config_falls_back_loudly` |
| existing dedupe preserved | `dedupe_suppresses_within_window` |

## Files

- `system/harness/src/alert.rs` — rail, loader, cap, tests.
- `system/templates/alerts.toml.example` — instance template (placeholders only,
  no real URL/address, no double-curly-brace template syntax).

## No-leak note

No real topic URL, email address, personal name, or absolute `/Users` path
(outside the `/Users/test` fixture in one test) appears in any committed file.
Test fixtures use `*.example.invalid` hosts/addresses and `/Users/test/...`.
