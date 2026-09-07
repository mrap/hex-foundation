# Codex parity spikes (Phase 0) ledger

Dated research entries for the Codex parity Phase 0 spikes. One entry per spike in the form: Question / Method (exact commands) / Result / Decision or Follow-up. Absolute temp paths are redacted to `<TMP>`.

## S0.5 Skill discovery and instruction-file fallbacks

Question. Does Codex 0.153.4 discover project skills placed under `.agents/skills/` (including a symlinked skill dir that carries extra hex frontmatter keys)? Does `project_doc_fallback_filenames = ["CLAUDE.md"]` make Codex read a `CLAUDE.md` when no `AGENTS.md` is present, and is the key accepted under `--strict-config`? Does Claude Code follow a symlinked `CLAUDE.md`?

Method. All Codex probes ran in a temp project with a temp `CODEX_HOME` seeded only with a copy of `~/.codex/auth.json`.

```
T=$(mktemp -d) && mkdir -p "$T/home"
cp ~/.codex/auth.json "$T/home/auth.json" && chmod 600 "$T/home/auth.json"

# (a) project skill via symlink under .agents/skills
mkdir -p "$T/proj" && (cd "$T/proj" && git init -q)
cp -R system/skills/hex-doctor "$T/skillcopy/hex-doctor"
mkdir -p "$T/proj/.agents/skills"
ln -s "$T/skillcopy/hex-doctor" "$T/proj/.agents/skills/hex-doctor"
cd "$T/proj" && CODEX_HOME="$T/home" codex debug prompt-input "hi"
# live enumeration (records exec-path stderr for frontmatter warnings):
cd "$T/proj" && CODEX_HOME="$T/home" codex exec --output-schema "$T/a-schema.json" -o "$T/a-out.json" \
  -s read-only -m gpt-5.4-mini "List the exact names of every skill available to you." < /dev/null

# (b) CLAUDE.md via project_doc_fallback_filenames, only CLAUDE.md present
mkdir -p "$T/projb" && (cd "$T/projb" && git init -q)
printf '# Project Notes\n\nThe project codeword is FALLBACKWORD-9931.\n' > "$T/projb/CLAUDE.md"
printf 'project_doc_fallback_filenames = ["CLAUDE.md"]\n' > "$T/home/config.toml"
cd "$T/projb" && CODEX_HOME="$T/home" codex debug prompt-input "What codeword is in my project instructions?"
# strict-config acceptance plus live echo (only codex exec supports --strict-config):
cd "$T/projb" && CODEX_HOME="$T/home" codex exec --strict-config -s read-only -m gpt-5.4-mini \
  "What codeword is in my project instructions? Answer with only the codeword." < /dev/null

# (c) Claude Code symlinked CLAUDE.md (disambiguating layout: distinct codewords)
mkdir -p "$T/projc"
printf '# Agents doc\n\nThe agents codeword is ALPHA-AGENTS-4471.\n' > "$T/projc/AGENTS.md"
printf '# Real instructions\n\nThe symlink codeword is BRAVO-SYMLINK-8802.\n' > "$T/projc/INSTRUCTIONS-REAL.md"
ln -s "INSTRUCTIONS-REAL.md" "$T/projc/CLAUDE.md"
CT=$(mktemp -d)  # temp CLAUDE_CONFIG_DIR so ~/.claude.json is not mutated
cd "$T/projc" && CLAUDE_CONFIG_DIR="$CT" claude -p --model sonnet --output-format json \
  "List every codeword that appears in your project instructions."
```

Result.

