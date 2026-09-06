//! harness::supervise — keep `com.hex.harness` alive across release/upgrade bounces.
//!
//! Background. The harness is a single launchd gui-agent (`com.hex.harness`) hosting the
//! in-process iii engine plus every worker (console :3113, writing-app :7777, …). A
//! `hex upgrade` / `hex harness restart` bounces it; if the restart does not complete, the
//! launchd job is left BOOTED OUT of the domain and nothing brings it back — launchd
//! `KeepAlive` cannot act on a job that is not in the domain, and an in-harness worker is
//! dead too. Incident 2026-06-12: the v0.42.0 upgrade bounced it, the restart did not
//! finish, and the harness sat dead ~3h → every session that depended on a supervised
//! daemon got connection-refused (those daemons are children of the dead harness). See
//! `me/decisions/harness-down-after-release-incident-2026-06-12.md`.
//!
//! Two mechanisms live here:
//!   1. [`restart_and_verify`] — used by EVERY path that bounces the harness
//!      (`hex upgrade`'s `restart_harness`, `hex harness restart`). After the daemon-green
//!      restart it VERIFIES the engine actually serves; if not it does ONE re-bootstrap and
//!      then escalates LOUDLY (S6 alert + nonzero) instead of the old silent `[WARN]`.
//!   2. [`ensure_once`] / [`watchdog_loop`] — a tiny peer daemon
//!      (`com.hex.harness-watchdog`, `KeepAlive`) that periodically re-bootstraps the
//!      harness if it is missing/dead. It is NEVER touched by upgrade/release (they only
//!      bounce `com.hex.harness`), so it survives to resurrect the harness within ~2 min.
//!
//! Cross-process safety (review R1). Both mechanisms take an exclusive file lock
//! (`.hex/run/harness-bootstrap.lock`) before any bootout/bootstrap, so the watchdog and an
//! in-flight upgrade can never race a bootstrap — daemon-green's own note: "a bootstrap that
//! races a bootout gets EIO". Without this, the watchdog could RECREATE the incident.
//!
//! Intentional-down (review R3). `hex harness stop` writes `.hex/run/harness-stopped`; the
//! watchdog respects it and will NOT resurrect a deliberately-stopped harness. `start` /
//! `restart` clear it.
//!
//! Reboot caveat (review R7). Both the harness and the watchdog are gui-domain agents;
//! daemon-green's `start` requires an active gui session, so neither loads at
//! reboot-to-loginwindow. The watchdog covers booted-out / crashed / mid-drain WHILE LOGGED
//! IN — it is not full reboot recovery (the harness needs the login keychain anyway).

use std::net::{TcpStream, ToSocketAddrs};
use std::path::{Path, PathBuf};
use std::time::{Duration, Instant};

/// Engine liveness address (the in-process iii WebSocket). A listener answering here means
/// the harness's engine is serving — same probe as `doctor::checks::iii_engine_health`.
pub const ENGINE_ADDR: &str = "127.0.0.1:49134";
pub const HARNESS_LABEL: &str = "com.hex.harness";
pub const WATCHDOG_LABEL: &str = "com.hex.harness-watchdog";

/// How long the watchdog sleeps between supervision passes.
pub const WATCHDOG_INTERVAL: Duration = Duration::from_secs(120);
/// How long to wait for the engine port to come up after a (re-)bootstrap.
const VERIFY_TIMEOUT: Duration = Duration::from_secs(30);

// ---------------------------------------------------------------------------
// Paths
// ---------------------------------------------------------------------------

fn run_dir(hex_dir: &Path) -> PathBuf {
    hex_dir.join(".hex").join("run")
}

/// Exclusive lock serializing every bootout/bootstrap (restart paths + watchdog).
fn lock_path(hex_dir: &Path) -> PathBuf {
    run_dir(hex_dir).join("harness-bootstrap.lock")
}

/// Sentinel written by `hex harness stop` so the watchdog won't revive an intentional stop.
fn stopped_sentinel(hex_dir: &Path) -> PathBuf {
    run_dir(hex_dir).join("harness-stopped")
}

