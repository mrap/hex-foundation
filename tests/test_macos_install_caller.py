#!/usr/bin/env python3
"""Exercise install.sh's common macOS app-installer boundary with a fake CLI."""
from __future__ import annotations

import json
import os
import stat
import subprocess
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "install.sh"


def _function_source() -> str:
    text = INSTALLER.read_text()
    start = text.index("_macos_app_enabled() {")
    end = text.index('echo "hex v${VERSION} installer"', start)
    return text[start:end]


def _prebuilt_source() -> str:
    text = INSTALLER.read_text()
    start = text.index("_harness_download_prebuilt() {")
    end = text.index("_harness_warn_missing() {", start)
    return text[start:end]


def _named_function_source(name: str, next_name: str) -> str:
    text = INSTALLER.read_text()
    start = text.index(f"{name}() {{")
    end = text.index(f"\n{next_name}() {{", start)
    return text[start:end]


def _block_source(start_marker: str, end_marker: str) -> str:
    text = INSTALLER.read_text()
    return text[text.index(start_marker) : text.index(end_marker, text.index(start_marker))]


def _git_tag_repo(temp: Path) -> tuple[Path, str]:
    repo = temp / "boi-repo"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.com"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "test"], check=True)
    (repo / "README").write_text("fixture\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "README"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "fixture"], check=True)
    subprocess.run(["git", "-C", str(repo), "tag", "v1.0.0"], check=True)
    sha = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "v1.0.0"], text=True).strip()
    return repo, sha


def test_common_app_installer_boundary() -> None:
    with tempfile.TemporaryDirectory(prefix="hex-install-caller-") as raw:
        temp = Path(raw)
        fake_bin = temp / "bin"
        fake_bin.mkdir()
        uname = fake_bin / "uname"
        uname.write_text("#!/bin/sh\nprintf '%s\\n' Darwin\n")
        uname.chmod(uname.stat().st_mode | stat.S_IXUSR)

        script_root = temp / "source"
        fake_installer = script_root / "system" / "scripts" / "macos-app-install.py"
        fake_installer.parent.mkdir(parents=True)
        fake_installer.write_text(
            """#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
with open(os.environ['FAKE_LOG'], 'a', encoding='utf-8') as stream:
    stream.write(json.dumps(args) + '\\n')
if os.environ.get('FAKE_FAIL') == args[0]:
    raise SystemExit(17)
result = {'schema_version': 1, 'product': args[1], 'mode': os.environ['FAKE_MODE'], 'managed': os.environ['FAKE_MODE'] != 'legacy-raw', 'policy_available': os.environ.get('FAKE_POLICY') == 'true'}
if args[0] == 'preflight' and os.environ['FAKE_MODE'] == 'signed-current': result['source_revision'] = 'a' * 40
print(json.dumps(result))
"""
        )
        fake_installer.chmod(fake_installer.stat().st_mode | stat.S_IXUSR)
        log = temp / "calls.jsonl"
        shell = temp / "boundary.sh"
        shell.write_text(
            _function_source()
            + """
set -euo pipefail
MACOS_APP_INSTALLER="$TEST_HELPER"
MACOS_APP_MODE=legacy-raw
MACOS_APP_MANAGED=false
MACOS_APP_POLICY_AVAILABLE=false
_macos_app_prepare boi "$TEST_ROOT"
test "$MACOS_APP_MODE" = signed-current
test "$MACOS_APP_MANAGED" = true
_macos_app_verify_current boi "$TEST_ROOT"
_macos_app_install boi "$TEST_ROOT" "$TEST_ROOT/input" 3.9.0 abc123 helper456
FAKE_MODE=empty FAKE_POLICY=true _macos_app_prepare hex "$TEST_ROOT/hex"
test "$MACOS_APP_MODE" = empty
test "$MACOS_APP_MANAGED" = true
FAKE_MODE=legacy-raw _macos_app_prepare hex "$TEST_ROOT/hex"
test "$MACOS_APP_MODE" = legacy-raw
test "$MACOS_APP_MANAGED" = false
if FAKE_MODE=legacy-raw _macos_app_recheck hex "$TEST_ROOT/hex" true; then
    exit 19
fi
test "$MACOS_APP_MANAGED" = true
"""
        )
        env = os.environ | {
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
            "SCRIPT_DIR": str(script_root),
            "TEST_HELPER": str(fake_installer),
            "FAKE_MODE": "signed-current",
            "FAKE_LOG": str(log),
            "TEST_ROOT": str(temp / "boi"),
        }
        result = subprocess.run(["bash", str(shell)], env=env, capture_output=True, text=True)
        assert result.returncode == 0, result.stderr

        calls = [json.loads(line) for line in log.read_text().splitlines()]
        assert [call[0] for call in calls] == ["mode", "preflight", "verify-current", "install", "mode", "preflight", "mode", "preflight", "mode", "preflight"]
        assert calls[3][1:4] == ["boi", "--root", str(temp / "boi")]
        assert calls[3][-8:] == ["--source", str(temp / "boi" / "input"), "--version", "3.9.0", "--source-revision", "abc123", "--helper-source-revision", "helper456"]

        failed = subprocess.run(
            ["bash", str(shell)],
            env=env | {"FAKE_FAIL": "install"},
            capture_output=True,
            text=True,
        )
        assert failed.returncode != 0
        assert "raw fallback" not in failed.stderr

        missing = subprocess.run(
            ["bash", str(shell)],
            env=env | {"TEST_HELPER": str(temp / "missing-app-installer.py")},
            capture_output=True,
            text=True,
        )
        assert missing.returncode != 0
        assert "helper is missing" in missing.stderr


