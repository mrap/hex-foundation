// Red tests for GROUP 2 repo-leak guards (spec Sfgm6qvqh, task Tvwq2efe7).
//
// Two leak classes must be caught before a private path or a build artifact
// can reach a public branch:
//
//   1. The committed `.githooks/pre-commit` hook must REJECT a staged blob
//      that carries an absolute private `/Users/<letter>` path (excluding the
//      `/Users/test` fixture allowance) AND must REJECT a staged path in the
//      artifact deny set (any `target*/` dir, `node_modules/`, `*.rlib`,
//      `*.rmeta`, `*.o`, `.DS_Store`). Today the file at that path is a
//      *different* hook (the legacy-rename guard) that accepts both — so the
//      bare `test -x .githooks/pre-commit` verification is already green and
//      cannot tell "leak-guard hook landed" from "nothing happened". These
//      tests close that hole by driving the hook against a bad staged tree.
//
//   2. `hex::sanitize::scan` must surface an *artifact-detection* category
//      (deny-set paths present in the tree/diff) alongside the existing
//      `/Users/` category, so the release gate remains the last line for both
//      leak classes.
//
// These tests are EXPECTED TO FAIL until `.githooks/pre-commit` is replaced
// with the leak-guard hook and `src/sanitize.rs` gains the artifact category.
// Mirrors the red-test convention already used by `lint_gates_test.rs`.
//
// Handoff notes for the implementation phase:
//   - The `hook-exists` verification (`test -x .githooks/pre-commit`) exits 0
//     today because a file exists there, but it is the legacy-rename guard,
//     not the leak guard; the discriminating signal is these `sanitize` tests.
//   - The clean-tree-pass test named in the behavior is not written here
//     because it cannot fail against the current code; add it green during
//     implementation.
//   - These red tests stage the artifact as git-tracked (`git add -f`), so key
//     the artifact category on tracked or staged paths, not a filesystem walk.
//     The repo gitignores `target/` and uses an out-of-repo `CARGO_TARGET_DIR`
//     plus per-worktree `target-cq` dirs, so a tree walk could flag gitignored
//     build dirs and leave the clean-tree test unsatisfiable.
//   - Existing sanitize unit tests check the violation count and the first
//     category for single-violation fixtures (see `sanitize.rs` near line 760
//     and near line 813). The new artifact category must not fire on those
//     fixtures and must not register before the `/Users/` category, or the
//     existing suite gains failures.

use std::fs;
use std::path::{Path, PathBuf};
use std::process::Command;

use hex::sanitize::scan;

/// Repo root — `CARGO_MANIFEST_DIR` is `system/harness`, so `../..` is the
/// checkout root. Same idiom the sanitize parity test uses.
fn repo_root() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR")).join("../..")
}

/// Run a git subcommand in `dir`; panic loudly if it fails (S6: no quiet
/// failures — a broken fixture must crash, never silently skip).
fn git(dir: &Path, args: &[&str]) {
    let out = Command::new("git")
        .args(args)
        .current_dir(dir)
        .output()
        .unwrap_or_else(|e| panic!("git {args:?} failed to spawn: {e}"));
    assert!(
        out.status.success(),
        "git {args:?} failed: {}",
        String::from_utf8_lossy(&out.stderr)
    );
}

/// Initialise a throwaway git repo with no global-config bleed-through.
fn init_repo(dir: &Path) {
    git(dir, &["init", "-q"]);
    git(dir, &["config", "user.email", "fixture@example.invalid"]);
    git(dir, &["config", "user.name", "fixture"]);
}

/// Run the committed pre-commit hook with CWD inside `repo`; return true when
/// the hook REJECTED the staged tree (non-zero exit).
fn hook_rejects(repo: &Path) -> bool {
    let hook = repo_root().join(".githooks/pre-commit");
    assert!(
        hook.exists(),
        "expected committed hook at {}",
        hook.display()
    );
    let out = Command::new(&hook)
        .current_dir(repo)
        .output()
        .unwrap_or_else(|e| panic!("pre-commit hook failed to spawn: {e}"));
    !out.status.success()
}

// ---------------------------------------------------------------------------
// 1. Hook rejects a staged private path string and a staged artifact path.
// ---------------------------------------------------------------------------

