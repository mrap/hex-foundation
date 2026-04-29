# Hex File System Zones

Two zones govern what `hex upgrade` touches. Never violate this boundary.

## CORE — overwritten by `hex upgrade`

```
system/                    ← scripts, skills, commands, hooks, harness
```

When you run `hex upgrade`, only the core zone is updated. Core files are
managed by the hex team and will be overwritten without warning.

## USER SPACE — never touched by `hex upgrade`

```
.hex/                      ← per-project hex configuration
.hex/extensions/           ← user-installed extensions
projects/                  ← your hex projects
templates/                 ← your custom templates
integrations/              ← third-party integration configs
extensions/                ← extensions bundled with this workspace
```

User space is yours. Hex upgrade will never write to, delete from, or modify
anything in these directories.

## Upgrade behavior

- `hex upgrade` creates a `system.bak-YYYYMMDD/` snapshot of the current core
  before applying changes, so you can diff or roll back.
- Your `.hex/extensions/` directory is created on first install and preserved
  across all upgrades.

## Extension install location

Extensions installed via `hex extension enable` land in `.hex/extensions/<name>/`.
Extensions bundled with this workspace live in `extensions/<name>/`.
Both are user-space: they survive upgrades untouched.
