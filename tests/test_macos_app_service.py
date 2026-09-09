import fcntl
import importlib.util
import json
import os
import plistlib
import shutil
import sys
import tempfile
import unittest
from pathlib import Path


SOURCE = Path(__file__).parents[1] / "system/scripts/macos-app-install.py"
spec = importlib.util.spec_from_file_location("macos_app_install_service", SOURCE)
INSTALL = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = INSTALL
spec.loader.exec_module(INSTALL)


class FakeSigner:
    def _result(self):
        return {"identifier": "com.mrap.hex.scipd", "version": "0.1.0", "team_id": "TEAM123456", "certificate_sha1": "A" * 40, "designated_requirements": {"arm64": "anchor apple generic"}, "mach_o_uuids": {"arm64": "11111111-1111-1111-1111-111111111111"}}

    def stage(self, source, product, policy, candidate, receipt):
        executable = candidate / "Contents/MacOS/scipd"
        executable.parent.mkdir(parents=True)
        shutil.copy2(source, executable)
        executable.chmod(0o755)
        (candidate / "Contents/Info.plist").write_bytes(b"fake")
        result = self._result()
        receipt.write_text(json.dumps(result), encoding="utf-8")
        return result

    def verify_installed(self, bundle, product, policy, expected=None):
        return self._result()


class FakeLaunchctl:
    def __init__(self, paths, loaded, program, fail_restart=False):
        self.paths = paths
        self.loaded = loaded
        self.program = program
        self.fail_restart = fail_restart
        self.calls = []
        self.lock_held = []

    def __call__(self, argv):
        self.calls.append(list(argv))
        fd = os.open(self.paths.lock, os.O_RDWR)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                self.lock_held.append(True)
            else:
                self.lock_held.append(False)
        finally:
            os.close(fd)
        if argv[0] == "print":
            if not self.loaded:
                return 1, "", "Could not find service"
            return 0, f"program = {self.program}\n", ""
        if argv[0] == "kickstart":
            if self.fail_restart:
                return 1, "", "kickstart failed"
            self.loaded = True
            self.program = str(self.paths.cli.absolute())
            return 0, "", ""
        raise AssertionError(argv)


