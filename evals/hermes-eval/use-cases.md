# Hermes Hex Migration — Use Cases

_Extracted from: me/learnings.md (68KB), CLAUDE.md (642 lines), evolution/observations.md, evolution/reflection-log.md, and recent Hermes sessions._
_Generated: 2026-04-03_

---

## Use Case Table

| ID | Category | Use Case | Frequency | Criticality | Source |
|---|---|---|---|---|---|
| UC-001 | Memory/Search | Search past decisions by topic keyword | Daily | High | CLAUDE.md SO #1; learnings.md |
| UC-002 | Memory/Search | Search people profiles by name or relationship | Weekly | High | CLAUDE.md people/ layout |
| UC-003 | Memory/Search | Search project history and status | Daily | High | CLAUDE.md SO #1; .hermes.md |
| UC-004 | Memory/Search | Rebuild FTS5 memory index after bulk writes | Weekly | Medium | CLAUDE.md memory rebuild commands |
| UC-005 | Memory/Search | Retrieve session transcript summaries by date | Weekly | Medium | CLAUDE.md raw/transcripts/ |
| UC-006 | Memory/Search | Search evolution observations for recurring friction | Weekly | Medium | evolution/observations.md |
| UC-007 | Memory/Search | Search learnings.md for specific behavioral patterns | Weekly | High | learnings.md (68KB, indexed) |
| UC-008 | Delegation/BOI | Dispatch research task with proper spec format | Daily | Critical | CLAUDE.md SO #15; learnings.md line 141+ |
| UC-009 | Delegation/BOI | Dispatch implementation task to BOI with Verify section | Daily | Critical | CLAUDE.md SO #11, #28 |
| UC-010 | Delegation/BOI | Check BOI queue status (running/pending/failed) | Daily | High | CLAUDE.md boi-delegation check; .hermes.md |
| UC-011 | Delegation/BOI | Decompose multi-phase work into parallel DAG before dispatch | Weekly | High | CLAUDE.md SO #29; learnings.md line 197 |
| UC-012 | Delegation/BOI | Monitor overnight BOI run for failures | Weekly | High | CLAUDE.md SO #30; observations.md |
| UC-013 | Delegation/BOI | Write BOI spec with ### t-N: headings, PENDING status, Spec/Verify sections | Daily | Critical | CLAUDE.md spec format; learnings.md |
| UC-014 | Delegation/BOI | Dispatch adversarial critic review on completed work | Weekly | High | CLAUDE.md SO #12; learnings.md |
| UC-015 | Delegation/BOI | Wire cross-spec dependencies mechanically (not verbal promises) | Weekly | High | CLAUDE.md SO #32 |
| UC-016 | Communication | Draft iMessage via imsg-safe with verified contact | Daily | Critical | SOUL.md imsg-safe; learnings.md iMessage section |
| UC-017 | Communication | Send Slack message to channel (concise, no markdown tables) | Daily | Critical | SOUL.md Slack preferences; learnings.md |
| UC-018 | Communication | Search Gmail for receipts, confirmations, itineraries | Weekly | High | SOUL.md SO #36; gws gmail |
| UC-019 | Communication | Draft email with lead-with-ask structure | Weekly | High | learnings.md communication style |
| UC-020 | Communication | Send location with verified Google Maps link | Occasional | Medium | learnings.md location sharing |
| UC-021 | Communication | Put copyable text (URLs, commands) in separate chat bubbles | Weekly | Medium | learnings.md iMessage formatting |
| UC-022 | Communication | Flag unreplied pings from people in messages | Daily | High | CLAUDE.md SO #5 |
| UC-023 | Context Management | Persist notable context to correct file immediately after message | Daily | Critical | CLAUDE.md SO #2; persist-after-every-message |
| UC-024 | Context Management | Log decision with date/context/reasoning/impact | Daily | High | CLAUDE.md SO #3; decision-logging-rule |
| UC-025 | Context Management | Add task or deadline to todo.md | Daily | High | CLAUDE.md todo.md layout |
| UC-026 | Context Management | Save raw unprocessed input to raw/ directory | Weekly | Medium | CLAUDE.md raw/ |
| UC-027 | Context Management | Distill raw input into canonical locations (people/, projects/, decisions/) | Weekly | High | CLAUDE.md distillation protocol |
| UC-028 | Context Management | Create/update person profile in people/{name}/profile.md | Weekly | High | CLAUDE.md people/ layout; learnings.md |
| UC-029 | Context Management | Run /hex-checkpoint or context-save to persist mid-session | Weekly | Medium | CLAUDE.md checkpoint; .claude/commands/ |
| UC-030 | Context Management | Acquire coordination lock before writing shared files | Daily | High | CLAUDE.md SO #34; .hermes.md SO-3 |
| UC-031 | Daily Practice | Set daily landings with L1-L4 priority tiers | Daily | Critical | CLAUDE.md daily-practice; landings skill |
| UC-032 | Daily Practice | Update landing status when work completes or blocks | Daily | Critical | CLAUDE.md SO #9; observations.md |
| UC-033 | Daily Practice | Generate morning brief from todo.md and landings | Daily | High | CLAUDE.md daily-briefing |
| UC-034 | Daily Practice | Set weekly targets on Mondays | Weekly | High | CLAUDE.md landings/weekly/ |
| UC-035 | Daily Practice | Prep meeting with context/attendees/agenda/risks/talking points | Daily | High | CLAUDE.md meeting-prep |
| UC-036 | Daily Practice | Manage open threads across days (carry state, next action) | Daily | High | CLAUDE.md open-threads |
| UC-037 | Daily Practice | Append timestamped changelog entry to today's landings | Daily | Medium | CLAUDE.md changelog format |
| UC-038 | Daily Practice | Run landings dashboard in tmux for live status | Daily | Medium | CLAUDE.md landings-dashboard.sh |
| UC-039 | Decision Support | Run conjecture-criticism before committing to architecture | Weekly | High | CLAUDE.md SO #16; conjecture-criticism skill |
| UC-040 | Decision Support | Structure decision with options, trade-offs, recommendation | Weekly | High | CLAUDE.md decision-logging; hex-decide |
| UC-041 | Decision Support | Surface pending evolution suggestions at morning brief | Daily | Medium | CLAUDE.md evolution/suggestions.md |
| UC-042 | Decision Support | Validate architectural direction against current intent before presenting | Weekly | High | learnings.md line 286 |
| UC-043 | Decision Support | Present pre-output critique checklist before recommending | Daily | High | CLAUDE.md pre-output-critique-gate |
| UC-044 | Code/Development | Dispatch code review to fresh subagent with adversarial instructions | Weekly | High | CLAUDE.md SO #12 |
| UC-045 | Code/Development | Follow TDD (red-green) before applying any fix | Weekly | High | CLAUDE.md SO #35; learnings.md TDD section |
| UC-046 | Code/Development | Isolate code changes in git worktree before mutating | Weekly | High | CLAUDE.md SO #26 |
| UC-047 | Code/Development | Security-vet third-party tools before wiring to credentials | Occasional | High | CLAUDE.md SO #20 |
| UC-048 | Code/Development | Run eval/tests before declaring work done | Daily | Critical | CLAUDE.md SO #14, #35 |
| UC-049 | Code/Development | Sync environment changes to ai-native-env repo | Weekly | Medium | learnings.md environment tooling; SO #10 |
| UC-050 | Code/Development | Group git commits logically (not one mega-commit, not one per file) | Weekly | Medium | learnings.md workflow preferences |
| UC-051 | People/Relationships | Read contact profile before sending message on Mike's behalf | Daily | Critical | SOUL.md SO #35; people/ directory |
| UC-052 | People/Relationships | Update person profile after learning new info in session | Weekly | High | CLAUDE.md distillation protocol |
| UC-053 | People/Relationships | Cross-reference people profiles when prepping meetings | Weekly | High | CLAUDE.md meeting-prep |
| UC-054 | People/Relationships | Track relationship threads and follow-ups | Weekly | Medium | CLAUDE.md people/ layout |
| UC-055 | Self-Improvement | Track friction in evolution/observations.md via evolution_db.py | Daily | High | CLAUDE.md SO #13; observations.md |
| UC-056 | Self-Improvement | Write improvement proposal to evolution/suggestions.md | Weekly | High | CLAUDE.md improvement-engine |
| UC-057 | Self-Improvement | Run session reflection (/reflect or hex-reflect) | Weekly | High | reflection-log.md; reflect skill |
| UC-058 | Self-Improvement | Create new skill when pattern appears 3+ times | Monthly | High | CLAUDE.md skill-self-creation |
| UC-059 | Self-Improvement | Update existing skill when it's found wrong or incomplete | Weekly | High | SOUL.md skills instructions |
| UC-060 | Self-Improvement | Log recurrence of reflection issues (R-NNN tracking) | Weekly | Medium | reflection-log.md schema |
| UC-061 | Self-Improvement | Measure improvement with quantitative metrics (fewer corrections, faster convergence) | Monthly | High | learnings.md line 192; evolution/metrics.md |
| UC-062 | Proactive Behavior | Cross-reference new info against todo.md on every message | Daily | Critical | CLAUDE.md standing-orders; .hermes.md |
| UC-063 | Proactive Behavior | Surface risks and trade-offs before being asked | Daily | High | CLAUDE.md pre-output-critique-gate |
| UC-064 | Proactive Behavior | Flag when BOI spec has failed 5+ iterations without progress | Weekly | High | CLAUDE.md SO #27 |
| UC-065 | Proactive Behavior | Use cron jobs for reactive/scheduled behavior (not manual polling) | Weekly | Medium | SOUL.md SO #31; cron skill |
| UC-066 | Proactive Behavior | Simplify response when Mike signals overwhelm | Occasional | Medium | learnings.md calibrate response complexity |
| UC-067 | Proactive Behavior | Detect and flag unrealistic/uniform eval results | Weekly | High | CLAUDE.md SO #21; learnings.md eval section |
| UC-068 | Proactive Behavior | Proactively surface open threads from previous day at morning brief | Daily | High | CLAUDE.md open-threads; morning brief |
| UC-069 | Memory/Search | Search email via gws CLI before asking Mike for info | Daily | High | SOUL.md SO #36; gws gmail |
| UC-070 | Communication | Manage Slack channels (create, decorate with emoji topic, bookmarks) | Occasional | Medium | SOUL.md Slack channel decoration |

