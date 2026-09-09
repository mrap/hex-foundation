import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


SOURCE = Path(__file__).parents[1] / "system/scripts/macos-app-install.py"
spec = importlib.util.spec_from_file_location("macos_app_install", SOURCE)
INSTALL = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = INSTALL
spec.loader.exec_module(INSTALL)


class FakeSigner:
    def __init__(self):
        self.stage_calls = []
        self.verify_calls = []
        self.fail_stage = False

    def result(self, product):
        return {
            "identifier": INSTALL.PRODUCTS[product].bundle_identifier,
            "version": "1.0.0",
            "team_id": "TEAM123456",
            "certificate_sha1": "A" * 40,
            "designated_requirements": {"arm64": "anchor apple generic"},
            "mach_o_uuids": {"arm64": "11111111-1111-1111-1111-111111111111"},
        }

    def stage(self, source, product, policy, candidate, receipt):
        self.stage_calls.append((source, product, policy, candidate, receipt))
        if self.fail_stage:
            raise INSTALL.InstallError("injected stage failure")
        item = INSTALL.PRODUCTS[product]
        executable = candidate / "Contents/MacOS" / item.executable
        executable.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, executable)
        executable.chmod(0o755)
        (candidate / "Contents/Info.plist").write_bytes(b"public fake plist")
        result = self.result(product)
        receipt.write_text(json.dumps(result), encoding="utf-8")
        return result

    def verify_installed(self, bundle, product, policy, expected=None):
        self.verify_calls.append((bundle, product, policy, expected))
        return self.result(product)


