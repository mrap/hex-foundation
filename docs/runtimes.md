# Codex and Claude runtime parity notes

Facts recorded during the Codex parity spikes (2026-09-07). Every fact carries the exact command that produced it. Absolute temp paths are redacted to `<TMP>`; account identifiers are redacted to `<REDACTED>`.

## Skills and instruction files

Recorded 2026-09-07 against codex-cli 0.153.4 and Claude Code 2.1.263. Probes ran in a temp project with a temp `CODEX_HOME` seeded only with a copy of `~/.codex/auth.json`. Setup:

```
T=$(mktemp -d) && mkdir -p "$T/home"
cp ~/.codex/auth.json "$T/home/auth.json" && chmod 600 "$T/home/auth.json"
mkdir -p "$T/proj" && (cd "$T/proj" && git init -q)
cp -R system/skills/hex-doctor "$T/skillcopy/hex-doctor"
mkdir -p "$T/proj/.agents/skills"
ln -s "$T/skillcopy/hex-doctor" "$T/proj/.agents/skills/hex-doctor"
```

### Codex discovers project skills from `.agents/skills/`

Codex 0.153.4 surfaces skills to the model in a `<skills_instructions>` developer message. It reads two skill roots: `r0` = `$CODEX_HOME/skills/.system` (built-in system skills) and `r1` = `<project>/.agents/skills` (project skills). Command:

```
cd "$T/proj" && CODEX_HOME="$T/home" codex debug prompt-input "hi"
```

The first developer message rendered these roots and skills (temp paths redacted):

```
### Skill roots
- `r0` = `<TMP>/home/skills/.system`
- `r1` = `<TMP>/proj/.agents/skills`
### Available skills
- imagegen: ... (file: r0/imagegen/SKILL.md)
- openai-docs: ... (file: r0/openai-docs/SKILL.md)
- plugin-creator: ... (file: r0/plugin-creator/SKILL.md)
- skill-creator: ... (file: r0/skill-creator/SKILL.md)
- skill-installer: ... (file: r0/skill-installer/SKILL.md)
- hex-doctor: Validate hex agent structure and repair issues. ... (file: r1/hex-doctor/SKILL.md)
## Hook payloads

Facts recorded 2026-09-07 against codex-cli 0.153.4 (ChatGPT auth). Every probe
used a temp CODEX_HOME plus a temp git project. The same dump hook was installed
in BOTH layers: the user layer `$CODEX_HOME/hooks.json` and the project layer
`<proj>/.codex/hooks.json`. The dump hook is a silent script that writes each
hook invocation's stdin JSON to its own file and exits 0. Hooks ran headless via
`--dangerously-bypass-hook-trust` (no interactive trust is possible headless; a
trusted-hash run is T3's concern).

Setup (abridged):

```
T=$(mktemp -d) && mkdir -p "$T/home" "$T/proj/.codex"
cp ~/.codex/auth.json "$T/home/auth.json" && chmod 600 "$T/home/auth.json"
# dump.sh writes stdin to a per-probe log dir, silent, exit 0
# hooks.json registers dump.sh for all 12 events; copied to $T/home/hooks.json
#   and $T/proj/.codex/hooks.json
printf '[projects."%s"]\ntrust_level = "trusted"\n' "$T/proj" > "$T/home/config.toml"
( cd "$T/proj" && git init -q && git commit --allow-empty -qm init )
```

### Both hook layers fire

Every event fired twice under one run: once from the user-layer
`$CODEX_HOME/hooks.json` and once from the project-layer
`<proj>/.codex/hooks.json`. Both layers are read and both run. Command:

```
CODEX_HOME="$T/home" codex exec --json -m gpt-5.4-mini -s workspace-write \
  --dangerously-bypass-hook-trust -C "$T/proj" \
  "Run the shell command: echo hello-from-probe-A . After it runs, reply with the single word DONE and stop." \
  < /dev/null
