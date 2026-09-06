//! `boi-spec-watch` — push-notify BOI spec/task state changes above phase level.
//!
//! Gap this closes (2026-07-30..08-01): nothing watched spec/task state above
//! phase level — Sa9r1xh9w's lane2-state sat `blocked` for 2 days until someone
//! happened to ask. `hex failures` watches the HARNESS; the boi daemon watches
//! nothing above phase level. A standalone launchd watcher was built then
//! REVERTED same day ("Don't ever use launchd. We have hex mechanics for
//! this!"). This is that watcher ported to a foundation harness worker — it
//! rides all harness observability (a telemetry row per fire, MISSED/NEVER-RAN
//! detection by `hex failures`, the shared alert path) for free.
//!
//! Every 5 minutes it opens `~/.boi/v2/boi.db` READ-ONLY (never writes, never
//! holds a lock; 14-day lookback), diffs the spec/task state against the prior
//! tick's persisted snapshot, and alerts on exactly two transition classes:
//!   1. a spec newly reaching a terminal status (completed / failed / canceled)
//!   2. a task newly entering `state='blocked'` (any reason)
//! Detection only, never remediates (same doctrine as `hex failures`).
//!
//! Failure stance (S6): boi.db absent → quiet no-op (this worker ships in
//! foundation and most instances never run BOI). boi.db present but unreadable
//! → the handler returns `Err`, which the runtime records as a `status=error`
//! telemetry row that `hex failures` counts — a loud, operator-visible failure.
//!
//! State: the prior-tick snapshot lives in the harness-owned runtime-state db
//! (`module_state::db_path`, `$HEX_DIR/.hex/harness/state.db`) — the same seam
//! `oss-releaser` uses — NOT an ad-hoc JSON file. First tick has no persisted
//! baseline, so it baselines SILENTLY (no alert storm on deploy).
//!
//! Alerts go through `hex::alert::notify` (stderr + telemetry row + deduped
//! macOS notification) — the exact path `oss-releaser`/`burn_guard` use.

use anyhow::Context as _;
use hex::worker::{ctx::Ctx, event::Event, Result, Worker};
use rusqlite::OptionalExtension;
use std::collections::BTreeMap;
use std::path::{Path, PathBuf};
use std::time::Duration;

/// Cron expression — every 5 minutes (7-field: sec min hour dom mon dow year).
pub const CRON_EVERY_5M: &str = "0 */5 * * * * *";

/// Spec statuses that are terminal (schema: `queued|running|completed|failed|canceled`).
const TERMINAL: [&str; 3] = ["completed", "failed", "canceled"];

/// How far back to look for specs (matches the reference watcher).
const LOOKBACK_DAYS: i64 = 14;

/// SQLite `busy_timeout` for the read-only open — bounded so a locked writer
/// never wedges the tick.
const BUSY_TIMEOUT: Duration = Duration::from_secs(5);

/// Anti-storm cap: at most one human-facing alert per task per this window.
/// Re-blocks/flaps inside the window are counted (the flap count) but only the
/// next eligible alert (after the window elapses) delivers, carrying the count.
const ALERT_CAP_SECS: i64 = 30 * 60;

/// Slot-starvation threshold: a task in state `active` whose most recent
/// `phase_run` signal (completed / heartbeat / start) is at least this old has
/// no live phase — the silent slot-starvation class. Alerts like a block.
const STARVATION_SECS: i64 = 30 * 60;

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

/// One task row as read from boi.db.
#[derive(Debug, Clone, PartialEq, Eq)]
struct TaskRow {
    task_id: String,
    ref_: Option<String>,
    state: String,
    spec_id: String,
    /// `blocked_reason.type` (the reason discriminator), if any.
    reason: Option<String>,
    /// Seconds since the task's most recent `phase_run` signal (completed /
    /// heartbeat / start), falling back to the task's own `started_at`. `None`
    /// when neither is known (a task with no phase_runs and no start time — an
    /// anomaly treated as not-starved, logged loudly by the caller).
    phase_idle_secs: Option<i64>,
}

/// A single tick's read of the watched slice of boi.db.
#[derive(Debug, Default, Clone, PartialEq, Eq)]
struct Snapshot {
    /// spec_id → status.
    specs: BTreeMap<String, String>,
    tasks: Vec<TaskRow>,
}

/// The prior tick's persisted view — enough to compute the two transition classes.
#[derive(Debug, Default, Clone, PartialEq, Eq)]
struct PersistedState {
    /// spec_id → status.
    specs: BTreeMap<String, String>,
    /// task_id → state.
    tasks: BTreeMap<String, String>,
}

/// A transition worth alerting an operator about.
#[derive(Debug, Clone, PartialEq, Eq)]
enum Transition {
    /// A spec newly reached a terminal status.
    SpecTerminal { spec_id: String, status: String },
    /// A task newly entered `blocked`.
    TaskBlocked {
        task_id: String,
        ref_: Option<String>,
        spec_id: String,
        reason: Option<String>,
    },
    /// A task newly entered slot-starvation: `active` with no live `phase_run`
    /// for at least `STARVATION_SECS`. Alerts the same way a block does.
    TaskStarved {
        task_id: String,
        ref_: Option<String>,
        spec_id: String,
    },
}

fn is_terminal(status: &str) -> bool {
    TERMINAL.contains(&status)
}

/// The alert-relevant "watch state" of a task: `blocked`, `starved`, or the raw
/// state. Persisted per tick so a re-entry (block or starvation) is detected as
/// a fresh transition and an exit clears it. `blocked` takes precedence over
/// starvation. A task in state `active` with an unknown `phase_idle_secs` is
/// treated as not-starved (never a false alert) — the caller logs the anomaly.
fn task_watch_state(t: &TaskRow) -> String {
    if t.state == "blocked" {
        return "blocked".to_string();
    }
    if t.state == "active" {
        match t.phase_idle_secs {
            Some(idle) if idle >= STARVATION_SECS => return "starved".to_string(),
            Some(_) => {}
            None => {
                eprintln!(
                    "boi-spec-watch: task {} is active but phase_idle_secs is unknown \
                     (no phase_runs and no started_at) — treating as not-starved",
                    t.task_id
                );
            }
        }
    }
    t.state.clone()
}

fn unix_now() -> i64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs() as i64)
        .unwrap_or(0)
}

// ---------------------------------------------------------------------------
// Pure diff (unit-tested; no IO)
// ---------------------------------------------------------------------------

