#!/usr/bin/env python3
"""Install a signed macOS app bundle with an owned, recoverable transaction.

This module owns filesystem publication only. Signature creation and signature
verification stay behind the injected signer protocol.
"""
from __future__ import annotations

import argparse
import contextlib
import ctypes
import dataclasses
import errno
import fcntl
import hashlib
import json
import os
import re
import secrets
import selectors
import types
import shutil
import stat
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Protocol


SCHEMA_VERSION = 1
STATE_SCHEMA_VERSION = 1
JOURNAL_SCHEMA_VERSION = 1
RENAME_SWAP = 0x00000002
RENAME_EXCL = 0x00000004
POLICY_RELATIVE = Path("Library/Application Support/Hex/build-signing/policy.json")
MAX_JSON_BYTES = 64 * 1024
MAX_HELPER_BYTES = 1024 * 1024


class InstallError(RuntimeError):
    """A bounded, operator-facing install or recovery failure."""

    def __init__(self, message: str, *, published: Optional[bool] = None):
        super().__init__(message)
        self.published = published


class Signer(Protocol):
    def stage(self, source: Path, product: str, policy: Path, candidate: Path, receipt: Path) -> Mapping[str, Any]: ...

    def verify_installed(self, bundle: Path, product: str, policy: Optional[Path], expected: Optional[Mapping[str, Any]] = None) -> Mapping[str, Any]: ...


class ProcessSigner:
    """Adapter for the accepted standalone signer helper CLI."""

    def __init__(self, helper: Optional[Path] = None, *, python: str = "/usr/bin/python3", timeout: float = 30.0):
        helper = helper or Path(__file__).with_name("macos-signing.py")
        if not helper.is_file() or helper.is_symlink():
            raise InstallError(f"signer helper is not a regular file: {helper}")
        self.helper = helper.absolute()
        self.python = python
        self.timeout = timeout

    def _run(self, argv: list[str]) -> Mapping[str, Any]:
        environment = {"HOME": str(Path.home()), "PATH": "/usr/bin:/bin", "LC_ALL": "C"}
        command = [self.python, "-I", "-B", str(self.helper), *argv]
        process = None
        try:
            process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                       env=environment, start_new_session=True)
            deadline = time.monotonic() + self.timeout
            buffers = [bytearray(), bytearray()]
            # Do not poll or reap the leader until both pipes close. Its allocated
            # PID anchors group cleanup when a cooperative descendant holds a pipe.
            with selectors.DefaultSelector() as selector:
                for index, stream in enumerate((process.stdout, process.stderr)):
                    os.set_blocking(stream.fileno(), False)
                    selector.register(stream, selectors.EVENT_READ, index)
                while selector.get_map():
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise InstallError(f"signer helper timed out: {self.helper}")
                    for key, _ in selector.select(remaining):
                        buffer = buffers[key.data]
                        chunk = os.read(key.fd, min(65536, MAX_JSON_BYTES + 1 - len(buffer)))
                        if not chunk:
                            selector.unregister(key.fileobj)
                        else:
                            buffer.extend(chunk)
                            if len(buffer) > MAX_JSON_BYTES:
                                raise InstallError("signer helper output is too large")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise InstallError(f"signer helper timed out: {self.helper}")
            returncode = process.wait(timeout=remaining)
            output, error = bytes(buffers[0]), bytes(buffers[1])
        except BaseException as exc:
            # Popen.wait timeout does not reap. No group signal follows a successful
            # wait, so a reused leader PID cannot select an unrelated process group.
            cleanup_error = None
            if process is not None and process.returncode is None:
                try:
                    os.killpg(process.pid, 9)
                except ProcessLookupError:
                    pass
                except OSError as failure:
                    cleanup_error = failure
                cleanup_deadline = time.monotonic() + 5
                try:
                    # Observe EOF after the group signal while the leader is still
                    # unreaped. Discard remaining bytes with constant memory.
                    with selectors.DefaultSelector() as cleanup_selector:
                        for stream in (process.stdout, process.stderr):
                            os.set_blocking(stream.fileno(), False)
                            cleanup_selector.register(stream, selectors.EVENT_READ)
                        while cleanup_selector.get_map():
                            remaining = cleanup_deadline - time.monotonic()
                            if remaining <= 0:
                                raise subprocess.TimeoutExpired(command, 5)
                            for key, _ in cleanup_selector.select(remaining):
                                if not os.read(key.fd, 65536):
                                    cleanup_selector.unregister(key.fileobj)
                except (OSError, subprocess.TimeoutExpired) as failure:
                    cleanup_error = failure
                try:
                    process.wait(timeout=max(0.001, cleanup_deadline - time.monotonic()))
                except (OSError, subprocess.TimeoutExpired) as failure:
                    cleanup_error = failure
            if cleanup_error is not None:
                raise InstallError(f"signer helper cleanup failed: {cleanup_error}; original: {exc}") from exc
            if isinstance(exc, (OSError, subprocess.TimeoutExpired)):
                raise InstallError(f"signer helper unavailable or timed out: {self.helper}") from exc
            raise
        finally:
            if process is not None:
                for stream in (process.stdout, process.stderr):
                    if stream is not None:
                        stream.close()
        if returncode:
            raise InstallError(f"signer helper failed ({returncode}): {error.decode(errors='replace').strip()[-500:]}")
        try:
            value = json.loads(output.decode("utf-8"))
        except (ValueError, UnicodeError) as exc:
            raise InstallError("signer helper returned invalid JSON") from exc
        if not isinstance(value, dict):
            raise InstallError("signer helper returned a non-object")
        return value

    def stage(self, source: Path, product: str, policy: Path, candidate: Path, receipt: Path, *, version: str = "1.0.0") -> Mapping[str, Any]:
        return self._run([str(source), product, str(policy), str(candidate), "--version", version, "--receipt", str(receipt)])

    def verify_installed(self, bundle: Path, product: str, policy: Optional[Path], expected: Optional[Mapping[str, Any]] = None) -> Mapping[str, Any]:
        args = ["verify-installed", str(bundle), product]
        if policy is not None:
            args.append(str(policy))
        return self._run(args)


