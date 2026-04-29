# Hex Extensibility Architecture — Proposal v2

> Supersedes `proposal.md` (2026-04-27) for the sections listed below.
> Read `proposal.md` first — this document addresses only the gaps exposed by
> `deep-use-cases.md`. Sections not mentioned here are unchanged from v1.
>
> Gap analysis source: `docs/extensibility/deep-use-cases.md §Architectural Gap Analysis`.
> 10 gaps identified. 4 rated Critical/High, 6 rated Medium.
> This document provides concrete design solutions for all 10.

---

## What Changed (Executive Summary)

| Gap | v1 State | v2 Resolution |
|-----|----------|---------------|
| 1 — Reactive tier data storage | No structured storage; key-value assets only | `extension_db:` + managed SQLite per extension + named queries |
| 2 — Secure credentials | None; tokens in hex.db | `secrets` capability + `$HEX_API/secrets/<key>` backed by OS keychain |
| 3 — Policy action vocabulary | Only `type: shell` defined | Full action vocabulary: `sse_publish`, `emit_event`, `http_call`; env var spec; error handling |
| 4 — Internal hex events | Unspecified | Defined canon of internal events fired by hex primitives |
| 5 — User identity | No concept | Tailscale WhoIs integration + `X-Hex-User` header + view ACLs |
| 6 — Port allocation | Manual hardcode | Hex port registry + `upstream: auto` + `$EXT_PORT` injection |
| 7 — Agent dispatch | Declare-only | `POST $HEX_API/agent/dispatch` + streaming output via SSE |
| 8 — Reactive cron | No cron primitive | `schedules:` block in `extension.yaml` with cron expressions |
| 9 — Process lifecycle | `start_command` only | Full `lifecycle:` spec: restart policy, health checks, log rotation |
| 10 — Asset API | GET/POST/DELETE only | PATCH (JSON Merge Patch), ETags, pagination |

Additionally:

| New Feature | Reason |
|-------------|--------|
| Extension config schema | Required for marketplace + generic config UI |
| Management REST API | Extensions need to install/remove other extensions (marketplace use case) |

---

## 1. Extension Database — Reactive Tier Storage (Gap 1)

### Problem

The reactive tier has no structured data storage. The key-value asset store forces fetch-merge-replace
patterns, can't be queried by field, and has no transactional semantics. Use cases that need
multi-row state (approval requests, notification history, kanban cards beyond a single blob) must
escalate to the full tier unnecessarily.

### Solution: `extension_db:` + Managed SQLite

Every extension can opt into a managed SQLite database by declaring `extension_db: true` in
`extension.yaml`. Hex creates `~/.hex/extensions/<name>/data/ext.db` at install time, runs
declared migrations at load time, and injects `$EXT_DB_PATH` into all extension processes.

#### `extension.yaml` declaration

```yaml
name: hex-approvals
version: "1.0.0"
engines:
  hex: ">=0.9.0"

extension_db: true
migrations:
  - db/migrations/001_initial.sql
  - db/migrations/002_add_comments.sql
```

#### Migration files

```sql
-- db/migrations/001_initial.sql
CREATE TABLE IF NOT EXISTS requests (
  id       TEXT PRIMARY KEY,
  title    TEXT NOT NULL,
  status   TEXT NOT NULL DEFAULT 'pending',
  requester TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS approvers (
  request_id TEXT NOT NULL REFERENCES requests(id),
  user       TEXT NOT NULL,
  decided_at TEXT,
  decision   TEXT,
  comment    TEXT
);
```

Migrations are applied in lexicographic order. Hex tracks applied migrations in a
`_hex_migrations` table within `ext.db` — it never re-applies a migration that has already run.

#### REST query access

For reactive-tier extensions (no own server), hex provides a **named query** REST endpoint so
views and policies can read extension data without spawning a process.

Declare named queries in `extension.yaml`:

```yaml
named_queries:
  pending_requests:
    sql: >
      SELECT r.id, r.title, r.requester, r.created_at,
             GROUP_CONCAT(a.user, ',') AS approvers
      FROM requests r
      LEFT JOIN approvers a ON a.request_id = r.id
      WHERE r.status = 'pending'
      GROUP BY r.id
      ORDER BY r.created_at DESC
      LIMIT :limit OFFSET :offset

  request_by_id:
    sql: SELECT * FROM requests WHERE id = :id
```

Named queries are read-only (`SELECT` only — no `INSERT/UPDATE/DELETE`). Access:

```
GET $HEX_API/ext/<name>/db/<query-name>?param=value
→ { "rows": [...], "count": N }
```

For writes, extensions use policy shell actions or full-tier server code. The `$EXT_DB_PATH`
env var is available to all shell actions and producer scripts:

```yaml
# In a policy action:
- type: shell
  command: |
    python3 -c "
    import sqlite3, json, os, sys
    db = sqlite3.connect(os.environ['EXT_DB_PATH'])
    data = json.loads(os.environ['HEX_EVENT_DATA'])
    db.execute('INSERT INTO requests VALUES (?,?,?,?,?)',
               [data['id'], data['title'], 'pending', data['requester'],
                data['timestamp']])
    db.commit()
    "
```