- (a) Codex 0.153.4 surfaces skills in a `<skills_instructions>` developer message with two roots: `r0` = `$CODEX_HOME/skills/.system` (built-in) and `r1` = `<project>/.agents/skills` (project). The symlinked `hex-doctor` skill is surfaced under `r1`, so Codex follows the symlink. Only `name` and `description` frontmatter are rendered; the extra `version: 1.0.0` key and the `<!-- # sync-safe -->` comment are dropped with no warning. A fresh `CODEX_HOME` is seeded with system skills (`imagegen`, `openai-docs`, `plugin-creator`, `review-agent`, `skill-creator`, `skill-installer` plus a `.codex-system-skills.marker`); six are surfaced to the model but `review-agent` is not. The live `codex exec --output-schema` call returned (exit 0, schema-conformant) `imagegen`, `openai-docs`, `plugin-creator`, `skill-creator`, `skill-installer`, `hex-doctor`, plus two plugin-namespaced entries (`deep-research-work:deep-research`, `plugin-management:plugin-management`) that are a model self-report artifact and are not in the authoritative `<skills_instructions>` block. Exec stderr logged `codex_skills_extension::host_service: skills cache cleared` and no frontmatter or unknown-field warning.
- (b) With `project_doc_fallback_filenames = ["CLAUDE.md"]` in the temp user config and only `CLAUDE.md` present, the model-visible prompt gains an `# AGENTS.md instructions for <TMP>/projb` block wrapping the `CLAUDE.md` body (codeword `FALLBACKWORD-9931`). Removing the key drops the block (control: zero matches). The live `codex exec --strict-config` call exited 0 (the key is recognized in 0.153.4, not a STOP condition) and the model answered `FALLBACKWORD-9931`. Note: `codex debug prompt-input` does not accept `--strict-config`; only `codex exec` gates config that way.
- (c) The live Claude call (exit 0, `is_error: false`, one turn) answered `The codeword in my project instructions is: BRAVO-SYMLINK-8802`. Only `BRAVO-SYMLINK-8802` (the symlink-target codeword) appeared; `ALPHA-AGENTS-4471` (the `AGENTS.md` codeword) did not. Claude Code 2.1.263 follows a symlinked `CLAUDE.md` and reads its target. First attempt with an empty temp `CLAUDE_CONFIG_DIR` returned `Not logged in`, so the dir was seeded with copies of `~/.claude/.credentials.json` and `~/.claude.json`; the real `~/.claude.json` mtime was unchanged before and after (isolated writes). The disambiguating layout deviates from the brief's `CLAUDE.md -> AGENTS.md` on purpose so the returned codeword pins down which read path fired.

Decision or Follow-up.

- Codex parity plan: ship hex skills to Codex as `.agents/skills/<name>/SKILL.md` (symlinks are fine); keep only `name` and `description` in frontmatter that Codex must honor (extra hex keys are ignored, not rejected). To have Codex read a hex `CLAUDE.md`, set `project_doc_fallback_filenames = ["CLAUDE.md"]` in the user config; it survives `--strict-config`.
- Claude parity: a `CLAUDE.md -> AGENTS.md` symlink lets a single instruction file feed Claude Code, since the symlink is followed. Follow-up: characterize whether Claude Code 2.1.263 reads `AGENTS.md` at all when a `CLAUDE.md` is present (this probe saw `CLAUDE.md` win and `AGENTS.md` not contribute); that precedence question is out of scope for S0.5 and belongs to a later phase.
- Isolation follow-up recorded: `CLAUDE_CONFIG_DIR` redirects both config and credential lookup, so any headless Claude probe must seed credentials into the temp config dir.
## S0.2 Codex hook payloads per event

Question: What JSON does Codex 0.153.4 send to each hook event under `codex exec`,
and which events are reachable headless? Do both the user and project hook layers
fire? Do HookStarted/HookCompleted appear in the `--json` stream?

Method: Built a temp CODEX_HOME and temp git project. Installed one silent dump
hook (writes stdin to a per-probe file, exits 0) for all 12 events in BOTH
`$CODEX_HOME/hooks.json` and `<proj>/.codex/hooks.json`. Trusted the project via
`[projects."<proj>"] trust_level = "trusted"` in the temp user config. Ran hooks
headless with `--dangerously-bypass-hook-trust`. Probes:

```
# Probe A: shell tool run (SessionStart, UserPromptSubmit, PreToolUse, PostToolUse, Stop, SessionEnd)
CODEX_HOME="$T/home" codex exec --json -m gpt-5.4-mini -s workspace-write \
  --dangerously-bypass-hook-trust -C "$T/proj" \
  "Run the shell command: echo hello-from-probe-A . ... reply ... DONE and stop." < /dev/null

# Probe B: apply_patch edit from a subdirectory (-C points at proj/sub)
CODEX_HOME="$T/home" codex exec --json -m gpt-5.4-mini -s workspace-write \
  --dangerously-bypass-hook-trust -C "$T/proj/sub" \
  "Edit the file target.txt ... Use the apply_patch tool ..." < /dev/null

# Probe C/C2: spawn one subagent (--enable multi_agent); C2 makes the subagent run a shell command
CODEX_HOME="$T/home" codex exec --json -m gpt-5.4-mini -s workspace-write \
  --enable multi_agent --dangerously-bypass-hook-trust -C "$T/proj" \
  "Launch one subagent ... compute 2+2 ..." < /dev/null

# Probe D: force compaction with a small context window
CODEX_HOME="$T/home" codex exec --json -m gpt-5.4-mini -s workspace-write \
  -c model_context_window=16000 --dangerously-bypass-hook-trust -C "$T/proj" \
  "Do the following one command at a time ... seq 1 400 ... seq 1601 2000 ... DONE." < /dev/null
```

Result:

- Both hook layers fire. Every event fired twice per run, once per layer.
- Reachable headless: SessionStart, UserPromptSubmit, PreToolUse, PostToolUse,
  Stop, SessionEnd, SubagentStart, SubagentStop, PreCompact, PostCompact.
- Not reached by the invocations probed here: PermissionRequest (all probes ran
  `approval_policy = never`; the `codex exec --approve-for-me` path routes approval
  requests and was not probed) and Interrupt (needs an interrupt signal; not probed
  via other paths).
- SessionStart `source`: `"startup"` normally, `"compact"` after an auto-compact.
- PreToolUse/PostToolUse `tool_name` is `"Bash"` for the shell tool and
  `"apply_patch"` for patches; subagent orchestration uses `spawn_agent` and
  `multi_agent_v1wait_agent`.
- apply_patch paths are cwd-relative. With `-C proj/sub`, the payload `cwd` is the
  subdirectory and the patch names the file as bare `target.txt`, not absolute and
  not git-root-relative.
- Stop has `stop_hook_active` and `last_assistant_message`. SessionEnd has
  `reason` (`"other"`) and its timeout is clamped to 3s.
- Subagents: UserPromptSubmit fires for the subagent prompt; PreToolUse and
  PostToolUse fire for the subagent's own tool calls; Stop fires only for the main
  agent, while the subagent ends with SubagentStop (which carries `agent_id`,
  `agent_type`, `agent_transcript_path`, `last_assistant_message`).
- PreCompact/PostCompact carry `trigger` (`"auto"`).
- HookStarted/HookCompleted do NOT appear in the `--json` stream. Stream types are
  `thread.started`, `turn.started`, `turn.completed`, `item.started`,
  `item.completed`. Hook notices appear only as `item.completed` error records.

Fixtures committed: tests/fixtures/codex/hooks/{session-start, session-start-compact,
user-prompt-submit, pre-tool-use-shell, pre-tool-use-apply-patch, post-tool-use,
stop, session-end, subagent-start, subagent-stop, pre-compact, post-compact}.json
(scrubbed). See docs/runtimes.md "## Hook payloads".

Decision or Follow-up: hex must not rely on the `--json` stream for hook
observability; watch hook process side effects instead. Re-verify `permission_mode`
under a trusted-hash run (not bypass) once T3 lands the hook hash, since this spike
only exercised the exec `approval_policy = never` path.

## S0.11 Interactive TUI turn (manual, cannot run headless)

Question: Does one interactive TUI turn produce the same hook sequence as
`codex exec`, and what does an interactive PermissionRequest payload look like?

Method: Not runnable in this spike. Interactive `codex` is forbidden headless and
PermissionRequest never fires under exec (`approval_policy = never`).

Result: Deferred to a manual step for Mike.

Decision or Follow-up: Manual check for Mike. Start `codex` (interactive TUI) in a
project that has the dump hook installed. Submit one prompt that triggers a shell
tool call and, if possible, one command that needs approval. Confirm the hook log
dir shows SessionStart, UserPromptSubmit, PreToolUse, PostToolUse, Stop, and any
PermissionRequest. Record the PermissionRequest payload shape; it is the one event
this headless spike could not capture.

