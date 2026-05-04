#!/usr/bin/env python3
"""
Build a structured failure brief for a failed BOI spec.

Usage: build-failure-brief.py <spec_id>

Output: Markdown brief to stdout.
"""

import glob
import importlib.util
import json
import sqlite3
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

BOI_DB = Path.home() / ".boi" / "boi-rust.db"
BOI_LOGS = Path.home() / ".boi" / "logs"
HEX_SCRIPTS = Path("/Users/mrap/mrap-hex/.hex/scripts")

FAILURE_FIX_HINTS = {
    "ProviderRateLimit": "retry with longer backoff",
    "VerifyFailed": "loosen or fix the verify command",
    "Timeout": "split tasks or bump timeout",
    "ToolError": "remove tool dependency",
    "WorkerCrash": "investigate worker logs; likely env",
}


def _db_connect():
    con = sqlite3.connect(str(BOI_DB))
    con.row_factory = sqlite3.Row
    return con


def get_spec_data(spec_id):
    con = _db_connect()
    row = con.execute(
        "SELECT id, title, status, error, queued_at, completed_at, iteration, spec_path "
        "FROM specs WHERE id = ?",
        (spec_id,),
    ).fetchone()
    con.close()
    return dict(row) if row else None


def get_failure_event(spec_id):
    con = _db_connect()
    row = con.execute(
        "SELECT message, failure_reason, timestamp FROM events "
        "WHERE spec_id = ? AND event_type = 'boi.spec.failed' ORDER BY seq DESC LIMIT 1",
        (spec_id,),
    ).fetchone()
    con.close()
    return dict(row) if row else None


def get_total_cost(spec_id):
    con = sqlite3.connect(str(BOI_DB))
    row = con.execute(
        "SELECT SUM(cost_usd) FROM phase_runs WHERE spec_id = ?", (spec_id,)
    ).fetchone()
    con.close()
    return row[0] or 0.0


def get_log_tail(spec_id, lines=30):
    """Return last N lines of spec worker logs, or a note if none found."""
    # Future convention: logs named <spec_id>-*.log
    pattern = str(BOI_LOGS / f"{spec_id}-*.log")
    log_files = sorted(glob.glob(pattern))
    # Also try lowercase
    if not log_files:
        pattern_lower = str(BOI_LOGS / f"{spec_id.lower()}-*.log")
        log_files = sorted(glob.glob(pattern_lower))
    if not log_files:
        return "(no per-spec log file found — check daemon logs)"
    log_path = log_files[-1]
    try:
        text = Path(log_path).read_text(errors="replace")
        tail = text.splitlines()[-lines:]
        return "\n".join(tail)
    except Exception as e:
        return f"(error reading {log_path}: {e})"


def parse_failure_reason(event):
    """
    Return (kind, detail, severity) from the failure event.

    Prefers the typed SA015 failure_reason JSON field.
    Falls back to heuristic classification of the message text.
    """
    if event and event.get("failure_reason"):
        fr = event["failure_reason"]
        try:
            data = json.loads(fr)
            return (
                data.get("kind", "Unknown"),
                data.get("detail", ""),
                data.get("severity", "error"),
            )
        except Exception:
            return fr, "", "error"

    msg = (event or {}).get("message", "")
    if not msg:
        return "Unknown", "no failure event found in database", "error"

    ml = msg.lower()
    if "rate limit" in ml or "ratelimit" in ml or "429" in ml or "too many requests" in ml:
        return "ProviderRateLimit", msg, "warn"
    if "plan-critique" in ml or "verify" in ml:
        return "VerifyFailed", msg, "block"
    if "timeout" in ml or "timed out" in ml:
        return "Timeout", msg, "block"
    if "tool" in ml and ("error" in ml or "fail" in ml):
        return "ToolError", msg, "error"
    if "crash" in ml or "panic" in ml or "signal" in ml or "deadlock" in ml:
        return "WorkerCrash", msg, "block"
    return "Unknown", msg, "error"


def get_spec_yaml(spec_path):
    if not spec_path:
        return "(spec_path not recorded in database)"
    try:
        return Path(spec_path).read_text()
    except Exception as e:
        return f"(error reading {spec_path}: {e})"


def resolve_owner(spec_id):
    """Call spec-owner-resolver.py; return (owner, resolution_path)."""
    script = HEX_SCRIPTS / "spec-owner-resolver.py"
    try:
        result = subprocess.run(
            ["python3", str(script), "--verbose", spec_id],
            capture_output=True,
            text=True,
            timeout=15,
        )
        owner = result.stdout.strip()
        resolution = "unknown"
        for line in result.stderr.splitlines():
            if "Resolved via" in line:
                # "[WARN] Resolved via <resolution>: <owner>"
                after = line.split("Resolved via", 1)[-1].strip()
                # Strip trailing ": <owner>" if present
                if ":" in after:
                    resolution = after.rsplit(":", 1)[0].strip()
                else:
                    resolution = after
            elif "weak-attribution" in line:
                resolution = "default (weak-attribution)"
        return owner or "hex-autonomy", resolution
    except Exception as e:
        return "hex-autonomy", f"resolver error: {e}"


