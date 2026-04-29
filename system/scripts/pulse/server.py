#!/usr/bin/env python3
"""Hex Pulse — live system health dashboard server. Port 8896."""

import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer, ThreadingHTTPServer
import queue as _queue
from threading import Lock, Thread

try:
    import anthropic as _anthropic
    _HAS_SDK = True
except ImportError:
    _HAS_SDK = False

PORT = 8896
_THIS = os.path.abspath(__file__)
HEX_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(_THIS))))
sys.path.insert(0, os.path.dirname(_THIS))
try:
    from test_collector import collect_tests_cached as _collect_tests_cached
    _HAS_TEST_COLLECTOR = True
except ImportError:
    _HAS_TEST_COLLECTOR = False
VITALS_SCRIPT = os.path.join(HEX_ROOT, ".hex", "scripts", "hex-vitals.py")
FLEET_BIN = os.path.join(HEX_ROOT, ".hex", "bin", "hex")
INITIATIVE_BIN = os.path.join(HEX_ROOT, ".hex", "scripts", "hex-initiative.py")
EXPERIMENT_BIN = os.path.join(HEX_ROOT, ".hex", "scripts", "hex-experiment.py")
AUDIT_DIR = os.path.expanduser("~/.hex/audit")
QUALITY_CHECK_BIN = os.path.join(HEX_ROOT, ".hex", "scripts", "quality-check.py")

_fleet_cache: dict = {"data": None, "ts": 0.0}
_fleet_lock = Lock()
_initiatives_cache: dict = {"data": None, "ts": 0.0}
_initiatives_lock = Lock()
_experiments_cache: dict = {"data": None, "ts": 0.0}
_experiments_lock = Lock()
_quality_cache: dict = {"data": None, "ts": 0.0}
_quality_lock = Lock()
_boi_queue_cache: dict = {"data": None, "ts": 0.0}
_boi_queue_lock = Lock()
_tests_cache: dict = {"data": None, "ts": 0.0}
_tests_lock = Lock()

BOI_BIN = os.path.expanduser("~/.boi/boi")
_sse_counter = 0
_sse_clients: list = []
_sse_clients_lock = Lock()
_msg_counter = 0
_msg_lock = Lock()
PULSE_MESSAGES_FILE = os.path.join(AUDIT_DIR, "pulse-messages.jsonl")
_pulse_file_lock = Lock()

# ── Data collection ───────────────────────────────────────────────────────────

def collect_vitals() -> dict:
    """Run hex-vitals.py and return its JSON output."""
    try:
        r = subprocess.run(
            ["python3", VITALS_SCRIPT],
            capture_output=True, text=True, timeout=15
        )
        if r.stdout.strip():
            return json.loads(r.stdout)
    except Exception as e:
        pass
    return {"_error": "hex-vitals unavailable", "signals": {}}


def _cutoff_24h() -> float:
    return time.time() - 86400


def _read_jsonl(path: str) -> list[dict]:
    """Read a JSONL file, return list of parsed dicts (silently handle missing/errors)."""
    if not os.path.exists(path):
        return []
    rows = []
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        rows.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass
    except OSError:
        pass
    return rows


def _ts_to_epoch(ts_str: str) -> float:
    """Parse ISO8601 timestamp to epoch float. Returns 0 on failure."""
    if not ts_str:
        return 0.0
    try:
        dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        return dt.timestamp()
    except Exception:
        return 0.0


def _append_pulse_record(record: dict) -> None:
    """Append one JSON record to the pulse messages JSONL file."""
    os.makedirs(AUDIT_DIR, exist_ok=True)
    line = json.dumps(record) + "\n"
    with _pulse_file_lock:
        with open(PULSE_MESSAGES_FILE, "a") as f:
            f.write(line)


def _get_pulse_messages(limit: int = 50) -> list[dict]:
    """Read pulse-messages.jsonl, merge by id, return newest `limit` messages."""
    rows = _read_jsonl(PULSE_MESSAGES_FILE)
    by_id: dict = {}
    order: list = []
    for row in rows:
        mid = row.get("id")
        if not mid:
            continue
        if mid not in by_id:
            by_id[mid] = {}
            order.append(mid)
        by_id[mid].update({k: v for k, v in row.items() if v is not None})
    merged = [by_id[mid] for mid in order]
    return merged[-limit:]


def collect_ownership_metrics() -> dict:
    """Read ownership audit JSONL files and compute summary stats."""
    cutoff = _cutoff_24h()
    result: dict = {}

    # Frustration signals: count entries in last 24h
    path = os.path.join(AUDIT_DIR, "frustration-signals.jsonl")
    rows = [r for r in _read_jsonl(path) if _ts_to_epoch(r.get("ts", "")) >= cutoff]
    result["frustration"] = {
        "count": len(rows),
        "sessions": len(set(r.get("session_id", str(i)) for i, r in enumerate(rows))),
        "available": os.path.exists(path),
    }

    # Memory effectiveness: feedback recurrence (no time filter — cumulative)
    path = os.path.join(AUDIT_DIR, "memory-effectiveness.jsonl")
    rows = _read_jsonl(path)
    high = sum(1 for r in rows if r.get("recurrence_rate", 0) > 0.5)
    result["feedback_recurrence"] = {
        "total_feedback": len(rows),
        "high_recurrence_count": high,
        "available": os.path.exists(path),
    }

    # Loop detections: count in last 24h
    path = os.path.join(AUDIT_DIR, "loop-detections.jsonl")
    rows = [r for r in _read_jsonl(path) if _ts_to_epoch(r.get("ts", "")) >= cutoff]
    result["loops"] = {
        "count": len(rows),
        "available": os.path.exists(path),
    }

    # Done-claim verification: verified_rate
    path = os.path.join(AUDIT_DIR, "done-claim-verification.jsonl")
    rows = [r for r in _read_jsonl(path) if _ts_to_epoch(r.get("ts", "")) >= cutoff]
    if rows:
        verified = sum(1 for r in rows if r.get("verified", False))
        rate = verified / len(rows)
    else:
        rate = 1.0  # no data → assume fully verified
    result["done_claims"] = {
        "total": len(rows),
        "verified_rate": round(rate, 4),
        "available": os.path.exists(path),
    }

    # Session anomalies: duplicate session pairs in last 24h
    path = os.path.join(AUDIT_DIR, "session-anomalies.jsonl")
    rows = [r for r in _read_jsonl(path) if _ts_to_epoch(r.get("ts", "")) >= cutoff]
    result["session_anomalies"] = {
        "count": len(rows),
        "available": os.path.exists(path),
    }

    return result


def collect_fleet() -> dict:
    """Run hex agent fleet and parse table output. Cached for 30s."""
    global _fleet_cache
    with _fleet_lock:
        if time.time() - _fleet_cache["ts"] < 30 and _fleet_cache["data"] is not None:
            return _fleet_cache["data"]

    data: dict = {"agents": [], "total_wakes": 0, "total_cost": 0.0, "active": 0, "_error": None}
    try:
        r = subprocess.run(
            [FLEET_BIN, "fleet"],
            capture_output=True, text=True, timeout=15
        )
        lines = r.stdout.splitlines()
        for line in lines[2:]:  # skip header and separator
            line = line.strip()
            if not line:
                continue
            # Remove ● marker, normalize whitespace
            parts = line.replace("●", "").split()
            # Expect: agent wakes last_wake active blocked $ cost
            if len(parts) < 6:
                continue
            try:
                name = parts[0]
                wakes = int(parts[1])
                active = int(parts[3])
                cost_str = parts[-1]
                cost = float(cost_str)
                data["agents"].append({
                    "name": name, "wakes": wakes, "active": active, "cost": cost
                })
                data["total_wakes"] += wakes
                data["total_cost"] += cost
                data["active"] += active
            except (ValueError, IndexError):
                pass
    except Exception as e:
        data["_error"] = str(e)

    data["total_cost"] = round(data["total_cost"], 4)
    data["agent_count"] = len(data["agents"])

    with _fleet_lock:
        _fleet_cache = {"data": data, "ts": time.time()}
    return data


def collect_initiatives() -> dict:
    """Run hex-initiative.py status --json and return structured data. Cached 60s."""
    global _initiatives_cache
    with _initiatives_lock:
        if time.time() - _initiatives_cache["ts"] < 60 and _initiatives_cache["data"] is not None:
            return _initiatives_cache["data"]

    result: dict = {"initiatives": [], "_error": None}
    try:
        r = subprocess.run(
            ["python3", INITIATIVE_BIN, "status", "--json"],
            capture_output=True, text=True, timeout=10, cwd=HEX_ROOT
        )
        if r.returncode == 0 and r.stdout.strip():
            raw = json.loads(r.stdout)
            initiatives = []
            for d in raw:
                krs = d.get("key_results") or []
                krs_met = sum(1 for kr in krs if kr.get("status") == "met")
                experiment_ids = d.get("experiments") or []
                initiatives.append({
                    "id": d.get("id", ""),
                    "name": d.get("id", ""),
                    "owner": d.get("owner", ""),
                    "status": d.get("status", ""),
                    "horizon": str(d.get("horizon", "") or ""),
                    "krs_met": krs_met,
                    "krs_total": len(krs),
                    "experiment_count": len(experiment_ids),
                    "experiment_ids": experiment_ids,
                })
            result["initiatives"] = initiatives
        else:
            result["_error"] = "unavailable"
    except Exception:
        result["initiatives"] = []
        result["_error"] = "unavailable"

    with _initiatives_lock:
        _initiatives_cache = {"data": result, "ts": time.time()}
    return result


