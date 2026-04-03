#!/usr/bin/env python3
"""hex stats — show workspace health metrics.

Usage:
    python3 .hex/stats.py
    python3 .hex/stats.py --days 7
"""

import os
import re
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

HEX_DIR = Path(__file__).resolve().parent
DB_PATH = HEX_DIR / "memory" / "memory.db"
STANDING_ORDERS_DIR = HEX_DIR / "standing-orders"
EVOLUTION_DIR = HEX_DIR / "evolution"
LANDINGS_DIR = HEX_DIR / "landings"


def _db(query, params=(), fetchone=False):
    """Safe DB query — returns [] or None on any error."""
    if not DB_PATH.exists():
        return None if fetchone else []
    try:
        conn = sqlite3.connect(str(DB_PATH))
        c = conn.cursor()
        c.execute(query, params)
        result = c.fetchone() if fetchone else c.fetchall()
        conn.close()
        return result
    except sqlite3.OperationalError:
        return None if fetchone else []


def memory_stats():
    now = datetime.now(timezone.utc)
    week_start = (now - timedelta(days=now.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    last_week_start = week_start - timedelta(days=7)

    total = (_db("SELECT COUNT(*) FROM memories", fetchone=True) or (0,))[0]
    this_week = (
        _db(
            "SELECT COUNT(*) FROM memories WHERE timestamp >= ?",
            (week_start.isoformat(),),
            fetchone=True,
        )
        or (0,)
    )[0]
    last_week = (
        _db(
            "SELECT COUNT(*) FROM memories WHERE timestamp >= ? AND timestamp < ?",
            (last_week_start.isoformat(), week_start.isoformat()),
            fetchone=True,
        )
        or (0,)
    )[0]
    top_searches = _db(
        "SELECT query, COUNT(*) c FROM search_log GROUP BY query ORDER BY c DESC LIMIT 5"
    )
    return total, this_week, last_week, top_searches


def standing_order_stats():
    total = 0
    auto_generated = 0
    if not STANDING_ORDERS_DIR.exists():
        return 0, 0
    for f in STANDING_ORDERS_DIR.glob("*.md"):
        text = f.read_text(encoding="utf-8", errors="ignore")
        headings = re.findall(r"^###\s+", text, re.MULTILINE)
        count = len(headings)
        total += count
        if "auto-generated" in text.lower() or "evolution" in f.stem.lower():
            auto_generated += count
    return total, auto_generated


def evolution_stats():
    friction = proposed = implemented = 0
    monthly = {}
    if not EVOLUTION_DIR.exists():
        return 0, 0, 0, {}
    for f in EVOLUTION_DIR.glob("*.md"):
        if f.name == "README.md":
            continue
        text = f.read_text(encoding="utf-8", errors="ignore")
        section = None
        for line in text.split("\n"):
            low = line.lower().strip()
            if low.startswith("#"):
                if "friction" in low:
                    section = "friction"
                elif "proposed" in low:
                    section = "proposed"
                else:
                    section = None
            elif line.strip().startswith("- "):
                if section == "friction":
                    friction += 1
                elif section == "proposed":
                    proposed += 1
                    if line.strip().startswith("- [x]"):
                        implemented += 1
        m = re.search(r"(\d{4}-\d{2})", f.name)
        if m:
            monthly[m.group(1)] = monthly.get(m.group(1), 0) + friction
    return friction, proposed, implemented, monthly


def landing_stats():
    sessions = 0
    levels = {"L1": 0, "L2": 0, "L3": 0, "L4": 0}
    completed = 0
    total_items = 0
    if not LANDINGS_DIR.exists():
        return 0, levels, 0, 0
    for f in LANDINGS_DIR.glob("*.md"):
        if f.name == "TEMPLATE.md":
            continue
        sessions += 1
        text = f.read_text(encoding="utf-8", errors="ignore")
        current = None
        for line in text.split("\n"):
            for lv in ("L1", "L2", "L3", "L4"):
                if lv in line and line.strip().startswith("#"):
                    current = lv
            s = line.strip()
            if current and s.startswith("- ") and s != "- (none)":
                levels[current] += 1
                total_items += 1
                if s.startswith("- [x]"):
                    completed += 1
    return sessions, levels, completed, total_items


def main():
    days = 30
    if "--days" in sys.argv:
        idx = sys.argv.index("--days")
        if idx + 1 < len(sys.argv):
            days = int(sys.argv[idx + 1])

    mem_total, mem_week, mem_last, top_searches = memory_stats()
    so_total, so_auto = standing_order_stats()
    ev_friction, ev_proposed, ev_impl, ev_monthly = evolution_stats()
    ln_sessions, ln_levels, ln_done, ln_total = landing_stats()

    delta = f"+{mem_week}" if mem_week >= 0 else str(mem_week)
    comp_pct = int(ln_done / ln_total * 100) if ln_total > 0 else 0

    # Friction trend
    friction_str = str(ev_friction)
    if ev_monthly and len(ev_monthly) >= 2:
        months = sorted(ev_monthly.keys())
        first, last = ev_monthly[months[0]], ev_monthly[months[-1]]
        if first > 0:
            pct = int((1 - last / first) * 100)
            friction_str = f"{first} → {last} ({pct}% {'reduction' if pct > 0 else 'increase'})"

    bar = "━" * 36
    print(f"\nhex stats — {days} days")
    print(bar)
    print(f"Memories:        {mem_total:>4} ({delta} this week)")
    print(f"Standing orders: {so_total:>4} ({so_auto} auto-generated)")
    print(f"Friction events: {friction_str:>4}" if ev_friction == 0 else f"Friction events: {friction_str}")
    print(f"Proposed fixes:  {ev_proposed:>4} ({ev_impl} implemented)")
    print(f"Landings:        {ln_sessions:>4} sessions tracked")
    print(f"Completion:      {comp_pct:>3}% of L1-L2 items landed")
    print(bar)

    if top_searches:
        print("\nTop searches:")
        for query, cnt in top_searches:
            print(f"  {cnt:>3}x  {query}")

    lvl_parts = [f"{k}:{v}" for k, v in ln_levels.items() if v > 0]
    if lvl_parts:
        print(f"\nLanding items:  {', '.join(lvl_parts)}")
    print()


if __name__ == "__main__":
    main()