@dataclasses.dataclass(frozen=True)
class Product:
    name: str
    app_name: str
    executable: str
    bundle_identifier: str
    cli_relative: Path
    alias_relative: Optional[Path]
    state_name: str


PRODUCTS = {
    "hex": Product("hex", "Hex.app", "hex", "com.mrap.hex", Path("bin/hex"), Path("bin/hex-agent"), "Hex.app.install-state.json"),
    "boi": Product("boi", "BOI.app", "boi", "com.mrap.boi", Path("bin/boi"), None, "BOI.app.install-state.json"),
}


@dataclasses.dataclass(frozen=True)
class Paths:
    product: str
    root: Path
    app: Path
    executable: Path
    cli: Path
    alias: Optional[Path]
    state: Path
    lock: Path
    journal: Path


def central_policy_path(home: Optional[Path] = None) -> Path:
    """Return the one machine policy path. Tests pass an explicit policy."""
    base = home if home is not None else Path.home()
    return base / POLICY_RELATIVE


def product_paths(product: str, root: Path) -> Paths:
    item = PRODUCTS.get(product)
    if item is None:
        raise InstallError(f"unknown product: {product}")
    root = root.absolute()
    app = root / item.app_name
    return Paths(
        product=product,
        root=root,
        app=app,
        executable=app / "Contents" / "MacOS" / item.executable,
        cli=root / item.cli_relative,
        alias=root / item.alias_relative if item.alias_relative else None,
        state=root / item.state_name,
        lock=root / f".{product}.app-install.lock",
        journal=root / f".{product}.app-install.journal.json",
    )


def _real(path: Path) -> Path:
    return path.absolute().resolve(strict=False)


def _within(path: Path, root: Path) -> bool:
    try:
        _real(path).relative_to(_real(root))
        return True
    except ValueError:
        return False


def _regular_identity(path: Path) -> dict[str, Any]:
    try:
        st = path.lstat()
    except OSError as exc:
        raise InstallError(f"cannot inspect {path}: {exc}") from exc
    return {"dev": st.st_dev, "ino": st.st_ino, "mode": stat.S_IMODE(st.st_mode), "size": st.st_size, "mtime_ns": st.st_mtime_ns}


def _entry_identity(path: Path) -> dict[str, Any]:
    if not os.path.lexists(path):
        return {"present": False}
    st = path.lstat()
    result: dict[str, Any] = {"present": True, "dev": st.st_dev, "ino": st.st_ino, "mode": stat.S_IMODE(st.st_mode), "kind": stat.filemode(st.st_mode)[0]}
    if stat.S_ISREG(st.st_mode):
        result["sha256"] = _sha256(path)
    elif stat.S_ISLNK(st.st_mode):
        result["target"] = os.readlink(path)
    return result


def _revalidate_parent(fd: int, path: Path) -> None:
    actual = os.fstat(fd)
    expected = path.stat()
    if not stat.S_ISDIR(actual.st_mode) or (actual.st_dev, actual.st_ino) != (expected.st_dev, expected.st_ino):
        raise InstallError(f"parent directory changed: {path}")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_helper_source(path: Path, expected_sha256: Optional[str] = None) -> bytes:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise InstallError(f"helper source is not a regular file: {path}")
        chunks = []
        total = 0
        while True:
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_HELPER_BYTES:
                raise InstallError(f"helper source is too large: {path}")
            chunks.append(chunk)
    finally:
        os.close(fd)
    content = b"".join(chunks)
    if expected_sha256 is not None and hashlib.sha256(content).hexdigest() != expected_sha256:
        raise InstallError(f"helper source changed during staging: {path}")
    return content


def _tree_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    if not path.is_dir() or path.is_symlink():
        raise InstallError(f"app candidate is not a directory: {path}")
    for child in sorted(path.rglob("*")):
        relative = child.relative_to(path).as_posix().encode()
        st = child.lstat()
        digest.update(relative + b"\0" + str(stat.S_IFMT(st.st_mode)).encode() + b"\0")
        if stat.S_ISREG(st.st_mode):
            digest.update(_sha256(child).encode())
        elif stat.S_ISLNK(st.st_mode):
            digest.update(os.readlink(child).encode())
    return digest.hexdigest()


def _json_digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _fsync_dir(fd: int) -> None:
    os.fsync(fd)


@contextlib.contextmanager
def _open_dir(path: Path, *, create: bool = False):
    if create:
        path.mkdir(parents=True, exist_ok=True)
    elif not path.is_dir() or path.is_symlink():
        raise InstallError(f"directory is missing or aliased: {path}")
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    try:
        yield fd
    finally:
        os.close(fd)


