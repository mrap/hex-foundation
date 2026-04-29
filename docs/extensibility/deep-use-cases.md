# Hex Extensibility — Deep Use Cases

> Stress-tests the proposed architecture (`docs/extensibility/proposal.md` + `docs/extensibility/hex-ui-extensions.md`)
> against 11 real-world extension scenarios. For each: tier, `extension.yaml`, state storage,
> real-time update path, hex primitives used, upgrade story, and what breaks.
> Written 2026-04-27.

---

## Tier Reference

| Tier | What it means | Hex types used |
|------|--------------|----------------|
| **static** | HTML/JS + SSE only. No backend logic, no persistent state beyond existing hex APIs. | `ui_view` |
| **reactive** | Adds event policies + SSE topic producers. No own process. State lives in hex assets or (proposed) extension-owned SQLite tables. | `ui_view` + `policy` + `sse_topic` |
| **full** | Extension brings its own server. Hex proxies, manages lifecycle, registers SSE topics. | `proxy` + user-managed server |

---

## Use Case 1: Git / Hex Activity Feed *(static)*

**Feature:** A real-time view showing the last 50 hex events with topic filtering — a live log-tail UI.

**Tier:** static

**extension.yaml:**
```yaml
name: hex-activity
version: "1.0.0"
type: ui_view
engines:
  hex: ">=0.8.0"
provides:
  views:
    - name: activity
      path: /ext/activity
      entry: views/activity/index.html
      title: "Activity Feed"
      icon: "📋"
      nav: true
      sse_topics:
        - hex.events
        - hex.messages
requires:
  capabilities:
    - events
    - sse
```

**State:** None beyond the browser's in-memory list. On load, fetches recent events via `GET /api/events?limit=50`. Live updates via SSE subscription to `hex.events`.

**Real-time updates:** `hexSSE(['hex.events'], handler)` from `hex-sdk.js`. Each event is prepended to the DOM list.

**Hex primitives used:** `GET /api/events`, `GET /sse?topic=hex.events`

**Upgrade story:** `hex upgrade` never touches `~/.hex/extensions/`. Static assets are always user-space. `hex-sdk.js` is served from the hex binary at `/ext/hex-sdk.js` — views that import it get the new version automatically on next page load after a hex upgrade.

**What breaks:** Nothing. This is the natural home for the static tier.

---

## Use Case 2: Kanban Board with Real-Time Cross-Tab Sync *(reactive)*

**Feature:** Drag-and-drop kanban board where card state syncs across multiple browser tabs instantly. Single-user (personal productivity).

**Tier:** reactive

**extension.yaml:**
```yaml
name: hex-kanban
version: "1.0.0"
engines:
  hex: ">=0.8.0"
provides:
  views:
    - name: kanban
      path: /ext/kanban
      entry: views/kanban/index.html
      title: "Kanban Board"
      icon: "🗂"
      nav: true
      sse_topics:
        - kanban.board.updated
  sse_topics:
    - name: kanban.board.updated
      schema:
        type: object
        properties:
          board_id: { type: string }
          updated_at: { type: string, format: date-time }
  policies:
    - policies/kanban-sync.yaml
requires:
  capabilities:
    - assets
    - events
    - sse
```

**State:** Board state as a JSON blob in hex assets: `POST /api/assets/kanban/board-default`. Each drag-and-drop writes the full board state. No separate database needed for a single board.

**Cross-tab sync flow:**
1. Tab A drops a card → `POST /api/assets/kanban/board-default` with updated JSON
2. That write emits a `hex.asset.changed` event on the event bus
3. Policy `kanban-sync.yaml` reacts → publishes SSE event `kanban.board.updated`
4. Tab B's SSE subscription fires → re-fetches `GET /api/assets/kanban/board-default` → updates DOM

**Policy (kanban-sync.yaml):**
```yaml
name: kanban-sync
rules:
  - name: broadcast-board-change
    trigger:
      event: hex.asset.changed
      filter: "$.key starts_with 'kanban/'"
    actions:
      - type: sse_publish
        topic: kanban.board.updated
        data: '{"board_id": "{{ $.key }}", "updated_at": "{{ $.timestamp }}"}'
```

