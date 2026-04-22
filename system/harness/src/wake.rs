use crate::{audit, charter, claude, cost, gate, message, prompt, queue, state};
use chrono::Utc;
use std::path::{Path, PathBuf};

pub struct WakeConfig {
    pub hex_dir: PathBuf,
    pub agent_id: String,
    pub trigger: String,
    pub payload: String,
}

pub fn run(config: WakeConfig) -> Result<i32, Box<dyn std::error::Error>> {
    let hex_dir = &config.hex_dir;
    let audit_dir = hex_dir.join(".hex/audit");
    let cost_dir = hex_dir.join(".hex/cost");
    let msg_dir = hex_dir.join(".hex/messages");

    // 1. Load charter — one canonical path, no fallbacks
    let charter_path = hex_dir.join(format!("projects/{}/charter.yaml", config.agent_id));
    if !charter_path.exists() {
        return Err(format!(
            "no charter at {} — agent '{}' is not registered (charter.yaml IS registration)",
            charter_path.display(), config.agent_id
        ).into());
    }
    let charter_data = charter::load(&charter_path)?;
    if charter_data.id != config.agent_id {
        return Err(format!(
            "charter id mismatch: CLI arg is '{}' but charter.id is '{}' in {} — these must match exactly",
            config.agent_id, charter_data.id, charter_path.display()
        ).into());
    }
    let charter_text = std::fs::read_to_string(&charter_path)?;

    // 2. HALT check
    let kill_switch = shellexpand::tilde(&charter_data.kill_switch).to_string();
    if Path::new(&kill_switch).exists() {
        audit::append(&audit_dir, &config.agent_id, "halted", &serde_json::json!({"reason": "kill_switch"}));
        eprintln!("[{}] HALTED: kill switch at {}", config.agent_id, kill_switch);
        return Ok(0);
    }

    // 3. Load or initialize state — same directory as charter, no fallbacks
    let state_dir = hex_dir.join(format!("projects/{}", config.agent_id));
    std::fs::create_dir_all(&state_dir)?;
    let state_path = state_dir.join("state.json");
    let mut agent_state = if state_path.exists() {
        state::load(&state_path)?
    } else {
        state::initialize(&config.agent_id, charter_data.budget.usd_per_day)
    };

    // 4. Reset per-shift cost, increment wake
    agent_state.cost.last_wake_usd = 0.0;
    agent_state.wake_count += 1;
    agent_state.last_wake = Some(Utc::now());

    // 5. Populate inbox
    let inbox_messages = message::receive(&msg_dir, &config.agent_id);
    agent_state.inbox = inbox_messages;
    message::clear_inbox(&msg_dir, &config.agent_id);

    // 6. Queue promotions
    let now = Utc::now();
    let sched_promoted = queue::promote_scheduled(&mut agent_state.queue, now);
    let unblocked = queue::promote_unblocked(&mut agent_state.queue);
    let inbox_items = queue::inbox_to_active(&mut agent_state);

    audit::append(&audit_dir, &config.agent_id, "wake-start", &serde_json::json!({
        "trigger": config.trigger,
        "wake_count": agent_state.wake_count,
        "scheduled_promoted": sched_promoted,
        "unblocked": unblocked,
        "inbox_items": inbox_items,
        "active_count": agent_state.queue.active.len(),
    }));

    // 7. Nothing actionable?
    if agent_state.queue.active.is_empty() {
        audit::append(&audit_dir, &config.agent_id, "wake-skip", &serde_json::json!({"reason": "nothing actionable"}));
        state::save(&agent_state, &state_path)?;
        return Ok(0);
    }

    // 8. Shift loop
    let shift_budget = charter_data.budget.usd_per_shift;
    let allowed_tools = ["Bash", "Read", "Write", "Edit", "Grep", "Glob"];
    let mut invocation = 0;

    loop {
        invocation += 1;

        let remaining = cost::shift_budget_remaining(&agent_state.cost, shift_budget);
        if remaining <= 0.0 {
            audit::append(&audit_dir, &config.agent_id, "shift-budget-hit", &serde_json::json!({
                "spent": agent_state.cost.last_wake_usd,
                "budget": shift_budget,
                "active_remaining": agent_state.queue.active.len(),
            }));
            break;
        }

        let prompt_text = prompt::build(&charter_text, &agent_state, &config.trigger, &config.payload);

        let claude_output = match claude::invoke(&prompt_text, "sonnet", &allowed_tools) {
            Ok(out) => out,
            Err(e) => {
                audit::append(&audit_dir, &config.agent_id, "claude-error", &serde_json::json!({
                    "error": e.to_string(),
                    "invocation": invocation,
                }));
                break;
            }
        };

        cost::record_invocation(&mut agent_state.cost, &claude_output);
        cost::append_ledger(&cost_dir, &config.agent_id, &claude_output);

        let response = match claude::parse_agent_response(&claude_output.result) {
            Ok(r) => r,
            Err(e) => {
                audit::append(&audit_dir, &config.agent_id, "response-parse-error", &serde_json::json!({
                    "error": e.to_string(),
                    "invocation": invocation,
                    "raw_length": claude_output.result.len(),
                }));
                break;
            }
        };

        // Validate and append trail entries
        for entry in &response.trail {
            match gate::validate(entry) {
                Ok(()) => {
                    agent_state.trail.push(entry.clone());
                    audit::append(&audit_dir, &config.agent_id, &format!("gate:{}", entry.entry_type), &entry.detail);
                }
                Err(violation) => {
                    audit::append(&audit_dir, &config.agent_id, "gate-violation", &serde_json::json!({
                        "type": entry.entry_type,
                        "violation": violation,
                    }));
                }
            }
        }

        // Apply queue updates
        agent_state.queue.active.retain(|item| !response.queue_updates.completed.contains(&item.id));
        for item in response.queue_updates.added_active {
            agent_state.queue.active.push(item);
        }
        for item in response.queue_updates.moved_to_blocked {
            agent_state.queue.blocked.push(item);
        }

        // Apply memory updates
        if let Some(updates) = response.memory_updates {
            if let (Some(mem), Some(upd)) = (agent_state.memory.as_object_mut(), updates.as_object()) {
                for (k, v) in upd {
                    mem.insert(k.clone(), v.clone());
                }
            }
        }

        // Deliver outbound messages
        for msg in &response.outbound_messages {
            match message::send(&msg_dir, msg) {
                Ok(()) => {
                    audit::append(&audit_dir, &config.agent_id, "message-sent", &serde_json::json!({
                        "to": msg.to,
                        "subject": msg.subject,
                    }));
                }
                Err(e) => {
                    eprintln!("[{}] MESSAGE SEND FAILED to {}: {e}", config.agent_id, msg.to);
                    audit::append(&audit_dir, &config.agent_id, "message-send-failed", &serde_json::json!({
                        "to": msg.to,
                        "subject": msg.subject,
                        "error": e.to_string(),
                    }));
                }
            }
        }

        if response.active_drained || agent_state.queue.active.is_empty() {
            break;
        }
    }

    // 9. Save state
    state::save(&agent_state, &state_path)?;

    audit::append(&audit_dir, &config.agent_id, "wake-complete", &serde_json::json!({
        "invocations": invocation,
        "shift_cost_usd": agent_state.cost.last_wake_usd,
        "trail_entries": agent_state.trail.len(),
        "active_remaining": agent_state.queue.active.len(),
    }));

    Ok(0)
}

