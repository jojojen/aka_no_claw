from __future__ import annotations

from openclaw_adapter.session_events import SessionRunEvent
from openclaw_adapter.session_projection import (
    is_authoritative_message,
    migrate_legacy_snapshot,
    project_session,
)


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


def test_projection_preserves_message_mode_and_legacy_chat_route():
    projection = project_session([
        _event(1, "user.message", {"text": "new", "mode": "chat"}, run_id="new"),
        _event(2, "run.accepted", {"mode": "chat"}, run_id="new"),
        _event(3, "user.message", {"text": "legacy"}, run_id="old"),
        _event(4, "run.accepted", run_id="old"),
        _event(5, "planner.completed", {"route": "stream_chat"}, run_id="old"),
    ])

    assert projection.messages[0]["mode"] == "chat"
    assert projection.runs["new"]["mode"] == "chat"
    assert projection.runs["old"]["route"] == "stream_chat"


def test_projection_marks_interrupted_partial_as_non_authoritative_but_keeps_it_visible():
    projection = project_session([
        _event(1, "user.message", {"text": "question", "mode": "chat"}),
        _event(2, "run.accepted", {"mode": "chat"}),
        _event(3, "assistant.message", {
            "text": "unfinished reasoning", "mode": "chat", "partial": True,
        }),
        _event(4, "run.interrupted"),
    ])

    assert [message["text"] for message in projection.messages] == [
        "question", "unfinished reasoning",
    ]
    assert is_authoritative_message(projection.messages[0], projection.runs) is True
    assert is_authoritative_message(projection.messages[1], projection.runs) is False


def test_legacy_snapshot_migration_is_stable_and_projects_visible_messages():
    legacy = {
        "updated_at": 123.0, "mode": "chat", "chat_backend": "local",
        "messages": [
            {"role": "user", "text": "hello", "modeLabel": "Chat"},
            {"role": "assistant", "text": "world", "mode": "chat"},
        ],
    }
    events = migrate_legacy_snapshot(legacy)
    projection = project_session(events)
    assert events == migrate_legacy_snapshot(legacy)
    assert [message["text"] for message in projection.messages] == ["hello", "world"]
    assert [message["mode"] for message in projection.messages] == ["chat", "chat"]
    assert projection.display_preferences == {"mode": "chat", "chat_backend": "local"}


def test_queue_snapshot_projects_without_creating_an_active_run():
    projection = project_session([
        _event(1, "queue.changed", {
            "running_prompt_id": "p1",
            "entries": [{"prompt_id": "p1", "version": 1, "position": 0, "text": "follow up"}],
        }, run_id="queue"),
    ])
    assert projection.to_dict()["prompt_queue"]["running_prompt_id"] == "p1"
    assert projection.active_run_ids == []


def test_session_clear_resets_messages_runs_queue_and_preferences():
    projection = project_session([
        _event(1, "session.created", {"display_preferences": {"mode": "chat"}}),
        _event(2, "user.message", {"text": "old"}),
        _event(3, "run.accepted"),
        _event(4, "queue.changed", {
            "running_prompt_id": None,
            "entries": [{"prompt_id": "p1", "text": "later"}],
        }, run_id="queue"),
        _event(5, "context.checkpoint", {"clear": True}, run_id="session"),
        _event(6, "tool.progress", {"stage": "research", "label": "late"}),
        _event(7, "assistant.message", {"text": "late answer"}),
    ])

    assert projection.messages == []
    assert projection.runs == {}
    assert projection.prompt_queue == {"running_prompt_id": None, "entries": []}
    assert projection.display_preferences == {}