**Hex primitives used:** assets (read/write), events (trigger), SSE (broadcast)

**Upgrade story:** Assets live in hex.db (user-space). Policy lives in `~/.hex/extensions/hex-kanban/`. Neither is touched by `hex upgrade`.

**What breaks:**
1. **`hex.asset.changed` event is not defined** — the proposal doesn't specify what events are emitted when `POST /api/assets/<key>` is called. If the hex API doesn't fire this event, the policy trigger has nothing to react to.
2. **`type: sse_publish` action doesn't exist** — the proposal only defines `type: shell` for policy actions. A first-class `sse_publish` action type needs to be added to the policy vocabulary.
3. **Event data templating is undefined** — `{{ $.key }}` and `{{ $.timestamp }}` templating syntax isn't specified in the policy format.
4. **No asset concurrency control** — two rapid drops in different tabs can produce a lost-update race. No CAS (compare-and-swap), ETags, or optimistic locking on assets.
5. **Asset size limits undefined** — a complex board with many cards could be 100KB+. No documented limit.

---

## Use Case 3: Prediction Market Tracker with Own Data Model *(full)*

**Feature:** Tracks Metaculus / Manifold / Polymarket markets. Stores bets, current prices, and user positions. Shows P&L, calibration charts, and alerts on significant price moves.

**Tier:** full — relational data model (markets, bets, positions, price history) needs a queryable database.

**extension.yaml:**
```yaml
name: hex-prediction-markets
version: "1.0.0"
type: proxy
engines:
  hex: ">=0.8.0"
provides:
  proxies:
    - name: prediction-markets
      path: /ext/prediction-markets
      upstream: http://localhost:7420
      title: "Prediction Markets"
      icon: "📈"
      health_check: /health
      start_command: "python3 ~/.hex/extensions/hex-prediction-markets/server/app.py"
      stop_signal: SIGTERM
  sse_topics:
    - name: prediction.price.alert
      schema:
        type: object
        properties:
          market_id: { type: string }
          old_price: { type: number }
          new_price: { type: number }
          pct_change: { type: number }
      producer: scripts/price-monitor.sh
      interval: 300s
requires:
  capabilities:
    - sse
    - events
```

**Extension server layout:**
```
~/.hex/extensions/hex-prediction-markets/
  extension.yaml
  server/
    app.py           ← Flask/FastAPI on port 7420
    models.py        ← Market, Bet, Position, PricePoint
    db.py            ← SQLite at data/markets.db
  data/
    markets.db       ← extension-owned, never touched by hex upgrade
  scripts/
    price-monitor.sh ← polls external APIs every 5min, publishes alerts
```

**State:** Extension-owned SQLite in `data/markets.db`. Hex never reads or writes this file. Schema migrations run at server startup.

**Real-time updates:** `price-monitor.sh` polls external APIs every 5 minutes. On >5% price move, publishes `prediction.price.alert` via `POST $HEX_API/sse/publish`. UI subscribes to this topic.

**Hex primitives used:** `type: proxy`, `start_command`, SSE topic producer, `$HEX_API`

