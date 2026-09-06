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
}

fn is_terminal(status: &str) -> bool {
    TERMINAL.contains(&status)
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
        if t.state == "blocked" {
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
        let mut stmt = conn
            .prepare(
                "SELECT t.task_id, t.ref, t.state, t.spec_id, t.blocked_reason \
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
                ))
            })
            .map_err(|e| format!("boi.db task query: {e}"))?;
        for row in rows {
            let (task_id, ref_, state, spec_id, blocked_reason) =
                row.map_err(|e| format!("boi.db task row: {e}"))?;
            let reason = blocked_reason.as_deref().and_then(parse_reason_type);
            tasks.push(TaskRow {
                task_id,
                ref_,
                state,
                spec_id,
                reason,
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
         );",
    )
    .map_err(|e| format!("boi-spec-watch state schema ({}): {e}", p.display()))?;
    Ok(conn)
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
fn save_state(hex_dir: &Path, snap: &Snapshot) -> std::result::Result<(), String> {
    let mut conn = state_open(hex_dir)?;
    let now = unix_now();
    let tx = conn
        .transaction()
        .map_err(|e| format!("boi-spec-watch state tx: {e}"))?;
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
            rusqlite::params![t.task_id, t.state, now],
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

// ---------------------------------------------------------------------------
// Alert emission (shared alert path)
// ---------------------------------------------------------------------------

/// Surface one transition via the shared alert path (stderr + telemetry row +
/// deduped macOS notification). Keyed per spec/task so a re-observed transition
/// inside the 6h dedupe window doesn't re-spam.
fn emit_alert(t: &Transition) {
    match t {
        Transition::SpecTerminal { spec_id, status } => {
            // All three terminal statuses alert (trigger unchanged). A terminally
            // FAILED spec IS a work-order-terminal-failure → the WorkOrderFailed
            // rail (push urgent + email); completed/canceled stay Default (push
            // only, normal priority).
            let class = if status == "failed" {
                hex::alert::AlertClass::WorkOrderFailed
            } else {
                hex::alert::AlertClass::Default
            };
            hex::alert::notify_with_class(
                &format!("boi-spec-watch:spec-terminal:{spec_id}"),
                "BOI spec terminal",
                &format!("spec {spec_id} → {}", status.to_uppercase()),
                class,
            );
        }
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
                    "task {label} ({spec_id}) BLOCKED: {}",
                    reason.clone().unwrap_or_else(|| "unknown".to_string())
                ),
            );
        }
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
    for t in &diff(prev.as_ref(), &snap) {
        emit_alert(t);
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

    // ---- call-site mapping: spec-terminal-failed → WorkOrderFailed rail ----

    /// RED (task Tbnve3dk9): `emit_alert` today sends every `SpecTerminal` at
    /// `AlertClass::Default` (push only, normal priority) — the three email
    /// classes are not yet mapped at their call sites. This pins the
    /// boi-spec-watch mapping end-to-end, driving the real `emit_alert` code
    /// path with a configured rail and observing delivery through the alert
    /// module's `test_sink` (email fires only for the three named classes):
    ///
    ///   * a terminally FAILED spec MUST reach the email rail and push at
    ///     `urgent` priority (proves `AlertClass::WorkOrderFailed` reaches the
    ///     rail);
    ///   * a non-failed terminal (completed) MUST still alert — the trigger is
    ///     unchanged (spec: "do not change any alert's trigger conditions") —
    ///     but push ONLY, at normal priority, with no email.
    ///
    /// Both halves fail against the pre-mapping workspace: the failed case
    /// sends no email today, so the class has not reached the rail.
    #[test]
    fn boi_spec_watch_spec_terminal_failed_maps_work_order_failed_class() {
        let _g = crate::telemetry::test_support::lock_env();

        fn rig(tmp: &tempfile::TempDir) {
            std::env::set_var("HEX_DIR", tmp.path());
            std::fs::create_dir_all(tmp.path().join(".hex/config")).unwrap();
            std::fs::write(
                tmp.path().join(".hex/config/alerts.toml"),
                "ntfy_topic_url = \"https://ntfy.example.invalid/t\"\n\
                 email = \"ops@example.invalid\"\n",
            )
            .unwrap();
            crate::alert::test_sink::reset();
        }

        // FAILED terminal → email rail + urgent push.
        {
            let tmp = tempfile::TempDir::new().unwrap();
            rig(&tmp);
            emit_alert(&Transition::SpecTerminal {
                spec_id: "Sredfail1".to_string(),
                status: "failed".to_string(),
            });
            let emails = crate::alert::test_sink::emails();
            let pushes = crate::alert::test_sink::pushes();
            assert_eq!(
                emails.len(),
                1,
                "failed terminal must reach the email rail (WorkOrderFailed); got {emails:?}"
            );
            assert_eq!(emails[0].to, "ops@example.invalid");
            assert_eq!(pushes.len(), 1, "failed terminal pushes exactly once");
            assert_eq!(
                pushes[0].priority, "urgent",
                "failed terminal must push at urgent priority: {pushes:?}"
            );
        }

        // Non-failed terminal (completed) → alert still fires, push only, normal.
        {
            let tmp = tempfile::TempDir::new().unwrap();
            rig(&tmp);
            emit_alert(&Transition::SpecTerminal {
                spec_id: "Sredok1".to_string(),
                status: "completed".to_string(),
            });
            let pushes = crate::alert::test_sink::pushes();
            assert_eq!(
                pushes.len(),
                1,
                "non-failed terminal must still alert — trigger unchanged"
            );
            assert_eq!(
                pushes[0].priority, "default",
                "non-failed terminal pushes at normal priority: {pushes:?}"
            );
            assert!(
                crate::alert::test_sink::emails().is_empty(),
                "non-failed terminal must not email: {:?}",
                crate::alert::test_sink::emails()
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
                blocked_reason JSON
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