def test_managed_prebuilt_fails_closed() -> None:
    with tempfile.TemporaryDirectory(prefix="hex-prebuilt-caller-") as raw:
        temp = Path(raw)
        fake_bin = temp / "bin"
        fake_bin.mkdir()
        (fake_bin / "uname").write_text("#!/bin/sh\nprintf '%s\\n' Darwin\n")
        (fake_bin / "uname").chmod(0o755)
        (fake_bin / "curl").write_text(
            "#!/bin/sh\nprintf '%s\\n' curl-called > \"$CURL_MARKER\"\nexit 0\n"
        )
        (fake_bin / "curl").chmod(0o755)
        helper = temp / "app-install.py"
        helper.write_text(
            "import json; print(json.dumps({'schema_version': 1, 'product': 'hex', 'mode': 'signed-current', 'managed': True, 'policy_available': True, 'source_revision': 'a' * 40}))\n"
        )
        root = temp / "hex"
        root.mkdir()
        shell = temp / "managed-prebuilt.sh"
        shell.write_text(
            _function_source()
            + _prebuilt_source()
            + """
set -euo pipefail
MACOS_APP_INSTALLER="$TEST_HELPER"
MACOS_APP_MODE=legacy-raw
MACOS_APP_MANAGED=false
MACOS_APP_POLICY_AVAILABLE=false
TARGET_DIR="$TEST_ROOT"
HARNESS_VERSION=v-test
_harness_download_prebuilt
"""
        )
        marker = temp / "curl.marker"
        result = subprocess.run(
            ["bash", str(shell)],
            env=os.environ
            | {
                "PATH": f"{fake_bin}:{os.environ['PATH']}",
                "TEST_HELPER": str(helper),
                "TEST_ROOT": str(root),
                "CURL_MARKER": str(marker),
            },
            capture_output=True,
            text=True,
        )
        assert result.returncode != 0
        assert "managed macOS Hex prebuilt installation is unsupported" in result.stderr
        assert not marker.exists()


