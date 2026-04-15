#!/usr/bin/env python3
"""Index workspace files into hex memory for full-text search.

Chunks markdown files by heading. Incremental by default — skips unchanged files
using mtime pre-filter + SHA-256 content hash.

Usage:
    python3 memory_index.py              # Incremental index
    python3 memory_index.py --full       # Full rebuild
    python3 memory_index.py --stats      # Show index stats
"""

import argparse
import hashlib
import os
import re
import sqlite3
import sys
import time
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


HEX_ROOT = _find_hex_root()
DB_PATH = HEX_ROOT / ".hex" / "memory.db" if HEX_ROOT else None

INDEX_DIRS = [".", "me", "projects", "people", "evolution", "landings", "raw"]
SKIP_DIRS = {".git", ".hex", ".claude", "node_modules", "__pycache__", ".venv"}
INDEX_EXTENSIONS = {".md", ".txt"}
MAX_CHUNK_WORDS = 400


def content_hash(text):
    """SHA-256 hex digest (first 16 chars)."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def should_skip(path, root):
    """Check if a path should be skipped."""
    try:
        parts = path.relative_to(root).parts
    except ValueError:
        return True
    return any(part in SKIP_DIRS for part in parts)


def chunk_by_heading(text, filepath):
    """Split markdown into chunks by heading.

    Returns list of (heading, content) tuples. Chunks exceeding MAX_CHUNK_WORDS
    are split into parts.
    """
    chunks = []
    current_heading = os.path.basename(filepath)
    current_lines = []

    for line in text.split("\n"):
        if re.match(r"^#{1,4}\s+", line):
            if current_lines:
                body = "\n".join(current_lines).strip()
                if body:
                    chunks.append((current_heading, body))
            current_heading = line.lstrip("#").strip()
            current_lines = [line]
        else:
            current_lines.append(line)

    if current_lines:
        body = "\n".join(current_lines).strip()
        if body:
            chunks.append((current_heading, body))

    # Split oversized chunks
    result = []
    for heading, body in chunks:
        words = body.split()
        if len(words) <= MAX_CHUNK_WORDS:
            result.append((heading, body))
        else:
            i = 0
            part = 0
            while i < len(words):
                end = min(i + MAX_CHUNK_WORDS, len(words))
                sub = " ".join(words[i:end])
                label = f"{heading} (part {part + 1})" if part > 0 else heading
                result.append((label, sub))
                part += 1
                i = end

    return result


def init_db(conn):
    """Create tables if they don't exist."""
    conn.executescript("""
        CREATE VIRTUAL TABLE IF NOT EXISTS chunks USING fts5(
            source_path, heading, chunk_index, content,
            tokenize='unicode61'
        );
        CREATE TABLE IF NOT EXISTS files (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            path TEXT UNIQUE NOT NULL,
            mtime REAL NOT NULL,
            content_hash TEXT NOT NULL DEFAULT '',
            indexed_at TEXT NOT NULL,
            chunk_count INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS metadata (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
    """)
    conn.commit()


def get_indexable_files(root):
    """Find all indexable markdown/text files under root."""
    files = []
    for index_dir in INDEX_DIRS:
        dir_path = root / index_dir
        if not dir_path.exists():
            continue
        if index_dir == ".":
            for f in dir_path.iterdir():
                if f.is_file() and f.suffix in INDEX_EXTENSIONS and not should_skip(f, root):
                    files.append(f)
        else:
            for f in dir_path.rglob("*"):
                if f.is_file() and f.suffix in INDEX_EXTENSIONS and not should_skip(f, root):
                    files.append(f)
    return files


def index(full=False):
    """Index workspace files into the chunks FTS5 table."""
    if DB_PATH is None or not DB_PATH.exists():
        print("ERROR: No memory database. Run install.sh first.", file=sys.stderr)
        sys.exit(1)

    conn = sqlite3.connect(str(DB_PATH))
    init_db(conn)

    if full:
        conn.execute("DELETE FROM chunks")
        conn.execute("DELETE FROM files")
        conn.commit()
        print("Full rebuild: cleared index")

    existing = {}
    for row in conn.execute("SELECT path, mtime, content_hash FROM files").fetchall():
        existing[row[0]] = (row[1], row[2])

    files = get_indexable_files(HEX_ROOT)
    indexed = 0
    skipped = 0
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")

    for filepath in files:
        rel_path = str(filepath.relative_to(HEX_ROOT))
        mtime = filepath.stat().st_mtime

        # Fast path: skip if mtime unchanged
        if rel_path in existing and existing[rel_path][0] == mtime:
            skipped += 1
            continue

        try:
            text = filepath.read_text(encoding="utf-8", errors="ignore")
        except (OSError, PermissionError):
            continue

        # Skip if content unchanged (hash check)
        chash = content_hash(text)
        if rel_path in existing and existing[rel_path][1] == chash:
            conn.execute("UPDATE files SET mtime = ? WHERE path = ?", (mtime, rel_path))
            skipped += 1
            continue

        # Remove old chunks for this file
        conn.execute("DELETE FROM chunks WHERE source_path = ?", (rel_path,))

        # Chunk and index
        chunks = chunk_by_heading(text, rel_path)
        for i, (heading, body) in enumerate(chunks):
            conn.execute(
                "INSERT INTO chunks (source_path, heading, chunk_index, content) VALUES (?, ?, ?, ?)",
                (rel_path, heading, str(i), body),
            )

        conn.execute(
            "INSERT OR REPLACE INTO files (path, mtime, content_hash, indexed_at, chunk_count) VALUES (?, ?, ?, ?, ?)",
            (rel_path, mtime, chash, ts, len(chunks)),
        )
        indexed += 1

    # Clean up deleted files
    current_paths = {str(f.relative_to(HEX_ROOT)) for f in files}
    for path in list(existing.keys()):
        if path not in current_paths:
            conn.execute("DELETE FROM chunks WHERE source_path = ?", (path,))
            conn.execute("DELETE FROM files WHERE path = ?", (path,))

    conn.commit()
    conn.close()
    print(f"Indexed {indexed} files ({skipped} unchanged, {len(files)} total)")


def stats():
    """Show index statistics."""
    if DB_PATH is None or not DB_PATH.exists():
        print("No memory database found.", file=sys.stderr)
        sys.exit(1)

    conn = sqlite3.connect(str(DB_PATH))
    file_count = conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
    chunk_count = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]

    has_memories = bool(conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='memories'"
    ).fetchone())
    memory_count = conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0] if has_memories else 0

    db_size = DB_PATH.stat().st_size / 1024
    conn.close()

    print(f"Files indexed: {file_count}")
    print(f"Chunks: {chunk_count}")
    print(f"Explicit memories: {memory_count}")
    print(f"Database: {DB_PATH}")
    print(f"Size: {db_size:.1f} KB")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Index hex workspace files")
    parser.add_argument("--full", action="store_true", help="Full rebuild")
    parser.add_argument("--stats", action="store_true", help="Show stats")
    args = parser.parse_args()
    if args.stats:
        stats()
    else:
        index(full=args.full)
