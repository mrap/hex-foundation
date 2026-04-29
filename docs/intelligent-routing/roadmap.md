# Intelligent Routing + Autonomous Memory — Implementation Roadmap

> Synthesizes findings from:
> - `memory-architectures.md` (t-1) — architectural survey
> - `memory-benchmark.md` (t-2) — FTS5 vs semantic benchmark on hex data
> - `routing-proposal.md` (t-3) — four-layer session routing design
> - `consolidation-design.md` (t-4) — dreaming system (autonomous memory maintenance)
>
> **Date:** 2026-04-27 | **Author:** BOI Worker q-919 (Iteration 5)

---

## Summary

Three research areas converge into a single phased plan. The key insight from the benchmark and routing design is that **most high-value work requires no new infrastructure** — the existing FTS5 index, sqlite-vec table, route-message-llm.py, and hex-events scheduler provide everything needed for Phase 1 and most of Phase 2.

| Phase | Theme | Timeline | Est. Effort |
|-------|-------|----------|-------------|
| Phase 1 | Foundation — memory + distillation | Week 1–2 | 8–12h |
| Phase 2 | Session lifecycle automation | Week 3–5 | 15–25h |
| Phase 3 | Full session manager + advanced memory | Month 2–3 | 30–50h |

---

## Phase 1: Foundation (Weeks 1–2)

**Principle:** Highest impact per hour. All items use existing infrastructure. No new daemons. Low blast radius.

### 1A — Hybrid memory search (FTS5 + semantic RRF)

**What:** Add a `hybrid_search()` function that runs FTS5 and semantic in parallel, then merges with Reciprocal Rank Fusion (RRF). Wire it into agent context loading.

**Why now:** The benchmark found FTS5 misses 100% of paraphrase queries (the type Mike most often uses) and semantic catches them all. This is the highest-recall improvement available with existing infrastructure.

**Deliverables:**
- `system/scripts/hybrid_search.py` — ~50 lines, `hybrid_search(query, k=5) -> list[Chunk]`
- Update hex agent memory retrieval path to call `hybrid_search()` instead of raw FTS5
- Query routing: if query contains 3+ specific keywords → FTS5 only (faster); otherwise → hybrid

**Effort:** 2–4 hours
**Dependencies:** None (FTS5 + sqlite-vec already installed and working)
**Latency impact:** +12ms per query (~5ms FTS5 → ~25ms hybrid). Acceptable.

---

### 1B — Fix embedding model mismatch

**What:** The current `vec_chunks` table holds 384-dim vectors (all-MiniLM-L6-v2), but the config targets Qwen3-Embedding-0.6B (1024 dims). Run `memory_schema_migrate.py` then a full re-index.

**Why now:** The benchmark notes this inconsistency. Qwen3 shows better paraphrase alignment on technical content (m1-semantic-demo: 10/10 abstract queries). Fixing this before wiring hybrid search avoids embedding drift between old and new chunks.

**Deliverables:**
- Run `memory_schema_migrate.py` to recreate `vec_chunks` at float[1024]
- Run hex-memory incremental indexing (overnight, ~8–12h on CPU / faster with MPS)
- Verify: query `SELECT count(*) FROM vec_chunks` returns ~50k rows at 1024 dims

**Effort:** 1 hour setup + overnight unattended run
**Dependencies:** None (Qwen3-0.6B already in config, fastembed or sentence-transformers installed)
**Risk:** Low — re-index is non-destructive (raw text unchanged, only vector index rebuilt)

---

### 1C — Session distillation (dreaming component 1)

**What:** After each session ends, automatically extract learnings, decisions, and action items from the transcript and write to `me/learnings.md`, `me/decisions/`, and `todo.md`. Replaces manual `/hex-reflect`.

**Why now:** This is the highest-value dreaming component and has no dependencies on Phase 2 session infrastructure. It uses existing `hex.session.stopped` events (or a 5-minute idle fallback) and the existing `coordination.py` lock. The cost is $0.005/session (~$1/month at current volume).

**Deliverables:**
- `system/scripts/dream/distill_session.py` — haiku-based extraction + dedup
- `system/scripts/dream/llm_client.py` — shared Anthropic SDK wrapper with prompt caching
- `system/scripts/dream/dream_state.py` — read/write `.hex/dream-state.json`
- `system/events/policies/dream-distill-session.yaml` — hex-events policy
- Test: run against a real session transcript from `raw/transcripts/`