#### Upgrade safety

`~/.hex/extensions/<name>/data/ext.db` is never written by `hex extension upgrade`. It is
explicitly excluded from the upgrade manifest. The extension's migration runner handles schema
evolution; hex's job is only to run new migration files added to the bundle.

#### Limitations

- Named queries are `SELECT` only — no arbitrary writes from the browser.
- `ext.db` is a single SQLite file. Concurrent writes from multiple policy actions are
  serialized by SQLite's WAL mode (enabled by hex at creation time: `PRAGMA journal_mode=WAL`).
- No cross-extension table access. Extensions are isolated to their own `ext.db`.

---

## 2. Secure Credential Storage (Gap 2)

### Problem

Extensions needing API tokens (GitHub, Slack, Gmail) have no safe storage option. Hex assets
live in `hex.db` which may be committed to version control or readable by other processes.

### Solution: `secrets` Capability + OS-Backed Secret Store

#### Backend Selection

| OS | Backend | Implementation |
|----|---------|----------------|
| macOS | Keychain | `security add-generic-password / find-generic-password` |
| Linux | libsecret | `secret-tool store / lookup` |
| Fallback | AES-256-GCM encrypted file | `~/.hex/secrets.enc` with key derived from machine ID |

Hex probes for the best available backend at startup and logs which one is active.

#### `extension.yaml` declaration

```yaml
requires:
  capabilities:
    - secrets   # extensions must declare this to access the secret store
```

#### REST API

```
GET    $HEX_API/secrets/<key>           → {"value": "<secret>"}
POST   $HEX_API/secrets/<key>           body: {"value": "<secret>"}  → 200 OK
DELETE $HEX_API/secrets/<key>           → 200 OK
GET    $HEX_API/secrets?prefix=<name>/  → {"keys": ["name/key1", "name/key2"]}
```

Secrets are namespaced by extension name automatically:
- Extension `hex-cicd` requesting `GET $HEX_API/secrets/github_token` accesses keychain entry
  keyed `hex.ext.hex-cicd.github_token`.
- Extensions cannot access other extensions' secrets (enforced by the `X-Hex-Extension` request
  header which hex injects when forwarding requests from extension processes).

#### Security invariants

1. **Never in hex.db** — secrets are never written to `hex.db`.
2. **Never in logs** — `$HEX_API/secrets/<key>` responses are excluded from hex's request log.
3. **Never in `hex doctor` output** — the `hex doctor` command shows secret key names only,
   never values.
4. **Never in hex.db backups** — `hex backup` excludes the secret store entirely.
5. **Never in `$HEX_EVENT_DATA`** — if an event contains a key matching a known secret name,
   hex redacts it in logs with `[REDACTED]`.

#### Usage in producer scripts

```bash
# scripts/poll-ci.sh
gh_token=$(curl -sf "$HEX_API/secrets/github_token" | python3 -c "import json,sys; print(json.load(sys.stdin)['value'])")
```

#### Config schema integration (see §11)

When `config_schema` declares a field with `type: secret`, `hex extension config <name>` prompts
the user and stores the value via the secrets API. The user never types tokens into config files.

---

## 3. Policy Action Vocabulary (Gap 3)

### Problem

The v1 proposal defined `type: shell` but never specified what environment variables are injected,
never defined alternative action types (`sse_publish`, `http_call`), and gave no error handling.

### Solution: Full Action Vocabulary

#### 3.1 Environment Variables Injected into Shell Actions

Every `type: shell` action receives these env vars from the triggering event:

| Variable | Value |
|----------|-------|
| `HEX_API` | Base URL of hex REST API (`http://localhost:<port>`) |
| `HEX_EVENT_TOPIC` | Topic name that fired the rule (e.g., `hex.asset.written`) |
| `HEX_EVENT_ID` | Unique ID of the event |
| `HEX_EVENT_TIMESTAMP` | ISO 8601 timestamp |
| `HEX_EVENT_DATA` | Full event data payload as a JSON string |
| `HEX_EVENT_<FIELD>` | Top-level fields extracted from `event.data` and injected individually. Field names are uppercased and dots replaced with underscores. E.g., `event.data.request_id` → `HEX_EVENT_REQUEST_ID`. |
| `EXT_DB_PATH` | Path to extension's SQLite database (if `extension_db: true`) |
| `HEX_EXTENSION_NAME` | Name of the extension owning this policy |
| `HEX_WORKSPACE` | Absolute path to the hex workspace root |

**Example:** For event `{ "topic": "approval.request.submitted", "data": { "id": "abc", "title": "Fix prod" } }`:

```
HEX_EVENT_TOPIC=approval.request.submitted
HEX_EVENT_ID=01j8x...
HEX_EVENT_TIMESTAMP=2026-04-27T21:00:00Z
HEX_EVENT_DATA={"id":"abc","title":"Fix prod"}
HEX_EVENT_ID=abc
HEX_EVENT_TITLE=Fix prod
```

#### 3.2 New First-Class Action Types

**`type: sse_publish`** — publish to an SSE topic without shelling out to curl:

```yaml
- type: sse_publish
  topic: kanban.board.updated
  data:
    board_id: "{{ event.data.key }}"
    updated_at: "{{ event.timestamp }}"
```