/// Mark the harness intentionally-down (called by `hex harness stop`). Best-effort + loud.
pub fn mark_intentionally_down(hex_dir: &Path) {
    let p = stopped_sentinel(hex_dir);
    if let Some(parent) = p.parent() {
        let _ = std::fs::create_dir_all(parent);
    }
    if let Err(e) = std::fs::write(&p, b"stopped via `hex harness stop`\n") {
        eprintln!(
            "[hex harness] WARN could not write stop sentinel {}: {e}",
            p.display()
        );
    }
}

/// Clear the intentional-down sentinel (called by `hex harness start` / `restart`).
pub fn clear_intentionally_down(hex_dir: &Path) {
    let p = stopped_sentinel(hex_dir);
    if p.exists() {
        if let Err(e) = std::fs::remove_file(&p) {
            eprintln!(
                "[hex harness] WARN could not clear stop sentinel {}: {e}",
                p.display()
            );
        }
    }
}

fn is_intentionally_down(hex_dir: &Path) -> bool {
    stopped_sentinel(hex_dir).exists()
}

// ---------------------------------------------------------------------------
// Engine probe (I/O; unit-tested with a TcpListener fixture, like iii_engine_health)
// ---------------------------------------------------------------------------

/// True iff something accepts a TCP connection on `addr` within a short timeout.
pub fn engine_listening(addr: &str) -> bool {
    let Ok(mut addrs) = addr.to_socket_addrs() else {
        return false;
    };
    addrs.any(|sa| TcpStream::connect_timeout(&sa, Duration::from_secs(2)).is_ok())
}

/// Poll `addr` until it serves or `timeout` elapses. Returns whether it came up.
pub fn wait_for_engine(addr: &str, timeout: Duration) -> bool {
    let deadline = Instant::now() + timeout;
    loop {
        if engine_listening(addr) {
            return true;
        }
        if Instant::now() >= deadline {
            return false;
        }
        std::thread::sleep(Duration::from_millis(500));
    }
}

// ---------------------------------------------------------------------------
// Pure decision logic (no I/O — the table-tested core)
// ---------------------------------------------------------------------------

/// daemon-green-independent health, so the decision tables don't depend on the external crate.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Health {
    Running,
    Stopped,
    Failed,
    NotInstalled,
}

impl Health {
    fn from_status(s: &daemon_green::ServiceStatus) -> Health {
        use daemon_green::ServiceStatus as S;
        match s {
            S::Running { .. } => Health::Running,
            S::Stopped => Health::Stopped,
            S::Failed { .. } => Health::Failed,
            S::NotInstalled => Health::NotInstalled,
        }
    }
}

/// What a single `ensure` pass should do.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum EnsureAction {
    /// Healthy — installed, running, engine serving. Quiet no-op.
    NoOp,
    /// Installed but dead/stopped/failed, or running-but-engine-not-serving → bootstrap.
    Reboot,
    /// Not installed at all → install plist then bootstrap.
    Install,
    /// Operator deliberately stopped it (sentinel present) → do nothing.
    SkipIntentionalDown,
}

/// Pure: decide what `ensure` should do. No I/O so it is exhaustively table-tested.
///
/// `engine_serving` distinguishes a job that is launchd-Running but whose in-process engine
/// has crashed (port dead) — that still needs a reboot.
pub fn decide_ensure(
    health: Health,
    engine_serving: bool,
    intentionally_down: bool,
) -> EnsureAction {
    if intentionally_down {
        return EnsureAction::SkipIntentionalDown;
    }
    match health {
        Health::NotInstalled => EnsureAction::Install,
        Health::Running if engine_serving => EnsureAction::NoOp,
        Health::Running | Health::Stopped | Health::Failed => EnsureAction::Reboot,
    }
}

