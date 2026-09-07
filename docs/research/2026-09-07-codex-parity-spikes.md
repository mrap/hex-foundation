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
