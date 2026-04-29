#!/usr/bin/env python3
"""social-dashboard extension server.
Port: $PORT (default 8899). Data: $HEX_ROOT/projects/brand/*.md + snapshots.jsonl.
"""

import http.server
import json
import os
import re
import socketserver
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

PORT = int(os.environ.get("PORT", 8899))
HEX_ROOT = Path(os.environ.get("AGENT_DIR", os.environ.get("HEX_ROOT", os.path.expanduser("~/hex"))))
BRAND_DIR = HEX_ROOT / "projects" / "brand"
SNAPSHOTS_FILE = BRAND_DIR / "snapshots.jsonl"
VIEWS_DIR = Path(__file__).parent / "views"
DISPLAY_NAME = os.environ.get("HEX_DISPLAY_NAME", "User")
DISPLAY_INITIALS = os.environ.get("HEX_DISPLAY_INITIALS", "U")


# ---------------------------------------------------------------------------
# Data parsers
# ---------------------------------------------------------------------------

def _parse_md_table(text: str) -> list[dict]:
    lines = [l.strip() for l in text.strip().splitlines() if l.strip().startswith("|")]
    if len(lines) < 3:
        return []
    headers = [h.strip() for h in lines[0].strip("|").split("|")]
    rows = []
    for line in lines[2:]:
        cells = [c.strip() for c in line.strip("|").split("|")]
        if len(cells) == len(headers):
            rows.append(dict(zip(headers, cells)))
    return rows


def _num(val: str) -> int | None:
    val = val.strip()
    if not val or val in ("—", "–", "-", "TBD", "N/A"):
        return None
    m = re.search(r"[\d,]+", val)
    return int(m.group().replace(",", "")) if m else None


def _load_posts() -> list[dict]:
    f = BRAND_DIR / "published-log.md"
    if not f.exists():
        return []
    rows = _parse_md_table(f.read_text())
    posts = []
    for r in rows:
        posts.append({
            "date": r.get("Date", ""),
            "platform": r.get("Platform", ""),
            "post_id": r.get("Post ID", "").strip() or None,
            "summary": r.get("Summary", ""),
            "impressions": _num(r.get("Impressions", "")),
            "likes": _num(r.get("Likes", "")),
            "bookmarks": _num(r.get("Bookmarks", "")),
            "experiment": r.get("Experiment", "").strip() or None,
        })
    posts.reverse()
    for p in posts:
        pid = p.get("post_id")
        if pid and pid not in ("—", "–", "-") and p.get("platform", "").upper() in ("X", "X/TWITTER"):
            p["url"] = f"https://x.com/mikerapadas/status/{pid}"
        else:
            p["url"] = None
    return posts[:20]


def _load_experiments() -> list[dict]:
    f = BRAND_DIR / "experiments.md"
    if not f.exists():
        return []
    text = f.read_text()
    exps = []
    for block in re.split(r"(?=^### EXP-)", text, flags=re.MULTILINE):
        m = re.match(r"### (EXP-\d+):\s*(.+)", block)
        if not m:
            continue
        eid, name = m.group(1), m.group(2).strip()
        status_m = re.search(r"\*\*Status:\*\*\s*(\w+)", block)
        status = status_m.group(1) if status_m else "UNKNOWN"
        hyp_m = re.search(r"\*\*Hypothesis:\*\*\s*(.+?)(?:\n\*\*|\n---|\Z)", block, re.DOTALL)
        hypothesis = hyp_m.group(1).strip()[:120] if hyp_m else ""
        plat_m = re.search(r"\*\*Platform:\*\*\s*(.+)", block)
        platform = plat_m.group(1).strip() if plat_m else ""
        launched_m = re.search(r"\*\*Launched:\*\*\s*(\S+)", block)
        launched = launched_m.group(1) if launched_m else None
        ends_m = re.search(r"\*\*Ends:\*\*\s*(\S+)", block)
        ends = ends_m.group(1) if ends_m else None
        trend_m = re.search(r"\*\*Trending:\*\*\s*(\w+)", block)
        if not trend_m:
            trend_m = re.search(r"Trending:\s*(\w+)", block)
        verdict_trending = trend_m.group(1) if trend_m else None
        metric_m = re.search(r"\*\*Data[^*]*\*\*\s*(.+?)(?:\n\*\*|\Z)", block, re.DOTALL)
        metric_summary = metric_m.group(1).strip()[:150] if metric_m else None
        scale_m = re.search(r"SCALE:\s*(.+?)(?:\n|$)", block)
        scale_target = scale_m.group(1).strip() if scale_m else None
        exps.append({
            "id": eid, "name": name, "status": status,
            "hypothesis": hypothesis, "platform": platform,
            "launched": launched, "ends": ends,
            "verdict_trending": verdict_trending,
            "metric_summary": metric_summary,
            "scale_target": scale_target,
        })
    return [e for e in exps if e["status"].upper() in ("RUNNING", "LAUNCHING", "READY")]