def build_brief(spec_id):
    spec = get_spec_data(spec_id)
    if not spec:
        print(f"ERROR: spec {spec_id!r} not found in database", file=sys.stderr)
        sys.exit(1)

    event = get_failure_event(spec_id)
    cost = get_total_cost(spec_id)
    kind, detail, _severity = parse_failure_reason(event)
    hint = FAILURE_FIX_HINTS.get(kind, "investigate the failure detail above")
    owner, resolution = resolve_owner(spec_id)
    spec_yaml = get_spec_yaml(spec.get("spec_path"))
    log_tail = get_log_tail(spec_id)

    failed_at = (event or {}).get("timestamp") or spec.get("completed_at") or "unknown"
    iterations = spec.get("iteration") or 0
    cost_str = f"{cost:.4f}" if cost else "0.0000"
    title = spec.get("title") or spec_id

    brief = f"""## Failure Brief — {spec_id}: {title}

**Reason:** {kind}
**Detail:** {detail}
**When:** {failed_at}, after {iterations} iterations
**Cost:** ${cost_str}
**Owner:** {owner} (resolution: {resolution})

## Spec
```yaml
{spec_yaml}
```

## Last 30 lines of worker log
```
{log_tail}
```

## Suggested actions (owner picks one)
1. **Revive:** dispatch a fixed version with adjustments.
   Hint: {hint}
2. **Redirect:** abandon this approach, write a different spec.
3. **Abandon:** close as won't-fix. Requires a one-line reason.
"""
    return brief


def _load_normalize_title():
    """Import normalize_title from detect-failure-pattern.py (avoid duplicating logic)."""
    path = HEX_SCRIPTS / "detect-failure-pattern.py"
    spec = importlib.util.spec_from_file_location("detect_failure_pattern", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.normalize_title


def _build_group_brief(failure_signature, specs):
    """Build one consolidated brief for a group of specs sharing a failure_signature."""
    spec_ids = [s["id"] for s in specs]
    ids_str = ", ".join(spec_ids)
    hint = FAILURE_FIX_HINTS.get(failure_signature, "investigate the failure detail above")

    lines = [f"## Consolidated Failure Brief — {failure_signature} ({len(specs)} spec(s))"]
    lines.append(f"\n**Specs affected:** {ids_str}")
    lines.append(f"**Failure signature:** {failure_signature}")
    lines.append(f"**Hint:** {hint}")
    lines.append("\n### Affected Specs")
    for s in specs:
        title = s.get("title") or s["id"]
        lines.append(f"- {s['id']}: {title}")
    lines.append("\n## Suggested actions (owner picks one)")
    lines.append(f"1. **Revive:** dispatch fixed versions with adjustments.\n   Hint: {hint}")
    lines.append("2. **Redirect:** abandon this approach, write different specs.")
    lines.append("3. **Abandon:** close as won't-fix. Requires a one-line reason per spec.")
    return "\n".join(lines)


def build_consolidated_briefs(spec_data_list):
    """Group specs by failure_signature and return one brief string per group.

    Same failure_signature (dash-normalized) → single brief listing all spec_ids.
    Degenerate case of 1 spec still returns a list with 1 brief.
    """
    normalize = _load_normalize_title()
    groups = defaultdict(list)
    for spec in spec_data_list:
        sig = normalize(spec.get("failure_signature") or "Unknown")
        groups[sig].append(spec)
    return [_build_group_brief(sig, specs) for sig, specs in groups.items()]


def run_self_test():
    try:
        con = _db_connect()
        row = con.execute(
            "SELECT id FROM specs WHERE status='FAILED' LIMIT 1"
        ).fetchone()
        con.close()
        if row:
            spec_id = row[0]
            build_brief(spec_id)
            print(f"PASS: brief built for {spec_id}")
        else:
            print("PASS: no failed specs (nothing to test)")
        sys.exit(0)
    except Exception as e:
        print(f"FAIL: {e}")
        sys.exit(1)


def main():
    if len(sys.argv) >= 2 and sys.argv[1] == "--test":
        run_self_test()
    if len(sys.argv) < 2 or not sys.argv[1].strip():
        print("Usage: build-failure-brief.py <spec_id>", file=sys.stderr)
        sys.exit(1)
    print(build_brief(sys.argv[1].strip()))


if __name__ == "__main__":
    main()
