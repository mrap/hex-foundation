# Hex System Architecture

> Entry point for the hex system. Read this first. ~15 minute read.

## What is Hex?

Hex is a self-improving AI agent system with three core components and four feedback loops:

- **hex-events** -- Event-driven policy engine (enforcement, automation)
- **Orchestrator** -- Dispatches AI tasks to workers (BOI is the default; swappable)
- **hex-ops** -- Operational glue (session management, dashboards, LaunchAgents)

The components handle execution. The feedback loops handle learning:

- **L1: Outcome to Pivot** -- Stalled KRs trigger new experiment hypotheses
- **L2: Error to Lesson** -- Corrections become mechanical behavioral guards
- **L3: Outcome to Threshold** -- Agent performance tunes fleet parameters
- **L4: Failure to Redesign** -- Cascade failures escalate to structural redesign

---

## System Diagram

```
  ┌──────────────────────────────────────────────────────────────────┐
  │  EXTERNAL SIGNALS                                                │
  │  git commits, cron timers, file changes, manual hex_emit.py     │
  └─────────────────────┬────────────────────────────────────────────┘
                        │ events
                        ▼
  ┌──────────────────────────────────────────────────────────────────┐
  │  hex-events (~/.hex-events/)                                     │
  │                                                                  │
  │  adapters/ ──► events.db (SQLite WAL) ──► policy engine         │
  │  (fswatch,                                (YAML ECA rules)      │
  │   scheduler)        ◄── deferred_events ──► actions             │
  │                          (debounce)         (shell, emit,       │
  │                                              notify, update-file)│
  └───────────────────────────┬──────────────────────────────────────┘
                              │ orchestrator.* events
                              ▼
  ┌──────────────────────────────────────────────────────────────────┐
  │  ORCHESTRATOR (pluggable — default: BOI at ~/.boi/)              │
  │                                                                  │
  │  Daemon ──► Worker 1 ──► Claude Code (isolated worktree)        │
  │         ──► Worker 2 ──► Claude Code (isolated worktree)        │
  │         ──► Worker 3 ──► Claude Code (isolated worktree)        │
  │                                                                  │
  │  Spec queue (boi.db) → dispatch → iterate → verify → commit     │
  └───────────────────────────┬──────────────────────────────────────┘
                              │ events back to hex-events
                              ▼
  ┌──────────────────────────────────────────────────────────────────┐
  │  hex-ops (.hex/scripts/, .hex/bin/)                              │
  │                                                                  │
  │  startup.sh  session.sh  landings-dashboard.sh                  │
  │  LaunchAgents: hex-eventd, fswatch adapter                       │
  │  hex binary: multi-agent harness (.hex/bin/hex)                  │
  └──────────────────────────────────────────────────────────────────┘
```

---

## Component Table

| Component | Role | Location | Test command |
|-----------|------|----------|--------------|
| **hex-events** | Event bus + policy engine | `~/.hex-events/` | `cd ~/.hex-events && python -m pytest tests/` |
| **BOI** (orchestrator) | Dispatch specs to Claude Code workers | `~/.boi/` | `cd ~/.boi && python -m pytest` |
| **hex-ops** | Session mgmt, dashboards, LaunchAgents | `.hex/scripts/`, `.hex/bin/` | Manual inspection |
| **hex-eventd** | hex-events daemon process | `~/.hex-events/hex_eventd.py` | `ps aux \| grep hex_eventd` |
| **hex harness** | Multi-agent fleet driver (Rust) | `.hex/bin/hex` | `cd .hex/harness && cargo test` |

---

## Event Flow

```
External signal
    │
    ▼
adapter or hex_emit.py  ──► INSERT into events.db (event_type, payload, source)
                                        │
                              hex-eventd polls every 2s
                                        │
                              match against policy rules (YAML, glob)
                                        │
                              evaluate conditions (ECA model)
                                        │
                    ┌───────────────────┴──────────────────────┐
                    ▼                                          ▼
              immediate actions                    deferred actions
              (shell, notify, emit,                (emit with delay/
               update-file)                         cancel_group)
                    │                                          │
                    ▼                                          ▼
              action_log table                   deferred_events table
                                                 (heapq, drained on due)
```

---

## Data Flow