## S0.12 apply_patch path form and subagent hook coverage

Question: Are apply_patch paths in the hook payload absolute or cwd-relative when
the run starts in a subdirectory? Do UserPromptSubmit, PreToolUse, and Stop fire
for subagents?

Method: Covered by S0.2 Probe B (subdirectory apply_patch) and Probes C/C2
(subagent). See the commands above.

Result:

- apply_patch paths are cwd-relative. Running with `-C proj/sub`, the PreToolUse
  payload `cwd` was the subdirectory and the patch body read
  `*** Update File: target.txt` (bare, relative to cwd), not an absolute path and
  not relative to the git root.
- UserPromptSubmit fires for the subagent's prompt (observed the subagent prompt
  `"Compute 2+2 ..."` as its own UserPromptSubmit).
- PreToolUse and PostToolUse fire for the subagent's own tool calls (observed the
  subagent's `echo i-am-the-subagent` Bash call).
- Stop does NOT fire for the subagent; the subagent's terminal hook is
  SubagentStop.

Decision or Follow-up: hex hook adapters that key on paths must treat apply_patch
paths as cwd-relative and resolve them against the payload `cwd`. Hook logic that
should also cover subagents can rely on UserPromptSubmit / PreToolUse / PostToolUse
firing inside subagents, but must treat SubagentStop (not Stop) as the subagent
terminal signal.

---

<!-- merged from task branch exec-envelope (operator conflict resolution 2026-09-07): duplicate h1 dropped, all content kept -->


Dated research ledger for the codex-parity Phase 0 spikes. One section per spike.
Each section states the Question, the Method with exact commands, the Result, and
the Decision or Follow-up. All probes ran against codex-cli 0.153.4 on macOS with
ChatGPT auth, using a throwaway `CODEX_HOME` and a git initialized temp project
(setup documented in docs/runtimes.md).

## S0.1 codex exec --json envelope plus --output-schema and stdin

Question. What events does `codex exec --json` emit for a one turn run, does
`--output-schema` enforce strict conformance or fall back, and does a 170 KB
prompt on stdin (with the positional `-`) reach the model?

Method. Temp CODEX_HOME with a copied auth.json, git initialized temp project.

```bash
# Envelope
CODEX_HOME="$T/home" codex exec --json -m gpt-5.4-mini -C "$PROJ" -s read-only \
  "Reply with exactly the single word: hello" < /dev/null
# Output schema: flat (3 required, additionalProperties false), nested, top-level oneOf
CODEX_HOME="$T/home" codex exec --json -m gpt-5.4-mini -C "$PROJ" -s read-only \
  --output-schema "$T/schema-flat.json"   -o "$T/result-flat.json"   "...demo, count 3, not done" < /dev/null
CODEX_HOME="$T/home" codex exec --json -m gpt-5.4-mini -C "$PROJ" -s read-only \
  --output-schema "$T/schema-nested.json" -o "$T/result-nested.json" "...meta.count 2, meta.tags [a,b]" < /dev/null
CODEX_HOME="$T/home" codex exec --json -m gpt-5.4-mini -C "$PROJ" -s read-only \
  --output-schema "$T/schema-oneof.json"  -o "$T/result-oneof.json"  "...kind number, value 7" < /dev/null
# Stdin: 174206 byte prompt, codeword at the end; three cases
CODEX_HOME="$T/home" codex exec --json -m gpt-5.4-mini -C "$PROJ" -s read-only - < "$T/big.txt"   # positional dash
CODEX_HOME="$T/home" codex exec --json -m gpt-5.4-mini -C "$PROJ" -s read-only   < "$T/big.txt"   # no positional
CODEX_HOME="$T/home" codex exec --json -m gpt-5.4-mini -C "$PROJ" -s read-only "...codeword near end..." < "$T/big.txt"
```

Result.

- Envelope: four JSONL events in order. `thread.started` (carries `thread_id`),
  `turn.started` (empty), `item.completed` (`item.type` `agent_message`, `text`
  `hello`), `turn.completed` (`usage` with `input_tokens`, `cached_input_tokens`,
  `cache_write_input_tokens`, `output_tokens`, `reasoning_output_tokens`).
  Human readable output and all tracing go to stderr. Captured in
  `tests/fixtures/codex/exec-envelope.jsonl` (thread_id scrubbed).
