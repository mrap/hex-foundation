#!/usr/bin/env python3
"""
Spec-to-owner resolver.

Given a spec_id or spec YAML path, returns the owning agent ID.

Resolution order (most-specific wins):
  1. Spec YAML field `agent: <id>`
  2. Spec's `initiative:` field → look up in initiatives/*.yaml
  3. Path heuristic: spec_path or content mentions projects/<agent>/**/*
  4. Title keyword match: title mentions an agent name
  5. Default: boi-optimizer + weak-attribution warning
"""

import sys
import os
import re
import sqlite3
import json
import glob as glob_mod
import warnings
from pathlib import Path

_hex_dir = os.environ.get("HEX_DIR")
if not _hex_dir:
    sys.exit("ERROR: HEX_DIR environment variable required (path to hex workspace)")
HEX_ROOT = Path(_hex_dir)
INITIATIVES_DIR = HEX_ROOT / "initiatives"
PROJECTS_DIR = HEX_ROOT / "projects"
BOI_DB = Path.home() / ".boi" / "boi-rust.db"
FALLBACK_AGENT = "boi-optimizer"

def _extract_top_level_scalars(text):
    """Extract top-level key: value scalar fields from YAML text via regex.

    Only reads lines until the first multi-line block or list starts,
    so we capture the header fields (title, initiative, agent, mode, etc.)
    without tripping on embedded markdown or invalid YAML inside task specs.
    """
    result = {}
    for line in text.splitlines():
        # Stop at list items or block scalars — everything after is body
        if re.match(r'^(tasks|outcomes|context|spec|timeline|key_results|experiments)\s*:', line):
            break
        if line.startswith('- '):
            break
        # Match: key: "quoted value" or key: unquoted value
        m = re.match(r'^([a-zA-Z_][\w-]*):\s*(?:"([^"]*)"|(.*?))\s*$', line)
        if m:
            key = m.group(1)
            val = m.group(2) if m.group(2) is not None else m.group(3)
            val = val.strip().strip("'\"")
            if val and not val.startswith('#'):
                result[key] = val
    return result


try:
    import yaml as _yaml

    def load_yaml(text):
        try:
            data = _yaml.safe_load(text)
            if isinstance(data, dict):
                return data
        except Exception:
            pass
        # Fall back to regex extraction of top-level scalar fields
        return _extract_top_level_scalars(text)
except ImportError:
    def load_yaml(text):
        return _extract_top_level_scalars(text)


def warn(msg):
    print(f"[WARN] {msg}", file=sys.stderr)


def _agent_valid(agent_id):
    """Return True if agent has charter.yaml in the projects directory."""
    return (PROJECTS_DIR / agent_id / "charter.yaml").exists()


def _agent_names():
    """Return sorted list of validated agent names (those with charter.yaml)."""
    names = []
    if PROJECTS_DIR.is_dir():
        for p in PROJECTS_DIR.iterdir():
            if p.is_dir() and not p.name.startswith('_') and (p / "charter.yaml").exists():
                names.append(p.name)
    # Sort longer names first so "boi-optimizer" matches before "boi"
    names.sort(key=len, reverse=True)
    return names


def _load_initiatives():
    """Load all initiative YAMLs → {initiative_id: owner}."""
    mapping = {}
    if not INITIATIVES_DIR.is_dir():
        return mapping
    for path in INITIATIVES_DIR.glob("*.yaml"):
        try:
            data = load_yaml(path.read_text())
            if isinstance(data, dict):
                init_id = data.get("id")
                owner = data.get("owner")
                if init_id and owner:
                    mapping[init_id] = owner
        except Exception:
            pass
    return mapping


def _spec_from_db(spec_id):
    """Look up spec_path from boi-rust.db by spec ID."""
    if not BOI_DB.exists():
        return None, None
    try:
        con = sqlite3.connect(str(BOI_DB))
        cur = con.execute(
            "SELECT spec_path, title FROM specs WHERE id = ?", (spec_id,)
        )
        row = cur.fetchone()
        con.close()
        if row:
            return row[0], row[1]
    except Exception as e:
        warn(f"DB lookup failed for {spec_id}: {e}")
    return None, None


