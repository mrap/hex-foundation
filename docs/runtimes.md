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

---

<!-- merged from task branch exec-envelope (operator conflict resolution 2026-09-07): duplicate h1 dropped, all content kept -->


Verified facts about the agent runtimes hex must target for Codex parity. Each
fact carries the exact command that produced it. All probes ran against
codex-cli 0.153.4 on macOS (Apple Silicon), logged in with ChatGPT auth.

Probe convention used throughout: every probe runs against a throwaway
`CODEX_HOME` that only holds a copy of `auth.json`, and against a git
initialized temp project. The setup is:

```bash
T=$(mktemp -d /tmp/hex-p0-t1.XXXXXX)
mkdir -p "$T/home"
cp ~/.codex/auth.json "$T/home/auth.json"
chmod 600 "$T/home/auth.json"
PROJ="$T/proj"; mkdir -p "$PROJ/.codex"
( cd "$PROJ" && git init -q && git commit -q --allow-empty -m init )
# Trust the project layer. IMPORTANT: use the resolved physical path.
REALPROJ=$(cd "$PROJ" && pwd -P)   # macOS resolves /tmp to /private/tmp
printf '[projects."%s"]\ntrust_level = "trusted"\n' "$REALPROJ" > "$T/home/config.toml"
```

Gotcha that bit every project layer probe: on macOS `/tmp` is a symlink to
`/private/tmp`, and Codex canonicalizes the project path before matching it
against `[projects."..."]` trust keys. A trust entry keyed on the `/tmp/...`
spelling never matches, so the project `.codex/config.toml` is silently ignored.
Always key the trust entry on `pwd -P` (the `/private/tmp/...` form).

## Exec envelope

`codex exec --json` prints one JSON object per line (JSONL). A one turn run
emits exactly four event types, in order: `thread.started`, `turn.started`,
`item.completed`, `turn.completed`.

```bash
CODEX_HOME="$T/home" codex exec --json -m gpt-5.4-mini -C "$PROJ" -s read-only \
  "Reply with exactly the single word: hello" < /dev/null
```

Result (captured, scrubbed, in `tests/fixtures/codex/exec-envelope.jsonl`):

- `{"type":"thread.started","thread_id":"<THREAD_ID>"}` carries the only thread
  identifier in the stream. It is a UUID; scrub it in fixtures.
- `{"type":"turn.started"}` has no fields.
- `{"type":"item.completed","item":{"id":"item_0","type":"agent_message","text":"hello"}}`
  carries the assistant text. Tool calls appear as their own `item.completed`
  events with `item.type` of `command_execution` (see Shell environment policy).
- `{"type":"turn.completed","usage":{...}}` carries token usage. Observed keys:
  `input_tokens` (11635), `cached_input_tokens` (8448),
  `cache_write_input_tokens` (0), `output_tokens` (18),
  `reasoning_output_tokens` (11).

The human readable progress and all `INFO` tracing go to stderr, never to the
`--json` stdout stream, so `--json` stdout is safe to parse line by line.

## Output schema

`--output-schema FILE` forwards the schema to the model API as a strict
structured output (`response_format` named `codex_output_schema`). Codex does
not soften or fall back: a schema the API rejects fails the whole turn.

Flat object schema (`additionalProperties:false`, three required fields), in
`tests/fixtures/codex/exec-output-schema.json`:

```bash
CODEX_HOME="$T/home" codex exec --json -m gpt-5.4-mini -C "$PROJ" -s read-only \
  --output-schema "$T/schema-flat.json" -o "$T/result-flat.json" \
  "Return a JSON object for a task titled 'demo', count 3, not done." < /dev/null
```

Result (exit 0): the `-o` file holds exactly the required keys and nothing else,
`{"title":"demo","count":3,"done":false}`
(`tests/fixtures/codex/exec-output-schema.result.json`). Conformance is strict.

Nested object schema (a `meta` object with its own required fields), in
`tests/fixtures/codex/exec-output-schema.nested.json`:

```bash
CODEX_HOME="$T/home" codex exec --json -m gpt-5.4-mini -C "$PROJ" -s read-only \
  --output-schema "$T/schema-nested.json" -o "$T/result-nested.json" \
  "Return a JSON object titled 'demo' with meta.count 2 and meta.tags ['a','b']." < /dev/null
```

Result (exit 0): `{"title":"demo","meta":{"count":2,"tags":["a","b"]}}`
(`tests/fixtures/codex/exec-output-schema.nested.result.json`). Nested objects
are supported and strictly conformed.

Top level `oneOf` schema, in `tests/fixtures/codex/exec-output-schema.oneof.json`:

```bash
CODEX_HOME="$T/home" codex exec --json -m gpt-5.4-mini -C "$PROJ" -s read-only \
  --output-schema "$T/schema-oneof.json" -o "$T/result-oneof.json" \
  "Return a JSON object with kind 'number' and value 7." < /dev/null
```