| Data | Lives in | Notes |
|------|----------|-------|
| Events | `~/.hex-events/events.db` | SQLite WAL, polled every 2s |
| Deferred events | `~/.hex-events/events.db` (deferred_events table) | Debounce/delay queue |
| Action log | `~/.hex-events/events.db` (action_log table) | Audit trail |
| Spec queue | `~/.boi/boi.db` | Orchestrator state |
| Spec files | `specs/` (or project dirs) | Source of truth for tasks |
| Agent rules | `CLAUDE.md` | Standing orders, session protocol |
| Session markers | `.sessions/` | Multi-session concurrency |
| Memory index | `.hex/memory.db` (SQLite FTS5) | Full-text search over notes |
| Agent audit trail | `.hex/audit/actions.jsonl` | Append-only agent action log |
| Cost ledger | `.hex/cost/ledger.jsonl` | Per-invocation cost tracking |
| KR snapshots | `.hex/audit/kr-snapshots.jsonl` | Initiative progress tracking |
| Approach library | `.hex/audit/approach-library.jsonl` | Successful experiment patterns |
| Pivot trail | `.hex/audit/pivots.jsonl` | Initiative pivot history |
| Telemetry | `.hex/telemetry/events.db` | Append-only structured event log |

---

## Session Lifecycle

### Startup
1. `startup.sh` runs (registered via UserPromptSubmit hook or manually)
2. Environment detected, session ID registered in `.sessions/`
3. Transcripts parsed to daily markdown
4. Memory index rebuilt (incremental)
5. Integrations checked (BOI status, hex-events daemon health)
6. Evolution suggestions surfaced

### During Work
- hex-eventd runs continuously, processing events from adapters
- BOI daemon dispatches specs to Claude Code workers in isolated worktrees
- Policies fire automatically on events (git commits, file changes, BOI completions)
- Landings tracked in `landings/` directory
- Multi-agent fleet driven by `.hex/bin/hex` harness

### Session End
- `/hex-shutdown` deregisters session ID
- Checkpoint written
- Outstanding BOI work left running (daemon persists across sessions)

---

## Multi-Agent Fleet

The hex harness (`.hex/bin/hex`) drives a fleet of autonomous agents. Each agent has a charter (`projects/<agent-id>/charter.yaml`) defining its scope, KPIs, and autonomy tier.

Key commands:
```bash
hex agent fleet                    # Fleet overview
hex agent wake <id> --trigger <e>  # Run agent shift
hex agent status <id>              # Single agent detail
hex agent message <from> <to> ...  # Async inter-agent message
```

Full multi-agent reference: [multi-agent.md](multi-agent.md).

---

## Replacing Components

hex-events is the stable core. The orchestrator and hex-ops scripts are swappable.

| Component | Swappable? | Notes |
|-----------|-----------|-------|
| hex-events | No -- it's the brain | Stable API: hex_emit.py + policies YAML |
| Orchestrator | **Yes** | Must implement the orchestrator interface |
| hex-ops scripts | Yes | Shell scripts; replace or extend freely |
| Claude Code agent | Yes | Any agent that reads CLAUDE.md works |

**Orchestrator interface:** See [orchestrator-interface.md](orchestrator-interface.md) for the event contract any orchestrator must fulfill.

---

## Feedback Loop Architecture

Hex is designed as a self-improving system. Four feedback loops close the gap between "system builds things" and "system learns from results."

### Loop Dependency Graph

```
L2 (Error→Lesson) ─── independent; can close now
                         │
L1 (Outcome→Pivot) ─────┼──────────────────────┐
         │               │                      │
         ▼               │                      ▼
    Approach Library ────┘              L4 (Failure→Redesign)
         │
         ▼
L3 (Outcome→Threshold) ─── depends on approach-library.jsonl entries
```

**Implementation order:** L2 → L1 → L4 → L3 (L3 is last because it consumes patterns that L1 produces).

### L1: Outcome to Pivot

| Field | Value |
|-------|-------|
| **Trigger** | `hex-initiative-loop-v2.py` Step 8 fires every 5 runs (~30h at 6h cadence) |
| **Measures** | KR values in `kr-snapshots.jsonl` -- delta over 5 runs |
| **Action** | If no KR moved: generate pivot spec and dispatch via BOI |
| **Feeds into** | New experiment; Step 1 of next loop iteration |
| **Meta-metric** | `pivot_to_kr_move_rate`: fraction of pivots that result in KR movement within 5 subsequent runs |

### L2: Error to Lesson

