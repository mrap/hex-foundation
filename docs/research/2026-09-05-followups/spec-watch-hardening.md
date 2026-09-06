# Spec-watch hardening (GROUP 1)

**Date:** 2026-09-05
**Task:** Tg3rysp23 (spec Sfgm6qvqh)
**Files:** `system/harness/src/modules/boi_spec_watch.worker.rs`, `system/harness/src/alert.rs`

## Problem (operator evidence 2026-08-31..09-04)

The `boi-spec-watch` worker alerted on new blocked transitions but had three
holes that let alerting both go silent and, in principle, storm:

1. Its dedup rode `alert::notify`'s shared 6h stamp file. An unblock never
   refreshed the stamp, so a re-block inside 6h was swallowed — silent forever
   (two tasks sat parked ~20h unalerted).
2. Nothing recorded when a blocked state ENDED, so park duration was not
   computable anywhere.
3. A task stuck in `active` with no live phase_run (slot starvation) was
   invisible — it never entered `blocked`, so nothing watched it.

## What shipped

### 1. Transition-keyed alerting — unblock clears the alert stamp
`alert::clear` / `alert::clear_at` (`system/harness/src/alert.rs:63`, `:70`)
delete a dedup stamp so the next `notify` for that key is guaranteed to fire.
Each tick, `cleared_tasks` (`boi_spec_watch.worker.rs:225`) finds tasks that
LEFT `blocked`/`starved`; `run_tick` (`:733`) clears both per-task stamp keys
(`task_alert_keys`) for each. A genuine new block episode is therefore
alert-eligible again instead of being swallowed by the 6h window.

### 2. Anti-storm cap with flap count
`register_episode_alert` (`boi_spec_watch.worker.rs:426`) is the per-task
throttle, backed by table `boi_spec_watch_alert` (`:405`). At most one
human-facing alert per task per `ALERT_CAP_SECS` = 30m (`:55`). Re-blocks inside
the window are counted (`pending_flaps`), not delivered; the next eligible alert
carries the accumulated count via `flap_suffix` in `emit_task_alert` (`:691`).

### 3. Append-only state-transition log (entries AND exits)
Table `boi_spec_watch_transitions` (`boi_spec_watch.worker.rs:396`) is written
as a side effect of `save_state` (`:538`) via `append_transition` (`:637`):
`save_state` reads the prior persisted rows BEFORE the full-replace, diffs
old→new watch state, and appends one row per change (`entity_kind`, `entity_id`,
`from_state`, `to_state`, `at`, `reason`) — both block/starve entries and their
`→ active` exits. The full-replace never touches the log, so it is append-only
and park duration stays computable. Tasks are keyed by their alert-relevant
"watch state" (`task_watch_state`, `:129`: `blocked` / `starved` / raw).

### 4. Slot-starvation alert class
`snapshot_from_conn` (`boi_spec_watch.worker.rs:265`) computes `phase_idle_secs`
per task: seconds since the newest `phase_runs` signal
(`COALESCE(completed_at, last_heartbeat_at, started_at)`), falling back to the
task's own `started_at`. A task in state `active` with `phase_idle_secs >=
STARVATION_SECS` = 30m (`:60`) classifies as `starved` and alerts the same way a
block does (`Transition::TaskStarved`, throttled by the same per-task cap). An
active task with unknown idle is treated as not-starved and logged loudly (S6),
never a false alert.

## Tests (all names contain `spec_watch`; gate: `cargo test spec_watch`)

- `boi_spec_watch_transition_log_records_entries_and_exits`
  (`boi_spec_watch.worker.rs:1170`) — log completeness: a block, an unblock, and
  a re-block within the old 6h window produce two `to_state='blocked'` rows and
  one `blocked→active` exit row (re-block not swallowed; park computable).
- `boi_spec_watch_alert_cap_one_per_30m_with_flap_count` (`:1243`) — a 5-minute
  flapper yields at most one human alert per 30m; the post-cap alert carries the
  accumulated flap count.
- `boi_spec_watch_reblock_after_cap_realerts` (`:1276`) — a re-block 31m later
  (within 6h, past the 30m cap) re-alerts; a re-block 5m after that is capped.
- `boi_spec_watch_starved_active_task_alerts_like_blocked` (`:1305`) — active +
  idle past threshold classifies/transitions as `starved`; a live phase does not.
- `boi_spec_watch_starvation_idle_computed_from_phase_runs` (`:1351`) — the
  `phase_idle_secs` SQL reads a 1m-old heartbeat as live and a 90m-old one as
  starved.
- `boi_spec_watch_recovery_is_detected_for_stamp_clear` (`:1412`) — a task that
  leaves blocked/starved (or ages out) is detected for stamp clearing; a still-
  blocked task is not.
- `clear_stamp_reenables_notify` (`system/harness/src/alert.rs:118`) — clearing a
  stamp re-enables `notify`; clearing an absent stamp is a quiet no-op.

## Constraints honored
- All errors loud (S6): db/read failures propagate `Err`; the one non-fatal
  anomaly (active task, unknown idle) logs to stderr and does not alert.
- No new failures on the existing suite; changes confined to
  `system/harness/`. No clippy/fmt gating.
- No personal names, absolute private paths, hostnames, or emails; no
  double-curly template syntax.