The `{{ ... }}` template syntax uses dot-path lookups into the event envelope. Supported
expressions: `event.topic`, `event.id`, `event.timestamp`, `event.data.<field>`.
No arbitrary code execution in templates — only field references.

**`type: emit_event`** — emit a new event onto the hex event bus:

```yaml
- type: emit_event
  topic: approval.notification.sent
  data:
    request_id: "{{ event.data.id }}"
    sent_to: "{{ event.data.approvers }}"
```

**`type: http_call`** — make an HTTP call without writing a shell script:

```yaml
- type: http_call
  method: POST
  url: "{{ env.WEBHOOK_URL }}"
  headers:
    Content-Type: application/json
    Authorization: "Bearer {{ secret.webhook_token }}"
  body:
    text: "New approval request: {{ event.data.title }}"
  on_error: log
```

`{{ secret.<key> }}` resolves via the secrets API (requires `secrets` capability).
`{{ env.<VAR> }}` resolves from the extension's environment.

#### 3.3 Error Handling

Each rule now supports an `on_error:` field:

```yaml
rules:
  - name: my-rule
    trigger:
      event: some.event
    on_error: skip       # skip | retry | alert | fail
    retry:
      max_attempts: 3
      delay: 10s
    actions:
      - type: shell
        command: "..."
```

| `on_error` value | Behavior |
|-----------------|---------|
| `skip` | Log warning, continue to next rule. Default. |
| `retry` | Retry with exponential backoff up to `retry.max_attempts` (default: 3). After exhaustion, logs error. |
| `alert` | Emit `hex.extension.policy.error` event with full context. |
| `fail` | Mark the extension as error state, stop processing. Use for critical invariants. |

#### 3.4 Action Timeouts

All action types honor a `timeout:` field (default: 30s):

```yaml
- type: shell
  timeout: 10s
  command: "..."
```

If the action exceeds its timeout, it's killed (SIGTERM → 5s → SIGKILL) and treated as an error
per `on_error:`.

---

## 4. Internal Hex Events (Gap 4)

### Problem

The reactive tier depends on policies reacting to hex's own API events (e.g., "an asset was written"),
but v1 never defined what events hex emits internally. Without this, `type: sse_publish` actions
triggered by asset writes are impossible.

### Solution: Defined Internal Event Canon

Hex emits these events automatically. They cannot be suppressed. They appear on the event bus
and are available to all policy triggers.

#### Asset Events

| Event topic | Fired when | Data fields |
|-------------|-----------|-------------|
| `hex.asset.written` | `POST /api/assets/<key>` completes | `key`, `size_bytes`, `prev_exists` |
| `hex.asset.deleted` | `DELETE /api/assets/<key>` completes | `key` |
| `hex.asset.read` | `GET /api/assets/<key>` serves (throttled: max 1/min per key) | `key` |

#### Message Events

| Event topic | Fired when | Data fields |
|-------------|-----------|-------------|
| `hex.message.sent` | `POST /api/messages` completes | `channel`, `text_preview` (first 100 chars) |

#### Extension Events

| Event topic | Fired when | Data fields |
|-------------|-----------|-------------|
| `hex.extension.loaded` | Extension successfully loaded at startup | `name`, `version`, `types` |
| `hex.extension.unloaded` | Extension disabled or removed | `name`, `reason` |
| `hex.extension.error` | Extension process crashes or health check fails | `name`, `error`, `exit_code` |
| `hex.extension.policy.error` | Policy rule action fails (when `on_error: alert`) | `extension`, `rule`, `error` |

#### Server Events

| Event topic | Fired when | Data fields |
|-------------|-----------|-------------|
| `hex.server.started` | HTTP server is ready to accept connections | `version`, `port` |
| `hex.server.stopping` | Shutdown signal received | `reason` |

#### Loop Guard

To prevent infinite loops (a policy reacts to `hex.asset.written` and then writes an asset,
firing `hex.asset.written` again), hex enforces a **maximum policy chain depth of 5**. If a policy
action fires an event that triggers another rule, and that chain exceeds 5 hops, hex logs a
warning and drops the event. The chain depth is tracked via an `X-Hex-Policy-Depth` header
on internal API calls made by policy actions.

---

## 5. User Identity and Multi-User Access (Gap 5)

### Problem

The hex server has no concept of authenticated user identity. In a Tailscale multi-user setup,
extensions can't distinguish between peers, enforce access control, or attribute actions.

### Solution: Tailscale Identity Integration + View ACLs

#### 5.1 Identity Model

Hex uses a two-level identity model:

| Scenario | Identity source | User identifier |
|----------|----------------|----------------|
| Local browser (no Tailscale) | Loopback IP | `local` |
| Tailscale peer | Tailscale WhoIs API | `<login>@<domain>` (e.g., `alice@example.com`) |
| Tailscale peer (no login) | Tailscale WhoIs API | `<hostname>` (e.g., `alice-mbp`) |

#### 5.2 Identity Resolution

When `hex server` starts with Tailscale active, it binds a WhoIs resolver:

```
Incoming request from IP 100.x.y.z
→ GET http://100.100.100.100/v0/whois?addr=100.x.y.z (Tailscale daemon)
→ Returns { "Node": { "Hostinfo": { "Hostname": "alice-mbp" } }, "UserProfile": { "LoginName": "alice@example.com" } }
→ Injects X-Hex-User: alice@example.com into the request context
```

Hex caches WhoIs results for 60 seconds to avoid hammering the Tailscale daemon.

If Tailscale is not active or the request comes from loopback, `X-Hex-User: local`.

#### 5.3 Accessing Identity in Extensions

The `X-Hex-User` header is available in:

- **Full-tier extension servers** — hex forwards `X-Hex-User` on all proxied requests.
  Extension servers can read it directly.

- **Policy shell actions** — injected as `$HEX_REQUEST_USER` environment variable (only for
  policies triggered by HTTP-originated events that carry user context).

- **Named queries** — the `:current_user` bind parameter is reserved and resolved automatically:
  ```yaml
  my_requests:
    sql: SELECT * FROM requests WHERE requester = :current_user
  ```

- **JS in views** — the hex SDK exposes the current user:
  ```js
  import { hexUser } from '/ext/hex-sdk.js';
  const { login, displayName } = await hexUser();
  // { login: 'alice@example.com', displayName: 'alice-mbp' }
  ```
  This calls `GET /api/me` which hex resolves from `X-Hex-User`.

#### 5.4 View and Widget ACLs

Views and widgets can declare access control in `extension.yaml`:

```yaml
views:
  - name: approvals
    path: /ext/approvals
    entry: views/approvals/index.html
    access:
      mode: allow_all      # allow_all | owner_only | tailnet_only | allowlist
      # owner_only: only 'local' user (the machine owner)
      # tailnet_only: any authenticated Tailscale peer
      # allowlist: only the listed logins
      allowlist:
        - alice@example.com
        - bob@example.com
```

Hex enforces ACLs at the HTTP layer before serving the view. Unauthorized requests receive
`403 Forbidden` with a styled hex error page.

Default mode: `owner_only` (safe default; Tailscale peers must be explicitly granted access).

#### 5.5 Extension-Scoped Identity in the Event Bus

Events emitted via `POST $HEX_API/events` from an extension process carry the `X-Hex-User`
of the request that triggered the extension action. This means policies can filter by user:

```yaml
rules:
  - name: log-approvals
    trigger:
      event: approval.request.submitted
      filter: "$.user == 'alice@example.com'"
    actions:
      - type: shell
        command: "echo 'Alice submitted: $HEX_EVENT_TITLE'"
```

---

## 6. Port Allocation Registry (Gap 6)

### Problem

Every full-tier extension hardcodes a localhost port. Multiple extensions can claim the same port.
No mechanism exists to allocate a free port or avoid conflicts.

### Solution: Hex Port Registry + `upstream: auto`

#### 6.1 Port Registry

Hex maintains a port allocation registry at `~/.hex/ports.yaml`:

```yaml
# ~/.hex/ports.yaml — managed by hex, do not edit manually
schema_version: "1"
range_start: 7400
range_end:   7499
allocations:
  hex-prediction-markets: 7400
  hex-chat:               7401
  hex-metrics:            7402
  hex-annotations:        7403
  hex-marketplace:        7404
  hex-finance:            7405
```

Ports in range 7400–7499 are reserved for hex extensions. Extensions outside this range
(pre-existing servers) continue to use hardcoded ports in `upstream:`.

#### 6.2 `upstream: auto` Declaration

Extension authors replace the hardcoded port with `upstream: auto`:

```yaml
proxies:
  - name: prediction-markets
    path: /ext/prediction-markets
    upstream: auto        # hex allocates a port from 7400-7499
    title: "Prediction Markets"
    start_command: "python3 ~/.hex/extensions/hex-prediction-markets/server/app.py"
```

At install time, `hex extension install` allocates the next available port and records it in
`~/.hex/ports.yaml`. The allocated port is injected as `$EXT_PORT` into the `start_command`:

```
python3 ~/.hex/extensions/hex-prediction-markets/server/app.py
# Receives env var: EXT_PORT=7400
```

The extension server binds to `$EXT_PORT` (or `0.0.0.0:$EXT_PORT` if it needs Tailscale access).

#### 6.3 Port Persistence

Port allocations are stable across hex restarts and upgrades. A reinstall of the same extension
reuses the same port. If a port is no longer in use (extension removed), it's released back to
the pool.

#### 6.4 Port Conflict Detection

`hex extension validate` now checks:

- If `upstream:` is a literal `http://localhost:N`, warn if port N is already allocated to
  another extension.
- Suggest `upstream: auto` if the declared port falls in the reserved range.

---

## 7. Agent Dispatch API (Gap 7)

### Problem

Extensions can declare agents but cannot invoke them. The `agent_harness` capability is inert —
there is no endpoint to dispatch an agent and receive its output.

### Solution: `POST $HEX_API/agent/dispatch` + SSE Streaming

#### 7.1 Dispatch Endpoint

