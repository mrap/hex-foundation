//! Shared macOS app-install callers. Signing and transaction rules live in Python.
//!
//! A missing policy is not permission to overwrite a previously signed app.
//! These checks coordinate trusted local installers, not hostile account owners.

use std::fs;
use std::io;
use std::path::{Path, PathBuf};

pub const HEX_APP_ID: &str = "com.mrap.hex";
const POLICY_RELATIVE: &str = "Library/Application Support/Hex/build-signing/policy.json";

#[derive(Clone, Debug)]
pub struct AppPaths {
    pub home: PathBuf,
    pub root: PathBuf,
    pub policy: PathBuf,
    pub app: PathBuf,
    pub executable: PathBuf,
    pub cli: PathBuf,
    pub state: PathBuf,
    pub lock: PathBuf,
    pub journal: PathBuf,
}

impl AppPaths {
    pub fn new(home: &Path, hex_dir: &Path) -> io::Result<Self> {
        if !home.is_absolute() || !hex_dir.is_absolute() {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "app paths must be absolute",
            ));
        }
        let root = hex_dir.join(".hex");
        let app = root.join("Hex.app");
        Ok(Self {
            home: home.to_owned(),
            policy: home.join(POLICY_RELATIVE),
            executable: app.join("Contents/MacOS/hex"),
            app,
            cli: root.join("bin/hex"),
            state: root.join("Hex.app.install-state.json"),
            lock: root.join(".hex.app-install.lock"),
            journal: root.join(".hex.app-install.journal.json"),
            root,
        })
    }

    /// Conservative admission only. The shared helper validates actual state.
    /// Dangling links and incomplete journals count as managed evidence.
    pub fn managed_evidence(&self) -> io::Result<bool> {
        for path in [&self.policy, &self.app, &self.state, &self.journal] {
            if entry_present(path)? {
                return Ok(true);
            }
        }
        match fs::symlink_metadata(&self.cli) {
            Ok(metadata) if metadata.file_type().is_symlink() => Ok(true),
            Ok(metadata) if metadata.is_file() => Ok(false),
            Ok(_) => Err(io::Error::other("unexpected Hex CLI file type")),
            Err(error) if error.kind() == io::ErrorKind::NotFound => Ok(false),
            Err(error) => Err(error),
        }
    }
}

fn entry_present(path: &Path) -> io::Result<bool> {
    match fs::symlink_metadata(path) {
        Ok(_) => Ok(true),
        Err(error) if error.kind() == io::ErrorKind::NotFound => Ok(false),
        Err(error) => Err(error),
    }
}

use std::process::Stdio;
use std::time::Duration;
use tokio::io::{AsyncRead, AsyncReadExt};
use tokio::process::Command;

#[derive(Debug)]
struct AppIdentityError(String);
impl std::fmt::Display for AppIdentityError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "signed-app check: {}", self.0)
    }
}
impl std::error::Error for AppIdentityError {}

const OUTPUT_LIMIT: u64 = 64 * 1024;

async fn bounded_read(reader: impl AsyncRead + Unpin) -> Result<Vec<u8>, AppIdentityError> {
    let mut bytes = Vec::new();
    reader
        .take(OUTPUT_LIMIT + 1)
        .read_to_end(&mut bytes)
        .await
        .map_err(|e| AppIdentityError(format!("verifier output read failed: {e}")))?;
    if bytes.len() as u64 > OUTPUT_LIMIT {
        return Err(AppIdentityError("verifier output exceeds limit".into()));
    }
    Ok(bytes)
}

// The child has a new group and is intentionally not reaped before output is
// drained. Keeping its PID allocated prevents group-ID reuse during cleanup.
#[allow(unsafe_code)]
fn kill_unreaped_group(pid: u32) -> Result<(), AppIdentityError> {
    let pid = i32::try_from(pid)
        .map_err(|_| AppIdentityError("verifier PID exceeds platform range".into()))?;
    if pid <= 1 {
        return Err(AppIdentityError("invalid verifier process group".into()));
    }
    // SAFETY: this is the group ID assigned to our still-unreaped child.
    if unsafe { libc::kill(-pid, libc::SIGKILL) } != 0 {
        let error = std::io::Error::last_os_error();
        if error.raw_os_error() != Some(libc::ESRCH) {
            return Err(AppIdentityError(format!(
                "verifier group cleanup failed: {error}"
            )));
        }
    }
    Ok(())
}

