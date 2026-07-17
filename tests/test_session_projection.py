from __future__ import annotations

from openclaw_adapter.session_events import SessionRunEvent
from openclaw_adapter.session_projection import migrate_legacy_snapshot, project_session


def _event(seq, event_type, payload=None, run_id="run-1", event_id=None):
    return SessionRunEvent(
        event_version=1, event_id=event_id or f"event-{seq}", session_id="web-default",
        run_id=run_id, seq=seq, occurred_at=999.0 - seq, type=event_type,
        visibility="user", payload=payload or {},
    )


def test_projection_is_deterministic_and_terminal_state_cannot_regress():
    events = [
        _event(1, "user.message", {"text": "hi"}),
        _event(2, "run.accepted"),
        _event(3, "run.completed"),
        _event(4, "run.started"),
        _event(5, "assistant.message", {"text": "done"}),
        _event(5, "assistant.message", {"text": "duplicate"}, event_id="event-5-copy"),
    ]
    projection = project_session(events)
    assert projection.to_dict()["runs"]["run-1"]["status"] == "completed"
    assert [message["text"] for message in projection.messages] == ["hi", "done"]
    assert projection.to_dict() == project_session(events).to_dict()


def test_legacy_snapshot_migration_is_stable_and_projects_visible_messages():
    legacy = {
        "updated_at": 123.0, "mode": "chat", "chat_backend": "local",
        "messages": [{"role": "user", "text": "hello"}, {"role": "assistant", "text": "world"}],
    }
    events = migrate_legacy_snapshot(legacy)
    projection = project_session(events)
    assert events == migrate_legacy_snapshot(legacy)
    assert [message["text"] for message in projection.messages] == ["hello", "world"]
    assert projection.display_preferences == {"mode": "chat", "chat_backend": "local"}
