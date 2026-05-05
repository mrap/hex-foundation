# Hex Versioning

## Source of Truth

`system/harness/Cargo.toml` is the single source of truth for the hex version. Everything derives from this file:

- **Rust binary** — `build.rs` injects git SHA only; `env!("CARGO_PKG_VERSION")` embeds the Cargo.toml version at compile time. The binary prints `hex 0.11.3 (abc1234)`.
- **`hex version`** — reads `env!("CARGO_PKG_VERSION")` + `env!("HEX_GIT_SHA")` at runtime.
- **Foundation releases** — git tags (`v0.11.3`) must match Cargo.toml. `release.sh` enforces this atomically.

## Version Flow

```
system/harness/Cargo.toml (source of truth)
    ├── env!("CARGO_PKG_VERSION") embedded at compile time → binary
    ├── git tag = v$(version in Cargo.toml) — enforced by release.sh
    └── hex VERSIONS file pinned by /hex-upgrade from Cargo.toml at tag
```

## Releasing a New Version

Use `release.sh bump-version <NEW_VERSION>`:

```bash
cd ~/github.com/mrap/hex-foundation
bash system/scripts/release.sh bump-version 0.11.4
```

This script:
1. Validates semver shape
2. Bumps `version = "..."` in `system/harness/Cargo.toml`
3. Runs `cargo build --release` to confirm compilation
4. `git add system/harness/Cargo.toml`
5. `git commit -m "bump: v0.11.4"`
6. `git tag v0.11.4`
7. Prints next steps (Docker E2E, push)

Push is manual — Mike approves before pushing.

## Why Cargo.toml (not version.txt)

- Cargo refuses to compile without a `version =` field, so Cargo.toml will always have the literal. `version.txt` was a parallel sidecar that could drift silently (and did — it reached 0.9.0 while Cargo.toml stayed at 0.8.0).
- `env!("CARGO_PKG_VERSION")` is idiomatic Rust — the documented way to surface a binary's own version, embedded at compile time with no runtime I/O.
- Cargo.toml lives in `system/harness/` alongside `src/main.rs`. It is the package manifest — part of the code, not a sidecar.