/// Verdict of restart + verify (+ one retry).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum RestartVerdict {
    /// Engine serving after the first restart+poll.
    Healthy,
    /// Engine serving only after the single re-bootstrap.
    Recovered,
    /// Still dead after restart + re-bootstrap → escalate loudly.
    Escalate,
}

/// Pure: classify restart verification. `retry` is only meaningful when `first` is false.
pub fn classify_restart(serving_after_first: bool, serving_after_retry: bool) -> RestartVerdict {
    if serving_after_first {
        RestartVerdict::Healthy
    } else if serving_after_retry {
        RestartVerdict::Recovered
    } else {
        RestartVerdict::Escalate
    }
}

// ---------------------------------------------------------------------------
// Bootstrap lock (review R1)
// ---------------------------------------------------------------------------

/// Acquire the exclusive bootstrap lock (blocking). Held for the lifetime of the returned
/// file handle — drop it to release. Serializes restart paths against the watchdog so no two
/// processes ever race a launchd bootstrap.
fn acquire_bootstrap_lock(hex_dir: &Path) -> std::io::Result<std::fs::File> {
    use fs2::FileExt;
    let p = lock_path(hex_dir);
    if let Some(parent) = p.parent() {
        let _ = std::fs::create_dir_all(parent);
    }
    let f = std::fs::OpenOptions::new()
        .create(true)
        .write(true)
        .truncate(false)
        .open(&p)?;
    f.lock_exclusive()?;
    Ok(f)
}

// ---------------------------------------------------------------------------
// Service specs
// ---------------------------------------------------------------------------

fn path_env() -> String {
    let base = std::env::var("PATH").unwrap_or_else(|_| "/usr/bin:/bin".to_string());
    if base.split(':').any(|p| p == "/opt/homebrew/bin") {
        base
    } else {
        format!("/opt/homebrew/bin:{base}")
    }
}

/// Platform-neutral spec for `com.hex.harness` (`hex harness serve`). Mirrors the historical
/// plist: HEX_DIR/III_URL/PATH env, keep_alive + run_at_load, log under `.hex/logs`.
pub fn build_harness_spec(hex_dir: &Path) -> daemon_green::ServiceSpec {
    let hex_bin = hex_dir.join(".hex").join("bin").join("hex");
    let log_path = hex_dir
        .join(".hex")
        .join("logs")
        .join("com.hex.harness.log");
    daemon_green::ServiceSpec::new(HARNESS_LABEL, hex_bin)
        .args(["harness", "serve"])
        .env("HEX_DIR", hex_dir.to_string_lossy().into_owned())
        .env("III_URL", "ws://127.0.0.1:49134")
        .env("PATH", path_env())
        .env("GOOGLE_WORKSPACE_CLI_KEYRING_BACKEND", "file")
        .working_dir(hex_dir)
        .keep_alive(true)
        .run_at_load(true)
        .log_path(log_path)
}

/// Spec for `com.hex.harness-watchdog` (`hex harness watchdog`). A tiny KeepAlive peer whose
/// only job is to keep the harness alive. KeepAlive (not StartInterval — daemon-green's
/// ServiceSpec has no StartInterval) keeps the loop process itself up; it sleeps internally.
pub fn build_watchdog_spec(hex_dir: &Path) -> daemon_green::ServiceSpec {
    let hex_bin = hex_dir.join(".hex").join("bin").join("hex");
    let log_path = hex_dir
        .join(".hex")
        .join("logs")
        .join("com.hex.harness-watchdog.log");
    daemon_green::ServiceSpec::new(WATCHDOG_LABEL, hex_bin)
        .args(["harness", "watchdog"])
        .env("HEX_DIR", hex_dir.to_string_lossy().into_owned())
        .env("PATH", path_env())
        .working_dir(hex_dir)
        .keep_alive(true)
        .run_at_load(true)
        .log_path(log_path)
}

// ---------------------------------------------------------------------------
// Drivers (I/O — thin shells over daemon-green + the pure logic above)
// ---------------------------------------------------------------------------