**Effort:** 4–6 hours
**Dependencies:** Anthropic SDK (already used in hex), `hex.session.stopped` event (or idle timer as fallback)
**Risk:** Low — writes are append-only with coordination locks; nothing is deleted

---

### 1D — Structured classification response from route-message-llm.py

**What:** Extend the router's JSON output to include `classification: { intent, topic, urgency, topic_keywords }` alongside the existing per-agent confidence list.

**Why now:** This is a backward-compatible prompt extension with no latency or cost increase. It unblocks Phase 2 (session affinity requires topic signal) and is trivial to implement.

**Deliverables:**
- Update `system/scripts/route-message-llm.py` prompt to emit `classification` block
- Update response parsing to extract + log the classification
- No behavior change for existing callers (new field is additive)

**Effort:** 1–2 hours
**Dependencies:** None
**Risk:** Minimal — model output format change; regression-test with 5 sample messages

---

### Phase 1 Success Criteria

- [ ] `hybrid_search("storing authentication tokens")` returns relevant result (FTS5 would miss this)
- [ ] `vec_chunks` table holds 1024-dim vectors; benchmark queries still hit at 100%
- [ ] Session ends → `me/learnings.md` updated within 10 minutes (no manual `/hex-reflect`)
- [ ] `route-message-llm.py` response includes `classification.topic` for all queries

---

## Phase 2: Session Lifecycle Automation (Weeks 3–5)

**Principle:** Build on Phase 1 foundations. Add session affinity, automated context loading, and lifecycle management. Requires one new lightweight component (`hex-session-manager` proto) and one DB migration.

### 2A — session_affinity table + passive logging

**What:** Add `session_affinity` table to `~/.cc-connect/sessions.db` (migration). Log affinity events passively for 1 week before enabling routing decisions.

**Why this order:** Starting with passive observation validates the classification signals before they drive real routing behavior. A week of data reveals if topic classification is accurate enough for affinity keying.

**Deliverables:**
- DB migration: `session_affinity` table (schema from routing-proposal.md)
- Logging hook: when a message is routed, record `(affinity_key, session_id, last_active_at)` without changing routing behavior
- Dashboard query: `SELECT affinity_key, count(*), avg(context_pct) FROM session_affinity GROUP BY affinity_key`

**Effort:** 2–3 hours
**Dependencies:** Phase 1D (topic signal from classifier)
**Risk:** Low — no behavior change; read-only observability layer

---

### 2B — context_loader.py

**What:** Token-budget-aware context assembly. Given an affinity key and topic keywords, returns a `ContextBundle` that fits within the model's window: checkpoint + hybrid memory chunks + project context + behavioral corrections.

**Why:** This is the core artifact that makes "seamless resume" possible. Without it, each session starts cold.

**Deliverables:**
- `system/scripts/context_loader.py` — `load_context(affinity_key, topic_keywords) -> ContextBundle`
- Unit tests: verify token budget not exceeded, checkpoint loaded first, memory chunks ranked by relevance
- Integration: wire into cc-connect's message dispatch (inject context as conversation prefix)

**Effort:** 4–6 hours
**Dependencies:** Phase 1A (hybrid search), Phase 2A (affinity table for checkpoint_path lookup)
**Risk:** Medium — context injection into running sessions requires cc-connect protocol understanding; validate against one channel first

---

### 2C — Auto-checkpoint (70%) and auto-compact (85%)

**What:** When context_pct reaches 70%, dispatch a background checkpoint. When it reaches 85%, hard-compact before next message delivery.

**Key open question to resolve:** Context_pct tracking. Recommended approach: use ECC `context-budget` skill heuristics (message count + time as proxy), with coordination.py lock on checkpoint dispatch to prevent race conditions.

**Deliverables:**
- `system/scripts/session_manager.py` — lifecycle state machine (ACTIVE → CHECKPOINT → COMPACT)
- hex-events action: `session-idle-checkpoint` (triggers for sessions idle >60 min)
- Add `timer.tick.hourly` handler to scan `session_affinity` for idle sessions
- cc-connect integration: check `context_pct` before each message dispatch

**Effort:** 5–8 hours
**Dependencies:** Phase 2A (affinity table), Phase 2B (context loader for re-hydration after compact)
**Risk:** Medium-High — auto-compact resets sessions; must validate checkpoint restoration before enabling in production