struct VerifierChild {
    child: tokio::process::Child,
    pid: u32,
    armed: bool,
}

impl Drop for VerifierChild {
    fn drop(&mut self) {
        if self.armed {
            if let Err(error) = kill_unreaped_group(self.pid) {
                eprintln!("signed-app cleanup on cancellation failed: {error}");
            }
        }
        // Child's kill_on_drop also requests direct-child cleanup/reaping.
    }
}

async fn run_verifier(
    program: &Path,
    args: &[&std::ffi::OsStr],
    home: &Path,
    lock_fd: Option<std::os::fd::RawFd>,
    timeout: Duration,
) -> Result<Vec<u8>, AppIdentityError> {
    let mut command = Command::new(program);
    command
        .args(args)
        .env_clear()
        .env("HOME", home)
        .env("PATH", "/usr/bin:/bin")
        .env("LC_ALL", "C")
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .process_group(0)
        .kill_on_drop(true);
    if let Some(fd) = lock_fd {
        // Only the forked child inherits this cooperative product lock.
        // The parent descriptor keeps CLOEXEC and remains owned by its guard.
        #[allow(unsafe_code)]
        unsafe {
            command.pre_exec(move || {
                let flags = libc::fcntl(fd, libc::F_GETFD);
                if flags < 0 || libc::fcntl(fd, libc::F_SETFD, flags & !libc::FD_CLOEXEC) < 0 {
                    return Err(io::Error::last_os_error());
                }
                Ok(())
            });
        }
    }
    let child = command
        .spawn()
        .map_err(|e| AppIdentityError(format!("verifier launch failed: {e}")))?;
    let pid = child
        .id()
        .ok_or_else(|| AppIdentityError("verifier has no PID".into()))?;
    let mut process = VerifierChild {
        child,
        pid,
        armed: true,
    };
    let stdout = process
        .child
        .stdout
        .take()
        .ok_or_else(|| AppIdentityError("verifier stdout unavailable".into()))?;
    let stderr = process
        .child
        .stderr
        .take()
        .ok_or_else(|| AppIdentityError("verifier stderr unavailable".into()))?;
    let deadline = tokio::time::Instant::now() + timeout;
    let output = tokio::time::timeout_at(deadline, async {
        let (stdout, stderr) = tokio::try_join!(bounded_read(stdout), bounded_read(stderr))?;
        Ok::<_, AppIdentityError>((stdout, stderr))
    })
    .await;
    let output = match output {
        Ok(Ok(output)) => output,
        other => {
            let cleanup = kill_unreaped_group(pid);
            let reap = tokio::time::timeout(Duration::from_secs(5), process.child.wait()).await;
            if matches!(reap, Ok(Ok(_))) {
                process.armed = false;
            }
            cleanup?;
            if !matches!(reap, Ok(Ok(_))) {
                return Err(AppIdentityError(
                    "verifier cleanup could not reap child".into(),
                ));
            }
            return Err(match other {
                Ok(Err(error)) => error,
                _ => AppIdentityError("verifier timed out".into()),
            });
        }
    };
    let status = match tokio::time::timeout_at(deadline, process.child.wait()).await {
        Ok(Ok(status)) => {
            process.armed = false;
            status
        }
        other => {
            let cleanup = kill_unreaped_group(pid);
            let reap = tokio::time::timeout(Duration::from_secs(5), process.child.wait()).await;
            if matches!(reap, Ok(Ok(_))) {
                process.armed = false;
            }
            cleanup?;
            if !matches!(reap, Ok(Ok(_))) {
                return Err(AppIdentityError(
                    "verifier cleanup could not reap child".into(),
                ));
            }
            return Err(AppIdentityError(match other {
                Ok(Err(error)) => format!("verifier wait failed: {error}"),
                _ => "verifier timed out".into(),
            }));
        }
    };
    if !status.success() {
        // The helper only receives public paths/policy. Bound its diagnostic;
        // no inherited provider environment reaches this child.
        return Err(AppIdentityError(format!(
            "verifier failed ({status}): {}",
            String::from_utf8_lossy(&output.1)
        )));
    }
    if !output.1.is_empty() {
        return Err(AppIdentityError(
            "verifier returned unexpected stderr".into(),
        ));
    }
    Ok(output.0)
}

