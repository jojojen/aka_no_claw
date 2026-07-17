from __future__ import annotations

import pytest

from openclaw_adapter.session_events import EventValidationError, SessionRunEvent


def _event(**changes):
    data = {
        "event_version": 1, "event_id": "event-1", "session_id": "web-default",
        "run_id": "run-1", "seq": 1, "occurred_at": 1.0, "type": "tool.progress",
        "visibility": "user", "payload": {"stage": "research", "completed": 1},
    }
    data.update(changes)
    return SessionRunEvent(**data)


def test_event_wire_shape_is_versioned_and_normalised():
    event = _event(payload={"label": "e\u0301"})
    assert event.to_dict() == {
        "event_version": 1, "event_id": "event-1", "session_id": "web-default",
        "run_id": "run-1", "seq": 1, "occurred_at": 1.0, "type": "tool.progress",
        "visibility": "user", "payload": {"label": "é"},
    }


@pytest.mark.parametrize("changes", [
    {"type": "made.up"}, {"seq": 0}, {"payload": {"api_key": "nope"}},
    {"payload": {"value": object()}},
])
def test_event_rejects_unknown_or_unsafe_values(changes):
    with pytest.raises(EventValidationError):
        _event(**changes)


def test_event_rejects_oversized_payload():
    with pytest.raises(EventValidationError):
        _event(payload={"text": "x" * (17 * 1024)})


def test_unknown_future_type_round_trips_for_an_older_projector():
    wire = _event().to_dict() | {"type": "future.noted"}
    assert SessionRunEvent.from_dict(wire).type == "future.noted"