Result (exit 1): no `-o` file is written. The stream emits an `error` event and
a `turn.failed` event carrying the API rejection
(`tests/fixtures/codex/exec-output-schema.oneof.error.json`):
`invalid_request_error`, code `invalid_json_schema`, message
`Invalid schema for response_format 'codex_output_schema': In context=(), 'oneOf' is not permitted.`,
status 400. There is no fallback; the process exits non zero.

Takeaway for parity: Codex enforces the OpenAI structured output subset. Flat
and nested objects with `additionalProperties:false` and `required` conform.
Constructs the API disallows (top level `oneOf`) fail the turn rather than
degrade.

## Stdin prompt

The positional `-` is not required to read a prompt from stdin. When stdin is a
pipe, Codex reads it as the prompt whether or not `-` is given. When a positional
prompt is also supplied, piped stdin is appended to it as a `<stdin>` block.
The help text states this and the probes confirm it. Test prompt: 174206 bytes
of filler ending with the codeword `PLATINUM-WALRUS-42` and an instruction to
reply with only that codeword.

```bash
# Case 1: positional '-' with piped stdin
CODEX_HOME="$T/home" codex exec --json -m gpt-5.4-mini -C "$PROJ" -s read-only - < "$T/big.txt"
# Case 2: no positional at all, piped stdin
CODEX_HOME="$T/home" codex exec --json -m gpt-5.4-mini -C "$PROJ" -s read-only   < "$T/big.txt"
# Case 3: positional prompt plus piped stdin
CODEX_HOME="$T/home" codex exec --json -m gpt-5.4-mini -C "$PROJ" -s read-only \
  "There is a codeword near the end of the appended text. Reply with only that codeword." < "$T/big.txt"
```

Result:

- Case 1 (`-`): the assistant reply is `PLATINUM-WALRUS-42`. The codeword sits at
  byte 174000 or so, so echoing it proves the full 170 KB prompt reached the
  model. One run first returned a safety refusal that still referenced the
  hidden codeword (proving it read the tail); an immediate re run echoed the
  codeword. The refusal is model nondeterminism on the adversarial framing, not
  a `-` behavior difference.
- Case 2 (no positional): reply `PLATINUM-WALRUS-42`. Same stdin as case 1, so
  `-` is confirmed optional.
- Case 3 (positional plus stdin): reply `PLATINUM-WALRUS-42`. Stderr logs
  `Reading additional input from stdin...`; the piped text is appended to the
  positional prompt as a `<stdin>` block.

## Shell environment policy

The tool shell inherits the launching process environment by default, including
variables whose names look like secrets. There is no default name based
stripping of `KEY`, `SECRET`, or `TOKEN` variables. Probe: export four vars in
the launching shell, then ask the model to run a shell command that lists the
matching variable names.

```bash
# Default policy
MY_TEST_KEY=1 MY_TEST_SECRET=1 MY_TEST_TOKEN=1 PLAIN=1 \
CODEX_HOME="$T/home" codex exec --json -m gpt-5.4-mini -C "$PROJ" -s workspace-write \
  "Run this exact shell command and report its full stdout verbatim (names only, no values): env | grep -E 'KEY|SECRET|TOKEN' | cut -d= -f1 | sort" < /dev/null

# Same prompt, inherit all of the parent environment
MY_TEST_KEY=1 MY_TEST_SECRET=1 MY_TEST_TOKEN=1 PLAIN=1 \
CODEX_HOME="$T/home" codex exec --json -m gpt-5.4-mini -C "$PROJ" -s workspace-write \
  -c shell_environment_policy.inherit=all \
  "Run this exact shell command and report its full stdout verbatim (names only, no values): env | grep -E 'KEY|SECRET|TOKEN' | cut -d= -f1 | sort" < /dev/null
```

The model makes a real shell tool call (visible in the stream as an
`item.completed` with `item.type` `command_execution`, wrapping the command in
the user shell). The `cut -d= -f1 | sort` above emits the matching names; the
counts below are the length of each list. Matching variable names seen by the
tool shell:

- Default policy (5 names): `CLAUDE_CODE_MESSAGING_TOKEN`, `MY_TEST_KEY`,
  `MY_TEST_SECRET`, `MY_TEST_TOKEN`, `STARSHIP_SESSION_KEY`.
- With `-c shell_environment_policy.inherit=all` (4 names):
  `CLAUDE_CODE_MESSAGING_TOKEN`, `MY_TEST_KEY`, `MY_TEST_SECRET`, `MY_TEST_TOKEN`.

