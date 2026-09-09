//! Actual CLI failure ordering. No real launchd mutation or signing is allowed.
#![cfg(target_os = "macos")]

use std::fs;
use std::os::unix::fs::PermissionsExt;
use std::path::Path;
use std::process::Command;

fn install_signed_fixture(root: &Path) {
    let home = root.join("home");
    let hex = root.join("hex");
    let source = root.join("source");
    fs::create_dir_all(&hex).unwrap();
    fs::write(hex.join("CLAUDE.md"), b"fixture").unwrap();
    let source_scripts = source.join("system/scripts");
    fs::create_dir_all(&source_scripts).unwrap();
    fs::create_dir_all(home.join("Library/Application Support/Hex/build-signing")).unwrap();
    fs::write(
        home.join("Library/Application Support/Hex/build-signing/policy.json"),
        br#"{"schema_version":1,"certificate_sha1":"AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA","team_id":"TEAM123456"}"#,
    )
    .unwrap();
    let source_installer = Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("../../system/scripts/macos-app-install.py")
        .canonicalize()
        .unwrap();
    fs::copy(
        &source_installer,
        source_scripts.join("macos-app-install.py"),
    )
    .unwrap();
    let signer = source_scripts.join("macos-signing.py");
    let signer_source = r#"import json, pathlib, shutil, sys
def _read_policy(path):
    value=json.loads(path.read_text())
    assert value['schema_version']==1 and len(value['certificate_sha1'])==40 and len(value['team_id'])==10
    return value
def _result():
    return {'identifier':'com.mrap.hex','version':'1.0.0','team_id':'TEAM123456','certificate_sha1':'A'*40,'designated_requirements':{'arm64':'anchor apple generic'},'mach_o_uuids':{'arm64':'11111111-1111-1111-1111-111111111111'}}
def main():
    args=sys.argv[1:]
    if args[0]=='verify-installed':
        _read_policy(pathlib.Path(args[3]))
        print(json.dumps(_result()))
    else:
        source=pathlib.Path(args[0]); candidate=pathlib.Path(args[3]); candidate.joinpath('Contents/MacOS').mkdir(parents=True); shutil.copy2(source,candidate/'Contents/MacOS/hex'); (candidate/'Contents/Info.plist').write_bytes(b'fixture plist'); print(json.dumps(_result()))
if __name__ == '__main__':
    main()
"#
    .replace("1.0.0", env!("CARGO_PKG_VERSION"));
    fs::write(&signer, signer_source).unwrap();
    let source_bin = source.join("hex-source");
    fs::write(&source_bin, b"fixture hex executable").unwrap();
    let output = Command::new("/usr/bin/python3")
        .args([
            "-I",
            "-B",
            source_scripts
                .join("macos-app-install.py")
                .to_str()
                .unwrap(),
            "install",
            "hex",
            "--root",
            hex.join(".hex").to_str().unwrap(),
            "--source",
            source_bin.to_str().unwrap(),
            "--version",
            env!("CARGO_PKG_VERSION"),
            "--source-revision",
            &"e".repeat(40),
            "--helper-source-revision",
            &"f".repeat(40),
            "--policy",
            home.join("Library/Application Support/Hex/build-signing/policy.json")
                .to_str()
                .unwrap(),
        ])
        .env_clear()
        .env("HOME", &home)
        .output()
        .unwrap();
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    assert!(hex.join(".hex/Hex.app.install-state.json").is_file());
    assert!(hex.join(".hex/bin/hex-agent").is_symlink());
}

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

#[test]
fn start_uses_verified_app_owner_and_common_installer() {
    let temp = tempfile::tempdir().unwrap();
    install_signed_fixture(temp.path());
    let home = temp.path().join("home");
    let hex = temp.path().join("hex");
    let spy = temp.path().join("launchctl");
    fs::write(
        &spy,
        "#!/bin/sh\nprintf '%s\\n' \"$*\" >> \"$HOME/launchctl-calls\"\ncase \"$1\" in print) exit 0;; *) exit 0;; esac\n",
    )
    .unwrap();
    fs::set_permissions(&spy, fs::Permissions::from_mode(0o755)).unwrap();
    let output = Command::new(env!("CARGO_BIN_EXE_hex"))
        .args(["harness", "start"])
        .env_clear()
        .env("HOME", &home)
        .env("HEX_DIR", &hex)
        .env("PATH", format!("{}:/usr/bin:/bin", temp.path().display()))
        .output()
        .unwrap();
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    let calls = fs::read_to_string(home.join("launchctl-calls")).unwrap();
    assert!(calls.contains("bootstrap"), "{calls}");
    let plist =
        fs::read_to_string(home.join("Library/LaunchAgents/com.hex.harness.plist")).unwrap();
    assert!(plist.contains("Hex.app/Contents/MacOS/hex"), "{plist}");
    assert!(plist.contains("com.mrap.hex"), "{plist}");
    assert!(hex.join(".hex/Hex.app.install-state.json").is_file());
}
