#!/usr/bin/env python3
"""Detect three-strike failure patterns in the BOI queue.

Queries the BOI SQLite DB for failures in the configured window.
A pattern fires when:
  - The same error kind appears 3+ times, OR
  - The same spec title (dash-normalized) appears 3+ times as failed

Outputs JSON to stdout if a pattern is found, nothing if no pattern.

Usage:
  python3 detect-failure-pattern.py [--window SECONDS] [spec_id]
    --window   lookback window in seconds (default: 86400)
    spec_id    optional — if given, only triggers if it's part of a new pattern
"""
import argparse
import json
import re
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path

BOI_DB = Path.home() / ".boi" / "boi-rust.db"
THRESHOLD = 3
DEFAULT_WINDOW_SECONDS = 86400


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Detect three-strike failure patterns")
    parser.add_argument(
        '--window', type=int, default=DEFAULT_WINDOW_SECONDS,
        help='Lookback window in seconds (default: 86400)'
    )
    parser.add_argument(
        'spec_id', nargs='?', default=None,
        help='Optional spec ID to scope the pattern check'
    )
    return parser.parse_args(argv)


def normalize_title(title):
    """Map em-dash/en-dash to hyphen and collapse surrounding whitespace."""
    if not title:
        return title
    # U+2014 em-dash, U+2013 en-dash → plain hyphen
    normalized = re.sub(r'[–—]', '-', title)
    # Normalize whitespace around hyphens and collapse runs
    normalized = re.sub(r'\s*-\s*', ' - ', normalized)
    normalized = re.sub(r'\s+', ' ', normalized).strip()
    return normalized


def db_connect():
    con = sqlite3.connect(str(BOI_DB))
    con.row_factory = sqlite3.Row
    return con


def get_recent_failures(con, window_seconds=DEFAULT_WINDOW_SECONDS):
    cutoff = (datetime.now(timezone.utc) - timedelta(seconds=window_seconds)).isoformat()
    rows = con.execute(
        """SELECT s.id, s.title, s.completed_at,
                  s.error
           FROM specs s
           WHERE s.status = 'failed'
             AND s.completed_at >= ?
           ORDER BY s.completed_at DESC""",
        (cutoff,),
    ).fetchall()
    return [dict(r) for r in rows]


def parse_kind(error_raw):
    if not error_raw:
        return "Unknown"
    try:
        fr = json.loads(error_raw)
        return fr.get("kind", "Unknown")
    except (json.JSONDecodeError, TypeError):
        return str(error_raw)


def detect_patterns(failures, trigger_spec_id=None):
    """Return list of pattern dicts, or empty list if nothing crosses threshold."""
    by_kind = defaultdict(list)
    by_title = defaultdict(list)

    for f in failures:
        kind = parse_kind(f.get("error"))
        by_kind[kind].append(f["id"])
        if f.get("title"):
            norm = normalize_title(f["title"])
            by_title[norm].append(f["id"])

    window_hours = DEFAULT_WINDOW_SECONDS // 3600
    patterns = []

    for kind, spec_ids in by_kind.items():
        if len(spec_ids) >= THRESHOLD:
            if trigger_spec_id and trigger_spec_id not in spec_ids:
                continue
            patterns.append({
                "pattern_type": "failure_kind",
                "key": kind,
                "description": f"Failure kind '{kind}' fired {len(spec_ids)}x in last {window_hours}h",
                "occurrences": spec_ids,
                "count": len(spec_ids),
                "recommended_owner": "boi-optimizer",
            })

    for title, spec_ids in by_title.items():
        if len(spec_ids) >= THRESHOLD:
            if trigger_spec_id and trigger_spec_id not in spec_ids:
                continue
            patterns.append({
                "pattern_type": "spec_title",
                "key": title,
                "description": f"Spec title '{title}' failed {len(spec_ids)}x in last {window_hours}h",
                "occurrences": spec_ids,
                "count": len(spec_ids),
                "recommended_owner": "boi-optimizer",
            })

    return patterns


def main():
    args = parse_args()

    if not BOI_DB.exists():
        print(f"[detect-failure-pattern] DB not found: {BOI_DB}", file=sys.stderr)
        sys.exit(0)

    con = db_connect()
    failures = get_recent_failures(con, window_seconds=args.window)
    con.close()

    patterns = detect_patterns(failures, args.spec_id)

    if not patterns:
        sys.exit(0)

    for p in patterns:
        print(json.dumps(p))


if __name__ == "__main__":
    main()
