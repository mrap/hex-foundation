# Changelog

All notable changes to hex-foundation will be documented in this file.

## [2026-05-05]

### Added
- `system/scripts/health/check-fleet-pulse.sh`: fleet-pulse watchdog — emits `hex.agent.needs-attention` events for dormant agents; composite liveness score with WARN/ERROR escalation; suppresses when budget-lockout active.
- `system/scripts/health/check-mike-pending.sh`: Mike-pending board monitor — tier:quiet/digest/direct-ping labels, coalesced per-run alerts, DM fallback to channel if Slack user ID not configured.
- `system/harness/src/wake.rs`: backlog auto-promotion with three safety constraints — proactive_initiatives gate (reactive-only agents never self-assign), per-agent daily wake-budget ceiling at 80% of `charter.budget.usd_per_day`, and a per-wake ceiling of 2 backlog items.

## [2026-05-04]

### Changed
- AGENTS.md: Added "Related repos" cross-link section in Quick Start pointing to boi and the local hex workspace, so agents navigating hex-foundation can find the delegation engine and production workspace
- templates/CLAUDE.md: Added Quick Start section with "Related repos" placeholder before the system-managed block