- Output schema: strict, no fallback. Codex forwards the schema to the API as
  `response_format` named `codex_output_schema`. Flat schema conforms exactly
  (exit 0, `{"title":"demo","count":3,"done":false}`). Nested object conforms
  exactly (exit 0, `{"title":"demo","meta":{"count":2,"tags":["a","b"]}}`). Top
  level `oneOf` is rejected by the API: `invalid_request_error`, code
  `invalid_json_schema`, `'oneOf' is not permitted`, status 400. The stream emits
  `error` and `turn.failed`, no `-o` file is written, and the process exits 1.
  Fixtures: `exec-output-schema.json` and `.result.json` (flat),
  `exec-output-schema.nested.json` and `.nested.result.json`,
  `exec-output-schema.oneof.json` and `.oneof.error.json`.
- Stdin: the codeword `PLATINUM-WALRUS-42` was echoed in all three cases, so the
  full 170 KB prompt reaches the model. The positional `-` is not required: with
  no positional and piped stdin the codeword is still echoed. With a positional
  prompt plus piped stdin, the pipe is appended as a `<stdin>` block (stderr logs
  `Reading additional input from stdin...`). One dash run first returned a safety
  refusal that still named the hidden codeword (proving it read the tail); an
  immediate re run echoed it. Refusal is model nondeterminism, not a `-`
  behavior difference.

Decision or Follow-up. Envelope shape and strict schema behavior are pinned for
the Phase 2 and 3 exec gates. Parity code must treat `turn.failed` as a non zero
exit and must not expect Codex to soften an API rejected schema. Feed prompts on
stdin with `/dev/null` closed on non prompt runs.

## S0.13 shell_environment_policy

Question. Does the default `shell_environment_policy` strip environment variable
names containing KEY, SECRET, or TOKEN from the tool shell? Two reads of the
config reference disagreed.

Method. Export four vars in the launching shell, ask the model to run a shell
command listing the matching names, under the default policy and under
`inherit=all`.

```bash
MY_TEST_KEY=1 MY_TEST_SECRET=1 MY_TEST_TOKEN=1 PLAIN=1 \
CODEX_HOME="$T/home" codex exec --json -m gpt-5.4-mini -C "$PROJ" -s workspace-write \
  "Run this exact shell command and report its full stdout verbatim (names only): env | grep -E 'KEY|SECRET|TOKEN' | cut -d= -f1 | sort" < /dev/null
# repeat with: -c shell_environment_policy.inherit=all
```

Result. No stripping. The model made a real `command_execution` tool call. Under
the default policy the tool shell saw 5 matching names: `CLAUDE_CODE_MESSAGING_TOKEN`,
`MY_TEST_KEY`, `MY_TEST_SECRET`, `MY_TEST_TOKEN`, `STARSHIP_SESSION_KEY`. Under
`inherit=all` it saw 4: the same set minus `STARSHIP_SESSION_KEY`. The load
bearing inference: `MY_TEST_KEY`, `MY_TEST_SECRET`, and `MY_TEST_TOKEN` are ad
hoc exports that no shell profile sets, so their presence in the tool shell under
the default policy can only be parent environment inheritance. That proves there
is no default name based stripping. The 5 versus 4 difference does not weaken
that inference. Its mechanism: the command was executed as `/bin/zsh -lc "..."`
in the default run and `/bin/zsh -c "..."` in the `inherit=all` run. The login
form re sources the user profile, which sets `STARSHIP_SESSION_KEY` fresh; the
non login form does not. That variable came from the profile, not the parent.
What this probe does not settle is why the invocation form changed between the
two runs (whether the `shell_environment_policy.inherit` key governs the login
shell form, or the form varied for an unrelated reason). See the follow-up below.

Decision or Follow-up. The default does not strip secret named vars, so the
plan's conditional `ignore_default_excludes` decision in runtime.toml is not
needed. The inverse risk is real: Codex tool shells inherit every secret in the
launching process environment. hex must scrub or drop secrets before launching
`codex exec` rather than rely on a default policy. Record this in
docs/runtimes.md (done).