---

## Category Summary

| Category | Count | Top Criticality |
|---|---|---|
| Memory/Search | 8 | High |
| Delegation/BOI | 8 | Critical |
| Communication | 7 | Critical |
| Context Management | 8 | Critical |
| Daily Practice | 8 | Critical |
| Decision Support | 5 | High |
| Code/Development | 7 | Critical |
| People/Relationships | 4 | Critical |
| Self-Improvement | 7 | High |
| Proactive Behavior | 8 | Critical |
| **Total** | **70** | |

---

## Key Patterns Observed (from sources)

### From learnings.md
- Mike wants BOI for everything except single-edit fixes. "It's better to use it than to not." (SO #15, recurred 3+ times)
- Mike corrects context bleed (carrying details across unrelated requests) — UC-015 prevention
- Mike expects TDD before fixes (R-entry recurrence: R-047 at count 11)
- Mike's "handle it" means take action, not present options — critical for UC-016/017
- Short declarative statements = action requests, not closures
- Landings updates missed during flow state (R-033 at count 4) — UC-032 is highest recurrence
- Inline implementation instead of BOI (R-013 at count 3) — UC-008/009 critical
- Failing to search before guessing (R-014) — UC-001 through UC-007

### From observations.md
- Landings dashboard goes stale (open) — UC-032 failure
- Agent waited for approval before dispatch despite clear directive — UC-008 failure
- Monolithic spec instead of parallel DAG — UC-011 failure
- BOI specs failed overnight unmonitored — UC-012 gap

### From reflection-log.md
- R-033 (landings not updated) recurred 4 times — most critical daily practice gap
- R-047 (inline vs BOI) recurred 11 times — highest recurrence overall
- R-013 (inline implementation) at count 3 — BOI delegation is highest-friction area
- R-090 (transcript truncation causing missed reflections) at count 2

### From recent Hermes sessions
- Mike explicitly said Hex Hermes should use mrap-hex as reference benchmark
- Active tasks: Investigate Hex Hermes system, compare against mrap-hex
- Mike expects system-level investigation, not just task execution