def test_boi_fast_path_uses_verified_state_without_raw_version_call() -> None:
    with tempfile.TemporaryDirectory(prefix="hex-boi-fast-path-") as raw:
        temp = Path(raw)
        repo, sha = _git_tag_repo(temp)
        home = temp / "home"
        bin_dir = home / ".boi" / "bin"
        bin_dir.mkdir(parents=True)
        fake_boi = temp / "fake-boi"
        fake_boi.write_text("#!/bin/sh\nprintf '%s\\n' version-called > \"$VERSION_MARKER\"\n")
        fake_boi.chmod(0o755)
        (bin_dir / "boi").symlink_to(fake_boi)
        shell = temp / "fast.sh"
        shell.write_text(
            _function_source()
            + _block_source("_verify_pinned_checkout() {", "_macos_app_prepare() {")
            + _block_source("_resolve_git_tag() {", "# BOI —")
            + _block_source("install_or_upgrade_boi() {", "\ninstall_or_upgrade_boi\n")
            + """
set -euo pipefail
write_boi_wrapper() { :; }
_macos_app_prepare() { MACOS_APP_MODE=signed-current; MACOS_APP_MANAGED=true; }
_macos_app_verify_current() { printf '{"schema_version":1,"product":"boi","mode":"signed-current","source_revision":"%s","version":"1.0.0"}\n' "$TEST_SHA"; }
HOME="$TEST_HOME"
BOI_REPO="$TEST_REPO"
BOI_VERSION=v1.0.0
MACOS_APP_MANAGED=true
MACOS_APP_MODE=signed-current
install_or_upgrade_boi
test ! -e "$VERSION_MARKER"
"""
        )
        result = subprocess.run(
            ["bash", str(shell)],
            env=os.environ
            | {
                "TEST_HOME": str(home),
                "TEST_REPO": str(repo),
                "TEST_SHA": sha,
                "VERSION_MARKER": str(temp / "version-called"),
            },
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr


def test_pinned_checkout_rejects_wrong_and_dirty_source() -> None:
    with tempfile.TemporaryDirectory(prefix="hex-boi-checkout-") as raw:
        temp = Path(raw)
        repo, sha = _git_tag_repo(temp)
        shell = temp / "check.sh"
        shell.write_text(
            _block_source("_verify_pinned_checkout() {", "_macos_app_prepare() {")
            + f"""
set -euo pipefail
_verify_pinned_checkout "$TEST_REPO" v1.0.0 "$TEST_SHA"
if _verify_pinned_checkout "$TEST_REPO" v1.0.0 "{'0' * 40}"; then exit 21; fi
printf dirty >> "$TEST_REPO/README"
if _verify_pinned_checkout "$TEST_REPO" v1.0.0 "$TEST_SHA"; then exit 22; fi
"""
        )
        result = subprocess.run(
            ["bash", str(shell)],
            env=os.environ | {"TEST_REPO": str(repo), "TEST_SHA": sha},
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr


def test_boi_build_failure_and_target_artifact_are_real_paths() -> None:
    with tempfile.TemporaryDirectory(prefix="hex-boi-build-") as raw:
        temp = Path(raw)
        repo, _ = _git_tag_repo(temp)
        fake_bin = temp / "bin"
        fake_bin.mkdir()
        cargo = fake_bin / "cargo"
        cargo.write_text(
            """#!/bin/sh
if [ "${CARGO_MODE:-fail}" = fail ]; then exit 23; fi
mkdir -p "$CARGO_TARGET_DIR/release" "$HOME/.boi/src/boi/target/release"
printf new > "$CARGO_TARGET_DIR/release/boi"
printf stale > "$HOME/.boi/src/boi/target/release/boi"
chmod +x "$CARGO_TARGET_DIR/release/boi"
"""
        )
        cargo.chmod(0o755)
        shell = temp / "build.sh"
        shell.write_text(
            _function_source()
            + _block_source("_verify_pinned_checkout() {", "_macos_app_prepare() {")
            + _block_source("_resolve_git_tag() {", "# BOI —")
            + _block_source("install_or_upgrade_boi() {", "\ninstall_or_upgrade_boi\n")
            + """
set -euo pipefail
write_boi_wrapper() { :; }
_macos_app_prepare() { MACOS_APP_MODE=legacy-raw; MACOS_APP_MANAGED=false; }
_macos_app_recheck() { :; }
HOME="$TEST_HOME"
BOI_REPO="$TEST_REPO"
BOI_VERSION=v1.0.0
install_or_upgrade_boi
"""
        )
        home = temp / "home"
        old = home / ".boi" / "bin" / "boi"
        old.parent.mkdir(parents=True)
        old.write_text("old")
        old.chmod(0o755)
        base_env = os.environ | {
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
            "TEST_HOME": str(home),
            "TEST_REPO": str(repo),
        }
        failed = subprocess.run(["bash", str(shell)], env=base_env, capture_output=True, text=True)
        assert failed.returncode != 0
        assert old.read_text(encoding="utf-8") == "old"
        succeeded = subprocess.run(
            ["bash", str(shell)],
            env=base_env | {"CARGO_MODE": "success", "CARGO_TARGET_DIR": str(temp / "explicit-target")},
            capture_output=True,
            text=True,
        )
        assert succeeded.returncode == 0, succeeded.stderr
        assert old.read_text(encoding="utf-8") == "new"


def test_hex_missing_artifact_preserves_alias() -> None:
    with tempfile.TemporaryDirectory(prefix="hex-missing-artifact-") as raw:
        temp = Path(raw)
        source = temp / "source"
        (source / "system" / "harness").mkdir(parents=True)
        target = temp / "target"
        (target / ".hex" / "bin").mkdir(parents=True)
        alias = target / ".hex" / "bin" / "hex-agent"
        alias.write_text("old-alias", encoding="utf-8")
        fake_bin = temp / "bin"
        fake_bin.mkdir()
        (fake_bin / "cargo").write_text("#!/bin/sh\nexit 0\n")
        (fake_bin / "cargo").chmod(0o755)
        shell = temp / "hex.sh"
        shell.write_text(
            _function_source()
            + _block_source("_harness_build_from_source() {", "# Build + deploy the code-intel")
            + """
set -euo pipefail
SCRIPT_DIR="$TEST_SOURCE"
TARGET_DIR="$TEST_TARGET"
_macos_app_prepare() { MACOS_APP_MODE=legacy-raw; MACOS_APP_MANAGED=false; }
_macos_app_recheck() { :; }
PATH="$TEST_BIN:$PATH"
_harness_build_from_source
"""
        )
        result = subprocess.run(
            ["bash", str(shell)],
            env=os.environ | {"TEST_SOURCE": str(source), "TEST_TARGET": str(target), "TEST_BIN": str(fake_bin)},
            capture_output=True,
            text=True,
        )
        assert result.returncode != 0
        assert alias.read_text(encoding="utf-8") == "old-alias"


def test_codeintel_managed_build_uses_exact_artifacts_and_reconcile() -> None:
    with tempfile.TemporaryDirectory(prefix="hex-codeintel-caller-") as raw:
        temp = Path(raw)
        source = temp / "source"
        (source / "system" / "code-intel").mkdir(parents=True)
        (source / "system" / "code-intel" / "Cargo.toml").write_text('version = "2.3.4"\n', encoding="utf-8")
        subprocess.run(["git", "init", "-q", str(source)], check=True)
        subprocess.run(["git", "-C", str(source), "config", "user.email", "test@example.com"], check=True)
        subprocess.run(["git", "-C", str(source), "config", "user.name", "test"], check=True)
        subprocess.run(["git", "-C", str(source), "add", "."], check=True)
        subprocess.run(["git", "-C", str(source), "commit", "-qm", "fixture"], check=True)
        source_revision = subprocess.check_output(["git", "-C", str(source), "rev-parse", "HEAD"], text=True).strip()
        target = temp / "target"
        fake_bin = temp / "bin"
        fake_bin.mkdir()
        (fake_bin / "cargo").write_text(
            "#!/bin/sh\n"
            "if [ \"${CARGO_MODE:-ok}\" = fail ]; then exit 23; fi\n"
            "if [ \"${SOURCE_CHANGE:-0}\" = 1 ]; then printf changed >> \"$TEST_SOURCE/system/code-intel/Cargo.toml\"; fi\n"
            "mkdir -p \"$CARGO_TARGET_DIR/release\"\n"
            "printf cq > \"$CARGO_TARGET_DIR/release/cq\"\n"
            "if [ \"${MISSING_SCIPD:-0}\" != 1 ]; then printf scipd > \"$CARGO_TARGET_DIR/release/scipd\"; fi\n"
            "chmod +x \"$CARGO_TARGET_DIR/release/cq\"; [ \"${MISSING_SCIPD:-0}\" = 1 ] || chmod +x \"$CARGO_TARGET_DIR/release/scipd\"\n",
            encoding="utf-8",
        )
        (fake_bin / "cargo").chmod(0o755)
        helper = temp / "macos-app-install.py"
        log = temp / "calls.jsonl"
        helper.write_text(
            "import json, os, sys\n"
            "args=sys.argv[1:]\n"
            "with open(os.environ['CALL_LOG'], 'a') as stream: stream.write(json.dumps(args)+'\\n')\n"
            "if args[0] == 'install' and os.environ.get('INSTALL_FAIL') == args[1]: raise SystemExit(17)\n"
            "if args[0] == 'compatibility-alias': print(json.dumps({'schema_version':1,'product':args[1],'source_revision':os.environ.get('TEST_REVISION','a'*40),'generation':'g','alias_path':os.environ['HEX_WORKSPACE']+'/.hex/bin/'+('scipd' if args[1]=='code-intel-daemon' else 'cq'),'target_path':args[3]+'/bin/'+('scipd' if args[1]=='code-intel-daemon' else 'cq'),'action':'current','changed':False,'published':False}))\n"
            "elif args[0] == 'service-reconcile' and os.environ.get('RECONCILE_BAD') == '1': print('{}'); raise SystemExit(0)\n"
            "elif args[0] == 'service-reconcile': pending=os.environ.get('SERVICE_PENDING') == '1'; dry='--dry-run' in args; action='would-update-stopped' if dry else ('recovered' if pending else 'stopped'); changed=action in {'recovered','updated-stopped','restarted'}; print(json.dumps({'schema_version':1,'product':args[1],'mode':'signed-current','service_action':action,'service_needs_change':dry or changed,'published':changed,'service_recovery_pending':pending,'plist_path':os.environ['HOME']+'/Library/LaunchAgents/com.hex.scipd.plist','executable_path':args[3]+'/SCIPD.app/Contents/MacOS/scipd'}))\n"
            "else: print(json.dumps({'schema_version':1,'product':args[1],'mode':'signed-current'}))\n",
            encoding="utf-8",
        )
        shell = temp / "codeintel.sh"
        shell.write_text(
            _function_source()
            + _block_source("_code_intel_build_and_deploy() {", "_harness_download_prebuilt() {")
            + """
set -euo pipefail
SCRIPT_DIR="$TEST_SOURCE"
TARGET_DIR="$TEST_TARGET"
MACOS_APP_INSTALLER="$TEST_HELPER"
_macos_app_enabled() { return 0; }
_code_intel_build_and_deploy "$TEST_TARGET_DIR" true true "$TEST_REVISION" signed-current signed-current "${CLI_REVISION:-}" "${DAEMON_REVISION:-}"
test "$(find "$TEST_TARGET_DIR" -path '*/release/cq' -type f | wc -l | tr -d ' ')" -ge 1
test "$(find "$TEST_TARGET_DIR" -path '*/release/scipd' -type f | wc -l | tr -d ' ')" -ge 1
test ! -e "$TEST_HEX/.hex/bin/cq"
test ! -e "$TEST_HEX/.hex/bin/scipd"
""",
            encoding="utf-8",
        )
        env = os.environ | {
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
            "TEST_SOURCE": str(source),
            "TEST_TARGET": str(temp / "hex"),
            "TEST_TARGET_DIR": str(target),
            "TEST_HELPER": str(helper),
            "TEST_HEX": str(temp / "hex"),
            "HEX_WORKSPACE": str(temp / "hex"),
            "CALL_LOG": str(log),
            "TEST_REVISION": source_revision,
        }
        result = subprocess.run(["bash", str(shell)], env=env, capture_output=True, text=True)
        assert result.returncode == 0, result.stderr
        calls = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
        assert [call[0:2] for call in calls] == [
            ["install", "code-intel-cli"],
            ["compatibility-alias", "code-intel-cli"],
            ["install", "code-intel-daemon"],
            ["compatibility-alias", "code-intel-daemon"],
            ["service-reconcile", "code-intel-daemon"],
        ]
        assert calls[0][calls[0].index("--version") + 1] == "2.3.4"
        assert calls[0][calls[0].index("--source") + 1].startswith(str(target / "code-intel-build."))
        assert calls[0][calls[0].index("--source") + 1].endswith("/release/cq")

        log.write_text("", encoding="utf-8")
        pending_retry = subprocess.run(
            ["bash", str(shell)],
            env=env | {"SERVICE_PENDING": "1", "CLI_REVISION": "old" * 10, "DAEMON_REVISION": "old" * 10},
            capture_output=True,
            text=True,
        )
        assert pending_retry.returncode == 0, pending_retry.stderr
        pending_calls = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
        service_calls = [call for call in pending_calls if call[0] == "service-reconcile"]
        assert "--dry-run" in service_calls[0]
        assert "--dry-run" not in service_calls[1]
        assert pending_calls.index(service_calls[0]) < pending_calls.index(next(call for call in pending_calls if call[0] == "install"))

        failed_build = subprocess.run(["bash", str(shell)], env=env | {"CARGO_MODE": "fail"}, capture_output=True, text=True)
        assert failed_build.returncode != 0
        (target / "release").mkdir(parents=True, exist_ok=True)
        (target / "release" / "scipd").write_text("stale", encoding="utf-8")
        missing_artifact = subprocess.run(["bash", str(shell)], env=env | {"MISSING_SCIPD": "1"}, capture_output=True, text=True)
        assert missing_artifact.returncode != 0
        assert "missing exact code-intel artifact" in missing_artifact.stderr
        failed_install = subprocess.run(["bash", str(shell)], env=env | {"INSTALL_FAIL": "code-intel-cli"}, capture_output=True, text=True)
        assert failed_install.returncode != 0
        failed_reconcile = subprocess.run(["bash", str(shell)], env=env | {"RECONCILE_BAD": "1"}, capture_output=True, text=True)
        assert failed_reconcile.returncode != 0
        changed_source = subprocess.run(["bash", str(shell)], env=env | {"SOURCE_CHANGE": "1"}, capture_output=True, text=True)
        assert changed_source.returncode != 0
        assert "changed during code-intel build" in changed_source.stderr


def test_managed_companion_failure_is_not_raw_fallback_after_hex_success() -> None:
    with tempfile.TemporaryDirectory(prefix="hex-managed-companion-") as raw:
        temp = Path(raw)
        source = temp / "source"
        (source / "system" / "harness").mkdir(parents=True)
        (source / "system" / "code-intel").mkdir(parents=True)
        (source / "system" / "code-intel" / "Cargo.toml").write_text('version = "2.3.4"\n', encoding="utf-8")
        subprocess.run(["git", "init", "-q", str(source)], check=True)
        subprocess.run(["git", "-C", str(source), "config", "user.email", "test@example.com"], check=True)
        subprocess.run(["git", "-C", str(source), "config", "user.name", "test"], check=True)
        subprocess.run(["git", "-C", str(source), "add", "."], check=True)
        subprocess.run(["git", "-C", str(source), "commit", "-qm", "fixture"], check=True)
        fake_bin = temp / "bin"
        fake_bin.mkdir()
        (fake_bin / "cargo").write_text(
            "#!/bin/sh\nmkdir -p \"$CARGO_TARGET_DIR/release\"\n"
            "printf hex > \"$CARGO_TARGET_DIR/release/hex\"\n"
            "printf cq > \"$CARGO_TARGET_DIR/release/cq\"\n"
            "printf scipd > \"$CARGO_TARGET_DIR/release/scipd\"\n"
            "chmod +x \"$CARGO_TARGET_DIR/release/hex\" \"$CARGO_TARGET_DIR/release/cq\" \"$CARGO_TARGET_DIR/release/scipd\"\n",
            encoding="utf-8",
        )
        (fake_bin / "cargo").chmod(0o755)
        log = temp / "installs.log"
        shell = temp / "managed.sh"
        shell.write_text(
            _function_source()
            + _block_source("_harness_build_from_source() {", "# Build + deploy the code-intel")
            + _block_source("_code_intel_build_and_deploy() {", "_harness_download_prebuilt() {")
            + """
set -euo pipefail
SCRIPT_DIR="$TEST_SOURCE"
TARGET_DIR="$TEST_TARGET"
VERSION=2.3.4
MACOS_APP_INSTALLER=fixture
_macos_app_prepare() { MACOS_APP_MODE=signed-current; MACOS_APP_MANAGED=true; MACOS_APP_POLICY_AVAILABLE=true; }
_macos_app_recheck() { :; }
_macos_app_install() { printf '%s\n' "$1" >> "$TEST_LOG"; [ "$1" != code-intel-cli ]; }
write_hex_sha_sidecar() { :; }
if _harness_build_from_source; then exit 21; fi
grep -qx hex "$TEST_LOG"
grep -qx code-intel-cli "$TEST_LOG"
test ! -e "$TEST_TARGET/.hex/bin/cq"
test ! -e "$TEST_TARGET/.hex/bin/scipd"
""",
            encoding="utf-8",
        )
        target = temp / "target"
        result = subprocess.run(
            ["bash", str(shell)],
            env=os.environ | {
                "PATH": f"{fake_bin}:{os.environ['PATH']}",
                "TEST_SOURCE": str(source),
                "TEST_TARGET": str(target),
                "TEST_LOG": str(log),
            },
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr
        assert "Hex is already installed" in result.stderr


if __name__ == "__main__":
    test_common_app_installer_boundary()
    test_managed_prebuilt_fails_closed()
    test_boi_fast_path_uses_verified_state_without_raw_version_call()
    test_pinned_checkout_rejects_wrong_and_dirty_source()
    test_boi_build_failure_and_target_artifact_are_real_paths()
    test_hex_missing_artifact_preserves_alias()
    test_codeintel_managed_build_uses_exact_artifacts_and_reconcile()
    test_managed_companion_failure_is_not_raw_fallback_after_hex_success()
    print("macOS install caller tests: ok")