Open follow-up. Confirm whether `shell_environment_policy.inherit` controls
whether the tool shell is a login shell (`-lc`) or not (`-c`). Cheap probe for a
later phase: ask the model, under each policy, to run `echo "$0 $-"` and report
whether the shell was login. Phase 2 and 3 need this because a login tool shell
re sources the user profile and can pull in profile only variables.

## S0.14 codex debug prompt-input

Question. What is the JSON shape of `codex debug prompt-input`, and does a 40 KB
AGENTS.md reach the model in full when `project_doc_max_bytes` is raised?

Method. Temp project with a 40 KB AGENTS.md (40975 bytes) carrying an early
marker and a tail marker past 39 KB, and `project_doc_max_bytes = 131072` in the
project `.codex/config.toml`. The subcommand has no `-C` flag, so run it from
inside the project. It makes no model call.

```bash
( cd "$PROJ" && CODEX_HOME="$T/home" codex debug prompt-input \
    "What are the codewords in my project instructions?" )
# CLI override alternative:
( cd "$PROJ" && CODEX_HOME="$T/home" codex debug prompt-input -c project_doc_max_bytes=131072 "..." )
```

Result. Output is a JSON array of messages, each
`{type:"message", id, role, content, internal_chat_message_metadata_passthrough}`
with `content` a list of text parts. A one turn setup emitted 5 messages: 3
`developer`, then 2 `user`. The first `user` message holds three parts
(`content_item_kinds` `plugins.recommendations`, `agents_md.instructions`,
`environments.environment_context`): the AGENTS.md is its own
`agents_md.instructions` part sitting alongside a distinct `<environment_context>`
part, not inside it. The second `user` message is the prompt. Shape pinned,
scrubbed, strings over 200 chars truncated, in
`tests/fixtures/codex/prompt-input.json`. The AGENTS.md reached the model in full
only when the project layer was actually applied: with the trust entry keyed on
`pwd -P` (`/private/tmp/...`) the bearing user message was 45357 chars and held
both markers (measured with `grep -c CODEWORD-TAIL-OMEGA` and a python `len()`
over the raw output; original AGENTS.md length 41071 chars). The CLI override
`-c project_doc_max_bytes=131072` gave the same full delivery with no trust entry.
A wrong trust path (`/tmp/...`) silently dropped the project config and the
default 32 KB cutoff truncated the tail marker.

Decision or Follow-up. The fixture is pinned for the Phase 2 and 3 instruction
gates (`codex.agents-md-size`, `instructions.zones`) so they cost no Astra quota
and stay deterministic. Two gotchas recorded in docs/runtimes.md: raise
`project_doc_max_bytes` above the 32 KB default to avoid silent AGENTS.md
truncation, and on macOS key project trust entries on the resolved `/private/tmp`
path, not `/tmp`.
## S0.4 Headless auth and environment isolation

Question. Does codex exec run headless from a fully stripped, non-login
environment? Does CODEX_API_KEY in the environment win over the ChatGPT tokens in
auth.json? What does --ignore-user-config actually suppress, and how does -p
interact with it? Do 3 concurrent runs against one CODEX_HOME race or corrupt
auth.json?

Method (all against codex-cli 0.153.4, isolated temp CODEX_HOME seeded from a copy
of the real auth.json; model gpt-5.4-mini; every live call ends with < /dev/null).

