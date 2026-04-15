# hex

Your AI brain. All your work flows through it.

The harder you push, the better it gets. Every correction becomes a rule. Every decision becomes memory. Give it the thing you don't know how to do. Watch it fail. Correct it. Now it never fails that way again. Do that for six weeks and you have something no one else can buy — an agent that works exactly the way you think, because you forged it.

**For:** engineers on Claude Code who are tired of their agent starting from zero every session.

---

## Quick start

```bash
git clone https://github.com/mrap/hex /tmp/hex-setup
bash /tmp/hex-setup/install.sh
cd ~/hex && claude
```

Your agent walks you through setup on first run. Three questions, then you're working.

---

## What you get

### Persistent memory
Every session, hex saves context to files. Between sessions, it searches before guessing.

- **Search:** `memory_search.py "query"` — FTS5 full-text search across all your project, people, and decision files
- **Save:** Observations, decisions, and learnings written to the right place immediately — not summarized into a chat bubble that disappears
- **Index:** Incremental SQLite index rebuilt on startup, rebuilt in full on checkpoint

### Operating model
CLAUDE.md ships with a battle-tested operating model:

- **20 core standing orders** — search-verify-assert, persist immediately, parallel by default, plan before building, review before shipping, and 15 more
- **Learning engine** — observes your communication style, decision patterns, work habits. Records to `me/learnings.md` with evidence and dates. Gets more accurate over time.
- **Improvement engine** — 5 phases: detect friction patterns → log observations → propose fixes after 3+ occurrences → implement after approval → track in changelog. The system improves itself.

### Session lifecycle
hex knows where it is in every session.

- **Startup** — loads priorities, today's landings, pending reflection fixes. First run triggers onboarding.
- **Checkpoint** — distill pass, write handoff file, update landings, trigger background reflection. Use mid-session before context gets heavy.
- **Shutdown** — quick distill, deregister session, trigger transcript backup and reflection.

Context monitoring: `ACTIVE` → `WARMING` (65%) → `HOT` (80%) → `CHECKPOINT` → `FRESH`. The agent tells you when it's getting heavy.

### Compounding engine
- **Reflect** — after each session, extract learnings, identify failures, produce standing order candidates
- **Consolidate** — daily automated pass audits the operating model for contradictions, staleness, and orphaned references
- **Debrief** — weekly walk-through of projects, org signals, relationships, career direction

### Doctor + upgrade
- **Doctor** — validates structure, checks for missing files and stale config. Runs automatically after upgrade.
- **Upgrade** — pulls latest system files without touching your data. Your customizations survive via zone-based CLAUDE.md merge.

### Multi-agent support
- `CLAUDE.md` — for Claude Code
- `AGENTS.md` — for Codex, Cursor, Gemini CLI, Aider, or any agent that reads markdown

---

## How it works

The repo is the installer. Your hex instance is separate.

```
github.com/mrap/hex       ← installer (this repo)
~/hex/                     ← your instance (not a git repo)
```

Your instance has two kinds of directories:

| Directory | Owned by | Survives upgrade? |
|-----------|----------|-------------------|
| `.hex/` | System | Overwritten |
| `CLAUDE.md`, `AGENTS.md` | System + you | System zone replaced, your zone preserved |
| `me/`, `projects/`, `people/` | You | Never touched |
| `evolution/`, `landings/`, `raw/` | You | Never touched |
| `todo.md`, `specs/` | You | Never touched |

The `.hex/` directory contains everything that runs: scripts, skills, commands, hooks, and templates. You don't edit it. When hex upgrades, only this directory gets replaced.

Your data — memory, learnings, decisions, project context — lives outside `.hex/` and is never overwritten.

---

## Commands

| Command | What it does |
|---------|-------------|
| `/hex-startup` | Session init. Load priorities, landings, reflection fixes. Triggers onboarding on first run. |
| `/hex-checkpoint` | Mid-session save. Distill pass, handoff file, landings update, background reflection. |
| `/hex-shutdown` | Session close. Quick distill, deregister session, trigger Stop hooks. |
| `/hex-reflect` | Session reflection. Extract learnings, identify failures, produce standing order candidates. |
| `/hex-consolidate` | System hygiene. Audit operating model for contradictions, staleness, orphaned refs. |
| `/hex-debrief` | Weekly debrief. Walk through projects, org signals, relationships, career. |
| `/hex-decide` | Structured decision framework. Context, options, reasoning, impact. |
| `/hex-triage` | Triage pending captures from `raw/`. Route to correct locations. |
| `/hex-doctor` | Health check. Validate structure, missing files, stale config. |
| `/hex-upgrade` | Pull latest system files. Run doctor after. |

---

## Customization

CLAUDE.md has two zones:

```markdown
<!-- hex:system-start — DO NOT EDIT BELOW THIS LINE -->
... standing orders, lifecycle, learning engine — managed by hex
<!-- hex:system-end -->

## My Rules

<!-- hex:user-start — YOUR CUSTOMIZATIONS GO HERE -->
... your personal standing orders and preferences go here
<!-- hex:user-end -->
```

Add your own rules in the user zone. They survive every upgrade, byte-for-byte.

Example additions:
```markdown
<!-- hex:user-start -->
- Always check Jira before starting work on a feature
- When writing SQL, add an EXPLAIN before any destructive query
- My preferred branch naming: {type}/{ticket}-{short-description}
<!-- hex:user-end -->
```

---

## Upgrading

```bash
hex upgrade
```

What it does:
1. Backs up `.hex/` to `.hex-upgrade-backup-YYYYMMDD/`
2. Fetches latest system files
3. Replaces `.hex/` and the system zone of `CLAUDE.md`
4. Preserves your user zone in `CLAUDE.md`
5. Runs `hex doctor`
6. Prints what changed

Your data is never touched. Rollback: restore from the backup directory.

Options:
- `hex upgrade --dry-run` — show what would change without changing it
- `hex upgrade --skip-boi` — skip BOI upgrade
- `hex upgrade --skip-events` — skip hex-events upgrade

---

## Requirements

- Python 3.10+
- git
- Claude Code CLI (`claude`)

Install script checks Python and git, exits with guidance if missing. Warns (non-blocking) if `claude` isn't found.

---

## Install options

```bash
# Default: installs to ~/hex/
bash install.sh

# Custom location
bash install.sh ~/my-hex

# Skip companion systems
bash install.sh --no-boi --no-events
```

No interactive prompts during install. All interaction happens in your first Claude Code session.