/// Diff the prior-tick state against this tick's snapshot.
///
/// `prev == None` means there is no persisted baseline yet (first tick ever) —
/// baseline SILENTLY: emit nothing, just let the caller persist the snapshot.
/// This is the anti-alert-storm-on-deploy rule.
///
/// Spec-terminal fires only when the spec was PREVIOUSLY SEEN in a non-terminal
/// state (a spec first observed already-terminal is not alerted — it was born
/// or finished before we started watching, and re-baselining shouldn't shout).
/// Task-blocked fires when a task is `blocked` now and was not `blocked` (or was
/// unseen) last tick — so a re-block after an unblock alerts again.
fn diff(prev: Option<&PersistedState>, cur: &Snapshot) -> Vec<Transition> {
    let prev = match prev {
        Some(p) => p,
        None => return Vec::new(), // first tick: baseline silently
    };
    let mut out = Vec::new();

    for (spec_id, status) in &cur.specs {
        if is_terminal(status) {
            if let Some(was) = prev.specs.get(spec_id) {
                if !is_terminal(was) {
                    out.push(Transition::SpecTerminal {
                        spec_id: spec_id.clone(),
                        status: status.clone(),
                    });
                }
            }
        }
    }

    for t in &cur.tasks {
        match task_watch_state(t).as_str() {
            "blocked" => {
                let newly = prev.tasks.get(&t.task_id).map_or(true, |w| w != "blocked");
                if newly {
                    out.push(Transition::TaskBlocked {
                        task_id: t.task_id.clone(),
                        ref_: t.ref_.clone(),
                        spec_id: t.spec_id.clone(),
                        reason: t.reason.clone(),
                    });
                }
            }
            "starved" => {
                let newly = prev.tasks.get(&t.task_id).map_or(true, |w| w != "starved");
                if newly {
                    out.push(Transition::TaskStarved {
                        task_id: t.task_id.clone(),
                        ref_: t.ref_.clone(),
                        spec_id: t.spec_id.clone(),
                    });
                }
            }
            _ => {}
        }
    }

    out
}

/// Tasks that LEFT an alert-worthy watch state (`blocked` or `starved`) this
/// tick — either recovered to another state or aged out of the window. Their
/// alert stamps are cleared so the next genuine episode re-alerts (transition-
/// keyed alerting; the core of the never-silent-forever fix).
fn cleared_tasks(prev: Option<&PersistedState>, cur: &Snapshot) -> Vec<String> {
    let prev = match prev {
        Some(p) => p,
        None => return Vec::new(),
    };
    let mut out = Vec::new();
    for (task_id, prev_ws) in &prev.tasks {
        if prev_ws != "blocked" && prev_ws != "starved" {
            continue;
        }
        let still_worthy = cur
            .tasks
            .iter()
            .find(|t| &t.task_id == task_id)
            .map(|t| {
                let ws = task_watch_state(t);
                ws == "blocked" || ws == "starved"
            })
            .unwrap_or(false); // disappeared → cleared
        if !still_worthy {
            out.push(task_id.clone());
        }
    }
    out
}

// ---------------------------------------------------------------------------
// boi.db read (read-only; fixture-tested)
// ---------------------------------------------------------------------------

/// `$HOME/.boi/v2/boi.db` — the reference watcher's path. BOI's db lives under
/// the real home, not `$HEX_DIR`.
fn boi_db_path() -> Result<PathBuf> {
    let home = std::env::var("HOME")
        .context("boi-spec-watch: HOME unset — cannot locate ~/.boi/v2/boi.db")?;
    Ok(PathBuf::from(home).join(".boi/v2/boi.db"))
}

/// Read the watched slice from an already-open connection. Split out so fixture
/// tests can drive it against a temp SQLite file with the real schema subset.
fn snapshot_from_conn(conn: &rusqlite::Connection) -> std::result::Result<Snapshot, String> {
    let cutoff = format!("-{LOOKBACK_DAYS} days");

    let mut specs = BTreeMap::new();
    {
        let mut stmt = conn
            .prepare(
                "SELECT spec_id, status FROM spec_runtime \
                 WHERE started_at >= datetime('now', ?1)",
            )
            .map_err(|e| format!("boi.db spec query prepare: {e}"))?;
        let rows = stmt
            .query_map(rusqlite::params![cutoff], |r| {
                Ok((r.get::<_, String>(0)?, r.get::<_, String>(1)?))
            })
            .map_err(|e| format!("boi.db spec query: {e}"))?;
        for row in rows {
            let (id, status) = row.map_err(|e| format!("boi.db spec row: {e}"))?;
            specs.insert(id, status);
        }
    }

    let mut tasks = Vec::new();
    {
        // `phase_idle_secs`: seconds since the task's most recent phase_run
        // signal. Per run, COALESCE picks that run's latest of
        // completed/heartbeat/start; the aggregate MAX takes the newest across
        // runs; COALESCE to the task's own started_at covers "no phase_runs
        // yet". Lexicographic MAX over datetime()-format text sorts correctly
        // (same assumption the started_at window already relies on).
        let mut stmt = conn
            .prepare(
                "SELECT t.task_id, t.ref, t.state, t.spec_id, t.blocked_reason, \
                    CAST(strftime('%s','now') AS INTEGER) - CAST(strftime('%s', COALESCE( \
                        (SELECT MAX(COALESCE(pr.completed_at, pr.last_heartbeat_at, pr.started_at)) \
                         FROM phase_runs pr WHERE pr.task_id = t.task_id), \
                        t.started_at \
                    )) AS INTEGER) AS phase_idle_secs \
                 FROM task_runtime t JOIN spec_runtime s ON s.spec_id = t.spec_id \
                 WHERE s.started_at >= datetime('now', ?1)",
            )
            .map_err(|e| format!("boi.db task query prepare: {e}"))?;
        let rows = stmt
            .query_map(rusqlite::params![cutoff], |r| {
                Ok((
                    r.get::<_, String>(0)?,
                    r.get::<_, Option<String>>(1)?,
                    r.get::<_, String>(2)?,
                    r.get::<_, String>(3)?,
                    r.get::<_, Option<String>>(4)?,
                    r.get::<_, Option<i64>>(5)?,
                ))
            })
            .map_err(|e| format!("boi.db task query: {e}"))?;
        for row in rows {
            let (task_id, ref_, state, spec_id, blocked_reason, phase_idle_secs) =
                row.map_err(|e| format!("boi.db task row: {e}"))?;
            let reason = blocked_reason.as_deref().and_then(parse_reason_type);
            tasks.push(TaskRow {
                task_id,
                ref_,
                state,
                spec_id,
                reason,
                phase_idle_secs,
            });
        }
    }

    Ok(Snapshot { specs, tasks })
}

/// Extract `blocked_reason.type` from the stored JSON. Malformed JSON → `None`
/// (the alert degrades to "unknown"; the block itself still alerts).
fn parse_reason_type(json: &str) -> Option<String> {
    serde_json::from_str::<serde_json::Value>(json)
        .ok()
        .and_then(|v| {
            v.get("type")
                .and_then(|t| t.as_str())
                .map(|s| s.to_string())
        })
}

/// Open boi.db READ-ONLY and snapshot it.
///
/// - Absent path → `Ok(None)`: quiet no-op (most instances never run BOI).
/// - Present but unreadable (open or query fails) → `Err`: LOUD, so the handler
///   returns Err and the runtime records a `hex failures`-counted error row.
fn read_snapshot(db_path: &Path) -> std::result::Result<Option<Snapshot>, String> {
    if !db_path.exists() {
        return Ok(None);
    }
    let conn = rusqlite::Connection::open_with_flags(
        db_path,
        rusqlite::OpenFlags::SQLITE_OPEN_READ_ONLY,
    )
    .map_err(|e| format!("boi.db open read-only ({}): {e}", db_path.display()))?;
    conn.busy_timeout(BUSY_TIMEOUT)
        .map_err(|e| format!("boi.db busy_timeout: {e}"))?;
    let snap = snapshot_from_conn(&conn)?;
    Ok(Some(snap))
}