@contextlib.contextmanager
def _product_lock(paths: Paths):
    paths.root.mkdir(parents=True, exist_ok=True)
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(paths.lock, flags, 0o600)
    except OSError as exc:
        raise InstallError(f"cannot open lock {paths.lock}: {exc}") from exc
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise InstallError(f"lock is not a regular file: {paths.lock}")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno in (errno.EACCES, errno.EAGAIN):
                raise InstallError(f"product install busy: {paths.lock}") from exc
            raise InstallError(f"cannot lock {paths.lock}: {exc}") from exc
        yield fd
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def _libc_renameatx(source_fd: int, source: str, destination_fd: int, destination: str, flags: int) -> None:
    if sys.platform != "darwin":
        raise InstallError("atomic app publication requires macOS renameatx_np")
    libc = ctypes.CDLL(None, use_errno=True)
    function = getattr(libc, "renameatx_np", None)
    if function is None:
        raise InstallError("macOS renameatx_np is unavailable")
    function.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    function.restype = ctypes.c_int
    if function(source_fd, os.fsencode(source), destination_fd, os.fsencode(destination), flags) != 0:
        error = ctypes.get_errno()
        raise InstallError(f"renameatx_np {source} -> {destination} failed: {os.strerror(error)}")


def _atomic_new(parent_fd: int, source: Path, destination: Path) -> None:
    if source.parent != destination.parent:
        raise InstallError("atomic publication requires one parent")
    _libc_renameatx(parent_fd, source.name, parent_fd, destination.name, RENAME_EXCL)


def _atomic_swap(parent_fd: int, source: Path, destination: Path) -> None:
    if source.parent != destination.parent:
        raise InstallError("atomic swap requires one parent")
    _libc_renameatx(parent_fd, source.name, parent_fd, destination.name, RENAME_SWAP)


def _atomic_move(source_fd: int, source: Path, destination_fd: int, destination: Path) -> None:
    if destination.exists() or destination.is_symlink():
        raise InstallError(f"rollback destination already exists: {destination}")
    _libc_renameatx(source_fd, source.name, destination_fd, destination.name, RENAME_EXCL)


def _write_private(path: Path, payload: bytes) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        offset = 0
        while offset < len(payload):
            written = os.write(fd, payload[offset:])
            if written <= 0:
                raise InstallError(f"short write to {path}")
            offset += written
        os.fsync(fd)
    finally:
        os.close(fd)


def _read_json(path: Path, label: str) -> dict[str, Any]:
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise InstallError(f"duplicate key in {label}: {key}")
            result[key] = value
        return result
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0))
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise InstallError(f"{label} is not a regular file: {path}")
            chunks = []
            total = 0
            while True:
                chunk = os.read(fd, 8192)
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_JSON_BYTES:
                    raise InstallError(f"{label} is too large: {path}")
                chunks.append(chunk)
        finally:
            os.close(fd)
        value = json.loads(b"".join(chunks).decode("utf-8"), object_pairs_hook=pairs)
    except (OSError, ValueError, UnicodeError) as exc:
        raise InstallError(f"invalid {label}: {path}") from exc
    if not isinstance(value, dict):
        raise InstallError(f"invalid {label}: {path}")
    return value