def _load_engagement() -> list[dict]:
    f = BRAND_DIR / "engagement-log.md"
    if not f.exists():
        return []
    rows = _parse_md_table(f.read_text())
    items = []
    for r in rows:
        items.append({
            "date": r.get("Date", ""),
            "username": r.get("Username", ""),
            "followers": _num(r.get("Followers", "")),
            "action": r.get("Action", ""),
            "notes": r.get("Notes", "").strip() or None,
        })
    items.reverse()
    return items[:20]


def _load_pipeline() -> dict:
    f = BRAND_DIR / "pipeline.md"
    empty = {"priority_queue": 0, "ready": 0, "in_drafting": 0, "ideas": 0, "product": 0}
    if not f.exists():
        return empty
    text = f.read_text()
    counts = dict(empty)
    return {"counts": counts, "items": _load_pipeline_items(text)}


def _load_pipeline_items(text: str) -> list[dict]:
    """Parse individual posts from pipeline.md with section, status, platform, and full draft content."""
    section_map = {
        "## priority queue": "priority",
        "## product announcements": "product",
        "## in drafting": "drafting",
        "## ready to publish": "ready",
        "## ideas": "ideas",
        "## shelved": "shelved",
    }
    items = []
    blocks = re.split(r"(?=^### [PD]-\d+:)", text, flags=re.MULTILINE)

    line_sections = {}
    sec = None
    for i, line in enumerate(text.splitlines()):
        ll = line.strip().lower()
        for prefix, section_name in section_map.items():
            if ll.startswith(prefix):
                sec = section_name
                break
        else:
            if ll.startswith("## ") and not ll.startswith("## status") and not ll.startswith("## voice") and not ll.startswith("## log"):
                sec = None
        line_sections[i] = sec

    current_line = 0
    for block in blocks:
        m = re.match(r"^### ([PD]-\d+):\s*(.+)", block)
        if not m:
            current_line += block.count("\n")
            continue

        block_start = text.find(block)
        block_line = text[:block_start].count("\n") if block_start >= 0 else current_line
        section = line_sections.get(block_line, "unknown")

        pid = m.group(1)
        title = m.group(2).strip()
        status = None
        platform = None

        for line in block.splitlines():
            stripped = line.strip()
            sm = re.match(r"\*\*Status:\*\*\s*(.+)", stripped)
            if sm:
                raw = sm.group(1).strip()
                status = raw.split("—")[0].split("(")[0].split(".")[0].strip()
            pm = re.match(r"\*\*Platform:\*\*\s*(.+)", stripped)
            if pm:
                platform = pm.group(1).strip()

        draft_lines = []
        in_draft = False
        for line in block.splitlines():
            stripped = line.strip()
            if stripped.startswith("> ") or stripped == ">":
                in_draft = True
                content = stripped[2:] if stripped.startswith("> ") else ""
                draft_lines.append(content)
            elif in_draft and stripped == "":
                draft_lines.append("")
            elif in_draft:
                in_draft = False

        draft_text = "\n".join(draft_lines).strip()
        tweets = []
        if re.search(r"^\d+/\s", draft_text, re.MULTILINE):
            parts = re.split(r"(?=^\d+/\s)", draft_text, flags=re.MULTILINE)
            for part in parts:
                part = part.strip()
                if part:
                    tweets.append(part)
        elif draft_text:
            tweets = [draft_text]

        items.append({
            "id": pid,
            "title": title,
            "section": section or "unknown",
            "status": status,
            "platform": platform,
            "draft": tweets,
        })

    seen = {}
    deduped = []
    for item in items:
        pid = item["id"]
        if pid in seen:
            existing = seen[pid]
            if len(item.get("draft", [])) > len(existing.get("draft", [])):
                deduped[deduped.index(existing)] = item
                seen[pid] = item
        else:
            seen[pid] = item
            deduped.append(item)

    return deduped


