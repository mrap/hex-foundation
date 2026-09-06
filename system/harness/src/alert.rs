//! Loud alert pathway: stderr + telemetry row + macOS notification.
//! Deduped per key via a stamp file so a 15-min cron can call this every
//! tick without producing notification spam. Never fails the caller (S6:
//! observe loudly, never break the observed job).

use std::path::Path;
use std::time::{Duration, SystemTime};

const DEDUPE_WINDOW: Duration = Duration::from_secs(6 * 3600);

/// Returns true if the alert fired (not suppressed by dedupe).
pub fn notify(key: &str, title: &str, msg: &str) -> bool {
    let hex_dir = match std::env::var("HEX_DIR") {
        Ok(d) => std::path::PathBuf::from(d),
        Err(_) => {
            eprintln!("ALERT [{key}] {title}: {msg} (HEX_DIR unset — stderr only)");
            return true;
        }
    };
    notify_at(&hex_dir, key, title, msg)
}

/// Inner, testable form.
pub fn notify_at(hex_dir: &Path, key: &str, title: &str, msg: &str) -> bool {
    if suppressed(hex_dir, key) {
        return false;
    }
    eprintln!("ALERT [{key}] {title}: {msg}");
    let _ = crate::telemetry::record(&crate::telemetry::TelemetryEvent {
        source: "alert".into(),
        event: key.into(),
        status: "alert".into(),
        duration_ms: None,
        exit_code: None,
        detail: Some(format!("{title}: {msg}")),
    });
    #[cfg(all(target_os = "macos", not(test)))]
    {
        let script = format!(
            "display notification \"{}\" with title \"{}\"",
            msg.replace('"', "'"),
            title.replace('"', "'")
        );
        if let Err(e) = std::process::Command::new("osascript")
            .arg("-e")
            .arg(&script)
            .status()
        {
            eprintln!("alert [{key}]: osascript failed: {e}");
        }
    }
    stamp(hex_dir, key);
    true
}

/// Clear a dedupe stamp so the next `notify(key, …)` is guaranteed to fire.
///
/// Used when a watched condition ENDS (e.g. a BOI task leaves `blocked`) so the
/// next genuine episode re-alerts instead of being swallowed by the shared 6h
/// dedupe window. Without this, an unblock→re-block inside 6h stays silent (the
/// bug this closes). Never fails the caller (S6): a missing stamp is a no-op; an
/// unexpected IO error is logged loudly to stderr, not propagated.
pub fn clear(key: &str) {
    if let Ok(d) = std::env::var("HEX_DIR") {
        clear_at(std::path::Path::new(&d), key);
    }
}

/// Inner, testable form of [`clear`].
pub fn clear_at(hex_dir: &Path, key: &str) {
    let p = stamp_path(hex_dir, key);
    match std::fs::remove_file(&p) {
        Ok(()) => {}
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => {}
        Err(e) => eprintln!("alert [{key}]: stamp clear failed: {e}"),
    }
}

fn stamp_path(hex_dir: &Path, key: &str) -> std::path::PathBuf {
    hex_dir.join(".hex/run/alerts").join(format!("{key}.last"))
}

fn suppressed(hex_dir: &Path, key: &str) -> bool {
    stamp_path(hex_dir, key)
        .metadata()
        .and_then(|m| m.modified())
        .map(|t| SystemTime::now().duration_since(t).unwrap_or(Duration::MAX) < DEDUPE_WINDOW)
        .unwrap_or(false)
}

fn stamp(hex_dir: &Path, key: &str) {
    let p = stamp_path(hex_dir, key);
    if let Some(parent) = p.parent() {
        let _ = std::fs::create_dir_all(parent);
    }
    if let Err(e) = std::fs::write(&p, b"") {
        eprintln!("alert [{key}]: stamp write failed: {e}");
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn dedupe_suppresses_within_window() {
        // Mutates the process-global HEX_DIR — hold the crate's single env
        // lock (telemetry/mod.rs contract) so parallel tests don't race.
        let _g = crate::telemetry::test_support::lock_env();
        let tmp = tempfile::TempDir::new().unwrap();
        // Keep the inner telemetry write hermetic (telemetry resolves
        // events.db from $HEX_DIR) — same pattern as telemetry/mod.rs tests.
        std::env::set_var("HEX_DIR", tmp.path());
        assert!(notify_at(tmp.path(), "test-key", "t", "m"));
        assert!(!notify_at(tmp.path(), "test-key", "t", "m")); // suppressed
    }

    #[test]
    fn clear_stamp_reenables_notify() {
        let _g = crate::telemetry::test_support::lock_env();
        let tmp = tempfile::TempDir::new().unwrap();
        std::env::set_var("HEX_DIR", tmp.path());
        assert!(notify_at(tmp.path(), "clear-key", "t", "m"));
        assert!(!notify_at(tmp.path(), "clear-key", "t", "m")); // suppressed by stamp
        clear_at(tmp.path(), "clear-key"); // unblock clears the stamp
        assert!(
            notify_at(tmp.path(), "clear-key", "t", "m"),
            "after clear, the next notify must fire again (re-block re-alert)"
        );
        // Clearing an absent stamp is a quiet no-op, never a panic.
        clear_at(tmp.path(), "never-stamped-key");
    }
}