/// Class-selection + emit seam for the harness-down escalation (task Tbnve3dk9).
///
/// A harness that fails to come back after restart + re-bootstrap is the
/// operator's "harness itself down" signal, so it rides the `HarnessDown` rail
/// (push urgent + email). Extracted from the `Escalate` arm so the mapping is
/// testable end-to-end through the alert module's `test_sink` without driving
/// real daemon-green I/O.
///
/// Deliberately the ONLY supervise alert mapped: `harness-watchdog-revive`
/// (below) fires on every recovery ACTION — including a successful revive — so
/// it is a recovery notice, not a harness-down page, and stays `Default`.
fn emit_harness_down_alert(hex_dir: &Path, msg: &str) {
    crate::alert::notify_at_with_class(
        hex_dir,
        "harness-restart-failed",
        "hex harness DOWN",
        msg,
        crate::alert::AlertClass::HarnessDown,
    );
}

/// Restart `label` then VERIFY the engine serves; one re-bootstrap on failure; escalate loud
/// (S6) if still dead. Holds the bootstrap lock so it cannot race the watchdog. Returns the
/// verdict, or `Err` (already alerted) when the harness is still down.
///
/// This replaces every bare `daemon_green::native().restart(label)` that previously swallowed
/// failure as a `[WARN]` and returned success.
pub fn restart_and_verify(hex_dir: &Path, label: &str) -> Result<RestartVerdict, String> {
    let _guard = acquire_bootstrap_lock(hex_dir).map_err(|e| format!("bootstrap lock: {e}"))?;
    let mgr = daemon_green::native();

    if let Err(e) = mgr.restart(label) {
        eprintln!("  [WARN] {label}: restart call failed: {e} — verifying / retrying anyway");
    }
    let serving_first = wait_for_engine(ENGINE_ADDR, VERIFY_TIMEOUT);

    let mut serving_retry = false;
    if !serving_first {
        eprintln!("  [WARN] {label}: engine not serving {VERIFY_TIMEOUT:?} after restart — one re-bootstrap");
        if let Err(e) = mgr.start(label) {
            eprintln!("  [WARN] {label}: re-bootstrap failed: {e}");
        }
        serving_retry = wait_for_engine(ENGINE_ADDR, VERIFY_TIMEOUT);
    }

    match classify_restart(serving_first, serving_retry) {
        RestartVerdict::Healthy => {
            println!("  [OK] {label} restarted — engine serving on {ENGINE_ADDR}");
            Ok(RestartVerdict::Healthy)
        }
        RestartVerdict::Recovered => {
            println!(
                "  [OK] {label} recovered after re-bootstrap — engine serving on {ENGINE_ADDR}"
            );
            Ok(RestartVerdict::Recovered)
        }
        RestartVerdict::Escalate => {
            let msg = format!(
                "{label} is DOWN after restart + re-bootstrap — engine not serving on {ENGINE_ADDR}. \
                 Recover with `hex harness start`."
            );
            eprintln!("  [FAIL] {msg}");
            emit_harness_down_alert(hex_dir, &msg);
            Err(msg)
        }
    }
}