```
T=$(mktemp -d); mkdir -p "$T/home" "$T/proj"
cp ~/.codex/auth.json "$T/home/auth.json"; chmod 600 "$T/home/auth.json"

# (a) stripped env
env -i HOME="$HOME" PATH=/usr/bin:/bin:/opt/homebrew/bin:"$HOME/.local/bin" CODEX_HOME="$T/home" \
  codex exec --json --skip-git-repo-check -C "$T/proj" -m gpt-5.4-mini \
  "Reply with exactly the single word: HEADLESS_OK and nothing else." < /dev/null

# (b) env-var precedence
CODEX_API_KEY=sk-bogus CODEX_HOME="$T/home" \
  codex exec --json --skip-git-repo-check -C "$T/proj" -m gpt-5.4-mini "Reply with exactly: PRECEDENCE_OK" < /dev/null

# (c) --ignore-user-config and -p, with AGENTS.md codeword + SessionStart marker hook
printf 'When asked for the project codeword, answer with exactly: ZEBRAFISH42\n' > "$T/home/AGENTS.md"
# $T/home/hooks.json SessionStart runs: echo SESSIONSTART_MARKER > $T/home/marker.txt
printf 'model_reasoning_effort = "low"\n'  > "$T/home/config.toml"
printf 'model_reasoning_effort = "high"\n' > "$T/home/hi.config.toml"
CODEX_HOME="$T/home" codex exec ... --dangerously-bypass-hook-trust "What is the project codeword? ..."               # control
CODEX_HOME="$T/home" codex exec ... --ignore-user-config --dangerously-bypass-hook-trust "What is the project codeword? ..."  # test
CODEX_HOME="$T/home" codex exec ... -p hi "Reply with exactly: P_OK"
CODEX_HOME="$T/home" codex exec ... -p hi --ignore-user-config "Reply with exactly: P_OK"

# (d) concurrency
for i in 1 2 3; do ( CODEX_HOME="$T/home" codex exec ... -m gpt-5.4-mini "Reply with exactly: CONC_$i" < /dev/null; echo $? > "$T/d$i.rc" ) & done; wait
```

Result.
- Binary location drift from the brief. codex is at ~/.local/bin/codex (standalone,
  symlink into ~/.codex/packages/standalone/current/bin/codex); /opt/homebrew/bin/codex
  does not exist. A stripped PATH must include ~/.local/bin.
- (a) exit 0. Model returned HEADLESS_OK. Headless from env -i works with only HOME,
  PATH (containing the codex binary), and CODEX_HOME. The exec --json stream uses
  item.* events; the older event_msg shape is only in the rollout files.
- (b) exit 1. CODEX_API_KEY=sk-bogus flipped auth to ApiKey mode
  (auth.recovery_reason="not_chatgpt_auth") and the request got 401
  invalid_api_key ("Incorrect API key provided: sk-bogus"). The environment
  variable wins over the ChatGPT tokens in auth.json.
- (c) --ignore-user-config only skips $CODEX_HOME/config.toml. $CODEX_HOME/AGENTS.md
  (codeword ZEBRAFISH42) still reached the model and the SessionStart hook still
  wrote its marker, both with and without the flag. -p hi layered
  hi.config.toml on top of base config (reasoning_effort low -> high), but
  -p hi --ignore-user-config dropped both base and profile, falling back to the
  built-in default medium. So --ignore-user-config suppresses the profile layer too.
- (d) All 3 runs exit 0 with distinct output and 3 rollouts written. auth.json mtime
  and last_refresh are unchanged (token not near expiry, so no refresh fired; a
  refresh was deliberately not forced to avoid rotating the shared live token). A
  benign non-fatal race surfaced: concurrent runs collide installing the shared
  system-skills dir (ERROR codex_skills_extension::host_service: failed to install
  system skills ... remove existing system skills dir), runs still succeed.

Decision. Headless automation must (1) point PATH at ~/.local/bin (not
/opt/homebrew/bin) on standalone installs; (2) never leak CODEX_API_KEY into a
ChatGPT-auth environment, since it silently overrides and 401s; (3) treat
--ignore-user-config as config.toml-plus-profile-only, and still isolate AGENTS.md
and hooks.json by controlling CODEX_HOME; (4) give each concurrent worker its own
CODEX_HOME to avoid the skills-dir race. Follow-up: the concurrent-refresh path is
unproven; retest near token expiry in a throwaway home before relying on it.

## S0.8 Project layer trust gating

Question. Is a project-local .codex/config.toml read by codex exec only when the
project is trusted in the user config, and is it skipped otherwise?

Method. Temp project whose .codex/config.toml sets model = "bogus-model-xyz". Run
once with no [projects] entry in the temp user config and once with a trust entry.
Base user config sets model = "gpt-5.4-mini" as the fallback so the untrusted arm
never calls an unsanctioned model.