| Field | Value |
|-------|-------|
| **Trigger** | Mike issues a correction; any agent logs a feedback event |
| **Measures** | Recurrence rate of same correction category |
| **Action** | Write correction to `behavioral_patterns` table; serve via `hex-memory check-behavior` at session start |
| **Feeds into** | Agent session initialization -- relevant patterns surfaced before agent acts |
| **Meta-metric** | `mean_recurrence_rate`: fraction of corrections that recur after being written as lessons |

### L3: Outcome to Threshold

| Field | Value |
|-------|-------|
| **Trigger** | `fleet-lead` wake cycle (6h); pattern library update |
| **Measures** | Agent KPI attainment rates, cost-per-action, pattern success rates |
| **Action** | Adjust agent budget, cadence, or strategy params in charter |
| **Feeds into** | Agent charter -- updated parameters govern next wake cycle |
| **Meta-metric** | Fleet-level KPI attainment rate; budget-to-outcome ratio per agent |

### L4: Failure to Redesign

| Field | Value |
|-------|-------|
| **Trigger** | 3+ consecutive pivot failures on same initiative |
| **Measures** | Pivot failure count; cascade failure events in hex-events |
| **Action** | Escalate to CoS; CoS surfaces to Mike; structural redesign spec dispatched |
| **Feeds into** | New initiative structure, not just new experiment hypothesis |
| **Meta-metric** | Time from confirmed cascade failure to structural redesign dispatch |

---

## Autonomy Tiers

Every agent operates at an autonomy tier (A0-A4). Tiers govern what actions an agent can take without human approval.

| Tier | Name | Behavior | Gate to advance |
|------|------|----------|-----------------|
| **A0** | Advisory | Read, report, no action. Outputs go to Mike or CoS. | Accuracy >= 90% over 10 wake cycles |
| **A1** | Suggest | Proposes actions with rationale. Requires human approval. | 5 consecutive approved suggestions; 0 rejected-as-wrong |
| **A2** | Auto-execute | Executes when defined gates pass. No approval needed within charter scope. | 30-day run; zero scope violations; KPIs met |
| **A3** | Proactive | Dispatches specs, pivots on failure, escalates after limit. Drives own work. | A2 sustained 60 days; pivot success >= 50%; escalation accuracy >= 80% |
| **A4** | Adaptive | Modifies own operational parameters based on measured outcomes. | A3 sustained 90 days; parameter changes consistently improve KPIs |

**Critical rule:** No agent may self-assign a higher tier. Tier advancement requires a human-reviewed BOI spec.

### charter.yaml Format

```yaml
autonomy_tier:
  current: A2
  target: A3
  assessment_date: "2026-04-26"
  advancement_gate: "Zero park events for 30 days; KPI attainment >= 80%"
  last_reviewed: "2026-04-26"
```

---

## Integration Contracts

Every contract specifies: **writer -> file/event/schema -> reader -> mismatch detection**.

### Initiative Loop to Self-Assessment

| Field | Value |
|-------|-------|
| **Writer** | `hex-initiative-loop-v2.py` Step 1 (Measure) |
| **Schema** | `kr-snapshots.jsonl`: `{"ts": ISO8601, "initiative_id": str, "kr_id": str, "value": float, "run_count": int}` |
| **Reader** | `self_improvement.py` `run_self_assess()` |
| **Mismatch detection** | Validates schema on load; logs `schema_error` to hex-events if key missing |

### Self-Assessment to Pattern Library

| Field | Value |
|-------|-------|
| **Writer** | `self_improvement.py` `_maybe_log_success()` |
| **Schema** | `approach-library.jsonl`: `{"ts": ISO8601, "initiative_id": str, "kr_id": str, "approach": str, "outcome": "pass\|fail", "kr_delta": float, "context_tags": [str]}` |
| **Reader** | `self_improvement.py` `find_applicable_patterns()`; `fleet-lead` (L3 loop) |
| **Mismatch detection** | Validates required keys; stale entries (>90 days) auto-archived |

### Corrections to Behavioral Patterns

| Field | Value |
|-------|-------|
| **Writer** | Any agent logging a correction; hex-memory ingestion |
| **Schema** | SQLite `behavioral_patterns` table: `(id, category, description, severity, created_at, recurrence_count, last_recurred, mechanical_guard_needed)` |
| **Reader** | `hex-memory check-behavior` at session start |
| **Mismatch detection** | Returns empty-safe response; logs `m7_unavailable` if DB missing |

### Initiative Loop to hex-events

