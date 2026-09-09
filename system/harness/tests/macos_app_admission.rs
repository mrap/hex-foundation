//! Actual CLI failure ordering. No real launchd mutation or signing is allowed.
#![cfg(target_os = "macos")]

use std::fs;
use std::os::unix::fs::PermissionsExt;
use std::path::Path;
use std::process::Command;

fn command(root: &Path, subcommand: &str) -> std::process::Output {
    let home = root.join("home");
    let hex = root.join("hex");
    fs::create_dir_all(home.join("Library/LaunchAgents")).unwrap();
    fs::write(
        home.join("Library/LaunchAgents/com.hex.harness.plist"),
        b"fixture service record",
    )
    .unwrap();
    fs::create_dir_all(home.join("Library/Application Support/Hex/build-signing")).unwrap();
    fs::write(
        home.join("Library/Application Support/Hex/build-signing/policy.json"),
        b"{}",
    )
    .unwrap();
    fs::create_dir_all(&hex).unwrap();
    fs::write(hex.join("CLAUDE.md"), b"fixture").unwrap();
    fs::create_dir_all(root.join("bin")).unwrap();
    let spy = root.join("bin/launchctl");
    fs::write(
        &spy,
        "#!/bin/sh\nprintf '%s\\n' \"$*\" >> \"$HOME/launchctl-calls\"\nexit 1\n",
    )
    .unwrap();
    fs::set_permissions(&spy, fs::Permissions::from_mode(0o755)).unwrap();
    Command::new(env!("CARGO_BIN_EXE_hex"))
        .args(["harness", subcommand])
        .env_clear()
        .env("HOME", &home)
        .env("HEX_DIR", &hex)
        .env(
            "PATH",
            format!("{}:/usr/bin:/bin", root.join("bin").display()),
        )
        .output()
        .unwrap()
}

#[test]
fn start_and_restart_reject_bad_install_before_any_service_call() {
    for subcommand in ["start", "restart"] {
        let temp = tempfile::tempdir().unwrap();
        let sentinel = temp.path().join("hex/.hex/run/harness-stopped");
        fs::create_dir_all(sentinel.parent().unwrap()).unwrap();
        fs::write(&sentinel, b"existing stop marker").unwrap();
        let output = command(temp.path(), subcommand);
        assert!(!output.status.success(), "{subcommand}");
        assert!(
            String::from_utf8_lossy(&output.stderr).contains("app identity preflight"),
            "{}",
            String::from_utf8_lossy(&output.stderr)
        );
        assert!(!temp.path().join("home/launchctl-calls").exists());
        assert!(!temp.path().join("hex/.hex/logs").exists());
        assert_eq!(
            fs::read(
                temp.path()
                    .join("home/Library/LaunchAgents/com.hex.harness.plist")
            )
            .unwrap(),
            b"fixture service record"
        );
        assert_eq!(fs::read(&sentinel).unwrap(), b"existing stop marker");
    }
}

#[test]
fn watchdog_recovery_only_reads_service_status_when_install_is_invalid() {
    let temp = tempfile::tempdir().unwrap();
    let output = command(temp.path(), "ensure");
    assert!(!output.status.success());
    assert!(String::from_utf8_lossy(&output.stderr).contains("blocks recovery"));
    let calls = fs::read_to_string(temp.path().join("home/launchctl-calls")).unwrap();
    assert!(!calls.is_empty());
    assert!(
        calls.lines().all(|line| line.starts_with("print ")),
        "{calls}"
    );
    assert!(!temp
        .path()
        .join("hex/.hex/run/harness-bootstrap.lock")
        .exists());
    assert!(!temp.path().join("hex/.hex/logs").exists());
}