def _load_headline(posts, experiments, pipeline_items) -> dict:
    f = BRAND_DIR / "audience-intel.md"
    followers = 480
    if f.exists():
        m = re.search(r"(\d[\d,]*)\s*X/Twitter followers", f.read_text())
        if m:
            followers = int(m.group(1).replace(",", ""))
    total_imp = sum(p.get("impressions") or 0 for p in posts)
    now = datetime.now(timezone.utc)
    week_ago = (now - timedelta(days=7)).strftime("%Y-%m-%d")
    posts_this_week = sum(1 for p in posts if p.get("date", "") >= week_ago)
    spend = 0.0
    sf = BRAND_DIR / "spend-log.md"
    if sf.exists():
        for m2 in re.finditer(r"\$\s*([\d,.]+)", sf.read_text()):
            try:
                spend += float(m2.group(1).replace(",", ""))
            except ValueError:
                pass
    queued = [i for i in pipeline_items if i["section"] in ("priority", "product", "ready", "drafting")]
    return {
        "followers": followers,
        "posts_this_week": posts_this_week,
        "total_impressions": total_imp,
        "active_experiments": len(experiments),
        "pipeline_ready": len(queued),
        "total_spend": spend,
    }


def _load_snapshots() -> list[dict]:
    if not SNAPSHOTS_FILE.exists():
        return []
    snaps = []
    for line in SNAPSHOTS_FILE.read_text().strip().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            snaps.append(json.loads(line))
        except json.JSONDecodeError:
            pass
    return snaps


def _build_trends(posts: list[dict], engagement: list[dict]) -> dict:
    imp_series = []
    for p in reversed(posts):
        if p.get("date") and p.get("impressions") is not None:
            imp_series.append({"date": p["date"], "value": p["impressions"], "label": p["summary"][:40]})

    day_counts = defaultdict(int)
    for p in posts:
        d = p.get("date", "")
        if d:
            day_counts[d] += 1
    sorted_days = sorted(day_counts.keys())
    posts_per_day = [{"date": d, "value": day_counts[d]} for d in sorted_days]

    week_counts = defaultdict(int)
    for p in posts:
        d = p.get("date", "")
        if d:
            try:
                dt = datetime.strptime(d, "%Y-%m-%d")
                week_key = dt.strftime("%Y-W%W")
                week_counts[week_key] += 1
            except ValueError:
                pass
    sorted_weeks = sorted(week_counts.keys())
    posts_per_week = [{"week": w, "value": week_counts[w]} for w in sorted_weeks]

    eng_day_counts = defaultdict(int)
    for e in engagement:
        d = e.get("date", "")
        if d:
            eng_day_counts[d] += 1
    sorted_eng_days = sorted(eng_day_counts.keys())
    eng_per_day = [{"date": d, "value": eng_day_counts[d]} for d in sorted_eng_days]

    snaps = _load_snapshots()
    follower_series = [{"date": s["date"], "value": s["followers"]} for s in snaps if "followers" in s]

    def project(series: list[dict], days_ahead: int = 14) -> list[dict]:
        if len(series) < 2:
            return []
        vals = [(i, s["value"]) for i, s in enumerate(series)]
        n = len(vals)
        sum_x = sum(v[0] for v in vals)
        sum_y = sum(v[1] for v in vals)
        sum_xy = sum(v[0] * v[1] for v in vals)
        sum_xx = sum(v[0] ** 2 for v in vals)
        denom = n * sum_xx - sum_x ** 2
        if denom == 0:
            return []
        slope = (n * sum_xy - sum_x * sum_y) / denom
        intercept = (sum_y - slope * sum_x) / n
        last_date = series[-1].get("date", "")
        try:
            last_dt = datetime.strptime(last_date, "%Y-%m-%d")
        except ValueError:
            return []
        proj = []
        for d in range(1, days_ahead + 1):
            future_dt = last_dt + timedelta(days=d)
            projected_val = intercept + slope * (n - 1 + d)
            proj.append({"date": future_dt.strftime("%Y-%m-%d"), "value": round(max(0, projected_val), 1)})
        return proj

    return {
        "impressions_per_post": imp_series,
        "impressions_projection": project(imp_series, 14),
        "posts_per_day": posts_per_day,
        "posts_per_week": posts_per_week,
        "engagement_per_day": eng_per_day,
        "follower_series": follower_series,
        "follower_projection": project(follower_series, 30) if len(follower_series) >= 2 else [],
        "goals": {
            "impressions_per_post": 500,
            "followers": 530,
            "posts_per_week": 5,
        },
    }