/// One idempotent supervision pass over `com.hex.harness`. Quiet no-op when healthy.
/// Re-bootstraps a missing/dead harness, respecting the intentional-down sentinel and taking
/// the bootstrap lock (so it never races an in-flight upgrade/restart). Returns the action.
pub fn ensure_once(hex_dir: &Path) -> EnsureAction {
    let mgr = daemon_green::native();
    let health = mgr
        .status(HARNESS_LABEL)
        .map(|s| Health::from_status(&s))
        .unwrap_or(Health::Failed);
    let serving = engine_listening(ENGINE_ADDR);
    let action = decide_ensure(health, serving, is_intentionally_down(hex_dir));

    match action {
        EnsureAction::NoOp | EnsureAction::SkipIntentionalDown => action,
        EnsureAction::Install | EnsureAction::Reboot => {
            // Serialize with any in-flight restart/upgrade (R1).
            let _guard = match acquire_bootstrap_lock(hex_dir) {
                Ok(g) => g,
                Err(e) => {
                    eprintln!("[hex harness watchdog] WARN could not take bootstrap lock: {e}");
                    return action;
                }
            };
            // Re-check under the lock — an upgrade may have just healed it.
            if engine_listening(ENGINE_ADDR) {
                return EnsureAction::NoOp;
            }
            if action == EnsureAction::Install {
                if let Err(e) = mgr.install(&build_harness_spec(hex_dir)) {
                    eprintln!("[hex harness watchdog] WARN install failed: {e}");
                }
            }
            if let Err(e) = mgr.start(HARNESS_LABEL) {
                eprintln!("[hex harness watchdog] WARN bootstrap failed: {e}");
            }
            let ok = wait_for_engine(ENGINE_ADDR, VERIFY_TIMEOUT);
            let msg = format!(
                "watchdog re-bootstrapped {HARNESS_LABEL} (was {health:?}, action {action:?}); \
                 engine serving={ok}"
            );
            eprintln!("[hex harness watchdog] {msg}");
            crate::alert::notify_at(
                hex_dir,
                "harness-watchdog-revive",
                "hex harness revived",
                &msg,
            );
            action
        }
    }
}