def collect_experiments() -> dict:
    """Run hex-experiment.py list --json and return structured data. Cached 60s."""
    global _experiments_cache
    with _experiments_lock:
        if time.time() - _experiments_cache["ts"] < 60 and _experiments_cache["data"] is not None:
            return _experiments_cache["data"]

    result: dict = {"experiments": [], "_error": None}
    try:
        r = subprocess.run(
            ["python3", EXPERIMENT_BIN, "list", "--json"],
            capture_output=True, text=True, timeout=10, cwd=HEX_ROOT
        )
        if r.returncode == 0 and r.stdout.strip():
            raw = json.loads(r.stdout)
            experiments = []
            for d in raw:
                metrics = d.get("metrics") or {}
                primary = metrics.get("primary") or {}
                primary_name = primary.get("name", "")
                baseline_vals = ((d.get("baseline") or {}).get("values") or {})
                post_vals = ((d.get("post_change") or {}).get("values") or {})
                experiments.append({
                    "id": d.get("id", ""),
                    "title": d.get("title", ""),
                    "status": d.get("state", ""),
                    "owner": d.get("owner", ""),
                    "initiative": d.get("initiative", ""),
                    "primary_metric_baseline": baseline_vals.get(primary_name),
                    "primary_metric_current": post_vals.get(primary_name),
                })
            result["experiments"] = experiments
        else:
            result["_error"] = "unavailable"
    except Exception:
        result["experiments"] = []
        result["_error"] = "unavailable"

    with _experiments_lock:
        _experiments_cache = {"data": result, "ts": time.time()}
    return result


def compute_scores(vitals: dict, ownership: dict, fleet: dict) -> tuple[float, float]:
    """Return (productivity_score 0-100, loop_score 0-100)."""
    signals = vitals.get("signals", {})

    # ── System Productivity ─────────────────────────────────────────
    cr = (signals.get("completion_rate") or {}).get("value")
    tp = (signals.get("task_throughput") or {}).get("value", 0) or 0
    ztf = (signals.get("zero_task_failures") or {}).get("value", 0) or 0

    completion_rate = cr if cr is not None else 0.75  # assume healthy if unknown
    norm_throughput = min(1.0, tp / 50.0)
    norm_failures = min(1.0, ztf / 10.0)

    productivity = min(100.0, (
        completion_rate * 40
        + norm_throughput * 30
        + (1 - norm_failures) * 30
    ))

    # ── Mike-in-the-Loop ────────────────────────────────────────────
    frustration_sessions = ownership.get("frustration", {}).get("sessions", 0)
    recurrence_high = ownership.get("feedback_recurrence", {}).get("high_recurrence_count", 0)
    loop_count = ownership.get("loops", {}).get("count", 0)
    verified_rate = ownership.get("done_claims", {}).get("verified_rate", 1.0)

    loop_score = min(100.0, (
        frustration_sessions * 10
        + recurrence_high * 15
        + loop_count * 20
        + (1 - verified_rate) * 55
    ))

    return round(productivity, 1), round(loop_score, 1)


def collect_quality_metrics() -> dict:
    """Collect quality antagonist metrics. Cached 60s. Graceful if unavailable."""
    global _quality_cache
    with _quality_lock:
        if time.time() - _quality_cache["ts"] < 60 and _quality_cache["data"] is not None:
            return _quality_cache["data"]

    unavailable: dict = {"available": False}

    cutoff = _cutoff_24h()

    # Read JSONL files for base metrics
    sweep_path = os.path.join(AUDIT_DIR, "quality-sweep.jsonl")
    audits_path = os.path.join(AUDIT_DIR, "quality-spec-audits.jsonl")

    sweep_rows = [r for r in _read_jsonl(sweep_path) if _ts_to_epoch(r.get("ts", "")) >= cutoff]
    audit_rows = [r for r in _read_jsonl(audits_path) if _ts_to_epoch(r.get("ts", "")) >= cutoff]

    # Try live data from quality-check.py if it exists
    live: dict = {}
    if os.path.exists(QUALITY_CHECK_BIN):
        try:
            r = subprocess.run(
                ["python3", QUALITY_CHECK_BIN, "--sweep", "--json"],
                capture_output=True, text=True, timeout=15, cwd=HEX_ROOT
            )
            if r.returncode == 0 and r.stdout.strip():
                live = json.loads(r.stdout)
        except Exception:
            pass

    # Derive metrics: prefer live data, fall back to JSONL
    if live:
        result = {
            "available": True,
            "specs_audited_24h": live.get("specs_audited_24h", len(audit_rows)),
            "gaming_detected": live.get("gaming_detected", 0),
            "krs_reverted": live.get("krs_reverted", 0),
            "velocity_anomalies": live.get("velocity_anomalies", 0),
            "quality_score_pct": live.get("quality_score_pct", None),
        }
    elif sweep_rows or audit_rows:
        specs_audited = len(audit_rows)
        gaming = sum(1 for r in audit_rows if r.get("gaming_detected") or r.get("flag") == "gaming")
        reverted = sum(1 for r in sweep_rows if r.get("kr_reverted") or r.get("action") == "revert")
        velocity = sum(1 for r in audit_rows if r.get("velocity_anomaly") or r.get("flag") == "velocity")
        passed = sum(1 for r in audit_rows if r.get("passed") is True)
        score = round(passed / specs_audited * 100) if specs_audited else None
        result = {
            "available": True,
            "specs_audited_24h": specs_audited,
            "gaming_detected": gaming,
            "krs_reverted": reverted,
            "velocity_anomalies": velocity,
            "quality_score_pct": score,
        }
    else:
        result = unavailable

    with _quality_lock:
        _quality_cache = {"data": result, "ts": time.time()}
    return result


def _boi_title(entry: dict) -> str:
    """Derive a display title from the spec path."""
    path = entry.get("original_spec_path") or entry.get("spec_path") or ""
    base = os.path.basename(path)
    for suffix in (".spec.md", ".md"):
        if base.endswith(suffix):
            base = base[: -len(suffix)]
    return base or entry.get("id", "")


def _fmt_duration(seconds: float) -> str:
    """Format elapsed seconds into a compact human-readable string."""
    s = int(seconds)
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m"
    return f"{s // 3600}h{(s % 3600) // 60}m"


def collect_boi_queue() -> dict:
    """Run boi status --all --json and return structured queue data. Cached 10s."""
    global _boi_queue_cache
    with _boi_queue_lock:
        if time.time() - _boi_queue_cache["ts"] < 10 and _boi_queue_cache["data"] is not None:
            return _boi_queue_cache["data"]

    unavailable: dict = {"available": False, "active": [], "queued": [], "recent_completed": [], "recent_failed": [], "summary": {"active": 0, "queued": 0, "completed_1h": 0, "success_rate": None}}

    if not os.path.exists(BOI_BIN):
        return unavailable

    try:
        r = subprocess.run(
            ["bash", BOI_BIN, "status", "--all", "--json"],
            capture_output=True, text=True, timeout=10
        )
        if not r.stdout.strip():
            raise ValueError("empty output")
        raw = json.loads(r.stdout)
    except Exception:
        with _boi_queue_lock:
            _boi_queue_cache = {"data": unavailable, "ts": time.time()}
        return unavailable

    now = time.time()
    cutoff_1h = now - 3600

    active: list = []
    queued: list = []
    recent_completed: list = []
    recent_failed: list = []
    completed_1h_count = 0
    failed_1h_count = 0

    for e in raw.get("entries", []):
        status = e.get("status", "")
        tid = e.get("id", "")
        title = _boi_title(e)
        tasks_done = e.get("tasks_done", 0) or 0
        tasks_total = e.get("tasks_total", 0) or 0

        if status == "running":
            start_ts = _ts_to_epoch(e.get("first_running_at", ""))
            duration = _fmt_duration(now - start_ts) if start_ts else "?"
            active.append({"id": tid, "title": title, "tasks_done": tasks_done, "tasks_total": tasks_total, "duration": duration})

        elif status == "queued":
            blocked_by = e.get("blocked_reason") or None
            queued.append({"id": tid, "title": title, "tasks_done": tasks_done, "tasks_total": tasks_total, "blocked_by": blocked_by})

        elif status == "completed":
            finish_ts = _ts_to_epoch(e.get("last_iteration_at", ""))
            if finish_ts >= cutoff_1h:
                completed_1h_count += 1
                if len(recent_completed) < 3:
                    ago = _fmt_duration(now - finish_ts)
                    recent_completed.append({"id": tid, "title": title, "tasks_done": tasks_done, "tasks_total": tasks_total, "completed_ago": ago})

        elif status in ("failed", "canceled"):
            finish_ts = _ts_to_epoch(e.get("last_iteration_at", ""))
            if finish_ts >= cutoff_1h:
                failed_1h_count += 1
                if len(recent_failed) < 3:
                    ago = _fmt_duration(now - finish_ts)
                    recent_failed.append({"id": tid, "title": title, "tasks_done": tasks_done, "tasks_total": tasks_total, "failed_ago": ago})

    total_finished_1h = completed_1h_count + failed_1h_count
    success_rate = round(completed_1h_count / total_finished_1h, 2) if total_finished_1h > 0 else None

    result: dict = {
        "available": True,
        "active": active,
        "queued": queued,
        "recent_completed": recent_completed,
        "recent_failed": recent_failed,
        "summary": {
            "active": len(active),
            "queued": len(queued),
            "completed_1h": completed_1h_count,
            "success_rate": success_rate,
        },
    }

    with _boi_queue_lock:
        _boi_queue_cache = {"data": result, "ts": time.time()}
    return result


def collect_tests_data() -> dict:
    """Collect test metrics via test_collector. Cached 5 min."""
    global _tests_cache
    with _tests_lock:
        if time.time() - _tests_cache["ts"] < 300 and _tests_cache["data"] is not None:
            return _tests_cache["data"]

    if not _HAS_TEST_COLLECTOR:
        result: dict = {"available": False, "message": "test_collector not available"}
    else:
        try:
            result = _collect_tests_cached(ttl=300.0)
        except Exception as e:
            result = {"available": False, "message": str(e)}

    with _tests_lock:
        _tests_cache = {"data": result, "ts": time.time()}
    return result


_FEEDBACK_LOOPS_BIN = os.path.join(HEX_ROOT, ".hex", "scripts", "hex-feedback-loops.py")
_feedback_loops_cache: dict = {"data": None, "ts": 0.0}
_feedback_loops_lock = Lock()
_behavioral_cache: dict = {"data": None, "ts": 0.0}
_behavioral_lock = Lock()

_BEHAVIORAL_MEMORY_MODULE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "behavioral_memory.py"
)


