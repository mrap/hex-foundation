#!/usr/bin/env python3
"""Search hex memory.

Searches both explicit memories (saved via memory_save.py) and indexed file
chunks (created by memory_index.py). Uses three-tier strategy:
  1. Exact FTS5 phrase match
  2. Prefix expansion (auth → auth*)
  3. LIKE substring fallback

Usage:
    python3 memory_search.py "query terms"
    python3 memory_search.py --top 5 "exact phrase"
    python3 memory_search.py --file people "name"
    python3 memory_search.py --compact "keyword"
"""

import argparse
import re
import sqlite3
import sys
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
    """Find memory.db in .hex/ or .claude/ (backwards compat)."""
    root = _find_hex_root()
    if root is None:
        return None
    for subdir in [".hex", ".claude"]:
        db = root / subdir / "memory.db"
        if db.exists():
            return db
    return None


DB_PATH = _find_db()


def _table_exists(conn, name):
    """Check if a table or virtual table exists."""
    return bool(conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type IN ('table','view') AND name = ?", (name,)
    ).fetchone())


def _fts_query(conn, table, fts_query, top, file_filter=None):
    """Run FTS5 MATCH query. Returns list of result tuples."""
    try:
        if table == "memories_fts":
            sql = """
                SELECT m.id, m.content, m.tags, m.source, m.created_at,
                       bm25(memories_fts) AS score
                FROM memories_fts
                JOIN memories m ON m.id = memories_fts.rowid
                WHERE memories_fts MATCH ?
            """
            params = [fts_query]
            if file_filter:
                sql += " AND m.source LIKE ?"
                params.append(f"%{file_filter}%")
            sql += " ORDER BY score LIMIT ?"
            params.append(top)
        else:
            sql = """
                SELECT source_path, heading, chunk_index, content,
                       bm25(chunks) AS score
                FROM chunks WHERE chunks MATCH ?
            """
            params = [fts_query]
            if file_filter:
                sql += " AND source_path LIKE ?"
                params.append(f"%{file_filter}%")
            sql += " ORDER BY score LIMIT ?"
            params.append(top)
        return conn.execute(sql, params).fetchall()
    except sqlite3.OperationalError:
        return []


def _prefix_query(query):
    """Convert 'auth middleware' → 'auth* middleware*' for prefix matching."""
    if any(op in query for op in ['"', '*', 'OR', 'AND', 'NOT', 'NEAR']):
        return None
    words = query.strip().split()
    return " ".join(w + "*" for w in words) if words else None


def _like_search(conn, query, top, file_filter=None):
    """LIKE fallback when FTS5 returns nothing."""
    pattern = f"%{query}%"
    results = []

    try:
        sql = "SELECT id, content, tags, source, created_at, 0 FROM memories WHERE content LIKE ?"
        params = [pattern]
        if file_filter:
            sql += " AND source LIKE ?"
            params.append(f"%{file_filter}%")
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(top)
        for row in conn.execute(sql, params).fetchall():
            results.append(("memory", row))
    except sqlite3.OperationalError:
        pass

    try:
        sql = "SELECT source_path, heading, chunk_index, content, 0 FROM chunks WHERE content LIKE ?"
        params = [pattern]
        if file_filter:
            sql += " AND source_path LIKE ?"
            params.append(f"%{file_filter}%")
        sql += " LIMIT ?"
        params.append(top)
        for row in conn.execute(sql, params).fetchall():
            results.append(("chunk", row))
    except sqlite3.OperationalError:
        pass

    return results


def _format_memory(row, compact, ctx):
    _id, content, tags, source, ts, score = row
    if compact:
        tag_str = f" [{tags}]" if tags else ""
        return f"[mem:{_id}]{tag_str} {content[:ctx].replace(chr(10), ' ')}"
    lines = [f"[memory #{_id}]  {source}  {ts}"]
    if tags:
        lines.append(f"  tags: {tags}")
    lines.append(f"  {content[:ctx * 3]}")
    return "\n".join(lines)


def _format_chunk(row, compact, ctx):
    src, heading, idx, content, score = row
    if compact:
        return f"[{src}:{heading}] {content[:ctx].replace(chr(10), ' ')}"
    return f"[{src}] ## {heading}\n  {content[:ctx * 3]}"


def search(query, top=10, file_filter=None, compact=False, context_chars=300):
    """Search hex memory across memories and chunks."""
    if DB_PATH is None:
        print("No memory database found. Run install.sh first.", file=sys.stderr)
        sys.exit(1)

    conn = sqlite3.connect(str(DB_PATH))
    has_memories = _table_exists(conn, "memories_fts")
    has_chunks = _table_exists(conn, "chunks")

    # Sanitize and build query variants
    sanitized = re.sub(r'["\*\(\)\{\}\^\~]', ' ', query.strip())
    terms = [t for t in sanitized.split() if t]
    if not terms:
        print("Empty query.", file=sys.stderr)
        conn.close()
        sys.exit(1)

    fts_queries = []
    if len(terms) > 1:
        fts_queries.append('"' + ' '.join(terms) + '"')          # phrase
        fts_queries.append(' '.join(f'"{t}"' for t in terms))    # AND
    else:
        fts_queries.append(f'"{terms[0]}"')

    prefix_q = _prefix_query(query)
    if prefix_q:
        fts_queries.append(prefix_q)

    # Try FTS5 queries in order
    memory_results = []
    chunk_results = []
    for fq in fts_queries:
        if has_memories and not memory_results:
            memory_results = _fts_query(conn, "memories_fts", fq, top, file_filter)
        if has_chunks and not chunk_results:
            chunk_results = _fts_query(conn, "chunks", fq, top, file_filter)
        if memory_results or chunk_results:
            break

    # LIKE fallback
    like_results = []
    if not memory_results and not chunk_results:
        like_results = _like_search(conn, query, top, file_filter)

    conn.close()

    # Format output
    output_lines = []
    count = 0

    for row in memory_results[:top]:
        if count > 0 and not compact:
            output_lines.append("---")
        output_lines.append(_format_memory(row, compact, context_chars))
        count += 1

    for row in chunk_results[:top]:
        if count > 0 and not compact:
            output_lines.append("---")
        output_lines.append(_format_chunk(row, compact, context_chars))
        count += 1

    for kind, row in like_results[:top]:
        if count > 0 and not compact:
            output_lines.append("---")
        if kind == "memory":
            output_lines.append(_format_memory(row, compact, context_chars))
        else:
            output_lines.append(_format_chunk(row, compact, context_chars))
        count += 1

    if count == 0:
        print(f"No results for: {query}")
    else:
        print("\n".join(output_lines))
        print(f"\n({count} result{'s' if count != 1 else ''})")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Search hex memory")
    parser.add_argument("query", help="Search query")
    parser.add_argument("--top", type=int, default=10, help="Max results (default: 10)")
    parser.add_argument("--file", default=None, help="Filter by file path substring")
    parser.add_argument("--compact", action="store_true", help="One-line per result")
    parser.add_argument("--context", type=int, default=300, help="Context chars (default: 300)")
    args = parser.parse_args()
    search(args.query, top=args.top, file_filter=args.file, compact=args.compact, context_chars=args.context)