/// The watchdog daemon body (`hex harness watchdog`): loop forever, one [`ensure_once`] pass
/// per [`WATCHDOG_INTERVAL`]. Its own launchd `KeepAlive` restarts it if it ever dies.
pub fn watchdog_loop(hex_dir: &Path) -> ! {
    eprintln!(
        "[hex harness watchdog] up — supervising {HARNESS_LABEL} every {WATCHDOG_INTERVAL:?}"
    );
    loop {
        let _ = ensure_once(hex_dir);
        std::thread::sleep(WATCHDOG_INTERVAL);
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::net::TcpListener;

    #[test]
    fn engine_listening_true_when_bound_false_when_not() {
        // Bound port → listening.
        let l = TcpListener::bind("127.0.0.1:0").unwrap();
        let addr = l.local_addr().unwrap().to_string();
        assert!(engine_listening(&addr), "should detect a bound listener");
        // Reserved unreachable port → not listening (fast).
        assert!(
            !engine_listening("127.0.0.1:1"),
            "nothing should listen on :1"
        );
    }

    #[test]
    fn wait_for_engine_returns_false_quickly_on_dead_port() {
        let start = Instant::now();
        assert!(!wait_for_engine("127.0.0.1:1", Duration::from_millis(700)));
        // Should honor the timeout, not hang.
        assert!(start.elapsed() < Duration::from_secs(5));
    }

    #[test]
    fn wait_for_engine_true_when_listener_present() {
        let l = TcpListener::bind("127.0.0.1:0").unwrap();
        let addr = l.local_addr().unwrap().to_string();
        assert!(wait_for_engine(&addr, Duration::from_secs(2)));
    }

    #[test]
    fn decide_ensure_intentional_down_always_skips() {
        for h in [
            Health::Running,
            Health::Stopped,
            Health::Failed,
            Health::NotInstalled,
        ] {
            for serving in [true, false] {
                assert_eq!(
                    decide_ensure(h, serving, true),
                    EnsureAction::SkipIntentionalDown,
                    "intentional-down must win for {h:?}/serving={serving}"
                );
            }
        }
    }

    #[test]
    fn decide_ensure_healthy_is_noop() {
        assert_eq!(
            decide_ensure(Health::Running, true, false),
            EnsureAction::NoOp
        );
    }

    #[test]
    fn decide_ensure_running_but_engine_dead_reboots() {
        // launchd says Running, but the in-process engine port is dead → crash → reboot.
        assert_eq!(
            decide_ensure(Health::Running, false, false),
            EnsureAction::Reboot
        );
    }

    #[test]
    fn decide_ensure_stopped_or_failed_reboots() {
        assert_eq!(
            decide_ensure(Health::Stopped, false, false),
            EnsureAction::Reboot
        );
        assert_eq!(
            decide_ensure(Health::Failed, false, false),
            EnsureAction::Reboot
        );
        // Even if the port happens to answer, a Stopped/Failed job should be rebooted.
        assert_eq!(
            decide_ensure(Health::Stopped, true, false),
            EnsureAction::Reboot
        );
    }

    #[test]
    fn decide_ensure_not_installed_installs() {
        assert_eq!(
            decide_ensure(Health::NotInstalled, false, false),
            EnsureAction::Install
        );
    }

    #[test]
    fn classify_restart_maps_all_three_verdicts() {
        assert_eq!(classify_restart(true, false), RestartVerdict::Healthy);
        assert_eq!(classify_restart(true, true), RestartVerdict::Healthy);
        assert_eq!(classify_restart(false, true), RestartVerdict::Recovered);
        assert_eq!(classify_restart(false, false), RestartVerdict::Escalate);
    }

    #[test]
    fn sentinel_roundtrip() {
        let tmp = std::env::temp_dir().join(format!("hex-supervise-test-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&tmp);
        assert!(!is_intentionally_down(&tmp));
        mark_intentionally_down(&tmp);
        assert!(is_intentionally_down(&tmp));
        clear_intentionally_down(&tmp);
        assert!(!is_intentionally_down(&tmp));
        let _ = std::fs::remove_dir_all(&tmp);
    }

    #[test]
    fn bootstrap_lock_is_acquirable_and_released_on_drop() {
        let tmp = std::env::temp_dir().join(format!("hex-supervise-lock-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&tmp);
        {
            let g = acquire_bootstrap_lock(&tmp).expect("first lock");
            drop(g);
        }
        // Re-acquire after drop must succeed.
        let _g2 = acquire_bootstrap_lock(&tmp).expect("re-lock after drop");
        let _ = std::fs::remove_dir_all(&tmp);
    }

    /// Call-site mapping (task Tbnve3dk9): the harness-down escalation must reach
    /// the email rail AND push at urgent priority — i.e. the production path
    /// selects `AlertClass::HarnessDown`. Drives the real `emit_harness_down_alert`
    /// against a configured rail and observes delivery through the alert module's
    /// `test_sink` (email fires only for the three named classes), so this is an
    /// end-to-end proof, not a constant assertion.
    #[test]
    fn harness_down_escalation_reaches_email_rail_urgent_class() {
        // Serialize on the crate HEX_DIR lock — test_sink is process-global and
        // shared with every other alert test `cargo test class` runs.
        let _g = crate::telemetry::test_support::lock_env();
        let tmp = tempfile::TempDir::new().unwrap();
        std::env::set_var("HEX_DIR", tmp.path());
        std::fs::create_dir_all(tmp.path().join(".hex/config")).unwrap();
        std::fs::write(
            tmp.path().join(".hex/config/alerts.toml"),
            "ntfy_topic_url = \"https://ntfy.example.invalid/t\"\n\
             email = \"ops@example.invalid\"\n",
        )
        .unwrap();
        crate::alert::test_sink::reset();

        // Pin the trigger→alert link: the `Escalate` verdict is the sole caller
        // of `emit_harness_down_alert` (restart + re-bootstrap both failed). If
        // the arms get rewired away from the helper this assertion still stands,
        // documenting exactly which verdict must reach the harness-down rail.
        assert_eq!(classify_restart(false, false), RestartVerdict::Escalate);

        emit_harness_down_alert(tmp.path(), "harness is DOWN after restart + re-bootstrap");

        let emails = crate::alert::test_sink::emails();
        let pushes = crate::alert::test_sink::pushes();
        assert_eq!(
            emails.len(),
            1,
            "harness-down must reach the email rail (HarnessDown); got {emails:?}"
        );
        assert_eq!(emails[0].to, "ops@example.invalid");
        assert_eq!(pushes.len(), 1, "harness-down pushes exactly once");
        assert_eq!(
            pushes[0].priority, "urgent",
            "harness-down must push at urgent priority: {pushes:?}"
        );
    }
}