---

### 2D — Knowledge graph update (dreaming component 4)

**What:** After each distillation, extract entities (people, projects, tools) and relationships from the distillation JSON and update `.hex/knowledge-graph.json`.

**Why Phase 2 not Phase 1:** Depends on distillation output being stable (Phase 1C). Also benefits from having a few weeks of distillation data to validate the graph schema against real content.

**Deliverables:**
- `system/scripts/dream/update_graph.py` — haiku-based entity/relationship extraction
- `system/events/policies/dream-update-graph.yaml` — triggers on `dream.distillation.complete`
- `.hex/knowledge-graph.json` initial schema bootstrapped from existing `me/people/*.md` and `projects/*/context.md`

**Effort:** 3–4 hours
**Dependencies:** Phase 1C (distillation must be running)
**Risk:** Low — graph updates are additive; knowledge-graph.json backed up before each write

---

### 2E — Memory compression (dreaming component 2)

**What:** Nightly run: summarize transcripts >7 days old, consolidate redundant learnings, archive inactive projects (>30 days without activity).

**Why Phase 2:** Needs at least 7 days of distillation output before first useful run. Also needs the hybrid search (1A) for duplicate detection in learnings consolidation.

**Deliverables:**
- `system/scripts/dream/compress_memory.py` — batched haiku calls (max 10 transcripts/run)
- `system/events/policies/dream-memory-compression.yaml` — daily trigger at 2am UTC
- Run report: `evolution/consolidation-latest.log` (already used by consolidate.sh)

**Effort:** 3–4 hours
**Dependencies:** Phase 1C (produces distillation output), Phase 1A (semantic dedup in learnings consolidation)
**Risk:** Low — transcripts never deleted (only summarized); learnings consolidation creates new file before overwriting

---

### Phase 2 Success Criteria

- [ ] Message arrives → session_affinity lookup finds correct session for topic
- [ ] Agent starts with checkpoint preloaded (no manual context loading)
- [ ] Context at 70% → checkpoint dispatched automatically, no user action
- [ ] Session idle 60+ min → auto-checkpointed before going cold
- [ ] `.hex/knowledge-graph.json` updates within 5 min of session end
- [ ] Old transcripts compressed; `raw/transcripts/summaries/` populated

---

## Phase 3: Full Session Manager + Advanced Memory (Month 2–3)

**Principle:** Aspirational. Requires stable Phase 1+2 infrastructure and operational data to validate assumptions. Higher risk, higher payoff.

### 3A — hex-session-manager as cc-connect middleware

**What:** Extract all routing logic (classification, affinity, context loading, lifecycle) from inline scripts into a dedicated `hex-session-manager` daemon. cc-connect becomes a pure Slack ↔ CC bridge.

**Why deferred:** Phase 2 delivers the same capabilities via inline scripts. The refactor to a standalone daemon is justified once the routing logic is proven and stable — not before. Premature extraction adds operational surface area without benefit.

**Deliverables:**
- `system/session_manager/` — lightweight Python daemon (asyncio, stdio IPC with cc-connect)
- HTTP API: `POST /route` → returns `{session_id, context_bundle, lifecycle_action}`
- Multi-channel support: Slack (`slack:CHANNEL:USER`), hex-ui (`ui:UUID`), CLI (`cli:host:pid`)
- Deployment: launchd plist or hex-events `system.start` action

**Effort:** 15–20 hours
**Dependencies:** Phase 2 fully stable (affinity + context loader + lifecycle proven in production)
**Risk:** High — introducing a new daemon with IPC boundary; phased rollout with shadow mode first

---

### 3B — Pattern detection + standing order candidates (dreaming component 3)

**What:** Every 6 hours, scan recent session summaries for recurring patterns. Promote high-frequency patterns to standing order candidates. Requires new `timer.tick.6h` event in hex-events scheduler.

**Why deferred:** Needs 4+ weeks of distillation and compression output to have enough summaries to detect patterns. Also needs human validation process (Mike reviews `standing-order-candidates.md`) before automation promotes anything.

**Deliverables:**
- `system/scripts/dream/detect_patterns.py` — haiku pattern extraction + sonnet judgment
- `system/events/policies/dream-pattern-detection.yaml` — 6h trigger
- Add `timer.tick.6h` to `adapters/scheduler.py`
- `evolution/standing-order-candidates.md` — new file; Mike reviews weekly

