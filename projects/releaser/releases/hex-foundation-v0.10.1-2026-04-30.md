# hex-foundation v0.10.1 — releaser auto-unblock fix

**Date:** 2026-04-30
**Tag:** v0.10.1

## Fix
The release pipeline stalled v0.10.0 for ~6h because the releaser agent's
message_reply blocks never auto-cleared after Sentinel's PASS landed in
the inbox. Two compounding harness bugs:

1. queue.rs:check_unblock_condition returned false for any blocked_type
   that wasn't 'telemetry' or 'timer'. message_reply blocks were silently
   permanent.
2. wake.rs accepted hallucinated blocked_since timestamps from agents,
   corrupting SLA math.

Both are fixed; harness now stamps blocked_since with the server clock on
apply and consults the wake-time inbox to clear message_reply blocks.

Diagnosis: projects/releaser/diagnosis-2026-04-30.md