**Upgrade story:** `hex extension upgrade` replaces bundle files but the checksum snapshot skips `data/markets.db` (it wasn't in the install manifest). Extension server handles its own migrations.

**What breaks:**
1. **Port conflict registry doesn't exist** — multiple full-tier extensions each hardcode a localhost port. Two extensions can claim port 8080 simultaneously. Hex needs a port allocation mechanism (dynamic assignment + injection as `$EXT_PORT`).
2. **Process crash restart policy is unspecified** — the proposal says `hex extension start` launches the process, but what happens when it crashes? No restart policy, watchdog, or supervisor semantics are defined.
3. **No auth between extension server and $HEX_API** — the server calls `POST $HEX_API/sse/publish` to emit alerts. There's no token or secret required in the proposal. Any localhost process can publish to any SSE topic.

---

## Use Case 4: CI/CD Dashboard Polling External APIs *(reactive)*

**Feature:** Polls GitHub Actions / CircleCI for build status across configured repos. Shows live status in a landing page widget and full view.

**Tier:** reactive — SSE topic producer handles polling; static view consumes results. This is the most natural reactive-tier use case.

**extension.yaml:**
```yaml
name: hex-cicd
version: "1.0.0"
engines:
  hex: ">=0.8.0"
provides:
  views:
    - name: cicd
      path: /ext/cicd
      entry: views/cicd/index.html
      title: "CI/CD"
      nav: true
      sse_topics:
        - cicd.build.status
  widgets:
    - name: build-status
      title: "Build Status"
      entry: widgets/build-status.html
      placement: sidebar
      size: compact
      sse_topics:
        - cicd.build.status
  sse_topics:
    - name: cicd.build.status
      schema:
        type: object
        properties:
          repo: { type: string }
          branch: { type: string }
          status: { type: string, enum: [pending, running, success, failure] }
          run_url: { type: string }
      producer: scripts/poll-ci.sh
      interval: 60s
requires:
  capabilities:
    - assets   # for config (repo list, API token)
    - sse
```

**State:** No persistent build history. Poll results are published as SSE events and consumed in memory. Config (repo list, GitHub token) stored in `GET/POST /api/assets/cicd/config`.

**Producer script (scripts/poll-ci.sh):**
```bash
#!/bin/bash
set -uo pipefail
config=$(curl -sf "$HEX_API/assets/cicd/config")
gh_token=$(echo "$config" | python3 -c "import json,sys; print(json.load(sys.stdin)['github_token'])")
repos=$(echo "$config" | python3 -c "import json,sys; print('\n'.join(json.load(sys.stdin)['repos']))")

while IFS= read -r repo; do
  run=$(curl -sf -H "Authorization: token $gh_token" \
    "https://api.github.com/repos/$repo/actions/runs?branch=main&per_page=1")
  status=$(echo "$run" | python3 -c "
import json,sys
r = json.load(sys.stdin)['workflow_runs']
if r: print(r[0].get('conclusion') or r[0]['status'])
")
  run_url=$(echo "$run" | python3 -c "
import json,sys
r = json.load(sys.stdin)['workflow_runs']
if r: print(r[0]['html_url'])
")
  curl -sf -X POST "$HEX_API/sse/publish" \
    -H "Content-Type: application/json" \
    -d "{\"topic\": \"cicd.build.status\", \"data\": {\"repo\": \"$repo\", \"branch\": \"main\", \"status\": \"$status\", \"run_url\": \"$run_url\"}}"
done <<< "$repos"
```

**Hex primitives used:** assets (config storage), SSE topic producer, `POST $HEX_API/sse/publish`

**Upgrade story:** Config asset survives hex upgrade (it's in hex.db user-space). Producer script is in extension bundle — `hex extension upgrade` replaces it if user hasn't modified it.

**What breaks:**
1. **Credentials in assets are not secure** — the GitHub token is stored in `hex.db`. If the repo is committed with `hex.db`, or if `hex.db` is readable by other users on the machine, the token leaks. No secure credential store.
2. **Producer failure is silent** — if `poll-ci.sh` fails (rate limit, network error), the SSE topic emits nothing. The UI has no way to distinguish "no new builds" from "poller is broken." Need a heartbeat or error event on SSE topics.
3. **No state between producer runs** — the script is invoked fresh every 60s. It can't detect transitions (e.g., "build went from running to success") without storing previous state somewhere (a temp file or asset), which the proposal doesn't standardize.

---

## Use Case 5: AI Chat Interface with Agent Routing *(full)*

**Feature:** A chat UI that routes messages to different hex agents based on context: coding → Opus, quick lookup → Haiku, creative → Sonnet. Maintains per-session conversation history.

**Tier:** full — routing logic requires server-side decision making and persistent conversation history.

**extension.yaml:**
```yaml
name: hex-chat
version: "1.0.0"
type: proxy
engines:
  hex: ">=0.8.0"
provides:
  proxies:
    - name: chat
      path: /ext/chat
      upstream: http://localhost:7430
      title: "AI Chat"
      icon: "💬"
      start_command: "python3 ~/.hex/extensions/hex-chat/server/main.py"
      health_check: /health
  sse_topics:
    - name: chat.message.streaming
      schema:
        type: object
        properties:
          session_id: { type: string }
          delta: { type: string }
          done: { type: boolean }
      producer: null   # published directly by extension server
requires:
  capabilities:
    - sse
    - agent_harness
    - messaging
```

**State:** Conversation history in extension-owned SQLite. Routing state (active agent, context) in memory within the server process.

**How routing works:**
1. User sends message to `POST /ext/chat/api/message`
2. Server classifies intent (heuristic or lightweight Haiku call)
3. Routes to appropriate agent via hex agent harness
4. Streams response back via `POST $HEX_API/sse/publish` on topic `chat.message.streaming`
5. UI subscribes to this topic, renders streaming deltas

**What breaks:**
1. **No agent dispatch API** — the proposal says extensions can *declare* agents, but there's no `POST $HEX_API/agent/dispatch` endpoint. Extensions can't *invoke* a hex agent. The `requires.capabilities: agent_harness` is meaningless without an invocation API.
2. **No streaming pipe from harness to SSE** — even if dispatch worked, the hex agent's output is text that streams from the harness. How does that stream flow into an SSE topic? The proposal has no mechanism for harness output → SSE forwarding.
3. **Port conflict** (same as all full-tier).

---

## Use Case 6: Custom Approval Workflow *(reactive)*

**Feature:** Multi-step workflow: requester submits a request (title, description, approvers list). Approvers see it in a dashboard and approve/reject with a comment. State persists across hex restarts.

**Tier:** reactive (intended) — hits hard limits on extension data storage.

**extension.yaml:**
```yaml
name: hex-approvals
version: "1.0.0"
engines:
  hex: ">=0.8.0"
provides:
  views:
    - name: approvals
      path: /ext/approvals
      entry: views/approvals/index.html
      title: "Approvals"
      nav: true
      sse_topics:
        - approval.status.changed
  policies:
    - policies/approval-workflow.yaml
  sse_topics:
    - name: approval.status.changed
      schema:
        type: object
        properties:
          request_id: { type: string }
          status: { type: string, enum: [pending, approved, rejected] }
          actor: { type: string }
requires:
  capabilities:
    - assets
    - events
    - sse
    - messaging
```

**State plan:** Each request stored as a hex asset:
```
POST /api/assets/approvals/<uuid>
{"id": "<uuid>", "title": "...", "status": "pending", "approvers": [...], "comments": []}
```

**Policy (approval-workflow.yaml):**
```yaml
name: approval-workflow
rules:
  - name: on-submit
    trigger:
      event: approval.request.submitted
    actions:
      - type: shell
        command: |
          curl -sf -X POST "$HEX_API/assets/approvals/$HEX_EVENT_ID" \
            -H "Content-Type: application/json" \
            -d "$HEX_EVENT_DATA"
          curl -sf -X POST "$HEX_API/messages" \
            -d "{\"channel\": \"approvals\", \"text\": \"New request: $HEX_EVENT_TITLE\"}"
  - name: on-decision
    trigger:
      event: approval.decision.made
    actions:
      - type: shell
        command: |
          # Must fetch, merge, and replace — no PATCH support
          existing=$(curl -sf "$HEX_API/assets/approvals/$HEX_EVENT_REQUEST_ID")
          updated=$(echo "$existing" | python3 -c "
import json,sys,os
d = json.load(sys.stdin)
d['status'] = os.environ['HEX_EVENT_STATUS']
d['decided_by'] = os.environ['HEX_EVENT_ACTOR']
print(json.dumps(d))
")
          curl -sf -X POST "$HEX_API/assets/approvals/$HEX_EVENT_REQUEST_ID" \
            -H "Content-Type: application/json" -d "$updated"
```

**What breaks:**
1. **No `PATCH /api/assets/<key>`** — the proposal only mentions `GET/POST/DELETE`. Updating a request status requires fetch-merge-replace with no atomicity guarantee. Concurrent decisions (two approvers approving simultaneously) produce a lost-update race.
2. **No structured query** — listing all pending approvals requires `GET /api/assets?prefix=approvals/` then filtering in JS. With 100+ requests, this transfers all request JSON to the client just to filter by status.
3. **Event data env var injection is unspecified** — the policy action uses `$HEX_EVENT_ID`, `$HEX_EVENT_DATA`, `$HEX_EVENT_STATUS` etc. The proposal never defines what environment variables are injected into shell actions from the triggering event.
4. **No background scheduler for reminders** — can't send "still awaiting approval after 24h" reminders without a full-tier server. The reactive tier has no cron primitive.

---

## Use Case 7: Metrics Dashboard with Rollups and Charts *(full)*

**Feature:** Aggregates telemetry events (latency, error rates, request counts) from hex events. Stores time-series data with rollups (1m, 5m, 1h). Renders charts.

**Tier:** full — time-series storage with rollup queries can't be done with key-value assets.

**extension.yaml:**
```yaml
name: hex-metrics
version: "1.0.0"
type: proxy
engines:
  hex: ">=0.8.0"
provides:
  proxies:
    - name: metrics
      path: /ext/metrics
      upstream: http://localhost:7440
      title: "Metrics"
      icon: "📊"
      start_command: "python3 ~/.hex/extensions/hex-metrics/server/app.py"
      health_check: /health
  widgets:
    - name: metrics-summary
      title: "System Health"
      entry: widgets/health.html
      placement: sidebar
      size: compact
      sse_topics:
        - metrics.health.updated
  sse_topics:
    - name: metrics.health.updated
      producer: scripts/health-check.sh
      interval: 30s
      schema:
        type: object
        properties:
          p99_latency_ms: { type: number }
          error_rate: { type: number }
          req_per_min: { type: number }
requires:
  capabilities:
    - events
    - sse
    - assets
```

**State:** Extension-owned SQLite with time-series schema:
```sql
CREATE TABLE metrics (ts INTEGER, name TEXT, value REAL, tags TEXT);
CREATE INDEX idx_ts_name ON metrics(ts, name);
```

**Real-time updates:** Extension server subscribes to hex events (polling `GET $HEX_API/events?since=<last_ts>`) and inserts into SQLite. Widget runs a 30s health-check producer for summary stats.

**What breaks:**
1. **Event subscription latency** — the server polls `GET $HEX_API/events?since=<last_ts>` to ingest events. Poll-based ingestion adds up to `poll_interval` seconds of latency. The proposal mentions `$HEX_EVENTS_SOCKET` for socket-based listening but doesn't define the protocol, message format, or auth.
2. **Port conflict and lifecycle issues** (same as all full-tier).

---

## Use Case 8: Collaborative Document Annotations *(full)*

**Feature:** Multiple users on the same Tailscale network annotate documents with sticky notes. Annotations sync in real-time with user attribution.

**Tier:** full — requires user identity, multi-user state, and persistent annotation storage.

**extension.yaml:**
```yaml
name: hex-annotations
version: "1.0.0"
type: proxy
engines:
  hex: ">=0.8.0"
provides:
  proxies:
    - name: annotations
      path: /ext/annotations
      upstream: http://localhost:7450
      title: "Annotations"
      icon: "📝"
      start_command: "python3 ~/.hex/extensions/hex-annotations/server/main.py"
      health_check: /health
requires:
  capabilities:
    - sse
    - messaging
```

**State:** Extension-owned SQLite: `annotations(id, doc_id, user, x, y, text, created_at)`. SSE for real-time sync between users.

**How real-time sync works:** User A posts annotation → server writes to SQLite → server publishes SSE event → User B's tab receives update and renders new sticky note.

**What breaks:**
1. **No user identity** — this is the most critical gap for any multi-user Tailscale scenario. The hex server has no concept of authenticated identity. The extension server sees two HTTP requests with different source IPs, but there's no identity claim (no session token, no Tailscale identity assertion). The proposal says nothing about user identity, sessions, or how Tailscale node identity maps to a hex user.
2. **No per-user access control** — in a multi-user Tailscale setup, are all extensions accessible to all peers? The proposal has no per-view ACL or per-extension visibility control.
3. **Concurrent annotation conflicts** — no OT/CRDT primitives. Last-write-wins on overlapping annotation edits.

---

## Use Case 9: Notification Center Aggregating Slack, Email, GitHub *(reactive attempted)*

**Feature:** Unified notification feed aggregating Slack messages, Gmail, GitHub PR mentions, and hex messages. Shows unread count in the landing page nav.

**Tier:** Intended reactive — but credential management and storage needs push it toward full.

**extension.yaml:**
```yaml
name: hex-notifications
version: "1.0.0"
engines:
  hex: ">=0.8.0"
provides:
  widgets:
    - name: notification-bell
      title: "Notifications"
      entry: widgets/bell.html
      placement: sidebar
      size: compact
      sse_topics:
        - notifications.new
  views:
    - name: notifications
      path: /ext/notifications
      entry: views/notifications/index.html
      title: "Notifications"
      nav: true
      sse_topics:
        - notifications.new
  sse_topics:
    - name: notifications.new
      producer: scripts/aggregate-notifications.sh
      interval: 120s
      schema:
        type: object
        properties:
          source: { type: string, enum: [slack, gmail, github, hex] }
          id: { type: string }
          title: { type: string }
          url: { type: string }
          unread_count: { type: integer }
requires:
  capabilities:
    - assets   # for credentials + notification history
    - messaging
    - sse
```

**State plan:** Notification history as hex assets keyed `notifications/<source>/<id>`. Unread count in `notifications/state`.

**What breaks:**
1. **Credential security** — Slack token, Gmail OAuth token, and GitHub token all need to be stored somewhere. The proposal offers no secure credential store. Hex assets live in `hex.db` — committed or readable on the filesystem. No keychain integration, no vault, no encrypted secrets.
2. **OAuth token refresh** — Gmail OAuth tokens expire in 1h. A background worker must refresh them. The reactive tier has no mechanism for background tasks that run on schedule (not event-triggered).
3. **Deduplication requires query** — the poll script runs every 2 minutes. Checking if a notification was already seen requires comparing against stored notification history. With 500+ stored notifications, `GET /api/assets?prefix=notifications/` transfers all of them to filter client-side. No server-side query.
4. **Producer state between runs** — "seen IDs" must be persisted between 120s poll invocations. The proposal doesn't standardize how producers persist inter-run state.

---

## Use Case 10: Integration Marketplace UI *(full)*

**Feature:** A web UI to browse available hex extensions, install/uninstall them with one click, configure them via forms, and see installed extension status.

**Tier:** full — needs to invoke `hex extension install/remove` (CLI commands) and read/write the extension index.

**extension.yaml:**
```yaml
name: hex-marketplace
version: "1.0.0"
type: proxy
engines:
  hex: ">=0.8.0"
provides:
  proxies:
    - name: marketplace
      path: /ext/marketplace
      upstream: http://localhost:7460
      title: "Extensions"
      icon: "🧩"
      start_command: "python3 ~/.hex/extensions/hex-marketplace/server/main.py"
      health_check: /health
requires:
  capabilities:
    - assets
    - events
```

**What breaks:**
1. **No management REST API** — the marketplace server needs to call `hex extension install <source>` and `hex extension remove <name>`. These are CLI commands only. No `POST $HEX_API/extensions/install` endpoint exists. The server could shell out to the `hex` binary, but this requires knowing the hex binary path, having write access to `~/.hex/extensions/`, and handling the subprocess lifecycle.
2. **No extension registry/catalogue** — the proposal mentions a "registry (future)" install source but defines no registry format, no catalogue API, and no discovery mechanism beyond git URLs. A marketplace has nothing to browse unless a catalogue API is built.
3. **No extension config schema** — extensions have configuration needs (API tokens, repo lists, thresholds). The proposal has no extension configuration schema or standardized config storage. Each extension invents its own config format in assets, making the marketplace unable to render a generic config form.

---

## Use Case 11: Financial Tracker with Recurring Transactions *(full)*

**Feature:** Tracks income, expenses, recurring transactions, budgets by category, and cash-flow projections. Imports from CSV. Shows charts.

**Tier:** full — requires relational data model (transactions, categories, budgets, recurring rules) and a background scheduler for processing recurring transactions daily.

**extension.yaml:**
```yaml
name: hex-finance
version: "1.0.0"
type: proxy
engines:
  hex: ">=0.8.0"
provides:
  proxies:
    - name: finance
      path: /ext/finance
      upstream: http://localhost:7470
      title: "Finance"
      icon: "💰"
      start_command: "python3 ~/.hex/extensions/hex-finance/server/app.py"
      health_check: /health
  sse_topics:
    - name: finance.budget.alert
      producer: scripts/budget-check.sh
      interval: 3600s
      schema:
        type: object
        properties:
          category: { type: string }
          spent: { type: number }
          budget: { type: number }
          pct_used: { type: number }
requires:
  capabilities:
    - events
    - sse
    - assets
```

**State:** Extension-owned SQLite: `transactions`, `categories`, `budgets`, `recurring_rules` tables. Server handles its own migrations and the recurring-transaction scheduler (APScheduler or similar).

**What breaks:**
1. **No cron/scheduler in reactive tier** — recurring transactions must generate entries at midnight daily. An SSE producer runs at a fixed poll interval but can't express "run at midnight." The reactive tier has no cron primitive. This forces even "simple" scheduled-data use cases into the full tier.
2. **No file upload in assets API** — CSV import requires `multipart/form-data` handling. The `POST /api/assets/<key>` endpoint isn't designed for large binary uploads. No mention of streaming, chunked uploads, or size limits.
3. **Port conflicts and lifecycle issues** (same as all full-tier).

---

## Architectural Gap Analysis

### Gap 1: Reactive Tier Has No Structured Data Storage

**Severity:** Critical  
**Affected:** Use Cases 2, 5, 6, 8, 9, 11

The tier model says reactive tier "adds SQLite tables" — but the current proposal has no mechanism for extension-owned tables in hex.db (or any extension-owned SQLite). The only writable extension storage is hex assets (key-value blobs). This missing primitive forces most real-world reactive use cases to either:
- Use assets awkwardly (fetch-all-filter, no partial updates, no transactions), or
- Escalate to full tier (bring your own server + SQLite)

**What needs to be defined:**
- An `extension_tables:` section in `extension.yaml` that declares SQL schema migrations
- A `GET $HEX_API/ext/<name>/db` endpoint (or SQLite file path injection) for querying extension tables
- Migration runner invoked by hex at extension load time

### Gap 2: No Secure Credential Storage

**Severity:** High  
**Affected:** Use Cases 3, 4, 8, 9

Extensions needing API tokens (GitHub, Slack, Gmail) have no safe storage option. Hex assets are stored in `hex.db` which may be committed to version control. The proposal has no keychain integration, no vault, and no encrypted secrets mechanism.

**What needs to be defined:**
- A `secrets:` capability in `requires:` that gates access to a separate secure store
- `GET/POST $HEX_API/secrets/<key>` endpoint backed by macOS Keychain / libsecret / environment injection
- Secrets excluded from hex.db commits, hex.db backups, and `hex doctor` output

### Gap 3: Policy Action Vocabulary Is Underspecified

**Severity:** High  
**Affected:** Use Cases 2, 6

The proposal shows `type: shell` policy actions but never defines:
- What environment variables are injected into shell actions (event data, event ID, event metadata)
- Whether `type: sse_publish` exists as a first-class action (vs. shelling out to `curl`)
- Error handling behavior (retry? skip? alert?)
- Action timeout behavior

### Gap 4: `hex.asset.changed` Event Is Not Defined

**Severity:** High  
**Affected:** Use Case 2

The reactive tier's cross-component communication pattern depends on the hex API firing `hex.asset.changed` (or similar) when `POST /api/assets/<key>` is called. The proposal doesn't define what events the hex API emits internally. Without this, policies can't react to asset writes.

### Gap 5: No User Identity for Multi-User (Tailscale) Scenarios

**Severity:** High  
**Affected:** Use Case 8

The hex server has no concept of authenticated user identity. In a multi-user Tailscale setup, extensions can't:
- Distinguish which Tailscale peer is making a request
- Enforce per-user access control on views or extension APIs
- Attribute actions to specific users

**What needs to be defined:**
- A user identity model (at minimum: Tailscale node hostname/IP → user identity)
- `X-Hex-User` or similar request header injected by the hex reverse proxy
- Optional per-view ACL in `extension.yaml`

### Gap 6: Full-Tier Port Allocation Has No Registry

**Severity:** Medium  
**Affected:** Use Cases 3, 5, 7, 8, 10, 11

Every full-tier extension hardcodes a localhost port. Hex needs a mechanism to:
- Allocate a free port dynamically and inject it as `$EXT_PORT` into the `start_command`
- Maintain a registry of allocated ports to avoid conflicts
- Pass the allocated port back to the proxy `upstream` configuration at runtime

### Gap 7: No Agent Dispatch API

**Severity:** Medium  
**Affected:** Use Case 5

Extensions can *declare* agents but can't *invoke* them. There's no `POST $HEX_API/agent/dispatch` endpoint. The `requires.capabilities: agent_harness` capability is currently inert.

### Gap 8: No Background Scheduler / Cron for Reactive Tier

**Severity:** Medium  
**Affected:** Use Cases 6, 9, 11

SSE producers run on a fixed interval (e.g., every 60s). There's no "run at specific time" or "run once daily" primitive. Any use case needing scheduled work must escalate to full tier.

### Gap 9: Extension Process Lifecycle Is Underspecified

**Severity:** Medium  
**Affected:** Use Cases 3, 5, 7, 8, 10, 11

The `start_command` mechanism doesn't specify:
- Restart policy on crash (restart always? on-failure? never?)
- Health check failure behavior (restart? alert? mark extension offline?)
- Log rotation for extension process logs
- Graceful shutdown sequence (SIGTERM → wait → SIGKILL timeout)

### Gap 10: No `hex.asset.changed` + Missing Asset Primitives

**Severity:** Medium  
**Affected:** Use Cases 2, 6

Beyond the missing `hex.asset.changed` event, the asset API needs:
- `PATCH /api/assets/<key>` for partial updates (JSON Merge Patch or JSON Patch)
- Conditional writes (`If-Match: <etag>`) for optimistic concurrency
- Defined size limits
- Defined behavior when `?prefix=` returns thousands of results (pagination)

---

## Summary: Tier Viability by Use Case

| # | Use Case | Intended Tier | Viable? | Blockers |
|---|----------|--------------|---------|----------|
| 1 | Activity feed | static | ✅ Works | None |
| 2 | Kanban board | reactive | ⚠️ Partial | Gaps 1, 3, 4, 10 |
| 3 | Prediction markets | full | ⚠️ Partial | Gaps 6, 9 |
| 4 | CI/CD dashboard | reactive | ⚠️ Partial | Gaps 2, 3 |
| 5 | AI chat routing | full | ❌ Blocked | Gap 7 (no agent dispatch) |
| 6 | Approval workflow | reactive | ⚠️ Awkward | Gaps 1, 3, 8, 10 |
| 7 | Metrics dashboard | full | ⚠️ Partial | Gaps 6, 9 |
| 8 | Collaborative annotations | full | ❌ Blocked | Gap 5 (no user identity) |
| 9 | Notification center | reactive | ❌ Blocked | Gaps 2, 8 |
| 10 | Extension marketplace | full | ❌ Blocked | Gap: no management API |
| 11 | Financial tracker | full | ⚠️ Partial | Gaps 6, 8, 9 |

**Legend:** ✅ = works as proposed · ⚠️ = works with workarounds · ❌ = requires architectural changes

---

*Next: `proposal-v2.md` — revised architecture addressing Gaps 1–10.*