/// A dedicated short-lived runtime also works from an existing Tokio caller.
/// The join keeps borrowed lock ownership alive through the entire operation.
fn run_python(
    args: &[std::ffi::OsString],
    home: &Path,
    lock_fd: Option<std::os::fd::RawFd>,
    timeout: Duration,
) -> io::Result<Vec<u8>> {
    std::thread::scope(|scope| {
        scope
            .spawn(|| {
                let runtime = tokio::runtime::Builder::new_current_thread()
                    .enable_all()
                    .build()?;
                let argv = args
                    .iter()
                    .map(std::ffi::OsString::as_os_str)
                    .collect::<Vec<_>>();
                runtime
                    .block_on(run_verifier(
                        Path::new("/usr/bin/python3"),
                        &argv,
                        home,
                        lock_fd,
                        timeout,
                    ))
                    .map_err(io::Error::other)
            })
            .join()
            .map_err(|_| io::Error::other("app helper thread panicked"))?
    })
}

const BOOTSTRAP: &str = r#"import hashlib, os, stat, sys
root, signing_hash, install_hash = sys.argv[1:4]
contents = {}
for name, expected in [('macos-signing.py', signing_hash), ('macos-app-install.py', install_hash)]:
    path = os.path.join(root, 'libexec', name)
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    with os.fdopen(fd, 'rb') as stream:
        mode = os.fstat(stream.fileno()).st_mode
        if not stat.S_ISREG(mode) or mode & (stat.S_ISUID | stat.S_ISGID | stat.S_ISVTX):
            raise ValueError('helper must be a regular file without special mode bits')
        source = stream.read(1048577)
    if len(source) > 1048576 or hashlib.sha256(source).hexdigest() != expected:
        raise ValueError('installed helper provenance mismatch: ' + name)
    contents[name] = source
path = os.path.join(root, 'libexec', 'macos-app-install.py')
sys.argv = [path] + sys.argv[4:]
sys.path.insert(0, os.path.dirname(path))
globals()['__file__'] = path
exec(compile(contents['macos-app-install.py'], path, 'exec'), globals())
"#;

#[derive(serde::Deserialize)]
struct HelperState {
    helpers: Helpers,
}

#[derive(serde::Deserialize)]
#[serde(deny_unknown_fields)]
struct Helpers {
    #[serde(rename = "macos-signing.py")]
    signing: HelperRecord,
    #[serde(rename = "macos-app-install.py")]
    install: HelperRecord,
}

#[derive(serde::Deserialize)]
#[serde(deny_unknown_fields)]
struct HelperRecord {
    sha256: String,
    source_revision: String,
}

fn read_regular(path: &Path, limit: u64) -> io::Result<Vec<u8>> {
    use std::io::Read;
    use std::os::unix::fs::OpenOptionsExt;
    let file = fs::OpenOptions::new()
        .read(true)
        .custom_flags(libc::O_NOFOLLOW | libc::O_NONBLOCK | libc::O_CLOEXEC)
        .open(path)?;
    if !file.metadata()?.is_file() {
        return Err(io::Error::other(format!(
            "not a regular file: {}",
            path.display()
        )));
    }
    let mut bytes = Vec::new();
    file.take(limit + 1).read_to_end(&mut bytes)?;
    if bytes.len() as u64 > limit {
        return Err(io::Error::other(format!(
            "file too large: {}",
            path.display()
        )));
    }
    Ok(bytes)
}

fn check_helper(path: &Path, record: &HelperRecord) -> io::Result<()> {
    use sha2::{Digest, Sha256};
    if record.sha256.len() != 64
        || !record.sha256.bytes().all(|b| b.is_ascii_hexdigit())
        || record.source_revision.len() != 40
        || !record
            .source_revision
            .bytes()
            .all(|b| b.is_ascii_hexdigit())
    {
        return Err(io::Error::other("invalid installed helper provenance"));
    }
    let actual = format!("{:x}", Sha256::digest(read_regular(path, 1024 * 1024)?));
    if actual != record.sha256.to_ascii_lowercase() {
        return Err(io::Error::other(format!(
            "installed helper hash mismatch: {}",
            path.display()
        )));
    }
    Ok(())
}

