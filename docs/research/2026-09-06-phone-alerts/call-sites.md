# Phone alerts — mapping the three email classes at their call sites (task Tbnve3dk9)

**Spec:** S6bg793ev · **Task:** Tbnve3dk9 · **Date:** 2026-09-06 · **Base:** develop 8b5a26bf

## What this task did

Task T2cf094yz built the severity-aware rail in `system/harness/src/alert.rs`
(`AlertClass`, `notify_with_class` / `notify_at_with_class`, the ntfy push +
gws/gmail email rails). This task wires the **three email classes** at the exact
call sites where those alerts fire today, leaving every other `notify` call at
`AlertClass::Default` (push only). **No alert's trigger condition changed** — the
same events fire the same alerts; only the class each carries changed.

## The three mappings

| Operator class | Site (repo-relative) | Alert key | Now sends |
|---|---|---|---|
| Spend threshold crossed | `system/harness/src/usage.rs` | `burn-guard` | `Spend` → push (urgent) + email |
| Harness itself down | `system/harness/src/harness/supervise.rs` | `harness-restart-failed` | `HarnessDown` → push (urgent) + email |
| Work order terminally failed | `system/harness/src/modules/boi_spec_watch.worker.rs` | `boi-spec-watch:spec-terminal:<id>` (status `failed`) | `WorkOrderFailed` → push (urgent) + email |

### 1. Burn guard spend threshold → `Spend`

`hex usage burn` (run every 10m by the `hex-burn-guard` worker, which just shells
out to it) fires the `burn-guard` alert when the trailing-window rate crosses the
threshold. That call now passes `AlertClass::Spend`. The sibling
`burn-guard-config` misconfiguration alert stays `Default`.

`usage.rs` is a **bin** module. The lib's `cfg(test)` delivery sink is not linked
into the binary target (the `hex` lib is a plain dependency, compiled without
`cfg(test)`), so a bin test that called `notify*` would hit the real curl/gws
arms. The mapping is therefore expressed as a pure `burn_alert_class(key)` seam
that both call sites route through, and the test asserts `burn-guard → Spend`
**and** `burn-guard-config → Default`. This proof is **compositional**: the seam
pins the class SELECTION; `alert.rs::email_classes_send_both_rails` and
`default_class_sends_push_only_at_normal_priority` pin the class → rails
delivery. Together they establish that the spend class reaches the rail.

### 2. Harness itself down → `HarnessDown`

`harness::supervise::restart_and_verify` escalates on the `Escalate` verdict —
the harness is still not serving after a restart **and** a re-bootstrap. That is
the definitive "harness itself down" signal (alert title "hex harness DOWN"). The
emit was extracted into `emit_harness_down_alert(hex_dir, msg)`, which passes
`AlertClass::HarnessDown`. Because `supervise.rs` is a **lib** module, the test
drives that real helper against a configured rail and observes delivery through
`test_sink` end-to-end: exactly one email + one `urgent` push.

### 3. boi-spec-watch spec-terminal-failed → `WorkOrderFailed`

`boi_spec_watch::emit_alert` handles the `SpecTerminal` transition. A terminally
**failed** spec now carries `AlertClass::WorkOrderFailed`; `completed` / `canceled`
stay `Default`. The trigger is unchanged — all three terminal statuses still
alert; only a `failed` terminal additionally emails and pushes urgent. Proven
end-to-end (lib module) via `test_sink`: `failed` → email + urgent push;
`completed` → push only at normal priority, no email.

## Sites considered and deliberately left `Default`

- **`system/harness/src/main.rs` — `downtime` / "telemetry gap"** (in
  `run_failures`, reached by `hex failures --alert`). This is a *retrospective
  gap report emitted by a live harness*: the `hex-failures` worker runs it on a
  daily cron, so the harness must be up to emit it, and the message itself names
  three possible causes ("harness down, box asleep, or restarted"). It does not
  detect that the harness is gone — the out-of-process probe
  (`com.hex.failures-probe` → `run_failures_probe`) does that, and it alerts via
  `osascript` directly, never through `alert::notify`. Left `Default`.
- **`system/harness/src/harness/supervise.rs` — `harness-watchdog-revive`**. This
  fires on every watchdog recovery ACTION (`Install`/`Reboot`), including a
  successful revive — it is a recovery notice, not a harness-down page. Left
  `Default`.
- **Every other `notify` call** across the harness (missed/not-landed digest
  rows, task-blocked, oss-releaser, applier, backup, resources, hitl, …) stays
  `Default` and now pushes at normal priority. Unchanged behavior otherwise.

## Tests (declared gate: `cargo test class`)

| Mapping | Test | Kind |
|---|---|---|
| spend | `usage::tests::burn_alert_class_maps_spend_and_leaves_config_default` | seam (bin) + compositional |
| harness-down | `harness::supervise::tests::harness_down_escalation_reaches_email_rail_urgent_class` | end-to-end via `test_sink` |
| work-order-failed | `modules::boi_spec_watch::tests::boi_spec_watch_spec_terminal_failed_maps_work_order_failed_class` | end-to-end via `test_sink` |

## Files

- `system/harness/src/usage.rs` — `burn_alert_class` seam + both call sites + test.
- `system/harness/src/harness/supervise.rs` — `emit_harness_down_alert` seam + `Escalate` call site + test.
- `system/harness/src/modules/boi_spec_watch.worker.rs` — `SpecTerminal` class selection + test (added in the red phase).

## No-leak note

No real topic URL, email address, personal name, or absolute `/Users` path
appears here or in any touched file. Tests reuse the existing fixtures
`https://ntfy.example.invalid/t` and `ops@example.invalid`.