@unittest.skipUnless(sys.platform == "darwin", "requires macOS renameatx_np")
class MacAppServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.root = self.base / ".codeintel"
        self.policy = self.base / "policy.json"
        self.policy.write_text(json.dumps({"schema_version": 1, "certificate_sha1": "A" * 40, "team_id": "TEAM123456"}), encoding="utf-8")
        self.source = self.base / "scipd"
        self.source.write_bytes(b"scipd")
        self.helper_sources = {}
        self.helper_provenance = {}
        for name, data in (("macos-signing.py", b"signer"), ("macos-app-install.py", b"installer")):
            path = self.base / name
            path.write_bytes(data)
            self.helper_sources[name] = path
            self.helper_provenance[name] = {"sha256": INSTALL._sha256(path), "source_revision": "f" * 40}
        self.signer = FakeSigner()
        self.paths = INSTALL.product_paths("code-intel-daemon", self.root)
        self.plist = self.base / "Library/LaunchAgents/com.hex.scipd.plist"
        self.plist.parent.mkdir(parents=True)
        self.addCleanup(self.temp.cleanup)

    def seed(self):
        return INSTALL.install("code-intel-daemon", self.root, self.source, self.signer, policy_path=self.policy, helper_provenance=self.helper_provenance, helper_sources=self.helper_sources, source_revision="e" * 40, version="0.1.0")

    def write_plist(self, arguments=None, associated=None, **extra):
        value = {"Label": INSTALL.SCIPD_LAUNCHD_LABEL, "ProgramArguments": arguments or [str(self.paths.executable.absolute())]}
        if associated is not None:
            value["AssociatedBundleIdentifiers"] = associated
        value.update(extra)
        self.plist.write_bytes(plistlib.dumps(value, sort_keys=False))

    def test_loaded_service_updates_and_restarts_under_product_lock(self):
        self.seed()
        self.write_plist([str(self.paths.executable.absolute())], ["wrong.id"], KeepAlive=True, Nice=10, EnvironmentVariables={"PATH": "/usr/bin"})
        launchctl = FakeLaunchctl(self.paths, True, str(self.paths.executable.absolute()))
        result = INSTALL.service_reconcile("code-intel-daemon", self.root, self.signer, policy_path=self.policy, launchctl=launchctl, plist_path=self.plist)
        self.assertEqual(result["service_action"], "restarted")
        self.assertTrue(all(launchctl.lock_held))
        self.assertIn(["kickstart", "-k", f"gui/{os.getuid()}/{INSTALL.SCIPD_LAUNCHD_LABEL}"], launchctl.calls)
        value = plistlib.loads(self.plist.read_bytes())
        self.assertEqual(value["ProgramArguments"], [str(self.paths.cli.absolute())])
        self.assertEqual(value["AssociatedBundleIdentifiers"], ["com.mrap.hex.scipd"])
        self.assertEqual(value["EnvironmentVariables"], {"PATH": "/usr/bin"})

    def test_stopped_service_updates_without_start(self):
        self.seed()
        self.write_plist([str(self.paths.executable.absolute())])
        launchctl = FakeLaunchctl(self.paths, False, "")
        result = INSTALL.service_reconcile("code-intel-daemon", self.root, self.signer, policy_path=self.policy, launchctl=launchctl, plist_path=self.plist)
        self.assertEqual(result["service_action"], "updated-stopped")
        self.assertEqual([call[0] for call in launchctl.calls], ["print"])

    def test_absent_service_does_not_create_or_start(self):
        self.seed()
        launchctl = FakeLaunchctl(self.paths, False, "")
        result = INSTALL.service_reconcile("code-intel-daemon", self.root, self.signer, policy_path=self.policy, launchctl=launchctl, plist_path=self.plist)
        self.assertEqual(result["service_action"], "absent")
        self.assertFalse(self.plist.exists())
        self.assertEqual([call[0] for call in launchctl.calls], ["print"])

    def test_dry_run_does_not_change_plist_or_restart(self):
        self.seed()
        self.write_plist([str(self.paths.executable.absolute())], ["wrong.id"])
        before = self.plist.read_bytes()
        launchctl = FakeLaunchctl(self.paths, True, str(self.paths.executable.absolute()))
        result = INSTALL.service_reconcile("code-intel-daemon", self.root, self.signer, policy_path=self.policy, dry_run=True, launchctl=launchctl, plist_path=self.plist)
        self.assertTrue(result["service_needs_change"])
        self.assertEqual(result["service_action"], "would-restart")
        self.assertEqual(before, self.plist.read_bytes())
        self.assertEqual([call[0] for call in launchctl.calls], ["print"])

    def test_invalid_arguments_fail_before_mutation(self):
        self.seed()
        self.write_plist(["/tmp/unknown-scipd"])
        before = self.plist.read_bytes()
        launchctl = FakeLaunchctl(self.paths, False, "")
        with self.assertRaisesRegex(INSTALL.InstallError, "ProgramArguments"):
            INSTALL.service_reconcile("code-intel-daemon", self.root, self.signer, policy_path=self.policy, launchctl=launchctl, plist_path=self.plist)
        self.assertEqual(before, self.plist.read_bytes())
        self.assertEqual([call[0] for call in launchctl.calls], ["print"])

    def test_restart_failure_reports_published(self):
        self.seed()
        self.write_plist([str(self.paths.executable.absolute())])
        launchctl = FakeLaunchctl(self.paths, True, str(self.paths.executable.absolute()), fail_restart=True)
        with self.assertRaisesRegex(INSTALL.InstallError, "restart failed") as context:
            INSTALL.service_reconcile("code-intel-daemon", self.root, self.signer, policy_path=self.policy, launchctl=launchctl, plist_path=self.plist)
        self.assertTrue(context.exception.published)

    def test_cli_product_is_rejected(self):
        with self.assertRaisesRegex(INSTALL.InstallError, "only code-intel-daemon"):
            INSTALL.service_reconcile("code-intel-cli", self.root, self.signer, policy_path=self.policy, launchctl=FakeLaunchctl(self.paths, False, ""), plist_path=self.plist)


if __name__ == "__main__":
    unittest.main()