#[derive(serde::Deserialize)]
struct OwnerResult {
    schema_version: u32,
    product: String,
    mode: String,
    executable_path: PathBuf,
    bundle_identifier: String,
    generation: String,
}

/// The guard is held across the caller's service operation.
pub struct VerifiedOwner {
    executable: PathBuf,
    signed: bool,
    _lock: Option<fs::File>,
}

impl VerifiedOwner {
    pub fn executable(&self) -> &Path {
        &self.executable
    }
    pub fn is_signed(&self) -> bool {
        self.signed
    }

    #[cfg(test)]
    pub(crate) fn fixture(executable: PathBuf, signed: bool) -> Self {
        Self {
            executable,
            signed,
            _lock: None,
        }
    }
}

fn verified_owner_at(paths: AppPaths) -> io::Result<VerifiedOwner> {
    use fs2::FileExt;
    use std::os::fd::AsRawFd;
    use std::os::unix::fs::OpenOptionsExt;
    if !paths.managed_evidence()? {
        return Ok(VerifiedOwner {
            executable: paths.cli,
            signed: false,
            _lock: None,
        });
    }
    // Service operations do not create installation directories or repair state.
    for dir in [
        &paths.root,
        &paths.root.join("bin"),
        &paths.root.join("libexec"),
    ] {
        if !fs::symlink_metadata(dir)?.is_dir() {
            return Err(io::Error::other(
                "signed app parent is not a real directory",
            ));
        }
    }
    let lock = fs::OpenOptions::new()
        .read(true)
        .write(true)
        .custom_flags(libc::O_NOFOLLOW | libc::O_NONBLOCK | libc::O_CLOEXEC)
        .open(&paths.lock)?;
    if !lock.metadata()?.is_file() {
        return Err(io::Error::other("invalid app install lock"));
    }
    lock.try_lock_exclusive()
        .map_err(|e| io::Error::new(e.kind(), format!("app install busy or unavailable: {e}")))?;
    if entry_present(&paths.journal)? {
        return Err(io::Error::other(
            "incomplete app install blocks service changes",
        ));
    }
    if !entry_present(&paths.policy)? {
        return Err(io::Error::other(
            "central signing policy missing; service registration is unchanged",
        ));
    }
    let provenance: HelperState = serde_json::from_slice(&read_regular(&paths.state, 128 * 1024)?)
        .map_err(io::Error::other)?;
    let helper = paths.root.join("libexec/macos-app-install.py");
    check_helper(&helper, &provenance.helpers.install)?;
    check_helper(
        &paths.root.join("libexec/macos-signing.py"),
        &provenance.helpers.signing,
    )?;
    let args = vec![
        "-I".into(),
        "-B".into(),
        "-c".into(),
        BOOTSTRAP.into(),
        paths.root.clone().into_os_string(),
        provenance.helpers.signing.sha256.into(),
        provenance.helpers.install.sha256.into(),
        "service-owner".into(),
        "hex".into(),
        "--root".into(),
        paths.root.clone().into_os_string(),
        "--lock-fd".into(),
        lock.as_raw_fd().to_string().into(),
    ];
    let bytes = run_python(
        &args,
        &paths.home,
        Some(lock.as_raw_fd()),
        Duration::from_secs(120),
    )?;
    let owner: OwnerResult = serde_json::from_slice(&bytes).map_err(io::Error::other)?;
    if owner.schema_version != 1
        || owner.product != "hex"
        || owner.mode != "signed-current"
        || owner.executable_path != paths.executable
        || owner.bundle_identifier != HEX_APP_ID
        || owner.generation.is_empty()
    {
        return Err(io::Error::other(
            "shared helper returned an unexpected service owner",
        ));
    }
    Ok(VerifiedOwner {
        executable: paths.executable,
        signed: true,
        _lock: Some(lock),
    })
}

pub fn verified_owner(hex_dir: &Path) -> io::Result<VerifiedOwner> {
    if !cfg!(target_os = "macos") {
        return Ok(VerifiedOwner {
            executable: hex_dir.join(".hex/bin/hex"),
            signed: false,
            _lock: None,
        });
    }
    let home = std::env::var_os("HOME")
        .map(PathBuf::from)
        .ok_or_else(|| io::Error::other("HOME is required for the shared signing policy"))?;
    verified_owner_at(AppPaths::new(&home, hex_dir)?)
}