| Field | Value |
|-------|-------|
| **Writer** | `hex-initiative-loop-v2.py` `_emit_event()` |
| **Schema** | `{"event": str, "agent": str, "initiative": str, "ts": ISO8601, "data": dict}` |
| **Reader** | hex-events daemon; experiment measurement/verdict/stale policies |
| **Mismatch detection** | Policies specify required event keys; daemon logs schema violations |

### Agent Charter to Harness Wake Gate

| Field | Value |
|-------|-------|
| **Writer** | Human-reviewed BOI spec (tier advancement) |
| **Schema** | `charter.yaml`: `autonomy_tier.current` field |
| **Reader** | Harness `wake.rs` initiative-progress gate |
| **Mismatch detection** | Harness reads charter tier; rejects wake completion if A2+ agent took zero actions on owned stalled KRs |

---

## The Self-Improvement Cycle

The full cycle from initiative to experiment to measurement to pivot:

```
                    ┌─────────────────────────────┐
                    │       INITIATIVE             │
                    │  (initiatives/*.yaml, KRs)   │
                    └──────────────┬──────────────┘
                                   │ KR defines success
                                   ▼
                    ┌─────────────────────────────┐
                    │       EXPERIMENT             │
                    │  (experiments/{id}.yaml)     │◀──────────────┐
                    └──────────────┬──────────────┘               │
                                   │ dispatches                    │
                                   ▼                               │
                    ┌─────────────────────────────┐               │
                    │         BOI SPEC             │               │
                    │  (hex-initiative-loop-v2.py  │               │
                    │   Step 3: dispatch)          │               │
                    └──────────────┬──────────────┘               │
                                   │ executes                      │
                                   ▼                               │
                    ┌─────────────────────────────┐               │
                    │         MEASURE              │               │
                    │  (Step 1: kr-snapshots.jsonl)│               │
                    │  Timer: every 6h             │               │
                    └──────────────┬──────────────┘               │
                                   │ every 5 runs                  │
                                   ▼                               │
                    ┌─────────────────────────────┐               │ new
                    │    STEP 8: SELF-ASSESS       │               │ experiment
                    │  (self_improvement.py        │               │
                    │   run_self_assess())         │               │
                    └──────┬──────────────┬────────┘               │
                           │              │                         │
                    KRs moved?        KRs stalled?                 │
                           │              │                         │
                           ▼              ▼                         │
               ┌───────────────┐  ┌──────────────────────────┐    │
               │ PATTERN LIBRARY│  │    PIVOT GENERATOR       │    │
               │ (approach-    │  │  (_generate_pivot())      │    │
               │  library.jsonl│  │  logs to pivots.jsonl     │    │
               │  _maybe_log_  │  └──────────┬───────────────┘    │
               │  success())   │             │ >=3 failures?       │
               └───────┬───────┘             │                     │
                       │               ┌─────┴──────┐             │
                       │               │  ESCALATE  │             │
                       │               │  (to CoS)  │             │
                       │               └────────────┘             │
                       │                     │ new hypothesis      │
                       │                     └─────────────────────┘
                       │
                       │ patterns feed
                       ▼
               ┌───────────────┐
               │  NEXT EXPER.  │
               │  seeded with  │
               │  patterns     │
               └───────────────┘
```

### Code References Per Step

| Step | File | Function/Line |
|------|------|---------------|
| Initiative to KRs | `initiatives/*.yaml` | `key_results:` block |
| Experiment dispatch | `.hex/scripts/hex-initiative-loop-v2.py` | `_dispatch_experiment()` Step 3 |
| KR snapshot write | `.hex/scripts/hex-initiative-loop-v2.py` | `_write_kr_snapshot()` in Step 1 |
| Step 8 trigger | `.hex/scripts/hex-initiative-loop-v2.py` | `run_loop()` -- `run_count % 5 == 0` gate |
| Self-assess | `.hex/scripts/self_improvement.py` | `run_self_assess()` |
| Pattern library write | `.hex/scripts/self_improvement.py` | `_maybe_log_success()` |
| Pivot generation | `.hex/scripts/self_improvement.py` | `_generate_pivot()` |
| Escalation | `.hex/scripts/self_improvement.py` | `_maybe_escalate()` |
| Pattern seeding | `.hex/scripts/self_improvement.py` | `find_applicable_patterns()` |

---

## Systems Archetypes

### Reinforcing Loops (amplify over time)

**R1: Better patterns -> Better experiments -> More KR movement -> More patterns.**
The approach library compounds: each successful experiment seeds the next one with richer context. Core growth loop.