// ---------------------------------------------------------------------------
// Persisted state — harness-owned runtime-state db (module_state seam)
// ---------------------------------------------------------------------------

fn state_open(hex_dir: &Path) -> std::result::Result<rusqlite::Connection, String> {
    let p = hex::module_state::db_path(hex_dir);
    if let Some(parent) = p.parent() {
        std::fs::create_dir_all(parent)
            .map_err(|e| format!("cannot create {}: {e}", parent.display()))?;
    }
    let conn = rusqlite::Connection::open(&p)
        .map_err(|e| format!("cannot open {}: {e}", p.display()))?;
    conn.execute_batch(
        "CREATE TABLE IF NOT EXISTS boi_spec_watch_spec (
            spec_id    TEXT PRIMARY KEY,
            status     TEXT NOT NULL,
            updated_at INTEGER NOT NULL
         );
         CREATE TABLE IF NOT EXISTS boi_spec_watch_task (
            task_id    TEXT PRIMARY KEY,
            state      TEXT NOT NULL,
            updated_at INTEGER NOT NULL
         );
         CREATE TABLE IF NOT EXISTS boi_spec_watch_meta (
            k TEXT PRIMARY KEY,
            v TEXT NOT NULL
         );
         CREATE TABLE IF NOT EXISTS boi_spec_watch_transitions (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            entity_kind TEXT NOT NULL,
            entity_id   TEXT NOT NULL,
            from_state  TEXT,
            to_state    TEXT NOT NULL,
            at          INTEGER NOT NULL,
            reason      TEXT
         );
         CREATE TABLE IF NOT EXISTS boi_spec_watch_alert (
            entity_id     TEXT PRIMARY KEY,
            last_alert_at INTEGER NOT NULL,
            pending_flaps INTEGER NOT NULL DEFAULT 0
         );",
    )
    .map_err(|e| format!("boi-spec-watch state schema ({}): {e}", p.display()))?;
    Ok(conn)
}

/// Register a fresh alert-worthy episode (a block or a starvation) for
/// `entity_id` observed at `now`, and decide whether a human-facing alert
/// should fire, honoring the per-entity anti-storm cap (at most one alert per
/// `ALERT_CAP_SECS`).
///
/// Returns `Some(flap_count)` to alert now — `flap_count` is the number of
/// alert-worthy episodes observed since the last human alert (this one
/// included), so `> 1` means re-blocks/flaps happened while suppressed. Returns
/// `None` when the cap suppresses this episode; the episode is still counted so
/// the next eligible alert carries the accumulated flap count. Loud on db
/// error (S6) — never silently swallowed.
fn register_episode_alert(
    conn: &rusqlite::Connection,
    entity_id: &str,
    now: i64,
) -> std::result::Result<Option<i64>, String> {
    let row: Option<(i64, i64)> = conn
        .query_row(
            "SELECT last_alert_at, pending_flaps FROM boi_spec_watch_alert WHERE entity_id = ?1",
            rusqlite::params![entity_id],
            |r| Ok((r.get(0)?, r.get(1)?)),
        )
        .optional()
        .map_err(|e| format!("boi-spec-watch alert read: {e}"))?;
    match row {
        // Never alerted for this entity → alert now, flap count 1.
        None => {
            conn.execute(
                "INSERT INTO boi_spec_watch_alert (entity_id, last_alert_at, pending_flaps) \
                 VALUES (?1, ?2, 0)",
                rusqlite::params![entity_id, now],
            )
            .map_err(|e| format!("boi-spec-watch alert insert: {e}"))?;
            Ok(Some(1))
        }
        Some((last, pending)) => {
            let flap = pending + 1;
            if now - last >= ALERT_CAP_SECS {
                // Cap elapsed → deliver, carrying the accumulated flap count,
                // and reset the window.
                conn.execute(
                    "UPDATE boi_spec_watch_alert SET last_alert_at = ?2, pending_flaps = 0 \
                     WHERE entity_id = ?1",
                    rusqlite::params![entity_id, now],
                )
                .map_err(|e| format!("boi-spec-watch alert update: {e}"))?;
                Ok(Some(flap))
            } else {
                // Inside the cap → suppress but count the flap for next time.
                conn.execute(
                    "UPDATE boi_spec_watch_alert SET pending_flaps = ?2 WHERE entity_id = ?1",
                    rusqlite::params![entity_id, flap],
                )
                .map_err(|e| format!("boi-spec-watch alert update: {e}"))?;
                Ok(None)
            }
        }
    }
}

/// Load the prior-tick state.
///
/// `Ok(None)` means NO baseline has been recorded yet (first tick) — distinct
/// from an empty-but-baselined state. A read *error* propagates as `Err` (loud,
/// S6) — never silently collapsed to `None`, which would swallow a tick's
/// transitions by spuriously re-baselining.
fn load_state(hex_dir: &Path) -> std::result::Result<Option<PersistedState>, String> {
    let conn = state_open(hex_dir)?;

    let baselined = conn
        .query_row(
            "SELECT 1 FROM boi_spec_watch_meta WHERE k = 'baselined'",
            [],
            |_| Ok(()),
        )
        .optional()
        .map_err(|e| format!("boi-spec-watch state meta read: {e}"))?
        .is_some();
    if !baselined {
        return Ok(None);
    }

    let mut specs = BTreeMap::new();
    {
        let mut stmt = conn
            .prepare("SELECT spec_id, status FROM boi_spec_watch_spec")
            .map_err(|e| format!("boi-spec-watch state spec query: {e}"))?;
        let rows = stmt
            .query_map([], |r| Ok((r.get::<_, String>(0)?, r.get::<_, String>(1)?)))
            .map_err(|e| format!("boi-spec-watch state spec query: {e}"))?;
        for row in rows {
            let (id, status) = row.map_err(|e| format!("boi-spec-watch state spec row: {e}"))?;
            specs.insert(id, status);
        }
    }

    let mut tasks = BTreeMap::new();
    {
        let mut stmt = conn
            .prepare("SELECT task_id, state FROM boi_spec_watch_task")
            .map_err(|e| format!("boi-spec-watch state task query: {e}"))?;
        let rows = stmt
            .query_map([], |r| Ok((r.get::<_, String>(0)?, r.get::<_, String>(1)?)))
            .map_err(|e| format!("boi-spec-watch state task query: {e}"))?;
        for row in rows {
            let (id, state) = row.map_err(|e| format!("boi-spec-watch state task row: {e}"))?;
            tasks.insert(id, state);
        }
    }

    Ok(Some(PersistedState { specs, tasks }))
}