@unittest.skipUnless(sys.platform == "darwin", "requires macOS renameatx_np")
class MacAppInstallTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / ".boi"
        self.root.mkdir()
        (self.root / "libexec").mkdir()
        self.helpers = {}
        for name in ("macos-signing.py", "macos-app-install.py"):
            path = self.root / "libexec" / name
            path.write_text(name, encoding="utf-8")
            self.helpers[name] = {"sha256": INSTALL._sha256(path), "source_revision": "f" * 40}
        self.policy = Path(self.temp.name) / "policy.json"
        self.policy.write_text(json.dumps({"schema_version": 1, "certificate_sha1": "A" * 40, "team_id": "TEAM123456"}), encoding="utf-8")
        self.source = Path(self.temp.name) / "boi-source"
        self.source.write_bytes(b"candidate bytes")
        self.signer = FakeSigner()
        self.addCleanup(self.temp.cleanup)

    def install(self):
        sources = {name: self.root / "libexec" / name for name in self.helpers}
        return INSTALL.install("boi", self.root, self.source, self.signer, policy_path=self.policy, helper_provenance=self.helpers, helper_sources=sources, source_revision="e" * 40)

    def test_empty_publication_uses_fixed_paths_and_state(self):
        result = self.install()
        paths = INSTALL.product_paths("boi", self.root)
        self.assertTrue(paths.app.is_dir())
        self.assertTrue(paths.executable.is_file())
        self.assertTrue(paths.cli.is_symlink())
        self.assertEqual(paths.cli.resolve(), paths.executable.resolve())
        self.assertEqual(result["bundle_identifier"], "com.mrap.boi")
        self.assertEqual(json.loads(paths.state.read_text())["mode"], "signed-current")
        self.assertFalse(paths.journal.exists())

    def test_legacy_raw_is_retained_in_rollback(self):
        paths = INSTALL.product_paths("boi", self.root)
        paths.cli.parent.mkdir()
        paths.cli.write_bytes(b"old raw")
        paths.cli.chmod(0o755)
        result = self.install()
        rollback = self.root / f".boi.app-install-rollback-{result['transaction_id']}" / "previous-cli"
        self.assertTrue(rollback.is_file())
        self.assertEqual(rollback.read_bytes(), b"old raw")
        self.assertTrue(paths.cli.is_symlink())

    def test_existing_app_uses_directory_swap(self):
        self.install()
        old_hash = INSTALL._tree_sha256(INSTALL.product_paths("boi", self.root).app)
        self.source.write_bytes(b"second candidate")
        result = self.install()
        paths = INSTALL.product_paths("boi", self.root)
        rollback = self.root / f".boi.app-install-rollback-{result['transaction_id']}" / "previous-app"
        self.assertEqual(INSTALL._tree_sha256(rollback), old_hash)
        self.assertNotEqual(INSTALL._tree_sha256(paths.app), old_hash)

    def test_compatibility_swap_failure_restores_previous_app(self):
        self.install()
        paths = INSTALL.product_paths("boi", self.root)
        old_app_hash = INSTALL._tree_sha256(paths.app)
        old_cli_target = os.readlink(paths.cli)
        original_swap = INSTALL._atomic_swap
        calls = {"count": 0}

        def fail_cli(parent_fd, source, destination):
            calls["count"] += 1
            if calls["count"] == 4:
                raise INSTALL.InstallError("injected compatibility swap failure")
            return original_swap(parent_fd, source, destination)

        INSTALL._atomic_swap = fail_cli
        try:
            with self.assertRaisesRegex(INSTALL.InstallError, "compatibility swap failure"):
                self.install()
        finally:
            INSTALL._atomic_swap = original_swap
        self.assertEqual(INSTALL._tree_sha256(paths.app), old_app_hash)
        self.assertEqual(os.readlink(paths.cli), old_cli_target)

    def test_rollback_refuses_actor_replacement(self):
        self.install()
        paths = INSTALL.product_paths("boi", self.root)
        original_swap = INSTALL._atomic_swap
        calls = {"count": 0}

        def actor_then_fail(parent_fd, source, destination):
            calls["count"] += 1
            if calls["count"] == 4:
                actor = paths.root / "BOI.app.actor"
                paths.app.rename(actor)
                paths.app.mkdir()
                (paths.app / "actor-owned").write_text("actor", encoding="utf-8")
                raise INSTALL.InstallError("injected compatibility swap failure")
            return original_swap(parent_fd, source, destination)

        INSTALL._atomic_swap = actor_then_fail
        try:
            with self.assertRaisesRegex(INSTALL.InstallError, "rollback failed"):
                self.install()
        finally:
            INSTALL._atomic_swap = original_swap
        self.assertTrue((paths.app / "actor-owned").is_file())

    def test_archive_move_failure_recovers_from_candidate(self):
        self.install()
        paths = INSTALL.product_paths("boi", self.root)
        old_hash = INSTALL._tree_sha256(paths.app)
        original_move = INSTALL._atomic_move
        failed = {"value": False}

        def fail_archive(source_fd, source, destination_fd, destination):
            if destination.name == "previous-app" and not failed["value"]:
                failed["value"] = True
                raise INSTALL.InstallError("injected archive move failure")
            return original_move(source_fd, source, destination_fd, destination)

        INSTALL._atomic_move = fail_archive
        try:
            with self.assertRaisesRegex(INSTALL.InstallError, "archive move failure"):
                self.install()
        finally:
            INSTALL._atomic_move = original_move
        self.assertEqual(INSTALL._tree_sha256(paths.app), old_hash)

    def test_stage_failure_leaves_journal_and_no_public_app(self):
        self.signer.fail_stage = True
        with self.assertRaises(INSTALL.InstallError):
            self.install()
        paths = INSTALL.product_paths("boi", self.root)
        self.assertTrue(paths.journal.is_file())
        self.assertFalse(paths.app.exists())

    def test_actor_change_during_staging_is_not_overwritten(self):
        paths = INSTALL.product_paths("boi", self.root)
        paths.cli.parent.mkdir()
        paths.cli.write_bytes(b"operator replacement")
        original_stage = self.signer.stage

        def stage_and_replace(*args):
            result = original_stage(*args)
            paths.cli.write_bytes(b"actor replacement")
            return result

        self.signer.stage = stage_and_replace
        with self.assertRaisesRegex(INSTALL.InstallError, "changed during staging"):
            INSTALL.install("boi", self.root, self.source, self.signer, policy_path=self.policy, helper_provenance=self.helpers, helper_sources={name: self.root / "libexec" / name for name in self.helpers}, source_revision="e" * 40)
        self.assertEqual(paths.cli.read_bytes(), b"actor replacement")

    def test_missing_policy_blocks_signed_install(self):
        paths = INSTALL.product_paths("boi", self.root)
        paths.app.mkdir()
        with self.assertRaisesRegex(INSTALL.InstallError, "policy"):
            INSTALL.install("boi", self.root, self.source, self.signer, policy_path=self.policy.with_name("missing.json"), helper_provenance=self.helpers, source_revision="e" * 40)

    def test_service_owner_requires_inherited_lock(self):
        with self.assertRaisesRegex(INSTALL.InstallError, "inherited product lock"):
            INSTALL.service_owner("boi", self.root, self.signer, policy_path=self.policy)

    def test_service_owner_returns_signed_current_and_checks_helpers(self):
        self.install()
        paths = INSTALL.product_paths("boi", self.root)
        fd = paths.lock.open("a+")
        try:
            import fcntl
            fcntl.flock(fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            owner = INSTALL.service_owner("boi", self.root, self.signer, policy_path=self.policy, lock_held=True)
        finally:
            fd.close()
        self.assertEqual(owner["mode"], "signed-current")
        self.assertTrue(owner["policy_available"])
        self.assertEqual(owner["source_revision"], "e" * 40)

    def test_service_owner_rejects_changed_helper(self):
        self.install()
        paths = INSTALL.product_paths("boi", self.root)
        helper = paths.root / "libexec" / "macos-app-install.py"
        helper.write_text("changed", encoding="utf-8")
        fd = paths.lock.open("a+")
        try:
            import fcntl
            fcntl.flock(fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaisesRegex(INSTALL.InstallError, "helper"):
                INSTALL.service_owner("boi", self.root, self.signer, policy_path=self.policy, lock_held=True)
        finally:
            fd.close()

    def test_helper_source_hash_mismatch_fails_before_publication(self):
        bad = dict(self.helpers)
        bad["macos-app-install.py"] = {"sha256": "0" * 64, "source_revision": "f" * 40}
        with self.assertRaisesRegex(INSTALL.InstallError, "changed during staging"):
            INSTALL.install("boi", self.root, self.source, self.signer, policy_path=self.policy, helper_provenance=bad, helper_sources={name: self.root / "libexec" / name for name in bad}, source_revision="e" * 40)

    def test_hex_agent_alias_publishes_and_points_to_hex(self):
        root = Path(self.temp.name) / ".hex"
        root.mkdir()
        (root / "libexec").mkdir()
        helpers = {}
        sources = {}
        for name in ("macos-signing.py", "macos-app-install.py"):
            path = root / "libexec" / name
            path.write_text(name, encoding="utf-8")
            helpers[name] = {"sha256": INSTALL._sha256(path), "source_revision": "f" * 40}
            sources[name] = path
        result = INSTALL.install("hex", root, self.source, self.signer, policy_path=self.policy, helper_provenance=helpers, helper_sources=sources, source_revision="e" * 40)
        paths = INSTALL.product_paths("hex", root)
        self.assertTrue(paths.alias.is_symlink())
        self.assertEqual(os.readlink(paths.alias), os.readlink(paths.cli))
        self.assertEqual(result["product"], "hex")

    def test_legacy_agent_alias_is_not_orphan_signed_evidence(self):
        root = self.root / "legacy-hex"
        paths = INSTALL.product_paths("hex", root)
        paths.cli.parent.mkdir(parents=True)
        paths.cli.write_bytes(b"legacy executable")
        paths.alias.symlink_to("hex")
        missing_policy = self.policy.with_name("missing.json")
        self.assertEqual(INSTALL.detect_mode("hex", root, missing_policy, self.signer), "legacy-raw")
        self.assertEqual(INSTALL.detect_mode("hex", root, self.policy, self.signer), "configured-legacy")

    def test_orphan_signed_agent_alias_blocks_legacy_and_empty(self):
        root = self.root / "orphan-hex"
        paths = INSTALL.product_paths("hex", root)
        paths.cli.parent.mkdir(parents=True)
        for raw_present in (False, True):
            if raw_present:
                paths.cli.write_bytes(b"legacy executable")
            for destination in ("../Hex.app/Contents/MacOS/hex", str(paths.executable)):
                paths.alias.symlink_to(destination)
                for policy in (self.policy, self.policy.with_name("missing.json")):
                    with self.assertRaisesRegex(INSTALL.InstallError, "orphan signed"):
                        INSTALL.detect_mode("hex", root, policy, self.signer)
                paths.alias.unlink()

    def test_verifier_version_missing_or_different_never_publishes(self):
        original = self.signer.verify_installed
        for value in (None, "2.0.0"):
            def verify(*args, **kwargs):
                result = original(*args, **kwargs)
                result.pop("version", None)
                if value is not None:
                    result["version"] = value
                return result
            self.signer.verify_installed = verify
            with self.assertRaisesRegex(INSTALL.InstallError, "version"):
                self.install()
            self.assertFalse(INSTALL.product_paths("boi", self.root).app.exists())
            # Each independent attempt gets a fresh root, not an erased journal.
            self.root = self.root.with_name(self.root.name + "-next")
            (self.root / "libexec").mkdir(parents=True)
            for name in self.helpers:
                (self.root / "libexec" / name).write_text(name)

    def test_mutation_directories_synced_before_committed_journal(self):
        original_sync, original_journal = INSTALL._fsync_dir, INSTALL._write_journal
        synced = set()
        def sync(fd):
            original_sync(fd)
            info = os.fstat(fd)
            synced.add((info.st_dev, info.st_ino))
        def journal(paths, value):
            if value["phase"] == "committed":
                for directory in (paths.root, paths.cli.parent, paths.root / "libexec", Path(value["rollback"])):
                    info = directory.stat()
                    self.assertIn((info.st_dev, info.st_ino), synced)
            original_journal(paths, value)
        INSTALL._fsync_dir, INSTALL._write_journal = sync, journal
        try:
            self.install()
        finally:
            INSTALL._fsync_dir, INSTALL._write_journal = original_sync, original_journal

    def test_directory_sync_failure_never_marks_committed(self):
        self.install()
        paths = INSTALL.product_paths("boi", self.root)
        old_bytes = paths.executable.read_bytes()
        self.source.write_bytes(b"new bytes must not commit")
        original = INSTALL._fsync_dir
        def fail(fd):
            raise OSError("injected directory sync error")
        INSTALL._fsync_dir = fail
        try:
            with self.assertRaisesRegex(INSTALL.InstallError, "directory sync error"):
                self.install()
        finally:
            INSTALL._fsync_dir = original
        self.assertNotEqual(json.loads(paths.journal.read_text())["phase"], "committed")
        self.assertEqual(paths.executable.read_bytes(), old_bytes)

    def test_inherited_lock_requires_held_lock_and_expected_inode(self):
        self.install()
        paths = INSTALL.product_paths("boi", self.root)
        unlocked = paths.lock.open("a+")
        try:
            with self.assertRaisesRegex(INSTALL.InstallError, "does not hold"):
                INSTALL._validate_lock_fd(unlocked.fileno(), paths)
        finally:
            unlocked.close()
        held = paths.lock.open("a+")
        import fcntl
        fcntl.flock(held.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            INSTALL._validate_lock_fd(held.fileno(), paths)
        finally:
            fcntl.flock(held.fileno(), fcntl.LOCK_UN)
            held.close()


@unittest.skipUnless(sys.platform == "darwin", "requires macOS renameatx_np")
class MacAppInstallCLITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        base = Path(self.temp.name)
        self.root = base / ".boi"
        self.root.mkdir()
        self.home = base
        self.policy = base / "Library/Application Support/Hex/build-signing/policy.json"
        self.policy.parent.mkdir(parents=True)
        self.policy.write_text(json.dumps({"schema_version": 1, "certificate_sha1": "A" * 40, "team_id": "TEAM123456"}), encoding="utf-8")
        self.source = base / "source"
        self.source.write_bytes(b"cli candidate")
        self.script = base / "macos-app-install.py"
        shutil.copy2(SOURCE, self.script)
        helper = base / "macos-signing.py"
        # Keep the exact accepted policy reader. Only the CLI crypto result is fake.
        shared = SOURCE.with_name("macos-signing.py").read_text().split('if __name__ == "__main__":')[0]
        fake_cli = "import json, pathlib, shutil, sys\nargs=sys.argv[1:]\nif args[0] == 'verify-installed':\n    bundle=pathlib.Path(args[1]); product=args[2]\n    print(json.dumps({'identifier':'com.mrap.boi','version':'1.0.0','team_id':'TEAM123456','certificate_sha1':'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA','designated_requirements':{'arm64':'anchor apple generic'},'mach_o_uuids':{'arm64':'11111111-1111-1111-1111-111111111111'}}))\nelse:\n    source=pathlib.Path(args[0]); candidate=pathlib.Path(args[3]); candidate.joinpath('Contents/MacOS').mkdir(parents=True); shutil.copy2(source,candidate/'Contents/MacOS/boi'); (candidate/'Contents/Info.plist').write_bytes(b'plist'); print(json.dumps({'identifier':'com.mrap.boi','version':'1.0.0','team_id':'TEAM123456','certificate_sha1':'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA','designated_requirements':{'arm64':'anchor apple generic'},'mach_o_uuids':{'arm64':'11111111-1111-1111-1111-111111111111'}}))\n"
        helper.write_text(shared + "\nif __name__ == '__main__':\n" + "\n".join("    " + line for line in fake_cli.splitlines()) + "\n")
        self.helper = helper
        self.addCleanup(self.temp.cleanup)

    def run_cli(self, *args):
        return subprocess.run([sys.executable, "-I", "-B", str(self.script), *args], capture_output=True, text=True, check=False, timeout=10, env={"HOME": str(self.home), "PATH": "/usr/bin:/bin"})

    def test_signer_runner_kills_timed_out_process_group(self):
        helper = Path(self.temp.name) / "slow-signing.py"
        helper.write_text("import time; time.sleep(2)", encoding="utf-8")
        runner = INSTALL.ProcessSigner(helper, timeout=0.05)
        with self.assertRaisesRegex(INSTALL.InstallError, "timed out"):
            runner._run(["verify-installed", "bundle", "boi"])

    def test_cli_rejects_noncentral_policy_before_mutation(self):
        other = self.home / "other-policy.json"
        shutil.copy2(self.policy, other)
        result = self.run_cli("preflight", "boi", "--root", str(self.root), "--policy", str(other))
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("central machine path", result.stderr)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_public_install_and_preflight(self):
        install = self.run_cli("install", "boi", "--root", str(self.root), "--source", str(self.source), "--version", "1.0.0", "--source-revision", "e" * 40, "--helper-source-revision", "f" * 40, "--policy", str(self.policy))
        self.assertEqual(install.returncode, 0, install.stderr)
        installed = json.loads(install.stdout)
        self.assertEqual(installed["schema_version"], 1)
        self.assertEqual(installed["product"], "boi")
        self.assertEqual(installed["mode"], "signed-current")
        self.assertEqual(installed["source_revision"], "e" * 40)
        self.assertEqual(installed["version"], "1.0.0")
        self.assertTrue(installed["generation"])
        self.assertEqual(set(installed["helpers"]), {"macos-signing.py", "macos-app-install.py"})
        self.assertTrue(all(set(value) == {"sha256", "source_revision"} for value in installed["helpers"].values()))
        result = self.run_cli("preflight", "boi", "--root", str(self.root), "--policy", str(self.policy))
        self.assertEqual(result.returncode, 0, result.stderr)
        preflight = json.loads(result.stdout)
        self.assertEqual(preflight["schema_version"], 1)
        self.assertEqual(preflight["source_revision"], "e" * 40)
        self.assertEqual(preflight["service_owner"]["mode"], "signed-current")


class FinalBoundaryTests(unittest.TestCase):
    def test_no_missing_shared_reader_fallback(self):
        with tempfile.TemporaryDirectory() as td:
            policy = Path(td) / "policy.json"
            policy.write_text(json.dumps({"schema_version": True, "certificate_sha1": "A" * 40, "team_id": "TEAM123456"}))
            original = INSTALL.__file__
            INSTALL.__file__ = str(Path(td) / "macos-app-install.py")
            try:
                with self.assertRaises(INSTALL.InstallError):
                    INSTALL._policy_mode(policy)
            finally:
                INSTALL.__file__ = original

    def test_actual_shared_policy_reader_rejects_bool_unknown_and_duplicate(self):
        with tempfile.TemporaryDirectory() as td:
            policy = Path(td) / "policy.json"
            valid = {"schema_version": 1, "certificate_sha1": "A" * 40, "team_id": "TEAM123456"}
            policy.write_text(json.dumps(valid))
            self.assertEqual(INSTALL._policy_mode(policy), "configured")
            payloads = [json.dumps(dict(valid, schema_version=True)), json.dumps(dict(valid, unknown=True)), '{"schema_version":1,' + json.dumps(valid)[1:]]
            for payload in payloads:
                policy.write_text(payload)
                with self.assertRaises(INSTALL.InstallError):
                    INSTALL._policy_mode(policy)

    def test_helper_limit_matches_service_consumer(self):
        with tempfile.TemporaryDirectory() as td:
            helper = Path(td) / "helper.py"
            helper.write_bytes(b"x" * (1024 * 1024 + 1))
            with self.assertRaisesRegex(INSTALL.InstallError, "too large"):
                INSTALL._read_helper_source(helper)

    def test_runner_drains_pipes_with_no_spooled_files(self):
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as td:
            helper = Path(td) / "large.py"
            helper.write_text("import sys\nsys.stdout.buffer.write(b'x' * 1048576); sys.stdout.flush()\n")
            with patch.object(tempfile, "TemporaryFile", side_effect=AssertionError("output must not spool to disk")):
                with self.assertRaisesRegex(INSTALL.InstallError, "output is too large"):
                    INSTALL.ProcessSigner(helper, timeout=2)._run([])

    def test_normal_leader_exit_does_not_leave_held_pipe_descendant(self):
        import fcntl
        with tempfile.TemporaryDirectory() as td:
            lock = Path(td) / "child.lock"
            helper = Path(td) / "descendant.py"
            helper.write_text("import os,fcntl,time\nr,w=os.pipe()\npid=os.fork()\nif pid == 0:\n os.close(r)\n f=open(" + repr(str(lock)) + ", 'w')\n fcntl.flock(f,fcntl.LOCK_EX)\n os.write(w,b'1');os.close(w)\n time.sleep(4)\n os._exit(0)\nos.close(w);os.read(r,1);os.close(r)\nprint('{}',flush=True)\n")
            with self.assertRaisesRegex(INSTALL.InstallError, "timed out"):
                INSTALL.ProcessSigner(helper, timeout=0.3)._run([])
            with lock.open('r') as stream:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)


    def test_cancellation_cleans_actual_pipe_holding_child(self):
        import fcntl
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as td:
            lock = Path(td) / "cancel-child.lock"
            helper = Path(td) / "cancel.py"
            helper.write_text("import os,fcntl,time\nr,w=os.pipe()\npid=os.fork()\nif pid == 0:\n os.close(r)\n f=open(" + repr(str(lock)) + ", 'w')\n fcntl.flock(f,fcntl.LOCK_EX)\n os.write(w,b'1');os.close(w)\n time.sleep(4)\n os._exit(0)\nos.close(w);os.read(r,1);os.close(r)\nprint('{}',flush=True)\ntime.sleep(4)\n")
            original = INSTALL.selectors.DefaultSelector
            interrupted = []
            class InterruptOnce(original):
                def select(self, timeout=None):
                    events = super().select(timeout)
                    if events and not interrupted:
                        interrupted.append(True)
                        raise KeyboardInterrupt("actual ready pipe cancellation")
                    return events
            with patch.object(INSTALL.selectors, "DefaultSelector", InterruptOnce):
                with self.assertRaises(KeyboardInterrupt):
                    INSTALL.ProcessSigner(helper, timeout=2)._run([])
            self.assertTrue(interrupted)
            with lock.open('r') as stream:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def test_stderr_bound_and_secret_environment_removal(self):
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as td:
            helper = Path(td) / "environment.py"
            helper.write_text("import json,os\nprint(json.dumps({'leaked': 'INSTALL_TEST_SECRET' in os.environ}))\n")
            with patch.dict(os.environ, {"INSTALL_TEST_SECRET": "fixture-only-not-a-secret"}):
                self.assertEqual(INSTALL.ProcessSigner(helper)._run([]), {"leaked": False})
            helper.write_text("import sys\nsys.stderr.buffer.write(b'x' * 1048576);sys.stderr.flush()\n")
            with self.assertRaisesRegex(INSTALL.InstallError, "output is too large"):
                INSTALL.ProcessSigner(helper, timeout=2)._run([])



if __name__ == "__main__":
    unittest.main(verbosity=2)
