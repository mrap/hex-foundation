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
