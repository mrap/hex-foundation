# /hex-consolidate — System Hygiene

Audit the operating model for contradictions, staleness, and orphaned references.

## Steps

1. **Scan for contradictions:** Read CLAUDE.md standing orders. Check if any rules contradict each other. Flag conflicts.

2. **Check for stale references:** Grep CLAUDE.md, todo.md, and evolution/ for file paths. Verify referenced files exist. Flag orphaned references.

3. **Audit standing orders:** Are any standing orders redundant? Could any be consolidated? Propose consolidation if count exceeds 25.

4. **Check evolution pipeline:**
   - Read `evolution/observations.md` — any patterns with frequency >= 3 that don't have a suggestion yet?
   - Read `evolution/suggestions.md` — any approved suggestions not yet implemented?
   - Read `evolution/changelog.md` — any recent changes that should be verified for effectiveness?

5. **Report findings:** List issues found, proposed fixes, and items needing user input. Write findings to `evolution/observations.md` if new patterns detected.
