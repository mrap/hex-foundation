# /hex-triage — Triage Pending Input

Route unprocessed captures from `raw/` to the right locations.

## Steps

1. **Scan raw/ directories:** Check `raw/transcripts/`, `raw/handoffs/`, and any other `raw/` subdirectories for unprocessed files.

2. **For each unprocessed item:**
   - Read the content
   - Determine where it belongs:
     - Person info → `people/{name}/profile.md`
     - Project update → `projects/{project}/context.md`
     - Decision → `me/decisions/{topic}-YYYY-MM-DD.md`
     - Action item → `todo.md`
     - Learning → `me/learnings.md`
   - Write the extracted content to the correct location
   - If the item doesn't clearly fit anywhere, ask the user

3. **Rebuild memory index:**
   ```bash
   python3 .hex/skills/memory/scripts/memory_index.py
   ```

4. **Report:** "Triaged N items. M routed automatically, K need your input."