/// Replace the persisted snapshot with `snap` and mark the baseline. Specs/tasks
/// that aged out of the 14-day window drop naturally (full replace). Atomic.
///
/// Side effect (same transaction): append every state change to the APPEND-ONLY
/// `boi_spec_watch_transitions` log — both entries AND exits — by reading the
/// prior persisted rows BEFORE the full-replace and diffing old→new. Tasks are
/// keyed by their alert-relevant watch state (`blocked` / `starved` / raw), so
/// a block, a starvation, and their recoveries are each one row. The full
/// replace NEVER touches the transition log, so it is never truncated and park
/// duration stays computable across ticks.
fn save_state(hex_dir: &Path, snap: &Snapshot) -> std::result::Result<(), String> {
    let mut conn = state_open(hex_dir)?;
    let now = unix_now();
    let tx = conn
        .transaction()
        .map_err(|e| format!("boi-spec-watch state tx: {e}"))?;

    // Read prior persisted state BEFORE the replace, to diff old→new.
    let prior_specs: BTreeMap<String, String> = {
        let mut stmt = tx
            .prepare("SELECT spec_id, status FROM boi_spec_watch_spec")
            .map_err(|e| format!("boi-spec-watch prior spec read: {e}"))?;
        let rows = stmt
            .query_map([], |r| Ok((r.get::<_, String>(0)?, r.get::<_, String>(1)?)))
            .map_err(|e| format!("boi-spec-watch prior spec read: {e}"))?;
        let mut m = BTreeMap::new();
        for row in rows {
            let (id, st) = row.map_err(|e| format!("boi-spec-watch prior spec row: {e}"))?;
            m.insert(id, st);
        }
        m
    };
    let prior_tasks: BTreeMap<String, String> = {
        let mut stmt = tx
            .prepare("SELECT task_id, state FROM boi_spec_watch_task")
            .map_err(|e| format!("boi-spec-watch prior task read: {e}"))?;
        let rows = stmt
            .query_map([], |r| Ok((r.get::<_, String>(0)?, r.get::<_, String>(1)?)))
            .map_err(|e| format!("boi-spec-watch prior task read: {e}"))?;
        let mut m = BTreeMap::new();
        for row in rows {
            let (id, st) = row.map_err(|e| format!("boi-spec-watch prior task row: {e}"))?;
            m.insert(id, st);
        }
        m
    };

    // Append transitions for every spec whose status changed.
    for (spec_id, status) in &snap.specs {
        let from = prior_specs.get(spec_id);
        if from.map(String::as_str) != Some(status.as_str()) {
            append_transition(&tx, "spec", spec_id, from.map(String::as_str), status, now, None)?;
        }
    }
    // Append transitions for every task whose watch state changed (entries and
    // exits both — an unblock/recovery is a `... → active` row).
    for t in &snap.tasks {
        let to_ws = task_watch_state(t);
        let from = prior_tasks.get(&t.task_id);
        if from.map(String::as_str) != Some(to_ws.as_str()) {
            let reason = if to_ws == "blocked" {
                t.reason.as_deref()
            } else {
                None
            };
            append_transition(
                &tx,
                "task",
                &t.task_id,
                from.map(String::as_str),
                &to_ws,
                now,
                reason,
            )?;
        }
    }

    // Full-replace the snapshot (transition log is untouched → append-only).
    tx.execute("DELETE FROM boi_spec_watch_spec", [])
        .map_err(|e| format!("boi-spec-watch state spec clear: {e}"))?;
    tx.execute("DELETE FROM boi_spec_watch_task", [])
        .map_err(|e| format!("boi-spec-watch state task clear: {e}"))?;
    for (spec_id, status) in &snap.specs {
        tx.execute(
            "INSERT OR REPLACE INTO boi_spec_watch_spec (spec_id, status, updated_at) \
             VALUES (?1, ?2, ?3)",
            rusqlite::params![spec_id, status, now],
        )
        .map_err(|e| format!("boi-spec-watch state spec write: {e}"))?;
    }
    for t in &snap.tasks {
        tx.execute(
            "INSERT OR REPLACE INTO boi_spec_watch_task (task_id, state, updated_at) \
             VALUES (?1, ?2, ?3)",
            rusqlite::params![t.task_id, task_watch_state(t), now],
        )
        .map_err(|e| format!("boi-spec-watch state task write: {e}"))?;
    }
    tx.execute(
        "INSERT OR REPLACE INTO boi_spec_watch_meta (k, v) VALUES ('baselined', '1')",
        [],
    )
    .map_err(|e| format!("boi-spec-watch state baseline write: {e}"))?;
    tx.commit()
        .map_err(|e| format!("boi-spec-watch state commit: {e}"))?;
    Ok(())
}

/// Append one row to the append-only state-transition log.
fn append_transition(
    tx: &rusqlite::Transaction<'_>,
    entity_kind: &str,
    entity_id: &str,
    from_state: Option<&str>,
    to_state: &str,
    at: i64,
    reason: Option<&str>,
) -> std::result::Result<(), String> {
    tx.execute(
        "INSERT INTO boi_spec_watch_transitions \
            (entity_kind, entity_id, from_state, to_state, at, reason) \
         VALUES (?1, ?2, ?3, ?4, ?5, ?6)",
        rusqlite::params![entity_kind, entity_id, from_state, to_state, at, reason],
    )
    .map_err(|e| format!("boi-spec-watch transition write: {e}"))?;
    Ok(())
}

// ---------------------------------------------------------------------------
// Alert emission (shared alert path)
// ---------------------------------------------------------------------------

/// The two per-task alert stamp keys — one per class. Kept distinct so the
/// telemetry `event` name (which is the key) and `hex failures` can separate
/// blocks from starvation. Both are cleared when a task recovers.
fn task_alert_keys(task_id: &str) -> [String; 2] {
    [
        format!("boi-spec-watch:task-blocked:{task_id}"),
        format!("boi-spec-watch:task-starved:{task_id}"),
    ]
}

/// The flap suffix appended to an alert body when re-blocks/flaps piled up while
/// suppressed by the anti-storm cap. Empty for a single (non-flapping) episode.
fn flap_suffix(flap: i64) -> String {
    if flap > 1 {
        format!(" [flapped {flap}x since last alert]")
    } else {
        String::new()
    }
}

/// Surface a spec-terminal transition via the shared alert path.
fn emit_spec_alert(spec_id: &str, status: &str) {
    hex::alert::notify(
        &format!("boi-spec-watch:spec-terminal:{spec_id}"),
        "BOI spec terminal",
        &format!("spec {spec_id} → {}", status.to_uppercase()),
    );
}

/// Surface a task block/starvation transition via the shared alert path, with
/// the flap count when re-blocks occurred inside the anti-storm window.
fn emit_task_alert(t: &Transition, flap: i64) {
    match t {
        Transition::TaskBlocked {
            task_id,
            ref_,
            spec_id,
            reason,
        } => {
            let label = ref_.clone().unwrap_or_else(|| task_id.clone());
            hex::alert::notify(
                &format!("boi-spec-watch:task-blocked:{task_id}"),
                "BOI task blocked",
                &format!(
                    "task {label} ({spec_id}) BLOCKED: {}{}",
                    reason.clone().unwrap_or_else(|| "unknown".to_string()),
                    flap_suffix(flap),
                ),
            );
        }
        Transition::TaskStarved {
            task_id,
            ref_,
            spec_id,
        } => {
            let label = ref_.clone().unwrap_or_else(|| task_id.clone());
            hex::alert::notify(
                &format!("boi-spec-watch:task-starved:{task_id}"),
                "BOI task starved",
                &format!(
                    "task {label} ({spec_id}) ACTIVE with no live phase_run for 30m{}",
                    flap_suffix(flap),
                ),
            );
        }
        Transition::SpecTerminal { .. } => {}
    }
}