```
POST $HEX_API/agent/dispatch
Content-Type: application/json

{
  "agent":   "my-agent",        // name of a registered agent (charter.yaml)
  "prompt":  "Summarize events from the last hour",
  "context": { "key": "value" }, // optional: extra context injected into agent's env
  "async":   true                // true: returns dispatch_id immediately; false: blocks
}
```

**Synchronous response** (`async: false`, max timeout 300s):
```json
{
  "dispatch_id": "01j8x...",
  "status": "completed",
  "output": "Here is the summary: ...",
  "elapsed_ms": 4200
}
```

**Async response** (`async: true`):
```json
{
  "dispatch_id": "01j8x...",
  "status": "running",
  "sse_topic": "hex.agent.01j8x.output"
}
```

#### 7.2 Streaming Output via SSE

The caller subscribes to `hex.agent.<dispatch_id>.output` for streaming deltas:

```js
import { hexSSE } from '/ext/hex-sdk.js';

const { dispatch_id } = await hexAPI('/agent/dispatch', {
  method: 'POST',
  body: JSON.stringify({ agent: 'hex-summarizer', prompt: 'Summarize today', async: true })
});

hexSSE([`hex.agent.${dispatch_id}.output`], (event) => {
  if (event.data.done) {
    stream.close();
    return;
  }
  appendToUI(event.data.delta);
});
```

Each SSE event on this topic has the shape:
```json
{ "dispatch_id": "01j8x...", "delta": "Here is", "done": false }
{ "dispatch_id": "01j8x...", "delta": " the summary", "done": false }
{ "dispatch_id": "01j8x...", "delta": "", "done": true, "output": "Here is the summary" }
```

#### 7.3 Status and Cancellation

```
GET    $HEX_API/agent/<dispatch_id>/status
→ { "status": "running|completed|failed|cancelled", "elapsed_ms": N }

DELETE $HEX_API/agent/<dispatch_id>
→ 200 OK  (cancels the dispatch, SIGTERM to agent process)
```

#### 7.4 Access Control

Extensions must declare `agent_harness` in `requires.capabilities` to use the dispatch endpoint.
An extension can only dispatch agents declared in its own `extension.yaml` or in the global
agent registry. It cannot dispatch arbitrary shell commands via agent dispatch.

---

## 8. Reactive Tier Cron / Background Scheduler (Gap 8)

### Problem

SSE producers run on a fixed interval but can't express "run at midnight" or "run once after
hex starts." Any use case needing scheduled work was forced into the full tier.

### Solution: `schedules:` Block in `extension.yaml`

```yaml
name: hex-finance
version: "2.0.0"
engines:
  hex: ">=0.9.0"
extension_db: true
migrations:
  - db/migrations/001_initial.sql

schedules:
  - name: process-recurring-transactions
    cron: "0 0 * * *"          # midnight daily (standard 5-field cron)
    command: scripts/process-recurring.sh
    timeout: 120s
    on_error: alert

  - name: budget-check
    cron: "0 * * * *"          # every hour
    command: scripts/budget-check.sh
    timeout: 30s

  - name: startup-sync
    trigger: on_start           # special trigger: runs once at hex server start
    command: scripts/initial-sync.sh
    timeout: 300s
```

#### Supported Triggers

| `trigger` / `cron` | Meaning |
|--------------------|---------|
| `cron: "5-field-cron"` | Standard 5-field cron expression |
| `trigger: on_start` | Once when the hex server starts (after all extensions are loaded) |
| `trigger: on_install` | Once immediately after the extension is installed |
| `interval: 60s` | Every N seconds (same as existing SSE producer `interval:` — unified here) |

#### Scheduler Implementation

Hex embeds a minimal cron scheduler (stdlib-only, no external dependency). It wakes up every 60
seconds to fire any overdue jobs. Jobs run as subprocess commands with `$HEX_API`, `$EXT_DB_PATH`,
`$HEX_WORKSPACE`, and `$EXT_PORT` injected. Output is written to `~/.hex/audit/ext-<name>-<job>.log`.

#### SSE Producer Unification

The existing `sse_topics[].producer + interval` pattern is now syntactic sugar over `schedules:`.
At load time, hex expands:

```yaml
sse_topics:
  - name: cicd.build.status
    producer: scripts/poll-ci.sh
    interval: 60s
```

into an equivalent `schedules:` entry. Both syntaxes remain valid.

---

## 9. Extension Process Lifecycle (Gap 9)

### Problem

The `start_command` mechanism in v1 doesn't specify what happens when a process crashes,
how health check failures are handled, or how logs are managed.

### Solution: Full `lifecycle:` Spec on Proxy Declarations

```yaml
proxies:
  - name: prediction-markets
    path: /ext/prediction-markets
    upstream: auto
    title: "Prediction Markets"

    lifecycle:
      start_command: "python3 server/app.py"
      stop_signal: SIGTERM
      stop_timeout: 10s          # after SIGTERM, wait this long before SIGKILL

      restart:
        policy: on-failure        # never | always | on-failure
        max_attempts: 5           # after this many consecutive failures, mark extension error
        delay: 5s                 # wait before each restart attempt
        delay_multiplier: 2.0     # exponential backoff (5s, 10s, 20s, ...)
        reset_after: 300s         # reset attempt counter if process stays up this long

      health_check:
        path: /health
        interval: 30s
        timeout: 5s
        failure_threshold: 3      # mark offline after N consecutive failures
        success_threshold: 1      # mark online after N consecutive successes
        initial_delay: 5s         # wait before first health check after start

      logs:
        path: "~/.hex/audit/ext-{name}.log"
        max_size: 10MB
        rotate_count: 5           # keep 5 rotated files
        rotate_on_start: false    # if true, rotate log on each restart
```

