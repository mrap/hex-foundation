# GROUP 2 — Repo leak guards (private paths + build artifacts)

**Spec** `Sfgm6qvqh` · **Task** `Tvwq2efe7` · **Date** 2026-09-05

## Problem

Operator decision 2026-09-04. Evidence: a worker tree-preservation commit swept
~1230 cargo artifact files carrying ~6000 private path strings toward a public
branch; caught only by review pre-push. Two leak classes were mechanically
uncommittable and had no committed guard:

1. Absolute **private home paths** — `/Users/<letter>...` (the `/Users/test`
   fixture is a deliberate allowance).
2. **Build artifacts** — any `target*/` directory, `node_modules/`, `*.rlib`,
   `*.rmeta`, `*.o`, `.DS_Store`.

## What shipped

### 1. Committed pre-commit hook — `.githooks/pre-commit`

Replaced the single legacy-rename guard with a three-guard hook that runs all
guards and reports every failure, ending in an explicit `exit 0` so a no-match
`grep` can never reject a clean commit under `pipefail`:

- **Guard 1** — the pre-existing legacy-rename guard (unchanged): blocks
  renaming a script to `.legacy.{sh,py}` while Rust callers under
  `system/harness/src/` still reference it.
- **Guard 2** — rejects staged **added** lines containing an absolute
  `/Users/<letter>` path; the single allowance is `/Users/test` (followed by
  `/` or end-of-token), mirroring the sanitize gate's boundary.
- **Guard 3** — rejects any staged path in the artifact deny set.

Design note (deliberate, spec-faithful): the sole private-path allowance is
`/Users/test`. This repo's own fixtures use other fake names (e.g. the `alice`
fixture) tagged `personalization-audit`; re-staging those exact lines would trip
Guard 2. A second, unspecified allowance would be a silent-skip channel against
HARD CONSTRAINT "all errors loud (S6)", so it was left out; the sanitize gate
already tolerates those fixtures at release time.

`.githooks/pre-commit-ci.sh` was left unchanged on purpose — nothing in the task
asks for it, and mirroring the leak guard into CI risks new red.

### 2. Harness wiring — `core.hooksPath`

- **Upgrade flow** — `configure_hooks_path()` in
  `system/harness/src/upgrade.rs` sets `git config core.hooksPath .githooks` in
  any workspace that is its own git top-level and carries `.githooks/`. Called
  from `run()` inside the `is_own_git_toplevel(&hex_dir)` block. Idempotent
  (no-op when already `.githooks`) and loud-but-non-fatal on failure so a
  missing hooks wiring never blocks a version sync.
- **Doctor warning** — `HooksPathConfigured` (`name = "git-hookspath"`) in
  `system/harness/src/doctor/checks/git.rs`, registered in
  `system/harness/src/doctor/runner.rs` right after `GitInitialized`. Three-way:
  **skip** when no `.githooks/`, **pass** when `core.hooksPath` → `.githooks`,
  **warn** when `.githooks/` is present but `core.hooksPath` is unset or points
  elsewhere.
- **Repo setup scope note** — the only initial-install entry point is
  `install.sh` at the repo root, which is **outside** this change's allowed set
  (`system/harness/`, `.githooks/`, `docs/`, the CLAUDE.md template). There is
  no `hex new`/`hex init`/`hex setup` subcommand inside `system/harness/`. So
  clone-time wiring is covered by the `hex doctor` warning plus a one-time
  `git config core.hooksPath .githooks`, documented in `docs/hex-ops.md`,
  rather than by editing the out-of-scope installer.

### 3. Sanitize artifact-detection category — `system/harness/src/sanitize.rs`

Added alongside the existing `/Users/` category so release-time stays the last
line for both leak classes:

- `ARTIFACT_LABEL` const (`&'static str`, so it fits `hit_labels`).
- `is_artifact_path(rel)` — the deny-set matcher (any `target*/` dir,
  `node_modules/`, `*.rlib`, `*.rmeta`, `*.o`, `.DS_Store`); `.o` etc. are
  suffix-anchored so `notes.org` / `foo.obj` never match.
- `tracked_artifacts(root)` — keys on the **git index** (`git ls-files -z`),
  guarded by a `.git` existence probe on the canonicalized root. A non-git tree
  has no tracked paths (zero artifacts, correct — not a swallowed error); a
  genuine `git ls-files` failure is surfaced loudly via the returned error.
  Chosen over a filesystem walk because the tree gitignores `target/` and uses
  out-of-repo `CARGO_TARGET_DIR` plus per-worktree `target-cq` dirs — a walk
  would false-positive on gitignored build dirs and make the clean-tree case
  unsatisfiable.
- Integrated in `scan()` **after** the content categories and the `.claude/`
  file-level check, so the `/Users/` category always registers first
  (`found[0]` invariant the existing suite asserts).

### 4. Docs

- `templates/AGENTS.md` (the CLAUDE.md template) — a new **Verify-gate
  footguns** table in the BOI section with the two required rows:
  `one-emit-file-per-task` and `dev-profile-test-gates`. This is where the spec
  points ("footgun table upstream (CLAUDE.md template)") and the only in-scope
  home for it — the live footgun table in
  `system/skills/boi-delegation/SKILL.md` is outside the allowed set, so it was
  not touched.
- `docs/hex-ops.md` — a **Repo leak guards** section documenting the hook, the
  `core.hooksPath` wiring, the doctor check, and the sanitize backstop.

## Tests (pin the behavior)

Integration file `system/harness/tests/sanitize_leak_guards_test.rs`:

- `sanitize_pre_commit_hook_rejects_private_path_and_artifact` — hook rejects a
  staged `/Users/<letter>` string and a staged `target-iso/app.rlib` artifact.
- `sanitize_pre_commit_hook_passes_clean_tree` — hook accepts a clean staged
  tree (including the `/Users/test` allowance).
- `sanitize_scan_flags_tracked_build_artifact` — `scan()` surfaces an artifact
  category for a git-tracked `*.rlib`.
- `sanitize_scan_clean_tracked_tree_has_no_artifact_category` — a clean tracked
  tree produces no artifact violations and is fully clean.

Unit test in `system/harness/src/sanitize.rs`:

- `artifact_matcher_denies_deny_set_and_allows_source` — pins the deny-set
  boundary (source/config/docs are not artifacts; `target*/` is intentionally
  broad).

## Verification

- `hook-exists`: `test -x .githooks/pre-commit` → 0.
- `guard-tests`: `export PATH="/opt/homebrew/bin:$PATH" && cd system/harness &&
  cargo test sanitize` → green.
- Full `cargo build --release` and `cargo test` — no NEW failures.

## Scope / constraints honored

- Files touched only under `.githooks/`, `system/harness/`, `docs/`, and the
  CLAUDE.md template (`templates/AGENTS.md`).
- No personal names, absolute `/Users` paths (outside `/Users/test` fixtures),
  hostnames, or email addresses in any committed file. No double-curly-brace
  template syntax.
- Deny set verified safe against the live tree: `git ls-files` matches no
  tracked deny-set path, so the new category does not fire on the real repo or
  the release gate.