#[test]
fn sanitize_pre_commit_hook_rejects_private_path_and_artifact() {
    // -- Case A: a staged blob carrying an absolute private /Users/ path. --
    let dir_a = tempfile::tempdir().unwrap();
    let a = dir_a.path();
    init_repo(a);
    // personalization-audit: test fixture — built at runtime so no raw
    // `/Users/<letter>` literal is committed in this source file (the source
    // token is `/Users/{}` which the sanitize `/Users/` pattern never matches).
    let private_line = format!("DATA_DIR=/Users/{}/hex-data/input.csv\n", "sampleuser");
    fs::write(a.join("config.sh"), private_line).unwrap();
    git(a, &["add", "config.sh"]);
    assert!(
        hook_rejects(a),
        "pre-commit hook must REJECT a staged absolute private /Users/ path"
    );

    // -- Case B: a staged path in the artifact deny set (target-iso/*.rlib). --
    let dir_b = tempfile::tempdir().unwrap();
    let b = dir_b.path();
    init_repo(b);
    fs::create_dir_all(b.join("target-iso")).unwrap();
    fs::write(b.join("target-iso").join("app.rlib"), [0u8, 1, 2, 3]).unwrap();
    git(b, &["add", "-f", "target-iso/app.rlib"]);
    assert!(
        hook_rejects(b),
        "pre-commit hook must REJECT a staged build-artifact path (target-iso/app.rlib)"
    );
}

// ---------------------------------------------------------------------------
// 1b. Hook ACCEPTS a clean staged tree (no private path, no artifact).
// ---------------------------------------------------------------------------

#[test]
fn sanitize_pre_commit_hook_passes_clean_tree() {
    let dir = tempfile::tempdir().unwrap();
    let repo = dir.path();
    init_repo(repo);
    // A benign real file staged — an empty index would be a weaker check that
    // a broken (always-reject) hook could still pass.
    fs::write(repo.join("README.md"), "# clean project\n\nJust prose.\n").unwrap();
    fs::write(repo.join("main.rs"), "fn main() { println!(\"ok\"); }\n").unwrap();
    // The /Users/test fixture allowance must not trip the private-path guard.
    fs::write(
        repo.join("fixture.toml"),
        "path = \"/Users/test/workspace/repo\"\n",
    )
    .unwrap();
    git(repo, &["add", "README.md", "main.rs", "fixture.toml"]);
    assert!(
        !hook_rejects(repo),
        "pre-commit hook must ACCEPT a clean staged tree (no private paths, no artifacts)"
    );
}

// ---------------------------------------------------------------------------
// 2. sanitize::scan surfaces the artifact-detection category.
// ---------------------------------------------------------------------------

#[test]
fn sanitize_scan_flags_tracked_build_artifact() {
    // A git repo where the artifact file is BOTH on disk AND git-tracked, so
    // the assertion holds whether the implementation walks the filesystem or
    // inspects the git index/diff — neither reading is forced.
    let dir = tempfile::tempdir().unwrap();
    let root = dir.path();
    init_repo(root);
    fs::create_dir_all(root.join("target-iso")).unwrap();
    fs::write(root.join("target-iso").join("app.rlib"), [0u8, 1, 2, 3]).unwrap();
    git(root, &["add", "-f", "target-iso/app.rlib"]);

    let found = scan(root, false).unwrap();
    assert!(
        found
            .iter()
            .any(|v| v.category.to_lowercase().contains("artifact")),
        "sanitize::scan must flag a build-artifact path with an artifact category; got {found:?}"
    );
}

// ---------------------------------------------------------------------------
// 3. sanitize::scan leaves a clean tracked tree untouched (no artifact category).
// ---------------------------------------------------------------------------

#[test]
fn sanitize_scan_clean_tracked_tree_has_no_artifact_category() {
    let dir = tempfile::tempdir().unwrap();
    let root = dir.path();
    init_repo(root);
    fs::write(root.join("README.md"), "# clean\n").unwrap();
    fs::create_dir_all(root.join("src")).unwrap();
    fs::write(root.join("src").join("main.rs"), "fn main() {}\n").unwrap();
    git(root, &["add", "README.md", "src/main.rs"]);

    let found = scan(root, false).unwrap();
    assert!(
        found
            .iter()
            .all(|v| !v.category.to_lowercase().contains("artifact")),
        "clean tracked tree must produce no artifact violations; got {found:?}"
    );
    assert!(
        found.is_empty(),
        "clean tracked tree must be fully clean; got {found:?}"
    );
}