#### Process States

```
installed → starting → running → (degraded) → stopped
                    ↑                ↓
                    └── restarting ←─┘
                                error (max_attempts exceeded)
```

States are tracked in `~/.hex/extensions/index.yaml` and surfaced by `hex extension list`:

```
EXTENSIONS
  hex-prediction-markets  v1.0.0  proxy  /ext/prediction-markets  running (pid 12345, healthy)
  hex-chat                v1.0.0  proxy  /ext/chat                 restarting (attempt 2/5)
  hex-metrics             v1.0.0  proxy  /ext/metrics              error (health check failed 3x)
```

#### Graceful Shutdown

On `hex server stop` or `hex extension stop <name>`:

1. Send `stop_signal` (default: SIGTERM) to the process
2. Wait up to `stop_timeout` (default: 10s)
3. If still running: send SIGKILL
4. Emit `hex.extension.unloaded` event

#### `hex extension logs <name>` Command

```bash
hex extension logs hex-metrics          # tail last 50 lines
hex extension logs hex-metrics --follow  # follow like `tail -f`
hex extension logs hex-metrics --since 1h  # show last 1 hour
```

---

## 10. Asset API Completions (Gap 10)

### Problem

The asset API (`GET/POST/DELETE`) has no partial update, no optimistic concurrency, no pagination,
and no defined size limits. Concurrent policy actions updating the same asset produce lost-update races.

### Solution: PATCH, ETags, Pagination, and Size Limits

#### 10.1 PATCH — Partial Update

```
PATCH /api/assets/<key>
Content-Type: application/merge-patch+json

{ "status": "approved", "decided_by": "alice@example.com" }
```