def _build_data() -> dict:
    posts = _load_posts()
    experiments = _load_experiments()
    engagement = _load_engagement()
    pipeline = _load_pipeline()
    pipeline_items = pipeline.get("items", [])
    headline = _load_headline(posts, experiments, pipeline_items)
    trends = _build_trends(posts, engagement)

    def _is_queued(item):
        s = (item.get("status") or "").upper()
        if s.startswith("PUBLISHED") or s.startswith("PROMOTED"):
            return False
        return item["section"] in ("priority", "product", "ready", "drafting")

    queued = [i for i in pipeline_items if _is_queued(i)]
    pipeline_counts = defaultdict(int)
    for item in pipeline_items:
        pipeline_counts[item["section"]] += 1
    return {
        "headline": headline,
        "posts": posts,
        "experiments": experiments,
        "engagement": engagement,
        "pipeline": dict(pipeline_counts),
        "queued": queued,
        "trends": trends,
    }


def _take_snapshot():
    posts = _load_posts()
    experiments = _load_experiments()
    engagement = _load_engagement()
    pipeline = _load_pipeline()
    pipeline_items = pipeline.get("items", [])
    headline = _load_headline(posts, experiments, pipeline_items)
    snap = {
        "date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "ts": datetime.now(timezone.utc).isoformat(),
        "followers": headline["followers"],
        "total_impressions": headline["total_impressions"],
        "posts_this_week": headline["posts_this_week"],
        "active_experiments": headline["active_experiments"],
        "total_posts": len(posts),
        "total_engagement_actions": len(engagement),
        "pipeline_ready": headline["pipeline_ready"],
    }
    existing = _load_snapshots()
    today = snap["date"]
    if any(s.get("date") == today for s in existing):
        return {"status": "already_captured", "date": today}
    with open(SNAPSHOTS_FILE, "a") as f:
        f.write(json.dumps(snap) + "\n")
    return {"status": "captured", "date": today, "snapshot": snap}


def _render_post_page(post_id: str) -> str:
    import html as _html
    pipeline = _load_pipeline()
    items = pipeline.get("items", [])
    posts = _load_posts()

    pipe_item = next((i for i in items if i["id"] == post_id), None)
    pub_item = next((p for p in posts if p.get("summary", "").startswith(f"{post_id}:") or
                     (pipe_item and p.get("post_id") and pipe_item["title"][:20] in p.get("summary", ""))),
                    None)

    if not pipe_item and not pub_item:
        return f"<html><body style='font-family:Work Sans,sans-serif;padding:3rem;background:#faf7f0'><p>Post {_html.escape(post_id)} not found.</p><a href='../'>Back</a></body></html>"

    title = pipe_item["title"] if pipe_item else pub_item.get("summary", post_id)
    platform = (pipe_item or {}).get("platform", "") or (pub_item or {}).get("platform", "")
    status = (pipe_item or {}).get("status", "")
    section = (pipe_item or {}).get("section", "")
    drafts = (pipe_item or {}).get("draft", [])

    x_url = None
    pub_data = None
    x_handle = "mikerapadas"
    for p in posts:
        pid = p.get("post_id")
        if pid and pid not in ("—", "-"):
            if post_id in p.get("summary", "") or (pipe_item and pipe_item["title"][:25] in p.get("summary", "")):
                x_url = f"https://x.com/{x_handle}/status/{pid}"
                pub_data = p
                break

    h = _html.escape
    section_colors = {
        "priority": "#c4553a", "product": "#b85c14",
        "ready": "#2d7a3a", "drafting": "#8a8880",
    }
    badge_color = section_colors.get(section, "#8a8880")

    tweets_html = ""
    for i, tweet in enumerate(drafts):
        lines = tweet.strip().split("\n")
        formatted = "<br>".join(h(l) for l in lines)
        tweets_html += f"""
        <div style="background:#fff;border:1px solid #e8e0d0;border-radius:10px;padding:18px 22px;margin-bottom:10px;position:relative">
            <div style="display:flex;align-items:center;gap:10px;margin-bottom:10px">
                <div style="width:36px;height:36px;border-radius:50%;background:#1c1a16;display:flex;align-items:center;justify-content:center;color:#faf7f0;font-family:Fraunces,serif;font-weight:700;font-size:0.85rem">MR</div>
                <div>
                    <div style="font-weight:600;font-size:0.88rem">{DISPLAY_NAME}</div>
                    <div style="font-size:0.75rem;color:#8a8880">@{x_handle}</div>
                </div>
                <div style="margin-left:auto;font-size:1.1rem">𝕏</div>
            </div>
            <div style="font-size:0.95rem;line-height:1.55;white-space:pre-line">{formatted}</div>
        </div>"""

    metrics_html = ""
    if pub_data:
        imp = pub_data.get("impressions")
        likes = pub_data.get("likes")
        bk = pub_data.get("bookmarks")
        metrics_html = f"""
        <div style="display:flex;gap:20px;margin:16px 0;font-size:0.85rem;color:#6e695f">
            <span><strong>{imp if imp is not None else '—'}</strong> impressions</span>
            <span><strong>{likes if likes is not None else '—'}</strong> likes</span>
            <span><strong>{bk if bk is not None else '—'}</strong> bookmarks</span>
        </div>"""

    link_html = ""
    if x_url:
        link_html = f'<a href="{x_url}" target="_blank" style="display:inline-block;margin:12px 0;padding:8px 16px;background:#1c1a16;color:#faf7f0;border-radius:8px;text-decoration:none;font-size:0.85rem;font-weight:500">View on 𝕏</a>'

    return f"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{h(post_id)}: {h(title)}</title>