#[derive(Debug, PartialEq, Eq)]
pub enum UpgradeMode {
    Legacy,
    Migrate,
    Signed(String),
}

fn current_paths(hex_dir: &Path) -> io::Result<AppPaths> {
    let home = std::env::var_os("HOME")
        .map(PathBuf::from)
        .ok_or_else(|| io::Error::other("HOME is required for the shared signing policy"))?;
    AppPaths::new(&home, hex_dir)
}

#[derive(serde::Deserialize)]
struct ModeResult {
    schema_version: u32,
    product: String,
    mode: String,
    source_revision: Option<String>,
    policy_available: Option<bool>,
    managed: Option<bool>,
}

fn source_command(
    paths: &AppPaths,
    source_dir: &Path,
    command: &str,
    extra: &[std::ffi::OsString],
) -> io::Result<ModeResult> {
    let helper = source_dir.join("system/scripts/macos-app-install.py");
    // Selected Foundation source is the updater's trust input. Do not substitute
    // an older installed helper or an executable found through PATH.
    read_regular(&helper, 1024 * 1024)?;
    read_regular(
        &source_dir.join("system/scripts/macos-signing.py"),
        1024 * 1024,
    )?;
    let mut args = vec![
        "-I".into(),
        "-B".into(),
        helper.into_os_string(),
        command.into(),
        "hex".into(),
        "--root".into(),
        paths.root.clone().into_os_string(),
    ];
    args.extend_from_slice(extra);
    let output = run_python(&args, &paths.home, None, Duration::from_secs(300))?;
    let result: ModeResult = serde_json::from_slice(&output).map_err(io::Error::other)?;
    if result.schema_version != 1 || result.product != "hex" {
        return Err(io::Error::other("unexpected app-installer result"));
    }
    Ok(result)
}

pub fn prepare_upgrade(hex_dir: &Path, source_dir: &Path) -> io::Result<UpgradeMode> {
    if !cfg!(target_os = "macos") {
        return Ok(UpgradeMode::Legacy);
    }
    let paths = current_paths(hex_dir)?;
    prepare_upgrade_at(&paths, source_dir)
}

fn prepare_upgrade_at(paths: &AppPaths, source_dir: &Path) -> io::Result<UpgradeMode> {
    if !paths.managed_evidence()? {
        return Ok(UpgradeMode::Legacy);
    }
    let result = source_command(paths, source_dir, "preflight", &[])?;
    if result.policy_available != Some(true) || result.managed != Some(true) {
        return Err(io::Error::other(
            "managed preflight did not verify the signing policy",
        ));
    }
    match result.mode.as_str() {
        "configured-legacy" | "empty" => {
            if !entry_present(&paths.policy)? {
                return Err(io::Error::other(
                    "signing policy disappeared during preflight",
                ));
            }
            Ok(UpgradeMode::Migrate)
        }
        "signed-current" => {
            let revision = result
                .source_revision
                .filter(|s| s.len() == 40 && s.bytes().all(|b| b.is_ascii_hexdigit()))
                .ok_or_else(|| {
                    io::Error::other("verified installation lacks product source revision")
                })?;
            Ok(UpgradeMode::Signed(revision))
        }
        mode => Err(io::Error::other(format!(
            "app state blocks upgrade: {mode}"
        ))),
    }
}