Follows [RFC 7396 JSON Merge Patch](https://tools.ietf.org/html/rfc7396). The server merges the
patch into the existing asset JSON. If the asset doesn't exist, returns 404 (use POST to create).

For array and deep-path operations, also accepts JSON Patch (RFC 6902):

```
PATCH /api/assets/<key>
Content-Type: application/json-patch+json

[
  { "op": "replace", "path": "/status", "value": "approved" },
  { "op": "add", "path": "/comments/-", "value": { "by": "alice", "text": "LGTM" } }
]
```

#### 10.2 ETags and Conditional Writes

Every `GET /api/assets/<key>` response includes:

```
ETag: "sha256:abcdef..."
```

Clients can then use `If-Match` on subsequent writes:

```
POST /api/assets/<key>
If-Match: "sha256:abcdef..."

{ "status": "approved" }
```

If the asset has been modified since the ETag was issued, the server returns:

```
409 Conflict
{ "error": "conflict", "message": "Asset modified since ETag was issued", "current_etag": "sha256:xyz..." }
```

This allows policy actions to implement optimistic concurrency without application-level locking:

```bash
# In policy shell action — fetch with ETag, then conditional write
etag=$(curl -sf -I "$HEX_API/assets/approvals/$id" | grep -i etag | awk '{print $2}' | tr -d '\r')
existing=$(curl -sf "$HEX_API/assets/approvals/$id")
updated=$(echo "$existing" | python3 -c "import json,sys,os; d=json.load(sys.stdin); d['status']=os.environ['HEX_EVENT_STATUS']; print(json.dumps(d))")
curl -sf -X POST "$HEX_API/assets/approvals/$id" \
  -H "If-Match: $etag" \
  -H "Content-Type: application/json" \
  -d "$updated"
# Returns 409 if concurrent write happened — policy's on_error: retry handles it
```

#### 10.3 Pagination

`GET /api/assets?prefix=<p>` now paginates:

```
GET /api/assets?prefix=approvals/&limit=20&cursor=<opaque-cursor>
→ {
    "items": [{ "key": "approvals/abc", "size": 512, "updated_at": "..." }, ...],
    "next_cursor": "<opaque>",
    "has_more": true
  }
```

Default `limit`: 50. Maximum `limit`: 500. `cursor` is an opaque string returned in `next_cursor`.

#### 10.4 Size Limits

| Limit | Default | Configurable via |
|-------|---------|-----------------|
| Single asset max size | 10 MB | `hex config set assets.max_size 50MB` |
| Total assets storage | 1 GB | `hex config set assets.total_limit 5GB` |

Exceeding limits returns `413 Payload Too Large` with a descriptive error. Extensions needing
large binary storage (CSV imports, video files) should use their own `ext.db` with a BLOB column,
or write files to a declared directory rather than using the asset API.

---

## 11. Extension Configuration Schema (New — addresses Marketplace gap)

### Problem

Each extension invents its own configuration format stored in assets. The marketplace (and
`hex extension config`) cannot render a generic configuration UI without a schema.

### Solution: `config_schema:` in `extension.yaml`

```yaml
config_schema:
  - key: github_token
    type: secret
    title: "GitHub Personal Access Token"
    description: "Needs 'repo' and 'actions:read' scopes."
    required: true

  - key: repos
    type: array
    items: { type: string, pattern: "^[\\w-]+/[\\w-]+$" }
    title: "Repositories to Watch"
    description: "One per line, format: owner/repo"
    default: []

  - key: poll_interval_seconds
    type: integer
    title: "Poll Interval"
    description: "How often to check for new build runs (seconds)."
    default: 60
    min: 30
    max: 3600
```

#### Field Types

| `type` | Storage | Input widget |
|--------|---------|-------------|
| `string` | hex asset | text input |
| `integer` / `number` | hex asset | number input with min/max |
| `boolean` | hex asset | toggle |
| `array` | hex asset | multi-line text |
| `secret` | secrets API | password input (never shown after save) |
| `enum` | hex asset | dropdown (requires `values:` list) |

#### CLI Access

```bash
hex extension config hex-cicd                  # interactive prompt for all required fields
hex extension config hex-cicd --get repos      # print value
hex extension config hex-cicd --set repos '["mrap/hex-foundation"]'
hex extension config hex-cicd --set github_token  # prompts securely (no echo)
```

#### Storage

Non-secret values are stored as hex assets under `ext-config/<extension-name>/<key>`. This
means they survive `hex extension upgrade` (assets are user-space) and are queryable via the
asset API by the extension itself. Secret-typed values are routed to the secrets API automatically.

---

## 12. Extension Management REST API (New — addresses Marketplace gap)

### Problem

A marketplace extension needs to invoke `hex extension install/remove` but these are CLI commands
only. Shelling out to `hex` is fragile and requires knowing the binary path.

### Solution: Management Endpoints Behind `extension_management` Capability

```yaml
requires:
  capabilities:
    - extension_management   # high-privilege capability, requires explicit user grant
```

```
GET    $HEX_API/extensions              → list of installed extensions (same as hex extension list --json)
GET    $HEX_API/extensions/<name>       → single extension detail
POST   $HEX_API/extensions              body: { "source": "github:user/ext" } → installs
DELETE $HEX_API/extensions/<name>       → removes
POST   $HEX_API/extensions/<name>/start → starts extension server (same as hex extension start)
POST   $HEX_API/extensions/<name>/stop  → stops extension server
GET    $HEX_API/extensions/<name>/logs  → last N lines of extension logs
```

The `extension_management` capability is **not granted by default**. It must be explicitly
granted at install time:

```bash
hex extension install ./hex-marketplace --grant extension_management
# Prompts: "hex-marketplace is requesting extension_management capability. Allow? [y/N]"
```

Without this grant, calls to management endpoints return `403 Forbidden`.

---

## 13. Updated `extension.yaml` Full Schema (v2)

The following is the complete v2 schema, incorporating all additions:

```yaml
# extension.yaml — v2 schema
name: my-extension
version: "2.0.0"
title: "My Extension"
description: "What it does"
author: "user@example.com"
license: "MIT"

engines:
  hex: ">=0.9.0 <3.0.0"

# Extension database (new in v2)
extension_db: true
migrations:
  - db/migrations/001_initial.sql

named_queries:
  my_query:
    sql: SELECT * FROM things WHERE status = :status

# Scheduled tasks (new in v2)
schedules:
  - name: daily-job
    cron: "0 0 * * *"
    command: scripts/daily.sh
    timeout: 120s
    on_error: alert

# Configuration schema (new in v2)
config_schema:
  - key: api_token
    type: secret
    title: "API Token"
    required: true

provides:
  skills:
    - skills/SKILL.md
  policies:
    - policies/my-policy.yaml
  commands:
    - commands/hex-report
  agents:
    - agents/my-agent/charter.yaml
  sse_topics:
    - name: my.topic.name
      schema:
        type: object
        properties:
          field: { type: string }
      producer: scripts/producer.sh
      interval: 60s
  views:
    - name: my-view
      path: /ext/my-view
      entry: views/my-view/index.html
      title: "My View"
      nav: true
      sse_topics: [my.topic.name]
      access:
        mode: owner_only    # allow_all | owner_only | tailnet_only | allowlist
  widgets:
    - name: my-widget
      title: "My Widget"
      entry: widgets/my-widget/widget.html
      placement: sidebar
      size: compact
      sse_topics: [my.topic.name]
  proxies:
    - name: my-server
      path: /ext/my-server
      upstream: auto         # or explicit http://localhost:PORT
      title: "My Server"
      lifecycle:
        start_command: "python3 server/app.py"
        stop_signal: SIGTERM
        stop_timeout: 10s
        restart:
          policy: on-failure
          max_attempts: 5
          delay: 5s
          delay_multiplier: 2.0
          reset_after: 300s
        health_check:
          path: /health
          interval: 30s
          timeout: 5s
          failure_threshold: 3
          initial_delay: 5s
        logs:
          path: "~/.hex/audit/ext-{name}.log"
          max_size: 10MB
          rotate_count: 5

requires:
  capabilities:
    - events
    - assets
    - messaging
    - sse
    - agent_harness
    - secrets            # new in v2: opt-in for secret store access
    - extension_management  # new in v2: high-privilege, explicit user grant required

sandbox:
  mode: subprocess

upgrade:
  strategy: preserve_user_data
```

---

## 14. Updated Phase Roadmap

### Phase 1 — Foundation (v0.8.x) ✓ Same as v1

### Phase 2 — Install + CLI (v0.9.x) — Updated

- [ ] `hex extension install/list/remove/upgrade` (unchanged)
- [ ] `~/.hex/extensions/index.yaml` management (unchanged)
- [ ] CLI command extensions (unchanged)
- [ ] Install-time checksum snapshot (unchanged)
- [ ] **NEW:** Port allocation registry (`~/.hex/ports.yaml` + `upstream: auto`)
- [ ] **NEW:** `extension_db:` + migration runner (SQLite creation + WAL mode)
- [ ] **NEW:** `$EXT_DB_PATH` injection into subprocess environments
- [ ] **NEW:** `config_schema:` parsing + `hex extension config` command
- [ ] **NEW:** Secrets capability + OS keychain integration

### Phase 3 — Hooks + SSE (v0.10.x) — Updated

- [ ] Extension-declared SSE topics (unchanged)
- [ ] **UPDATED:** Policy action vocabulary: `sse_publish`, `emit_event`, `http_call`, error handling, timeouts
- [ ] **NEW:** Full `HEX_EVENT_*` environment variable injection spec
- [ ] **NEW:** Internal event canon (`hex.asset.written`, `hex.asset.deleted`, etc.)
- [ ] **NEW:** Policy chain loop guard (max depth 5)
- [ ] **NEW:** `schedules:` block + cron scheduler
- [ ] **NEW:** Named query REST endpoint (`GET $HEX_API/ext/<name>/db/<query>`)
- [ ] `HEX_API` environment variable injection (unchanged)

### Phase 4 — Agents + UI (v1.0.x) — Updated

- [ ] Agent behavior extensions (unchanged)
- [ ] UI view extensions (unchanged)
- [ ] `hex extension enable/disable` (unchanged)
- [ ] Extension Host subprocess isolation (unchanged)
- [ ] **NEW:** `POST $HEX_API/agent/dispatch` + streaming SSE output
- [ ] **NEW:** `GET /api/me` identity endpoint + `hexUser()` SDK function
- [ ] **NEW:** View ACLs (`access:` block) + Tailscale WhoIs resolver
- [ ] **NEW:** `X-Hex-User` header injection on proxied requests
- [ ] **UPDATED:** Asset API: PATCH (Merge + JSON Patch), ETags, pagination, size limits
- [ ] **NEW:** Full `lifecycle:` spec on proxy declarations
- [ ] **NEW:** `hex extension logs <name>` command

### Phase 5 — Ecosystem (v1.x+) — Updated

- [ ] Extension registry / discovery (unchanged)
- [ ] Hex as MCP host (unchanged)
- [ ] Hex as MCP server (unchanged)
- [ ] **NEW:** Management REST API + `extension_management` capability
- [ ] **NEW:** Extension marketplace UI extension (now unblocked by management API + config schema)

---

## 15. Revised Tier Viability

With v2 changes applied:

| # | Use Case | Tier | v1 Status | v2 Status | Key changes that unblock it |
|---|----------|------|-----------|-----------|----------------------------|
| 1 | Activity feed | static | ✅ Works | ✅ Works | None needed |
| 2 | Kanban board | reactive | ⚠️ Partial | ✅ Works | Gap 3 (sse_publish action), Gap 4 (hex.asset.written event), Gap 10 (ETags) |
| 3 | Prediction markets | full | ⚠️ Partial | ✅ Works | Gap 6 (port registry), Gap 9 (lifecycle) |
| 4 | CI/CD dashboard | reactive | ⚠️ Partial | ✅ Works | Gap 2 (secrets), Gap 3 (env vars) |
| 5 | AI chat routing | full | ❌ Blocked | ✅ Works | Gap 7 (agent dispatch) |
| 6 | Approval workflow | reactive | ⚠️ Awkward | ✅ Works | Gap 1 (ext_db), Gap 3 (action vocab), Gap 8 (schedules) |
| 7 | Metrics dashboard | full | ⚠️ Partial | ✅ Works | Gap 6 (port registry), Gap 9 (lifecycle) |
| 8 | Collaborative annotations | full | ❌ Blocked | ✅ Works | Gap 5 (user identity), Gap 6 (port registry) |
| 9 | Notification center | reactive→full | ❌ Blocked | ✅ Works | Gap 2 (secrets), Gap 8 (schedules) |
| 10 | Extension marketplace | full | ❌ Blocked | ✅ Works | §12 (management API), §11 (config schema) |
| 11 | Financial tracker | full | ⚠️ Partial | ✅ Works | Gap 6 (port registry), Gap 9 (lifecycle) |

All 11 use cases from `deep-use-cases.md` are unblocked by v2.

---

*Previous: `docs/extensibility/deep-use-cases.md` — use cases and gap analysis*
*Previous: `docs/extensibility/proposal.md` — v1 architecture (still valid for unchanged sections)*