def _resolve_from_path(path_str, agent_names):
    """
    Check if a file path contains projects/<agent>/ — return agent if found.
    """
    if not path_str:
        return None
    m = re.search(r'projects/([^/]+)/', path_str)
    if m:
        candidate = m.group(1)
        if candidate in agent_names:
            return candidate
    return None


def _resolve_from_content(content, agent_names):
    """
    Search spec content for projects/<agent>/ mentions — return first match.
    """
    if not content:
        return None
    matches = re.findall(r'projects/([^/\s\'"]+)/', content)
    agent_set = set(agent_names)
    for candidate in matches:
        if candidate in agent_set:
            return candidate
    return None


def _resolve_from_title(title, agent_names):
    """
    Check if any agent name appears as a word in the title.
    Case-insensitive. Longer names match before shorter.
    """
    if not title:
        return None
    title_lower = title.lower()
    for name in agent_names:  # already sorted longest-first
        # Match whole-word or hyphen-bounded
        if re.search(r'(?<![a-z0-9-])' + re.escape(name) + r'(?![a-z0-9-])', title_lower):
            return name
    return None


def resolve(spec_id_or_path, verbose=False):
    """
    Resolve owning agent for a spec.

    Args:
        spec_id_or_path: spec ID (e.g. "SC5D5") or YAML file path
        verbose: if True, print resolution path to stderr

    Returns:
        (owner, resolution_path) tuple
    """
    agent_names = _agent_names()
    initiatives = _load_initiatives()

    spec_yaml_text = None
    spec_path_str = None
    db_title = None

    # Determine if input is a file path or a spec ID
    arg = str(spec_id_or_path).strip()
    if os.path.exists(arg):
        spec_path_str = arg
        try:
            spec_yaml_text = Path(arg).read_text()
        except Exception as e:
            warn(f"Cannot read {arg}: {e}")
    else:
        # Treat as spec ID — look up in DB
        spec_path_str, db_title = _spec_from_db(arg)
        if spec_path_str and os.path.exists(spec_path_str):
            try:
                spec_yaml_text = Path(spec_path_str).read_text()
            except Exception as e:
                warn(f"Cannot read {spec_path_str}: {e}")
        elif not spec_path_str:
            warn(f"Spec ID {arg!r} not found in DB")

    # Parse YAML
    spec_data = {}
    if spec_yaml_text:
        try:
            spec_data = load_yaml(spec_yaml_text) or {}
            if not isinstance(spec_data, dict):
                spec_data = {}
        except Exception as e:
            warn(f"YAML parse error: {e}")

    title = spec_data.get("title") or db_title or ""

    # --- Resolution step 1: explicit agent field ---
    if spec_data.get("agent"):
        owner = spec_data["agent"]
        if _agent_valid(owner):
            resolution = "explicit agent: field"
            if verbose:
                warn(f"Resolved via {resolution}: {owner}")
            return owner, resolution
        else:
            warn(f"Explicit agent {owner!r} has no charter.yaml; falling through to heuristics")

    # --- Resolution step 2: initiative field → look up owner ---
    initiative_id = spec_data.get("initiative")
    if initiative_id and initiative_id in initiatives:
        owner = initiatives[initiative_id]
        if _agent_valid(owner):
            resolution = f"initiative {initiative_id}"
            if verbose:
                warn(f"Resolved via {resolution}: {owner}")
            return owner, resolution
        else:
            warn(f"Initiative {initiative_id!r} owner {owner!r} has no charter.yaml; falling through")
    elif initiative_id:
        warn(f"Initiative {initiative_id!r} not found in {INITIATIVES_DIR}")

    # --- Resolution step 3: path heuristic ---
    # Check spec_path itself (spec lives under projects/<agent>/)
    owner = _resolve_from_path(spec_path_str, agent_names)
    if owner:
        resolution = f"path heuristic (spec_path: {spec_path_str})"
        if verbose:
            warn(f"Resolved via {resolution}: {owner}")
        return owner, resolution

    # Check workspace field (spec runs in agent's project directory)
    workspace = spec_data.get("workspace", "")
    owner = _resolve_from_path(workspace, agent_names)
    if owner:
        resolution = f"path heuristic (workspace: {workspace})"
        if verbose:
            warn(f"Resolved via {resolution}: {owner}")
        return owner, resolution

    # Note: we do NOT scan full YAML content — output paths mentioned
    # inside tasks/context ("write to projects/x/") are not ownership signals.

    # --- Resolution step 4: title keyword match ---
    owner = _resolve_from_title(title, agent_names)
    if owner:
        resolution = f"title keyword match ({title!r} → {owner})"
        if verbose:
            warn(f"Resolved via {resolution}: {owner}")
        return owner, resolution

    # --- Resolution step 5: default ---
    warn(f"weak-attribution: could not resolve owner for {arg!r} (title={title!r}); defaulting to {FALLBACK_AGENT}")
    return FALLBACK_AGENT, "default (weak-attribution)"


