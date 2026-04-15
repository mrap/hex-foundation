# /hex-upgrade — Upgrade hex

Pull latest system files from the hex repo. Preserves user data and custom rules.

## Usage

- `bash .hex/scripts/upgrade.sh` — upgrade everything
- `bash .hex/scripts/upgrade.sh --dry-run` — show what would change
- `bash .hex/scripts/upgrade.sh --skip-boi --skip-events` — skip companion upgrades

## What it does

1. Fetches latest hex-foundation repo
2. Backs up current .hex/ directory
3. Replaces system files (.hex/)
4. Merges CLAUDE.md: system zone updated, your custom rules preserved
5. Runs hex doctor to verify

## What it preserves

- Everything in `me/`, `projects/`, `people/`, `evolution/`, `landings/`, `raw/`
- Your custom rules between `hex:user-start` and `hex:user-end` in CLAUDE.md
- Your `todo.md`
- Your `.hex/memory.db`