**Effort:** 4–6 hours
**Dependencies:** Phase 2E (compression must be producing summaries for at least 2 weeks)
**Risk:** Low for detection (tagged `[AUTO]`, never auto-applied); Medium for standing order promotion (requires human sign-off before any hook is created)

---

### 3C — Memory pruning (dreaming component 5)

**What:** Weekly scan to flag stale learnings (referenced entity not mentioned in 90 days), archive superseded decisions, surface duplicates using embedding similarity.

**Why deferred:** Needs embeddings from Phase 1B (Qwen3 at 1024 dims) and 6–8 weeks of distillation history to have meaningful signal. False positive pruning is more damaging than stale entries — patience is correct here.

**Deliverables:**
- `system/scripts/dream/prune_memory.py` — deterministic rules + minimal LLM for contradiction detection
- `system/events/policies/dream-memory-pruning.yaml` — weekly trigger (Sunday 3am UTC)
- Add `timer.tick.weekly` to `adapters/scheduler.py`
- `evolution/pruning-report.md` — human-readable report; nothing auto-deleted

**Effort:** 3–4 hours
**Dependencies:** Phase 1B (Qwen3 vectors for duplicate detection), Phase 2E (compression producing summaries)
**Risk:** Low — all pruning is tagging + archiving, never deletion; recovery is simple `mv`

---

### 3D — Tiered memory (hot/warm/cold)

**What:** Hot tier: last 7 days in RAM (pre-loaded at hex-session-manager startup). Warm tier: last 30 days on-disk vector search. Cold tier: compressed FTS5 only for >30 day content.

**Why deferred:** The benchmark shows 25ms hybrid search is already fast enough for interactive use. Tiering adds complexity that's only justified once the corpus grows substantially (current: 50k chunks / 354MB — manageable without tiering). Revisit at 500k chunks.

**Deliverables:**
- Tier configuration in `~/.hex/memory-config.json`
- Hot tier: in-process numpy array loaded at startup by hex-session-manager
- Warm/cold tier: existing sqlite-vec with recency filter

**Effort:** 8–12 hours
**Dependencies:** Phase 3A (session manager as host for hot tier RAM cache)
**Risk:** Medium — memory footprint of hot tier must be bounded; evaluate corpus size before committing

---

### Phase 3 Success Criteria

- [ ] cc-connect deploys with hex-session-manager middleware; Slack routing unchanged from user perspective
- [ ] Pattern detection running; `standing-order-candidates.md` has content after 2 weeks
- [ ] Weekly pruning report generated; stale learnings tagged (not deleted)
- [ ] Tiered retrieval benchmark: hot tier latency < 5ms for recent content

---

## Full Dependency Graph

```
Phase 1A (hybrid search) ─────────────────────────────────────────┐
Phase 1B (Qwen3 reindex) ─────────────────────────────────────────┤
Phase 1C (distillation)  ────────────┬────────────────────────────┤
Phase 1D (classifier ext)────────────┤                            │
                                     ↓                            │
Phase 2A (affinity table + logging) ─┬──────────────────────────┐ │
                                     ↓                          │ │
Phase 2B (context_loader) ───────────┬──────────────────────────┤ │
                                     ↓                          │ │
Phase 2C (lifecycle: checkpoint/compact)                        │ │
                                                                │ │
Phase 2D (graph update) ← 1C ───────────────────────────────── │ │
Phase 2E (compression) ← 1A + 1C ────────────────────────────── ─ ┤
                                                                   │
Phase 3A (session-manager daemon) ← 2A + 2B + 2C stable ─────────┤
Phase 3B (pattern detection) ← 2E (2+ weeks) ─────────────────── ┤
Phase 3C (memory pruning) ← 1B + 2E (6+ weeks) ─────────────────┤
Phase 3D (tiered memory) ← 3A ──────────────────────────────────┘
```

---

## Sequenced Task List

For implementation tracking, the ordered task sequence is:

| Order | Task | Phase | Effort | Unblocks |
|-------|------|-------|--------|---------|
| 1 | Fix embedding mismatch (1B) — kick off overnight | 1 | 1h setup | 2A (dedup quality) |
| 2 | Hybrid search (1A) | 1 | 2–4h | 2B, 3D |
| 3 | Classifier extension (1D) | 1 | 1–2h | 2A |
| 4 | Session distillation (1C) | 1 | 4–6h | 2D, 2E, 3B, 3C |
| 5 | Affinity table + passive logging (2A) | 2 | 2–3h | 2B, 2C |
| 6 | context_loader.py (2B) | 2 | 4–6h | 2C, 3A |
| 7 | Knowledge graph update (2D) | 2 | 3–4h | (standalone) |
| 8 | Auto-checkpoint + compact (2C) | 2 | 5–8h | 3A |
| 9 | Memory compression (2E) | 2 | 3–4h | 3B, 3C |
| 10 | Pattern detection (3B) | 3 | 4–6h | (standalone) |
| 11 | Memory pruning (3C) | 3 | 3–4h | (standalone) |
| 12 | hex-session-manager daemon (3A) | 3 | 15–20h | 3D |
| 13 | Tiered memory (3D) | 3 | 8–12h | (final) |

**Total effort estimate:**
- Phase 1: 8–13 hours
- Phase 2: 17–25 hours
- Phase 3: 30–44 hours
- **Grand total: 55–82 hours** of focused implementation

At a realistic pace (2–3 hours/day of engineering time), Phase 1 completes in 4–6 days, Phase 2 in 2–3 weeks, and Phase 3 over the following 3–5 weeks.

---

## Cost Projection

| System | Daily cost | Monthly cost |
|--------|-----------|--------------|
| Session distillation (haiku, 5 sessions/day) | $0.025 | $0.75 |
| Memory compression (haiku, nightly) | $0.10 | $3.00 |
| Pattern detection (haiku + sonnet, 4×/day) | $0.03 | $0.90 |
| Knowledge graph update (haiku, per distillation) | $0.005 | $0.15 |
| Memory pruning (haiku, weekly) | $0.001 | $0.03 |
| **Dreaming total** | **~$0.16** | **~$4.83** |
| Hybrid search overhead (negligible — local) | $0.00 | $0.00 |
| **Grand total autonomous overhead** | **~$0.16/day** | **~$5/month** |

This is the cost to run a fully autonomous memory system on hex. It does not include interactive session costs (which are already paid).

---

## Risk Register

| Risk | Probability | Impact | Mitigation |
|------|------------|--------|------------|
| Context_pct tracking inaccurate → wrong checkpoint trigger | Medium | Medium | Start with conservative thresholds (70/85); monitor false trigger rate for 1 week |
| Distillation false positives (wrong learnings extracted) | Low-Medium | Medium | Haiku output reviewed by Mike weekly for first month; bad entries manually removed |
| Embedding re-index takes >12h, blocks 1A deployment | Low | Low | 1A can deploy with existing 384-dim vectors; re-index upgrades quality, doesn't gate functionality |
| auto-compact resets the wrong session | Low | High | Shadow mode first: log compact decisions for 3 days before enabling |
| Pattern detection promotes false patterns to standing orders | Low | High | Never auto-promote; all candidates require Mike's explicit review before any hook is created |
| Knowledge graph grows unbounded | Low | Low | Cap at 10k nodes; archive nodes with `last_mentioned` > 180 days |

---

## Decision Rationale: Phase Ordering

**Decision:** Distillation (1C) before session lifecycle (2A/2B/2C).

| Option | Description | Score |
|--------|-------------|:-----:|
| **Distillation first** | Memory health unlocked before routing; stand-alone, low-risk | 4.8 |
| Routing first | Seamless context loading before autonomous memory | 3.5 |
| Parallel | Both Phase 1 and Phase 2 started simultaneously | 2.5 |

**Margin:** 4.8 vs 3.5 — moderate

**Key trade-off:** Distillation is lower risk and produces the data that routing depends on (session summaries, checkpoint content). Starting routing first requires deploying context_loader before having well-populated checkpoints — it would load empty or stale context. Distillation first means routing has rich checkpoints to load when it arrives.

**Assumptions that could change the verdict:**
- If session context loading pain is acute (Mike frequently frustrated by cold-start), routing moves to Phase 1 alongside distillation.
- If distillation LLM costs spike unexpectedly, its position as a Phase 1 item would be reconsidered.

**Dissenting view:** Routing is the flagship user-facing feature of this initiative. Delivering seamless session resume early demonstrates momentum and provides a forcing function for distillation quality (bad distillation → bad context loads → visible regression → faster improvement cycle).