def run_tests():
    """Run assertions on known failed specs from 2026-04-29 queue."""
    print("Running spec-owner-resolver tests...")

    failures = []

    test_cases = [
        # (spec_id, expected_owner, description)
        # Context Routing — initiative hex-ui-context-routing not in initiatives/ → default
        ("SB735", "hex-autonomy", "Context Routing Heuristics → hex-autonomy (default)"),
        ("SFE27", "hex-autonomy", "Context Routing Heuristics dup → hex-autonomy"),
        ("S19EB", "hex-autonomy", "Context Routing Heuristics dup2 → hex-autonomy"),
        # Brand-related specs — initiative init-brand-distribution → brand
        ("SCF5E", "brand", "Synthesize Brand Lab Proposals → brand"),
        ("sadd5", "brand", "Synthesize Brand Lab Proposals dup → brand"),
        ("S049F", "brand", "Mirofish business opportunity → brand"),
        ("SA5B6", "brand", "Profile Rebrand → brand"),
        ("S4B32", "brand", "Deep Personality Research → brand"),
        # A-16 Research — spec_path contains projects/hex-autonomy/
        ("S51A1", "hex-autonomy", "A-16 Wake Prompt Research → hex-autonomy (path)"),
    ]

    for spec_id, expected, description in test_cases:
        owner, resolution = resolve(spec_id, verbose=False)
        status = "PASS" if owner == expected else "FAIL"
        if owner != expected:
            failures.append((spec_id, expected, owner, resolution, description))
        print(f"  [{status}] {spec_id}: {description}")
        if status == "FAIL":
            print(f"         expected={expected!r}, got={owner!r}, via={resolution!r}")

    # Also test with a direct YAML path
    yaml_path = str(HEX_ROOT / "specs" / "synthesize-brand-proposals.yaml")
    if os.path.exists(yaml_path):
        owner, resolution = resolve(yaml_path)
        expected = "brand"
        status = "PASS" if owner == expected else "FAIL"
        if owner != expected:
            failures.append(("yaml-path", expected, owner, resolution, "YAML path → brand"))
        print(f"  [{status}] YAML path test: synthesize-brand-proposals.yaml → brand")
        if status == "FAIL":
            print(f"         expected={expected!r}, got={owner!r}, via={resolution!r}")

    print()
    total = len(test_cases) + 1
    passed = total - len(failures)
    if failures:
        print(f"FAIL: {len(failures)}/{total} assertions failed")
        return 1
    else:
        print(f"PASS: {passed}/{total} assertions passed")
        return 0


def main():
    if "--test" in sys.argv:
        sys.exit(run_tests())

    verbose = "--verbose" in sys.argv or "-v" in sys.argv
    args = [a for a in sys.argv[1:] if not a.startswith("-")]

    if not args:
        print("Usage: spec-owner-resolver.py <spec_id_or_yaml_path> [--verbose]")
        print("       spec-owner-resolver.py --test")
        sys.exit(1)

    owner, resolution = resolve(args[0], verbose=verbose)
    print(owner)

    if verbose:
        print(f"# Resolution: {resolution}", file=sys.stderr)


if __name__ == "__main__":
    main()