def _write_journal(paths: Paths, journal: Mapping[str, Any]) -> None:
    required = {"schema_version", "transaction_id", "product", "phase", "root", "candidate", "rollback", "expected_old_app", "expected_old_cli"}
    if journal.get("schema_version") != JOURNAL_SCHEMA_VERSION or not required.issubset(journal):
        raise InstallError("invalid journal schema")
    if journal["product"] != paths.product or journal["root"] != str(paths.root):
        raise InstallError("journal product or root does not match fixed paths")
    for key in ("candidate", "rollback"):
        candidate_path = Path(str(journal[key]))
        if not _within(candidate_path, paths.root) or candidate_path.parent != paths.root:
            raise InstallError(f"journal {key} escaped fixed root")
    temporary = paths.journal.with_name(paths.journal.name + f".tmp-{journal['transaction_id']}")
    if os.path.lexists(temporary):
        raise InstallError(f"journal temporary path exists: {temporary}")
    if os.path.lexists(paths.journal):
        current = _read_json(paths.journal, "transaction journal")
        if current.get("transaction_id") != journal.get("transaction_id"):
            raise InstallError("journal ownership changed")
    _write_private(temporary, (json.dumps(journal, sort_keys=True, indent=2) + "\n").encode())
    if os.path.lexists(paths.journal):
        os.replace(temporary, paths.journal)
    else:
        fd = os.open(paths.root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0))
        try:
            _atomic_new(fd, temporary, paths.journal)
        finally:
            os.close(fd)
    fd = os.open(paths.root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _path_guard(paths: Paths) -> None:
    if paths.root.is_symlink() or not _within(paths.app, paths.root) or not _within(paths.cli, paths.root) or paths.app.parent != paths.root or paths.cli.parent != paths.root / "bin":
        raise InstallError("product path escaped fixed root")


def _policy_mode(policy: Path) -> str:
    if policy.is_symlink():
        raise InstallError(f"central signing policy must not be a symlink: {policy}")
    if not policy.exists():
        return "missing"
    if not policy.is_file():
        raise InstallError(f"central signing policy is not a regular file: {policy}")
    # Use the exact retained sibling source, never a second policy validator.
    signer_path = Path(__file__).with_name("macos-signing.py")
    module_name = "_macos_install_policy_" + secrets.token_hex(8)
    module = types.ModuleType(module_name)
    module.__file__ = str(signer_path)
    sys.modules[module_name] = module
    try:
        content = _read_helper_source(signer_path)
        exec(compile(content, str(signer_path), "exec"), module.__dict__)
        reader = getattr(module, "_read_policy", None)
        if not callable(reader):
            raise InstallError("accepted signer policy reader is unavailable")
        reader(policy)
    except Exception as exc:
        raise InstallError(f"invalid central signing policy: {policy}: {exc}") from exc
    finally:
        del sys.modules[module_name]
    return "configured"


def _valid_revision(value: Any) -> bool:
    return isinstance(value, str) and bool(re.fullmatch(r"(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})", value))


def _validate_state(paths: Paths, product: Product, state: Mapping[str, Any]) -> None:
    required = {"schema_version", "product", "mode", "bundle_identifier", "bundle_path", "executable_path", "compatibility_path", "generation", "transaction_id", "team_id", "certificate_sha1", "designated_requirements", "mach_o_uuids", "bundle_sha256", "executable_sha256", "helpers", "source_revision", "version"}
    if state.get("schema_version") != STATE_SCHEMA_VERSION or not required.issubset(state):
        raise InstallError(f"invalid state record: {paths.state}")
    if state["product"] != product.name or state["mode"] != "signed-current" or state["bundle_identifier"] != product.bundle_identifier or not _valid_revision(state["source_revision"]) or not isinstance(state["version"], str) or not state["version"]:
        raise InstallError("state record does not match fixed product map")
    for field in ("bundle_path", "executable_path", "compatibility_path"):
        if not _within(Path(state[field]), paths.root):
            raise InstallError("state record path escaped fixed root")
    if _real(Path(state["bundle_path"])) != _real(paths.app) or _real(Path(state["executable_path"])) != _real(paths.executable) or _real(Path(state["compatibility_path"])) != _real(paths.cli):
        raise InstallError("state record path mismatch")
    helpers = state["helpers"]
    if not isinstance(helpers, dict) or set(helpers) != {"macos-signing.py", "macos-app-install.py"}:
        raise InstallError("state helper provenance is incomplete")
    for helper in helpers.values():
        if not isinstance(helper, dict) or set(helper) != {"sha256", "source_revision"} or len(helper["sha256"]) != 64 or not re.fullmatch(r"[0-9a-fA-F]{64}", helper["sha256"]) or not _valid_revision(helper["source_revision"]):
            raise InstallError("invalid state helper provenance")


def _validate_current_helpers(paths: Paths, helpers: Mapping[str, Any]) -> None:
    for name, helper in helpers.items():
        path = paths.root / "libexec" / name
        if not path.is_file() or path.is_symlink() or _sha256(path) != helper["sha256"]:
            raise InstallError(f"installed helper does not match state: {name}")


def _verify_matches_state(state: Mapping[str, Any], verified: Mapping[str, Any], product: Product) -> None:
    if verified.get("identifier", verified.get("bundle_identifier")) != product.bundle_identifier:
        raise InstallError("verifier returned the wrong product identifier")
    for state_key, verified_key in (("team_id", "team_id"), ("certificate_sha1", "certificate_sha1"), ("designated_requirements", "designated_requirements"), ("mach_o_uuids", "mach_o_uuids")):
        if state.get(state_key) != verified.get(verified_key):
            raise InstallError(f"installed verifier result does not match state: {state_key}")
    if state.get("bundle_sha256") != verified.get("bundle_sha256", state.get("bundle_sha256")) or state.get("executable_sha256") != verified.get("executable_sha256", state.get("executable_sha256")):
        raise InstallError("installed verifier result does not match current hashes")


def detect_mode(product: str, root: Path, policy_path: Path, signer: Signer) -> str:
    item = PRODUCTS.get(product)
    if item is None:
        raise InstallError(f"unknown product: {product}")
    paths = product_paths(product, root)
    _path_guard(paths)
    if os.path.lexists(paths.journal):
        journal = _read_json(paths.journal, "transaction journal")
        if journal.get("schema_version") != JOURNAL_SCHEMA_VERSION or journal.get("product") != product or journal.get("root") != str(paths.root):
            raise InstallError(f"invalid or foreign install journal: {paths.journal}")
        raise InstallError(f"open install journal requires recovery: {paths.journal}")
    app = os.path.lexists(paths.app)
    cli = os.path.lexists(paths.cli)
    state_exists = os.path.lexists(paths.state)
    policy = _policy_mode(policy_path)
    if state_exists:
        state = _read_json(paths.state, "state record")
        _validate_state(paths, item, state)
        if not app or not cli or (paths.alias is not None and not os.path.lexists(paths.alias)):
            raise InstallError("signed state is missing its app or compatibility path")
        if policy != "configured":
            return "signed-policy-missing"
        verified = signer.verify_installed(paths.app, product, policy_path, state)
        _verify_matches_state(state, verified, item)
        return "signed-current"
    if paths.alias is not None and paths.alias.is_symlink() and _within(paths.alias, paths.app):
        raise InstallError("orphan signed agent alias exists without signed state record")
    if app:
        raise InstallError("app exists without signed state record")
    if cli:
        if not paths.cli.is_file() or paths.cli.is_symlink():
            raise InstallError("legacy compatibility path is not a regular file")
        return "configured-legacy" if policy == "configured" else "legacy-raw"
    return "empty"


def service_owner(product: str, root: Path, signer: Signer, *, policy_path: Optional[Path] = None, lock_held: bool = False) -> dict[str, Any]:
    """Return verified public owner state for a caller holding the product lock."""
    if not lock_held:
        raise InstallError("service_owner requires the inherited product lock")
    item = PRODUCTS.get(product)
    if item is None:
        raise InstallError(f"unknown product: {product}")
    paths = product_paths(product, root)
    _path_guard(paths)
    if not paths.root.is_dir() or not paths.cli.parent.is_dir():
        raise InstallError("product root or compatibility parent is absent")
    if os.path.lexists(paths.journal):
        raise InstallError(f"open install journal blocks service ownership: {paths.journal}")
    if not paths.state.is_file() or not paths.app.is_dir() or not paths.executable.is_file() or not paths.cli.exists():
        raise InstallError("signed service owner is not installed")
    if not paths.cli.is_symlink() or os.readlink(paths.cli) != _make_relative_cli_target(paths):
        raise InstallError("compatibility path does not point to the installed executable")
    if paths.alias is not None and (not paths.alias.is_symlink() or os.readlink(paths.alias) != _make_relative_cli_target(paths)):
        raise InstallError("agent compatibility path does not point to the installed executable")
    state = _read_json(paths.state, "state record")
    _validate_state(paths, item, state)
    _validate_current_helpers(paths, state["helpers"])
    policy = policy_path or central_policy_path()
    if _policy_mode(policy) != "configured":
        raise InstallError("central signing policy is required to verify service ownership")
    verified = dict(signer.verify_installed(paths.app, product, policy, state))
    _verify_matches_state(state, verified, item)
    if verified.get("version") != state["version"]:
        raise InstallError("installed verifier result does not match state: version")
    if _tree_sha256(paths.app) != state["bundle_sha256"] or _sha256(paths.executable) != state["executable_sha256"]:
        raise InstallError("installed app hashes differ from state")
    return {"schema_version": STATE_SCHEMA_VERSION, "product": product, "mode": "signed-current", "policy_available": True, "bundle_path": str(paths.app.absolute()), "executable_path": str(paths.executable.absolute()), "compatibility_path": str(paths.cli.absolute()), "bundle_identifier": item.bundle_identifier, "generation": state["generation"], "version": state["version"], "team_id": state["team_id"], "certificate_sha1": state["certificate_sha1"], "designated_requirements": state["designated_requirements"], "mach_o_uuids": state["mach_o_uuids"], "bundle_sha256": state["bundle_sha256"], "executable_sha256": state["executable_sha256"], "helpers": state["helpers"], "source_revision": state["source_revision"]}


def preflight(product: str, root: Path, signer: Signer, *, policy_path: Optional[Path] = None, lock_held: bool = False) -> dict[str, Any]:
    """Return bounded mode and owner information without mutating the root."""
    if not lock_held:
        raise InstallError("preflight requires the inherited product lock")
    policy = policy_path or central_policy_path()
    mode = detect_mode(product, root, policy, signer)
    result = {"schema_version": STATE_SCHEMA_VERSION, "product": product, "mode": mode, "policy_available": _policy_mode(policy) == "configured", "managed": _policy_mode(policy) == "configured" or product_paths(product, root).state.exists(), "policy_path": str(policy.absolute()), "state_path": str(product_paths(product, root).state.absolute()), "lock_path": str(product_paths(product, root).lock.absolute()), "journal_path": str(product_paths(product, root).journal.absolute())}
    if mode.startswith("signed"):
        result["service_owner"] = service_owner(product, root, signer, policy_path=policy, lock_held=True)
        result["source_revision"] = result["service_owner"]["source_revision"]
    return result


def _same_identity(path: Path, expected: Mapping[str, Any]) -> bool:
    return _entry_identity(path) == dict(expected)


def _make_relative_cli_target(paths: Paths) -> str:
    return os.path.relpath(paths.executable, paths.cli.parent)


def _new_candidate(paths: Paths, transaction_id: str) -> Path:
    return paths.root / f".{paths.app.stem}.candidate-{transaction_id}.app"


def _new_cli_candidate(paths: Paths, transaction_id: str) -> Path:
    return paths.cli.parent / f".{paths.cli.name}.candidate-{transaction_id}"


def _rollback_dir(paths: Paths, transaction_id: str) -> Path:
    return paths.root / f".{paths.product}.app-install-rollback-{transaction_id}"


def _clear_journal(paths: Paths, transaction_id: str) -> None:
    if not paths.journal.is_file() or _read_json(paths.journal, "transaction journal").get("transaction_id") != transaction_id:
        raise InstallError("journal ownership changed before cleanup")
    paths.journal.unlink()
    with _open_dir(paths.root) as root_fd:
        _fsync_dir(root_fd)


def _restore_public(parent_fd: int, target: Path, rollback: Path, old_name: str, had_old: bool, transaction_id: str, expected_current: Mapping[str, Any], fallback: Optional[Path]) -> None:
    if not os.path.lexists(target):
        return
    if _entry_identity(target) != dict(expected_current):
        raise InstallError(f"refusing rollback over actor replacement: {target}")
    rollback_fd = os.open(rollback, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0))
    try:
        if had_old:
            archived = rollback / old_name
            if not os.path.lexists(archived) and fallback is not None and os.path.lexists(fallback):
                _atomic_move(parent_fd, fallback, rollback_fd, archived)
            temporary = target.parent / f".{target.name}.failed-{transaction_id}"
            if os.path.lexists(temporary):
                raise InstallError(f"rollback temporary path exists: {temporary}")
            _atomic_move(rollback_fd, rollback / old_name, parent_fd, temporary)
            _revalidate_parent(parent_fd, target.parent)
            if _entry_identity(target) != dict(expected_current):
                raise InstallError(f"refusing rollback over actor replacement: {target}")
            _atomic_swap(parent_fd, temporary, target)
            _atomic_move(parent_fd, temporary, rollback_fd, rollback / f"failed-{old_name}")
        else:
            _atomic_move(parent_fd, target, rollback_fd, rollback / f"failed-{old_name}")
    finally:
        os.close(rollback_fd)


