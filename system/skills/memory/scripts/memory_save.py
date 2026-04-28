#!/usr/bin/env python3
"""Save a memory to the hex memory database.

Usage:
    python3 memory_save.py 'JWT tokens use httpOnly cookies'
    python3 memory_save.py 'content' --tags 'auth,security' --source 'middleware.ts'
"""

import argparse
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path


def _find_hex_root():
    """Walk up from script location to find hex root (has CLAUDE.md)."""
    d = Path(__file__).resolve().parent
    for _ in range(6):
        if (d / "CLAUDE.md").exists():
            return d
        d = d.parent
    return None


def _find_db():
    root = _find_hex_root()
    if root is None:
        return None
    for subdir in [".hex", ".claude"]:
        db = root / subdir / "memory.db"
        if db.exists():
            return db
    return None


DB_PATH = _find_db()


def save(content, tags="", source=""):
    """Save an explicit memory."""
    if not content or not content.strip():
        print("Error: memory content cannot be empty.", file=sys.stderr)
        sys.exit(1)

    if DB_PATH is None or not DB_PATH.exists():
        print("No memory database found. Run install.sh first.", file=sys.stderr)
        sys.exit(1)

    conn = sqlite3.connect(str(DB_PATH))
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")

    conn.execute(
        "INSERT INTO memories (content, tags, source, timestamp) VALUES (?, ?, ?, ?)",
        (content, tags, source, ts),
    )
    mem_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    total = conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]

    conn.commit()
    conn.close()

    print(f"Saved memory #{mem_id}: {content[:80]}{'...' if len(content) > 80 else ''}")
    print(f"  ({total} memories total)")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Save a memory to hex")
    parser.add_argument("content", help="Memory content")
    parser.add_argument("--tags", default="", help="Comma-separated tags")
    parser.add_argument("--source", default="", help="Source file or context")
    args = parser.parse_args()
    save(args.content, tags=args.tags, source=args.source)