**R2: Lower recurrence -> More agent capacity -> Better KPI attainment -> Lower recurrence.**
As L2 closes, agents spend less time on corrections and more on value work.

### Balancing Loops (regulate toward a target)

**B1: KR stalls -> Pivot -> New experiment -> KR moves.**
Without Step 8, KRs stall indefinitely with no correction signal.

**B2: Agent parks -> Harness gate rejects -> Agent forced to act.**
Without the `wake.rs` gate, an agent can park indefinitely.

### Leverage Points (descending order)

1. **Wire Step 8** -- closes L1, enables R1, enables B1. Single highest-leverage action.
2. **Build behavioral memory M7** -- closes L2, enables R2. Second highest.
3. **Add harness initiative-progress gate** -- closes B2, prevents loop bypass. Structural fix.
4. **Restore 6h cadence** -- prerequisite for correct loop timing.

---

## Design Principles

Three anti-patterns to guard against:

1. **Verify commands must verify behavior, not file existence.** Specs pass verify because verify commands test `test -f`, not functional behavior. Verify commands must run the script, parse output, and assert on content.

2. **Integration before dispatch.** Before dispatching any spec that touches a shared component, check the integration contract. If undefined, define it first.

3. **Mechanical enforcement, not textual rules.** Charter rules written as text are documentation, not enforcement. Every rule that must be enforced needs a corresponding harness gate or hex-events policy.

---

## Conflict Resolutions

| Conflict | Resolution |
|----------|-----------|
| Memory architecture: Hindsight vs sqlite-vec/LanceDB | **sqlite-vec/LanceDB is canonical.** Hindsight archived. |
| Initiative loop: v1 vs v2 | **v2 (`hex-initiative-loop-v2.py`) is canonical.** |
| Step 8 timing: 5-run cadence vs 7-day stall window | **6h cadence.** Step 8 fires every ~30h -- well inside 7-day window. |
| Charter rules vs actual agent behavior | **Textual rules are documentation; harness gates are enforcement.** |

---

## Formal Loop Registry

For use by `hex-feedback-loops.py`:

```json
{
  "loops": [
    {
      "id": "L1",
      "name": "outcome-to-pivot",
      "trigger_event": "hex.initiative.loop.step8",
      "trigger_file": ".hex/scripts/hex-initiative-loop-v2.py",
      "measure_file": ".hex/audit/kr-snapshots.jsonl",
      "action_file": ".hex/scripts/self_improvement.py",
      "feedback_file": ".hex/audit/pivots.jsonl",
      "stale_threshold_hours": 6
    },
    {
      "id": "L2",
      "name": "error-to-lesson",
      "trigger_event": "hex.agent.correction",
      "trigger_file": ".hex/memory/memory.db",
      "measure_file": ".hex/memory/memory.db",
      "action_file": "hex-memory check-behavior",
      "feedback_file": ".hex/memory/memory.db",
      "stale_threshold_hours": 24
    },
    {
      "id": "L3",
      "name": "outcome-to-threshold",
      "trigger_event": "hex.initiative.loop.completed",
      "trigger_file": ".hex/audit/approach-library.jsonl",
      "measure_file": "projects/*/charter.yaml",
      "action_file": "projects/fleet-lead/charter.yaml",
      "feedback_file": "projects/*/charter.yaml",
      "stale_threshold_hours": 48
    },
    {
      "id": "L4",
      "name": "failure-to-redesign",
      "trigger_event": "hex.initiative.cascade_failure",
      "trigger_file": ".hex/audit/pivots.jsonl",
      "measure_file": ".hex/audit/pivots.jsonl",
      "action_file": "projects/cos/state.json",
      "feedback_file": "initiatives/*.yaml",
      "stale_threshold_hours": 72
    }
  ]
}
```

---

## Further Reading

| Doc | Contents |
|-----|----------|
| [hex-events.md](hex-events.md) | Policy YAML schema, operators, actions, CLI reference, DB schema |
| [orchestrator-interface.md](orchestrator-interface.md) | Event contract, BOI setup, roll-your-own guide |
| [hex-ops.md](hex-ops.md) | Scripts reference, LaunchAgents, session protocol, memory system |
| [policies.md](policies.md) | Catalog of active policies with trigger/action/test for each |
| [multi-agent.md](multi-agent.md) | Agent fleet, harness mechanics, state model, gates, messaging, CLI |
