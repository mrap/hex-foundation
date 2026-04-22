use hex_agent::claude;

#[test]
fn test_parse_claude_json_output() {
    let raw = r#"{
        "type": "result",
        "subtype": "success",
        "is_error": false,
        "duration_ms": 37360,
        "duration_api_ms": 2358,
        "num_turns": 1,
        "result": "{\"trail\":[],\"queue_updates\":{\"completed\":[],\"added_active\":[],\"moved_to_blocked\":[],\"parked\":[]},\"memory_updates\":null,\"outbound_messages\":[],\"active_drained\":true}",
        "stop_reason": "end_turn",
        "total_cost_usd": 0.036,
        "usage": {
            "input_tokens": 5000,
            "output_tokens": 200,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 4500
        },
        "session_id": "test-session"
    }"#;

    let output = claude::parse_output(raw).unwrap();
    assert_eq!(output.total_cost_usd, 0.036);
    assert_eq!(output.usage.input_tokens, 5000);
    assert_eq!(output.usage.output_tokens, 200);
    assert_eq!(output.duration_ms, 37360);
}

#[test]
fn test_parse_agent_response_from_result() {
    let result_json = r#"{"trail":[{"ts":"2026-04-22T12:00:00Z","type":"observe","detail":{"what":"log.jsonl","noted":"healthy"},"queue_item":"t-1"}],"queue_updates":{"completed":["t-1"],"added_active":[],"moved_to_blocked":[],"parked":[]},"memory_updates":{"last_pattern":"all healthy"},"outbound_messages":[],"active_drained":true}"#;
    let response = claude::parse_agent_response(result_json).unwrap();
    assert_eq!(response.trail.len(), 1);
    assert_eq!(response.trail[0].entry_type, "observe");
    assert_eq!(response.queue_updates.completed, vec!["t-1"]);
    assert!(response.active_drained);
}

#[test]
fn test_parse_malformed_output() {
    let result = claude::parse_output("not json at all");
    assert!(result.is_err());
}

#[test]
fn test_build_invocation_args() {
    let args = claude::build_args("sonnet", &["Bash", "Read", "Write"]);
    assert!(args.contains(&"--output-format".to_string()));
    assert!(args.contains(&"json".to_string()));
    assert!(args.contains(&"--model".to_string()));
    assert!(args.contains(&"sonnet".to_string()));
}