```

The two-layer conclusion is not just a doubled count: the run's own notices name
both files by path (from the probe D stream, scrubbed):

```
clamping SessionEnd hook timeout to 3s in <TMP>/home/hooks.json
clamping SessionEnd hook timeout to 3s in <TMP>/proj/.codex/hooks.json
```

### Common envelope

Most events share: `session_id`, `turn_id`, `transcript_path`, `cwd`,
`hook_event_name`, `model`, `permission_mode`. Under `codex exec` the approval
policy is `never`, so `permission_mode` reads `"bypassPermissions"` in every
payload (this reflects exec's default approval policy, not the hook-trust
bypass). `SessionStart` and `SessionEnd` omit `turn_id`; `SessionEnd` also omits
`model` and `permission_mode`. Fixtures live under `tests/fixtures/codex/hooks/`
(temp paths, ids, and account fields scrubbed to `<TMP>`, `<SESSION_ID>`,
`<TURN_ID>`, `<TOOL_USE_ID>`, `<AGENT_ID>`, `<HOME_DIR>`).

### Per-event notes

- SessionStart carries `source`. Observed values: `"startup"` (normal start) and
  `"compact"` (a fresh SessionStart fires after an auto-compaction). Fixtures:
  `session-start.json`, `session-start-compact.json`.
- UserPromptSubmit carries `prompt` (the raw user text). Fixture:
  `user-prompt-submit.json`.
- PreToolUse carries `tool_name`, `tool_input`, `tool_use_id`. The shell tool is
  reported as `tool_name: "Bash"`; patches as `tool_name: "apply_patch"`.
  Fixtures: `pre-tool-use-shell.json`, `pre-tool-use-apply-patch.json`.
- PostToolUse adds `tool_response` to the PreToolUse shape. Fixture:
  `post-tool-use.json`.
- Stop carries `stop_hook_active` (observed `false`) and `last_assistant_message`
  (observed `"DONE"`). Both keys are present. Fixture: `stop.json`.
- SessionEnd fires on exec completion. It carries `reason` (observed `"other"`)
  and a minimal envelope (`session_id`, `transcript_path`, `cwd`,
  `hook_event_name`). Its hook timeout is clamped to 3s regardless of the
  configured value (the run emits a notice `clamping SessionEnd hook timeout to
  3s`). Fixture: `session-end.json`.

### apply_patch from a subdirectory

Run from a subdirectory with `-C <proj>/sub`, editing a pre-created file:

```
CODEX_HOME="$T/home" codex exec --json -m gpt-5.4-mini -s workspace-write \
  --dangerously-bypass-hook-trust -C "$T/proj/sub" \
  "Edit the file target.txt in the current directory: append a third line ... Use the apply_patch tool ..." \
  < /dev/null
```

The PreToolUse payload's `cwd` is the subdirectory, and the patch body in
`tool_input.command` names the file cwd-relative (`*** Update File: target.txt`),
NOT as an absolute path and NOT relative to the git root. Patch paths are
resolved against the invocation cwd. Fixture: `pre-tool-use-apply-patch.json`.

### Subagent events (multi_agent)

`multi_agent` is a stable feature and effective by default; the probe added
`--enable multi_agent` and asked the model to spawn one subagent:

```
CODEX_HOME="$T/home" codex exec --json -m gpt-5.4-mini -s workspace-write \
  --enable multi_agent --dangerously-bypass-hook-trust -C "$T/proj" \
  "Launch one subagent (use your agent/task tool) to compute 2+2 and report ..." \
  < /dev/null
```

Findings:

- The project skill `hex-doctor`, reached through the `.agents/skills/hex-doctor` symlink, is surfaced. Codex follows the symlink; the model sees the skill under root `r1`.
- Codex renders only the `name` and `description` frontmatter keys. The hex-doctor `SKILL.md` carries an extra `version: 1.0.0` frontmatter key and a `<!-- # sync-safe -->` comment. Neither reaches the model and neither triggers a warning. Extra frontmatter keys are silently ignored (observed on the `codex debug prompt-input` surface; the `codex exec` surface is recorded below).
- Codex seeds built-in system skills into a fresh `CODEX_HOME` on first use. `ls "$T/home/skills/.system"` lists a `.codex-system-skills.marker` file plus `imagegen`, `openai-docs`, `plugin-creator`, `review-agent`, `skill-creator`, and `skill-installer`. Six of these are surfaced to the model; `review-agent` exists on disk but is not in the model-visible list.

Live enumeration on the `codex exec` path confirms the same discovery and confirms no frontmatter warning:

```
cd "$T/proj" && CODEX_HOME="$T/home" codex exec --output-schema "$T/a-schema.json" -o "$T/a-out.json" \
  -s read-only -m gpt-5.4-mini "List the exact names of every skill available to you." < /dev/null
```

- Exit 0. The `-o` structured output conformed to the flat schema and returned: `imagegen`, `openai-docs`, `plugin-creator`, `skill-creator`, `skill-installer`, `hex-doctor`, plus two plugin-namespaced entries (`deep-research-work:deep-research`, `plugin-management:plugin-management`). The two namespaced entries are a model self-report artifact and are not in the authoritative `<skills_instructions>` block; treat `codex debug prompt-input` as the source of truth for what the model actually sees.
- `codex exec` stderr logged `codex_skills_extension::host_service: skills cache cleared` and a feature list including `SkillSearch`, `SkillMcpDependencyInstall`, and `Plugins`. There was no frontmatter, unknown-field, or version-key warning on the exec path either. Codex silently ignores unrecognized `SKILL.md` frontmatter keys (the hex `version` key and the `<!-- # sync-safe -->` comment) on both the prompt-input and exec surfaces.