// ---------------------------------------------------------------------------
// Handler
// ---------------------------------------------------------------------------

fn run_tick(_e: Event, _ctx: Ctx) -> Result<()> {
    let db = boi_db_path()?;
    let snap = match read_snapshot(&db).map_err(|e| anyhow::anyhow!(e))? {
        // Absent boi.db → quiet no-op (debug-level at most). Most foundation
        // instances never run BOI; this must not be noise for them.
        None => return Ok(()),
        Some(s) => s,
    };

    let hex_dir = resolve_hex_dir()?;
    let prev = load_state(&hex_dir).map_err(|e| anyhow::anyhow!(e))?;

    // Transition-keyed alerting: a task that LEFT blocked/starved clears its
    // alert stamps, so the next genuine episode re-alerts instead of being
    // swallowed by the shared 6h dedupe window.
    for task_id in cleared_tasks(prev.as_ref(), &snap) {
        for key in task_alert_keys(&task_id) {
            hex::alert::clear(&key);
        }
    }

    let now = unix_now();
    let alert_conn = state_open(&hex_dir).map_err(|e| anyhow::anyhow!(e))?;
    for t in &diff(prev.as_ref(), &snap) {
        match t {
            Transition::SpecTerminal { spec_id, status } => emit_spec_alert(spec_id, status),
            Transition::TaskBlocked { task_id, .. } | Transition::TaskStarved { task_id, .. } => {
                // Anti-storm cap: at most one human alert per task per 30m; the
                // body carries the flap count for episodes suppressed meanwhile.
                if let Some(flap) = register_episode_alert(&alert_conn, task_id, now)
                    .map_err(|e| anyhow::anyhow!(e))?
                {
                    emit_task_alert(t, flap);
                }
            }
        }
    }

    save_state(&hex_dir, &snap).map_err(|e| anyhow::anyhow!(e))?;
    Ok(())
}

/// `$HEX_DIR`, else `$HOME/hex` — the same resolution `module_state`/`oss-releaser` use.
fn resolve_hex_dir() -> Result<PathBuf> {
    if let Ok(d) = std::env::var("HEX_DIR") {
        return Ok(PathBuf::from(d));
    }
    let home = std::env::var("HOME")
        .context("boi-spec-watch: neither HEX_DIR nor HOME set — cannot locate the state db")?;
    Ok(PathBuf::from(home).join("hex"))
}