```
T=$(mktemp -d); mkdir -p "$T/home" "$T/proj/.codex"
cp ~/.codex/auth.json "$T/home/auth.json"; chmod 600 "$T/home/auth.json"
printf 'model = "bogus-model-xyz"\n' > "$T/proj/.codex/config.toml"
PROJ_REAL=$(cd "$T/proj" && pwd -P)

# untrusted
printf 'model = "gpt-5.4-mini"\n' > "$T/home/config.toml"
CODEX_HOME="$T/home" codex exec --json --skip-git-repo-check -C "$T/proj" "Reply with exactly: E_OK" < /dev/null

# trusted
{ printf 'model = "gpt-5.4-mini"\n\n'; printf '[projects."%s"]\ntrust_level = "trusted"\n' "$PROJ_REAL"; } > "$T/home/config.toml"
CODEX_HOME="$T/home" codex exec --json --skip-git-repo-check -C "$T/proj" "Reply with exactly: E_OK" < /dev/null
```

Result.
- Untrusted: exit 0, resolved model = gpt-5.4-mini. The project config.toml was not
  read.
- Trusted: exit 1, resolved model = bogus-model-xyz, API returned 400
  invalid_request_error "The 'bogus-model-xyz' model is not supported when using
  Codex with a ChatGPT account." The project config.toml was read and layered on top.
- The trust-table key must be the canonicalized path. macOS mktemp -d returns a
  /var/folders (or /tmp) path that is a symlink into /private/...; codex canonicalizes
  cwd before matching, so writing the unresolved path makes the trusted arm behave
  like the untrusted arm. Top-level keys in the user config must precede the first
  [projects."..."] table.

Decision. hex must write [projects."<canonical abs path>"] trust_level = "trusted"
(resolved with pwd -P / realpath) into the user config before it can rely on any
project .codex/config.toml being honored. Untrusted projects are safe by default:
their config layer is silently ignored.

## S0.9 Quota and token accounting

Question. Where do rate limits and token usage live in a headless run, and does a
turn that makes multiple tool calls count as one message for quota?

Method. One codex exec run (gpt-5.4-mini, read-only sandbox) that makes 3 shell tool
calls in a single turn, then extract the token_count records from the temp
CODEX_HOME rollout.

```
T=$(mktemp -d); mkdir -p "$T/home" "$T/proj"
cp ~/.codex/auth.json "$T/home/auth.json"; chmod 600 "$T/home/auth.json"
CODEX_HOME="$T/home" codex exec --json --skip-git-repo-check -C "$T/proj" -m gpt-5.4-mini -s read-only \
  "Run these three shell commands one at a time using your shell tool: first 'pwd', then 'date +%Y', then 'echo TOOLCALL_DONE'. After all three, reply with the single word FINISHED." < /dev/null
rf=$(find "$T/home/sessions" -name '*.jsonl' | head -1)
# count token_count records and read total_token_usage.total_tokens per record
```

Result.
- Rate limits live in the rollout as event_msg records of type token_count, each
  with info (total_token_usage, last_token_usage, model_context_window) and a
  rate_limits object (limit_id, primary.used_percent, primary.window_minutes,
  primary.resets_at, credits, plan_type). A scrubbed copy is committed at
  tests/fixtures/codex/rate-limits.json.
- The 3-tool-call turn produced 4 token_count records: one per model step (initial
  plus one after each tool-call result), each with its own rate_limits snapshot.
- info.total_token_usage.total_tokens accumulates across the invocation
  (11876 -> 23868 -> 35965 -> 48119); info.last_token_usage is the per-step usage.
- primary.used_percent was 0.0 for every record (10080-minute window, plan_type pro).
  Whether a multi-tool-call turn counts as one server-side message is NOT resolvable
  from a used_percent delta at this utilization; 0.0 -> 0.0 is a null, not a finding.

Decision. For quota tracking hex should read the rate_limits snapshot from the last
token_count record of a run (used_percent, window_minutes, resets_at), and treat
token usage as cumulative per invocation via total_token_usage, not per tool call.
Follow-up: re-measure the used_percent delta on an account with meaningful
utilization to settle the per-message-versus-per-turn question.