pub fn install_build(
    hex_dir: &Path,
    source_dir: &Path,
    executable: &Path,
    version: &str,
    revision: &str,
) -> io::Result<()> {
    let paths = current_paths(hex_dir)?;
    let extra = vec![
        "--source".into(),
        executable.as_os_str().to_owned(),
        "--version".into(),
        version.into(),
        "--source-revision".into(),
        revision.into(),
        "--helper-source-revision".into(),
        revision.into(),
    ];
    let result = source_command(&paths, source_dir, "install", &extra)?;
    if result.mode != "signed-current" || result.source_revision.as_deref() != Some(revision) {
        return Err(io::Error::other(
            "app installer did not commit the requested signed source",
        ));
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::os::unix::fs::symlink;

    #[test]
    fn raw_legacy_is_distinct_from_every_signed_evidence_path() {
        let temp = tempfile::tempdir().unwrap();
        let paths = AppPaths::new(temp.path(), &temp.path().join("hex")).unwrap();
        assert!(!paths.managed_evidence().unwrap());
        fs::create_dir_all(paths.cli.parent().unwrap()).unwrap();
        fs::write(&paths.cli, b"legacy raw").unwrap();
        assert!(!paths.managed_evidence().unwrap());
        for path in [&paths.policy, &paths.app, &paths.state, &paths.journal] {
            fs::create_dir_all(path.parent().unwrap()).unwrap();
            symlink("missing", path).unwrap();
            assert!(paths.managed_evidence().unwrap(), "{}", path.display());
            fs::remove_file(path).unwrap();
        }
        fs::remove_file(&paths.cli).unwrap();
        symlink(&paths.executable, &paths.cli).unwrap();
        assert!(paths.managed_evidence().unwrap());
    }

    #[test]
    fn invalid_cli_type_is_not_legacy() {
        let temp = tempfile::tempdir().unwrap();
        let paths = AppPaths::new(temp.path(), &temp.path().join("hex")).unwrap();
        fs::create_dir_all(&paths.cli).unwrap();
        assert!(paths.managed_evidence().is_err());
        assert!(AppPaths::new(Path::new("relative"), temp.path()).is_err());
    }

    fn fixture(paths: &AppPaths, behavior: &str) {
        use sha2::{Digest, Sha256};
        fs::create_dir_all(paths.root.join("libexec")).unwrap();
        fs::create_dir_all(paths.cli.parent().unwrap()).unwrap();
        fs::create_dir_all(paths.policy.parent().unwrap()).unwrap();
        fs::write(&paths.policy, b"fixture public policy").unwrap();
        fs::write(&paths.lock, b"").unwrap();
        let script = format!(
            r#"import sys, os, json, fcntl
from pathlib import Path
root=Path(sys.argv[sys.argv.index('--root')+1])
fd=int(sys.argv[sys.argv.index('--lock-fd')+1])
assert sys.argv[1:3] == ['service-owner','hex']
assert os.fstat(fd).st_ino == os.stat(root/'.hex.app-install.lock').st_ino
with open(root/'.hex.app-install.lock','rb') as other:
    try: fcntl.flock(other.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
    except BlockingIOError: pass
    else: raise AssertionError('parent lock not held')
(root/'called').write_text('yes')
result={{'schema_version':1,'product':'hex','mode':'signed-current','executable_path':str(root/'Hex.app/Contents/MacOS/hex'),'bundle_identifier':'com.mrap.hex','generation':'generation-a'}}
{behavior}
"#
        );
        let mut helpers = serde_json::Map::new();
        for (name, content) in [
            ("macos-app-install.py", script.as_bytes()),
            ("macos-signing.py", b"# fixture only\n".as_slice()),
        ] {
            fs::write(paths.root.join("libexec").join(name), content).unwrap();
            helpers.insert(name.into(), serde_json::json!({"sha256":format!("{:x}",Sha256::digest(content)),"source_revision":"a".repeat(40)}));
        }
        fs::write(
            &paths.state,
            serde_json::to_vec(&serde_json::json!({"helpers":helpers})).unwrap(),
        )
        .unwrap();
    }

    #[test]
    fn actual_helper_admission_holds_lock_through_owner_lifetime() {
        use fs2::FileExt;
        let temp = tempfile::tempdir().unwrap();
        let paths = AppPaths::new(temp.path(), &temp.path().join("hex")).unwrap();
        fixture(&paths, "print(json.dumps(result))");
        let owner = verified_owner_at(paths.clone()).unwrap();
        assert!(owner.is_signed());
        assert_eq!(owner.executable(), paths.executable);
        let another = fs::File::open(&paths.lock).unwrap();
        assert!(another.try_lock_exclusive().is_err());
        drop(owner);
        another.try_lock_exclusive().unwrap();
    }

    #[test]
    fn bad_provenance_policy_or_journal_never_executes_helper() {
        for defect in ["hash", "policy", "journal", "state-symlink"] {
            let temp = tempfile::tempdir().unwrap();
            let paths = AppPaths::new(temp.path(), &temp.path().join("hex")).unwrap();
            fixture(&paths, "print(json.dumps(result))");
            match defect {
                "hash" => {
                    fs::write(paths.root.join("libexec/macos-signing.py"), b"changed").unwrap()
                }
                "policy" => fs::remove_file(&paths.policy).unwrap(),
                "journal" => fs::write(&paths.journal, b"incomplete").unwrap(),
                _ => {
                    fs::remove_file(&paths.state).unwrap();
                    symlink("missing", &paths.state).unwrap();
                }
            }
            assert!(verified_owner_at(paths.clone()).is_err(), "{defect}");
            assert!(!paths.root.join("called").exists(), "{defect}");
        }
    }

    #[test]
    fn actual_helper_wrong_duplicate_or_failed_result_cannot_admit() {
        for behavior in [
            "result['product']='boi';print(json.dumps(result))",
            "print('{\"schema_version\":1,\"schema_version\":1}')",
            "sys.exit(7)",
            "print(json.dumps(result));print('failure',file=sys.stderr)",
        ] {
            let temp = tempfile::tempdir().unwrap();
            let paths = AppPaths::new(temp.path(), &temp.path().join("hex")).unwrap();
            fixture(&paths, behavior);
            assert!(verified_owner_at(paths).is_err(), "{behavior}");
        }
    }

    #[test]
    fn bounded_runner_rejects_overflow_and_a_sleeping_child() {
        let temp = tempfile::tempdir().unwrap();
        for code in [
            "import os; os.write(1,b'x'*100000)",
            "import os; os.write(2,b'x'*100000)",
            "import time; time.sleep(10)",
        ] {
            let args = ["-I".into(), "-B".into(), "-c".into(), code.into()];
            let started = std::time::Instant::now();
            assert!(run_python(&args, temp.path(), None, Duration::from_millis(200)).is_err());
            assert!(started.elapsed() < Duration::from_secs(3));
        }
    }

    #[test]
    fn managed_preflight_never_accepts_raw_or_wrong_source_result() {
        let temp = tempfile::tempdir().unwrap();
        let paths = AppPaths::new(temp.path(), &temp.path().join("hex")).unwrap();
        fs::create_dir_all(paths.policy.parent().unwrap()).unwrap();
        fs::write(&paths.policy, b"fixture public policy").unwrap();
        let source = temp.path().join("source");
        fs::create_dir_all(source.join("system/scripts")).unwrap();
        let helper = source.join("system/scripts/macos-app-install.py");
        fs::write(source.join("system/scripts/macos-signing.py"), b"# fixture").unwrap();
        for response in [
            serde_json::json!({"schema_version":1,"product":"hex","mode":"legacy-raw","managed":true,"policy_available":true}),
            serde_json::json!({"schema_version":1,"product":"boi","mode":"signed-current","source_revision":"a".repeat(40),"managed":true,"policy_available":true}),
            serde_json::json!({"schema_version":1,"product":"hex","mode":"signed-current","source_revision":"release:v1","managed":true,"policy_available":true}),
            serde_json::json!({"schema_version":1,"product":"hex","mode":"signed-current","source_revision":"a".repeat(40)}),
            serde_json::json!({"schema_version":1,"product":"hex","mode":"empty","managed":"true","policy_available":true}),
            serde_json::json!({"schema_version":1,"product":"hex","mode":"empty","managed":true,"policy_available":false}),
        ] {
            fs::write(&helper, format!("print({:?})\n", response.to_string())).unwrap();
            assert!(prepare_upgrade_at(&paths, &source).is_err(), "{response}");
        }
        for (mode, expected) in [
            ("empty", UpgradeMode::Migrate),
            ("configured-legacy", UpgradeMode::Migrate),
            ("signed-current", UpgradeMode::Signed("a".repeat(40))),
        ] {
            let response = serde_json::json!({"schema_version":1,"product":"hex","mode":mode,"source_revision":"a".repeat(40),"managed":true,"policy_available":true});
            fs::write(&helper, format!("print({:?})\n", response.to_string())).unwrap();
            assert_eq!(prepare_upgrade_at(&paths, &source).unwrap(), expected);
        }
        fs::remove_file(&helper).unwrap();
        assert!(prepare_upgrade_at(&paths, &source).is_err());
        fs::remove_file(&paths.policy).unwrap();
        assert_eq!(
            prepare_upgrade_at(&paths, &source).unwrap(),
            UpgradeMode::Legacy
        );
    }
}