The load bearing signal is that `MY_TEST_KEY`, `MY_TEST_SECRET`, and
`MY_TEST_TOKEN` reached the tool shell under both policies. These three are ad
hoc exports in the launching shell that no shell profile sets, so their presence
in the tool shell can only be parent environment inheritance. That proves there
is no default name based stripping, independent of any shell wrapping question.
The 5 versus 4 difference does not weaken that inference, but its cause is worth
recording rather than dismissing. In these two runs the command was executed as
`/bin/zsh -lc "..."` under the default policy and `/bin/zsh -c "..."` under
`inherit=all`. The login form (`-lc`) re sources the user profile, which sets
`STARSHIP_SESSION_KEY` fresh; the non login form does not, which accounts for the
one extra name. What this probe does not settle is why the invocation form
changed between the two runs: whether the `shell_environment_policy.inherit` key
itself governs whether the tool shell is a login shell, or the form varied for an
unrelated reason. Phase 2 and 3 care whether the Codex tool shell re sources the
user profile, so this stays an open follow-up (recorded in the ledger), not a
closed result.

Parity implication: a hex process that shells out to `codex exec` must assume
the model's tool shell can read every secret in the hex process environment.
Scrub or drop secrets before launching Codex; do not rely on a default policy to
hide them. The model itself refused to echo secret values verbatim, but that is
a model behavior, not an environment guarantee.

## Prompt input debug

`codex debug prompt-input "<prompt>"` renders the model visible input as a JSON
array of messages. It has no `-C` flag, so run it from inside the project
directory. It makes no model call, so it does not consume quota. The array holds
one message per input block; each message is
`{type:"message", id, role, content, internal_chat_message_metadata_passthrough}`
with `content` a list of text parts. A one turn setup emits five messages: three
`developer` role (system and tool preamble), then two `user` role. The first
`user` message carries three separate text parts, tagged in
`internal_chat_message_metadata_passthrough.content_item_kinds` as
`plugins.recommendations`, `agents_md.instructions`, and
`environments.environment_context`. The project `AGENTS.md` is its own
`agents_md.instructions` part (embedded as `# AGENTS.md instructions for <path>`
wrapping an `<INSTRUCTIONS>` block); it sits alongside a distinct
`<environment_context>` part, not inside it. The second `user` message is the
user prompt. Parity gates that read this fixture should locate the `AGENTS.md` by
matching the `agents_md.instructions` kind string, not by indexing a fixed
`content` position: Codex may add or reorder parts (for example, the
`plugins.recommendations` part is absent when no plugins are recommended), which
would shift positional offsets. The scrubbed and truncated shape is in
`tests/fixtures/codex/prompt-input.json` (every string over 200 chars is cut with
a note of its original length).

The probe uses a 40 KB `AGENTS.md` (40975 bytes on disk) with an early marker
`CODEWORD-EARLY-ALPHA` near the top and a tail marker `CODEWORD-TAIL-OMEGA` past
the 39 KB mark, plus `project_doc_max_bytes = 131072` in the project
`.codex/config.toml`.

```bash
( cd "$PROJ" && CODEX_HOME="$T/home" codex debug prompt-input \
    "What are the codewords in my project instructions?" )
```

Result:

- With the trust entry keyed on the wrong `/tmp/...` path, the project config is
  ignored, the default `project_doc_max_bytes` (32 KB) applies, and the tail
  marker is absent (the `AGENTS.md` bearing message stops around 32 KB). This is
  the trust path gotcha described at the top of this file.
- With the trust entry keyed on `pwd -P` (`/private/tmp/...`), the project config
  applies and the `AGENTS.md` reaches the model in full. Measured on the raw
  (unredacted) output: `grep -c CODEWORD-TAIL-OMEGA out.json` returns 1, and a
  python `len()` over the joined text parts of the `AGENTS.md` bearing user
  message returns 45357 chars, holding both `CODEWORD-EARLY-ALPHA` and
  `CODEWORD-TAIL-OMEGA`. That 45357 is the whole first `user` message (the three
  parts joined: `plugins.recommendations` plus `agents_md.instructions` plus
  `environments.environment_context`), not the `AGENTS.md` alone. The `AGENTS.md`
  text is its own part, embedded as `# AGENTS.md instructions for <path>` wrapping
  an `<INSTRUCTIONS>` block; the fixture's truncation note records that part's
  original length as 41071 chars, which is the 40975 byte on-disk file plus the
  `# AGENTS.md instructions ...` / `<INSTRUCTIONS>` wrapper.
- The same full delivery happens with a CLI override, no project config or trust
  entry needed:
  `codex debug prompt-input -c project_doc_max_bytes=131072 "..."` echoes both
  markers.

Parity implication: to feed a large hex `AGENTS.md` to Codex, raise
`project_doc_max_bytes` (default 32 KB truncates silently). Either set it in a
trusted project `.codex/config.toml` (trust keyed on the resolved path) or pass
`-c project_doc_max_bytes=<n>` on the command line.