def collect_behavioral_health() -> dict:
    """Return behavioral_pattern stats for the System Health panel (cached 120s).

    Uses check_behavior to surface any HIGH-risk patterns active right now,
    and get_behavioral_health for aggregate counts. This closes the
    error-to-lesson feedback loop in the Pulse dashboard.
    """
    with _behavioral_lock:
        if time.time() - _behavioral_cache["ts"] < 120 and _behavioral_cache["data"] is not None:
            return _behavioral_cache["data"]

    data: dict = {"status": "unavailable"}
    try:
        _scripts = os.path.dirname(os.path.abspath(__file__))
        if _scripts not in sys.path:
            sys.path.insert(0, _scripts)
        from behavioral_memory import get_behavioral_health, check_behavior  # noqa: PLC0415
        health = get_behavioral_health()
        # Spot-check for HIGH-risk patterns relevant to common agent actions
        spot = check_behavior("agent dispatching spec or sending slack message")
        data = {
            "status": health.get("status", "ok"),
            "total_patterns": health.get("total_patterns", 0),
            "high_recurrence_count": health.get("high_recurrence_count", 0),
            "total_corrections": health.get("total_corrections", 0),
            "spot_risk_level": spot.get("risk_level", "NONE"),
            "spot_top_pattern": spot["matches"][0]["pattern"][:80] if spot.get("matches") else None,
        }
    except Exception as exc:
        data = {"status": "error", "error": str(exc)}

    with _behavioral_lock:
        _behavioral_cache["data"] = data
        _behavioral_cache["ts"] = time.time()
    return data


def collect_feedback_loops() -> dict:
    """Run hex-feedback-loops.py and return its JSON output (cached 60s)."""
    with _feedback_loops_lock:
        if time.time() - _feedback_loops_cache["ts"] < 60 and _feedback_loops_cache["data"] is not None:
            return _feedback_loops_cache["data"]
    try:
        r = subprocess.run(
            ["python3", _FEEDBACK_LOOPS_BIN],
            capture_output=True, text=True, timeout=15,
        )
        if r.stdout.strip():
            data = json.loads(r.stdout)
            with _feedback_loops_lock:
                _feedback_loops_cache["data"] = data
                _feedback_loops_cache["ts"] = time.time()
            return data
    except Exception:
        pass
    return {"overall": "unknown", "loops": [], "summary": {}}


def get_all_metrics() -> dict:
    """Collect all data and compute composite scores."""
    vitals = collect_vitals()
    ownership = collect_ownership_metrics()
    fleet = collect_fleet()
    initiatives = collect_initiatives()
    experiments = collect_experiments()
    quality = collect_quality_metrics()
    boi_queue = collect_boi_queue()
    tests = collect_tests_data()
    feedback_loops = collect_feedback_loops()
    behavioral_health = collect_behavioral_health()

    productivity_score, loop_score = compute_scores(vitals, ownership, fleet)

    return {
        "ts": datetime.now(timezone.utc).isoformat(),
        "productivity_score": productivity_score,
        "loop_score": loop_score,
        "productivity": vitals,
        "user_experience": ownership,
        "fleet": fleet,
        "initiatives": initiatives,
        "experiments": experiments,
        "quality": quality,
        "boi_queue": boi_queue,
        "tests": tests,
        "system_health": feedback_loops,
        "behavioral_health": behavioral_health,
    }


# ── Message handling ─────────────────────────────────────────────────────────

def _push_sse(event_type: str, data: dict):
    """Push a typed SSE event to all connected clients."""
    payload = f"event: {event_type}\ndata: {json.dumps(data)}\n\n"
    with _sse_clients_lock:
        for q in list(_sse_clients):
            try:
                q.put_nowait(payload)
            except _queue.Full:
                pass


def _push_response(response: dict):
    """Push a response event to all connected SSE clients."""
    _push_sse("response", response)


# ── Dashboard Context (Approach E: Dashboard-as-Memory) ───────────────────────

_PULSE_ADDENDUM = (
    '\n\nYou are responding via the Pulse dashboard surface. '
    'Respond ONLY with a JSON object (no prose, no markdown, just raw JSON) with these keys: '
    '{"effect_type": "highlight|annotate|prose|action|directive", '
    '"target": "metric-id or null", '
    '"text": "response text under 80 words", '
    '"action": "command to run or null"}. '
    'Metric IDs: d-cr (completion rate), d-tp (throughput), d-ztf (failures), '
    'd-fr (frustration), d-frc (feedback recurrence), d-lp (loops), d-dc (done-claims), '
    'd-sa (session anomalies). '
    'DIRECTIVE DETECTION: If the user says "I want X", "add X", "build X", "create X", "fix X" — '
    'these are directives, not queries. Return effect_type: directive with a spec outline. '
    'When returning directive, also include: '
    '"spec_title": short title for the BOI spec (string), '
    '"spec_tasks": array of {"title": "...", "spec_text": "..."} for 1-3 tasks, '
    '"initiative": which initiative this serves (default "init-reduce-mike-on-loop").'
)

# Persistent hex session for Pulse surface
_hex_session_id = None


class DashboardContext:
    """Pulse is a surface. Hex is the brain.

    Messages route through claude -p with full CLAUDE.md context,
    same as cc-connect does for Slack. Dashboard state is injected
    as context alongside the user's message.
    """

    MAX_EFFECTS = 5

    def __init__(self):
        self._recent_effects: list[dict] = []

    def handle(self, text: str, dashboard_state: dict) -> dict:
        global _hex_session_id
        state_summary = self._summarize_state(dashboard_state)
        recent = self._format_recent()

        prompt_parts = [
            f"[Pulse dashboard context]\n{state_summary}",
        ]
        if recent:
            prompt_parts.append(f"[Recent interactions]\n{recent}")
        prompt_parts.append(f"[User message]\n{text}")
        prompt_parts.append(_PULSE_ADDENDUM)
        full_prompt = "\n\n".join(prompt_parts)

        response = self._call_hex(full_prompt)
        self._recent_effects.append({
            "query": text,
            "effect_type": response.get("effect_type"),
            "target": response.get("target"),
            "text": response.get("text"),
        })
        if len(self._recent_effects) > self.MAX_EFFECTS:
            self._recent_effects.pop(0)
        return response

    def _summarize_state(self, state: dict) -> str:
        p = state.get("productivity", {})
        sigs = p.get("signals", {})
        ux = state.get("user_experience", {})
        fleet = state.get("fleet", {})
        lines = [
            f"Productivity score: {state.get('productivity_score', '?')}/100",
            f"Loop score: {state.get('loop_score', '?')}/100 (lower=better)",
            f"Completion rate: {sigs.get('completion_rate', {}).get('value', '?')}",
            f"Task throughput: {sigs.get('task_throughput', {}).get('value', '?')}",
            f"Zero-task failures: {sigs.get('zero_task_failures', {}).get('value', '?')}",
            f"Frustration signals: {ux.get('frustration', {}).get('count', '?')}",
            f"Feedback recurrence (high): {ux.get('feedback_recurrence', {}).get('high_recurrence_count', '?')}",
            f"Active loops: {ux.get('loops', {}).get('count', '?')}",
            f"Fleet: {fleet.get('agent_count', '?')} agents, {fleet.get('total_wakes', '?')} wakes, ${fleet.get('total_cost', '?'):.2f}" if isinstance(fleet.get('total_cost'), (int, float)) else f"Fleet: {fleet.get('agent_count', '?')} agents",
        ]
        return "\n".join(lines)

    def _format_recent(self) -> str:
        if not self._recent_effects:
            return ""
        return "\n".join(
            f"  - [{e.get('effect_type','?')}] {e.get('query','')} → {e.get('text','')}"
            for e in self._recent_effects
        )

    def _parse_raw(self, raw: str) -> dict:
        raw = raw.strip()
        # Strip markdown code fences (brain sometimes wraps JSON in ```json ... ```)
        stripped = re.sub(r'^```(?:json)?\s*', '', raw, flags=re.MULTILINE)
        stripped = re.sub(r'\s*```\s*$', '', stripped, flags=re.MULTILINE).strip()
        for candidate in (stripped, raw):
            try:
                return json.loads(candidate)
            except json.JSONDecodeError:
                pass
        # Fallback: scan for a top-level JSON object via decoder
        decoder = json.JSONDecoder()
        for start in range(len(raw)):
            if raw[start] == '{':
                try:
                    obj, _ = decoder.raw_decode(raw, start)
                    if isinstance(obj, dict) and 'effect_type' in obj:
                        return obj
                except json.JSONDecodeError:
                    pass
        return {"effect_type": "prose", "target": None, "text": raw[:200], "action": None}

    def _call_hex(self, prompt: str) -> dict:
        global _hex_session_id
        claude_bin = shutil.which("claude") or os.path.expanduser("~/.local/bin/claude")
        if not os.path.exists(claude_bin):
            return {"effect_type": "prose", "text": "hex not available (claude not on PATH).", "target": None, "action": None}

        cmd = [claude_bin, '-p', prompt, '--output-format', 'json', '--dangerously-skip-permissions']
        if _hex_session_id:
            cmd.extend(['--resume', _hex_session_id])

        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=120, cwd=HEX_ROOT)
        except subprocess.TimeoutExpired:
            return {"effect_type": "prose", "text": "Response timed out.", "target": None, "action": None}

        import sys
        if r.returncode != 0 or not r.stdout.strip():
            print(f"[pulse] claude exit={r.returncode} stderr={r.stderr[:300]}", file=sys.stderr, flush=True)
            return {"effect_type": "prose", "text": f"hex error (exit {r.returncode}): {r.stderr[:100]}", "target": None, "action": None}
        try:
            outer = json.loads(r.stdout)
            if not _hex_session_id and outer.get("session_id"):
                _hex_session_id = outer["session_id"]
            inner = outer.get('result') or outer.get('content') or ''
            if isinstance(inner, str):
                return self._parse_raw(inner)
            elif isinstance(inner, dict):
                return inner
        except (json.JSONDecodeError, AttributeError) as e:
            print(f"[pulse] parse error: {e}, stdout={r.stdout[:300]}", file=sys.stderr, flush=True)
        return {"effect_type": "prose", "text": "Could not parse hex response.", "target": None, "action": None}


_ctx = DashboardContext()