### Codex reads a CLAUDE.md via `project_doc_fallback_filenames`

Codex reads only `AGENTS.md` as a project doc by default. Point it at `CLAUDE.md` with the user-config key `project_doc_fallback_filenames`. Setup: a temp project holding only `CLAUDE.md` (codeword `FALLBACKWORD-9931`) and no `AGENTS.md`; the key written as a top-level key in the temp user config:

```
printf 'project_doc_fallback_filenames = ["CLAUDE.md"]\n' > "$T/home/config.toml"
cd "$T/projb" && CODEX_HOME="$T/home" codex debug prompt-input "What codeword is in my project instructions?"
```

With the key set, the model-visible prompt contains a project-doc block (temp path redacted):

```
# AGENTS.md instructions for <TMP>/projb
<INSTRUCTIONS>
# Project Notes
The project codeword is FALLBACKWORD-9931.
</INSTRUCTIONS>
```

Control: with `$T/home/config.toml` removed, the same command yields zero matches for `FALLBACKWORD-9931`. So the fallback key is what makes `CLAUDE.md` reach the model; without it `CLAUDE.md` is ignored. Codex labels the injected `CLAUDE.md` block as `AGENTS.md instructions`, treating the fallback file as an AGENTS.md-equivalent project doc.

`project_doc_fallback_filenames` is accepted under `--strict-config` (it is a recognized key in 0.153.4, not a STOP condition). Note `codex debug prompt-input` does not accept `--strict-config`; only `codex exec` gates config strictly. The live check both validates the key and confirms the codeword reaches the model:

```
cd "$T/projb" && CODEX_HOME="$T/home" codex exec --strict-config -s read-only -m gpt-5.4-mini \
  "What codeword is in my project instructions? Answer with only the codeword." < /dev/null
```

Exit 0 (strict-config did not reject the key) and the model answered `FALLBACKWORD-9931`.

### Claude Code and a symlinked CLAUDE.md

Claude Code 2.1.263 follows a symlinked `CLAUDE.md`. To separate "symlink followed" from "AGENTS.md read natively" (Claude also reads `AGENTS.md`), the test dir uses distinct codewords: a real `AGENTS.md` with `ALPHA-AGENTS-4471`, a real `INSTRUCTIONS-REAL.md` with `BRAVO-SYMLINK-8802`, and `CLAUDE.md` as a symlink to `INSTRUCTIONS-REAL.md`. This deviates from the brief's `CLAUDE.md -> AGENTS.md` layout on purpose: a shared codeword could not tell the two read paths apart. The Claude call ran with a temp `CLAUDE_CONFIG_DIR` seeded with copies of `~/.claude/.credentials.json` and `~/.claude.json` so auth works while all writes stay isolated (the real `~/.claude.json` mtime was unchanged before and after):

```
CT=$(mktemp -d)
cp ~/.claude/.credentials.json "$CT/.credentials.json" && chmod 600 "$CT/.credentials.json"
cp ~/.claude.json "$CT/.claude.json" && chmod 600 "$CT/.claude.json"
cd "$T/projc" && CLAUDE_CONFIG_DIR="$CT" claude -p --model sonnet --output-format json \
  --dangerously-skip-permissions "List every codeword that appears in your project instructions. Include all of them."
rm -rf "$CT"
```

Result (`is_error: false`, one turn): `The codeword in my project instructions is: BRAVO-SYMLINK-8802`. Only `BRAVO-SYMLINK-8802` appeared; `ALPHA-AGENTS-4471` did not.

Findings:

- Claude Code follows the `CLAUDE.md` symlink and reads its target. `BRAVO-SYMLINK-8802` is reachable only through the `CLAUDE.md` symlink, and it came back, so the symlink is followed (not skipped for being a symlink).
- With a `CLAUDE.md` present, the sibling `AGENTS.md` codeword (`ALPHA-AGENTS-4471`) did not surface. In this configuration `CLAUDE.md` is the project-instruction file Claude used; `AGENTS.md` did not contribute its codeword. This is an observation about precedence in the presence of `CLAUDE.md`, not a claim that Claude never reads `AGENTS.md`.
- Auth isolation note: `CLAUDE_CONFIG_DIR` redirects both the config file and the credentials lookup. Pointing it at an empty temp dir yields `Not logged in`; seeding it with a copy of `~/.claude/.credentials.json` (plus `~/.claude.json`) restores auth while keeping the real user config untouched.

