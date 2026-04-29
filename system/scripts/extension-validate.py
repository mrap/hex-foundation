#!/usr/bin/env python3
"""Validate a hex extension manifest (extension.yaml)."""

import sys
import os
import re
import glob as glob_module

try:
    import yaml
except ImportError:
    # Fallback: minimal YAML parser for simple key:value structures
    yaml = None

REQUIRED_FIELDS = ["name", "version", "description", "type"]
VALID_TYPES = {"static", "reactive", "full"}
VALID_CAPABILITIES = {"events", "messaging", "assets", "storage", "commands"}
VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")
ENGINE_SPEC_RE = re.compile(r"^([><=!]=?)\s*(\d+\.\d+\.\d+)$")


def parse_version(v):
    parts = v.strip().split(".")
    return tuple(int(x) for x in parts)


def version_satisfies(actual, spec_str):
    """Check if actual version satisfies a spec like '>=0.8.0'."""
    m = ENGINE_SPEC_RE.match(spec_str.strip())
    if not m:
        return False, f"Cannot parse version spec '{spec_str}'"
    op, req = m.group(1), m.group(2)
    a, r = parse_version(actual), parse_version(req)
    ops = {">=": a >= r, ">": a > r, "<=": a <= r, "<": a < r, "==": a == r, "!=": a != r}
    if op not in ops:
        return False, f"Unknown operator '{op}'"
    return ops[op], None


def load_yaml(path):
    with open(path) as f:
        content = f.read()
    if yaml:
        return yaml.safe_load(content)
    # Minimal fallback: only handles flat key: value (not needed for validation flow,
    # but avoids hard crash when PyYAML is absent — real validation needs yaml).
    raise RuntimeError("PyYAML is required: pip install pyyaml")


def get_hex_version():
    candidates = [
        os.path.join(os.path.dirname(__file__), "..", "..", "version.txt"),
        os.path.join(os.path.dirname(__file__), "..", "version.txt"),
    ]
    for c in candidates:
        p = os.path.normpath(c)
        if os.path.isfile(p):
            return open(p).read().strip()
    return "0.8.0"


def collect_all_extension_dirs(ext_dir):
    """Return list of extension directories adjacent to the one being validated."""
    if not os.path.isdir(ext_dir):
        return []
    return [
        d for d in os.listdir(ext_dir)
        if os.path.isdir(os.path.join(ext_dir, d)) and not d.startswith(".")
    ]


