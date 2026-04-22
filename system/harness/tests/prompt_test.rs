use hex_agent::prompt;

#[test]
fn test_prompt_contains_charter_and_state() {
    let charter_text = "id: test\nrole: Test Agent\nobjective: Be useful";
    let state = hex_agent::state::initialize("test", 2.0);
    let p = prompt::build(charter_text, &state, "timer.tick.30m", "{}");
    assert!(p.contains("Test Agent"), "prompt should contain charter text");
    assert!(p.contains("timer.tick.30m"), "prompt should contain trigger");
    assert!(p.contains("active"), "prompt should reference active queue");
    assert!(p.contains("AgentResponse"), "prompt should describe response format");
}

#[test]
fn test_prompt_includes_response_schema() {
    let state = hex_agent::state::initialize("test", 2.0);
    let p = prompt::build("id: test", &state, "manual", "{}");
    assert!(p.contains("trail"), "must describe trail");
    assert!(p.contains("queue_updates"), "must describe queue_updates");
    assert!(p.contains("active_drained"), "must describe active_drained");
    assert!(p.contains("observe"), "must list action types");
    assert!(p.contains("find"), "must list action types");
    assert!(p.contains("decide"), "must list action types");
}
