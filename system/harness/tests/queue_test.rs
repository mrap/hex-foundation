use hex_agent::queue;
use hex_agent::types::*;
use chrono::{Utc, Duration};

#[test]
fn test_promote_due_scheduled_items() {
    let now = Utc::now();
    let mut state = hex_agent::state::initialize("test", 2.0);
    state.queue.scheduled.push(ScheduledItem {
        id: "s-1".into(),
        summary: "Health check".into(),
        interval_seconds: 1800,
        last_run: Some(now - Duration::seconds(2000)),
        next_due: now - Duration::seconds(200),
    });
    state.queue.scheduled.push(ScheduledItem {
        id: "s-2".into(),
        summary: "Initiative review".into(),
        interval_seconds: 21600,
        last_run: Some(now - Duration::seconds(100)),
        next_due: now + Duration::seconds(21500),
    });
    let promoted = queue::promote_scheduled(&mut state.queue, now);
    assert_eq!(promoted, 1);
    assert_eq!(state.queue.active.len(), 1);
    assert_eq!(state.queue.active[0].summary, "Health check");
}

#[test]
fn test_inbox_creates_active_items() {
    let now = Utc::now();
    let mut state = hex_agent::state::initialize("test", 2.0);
    state.inbox.push(Message {
        id: "msg-1".into(),
        from: "cos".into(),
        to: "test".into(),
        subject: "Check v2-arch".into(),
        body: "Dead for 12 hours".into(),
        initiative_id: None,
        response_requested: false,
        in_reply_to: None,
        sent_at: now,
    });
    let created = queue::inbox_to_active(&mut state);
    assert_eq!(created, 1);
    assert_eq!(state.queue.active.len(), 1);
    assert!(state.queue.active[0].summary.contains("Check v2-arch"));
}
