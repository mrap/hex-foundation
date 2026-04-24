use crate::types::{AgentResponse, AssessmentResponse, ClaudeOutput, Message, QueueUpdates, TrailEntry};
use std::io::Write;
use std::process::{Command, Stdio};

pub fn parse_output(raw: &str) -> Result<ClaudeOutput, Box<dyn std::error::Error>> {
    let output: ClaudeOutput = serde_json::from_str(raw)?;
    Ok(output)
}

pub fn parse_agent_response(
    result_text: &str,
) -> Result<AgentResponse, Box<dyn std::error::Error>> {
    let cleaned = extract_json(result_text);
    match serde_json::from_str::<AgentResponse>(&cleaned) {
        Ok(response) => Ok(response),
        Err(strict_err) => {
            eprintln!("[harness] parse_agent_response: strict parse failed ({strict_err}), attempting partial recovery");
            // Try to parse as raw JSON value and reconstruct field-by-field
            let val: serde_json::Value = serde_json::from_str(&cleaned).map_err(|json_err| {
                eprintln!("[harness] parse_agent_response: not valid JSON ({json_err}), discarding response");
                json_err
            })?;
            let trail = val
                .get("trail")
                .and_then(|t| serde_json::from_value::<Vec<TrailEntry>>(t.clone()).ok())
                .unwrap_or_else(|| {
                    eprintln!("[harness] parse_agent_response: trail field unrecoverable");
                    vec![]
                });
            let queue_updates = val
                .get("queue_updates")
                .and_then(|q| serde_json::from_value::<QueueUpdates>(q.clone()).ok())
                .unwrap_or_default();
            let memory_updates = val.get("memory_updates").cloned();
            let outbound_messages = val
                .get("outbound_messages")
                .and_then(|m| serde_json::from_value::<Vec<Message>>(m.clone()).ok())
                .unwrap_or_default();
            let active_drained = val
                .get("active_drained")
                .and_then(|v| v.as_bool())
                .unwrap_or(false);
            eprintln!(
                "[harness] parse_agent_response: partial recovery ({} trail entries, {} messages)",
                trail.len(),
                outbound_messages.len()
            );
            Ok(AgentResponse {
                trail,
                queue_updates,
                memory_updates,
                outbound_messages,
                active_drained,
            })
        }
    }
}

pub fn parse_assessment_response(
    result_text: &str,
) -> Result<AssessmentResponse, Box<dyn std::error::Error>> {
    let cleaned = extract_json(result_text);
    let response: AssessmentResponse = serde_json::from_str(&cleaned)?;
    Ok(response)
}

fn extract_json(text: &str) -> String {
    let trimmed = text.trim();

    // Try direct parse first
    if trimmed.starts_with('{') {
        return trimmed.to_string();
    }

    // Strip markdown code fences: ```json ... ``` or ``` ... ```
    if let Some(start) = trimmed.find("```") {
        let after_fence = &trimmed[start + 3..];
        // Skip optional language tag (e.g., "json")
        let content_start = after_fence.find('\n').map(|i| i + 1).unwrap_or(0);
        let content = &after_fence[content_start..];
        if let Some(end) = content.find("```") {
            let inner = content[..end].trim();
            if inner.starts_with('{') {
                return inner.to_string();
            }
        }
    }

    // Find first { and last } as fallback
    if let (Some(start), Some(end)) = (trimmed.find('{'), trimmed.rfind('}')) {
        if start < end {
            return trimmed[start..=end].to_string();
        }
    }

    trimmed.to_string()
}

pub fn build_args(model: &str, allowed_tools: &[&str]) -> Vec<String> {
    let tools_str = allowed_tools.join(",");
    vec![
        "-p".to_string(),
        "--output-format".to_string(),
        "json".to_string(),
        "--model".to_string(),
        model.to_string(),
        "--allowedTools".to_string(),
        tools_str,
        "--dangerously-skip-permissions".to_string(),
    ]
}

pub fn invoke(
    prompt: &str,
    model: &str,
    allowed_tools: &[&str],
) -> Result<ClaudeOutput, Box<dyn std::error::Error>> {
    let args = build_args(model, allowed_tools);
    let mut child = Command::new("claude")
        .args(&args)
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .map_err(|e| format!("failed to spawn claude: {e}"))?;

    if let Some(mut stdin) = child.stdin.take() {
        stdin.write_all(prompt.as_bytes())?;
    }

    let output = child.wait_with_output()?;
    if !output.status.success() {
        let stderr = String::from_utf8_lossy(&output.stderr);
        return Err(format!("claude exited with {}: {stderr}", output.status).into());
    }

    let stdout = String::from_utf8(output.stdout)?;
    parse_output(&stdout)
}
