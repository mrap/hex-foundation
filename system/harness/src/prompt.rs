use crate::types::AgentState;

pub fn build(charter_text: &str, state: &AgentState, trigger: &str, payload: &str) -> String {
    let trail_recent: Vec<_> = state.trail.iter().rev().take(20).collect();
    let trail_json = serde_json::to_string_pretty(&trail_recent).unwrap_or_default();
    let queue_json = serde_json::to_string_pretty(&state.queue).unwrap_or_default();
    let memory_json = serde_json::to_string_pretty(&state.memory).unwrap_or_default();
    let inbox_json = serde_json::to_string_pretty(&state.inbox).unwrap_or_default();
    let cost_json = serde_json::to_string_pretty(&state.cost).unwrap_or_default();
    let initiatives_json = serde_json::to_string_pretty(&state.initiatives).unwrap_or_default();

    format!(r#"# Charter

{charter_text}

---

# Wake Context

- Trigger: {trigger}
- Payload: {payload}
- Wake count: {wake_count}
- Last wake: {last_wake}

## Queue

```json
{queue_json}
```

## Inbox (unread messages)

```json
{inbox_json}
```

## Recent trail (last 20 entries)

```json
{trail_json}
```

## Working memory

```json
{memory_json}
```

## Initiatives

```json
{initiatives_json}
```

## Cost

```json
{cost_json}
```

---

# Instructions

Work your active queue. For each item, use the action types below to observe, analyze, decide, and act. You choose the workflow -- the harness logs everything.

When you're done with all active items (or have moved them to blocked/parked), set `active_drained: true`.

## Action types

Each trail entry must have `type` and `detail` fields. Required detail fields per type:

| Type | Required fields |
|------|----------------|
| observe | what, noted |
| find | finding, evidence |
| decide | decision, alternatives, reasoning |
| act | action, result |
| verify | check, evidence, status |
| delegate | initiative_id, to, context |
| park | item_id, reason, resume_condition |
| reframe | abandoned, reason, new_framing |
| message_sent | to, subject, body |

## Response format

Return a single JSON object (AgentResponse) with exactly these fields:

```json
{{
  "trail": [
    {{"ts": "ISO-8601", "type": "observe", "detail": {{"what": "...", "noted": "..."}}, "queue_item": "t-1"}}
  ],
  "queue_updates": {{
    "completed": ["t-1"],
    "added_active": [],
    "moved_to_blocked": [],
    "parked": []
  }},
  "memory_updates": {{"key": "value"}},
  "outbound_messages": [],
  "active_drained": true
}}
```

Respond ONLY with the JSON object. No prose before or after.
"#,
        wake_count = state.wake_count,
        last_wake = state.last_wake.map(|t| t.to_rfc3339()).unwrap_or_else(|| "never".into()),
    )
}
