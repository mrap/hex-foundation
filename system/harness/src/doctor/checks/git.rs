use crate::doctor::check::{Category, CheckResult, Context, DoctorCheck};
use std::process::Command;

/// check_2: HEX_DIR is a git repository.
pub struct GitInitialized;

impl DoctorCheck for GitInitialized {
    fn name(&self) -> &str {
        "git-initialized"
    }
    fn category(&self) -> Category {
        Category::Health
    }
    fn run(&self, ctx: &Context) -> CheckResult {
        let git_dir = ctx.hex_dir.join(".git");
        if git_dir.exists() {
            return CheckResult::pass(".git/ initialized");
        }
        // Fallback: try `git rev-parse`
        let ok = Command::new("git")
            .arg("rev-parse")
            .arg("--git-dir")
            .current_dir(&ctx.hex_dir)
            .output()
            .map(|o| o.status.success())
            .unwrap_or(false);
        if ok {
            CheckResult::pass(".git/ initialized (worktree)")
        } else {
            CheckResult::fail(".git/ missing — run `git init` to fix")
        }
    }
}

/// check: when the repo carries a committed `.githooks/` dir, `core.hooksPath`
/// must point at it — otherwise the committed leak-guard / legacy-rename
/// pre-commit hook is dead code and private paths or build artifacts can slip
/// into a commit. Warns only when `.githooks/` is present and unwired; a repo
/// without `.githooks/` is skipped (nothing to wire).
pub struct HooksPathConfigured;

impl DoctorCheck for HooksPathConfigured {
    fn name(&self) -> &str {
        "git-hookspath"
    }
    fn category(&self) -> Category {
        Category::Health
    }
    fn run(&self, ctx: &Context) -> CheckResult {
        let githooks = ctx.hex_dir.join(".githooks");
        if !githooks.is_dir() {
            return CheckResult::skip("no .githooks/ in repo — nothing to wire");
        }
        let configured = Command::new("git")
            .args(["config", "--get", "core.hooksPath"])
            .current_dir(&ctx.hex_dir)
            .output()
            .ok()
            .filter(|o| o.status.success())
            .map(|o| String::from_utf8_lossy(&o.stdout).trim().to_string())
            .unwrap_or_default();
        if configured.is_empty() {
            return CheckResult::warn(
                "core.hooksPath is unset but .githooks/ is present — run \
                 `git config core.hooksPath .githooks` so the leak-guard pre-commit hook fires",
            );
        }
        let points_at_githooks = configured == ".githooks"
            || std::fs::canonicalize(ctx.hex_dir.join(&configured)).ok()
                == std::fs::canonicalize(&githooks).ok();
        if points_at_githooks {
            CheckResult::pass("core.hooksPath → .githooks")
        } else {
            CheckResult::warn(format!(
                "core.hooksPath is '{configured}', not .githooks — the committed leak-guard \
                 pre-commit hook will not fire"
            ))
        }
    }
}