def _prewarm_hex():
    """Prime the CLAUDE.md cache on startup so first real message is fast."""
    import sys
    print("[pulse] pre-warming hex session...", file=sys.stderr, flush=True)
    try:
        response = _ctx._call_hex("Respond with exactly: {\"effect_type\":\"prose\",\"text\":\"ready\",\"target\":null,\"action\":null}")
        print(f"[pulse] hex pre-warmed, session={_hex_session_id}, response={response.get('text','?')}", file=sys.stderr, flush=True)
    except Exception as e:
        print(f"[pulse] pre-warm failed: {e}", file=sys.stderr, flush=True)


Thread(target=_prewarm_hex, daemon=True).start()


def _handle_message(text: str, message_id: str = ""):
    """Route message through DashboardContext and push structured response to SSE clients."""
    fallback = {"effect_type": "prose", "text": "Couldn't process that right now.", "target": None}
    if message_id:
        _push_sse("message_status", {"id": message_id, "status": "processing"})
    try:
        metrics = get_all_metrics()
        response = _ctx.handle(text, metrics)

        # Action whitelist enforcement
        if response.get('effect_type') == 'action' and response.get('action'):
            action = response['action']
            cmd = None
            if action.startswith('hex agent wake ') and len(action.split()) == 4:
                cmd = [FLEET_BIN, 'agent', 'wake', action.split()[3]]
            elif action == 'hex agent fleet':
                cmd = [FLEET_BIN, 'agent', 'fleet']
            elif action == 'bash .hex/scripts/metrics/run-all.sh':
                cmd = ['bash', os.path.join(HEX_ROOT, '.hex', 'scripts', 'metrics', 'run-all.sh')]

            if cmd:
                try:
                    ar = subprocess.run(cmd, capture_output=True, text=True, timeout=15, cwd=HEX_ROOT)
                    response['action_result'] = (ar.stdout.strip() or ar.stderr.strip())[:200]
                except Exception as e:
                    response['action_result'] = f'Error: {e}'
            else:
                response['action'] = None
                response['text'] = (response.get('text') or '') + ' (action not permitted)'

        # Directive handling: generate a BOI spec and dispatch it
        if response.get('effect_type') == 'directive':
            spec_title = response.get('spec_title') or 'Pulse Directive'
            spec_tasks = response.get('spec_tasks') or []
            initiative = response.get('initiative') or 'init-reduce-mike-on-loop'
            productivity = metrics.get('productivity_score', '?')
            loop_sc = metrics.get('loop_score', '?')

            tasks_section = ""
            for i, task in enumerate(spec_tasks, 1):
                tasks_section += (
                    f"\n### t-{i}: {task.get('title', 'Task')}\n"
                    f"PENDING\n\n"
                    f"**Spec:** {task.get('spec_text', '')}\n\n"
                    f"**Verify:** `echo verify`\n"
                )

            spec_content = (
                f"# {spec_title}\n\n"
                f"**Mode:** execute\n"
                f"**Initiative:** {initiative}\n\n"
                f"## Context\n\n"
                f'Directive from Mike via Pulse: "{text}"\n'
                f"Dashboard state at time of request: productivity={productivity}, loop={loop_sc}\n\n"
                f"## Tasks\n"
                f"{tasks_section}"
            )

            specs_dir = os.path.join(HEX_ROOT, "specs")
            os.makedirs(specs_dir, exist_ok=True)
            ts_stamp = int(time.time())
            spec_path = os.path.join(specs_dir, f"pulse-directive-{ts_stamp}.spec.md")
            tmp_path = spec_path + ".tmp"
            try:
                with open(tmp_path, "w") as _sf:
                    _sf.write(spec_content)
                os.rename(tmp_path, spec_path)
                dr = subprocess.run(
                    ["bash", BOI_BIN, "dispatch", "--spec", spec_path],
                    capture_output=True, text=True, timeout=30, cwd=HEX_ROOT
                )
                _mid = re.search(r'(q-\d+)', dr.stdout + dr.stderr)
                spec_id = _mid.group(1) if _mid else "q-?"
                response['spec_id'] = spec_id
                response['text'] = f"Dispatched: {spec_title} as {spec_id}"
            except Exception as _de:
                response['effect_type'] = 'prose'
                response['text'] = f"Couldn't dispatch: {_de}"

        if message_id:
            response['message_id'] = message_id
            rec_resp = {
                "effect_type": response.get("effect_type"),
                "text": response.get("text"),
                "target": response.get("target"),
            }
            if response.get("spec_id"):
                rec_resp["spec_id"] = response.get("spec_id")
            if response.get("spec_title"):
                rec_resp["spec_title"] = response.get("spec_title")
            _append_pulse_record({
                "id": message_id,
                "ts": datetime.now(timezone.utc).isoformat(),
                "status": "responded",
                "response": rec_resp,
            })
        _push_response(response)
    except Exception as exc:
        if message_id:
            _append_pulse_record({"id": message_id, "ts": datetime.now(timezone.utc).isoformat(), "status": "failed", "error": str(exc)})
            _push_sse("message_status", {"id": message_id, "status": "failed", "error": str(exc)})
        _push_response(fallback)


# ── Dashboard HTML ────────────────────────────────────────────────────────────