/// Build the `boi-spec-watch` worker.
pub fn worker() -> Worker {
    Worker::new("boi-spec-watch").on_cron_named("every-5m", CRON_EVERY_5M, run_tick)
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;

    fn ps(
        specs: &[(&str, &str)],
        tasks: &[(&str, &str)],
    ) -> PersistedState {
        PersistedState {
            specs: specs
                .iter()
                .map(|(a, b)| (a.to_string(), b.to_string()))
                .collect(),
            tasks: tasks
                .iter()
                .map(|(a, b)| (a.to_string(), b.to_string()))
                .collect(),
        }
    }

    fn task(task_id: &str, state: &str, spec_id: &str, reason: Option<&str>) -> TaskRow {
        TaskRow {
            task_id: task_id.to_string(),
            ref_: Some(format!("{task_id}-ref")),
            state: state.to_string(),
            spec_id: spec_id.to_string(),
            reason: reason.map(|s| s.to_string()),
            phase_idle_secs: None,
        }
    }

    /// A task with an explicit `phase_idle_secs` — for the starvation class.
    fn task_idle(
        task_id: &str,
        state: &str,
        spec_id: &str,
        reason: Option<&str>,
        phase_idle_secs: Option<i64>,
    ) -> TaskRow {
        TaskRow {
            phase_idle_secs,
            ..task(task_id, state, spec_id, reason)
        }
    }

    fn snap(specs: &[(&str, &str)], tasks: Vec<TaskRow>) -> Snapshot {
        Snapshot {
            specs: specs
                .iter()
                .map(|(a, b)| (a.to_string(), b.to_string()))
                .collect(),
            tasks,
        }
    }

    // ---- diff: baseline / first run ----

    #[test]
    fn boi_spec_watch_diff_first_run_is_silent() {
        // No persisted baseline → emit nothing even with a blocked task and a
        // terminal spec present.
        let cur = snap(
            &[("S1", "completed")],
            vec![task("T1abcdef2", "blocked", "S1", Some("merge_conflict"))],
        );
        assert_eq!(diff(None, &cur), Vec::new());
    }

    // ---- diff: spec terminal ----

    #[test]
    fn boi_spec_watch_diff_spec_reaches_terminal_once() {
        let prev = ps(&[("S1", "running")], &[]);
        let cur = snap(&[("S1", "completed")], vec![]);
        assert_eq!(
            diff(Some(&prev), &cur),
            vec![Transition::SpecTerminal {
                spec_id: "S1".to_string(),
                status: "completed".to_string(),
            }]
        );

        // Terminal-only-once: once persisted as completed, no re-alert.
        let prev2 = ps(&[("S1", "completed")], &[]);
        assert_eq!(diff(Some(&prev2), &cur), Vec::new());
    }

    #[test]
    fn boi_spec_watch_diff_spec_first_seen_terminal_is_silent() {
        // A spec never seen non-terminal (appeared already terminal) must not
        // alert — matches the reference (`prev is not None`) semantics.
        let prev = ps(&[], &[]);
        let cur = snap(&[("S1", "failed")], vec![]);
        assert_eq!(diff(Some(&prev), &cur), Vec::new());
    }

    #[test]
    fn boi_spec_watch_diff_all_three_terminal_statuses() {
        for status in ["completed", "failed", "canceled"] {
            let prev = ps(&[("S1", "running")], &[]);
            let cur = snap(&[("S1", status)], vec![]);
            assert_eq!(
                diff(Some(&prev), &cur),
                vec![Transition::SpecTerminal {
                    spec_id: "S1".to_string(),
                    status: status.to_string(),
                }],
                "status {status} must alert"
            );
        }
    }

    // ---- diff: task blocked ----

    #[test]
    fn boi_spec_watch_diff_task_newly_blocked() {
        let prev = ps(&[("S1", "running")], &[("T1abcdef2", "active")]);
        let cur = snap(
            &[("S1", "running")],
            vec![task("T1abcdef2", "blocked", "S1", Some("cap_exceeded"))],
        );
        assert_eq!(
            diff(Some(&prev), &cur),
            vec![Transition::TaskBlocked {
                task_id: "T1abcdef2".to_string(),
                ref_: Some("T1abcdef2-ref".to_string()),
                spec_id: "S1".to_string(),
                reason: Some("cap_exceeded".to_string()),
            }]
        );
    }

    #[test]
    fn boi_spec_watch_diff_task_already_blocked_no_realert() {
        let prev = ps(&[("S1", "running")], &[("T1abcdef2", "blocked")]);
        let cur = snap(
            &[("S1", "running")],
            vec![task("T1abcdef2", "blocked", "S1", Some("merge_conflict"))],
        );
        assert_eq!(diff(Some(&prev), &cur), Vec::new());
    }

    #[test]
    fn boi_spec_watch_diff_task_unblock_then_reblock_alerts_again() {
        // was blocked → active: no alert (leaving blocked is not a transition class).
        let prev = ps(&[("S1", "running")], &[("T1abcdef2", "blocked")]);
        let active = snap(
            &[("S1", "running")],
            vec![task("T1abcdef2", "active", "S1", None)],
        );
        assert_eq!(diff(Some(&prev), &active), Vec::new());

        // active → blocked again: alerts.
        let prev2 = ps(&[("S1", "running")], &[("T1abcdef2", "active")]);
        let blocked = snap(
            &[("S1", "running")],
            vec![task("T1abcdef2", "blocked", "S1", None)],
        );
        assert_eq!(
            diff(Some(&prev2), &blocked),
            vec![Transition::TaskBlocked {
                task_id: "T1abcdef2".to_string(),
                ref_: Some("T1abcdef2-ref".to_string()),
                spec_id: "S1".to_string(),
                reason: None,
            }]
        );
    }

    #[test]
    fn boi_spec_watch_diff_newly_appeared_blocked_task_alerts() {
        // A task unseen last tick that is blocked now → newly blocked.
        let prev = ps(&[("S1", "running")], &[]);
        let cur = snap(
            &[("S1", "running")],
            vec![task("T1abcdef2", "blocked", "S1", Some("manual"))],
        );
        assert_eq!(
            diff(Some(&prev), &cur),
            vec![Transition::TaskBlocked {
                task_id: "T1abcdef2".to_string(),
                ref_: Some("T1abcdef2-ref".to_string()),
                spec_id: "S1".to_string(),
                reason: Some("manual".to_string()),
            }]
        );
    }

    // ---- reason parsing ----

    #[test]
    fn boi_spec_watch_parse_reason_type() {
        assert_eq!(
            parse_reason_type(r#"{"type":"merge_conflict","conflict_files":[]}"#),
            Some("merge_conflict".to_string())
        );
        assert_eq!(parse_reason_type(r#"{"cap":3}"#), None); // no `type`
        assert_eq!(parse_reason_type("not json"), None); // malformed → None (still alerts)
    }

    // ---- fixture: snapshot_from_conn against real schema subset ----

    /// Build a temp boi.db with the queried schema subset and seed it.
    fn fixture_db() -> rusqlite::Connection {
        let conn = rusqlite::Connection::open_in_memory().unwrap();
        conn.execute_batch(
            "CREATE TABLE spec_runtime (
                spec_id    TEXT PRIMARY KEY,
                status     TEXT NOT NULL,
                started_at TIMESTAMP
             );
             CREATE TABLE task_runtime (
                task_id        TEXT PRIMARY KEY,
                spec_id        TEXT NOT NULL,
                ref            TEXT,
                state          TEXT NOT NULL,
                blocked_reason JSON,
                started_at     TIMESTAMP
             );
             CREATE TABLE phase_runs (
                id                TEXT PRIMARY KEY,
                spec_id           TEXT NOT NULL,
                task_id           TEXT,
                started_at        TIMESTAMP NOT NULL,
                last_heartbeat_at TIMESTAMP,
                completed_at      TIMESTAMP
             );",
        )
        .unwrap();
        // In-window spec (-1 day) with two tasks (one blocked w/ reason, one active).
        conn.execute(
            "INSERT INTO spec_runtime (spec_id, status, started_at) \
             VALUES ('S1', 'running', datetime('now', '-1 days'))",
            [],
        )
        .unwrap();
        conn.execute(
            "INSERT INTO task_runtime (task_id, spec_id, ref, state, blocked_reason) \
             VALUES ('T1abcdef2', 'S1', 'lane2', 'blocked', '{\"type\":\"merge_conflict\"}')",
            [],
        )
        .unwrap();
        conn.execute(
            "INSERT INTO task_runtime (task_id, spec_id, ref, state, blocked_reason) \
             VALUES ('T2abcdef3', 'S1', 'lane1', 'active', NULL)",
            [],
        )
        .unwrap();
        // Aged-out spec (-20 days) with a task — must be EXCLUDED by the window.
        conn.execute(
            "INSERT INTO spec_runtime (spec_id, status, started_at) \
             VALUES ('SOLD', 'completed', datetime('now', '-20 days'))",
            [],
        )
        .unwrap();
        conn.execute(
            "INSERT INTO task_runtime (task_id, spec_id, ref, state, blocked_reason) \
             VALUES ('T9abcdef4', 'SOLD', 'old', 'blocked', '{\"type\":\"stale\"}')",
            [],
        )
        .unwrap();
        conn
    }

    #[test]
    fn boi_spec_watch_snapshot_reads_window_and_parses_reason() {
        let conn = fixture_db();
        let s = snapshot_from_conn(&conn).unwrap();

        // Only the in-window spec.
        assert_eq!(s.specs.len(), 1);
        assert_eq!(s.specs.get("S1"), Some(&"running".to_string()));
        assert!(!s.specs.contains_key("SOLD"));

        // Only the in-window spec's tasks; aged-out task excluded.
        let ids: Vec<&str> = s.tasks.iter().map(|t| t.task_id.as_str()).collect();
        assert!(ids.contains(&"T1abcdef2"));
        assert!(ids.contains(&"T2abcdef3"));
        assert!(!ids.contains(&"T9abcdef4"));

        let blocked = s.tasks.iter().find(|t| t.task_id == "T1abcdef2").unwrap();
        assert_eq!(blocked.state, "blocked");
        assert_eq!(blocked.reason.as_deref(), Some("merge_conflict"));
        assert_eq!(blocked.ref_.as_deref(), Some("lane2"));

        let active = s.tasks.iter().find(|t| t.task_id == "T2abcdef3").unwrap();
        assert_eq!(active.reason, None);
    }

    // ---- failure paths ----

    #[test]
    fn boi_spec_watch_read_snapshot_absent_is_noop() {
        let dir = tempfile::TempDir::new().unwrap();
        let missing = dir.path().join("nope/boi.db");
        assert_eq!(read_snapshot(&missing).unwrap(), None);
    }

    #[test]
    fn boi_spec_watch_read_snapshot_unreadable_is_loud() {
        // A present-but-not-a-database file must surface an Err (loud), not None.
        let dir = tempfile::TempDir::new().unwrap();
        let bogus = dir.path().join("boi.db");
        std::fs::write(&bogus, b"this is not a sqlite database at all").unwrap();
        assert!(
            read_snapshot(&bogus).is_err(),
            "an unreadable boi.db must be a loud Err, never a silent no-op"
        );
    }

    // ---- state roundtrip (explicit hex_dir; no env mutation → no flake) ----

    #[test]
    fn boi_spec_watch_state_absent_before_baseline() {
        let dir = tempfile::TempDir::new().unwrap();
        // No baseline recorded yet → None (first tick).
        assert_eq!(load_state(dir.path()).unwrap(), None);
    }

    #[test]
    fn boi_spec_watch_state_roundtrip_and_baseline() {
        let dir = tempfile::TempDir::new().unwrap();
        let s = snap(
            &[("S1", "running"), ("S2", "completed")],
            vec![
                task("T1abcdef2", "blocked", "S1", Some("manual")),
                task("T2abcdef3", "active", "S2", None),
            ],
        );
        save_state(dir.path(), &s).unwrap();

        let loaded = load_state(dir.path()).unwrap().expect("baselined → Some");
        let mut want_specs = BTreeMap::new();
        want_specs.insert("S1".to_string(), "running".to_string());
        want_specs.insert("S2".to_string(), "completed".to_string());
        assert_eq!(loaded.specs, want_specs);

        let mut want_tasks = BTreeMap::new();
        want_tasks.insert("T1abcdef2".to_string(), "blocked".to_string());
        want_tasks.insert("T2abcdef3".to_string(), "active".to_string());
        assert_eq!(loaded.tasks, want_tasks);

        // Full replace: a later, smaller snapshot drops aged-out rows.
        let s2 = snap(&[("S1", "completed")], vec![]);
        save_state(dir.path(), &s2).unwrap();
        let loaded2 = load_state(dir.path()).unwrap().unwrap();
        assert_eq!(loaded2.specs.len(), 1);
        assert_eq!(loaded2.specs.get("S1"), Some(&"completed".to_string()));
        assert!(loaded2.tasks.is_empty());
    }

    // ---- GROUP 1 (RED): append-only state-transition log ----
    //
    // Behavior owed by GROUP 1 (spec-watch-hardening): the worker must persist
    // an APPEND-ONLY state-transition log (entity id, from_state, to_state, at,
    // reason) in the harness-owned runtime-state db — recording BOTH block
    // entries AND exits — so (a) park duration is computable and (b) a re-block
    // inside the old 6h alert-dedup window is never silent. This is the concrete
    // artifact behind the contract's "a re-block within 6h of a prior alert
    // produces a second alert row".
    //
    // Contract pinned by this test (documented in
    // docs/research/2026-09-05-followups/spec-watch-hardening.md by the
    // implementation phase):
    //   * table `boi_spec_watch_transitions` in `module_state::db_path`
    //   * columns at least `entity_id`, `from_state`, `to_state`
    //   * append-only: NEVER cleared by `save_state`'s full-replace
    //   * written atomically as a side effect of `save_state` (read the prior
    //     rows before the delete, diff old→new, append the transitions) so the
    //     log and the snapshot can never diverge.
    //
    // RED until GROUP 1 lands: the table does not exist, so the first query
    // `.expect(...)` fails. It compiles against the current API (no new function
    // names, no env mutation, no boi.db fixture) and fails on assertion — not a
    // build break.
    #[test]
    fn boi_spec_watch_transition_log_records_entries_and_exits() {
        let dir = tempfile::TempDir::new().unwrap();

        // Tick 0 — baseline: task active. Establishes a known prior so the first
        // block is an unambiguous active→blocked entry.
        save_state(
            dir.path(),
            &snap(&[("S1", "running")], vec![task("T1abcdef2", "active", "S1", None)]),
        )
        .unwrap();
        // Tick 1 — BLOCK: active → blocked (entry).
        save_state(
            dir.path(),
            &snap(
                &[("S1", "running")],
                vec![task("T1abcdef2", "blocked", "S1", Some("merge_conflict"))],
            ),
        )
        .unwrap();
        // Tick 2 — UNBLOCK: blocked → active (exit — makes park duration computable).
        save_state(
            dir.path(),
            &snap(&[("S1", "running")], vec![task("T1abcdef2", "active", "S1", None)]),
        )
        .unwrap();
        // Tick 3 — RE-BLOCK (well within the old 6h dedup window): active → blocked.
        // This is the episode the old stamp-file dedup swallowed for ~20h.
        save_state(
            dir.path(),
            &snap(
                &[("S1", "running")],
                vec![task("T1abcdef2", "blocked", "S1", Some("merge_conflict"))],
            ),
        )
        .unwrap();

        let conn = rusqlite::Connection::open(crate::module_state::db_path(dir.path()))
            .expect("open state db");

        // Both block episodes are logged → the re-block produces a SECOND row.
        let block_entries: i64 = conn
            .query_row(
                "SELECT COUNT(*) FROM boi_spec_watch_transitions \
                 WHERE entity_id = 'T1abcdef2' AND to_state = 'blocked'",
                [],
                |r| r.get(0),
            )
            .expect(
                "append-only table boi_spec_watch_transitions must exist and record block entries",
            );
        assert_eq!(
            block_entries, 2,
            "every block episode must be logged; the re-block within 6h must NOT be swallowed"
        );

        // The unblock (exit) is logged too → both entries AND exits are persisted.
        let block_exits: i64 = conn
            .query_row(
                "SELECT COUNT(*) FROM boi_spec_watch_transitions \
                 WHERE entity_id = 'T1abcdef2' AND from_state = 'blocked' AND to_state = 'active'",
                [],
                |r| r.get(0),
            )
            .expect("boi_spec_watch_transitions must record exits, not just entries");
        assert_eq!(
            block_exits, 1,
            "the unblock (blocked→active exit) must be logged so park duration is computable"
        );
    }

    // ---- GROUP 1: anti-storm cap + flap count ----

    #[test]
    fn boi_spec_watch_alert_cap_one_per_30m_with_flap_count() {
        let dir = tempfile::TempDir::new().unwrap();
        let conn = state_open(dir.path()).unwrap();
        // A task that flaps every 5 minutes. Only the first block (t=0) is an
        // eligible human alert; every re-block inside the 30m cap is suppressed
        // but counted.
        let t0 = 1_000_000_i64;
        let mut human_alerts = 0;
        for step in 0..6 {
            // t = 0, 5, 10, 15, 20, 25 minutes
            let now = t0 + step * 5 * 60;
            if register_episode_alert(&conn, "T1abcdef2", now)
                .unwrap()
                .is_some()
            {
                human_alerts += 1;
            }
        }
        assert_eq!(
            human_alerts, 1,
            "a 5-minute flapper must produce at most 1 human alert per 30 minutes"
        );
        // At t=30m the cap has elapsed → the next block delivers, carrying the
        // flap count: 5 suppressed episodes (t=5..25) + this one = 6.
        let at_30m = t0 + 30 * 60;
        assert_eq!(
            register_episode_alert(&conn, "T1abcdef2", at_30m).unwrap(),
            Some(6),
            "the post-cap alert must carry the accumulated flap count"
        );
    }

    #[test]
    fn boi_spec_watch_reblock_after_cap_realerts() {
        let dir = tempfile::TempDir::new().unwrap();
        let conn = state_open(dir.path()).unwrap();
        let t0 = 2_000_000_i64;
        // First block → alert.
        assert_eq!(
            register_episode_alert(&conn, "T1abcdef2", t0).unwrap(),
            Some(1)
        );
        // A re-block 31 minutes later — well within the old 6h dedup window that
        // used to swallow it, but past the 30m cap — MUST re-alert.
        let reblock = t0 + 31 * 60;
        assert_eq!(
            register_episode_alert(&conn, "T1abcdef2", reblock).unwrap(),
            Some(1),
            "a re-block within 6h of a prior alert must produce a second alert"
        );
        // A third block only 5 minutes after that is capped (no storm).
        let flap = t0 + 36 * 60;
        assert_eq!(
            register_episode_alert(&conn, "T1abcdef2", flap).unwrap(),
            None,
            "a re-block inside the 30m cap must be suppressed (counted, not delivered)"
        );
    }

    // ---- GROUP 1: slot-starvation class ----

    #[test]
    fn boi_spec_watch_starved_active_task_alerts_like_blocked() {
        // Pure watch-state classification: active + idle past the threshold =
        // starved; a live phase (small idle) = active.
        assert_eq!(
            task_watch_state(&task_idle(
                "T1abcdef2",
                "active",
                "S1",
                None,
                Some(STARVATION_SECS + 60)
            )),
            "starved"
        );
        assert_eq!(
            task_watch_state(&task_idle("T1abcdef2", "active", "S1", None, Some(60))),
            "active"
        );

        // active → starved is a fresh alert-worthy transition, just like a block.
        let prev = ps(&[("S1", "running")], &[("T1abcdef2", "active")]);
        let cur = snap(
            &[("S1", "running")],
            vec![task_idle("T1abcdef2", "active", "S1", None, Some(STARVATION_SECS + 1))],
        );
        assert_eq!(
            diff(Some(&prev), &cur),
            vec![Transition::TaskStarved {
                task_id: "T1abcdef2".to_string(),
                ref_: Some("T1abcdef2-ref".to_string()),
                spec_id: "S1".to_string(),
            }]
        );

        // Already starved last tick → no re-alert (transition, not state).
        let prev_starved = ps(&[("S1", "running")], &[("T1abcdef2", "starved")]);
        assert_eq!(diff(Some(&prev_starved), &cur), Vec::new());

        // A live phase (small idle) is not starved → no alert.
        let cur_live = snap(
            &[("S1", "running")],
            vec![task_idle("T1abcdef2", "active", "S1", None, Some(60))],
        );
        assert_eq!(diff(Some(&prev), &cur_live), Vec::new());
    }

    #[test]
    fn boi_spec_watch_starvation_idle_computed_from_phase_runs() {
        let conn = rusqlite::Connection::open_in_memory().unwrap();
        conn.execute_batch(
            "CREATE TABLE spec_runtime (spec_id TEXT PRIMARY KEY, status TEXT NOT NULL, started_at TIMESTAMP);
             CREATE TABLE task_runtime (task_id TEXT PRIMARY KEY, spec_id TEXT NOT NULL, ref TEXT, state TEXT NOT NULL, blocked_reason JSON, started_at TIMESTAMP);
             CREATE TABLE phase_runs (id TEXT PRIMARY KEY, spec_id TEXT NOT NULL, task_id TEXT, started_at TIMESTAMP NOT NULL, last_heartbeat_at TIMESTAMP, completed_at TIMESTAMP);",
        )
        .unwrap();
        conn.execute(
            "INSERT INTO spec_runtime (spec_id, status, started_at) \
             VALUES ('S1', 'running', datetime('now', '-1 days'))",
            [],
        )
        .unwrap();
        // Live task: heartbeat 1 minute ago → not starved.
        conn.execute(
            "INSERT INTO task_runtime (task_id, spec_id, ref, state, started_at) \
             VALUES ('T1abcdef2', 'S1', 'live', 'active', datetime('now', '-2 hours'))",
            [],
        )
        .unwrap();
        conn.execute(
            "INSERT INTO phase_runs (id, spec_id, task_id, started_at, last_heartbeat_at) \
             VALUES ('P1abcdef2', 'S1', 'T1abcdef2', datetime('now', '-90 minutes'), datetime('now', '-1 minutes'))",
            [],
        )
        .unwrap();
        // Stuck task: last heartbeat 90 minutes ago → starved.
        conn.execute(
            "INSERT INTO task_runtime (task_id, spec_id, ref, state, started_at) \
             VALUES ('T2abcdef3', 'S1', 'stuck', 'active', datetime('now', '-2 hours'))",
            [],
        )
        .unwrap();
        conn.execute(
            "INSERT INTO phase_runs (id, spec_id, task_id, started_at, last_heartbeat_at) \
             VALUES ('P2abcdef3', 'S1', 'T2abcdef3', datetime('now', '-95 minutes'), datetime('now', '-90 minutes'))",
            [],
        )
        .unwrap();

        let s = snapshot_from_conn(&conn).unwrap();
        let live = s.tasks.iter().find(|t| t.task_id == "T1abcdef2").unwrap();
        let stuck = s.tasks.iter().find(|t| t.task_id == "T2abcdef3").unwrap();
        assert!(
            live.phase_idle_secs.unwrap() < STARVATION_SECS,
            "a heartbeat 1m ago must read as live (idle {:?})",
            live.phase_idle_secs
        );
        assert!(
            stuck.phase_idle_secs.unwrap() >= STARVATION_SECS,
            "a heartbeat 90m ago must read as starved (idle {:?})",
            stuck.phase_idle_secs
        );
        assert_eq!(task_watch_state(live), "active");
        assert_eq!(task_watch_state(stuck), "starved");
    }

    // ---- GROUP 1: transition-keyed stamp clearing on recovery ----

    #[test]
    fn boi_spec_watch_recovery_is_detected_for_stamp_clear() {
        // Prior: T1 blocked. Now: T1 active → cleared (its alert stamp must be
        // cleared so the next block re-alerts).
        let prev = ps(&[("S1", "running")], &[("T1abcdef2", "blocked")]);
        let cur = snap(
            &[("S1", "running")],
            vec![task("T1abcdef2", "active", "S1", None)],
        );
        assert_eq!(cleared_tasks(Some(&prev), &cur), vec!["T1abcdef2".to_string()]);

        // Prior: T1 starved. Now: disappeared (aged out of the window) → cleared.
        let prev_starved = ps(&[("S1", "running")], &[("T1abcdef2", "starved")]);
        let empty = snap(&[("S1", "running")], vec![]);
        assert_eq!(
            cleared_tasks(Some(&prev_starved), &empty),
            vec!["T1abcdef2".to_string()]
        );

        // Still blocked → not cleared (episode ongoing).
        let cur_blocked = snap(
            &[("S1", "running")],
            vec![task("T1abcdef2", "blocked", "S1", None)],
        );
        assert!(cleared_tasks(Some(&prev), &cur_blocked).is_empty());

        // No prior baseline → nothing to clear.
        assert!(cleared_tasks(None, &cur).is_empty());
    }

    // ---- worker wiring ----

    #[test]
    fn boi_spec_watch_worker_is_5min_cron() {
        let w = worker();
        assert_eq!(w.name, "boi-spec-watch");
        assert_eq!(w.handlers.len(), 1);
        let (name, spec, _h) = w.handlers.into_iter().next().unwrap();
        assert_eq!(name.as_deref(), Some("every-5m"));
        assert_eq!(
            spec,
            crate::worker::TriggerSpec::Cron {
                expression: CRON_EVERY_5M.to_string(),
            }
        );
    }
}