### Parity summary

- Codex project skills live under `.agents/skills/<name>/SKILL.md`; symlinked skill dirs are followed; only `name` and `description` frontmatter are used and extra keys are ignored silently. Where Claude Code discovers project skills was not probed in this spike; a shared `SKILL.md` body still needs to be reachable from whatever root each runtime scans, and Codex following symlinks makes a single physical skill dir bridgeable.
- Codex reads `AGENTS.md` by default and reads `CLAUDE.md` only when `project_doc_fallback_filenames` includes it. Claude Code reads `CLAUDE.md` by default and follows a `CLAUDE.md` symlink. A `CLAUDE.md -> AGENTS.md` symlink is therefore a workable single-source-of-truth bridge for Claude; for Codex the `project_doc_fallback_filenames = ["CLAUDE.md"]` key is the bridge in the other direction.
- SubagentStart carries `agent_id` and `agent_type` (observed `"default"`); its
  `session_id` is the PARENT session. Fixture: `subagent-start.json`.
- SubagentStop carries `agent_id`, `agent_type`, `agent_transcript_path` (the
  subagent's own rollout), `stop_hook_active`, and `last_assistant_message` (the
  subagent's answer, observed `"4"`). Fixture: `subagent-stop.json`.
- The parent's orchestration tool calls are `spawn_agent` and
  `multi_agent_v1wait_agent`; each produced its own PreToolUse and PostToolUse.
- UserPromptSubmit DOES fire for the subagent's prompt. In the subagent run
  UserPromptSubmit fired for both the parent prompt and the subagent prompt
  (`"Compute 2+2 and report only the result succinctly."`).
- PreToolUse and PostToolUse DO fire for the subagent's own tool calls. A second
  probe instructed the subagent to run `echo i-am-the-subagent`; that Bash call
  produced its own PreToolUse and PostToolUse.
- Stop does NOT fire separately for the subagent. Only the main agent emits Stop;
  the subagent's terminal hook is SubagentStop.

### Compaction events

Reachable headless by shrinking the context window and driving several turns:

```
CODEX_HOME="$T/home" codex exec --json -m gpt-5.4-mini -s workspace-write \
  -c model_context_window=16000 --dangerously-bypass-hook-trust -C "$T/proj" \
  "Do the following one command at a time ... (1) run 'seq 1 400'; ... (5) run 'seq 1601 2000'. After all five, reply DONE." \
  < /dev/null
```

PreCompact and PostCompact both fired, each carrying `trigger` (observed
`"auto"`) plus the common envelope. After the auto-compaction a new SessionStart
fired with `source: "compact"`. Fixtures: `pre-compact.json`,
`post-compact.json`. Limitation: each fixture was scrubbed independently, so
`session-start.json` and `session-start-compact.json` both show `<SESSION_ID>`
and the committed fixtures cannot answer whether an auto-compaction keeps the same
session id or starts a new thread. A fresh live run is needed to settle that.

### Events not reached headless

- PermissionRequest did not fire under the invocations probed here, which all ran
  `approval_policy = never` (the exec default; no interactive approval requests are
  raised). The `codex exec --approve-for-me` path routes approval requests through
  automated review and was not probed, so PermissionRequest may be reachable there.
- Interrupt did not fire under the one-shot runs probed here; it needs an interrupt
  signal, which these headless runs did not produce. Not probed via other paths.

### HookStarted / HookCompleted are NOT in the --json stream

Grepping the captured `--json` stream found no `HookStarted`/`HookCompleted`
lines (also checked case-insensitive and snake_case). Command:

```
grep -iE 'hookstarted|hookcompleted|hook_started|hook_completed' probeA.jsonl
```

The `--json` stream event types are `thread.started`, `turn.started`,
`turn.completed`, `item.started`, `item.completed`. Hook activity surfaces only
as `item.completed` records of `type: "error"` (for example the hook-trust bypass
warning and the timeout-clamp notices); there is no dedicated hook lifecycle
event in the stream. A consumer that needs hook observability must read the hook
process side effects, not the JSON stream.

### Manual TUI check (S0.11, cannot run headless)

One interactive TUI turn is not reproducible headless. Manual step for Mike:
start `codex` (interactive TUI) inside a project that has the dump hook
installed, submit one prompt that triggers a shell tool call, and confirm the
same event sequence lands in the hook log dir (SessionStart, UserPromptSubmit,
PreToolUse, PostToolUse, Stop) plus any PermissionRequest raised when a command
needs approval. The interactive PermissionRequest payload is the one field this
headless spike could not capture; record its shape when performed.