DASHBOARD_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8"/><meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>hex pulse</title>
<script src="/comments/widget.js"></script>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{background:#faf8f5;color:#1a1a1a;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;min-height:100vh;display:flex;flex-direction:column}
header{background:#1a1a1a;color:#faf8f5;padding:11px 24px;display:flex;align-items:center;justify-content:space-between}
header h1{font-size:.9rem;font-weight:500;letter-spacing:.07em}
.hdr-r{display:flex;align-items:center;gap:10px;font-size:.75rem;color:#888}
.pdot{width:8px;height:8px;border-radius:50%;background:#555;flex-shrink:0}
@keyframes pf{0%{opacity:1}100%{opacity:.3}}
.pdot.on{background:#2d8a4e;animation:pf .8s ease-out forwards}
.rc{color:#e6a817;display:none}
.rc.vis{display:inline}
main{flex:1;padding:28px 24px 16px;max-width:860px;margin:0 auto;width:100%;display:flex;flex-direction:column;gap:28px}
.heroes{display:grid;grid-template-columns:1fr 1fr;gap:20px}
.hero{text-align:center;padding:28px 12px}
.hlabel{font-size:.7rem;text-transform:uppercase;letter-spacing:.1em;color:#888;margin-bottom:6px}
.hscore{font-size:4.5rem;font-weight:700;line-height:1;color:#ccc;transition:color .4s}
.hq{font-size:.7rem;color:#aaa;margin-top:5px}
.green{color:#2d8a4e}.amber{color:#e6a817}.red{color:#c62828}.muted{color:#bbb}
.signals{display:grid;grid-template-columns:1fr 1fr;gap:20px}
.scol h3{font-size:.7rem;text-transform:uppercase;letter-spacing:.1em;color:#999;margin-bottom:10px}
.srow{display:flex;align-items:center;justify-content:space-between;padding:7px 0;border-bottom:1px solid #ede8e0}
.srow:last-child{border-bottom:none}
.sname{font-size:.82rem;color:#666}
.sright{display:flex;align-items:center;gap:7px}
.dot{width:7px;height:7px;border-radius:50%;background:#ddd;flex-shrink:0}
.sval{font-size:.88rem;font-weight:600;min-width:36px;text-align:right;color:#bbb;transition:color .3s}
footer{background:#1a1a1a;color:#666;padding:9px 24px;font-size:.78rem;display:flex;gap:20px;align-items:center;flex-wrap:wrap}
footer b{color:#faf8f5;font-weight:500}
/* FAB — quick send */
.pm-pill{position:fixed;bottom:24px;right:24px;width:56px;height:56px;border-radius:50%;background:#1a1a1a;color:#fff;font-size:1.5rem;line-height:1;display:flex;align-items:center;justify-content:center;cursor:pointer;transition:box-shadow .2s,opacity .25s,transform .15s;z-index:100;user-select:none;border:none;box-shadow:0 4px 12px rgba(0,0,0,.3)}
.pm-pill:hover{box-shadow:0 6px 20px rgba(0,0,0,.45);transform:scale(1.05)}
.pm-pill:active{transform:scale(.93)}
/* stark state */
.pm-overlay{position:fixed;inset:0;z-index:99;display:none}
.pm-overlay.active{display:block}
.pm-stark{position:fixed;left:50%;top:40%;transform:translate(-50%,-50%);width:min(680px,calc(100vw - 48px));z-index:101;display:none;flex-direction:column;gap:6px}
.pm-stark.active{display:flex}
.pm-stark input{width:100%;height:48px;border:1px solid #ddd;border-radius:6px;padding:0 16px;font-size:1rem;background:#fff;outline:none;box-shadow:0 2px 20px rgba(0,0,0,.1)}
.pm-hint{font-size:.72rem;color:#aaa;text-align:right}
body.pm-open main,body.pm-open footer{opacity:.4;pointer-events:none;transition:opacity .25s}
/* response overlays */
.hex-prose{position:fixed;left:50%;top:50%;transform:translate(-50%,-50%);max-width:480px;width:calc(100vw - 48px);background:#fff;box-shadow:0 4px 24px rgba(0,0,0,.15);padding:16px 20px;border-radius:8px;font-size:.9rem;line-height:1.5;z-index:200;opacity:0;transition:opacity .3s;pointer-events:none}
.hex-prose.vis{opacity:1;pointer-events:auto}
.hx-close{float:right;cursor:pointer;color:#aaa;margin-left:8px;font-size:1rem}
.hex-toast{position:fixed;top:20px;right:20px;background:#2d8a4e;color:#fff;padding:10px 16px;border-radius:6px;font-size:.85rem;z-index:300;opacity:0;transition:opacity .3s;pointer-events:none;max-width:320px;white-space:pre-wrap}
.hex-toast.vis{opacity:1}
.hex-annotate{font-size:.72rem;color:#888;margin-top:2px;display:block}
@keyframes hexGlow{0%,66%{box-shadow:none}33%,100%{box-shadow:0 0 12px rgba(45,138,78,.4)}}
.hex-hl{animation:hexGlow 2s ease forwards}
@media(max-width:768px){main{padding:16px 14px 88px}.heroes,.signals{grid-template-columns:1fr}.hscore{font-size:3.2rem}.srow{min-height:44px}footer{padding:7px 14px;gap:12px;font-size:.72rem}.pm-pill{bottom:20px;right:16px}.pm-stark{width:calc(100vw - 24px);top:auto;bottom:96px;left:12px;right:12px;transform:none}.pm-stark input{border-radius:4px}.init-owner,.init-dots{display:none}.msg-panel{width:100%}.msg-cnt{right:auto;left:calc(50% + 34px);bottom:22px}#boi-rows,.boi-more{display:none}.hex-prose{left:12px;right:12px;width:auto;max-width:none;transform:none;top:auto;bottom:96px}.hex-toast{left:12px;right:12px;max-width:none}}
.init-row{display:flex;align-items:center;gap:8px;padding:7px 0;border-bottom:1px solid #ede8e0}.init-row:last-child{border-bottom:none}.init-name{font-size:.82rem;flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.init-owner{font-size:.75rem;color:#aaa;min-width:80px}.init-dots{letter-spacing:2px;min-width:64px;font-size:.75rem}.init-frac{font-size:.75rem;color:#888;min-width:40px;text-align:right}.init-exp{font-size:.72rem;color:#888;min-width:80px;text-align:right}
.exp-toggle{font-size:.8rem;color:#888;padding:8px 0;cursor:pointer;user-select:none}.exp-toggle:hover{color:#444}.exp-list{display:none}.exp-list.open{display:block}
.exp-row{display:flex;align-items:center;gap:8px;padding:6px 0;border-bottom:1px solid #ede8e0;font-size:.8rem}.exp-row:last-child{border-bottom:none}.exp-id{color:#aaa;min-width:56px;font-size:.72rem}.exp-title{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:#555}.exp-st{min-width:80px;font-size:.7rem;font-weight:600;text-align:right}.exp-met{min-width:80px;font-size:.72rem;color:#888;text-align:right}

.msg-panel{position:fixed;right:0;top:44px;bottom:44px;width:320px;background:#fff;border-left:1px solid #e0dcd6;z-index:150;transform:translateX(100%);transition:transform 200ms ease;display:flex;flex-direction:column}.msg-panel.open{transform:translateX(0)}
.msg-phdr{display:flex;align-items:center;justify-content:space-between;padding:12px 16px;border-bottom:1px solid #e0dcd6;flex-shrink:0}.msg-ptitle{font-size:.75rem;font-weight:600;color:#888;text-transform:uppercase;letter-spacing:.08em}.msg-pclose{cursor:pointer;color:#aaa;font-size:1.2rem;line-height:1}.msg-pclose:hover{color:#333}
.msg-list{flex:1;overflow-y:auto;padding:12px;display:flex;flex-direction:column;gap:10px}.msg-item{padding:10px 12px;border-radius:6px;background:#faf8f5;border:1px solid #ede8e0}.msg-txt{font-size:.875rem;color:#1a1a1a;word-break:break-word;line-height:1.4}.msg-meta{font-size:.68rem;color:#999;margin-top:5px;display:flex;align-items:center;gap:5px}
.ms-dot{width:6px;height:6px;border-radius:50%;display:inline-block;flex-shrink:0;background:#ccc}.ms-dot.processing{background:#e6a817;animation:msDot 1s infinite}.ms-dot.responded{background:#2d8a4e}.ms-dot.failed{background:#c62828}@keyframes msDot{0%,100%{opacity:1}50%{opacity:.35}}
.msg-preview{margin-top:8px;padding:5px 8px;border-left:2px solid #e0dcd6;font-size:.75rem;color:#666;overflow:hidden;display:-webkit-box;-webkit-line-clamp:3;-webkit-box-orient:vertical;line-height:1.45}.msg-badge{font-size:.65rem;background:#f0ede8;color:#888;padding:1px 5px;border-radius:3px;font-weight:600;margin-right:3px}.msg-badge-dispatch{background:#e6f4ec;color:#2d8a4e}
.msg-cnt{position:fixed;bottom:28px;right:88px;min-width:22px;height:22px;border-radius:11px;background:#444;color:#fff;font-size:.65rem;display:none;align-items:center;justify-content:center;cursor:pointer;z-index:101;font-weight:700;padding:0 5px}.msg-cnt.vis{display:flex}
#boi-section{font-size:.82rem}
.boi-hdr{font-size:.7rem;text-transform:uppercase;letter-spacing:.1em;color:#999;margin-bottom:8px}
.boi-summary{font-size:.75rem;color:#888;margin-bottom:10px;padding-bottom:6px;border-bottom:1px solid #ede8e0}
.boi-row{display:flex;align-items:center;gap:8px;padding:6px 0;border-bottom:1px solid #ede8e0;font-size:.8rem}.boi-row:last-child{border-bottom:none}
.boi-status{font-size:.85rem;font-weight:700;min-width:14px;text-align:center}
.boi-id{font-family:monospace;color:#aaa;min-width:52px;font-size:.75rem}
.boi-title{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:#555}
.boi-prog{font-size:.75rem;color:#888;min-width:28px;text-align:right}
.boi-meta{font-size:.72rem;color:#aaa;min-width:60px;text-align:right}
.boi-more{font-size:.75rem;color:#aaa;padding:6px 0}

#tests-section{font-size:.82rem}
.tests-hdr{font-size:.7rem;text-transform:uppercase;letter-spacing:.1em;color:#999;margin-bottom:8px}
.tests-bar{height:8px;border-radius:4px;background:#ede8e0;overflow:hidden;margin-bottom:10px;display:flex}
.tests-bar-pass{background:#2d8a4e;height:100%}
.tests-bar-fail{background:#c62828;height:100%}
.tests-bar-skip{background:#e6a817;height:100%}
.tests-counts{display:flex;gap:16px;font-size:.78rem;margin-bottom:8px;flex-wrap:wrap}
.tests-count-item{display:flex;align-items:center;gap:4px}
.tests-count-dot{width:8px;height:8px;border-radius:50%;flex-shrink:0}
.tests-failing{margin-top:8px}
.tests-failing-toggle{font-size:.78rem;color:#c62828;cursor:pointer;user-select:none;padding:3px 0}.tests-failing-toggle:hover{color:#9b1c1c}
.tests-failing-list{display:none}.tests-failing-list.open{display:block}
.tests-failing-item{font-size:.78rem;color:#c62828;padding:3px 0;border-bottom:1px solid #f5ede0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.tests-failing-item:last-child{border-bottom:none}
.tests-ts{font-size:.72rem;color:#aaa;margin-top:6px}
.tests-placeholder{font-size:.8rem;color:#aaa;padding:8px 0;display:block}
.tests-refresh-btn{background:none;border:none;cursor:pointer;font-size:.85rem;color:#ccc;padding:0 0 0 5px;vertical-align:middle;line-height:1}.tests-refresh-btn:hover{color:#555}.tests-refresh-btn:disabled{opacity:.4;cursor:default}
</style>
</head>
<body>
<header>
  <h1>hex pulse</h1>
  <div class="hdr-r">
    <span class="rc" id="rc">reconnecting…</span>
    <span id="age">—</span>
    <div class="pdot" id="pdot"></div>
  </div>
</header>

<main>
  <section class="heroes">
    <div class="hero">
      <div class="hlabel">System Productivity</div>
      <div class="hscore muted" id="productivity_score">—</div>
      <div class="hq">≥80 healthy</div>
    </div>
    <div class="hero">
      <div class="hlabel">Mike in the Loop</div>
      <div class="hscore muted" id="loop_score">—</div>
      <div class="hq">≤20 healthy · lower is better</div>
    </div>
  </section>

  <section class="signals">
    <div class="scol">
      <h3>Productivity</h3>
      <div class="srow"><span class="sname">Completion rate</span>
        <div class="sright"><div class="dot" id="d-cr"></div><span class="sval" id="v-cr">—</span></div></div>
      <div class="srow"><span class="sname">Task throughput (24h)</span>
        <div class="sright"><div class="dot" id="d-tp"></div><span class="sval" id="v-tp">—</span></div></div>
      <div class="srow"><span class="sname">Zero-task failures</span>
        <div class="sright"><div class="dot" id="d-ztf"></div><span class="sval" id="v-ztf">—</span></div></div>
    </div>
    <div class="scol">
      <h3>Experience</h3>
      <div class="srow"><span class="sname">Frustration signals</span>
        <div class="sright"><div class="dot" id="d-fr"></div><span class="sval" id="v-fr">—</span></div></div>
      <div class="srow"><span class="sname">Feedback recurrence</span>
        <div class="sright"><div class="dot" id="d-frc"></div><span class="sval" id="v-frc">—</span></div></div>
      <div class="srow"><span class="sname">Active loops</span>
        <div class="sright"><div class="dot" id="d-lp"></div><span class="sval" id="v-lp">—</span></div></div>
      <div class="srow"><span class="sname">Unverified done-claims</span>
        <div class="sright"><div class="dot" id="d-dc"></div><span class="sval" id="v-dc">—</span></div></div>
      <div class="srow"><span class="sname">Duplicate sessions</span>
        <div class="sright"><div class="dot" id="d-sa"></div><span class="sval" id="v-sa">—</span></div></div>
    </div>
  </section>

  <section class="signals" id="quality-section">
    <div class="scol" style="grid-column:1/-1">
      <h3>Quality</h3>
    </div>
    <div class="scol">
      <div class="srow"><span class="sname">Gaming detected</span>
        <div class="sright"><div class="dot" id="d-qgm"></div><span class="sval" id="v-qgm">—</span></div></div>
      <div class="srow"><span class="sname">KRs reverted</span>
        <div class="sright"><div class="dot" id="d-qkr"></div><span class="sval" id="v-qkr">—</span></div></div>
    </div>
    <div class="scol">
      <div class="srow"><span class="sname">Velocity anomalies</span>
        <div class="sright"><div class="dot" id="d-qva"></div><span class="sval" id="v-qva">—</span></div></div>
      <div class="srow"><span class="sname">Quality score</span>
        <div class="sright"><div class="dot" id="d-qsc"></div><span class="sval" id="v-qsc">—</span></div></div>
    </div>
  </section>

  <section id="boi-section">
    <div class="boi-hdr">BOI Queue</div>
    <div class="boi-summary" id="boi-summary">—</div>
    <div id="boi-rows"></div>
  </section>

  <section id="tests-section">
    <div class="tests-hdr">Tests <button class="tests-refresh-btn" id="tests-refresh-btn" title="Refresh test results">↻</button></div>
    <div id="tests-content"><span class="tests-placeholder">—</span></div>
  </section>

  <section id="init-section">
    <div style="font-size:.7rem;text-transform:uppercase;letter-spacing:.1em;color:#999;margin-bottom:10px">Initiatives</div>
    <div id="init-rows">—</div>
    <div class="exp-toggle" id="exp-toggle">▸ Experiments (— total)</div>
    <div class="exp-list" id="exp-list"></div>
  </section>
</main>

<footer>
  Agents: <b id="f-agents">—</b>
  &nbsp;·&nbsp; Wakes/24h: <b id="f-wakes">—</b>
  &nbsp;·&nbsp; Cost/24h: $<b id="f-cost">—</b>
</footer>

<button class="pm-pill" id="pm-pill" aria-label="Ask hex">+</button>
<div class="pm-overlay" id="pm-overlay"></div>
<div class="pm-stark" id="pm-stark">
  <input type="text" id="pm-input" maxlength="500" placeholder="Ask hex anything…" autocomplete="off"/>
  <div class="pm-hint" id="pm-hint">Enter to send · Esc to close</div>
</div>
<div class="msg-cnt" id="msg-cnt" title="Messages this session"></div>
<div class="msg-panel" id="msg-panel">
  <div class="msg-phdr">
    <span class="msg-ptitle">Messages</span>
    <span class="msg-pclose" id="msg-pclose">&#215;</span>
  </div>
  <div class="msg-list" id="msg-list"></div>
</div>

<script>
var lastTs = null;
var C = {green:'#2d8a4e', amber:'#e6a817', red:'#c62828', muted:'#bbb'};
var msgs=[],_pendingSends=[],msgOpen=false;

function col(k){ return C[k]||'#1a1a1a'; }

function setVal(id, v, k){
  var el=document.getElementById(id); if(!el) return;
  el.textContent = (v===null||v===undefined)?'—':v;
  el.style.color = (v===null||v===undefined)?C.muted:col(k||'');
}
function setDot(id, k){
  var el=document.getElementById(id); if(el) el.style.background=col(k);
}
function ps(s){ return s>=80?'green':s>=60?'amber':'red'; }
function ls(s){ return s<=20?'green':s<=50?'amber':'red'; }

var _expOpen=false,_exps=[],_testsFailOpen=false;
document.getElementById('exp-toggle').addEventListener('click',function(){_expOpen=!_expOpen;document.getElementById('exp-list').classList.toggle('open',_expOpen);renderExpHdr(_exps);});
var ESC={ACTIVE:'#2d8a4e',MEASURING:'#e6a817',VERDICT_PASS:'#2d8a4e',VERDICT_FAIL:'#c62828',DRAFT:'#aaa',BASELINE:'#4a90d9'};
function renderExpHdr(list){var a=(list||[]).filter(function(e){return e.status==='ACTIVE';}).length,dr=(list||[]).filter(function(e){return e.status==='DRAFT';}).length,ar=_expOpen?'▾':'▸';document.getElementById('exp-toggle').textContent=ar+' Experiments ('+(list||[]).length+' total: '+a+' active, '+dr+' draft)';}
function renderExps(list){var h='';(list||[]).forEach(function(e){var c=ESC[e.status]||'#aaa',t=(e.title||e.id||'').substring(0,45),m=[e.primary_metric_baseline!=null?'baseline: '+e.primary_metric_baseline:'',e.primary_metric_current!=null?'current: '+e.primary_metric_current:''].filter(Boolean).join(' · ');h+='<div class="exp-row"><span class="exp-id">'+(e.id||'')+'</span><span class="exp-title" title="'+(e.title||'').replace(/"/g,'&#34;')+'">'+ t+'</span><span class="exp-st" style="color:'+c+'">'+(e.status||'')+'</span><span class="exp-met">'+m+'</span></div>';});document.getElementById('exp-list').innerHTML=h||'<div style="padding:8px 0;font-size:.8rem;color:#aaa">No experiments</div>';}
function iScore(i){var n=Date.now(),h=i.horizon?new Date(i.horizon).getTime():null,p=i.krs_total>0?i.krs_met/i.krs_total:0;if(h&&(h-n)<14*86400*1000&&p<0.5)return 0;if(i._ae>0)return 1;if(p>0.5)return 2;return 3;}
var ICL=['#c62828','#e6a817','#2d8a4e','#bbb'];
function renderInits(data,exps){var list=(data||{}).initiatives||[],eb={};(exps||[]).forEach(function(e){if(e.initiative){if(!eb[e.initiative])eb[e.initiative]={a:0,t:0};eb[e.initiative].t++;if(e.status==='ACTIVE')eb[e.initiative].a++;}});list=list.map(function(i){var ec=eb[i.id]||{a:0,t:0};return Object.assign({},i,{_ae:ec.a,_et:ec.t});});list.sort(function(a,b){return iScore(a)-iScore(b);});var h='';list.slice(0,10).forEach(function(i){var c=ICL[iScore(i)],d='';for(var k=0;k<i.krs_total;k++)d+=k<i.krs_met?'●':'○';h+='<div class="init-row"><span class="init-name" style="color:'+c+'" title="'+(i.name||i.id||'').replace(/"/g,'&#34;')+'">'+(i.name||i.id||'').substring(0,35)+'</span><span class="init-owner">'+(i.owner||'')+'</span><span class="init-dots">'+d+'</span><span class="init-frac">'+i.krs_met+'/'+i.krs_total+' KRs</span><span class="init-exp">'+(i._et>0?(i._ae+' active'):'no exp')+'</span></div>';});if(list.length>10)h+='<div style="font-size:.75rem;color:#aaa;padding:6px 0">+ '+(list.length-10)+' more</div>';document.getElementById('init-rows').innerHTML=h||'<div style="font-size:.8rem;color:#aaa;padding:8px 0">No initiatives</div>';}

function renderTests(td){
  var el=document.getElementById('tests-content');
  if(!el)return;
  if(!td||td.available===false){
    el.innerHTML='<span class="tests-placeholder">No data — run tests</span>';
    return;
  }
  var total=td.total||0,passed=td.passed||0,failed=td.failed||0,skipped=td.skipped||0;
  var passW=total>0?((passed/total)*100).toFixed(1):0;
  var failW=total>0?((failed/total)*100).toFixed(1):0;
  var skipW=total>0?((skipped/total)*100).toFixed(1):0;
  var failList=td.failing_tests||[];
  var ts=td.last_run_ts?new Date(td.last_run_ts).toLocaleString():'—';
  var h='';
  if(total>0){
    h+='<div class="tests-bar"><div class="tests-bar-pass" style="width:'+passW+'%"></div><div class="tests-bar-fail" style="width:'+failW+'%"></div><div class="tests-bar-skip" style="width:'+skipW+'%"></div></div>';
  }
  h+='<div class="tests-counts">'
    +'<span class="tests-count-item"><span class="tests-count-dot" style="background:#2d8a4e"></span>'+passed+' passed</span>'
    +'<span class="tests-count-item"><span class="tests-count-dot" style="background:#c62828"></span>'+failed+' failed</span>'
    +(skipped>0?'<span class="tests-count-item"><span class="tests-count-dot" style="background:#e6a817"></span>'+skipped+' skipped</span>':'')
    +'<span style="color:#aaa;font-size:.75rem">'+total+' total</span>'
    +'</div>';
  if(failList.length>0){
    var ar=_testsFailOpen?'▾':'▸';
    h+='<div class="tests-failing">'
      +'<div class="tests-failing-toggle" id="tests-fail-toggle">'+ar+' '+failList.length+' failing test'+(failList.length!==1?'s':'')+'</div>'
      +'<div class="tests-failing-list'+(  _testsFailOpen?' open':'')+'\" id="tests-fail-list\">';
    failList.slice(0,20).forEach(function(n){h+='<div class="tests-failing-item">✗ '+escH(n)+'</div>';});
    if(failList.length>20)h+='<div style="font-size:.72rem;color:#aaa">+ '+(failList.length-20)+' more</div>';
    h+='</div></div>';
  }
  h+='<div class="tests-ts">Last run: '+escH(ts)+'</div>';
  el.innerHTML=h;
  var tog=document.getElementById('tests-fail-toggle');
  if(tog){tog.addEventListener('click',function(){_testsFailOpen=!_testsFailOpen;var lst=document.getElementById('tests-fail-list');if(lst)lst.classList.toggle('open',_testsFailOpen);tog.textContent=(_testsFailOpen?'▾':'▸')+' '+failList.length+' failing test'+(failList.length!==1?'s':'');});}
}

function refreshTests(){
  var btn=document.getElementById('tests-refresh-btn');
  if(btn)btn.disabled=true;
  fetch(base+'/api/tests/refresh').then(function(r){return r.json();}).then(function(td){
    renderTests(td);
    if(btn)btn.disabled=false;
  }).catch(function(){if(btn)btn.disabled=false;});
}
document.getElementById('tests-refresh-btn').addEventListener('click',refreshTests);
refreshTests();

function renderBoi(bq){
  var sumEl=document.getElementById('boi-summary');
  var rowsEl=document.getElementById('boi-rows');
  if(!bq||!bq.available){sumEl.textContent='BOI unavailable';rowsEl.innerHTML='';return;}
  var s=bq.summary||{};
  var parts=[];
  if(s.active!=null)parts.push(s.active+' active');
  if(s.queued!=null)parts.push(s.queued+' queued');
  if(s.completed_1h!=null)parts.push(s.completed_1h+' completed/1h');
  if(s.success_rate!=null)parts.push(Math.round(s.success_rate*100)+'% success');
  sumEl.textContent=parts.join(' · ')||'—';
  var rows=[];
  (bq.active||[]).forEach(function(r){rows.push({st:'active',d:r});});
  (bq.queued||[]).forEach(function(r){rows.push({st:'queued',d:r});});
  (bq.recent_completed||[]).forEach(function(r){rows.push({st:'completed',d:r});});
  (bq.recent_failed||[]).forEach(function(r){rows.push({st:'failed',d:r});});
  var visible=rows.slice(0,10),extra=rows.length-visible.length,h='';
  visible.forEach(function(item){
    var r=item.d,ico,clr;
    if(item.st==='active'){ico='▸';clr='#2d8a4e';}
    else if(item.st==='queued'){ico='○';clr='#aaa';}
    else if(item.st==='completed'){ico='✓';clr='#bbb';}
    else{ico='✗';clr='#c62828';}
    var prog=(r.tasks_done!=null&&r.tasks_total!=null)?r.tasks_done+'/'+r.tasks_total:'';
    var meta='';
    if(item.st==='active')meta=r.duration||'';
    else if(item.st==='queued')meta=r.blocked_by?'after '+r.blocked_by:'queued';
    else if(item.st==='completed')meta=r.completed_ago?r.completed_ago+' ago':'';
    else meta=r.failed_ago?r.failed_ago+' ago':'';
    var title=(r.title||r.id||'').substring(0,40);
    h+='<div class="boi-row"><span class="boi-status" style="color:'+clr+'">'+ico+'</span>'
      +'<span class="boi-id">'+escH(r.id||'')+'</span>'
      +'<span class="boi-title" title="'+escH(r.title||'')+'">'+escH(title)+'</span>'
      +'<span class="boi-prog">'+escH(prog)+'</span>'
      +'<span class="boi-meta">'+escH(meta)+'</span></div>';
  });
  if(extra>0)h+='<div class="boi-more">+ '+extra+' more queued</div>';
  if(!h)h='<div style="font-size:.8rem;color:#aaa;padding:8px 0">No active specs</div>';
  rowsEl.innerHTML=h;
}

function update(d){
  lastTs=new Date();
  // Hero scores
  var p=d.productivity_score, l=d.loop_score;
  var pEl=document.getElementById('productivity_score');
  pEl.textContent=(p!=null)?Math.round(p):'—';
  pEl.style.color=(p!=null)?col(ps(p)):C.muted;
  var lEl=document.getElementById('loop_score');
  lEl.textContent=(l!=null)?Math.round(l):'—';
  lEl.style.color=(l!=null)?col(ls(l)):C.muted;

  // Productivity sub-signals
  var sig=((d.productivity||{}).signals)||{};
  var cr=(sig.completion_rate||{}).value;
  if(cr!=null){var ck=cr>=.8?'green':cr>=.6?'amber':'red';setVal('v-cr',Math.round(cr*100)+'%',ck);setDot('d-cr',ck);}
  var tp=(sig.task_throughput||{}).value;
  if(tp!=null){var tk=tp>=20?'green':tp>=5?'amber':'red';setVal('v-tp',tp,tk);setDot('d-tp',tk);}
  var ztf=(sig.zero_task_failures||{}).value;
  if(ztf!=null){var zk=ztf===0?'green':ztf<=2?'amber':'red';setVal('v-ztf',ztf,zk);setDot('d-ztf',zk);}

  // Experience sub-signals
  var ue=d.user_experience||{};
  var fr=(ue.frustration||{}).count;
  if(fr!=null){var fk=fr===0?'green':fr<=3?'amber':'red';setVal('v-fr',fr,fk);setDot('d-fr',fk);}
  var frc=(ue.feedback_recurrence||{}).high_recurrence_count;
  if(frc!=null){var rk=frc===0?'green':frc<=2?'amber':'red';setVal('v-frc',frc,rk);setDot('d-frc',rk);}
  var lp=(ue.loops||{}).count;
  if(lp!=null){var lk=lp===0?'green':lp<=3?'amber':'red';setVal('v-lp',lp,lk);setDot('d-lp',lk);}
  var dc=ue.done_claims||{};
  if(dc.total!=null){var u=dc.total?Math.round((1-dc.verified_rate)*dc.total):0;var dk=u===0?'green':u<=2?'amber':'red';setVal('v-dc',u,dk);setDot('d-dc',dk);}
  var sa=(ue.session_anomalies||{}).count;
  if(sa!=null){var sk=sa===0?'green':sa<=2?'amber':'red';setVal('v-sa',sa,sk);setDot('d-sa',sk);}

  // Fleet
  var fl=d.fleet||{};
  document.getElementById('f-agents').textContent=fl.agent_count!=null?fl.agent_count:'—';
  document.getElementById('f-wakes').textContent=fl.total_wakes!=null?fl.total_wakes:'—';
  document.getElementById('f-cost').textContent=fl.total_cost!=null?fl.total_cost.toFixed(2):'—';

  // Quality metrics
  var q=d.quality||{};
  if(q.available===false){
    ['v-qgm','v-qkr','v-qva','v-qsc'].forEach(function(id){setVal(id,null,null);});
    ['d-qgm','d-qkr','d-qva','d-qsc'].forEach(function(id){setDot(id,'');});
  } else if(q.available){
    var gm=q.gaming_detected;if(gm!=null){var gk=gm===0?'green':'red';setVal('v-qgm',gm,gk);setDot('d-qgm',gk);}
    var kr=q.krs_reverted;if(kr!=null){var kk=kr===0?'green':'red';setVal('v-qkr',kr,kk);setDot('d-qkr',kk);}
    var va=q.velocity_anomalies;if(va!=null){var vk=va<=3?'green':'amber';setVal('v-qva',va,vk);setDot('d-qva',vk);}
    var qs=q.quality_score_pct;if(qs!=null){var qk=qs>=90?'green':qs>=70?'amber':'red';setVal('v-qsc',qs+'%',qk);setDot('d-qsc',qk);}
  }

  // BOI queue
  renderBoi(d.boi_queue);

  // Tests
  renderTests(d.tests);

  // Initiatives & experiments
  _exps=(d.experiments||{}).experiments||[];renderInits(d.initiatives,_exps);renderExpHdr(_exps);renderExps(_exps);

  // Pulse dot blink
  var dot=document.getElementById('pdot');
  dot.classList.remove('on'); void dot.offsetWidth; dot.classList.add('on');
}

var base=location.pathname.replace(/[/]$/,'');
var es=new EventSource(base+'/api/stream');
es.onmessage=function(e){
  try{update(JSON.parse(e.data));}catch(_){}
  document.getElementById('rc').classList.remove('vis');
};
es.onerror=function(){ document.getElementById('rc').classList.add('vis'); };

setInterval(function(){
  if(!lastTs) return;
  var s=Math.round((Date.now()-lastTs)/1000);
  document.getElementById('age').textContent='Updated '+s+'s ago';
},1000);

// Message log panel
function escH(s){return(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');}
function openMsgs(){if(msgOpen)return;msgOpen=true;document.getElementById('msg-panel').classList.add('open');}
function closeMsgs(){if(!msgOpen)return;msgOpen=false;document.getElementById('msg-panel').classList.remove('open');}
function updMsgCnt(){var n=msgs.length,e=document.getElementById('msg-cnt');e.textContent=n?String(n):'';n?e.classList.add('vis'):e.classList.remove('vis');}
function renderMsg(m){
  var el=document.getElementById('mi-'+m.id);
  if(!el){el=document.createElement('div');el.className='msg-item';el.id='mi-'+m.id;var lst=document.getElementById('msg-list');lst.insertBefore(el,lst.firstChild);}
  var ts=m.ts.toLocaleTimeString([],{hour:'2-digit',minute:'2-digit'});
  var sl={sending:'sending…',received:'received',processing:'processing…',responded:'responded',failed:'failed'}[m.status]||m.status;
  var dc=(m.status==='sending'||m.status==='received')?'':'ms-dot '+m.status;
  var sdot='<span class="ms-dot '+(m.status==='sending'?'':m.status)+'"></span>';
  var prev='';
  if(m.status==='responded'&&m.resp){
    var badge,pt;
    if(m.resp.effect_type==='directive'){
      var sid=m.resp.spec_id||'';
      badge='<span class="msg-badge msg-badge-dispatch">dispatched'+(sid?' as '+escH(sid):'')+'</span>';
      pt=m.resp.spec_title?escH(m.resp.spec_title):escH((m.resp.text||'').substring(0,240));
    }else{
      badge=m.resp.effect_type?'<span class="msg-badge">'+escH(m.resp.effect_type)+'</span>':'';
      pt=m.resp.text?escH(m.resp.text.substring(0,240)):(m.resp.action_result?escH((''+m.resp.action_result).substring(0,240)):'');
    }
    prev='<div class="msg-preview">'+badge+pt+'</div>';
  }else if(m.status==='failed'&&m.error){
    prev='<div class="msg-preview" style="border-left-color:#c62828;color:#c62828">'+escH(m.error)+'</div>';
  }
  el.innerHTML='<div class="msg-txt">'+escH(m.text)+'</div><div class="msg-meta">'+sdot+ts+' \xb7 '+sl+'</div>'+prev;
}
document.getElementById('msg-pclose').addEventListener('click',closeMsgs);
document.getElementById('msg-cnt').addEventListener('click',function(){msgOpen?closeMsgs():openMsgs();});
document.addEventListener('click',function(e){
  if(!msgOpen)return;
  var panel=document.getElementById('msg-panel');
  var cnt=document.getElementById('msg-cnt');
  if(!panel.contains(e.target)&&e.target!==cnt)closeMsgs();
});

// Prompt stark/parked state machine (CP-11)
var pmOpen=false;
function openStark(){
  if(pmOpen)return;
  pmOpen=true;
  document.body.classList.add('pm-open');
  var pill=document.getElementById('pm-pill');
  pill.style.opacity='0';pill.style.pointerEvents='none';
  document.getElementById('pm-overlay').classList.add('active');
  document.getElementById('pm-stark').classList.add('active');
  document.getElementById('pm-input').focus();
}
function closeStark(){
  if(!pmOpen)return;
  pmOpen=false;
  document.body.classList.remove('pm-open');
  var pill=document.getElementById('pm-pill');
  pill.style.opacity='';pill.style.pointerEvents='';
  document.getElementById('pm-overlay').classList.remove('active');
  document.getElementById('pm-stark').classList.remove('active');
  document.getElementById('pm-input').value='';
  document.getElementById('pm-input').blur();
  document.getElementById('pm-hint').textContent='Enter to send · Esc to close';
}
function sendMessage(){
  var txt=document.getElementById('pm-input').value.trim();
  if(!txt)return;
  closeStark();
  var pid='p-'+Date.now();
  var m={id:pid,text:txt,ts:new Date(),status:'sending',resp:null,error:null};
  msgs.unshift(m);renderMsg(m);updMsgCnt();openMsgs();
  _pendingSends.push({pid:pid,text:txt});
  fetch(base+'/api/message',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({text:txt})}).catch(function(){
    for(var i=0;i<msgs.length;i++){if(msgs[i].id===pid){msgs[i].status='failed';msgs[i].error='Failed to send';renderMsg(msgs[i]);break;}}
    _pendingSends=_pendingSends.filter(function(p){return p.pid!==pid;});
  });
}
document.getElementById('pm-pill').addEventListener('click',openStark);
document.getElementById('pm-overlay').addEventListener('click',closeStark);
document.getElementById('pm-input').addEventListener('keydown',function(e){
  if(e.key==='Enter'){sendMessage();}
  if(e.key==='Escape'){closeStark();}
});
document.getElementById('pm-input').addEventListener('input',function(){
  var n=this.value.length;
  document.getElementById('pm-hint').textContent=n?((500-n)+' chars · Enter to send · Esc to close'):'Enter to send · Esc to close';
});
document.addEventListener('keydown',function(e){
  if(!pmOpen&&e.key==='/'&&document.activeElement.tagName!=='INPUT'){e.preventDefault();openStark();}
  if(pmOpen&&e.key==='Escape'){closeStark();}
  if(!pmOpen&&e.key==='Escape'){closeMsgs();}
});
// Response effect renderer (CP-12/13/14, CP-17)
var MNAMES=['Completion rate','Task throughput','Zero-task failures','Frustration signals','Feedback recurrence','Active loops','Unverified done-claims','Duplicate sessions'];
function boldN(t){var s=t.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');MNAMES.forEach(function(n){s=s.replace(new RegExp('('+n+')','gi'),'<b>$1</b>');});return s;}
function mkEl(id,cls){var e=document.getElementById(id);if(!e){e=document.createElement('div');e.id=id;document.body.appendChild(e);}e.className=cls;return e;}
function showProse(txt){var e=mkEl('hx-prose','hex-prose');e.innerHTML=boldN(txt||'');e._p=false;clearTimeout(e._t);e.classList.add('vis');e._t=setTimeout(function(){if(!e._p)e.classList.remove('vis');},6000);e.onclick=function(){if(!e._p){e._p=true;clearTimeout(e._t);var x=document.createElement('span');x.className='hx-close';x.innerHTML='&times;';x.onclick=function(ev){ev.stopPropagation();e.classList.remove('vis');e._p=false;};e.appendChild(x);}};}
function getRow(id){var el=document.getElementById(id);return el?el.closest('.srow'):null;}
function showHL(tid,txt){var row=getRow(tid);if(!row){if(txt)showProse(txt);return;}row.classList.remove('hex-hl');void row.offsetWidth;row.classList.add('hex-hl');setTimeout(function(){row.classList.remove('hex-hl');},2100);if(txt)showAnn(tid,txt);}
function showAnn(tid,txt){var old=document.getElementById('ann-'+tid);if(old)old.remove();var row=getRow(tid);if(!row)return;var a=document.createElement('span');a.className='hex-annotate';a.id='ann-'+tid;a.textContent=txt||'';row.appendChild(a);}
function showToast(txt,res){var e=mkEl('hx-toast','hex-toast');clearTimeout(e._t);e.textContent='✓ '+(txt||'')+(res?' '+res:'');e.classList.add('vis');e._t=setTimeout(function(){e.classList.remove('vis');},4000);}
es.addEventListener('message_ack',function(e){try{
  var d=JSON.parse(e.data);
  var pi=null;
  for(var i=0;i<_pendingSends.length;i++){if(_pendingSends[i].text===d.text){pi=_pendingSends.splice(i,1)[0];break;}}
  if(pi){
    for(var i=0;i<msgs.length;i++){if(msgs[i].id===pi.pid){
      var el=document.getElementById('mi-'+pi.pid);
      if(el)el.id='mi-'+d.id;
      msgs[i].id=d.id;msgs[i].status=d.status||'received';renderMsg(msgs[i]);break;
    }}
  } else {
    var existing=false;for(var i=0;i<msgs.length;i++){if(msgs[i].id===d.id){existing=true;break;}}
    if(!existing){var m={id:d.id,text:d.text,ts:new Date(),status:d.status||'received',resp:null,error:null};msgs.unshift(m);renderMsg(m);updMsgCnt();openMsgs();}
  }
}catch(_){}});
es.addEventListener('message_status',function(e){try{
  var d=JSON.parse(e.data);
  for(var i=0;i<msgs.length;i++){if(msgs[i].id===d.id){msgs[i].status=d.status;if(d.error)msgs[i].error=d.error;renderMsg(msgs[i]);break;}}
}catch(_){}});
es.addEventListener('response',function(e){try{var d=JSON.parse(e.data),f=d.effect_type;
  if(d.message_id){for(var i=0;i<msgs.length;i++){if(msgs[i].id===d.message_id){msgs[i].status='responded';msgs[i].resp=d;renderMsg(msgs[i]);break;}}}
  if(f==='prose')showProse(d.text);else if(f==='highlight')showHL(d.target,d.text);else if(f==='annotate')showAnn(d.target,d.text);else if(f==='action')showToast(d.text,d.action_result);else if(f==='directive'){}else showProse(d.text);}catch(_){}});
var _ou=update;update=function(d){_ou(d);document.querySelectorAll('.hex-annotate').forEach(function(a){a.remove();});};

// Load message history on page load (t-2)
function loadMsgHistory(){
  fetch(base+'/api/messages').then(function(r){return r.json();}).then(function(data){
    var loaded=0;
    data.forEach(function(m){
      var dup=false;for(var i=0;i<msgs.length;i++){if(msgs[i].id===m.id){dup=true;break;}}
      if(dup)return;
      var obj={id:m.id,text:m.text||'',ts:new Date(m.ts),status:m.status||'received',resp:m.response||null,error:m.error||null};
      msgs.push(obj);renderMsg(obj);loaded++;
    });
    if(loaded)updMsgCnt();
  }).catch(function(){});
}
loadMsgHistory();
</script>
</body>
</html>"""


# ── HTTP Handler ──────────────────────────────────────────────────────────────

class PulseHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass  # silence access log

    def do_POST(self):
        path = self.path.split("?")[0]
        if path == "/api/message":
            self._message_endpoint()
        else:
            self._send(404, "text/plain", b"Not found")

    def _message_endpoint(self):
        global _msg_counter
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        try:
            data = json.loads(body)
            text = (data.get("text") or "").strip()
        except (json.JSONDecodeError, AttributeError):
            self._send(400, "application/json", b'{"error":"Invalid JSON"}')
            return
        if not text or len(text) > 500:
            self._send(400, "application/json", b'{"error":"Text must be 1-500 chars"}')
            return
        with _msg_lock:
            _msg_counter += 1
            msg_id = f"msg-{_msg_counter}"
        _append_pulse_record({"id": msg_id, "ts": datetime.now(timezone.utc).isoformat(), "text": text, "status": "received"})
        _push_sse("message_ack", {"id": msg_id, "status": "received", "text": text})
        Thread(target=_handle_message, args=(text, msg_id), daemon=True).start()
        resp = json.dumps({"id": msg_id, "status": "received"}).encode()
        self._send(202, "application/json", resp)

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/":
            self._send(200, "text/html; charset=utf-8", DASHBOARD_HTML.encode())
        elif path == "/api/vitals":
            data = get_all_metrics()
            body = json.dumps(data, indent=2).encode()
            self._send(200, "application/json", body)
        elif path == "/api/messages":
            body = json.dumps(_get_pulse_messages()).encode()
            self._send(200, "application/json", body)
        elif path == "/api/tests":
            td = collect_tests_data()
            api = {
                "pass": td.get("passed", 0),
                "fail": td.get("failed", 0),
                "skip": td.get("skipped", 0),
                "last_run": td.get("last_run_ts"),
                "failures": td.get("failing_tests", []),
            }
            body = json.dumps(api, indent=2).encode()
            self._send(200, "application/json", body)
        elif path == "/api/tests/refresh":
            with _tests_lock:
                _tests_cache["ts"] = 0.0
            td = collect_tests_data()
            body = json.dumps(td).encode()
            self._send(200, "application/json", body)
        elif path == "/api/context":
            body = json.dumps({"recent_effects": _ctx._recent_effects, "count": len(_ctx._recent_effects)}).encode()
            self._send(200, "application/json", body)
        elif path == "/api/stream":
            self._serve_sse()
        else:
            self._send(404, "text/plain", b"Not found")

    def _send(self, code: int, ctype: str, body: bytes):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve_sse(self):
        global _sse_counter
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        q: _queue.Queue = _queue.Queue(maxsize=20)
        with _sse_clients_lock:
            _sse_clients.append(q)
        try:
            while True:
                try:
                    event = q.get(timeout=5)
                    self.wfile.write(event.encode())
                    self.wfile.flush()
                except _queue.Empty:
                    _sse_counter += 1
                    data = get_all_metrics()
                    payload = f"id: {_sse_counter}\ndata: {json.dumps(data)}\n\n"
                    self.wfile.write(payload.encode())
                    self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            with _sse_clients_lock:
                _sse_clients.remove(q)


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    if "--test" in sys.argv:
        data = get_all_metrics()
        print(json.dumps(data))
        sys.exit(0)

    print(f"hex pulse listening on http://127.0.0.1:{PORT}", flush=True)
    ThreadingHTTPServer.allow_reuse_address = True
    server = ThreadingHTTPServer(("127.0.0.1", PORT), PulseHandler)
    server.daemon_threads = True
    server.serve_forever()