<script src="/comments/widget.js"></script>
<link href="https://fonts.googleapis.com/css2?family=Fraunces:wght@500;700&family=Work+Sans:wght@300;400;500;600&display=swap" rel="stylesheet">
<style>
body {{ background:#faf7f0; color:#1c1a16; font-family:'Work Sans',sans-serif; margin:0; line-height:1.5; }}
.wrap {{ max-width:600px; margin:0 auto; padding:2rem; }}
a.back {{ font-size:0.82rem; color:#8a8880; text-decoration:none; }}
a.back:hover {{ color:#c4553a; }}
</style>
</head><body>
<div class="wrap" data-comment-asset="post:{h(post_id)}" data-comment-label="{h(post_id)}: {h(title[:50])}">
  <a class="back" href="../">&larr; Back to dashboard</a>
  <div style="margin:1.5rem 0 0.5rem;display:flex;align-items:center;gap:10px;flex-wrap:wrap">
    <span style="font-family:Fraunces,serif;font-size:0.9rem;font-weight:700;color:#8a8880">{h(post_id)}</span>
    <span style="font-size:0.68rem;padding:2px 8px;border-radius:4px;font-weight:600;background:#f0ebe3;color:{badge_color}">{h(section.upper() if section else '')}</span>
    {f'<span style="font-size:0.68rem;padding:2px 8px;border-radius:4px;font-weight:600;background:#e8f5e8;color:#2d7a3a">PUBLISHED</span>' if x_url else ''}
  </div>
  <h1 style="font-family:Fraunces,serif;font-size:1.5rem;font-weight:700;letter-spacing:-0.02em;margin:0 0 0.3rem">{h(title)}</h1>
  <div style="font-size:0.82rem;color:#8a8880;margin-bottom:1.5rem">{h(platform)}</div>
  {link_html}
  {metrics_html}
  <div style="margin-top:1rem">
    {tweets_html if tweets_html else '<p style="color:#a09a90;font-style:italic">No draft content available for this post.</p>'}
  </div>
</div>
</body></html>"""


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    def _send(self, code, content_type, body):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?")[0].rstrip("/") or "/"
        if path in ("/", ""):
            html = (VIEWS_DIR / "index.html").read_text()
            self._send(200, "text/html; charset=utf-8", html)
        elif path == "/health":
            self._send(200, "application/json", json.dumps({"status": "ok"}))
        elif path == "/api/data":
            self._send(200, "application/json", json.dumps(_build_data()))
        elif path == "/api/trends":
            posts = _load_posts()
            engagement = _load_engagement()
            self._send(200, "application/json", json.dumps(_build_trends(posts, engagement)))
        elif path == "/api/snapshots":
            self._send(200, "application/json", json.dumps(_load_snapshots()))
        elif path.startswith("/post/"):
            post_id = path[6:]
            self._send(200, "text/html; charset=utf-8", _render_post_page(post_id))
        else:
            self._send(404, "text/plain", "not found")

    def do_POST(self):
        path = self.path.split("?")[0].rstrip("/") or "/"
        if path == "/api/snapshot":
            result = _take_snapshot()
            self._send(200, "application/json", json.dumps(result))
        else:
            self._send(404, "text/plain", "not found")


if __name__ == "__main__":
    with socketserver.TCPServer(("127.0.0.1", PORT), Handler) as httpd:
        httpd.allow_reuse_address = True
        print(f"social-dashboard on http://127.0.0.1:{PORT}", flush=True)
        httpd.serve_forever()