def install(product: str, root: Path, source: Path, signer: Signer, *, policy_path: Optional[Path] = None, helper_provenance: Optional[Mapping[str, Any]] = None, helper_sources: Optional[Mapping[str, Path]] = None, source_revision: Optional[str] = None, version: str = "1.0.0") -> dict[str, Any]:
    """Stage and publish one complete app. The caller owns no service action."""
    item = PRODUCTS.get(product)
    if item is None:
        raise InstallError(f"unknown product: {product}")
    paths = product_paths(product, root)
    policy = policy_path or central_policy_path()
    _path_guard(paths)
    if source_revision is None or not _valid_revision(source_revision) or helper_provenance is None:
        raise InstallError("source revision and both helper provenance records are required")
    if set(helper_provenance) != {"macos-signing.py", "macos-app-install.py"}:
        raise InstallError("both helper provenance records are required")
    if not all(isinstance(value, Mapping) and set(value) == {"sha256", "source_revision"} and _valid_revision(value.get("source_revision")) and re.fullmatch(r"[0-9a-fA-F]{64}", str(value.get("sha256"))) for value in helper_provenance.values()):
        raise InstallError("helper source revisions must be full git SHA values")
    if _policy_mode(policy) != "configured":
        if os.path.lexists(paths.state) or os.path.lexists(paths.app):
            raise InstallError(f"central signing policy missing in signed mode: {policy}")
        raise InstallError(f"central signing policy required for signed publication: {policy}")
    transaction_id = secrets.token_hex(12)
    candidate = _new_candidate(paths, transaction_id)
    receipt = candidate.with_name(candidate.name + ".receipt.json")
    rollback = _rollback_dir(paths, transaction_id)
    old_app = _entry_identity(paths.app)
    old_cli = _entry_identity(paths.cli)
    old_alias = _entry_identity(paths.alias) if paths.alias is not None else {"present": False}
    old_state = _entry_identity(paths.state)
    if helper_sources is None:
        raise InstallError("both helper source files are required")
    if set(helper_sources) != set(helper_provenance):
        raise InstallError("both helper source files are required")
    for name, source_path in helper_sources.items():
        if not source_path.is_file() or source_path.is_symlink():
            raise InstallError(f"helper source is not a regular file: {source_path}")
    paths.root.mkdir(parents=True, exist_ok=True)
    (paths.root / "libexec").mkdir(parents=True, exist_ok=True)
    with _product_lock(paths), _open_dir(paths.root) as app_fd, _open_dir(paths.cli.parent, create=True) as cli_fd, _open_dir(paths.root / "libexec") as helper_fd:
        if os.path.lexists(paths.journal):
            raise InstallError(f"open install journal requires recovery: {paths.journal}")
        initial_mode = detect_mode(product, root, policy, signer)
        if initial_mode == "signed-policy-missing":
            raise InstallError("signed product cannot be replaced while central policy is missing")
        if initial_mode == "signed-current" and old_app != _entry_identity(paths.app):
            raise InstallError("app changed during state detection")
        journal: dict[str, Any] = {"schema_version": JOURNAL_SCHEMA_VERSION, "transaction_id": transaction_id, "product": product, "phase": "staging", "root": str(paths.root), "candidate": str(candidate), "rollback": str(rollback), "expected_old_app": old_app, "expected_old_cli": old_cli}
        published: list[tuple[int, Path, str, bool, dict[str, Any], Optional[Path]]] = []
        _write_journal(paths, journal)
        try:
            if isinstance(signer, ProcessSigner):
                result = dict(signer.stage(source, product, policy, candidate, receipt, version=version))
            else:
                result = dict(signer.stage(source, product, policy, candidate, receipt))
            verified = dict(signer.verify_installed(candidate, product, policy, result))
            if verified.get("identifier", verified.get("bundle_identifier")) != item.bundle_identifier:
                raise InstallError("signer result has the wrong product identifier")
            if verified.get("version") != version:
                raise InstallError("verified candidate version differs from requested version")
            candidate_executable = candidate / "Contents" / "MacOS" / item.executable
            if not candidate.is_dir() or candidate.is_symlink() or not candidate_executable.is_file() or candidate_executable.is_symlink():
                raise InstallError("signer did not produce a complete app candidate")
            if _entry_identity(paths.app) != old_app or _entry_identity(paths.cli) != old_cli or (paths.alias is not None and _entry_identity(paths.alias) != old_alias):
                raise InstallError("destination changed during staging")
            rollback.mkdir()
            journal.update({"phase": "app-swap", "candidate_hash": _tree_sha256(candidate), "verified": verified})
            _write_journal(paths, journal)
            helper_candidates = {}
            old_helpers = {}
            for name, source_path in helper_sources.items():
                candidate_helper = paths.root / "libexec" / f".{name}.candidate-{transaction_id}"
                _write_private(candidate_helper, _read_helper_source(source_path, str(helper_provenance[name]["sha256"])))
                helper_candidates[name] = candidate_helper
                old_helpers[name] = _entry_identity(paths.root / "libexec" / name)
            if _entry_identity(paths.app) != old_app or _entry_identity(paths.cli) != old_cli:
                raise InstallError("destination changed before helper publication")
            journal.update({"phase": "helper-swap", "old_helpers": old_helpers})
            _write_journal(paths, journal)
            for name, candidate_helper in helper_candidates.items():
                target = paths.root / "libexec" / name
                _revalidate_parent(helper_fd, paths.root / "libexec")
                if old_helpers[name]["present"]:
                    _atomic_swap(helper_fd, candidate_helper, target)
                    published.append((helper_fd, target, f"previous-{name}", True, _entry_identity(target), candidate_helper))
                    rollback_fd = os.open(rollback, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
                    try:
                        _atomic_move(helper_fd, candidate_helper, rollback_fd, rollback / f"previous-{name}")
                    finally:
                        os.close(rollback_fd)
                else:
                    _atomic_new(helper_fd, candidate_helper, target)
                    published.append((helper_fd, target, f"previous-{name}", False, _entry_identity(target), None))
            if _entry_identity(paths.app) != old_app or _entry_identity(paths.cli) != old_cli:
                raise InstallError("destination changed before app publication")
            if old_app["present"]:
                _revalidate_parent(app_fd, paths.root)
                _atomic_swap(app_fd, candidate, paths.app)
                published_app = _entry_identity(paths.app)
                published.append((app_fd, paths.app, "previous-app", True, published_app, candidate))
                rollback_fd = os.open(rollback, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
                try:
                    _atomic_move(app_fd, candidate, rollback_fd, rollback / "previous-app")
                finally:
                    os.close(rollback_fd)
            else:
                _revalidate_parent(app_fd, paths.root)
                _atomic_new(app_fd, candidate, paths.app)
                published_app = _entry_identity(paths.app)
                published.append((app_fd, paths.app, "previous-app", False, published_app, None))
            cli_candidate = _new_cli_candidate(paths, transaction_id)
            cli_candidate.parent.mkdir(parents=True, exist_ok=True)
            os.symlink(_make_relative_cli_target(paths), cli_candidate)
            journal["phase"] = "cli-swap"
            _write_journal(paths, journal)
            if _entry_identity(paths.app) != published_app or _entry_identity(paths.cli) != old_cli:
                raise InstallError("destination changed before compatibility publication")
            if old_cli["present"]:
                _revalidate_parent(cli_fd, paths.cli.parent)
                _atomic_swap(cli_fd, cli_candidate, paths.cli)
                published_cli = _entry_identity(paths.cli)
                published.append((cli_fd, paths.cli, "previous-cli", True, published_cli, cli_candidate))
                rollback_fd = os.open(rollback, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
                try:
                    _atomic_move(cli_fd, cli_candidate, rollback_fd, rollback / "previous-cli")
                finally:
                    os.close(rollback_fd)
            else:
                _revalidate_parent(cli_fd, paths.cli.parent)
                _atomic_new(cli_fd, cli_candidate, paths.cli)
                published_cli = _entry_identity(paths.cli)
                published.append((cli_fd, paths.cli, "previous-cli", False, published_cli, None))
            published_alias = published_cli
            if paths.alias is not None:
                alias_candidate = paths.alias.parent / f".{paths.alias.name}.candidate-{transaction_id}"
                os.symlink(_make_relative_cli_target(paths), alias_candidate)
                journal["phase"] = "alias-swap"
                _write_journal(paths, journal)
                if _entry_identity(paths.app) != published_app or _entry_identity(paths.cli) != published_cli or _entry_identity(paths.alias) != old_alias:
                    raise InstallError("destination changed before agent compatibility publication")
                if old_alias["present"]:
                    _revalidate_parent(cli_fd, paths.alias.parent)
                    _atomic_swap(cli_fd, alias_candidate, paths.alias)
                    published_alias = _entry_identity(paths.alias)
                    published.append((cli_fd, paths.alias, "previous-alias", True, published_alias, alias_candidate))
                    rollback_fd = os.open(rollback, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
                    try:
                        _atomic_move(cli_fd, alias_candidate, rollback_fd, rollback / "previous-alias")
                    finally:
                        os.close(rollback_fd)
                else:
                    _revalidate_parent(cli_fd, paths.alias.parent)
                    _atomic_new(cli_fd, alias_candidate, paths.alias)
                    published_alias = _entry_identity(paths.alias)
                    published.append((cli_fd, paths.alias, "previous-alias", False, published_alias, None))
            state = {"schema_version": STATE_SCHEMA_VERSION, "product": product, "mode": "signed-current", "bundle_identifier": item.bundle_identifier, "bundle_path": str(paths.app.absolute()), "executable_path": str(paths.executable.absolute()), "compatibility_path": str(paths.cli.absolute()), "generation": transaction_id, "transaction_id": transaction_id, "version": verified["version"], "bundle_sha256": _tree_sha256(paths.app), "executable_sha256": _sha256(paths.executable), "previous_compatibility": old_cli, "team_id": verified.get("team_id"), "certificate_sha1": verified.get("certificate_sha1"), "designated_requirements": verified.get("designated_requirements"), "mach_o_uuids": verified.get("mach_o_uuids"), "source_revision": source_revision, "signer_helper_sha256": verified.get("signer_helper_sha256"), "helpers": dict(helper_provenance or {})}
            state_temp = paths.state.with_name(paths.state.name + f".tmp-{transaction_id}")
            _write_private(state_temp, (json.dumps(state, sort_keys=True, indent=2) + "\n").encode())
            journal["phase"] = "state-swap"
            _write_journal(paths, journal)
            if _entry_identity(paths.app) != published_app or _entry_identity(paths.cli) != published_cli or (paths.alias is not None and _entry_identity(paths.alias) != published_alias) or _entry_identity(paths.state) != old_state:
                raise InstallError("destination changed before state publication")
            if os.path.lexists(paths.state):
                _revalidate_parent(app_fd, paths.root)
                _atomic_swap(app_fd, state_temp, paths.state)
                published.append((app_fd, paths.state, "previous-state.json", True, _entry_identity(paths.state), state_temp))
                rollback_fd = os.open(rollback, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
                try:
                    _atomic_move(app_fd, state_temp, rollback_fd, rollback / "previous-state.json")
                finally:
                    os.close(rollback_fd)
            else:
                _revalidate_parent(app_fd, paths.root)
                _atomic_new(app_fd, state_temp, paths.state)
                published.append((app_fd, paths.state, "previous-state.json", False, _entry_identity(paths.state), None))
            # Persist every mutated directory before marking the journal committed.
            with _open_dir(rollback) as rollback_fd:
                for directory_fd in (helper_fd, cli_fd, rollback_fd, app_fd):
                    _fsync_dir(directory_fd)
            journal["phase"] = "committed"
            _write_journal(paths, journal)
            _clear_journal(paths, transaction_id)
            return state
        except Exception as exc:
            rollback_errors = []
            if rollback.is_dir():
                for parent_fd, target, old_name, had_old, expected_current, fallback in reversed(published):
                    try:
                        _restore_public(parent_fd, target, rollback, old_name, had_old, transaction_id, expected_current, fallback)
                    except Exception as rollback_exc:
                        rollback_errors.append(str(rollback_exc))
            detail = str(exc)
            if rollback_errors:
                detail += "; rollback failed: " + "; ".join(rollback_errors)
            raise InstallError(detail, published=bool(published) or bool(rollback_errors)) from exc


def _helper_provenance(helper: Path, root: Path, source_revision: str) -> dict[str, Any]:
    app_install = Path(__file__).absolute()
    return {
        "macos-signing.py": {"sha256": _sha256(helper), "source_revision": source_revision},
        "macos-app-install.py": {"sha256": _sha256(app_install), "source_revision": source_revision},
    }


def _emit(value: Mapping[str, Any]) -> int:
    print(json.dumps(dict(value), sort_keys=True, indent=2))
    return 0


def _validate_lock_fd(fd: int, paths: Paths) -> None:
    try:
        actual = os.fstat(fd)
        expected = paths.lock.stat()
        if not stat.S_ISREG(actual.st_mode) or (actual.st_dev, actual.st_ino) != (expected.st_dev, expected.st_ino):
            raise InstallError("inherited fd is not the expected product lock")
        probe = os.open(paths.lock, os.O_RDWR | getattr(os, "O_NOFOLLOW", 0))
        try:
            try:
                fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                if exc.errno not in (errno.EACCES, errno.EAGAIN):
                    raise InstallError("cannot validate inherited product lock") from exc
            else:
                fcntl.flock(probe, fcntl.LOCK_UN)
                raise InstallError("inherited fd does not hold the product lock")
        finally:
            os.close(probe)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise InstallError("inherited fd does not hold the product lock") from exc
    except OSError as exc:
        raise InstallError("inherited lock fd is invalid") from exc


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    def common(name: str):
        command = commands.add_parser(name)
        command.add_argument("product", choices=sorted(PRODUCTS))
        command.add_argument("--root", required=True, type=Path)
        command.add_argument("--policy", type=Path)
        command.add_argument("--lock-fd", type=int)
        return command

    common("mode")
    common("preflight")
    common("verify-current")
    common("service-owner")
    install_parser = commands.add_parser("install")
    install_parser.add_argument("product", choices=sorted(PRODUCTS))
    install_parser.add_argument("--root", required=True, type=Path)
    install_parser.add_argument("--source", required=True, type=Path)
    install_parser.add_argument("--version", required=True)
    install_parser.add_argument("--source-revision", required=True)
    install_parser.add_argument("--helper-source-revision", required=True)
    install_parser.add_argument("--policy", type=Path)
    args = parser.parse_args(argv)
    policy = central_policy_path()
    try:
        if args.policy is not None and args.policy.absolute() != policy.absolute():
            raise InstallError("CLI signing policy must use the central machine path")
        signer = ProcessSigner()
        if args.command == "mode":
            paths = product_paths(args.product, args.root)
            if paths.root.exists():
                with _product_lock(paths):
                    mode = detect_mode(args.product, args.root, policy, signer)
            else:
                mode = "empty"
            return _emit({"schema_version": STATE_SCHEMA_VERSION, "product": args.product, "mode": mode, "policy_available": _policy_mode(policy) == "configured", "managed": _policy_mode(policy) == "configured" or paths.state.exists(), "bundle_path": str(paths.app.absolute()), "executable_path": str(paths.executable.absolute()), "compatibility_path": str(paths.cli.absolute()), "state_path": str(paths.state.absolute()), "lock_path": str(paths.lock.absolute()), "journal_path": str(paths.journal.absolute())})
        if args.command == "preflight":
            paths = product_paths(args.product, args.root)
            if args.lock_fd is not None:
                _validate_lock_fd(args.lock_fd, paths)
                result = preflight(args.product, args.root, signer, policy_path=policy, lock_held=True)
            else:
                with _product_lock(paths):
                    result = preflight(args.product, args.root, signer, policy_path=policy, lock_held=True)
            return _emit(result)
        if args.command in {"verify-current", "service-owner"}:
            paths = product_paths(args.product, args.root)
            if args.lock_fd is not None:
                _validate_lock_fd(args.lock_fd, paths)
                result = service_owner(args.product, args.root, signer, policy_path=policy, lock_held=True)
            elif args.command == "verify-current":
                with _product_lock(paths):
                    result = service_owner(args.product, args.root, signer, policy_path=policy, lock_held=True)
            else:
                raise InstallError("service-owner requires --lock-fd")
            return _emit(result)
        helper = Path(__file__).with_name("macos-signing.py")
        state = install(args.product, args.root, args.source, signer, policy_path=policy, helper_provenance=_helper_provenance(helper, args.root, args.helper_source_revision), helper_sources={"macos-signing.py": helper, "macos-app-install.py": Path(__file__).absolute()}, source_revision=args.source_revision, version=args.version)
        return _emit(state)
    except InstallError as exc:
        print(json.dumps({"schema_version": STATE_SCHEMA_VERSION, "error": str(exc), "published": bool(exc.published) if exc.published is not None else False}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