def validate(manifest_path, check_siblings=True):
    errors = []
    warnings = []
    info = []

    if not os.path.isfile(manifest_path):
        return [f"File not found: {manifest_path}"], [], []

    ext_dir = os.path.dirname(os.path.abspath(manifest_path))
    ext_name_from_dir = os.path.basename(ext_dir)

    try:
        manifest = load_yaml(manifest_path)
    except Exception as e:
        return [f"YAML parse error: {e}"], [], []

    if not isinstance(manifest, dict):
        return ["Manifest must be a YAML mapping"], [], []

    # Required fields
    for field in REQUIRED_FIELDS:
        if field not in manifest:
            errors.append(f"Missing required field: '{field}'")

    # name
    name = manifest.get("name", "")
    if name and name != ext_name_from_dir:
        warnings.append(
            f"name '{name}' does not match directory name '{ext_name_from_dir}'"
        )

    # version
    version = manifest.get("version", "")
    if version and not VERSION_RE.match(str(version)):
        errors.append(f"version must be semver (e.g. '1.0.0'), got: '{version}'")

    # type
    ext_type = manifest.get("type", "")
    if ext_type and ext_type not in VALID_TYPES:
        errors.append(f"type must be one of {sorted(VALID_TYPES)}, got: '{ext_type}'")

    # engines.hex
    engines = manifest.get("engines", {})
    if isinstance(engines, dict) and "hex" in engines:
        hex_version = get_hex_version()
        ok, err = version_satisfies(hex_version, engines["hex"])
        if not ok:
            errors.append(
                f"engines.hex '{engines['hex']}' not satisfied by current hex {hex_version}: {err}"
            )
        else:
            info.append(f"engines.hex '{engines['hex']}' satisfied by hex {hex_version}")

    # views — check entry files exist
    for view in manifest.get("views", []):
        if not isinstance(view, dict):
            errors.append("Each view must be a mapping")
            continue
        for required in ("name", "path", "entry"):
            if required not in view:
                errors.append(f"View missing '{required}': {view}")
        entry = view.get("entry", "")
        if entry:
            full = os.path.join(ext_dir, entry)
            if not os.path.isfile(full):
                warnings.append(f"View entry not found: {entry}")

    # commands — check entry files exist
    for cmd in manifest.get("commands", []):
        if not isinstance(cmd, dict):
            errors.append("Each command must be a mapping")
            continue
        entry = cmd.get("entry", "")
        if entry:
            full = os.path.join(ext_dir, entry)
            if not os.path.isfile(full) and not os.path.isfile(full + ".sh") \
                    and not os.path.isfile(full + ".py"):
                warnings.append(f"Command entry not found: {entry}")

    # policies — check files exist
    for pol in manifest.get("policies", []):
        full = os.path.join(ext_dir, pol)
        if not os.path.isfile(full):
            warnings.append(f"Policy file not found: {pol}")

    # skills — check files exist
    for skill in manifest.get("skills", []):
        full = os.path.join(ext_dir, skill)
        if not os.path.isfile(full):
            warnings.append(f"Skill file not found: {skill}")

    # tables — check migration files exist
    for migration in manifest.get("tables", []):
        full = os.path.join(ext_dir, migration)
        if not os.path.isfile(full):
            warnings.append(f"Migration file not found: {migration}")

    # server (full-tier)
    server = manifest.get("server", {})
    if ext_type == "full" and not server:
        warnings.append("type=full but no 'server' block defined")
    if server and isinstance(server, dict):
        if "command" not in server:
            errors.append("server block must include 'command'")

    # requires.capabilities
    requires = manifest.get("requires", {})
    if isinstance(requires, dict):
        caps = requires.get("capabilities", [])
        for cap in caps:
            if cap not in VALID_CAPABILITIES:
                warnings.append(
                    f"Unknown capability '{cap}' (known: {sorted(VALID_CAPABILITIES)})"
                )

    # Sibling conflict check (duplicate view paths and SSE topics)
    if check_siblings and name:
        parent_dir = os.path.dirname(ext_dir)
        siblings = collect_all_extension_dirs(parent_dir)
        my_paths = {v.get("path") for v in manifest.get("views", []) if isinstance(v, dict)}
        my_topics = set(manifest.get("sse_topics", []))

        for sibling in siblings:
            if sibling == ext_name_from_dir:
                continue
            sibling_manifest_path = os.path.join(parent_dir, sibling, "extension.yaml")
            if not os.path.isfile(sibling_manifest_path):
                continue
            try:
                sibling_manifest = load_yaml(sibling_manifest_path)
            except Exception:
                continue
            for sv in sibling_manifest.get("views", []):
                if not isinstance(sv, dict):
                    continue
                sp = sv.get("path")
                if sp and sp in my_paths:
                    errors.append(
                        f"View path '{sp}' conflicts with extension '{sibling}'"
                    )
            for st in sibling_manifest.get("sse_topics", []):
                if st in my_topics:
                    warnings.append(
                        f"SSE topic '{st}' also declared by extension '{sibling}'"
                    )

    return errors, warnings, info


def main():
    args = sys.argv[1:]

    # Support being called as: extension-validate.py <path>
    # or via: hex extension validate <path>
    if not args:
        print("Usage: extension-validate.py <path-to-extension-dir-or-manifest>")
        sys.exit(1)

    path = args[0]

    # Accept either the directory or the manifest file directly
    if os.path.isdir(path):
        manifest_path = os.path.join(path, "extension.yaml")
    else:
        manifest_path = path

    ext_label = os.path.basename(os.path.dirname(os.path.abspath(manifest_path)))
    print(f"Validating extension: {ext_label}")
    print(f"Manifest:  {manifest_path}")
    print()

    errors, warnings, info = validate(manifest_path)

    for line in info:
        print(f"  ✓  {line}")
    for line in warnings:
        print(f"  ⚠  {line}")
    for line in errors:
        print(f"  ✗  {line}")

    if not errors and not warnings and not info:
        print("  ✓  All checks passed")

    print()
    if errors:
        print(f"Result: INVALID ({len(errors)} error(s), {len(warnings)} warning(s))")
        sys.exit(1)
    elif warnings:
        print(f"Result: VALID with {len(warnings)} warning(s)")
    else:
        print("Result: VALID")


if __name__ == "__main__":
    main()
