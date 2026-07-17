"""Pure, deterministic projection of durable session/run events."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

from .session_events import SessionRunEvent, TERMINAL_EVENT_TYPES


PROJECTION_VERSION = 1
_TERMINAL_STATUS = {
    "run.completed": "completed", "run.failed": "failed", "run.cancelled": "cancelled",
    "run.interrupted": "interrupted",
}


@dataclass
class SessionProjection:
    session_id: str | None = None
    messages: list[dict[str, object]] = field(default_factory=list)
    runs: dict[str, dict[str, object]] = field(default_factory=dict)
    progress: dict[str, dict[str, object]] = field(default_factory=dict)
    display_preferences: dict[str, object] = field(default_factory=dict)
    last_cursor: int = 0
    active_run_ids: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return {
            "projection_version": PROJECTION_VERSION,
            "session_id": self.session_id,
            "messages": self.messages,
            "runs": {key: self.runs[key] for key in sorted(self.runs)},
            "progress": {key: self.progress[key] for key in sorted(self.progress)},
            "display_preferences": self.display_preferences,
            "last_cursor": self.last_cursor,
            "active_run_ids": self.active_run_ids,
        }


def project_session(events: Iterable[SessionRunEvent]) -> SessionProjection:
    """Reduce ordered events without consulting clocks or mutable storage.

    Duplicate event IDs/sequences are no-ops. A terminal run state never changes,
    even if a later event describes a stale worker transition.
    """
    projection = SessionProjection()
    seen_ids: set[str] = set()
    seen_sequences: set[int] = set()
    for event in events:
        if event.event_id in seen_ids or event.seq in seen_sequences:
            continue
        seen_ids.add(event.event_id)
        seen_sequences.add(event.seq)
        if projection.session_id is None:
            projection.session_id = event.session_id
        elif projection.session_id != event.session_id:
            raise ValueError("cannot project events from multiple sessions")
        projection.last_cursor = max(projection.last_cursor, event.seq)
        _apply(projection, event)
    projection.active_run_ids = sorted(
        run_id for run_id, run in projection.runs.items() if run["status"] not in _TERMINAL_STATUS.values()
    )
    return projection


def _apply(projection: SessionProjection, event: SessionRunEvent) -> None:
    if event.type == "session.created":
        preferences = event.payload.get("display_preferences")
        if isinstance(preferences, dict):
            projection.display_preferences = preferences
        return
    if event.type in {"user.message", "assistant.message"}:
        text = event.payload.get("text")
        if isinstance(text, str):
            projection.messages.append({
                "event_id": event.event_id, "run_id": event.run_id,
                "role": "user" if event.type == "user.message" else "assistant", "text": text,
            })
        return
    run = projection.runs.setdefault(event.run_id, {"status": "accepted", "last_seq": event.seq})
    if run["status"] in _TERMINAL_STATUS.values() and event.type not in TERMINAL_EVENT_TYPES:
        return
    run["last_seq"] = event.seq
    if event.type == "run.accepted":
        run["status"] = "accepted"
    elif event.type == "run.started":
        run["status"] = "running"
    elif event.type in _TERMINAL_STATUS:
        if run["status"] not in _TERMINAL_STATUS.values():
            run["status"] = _TERMINAL_STATUS[event.type]
    elif event.type == "tool.progress":
        stage = event.payload.get("stage")
        if isinstance(stage, str):
            projection.progress[f"{event.run_id}:{stage}"] = dict(event.payload)


def migrate_legacy_snapshot(snapshot: dict, *, session_id: str = "web-default") -> list[SessionRunEvent]:
    """Build deterministic evidence-tagged events from the old snapshot format."""
    from uuid import NAMESPACE_URL, uuid5

    updated_at = snapshot.get("updated_at", 0.0)
    occurred_at = float(updated_at) if isinstance(updated_at, (int, float)) else 0.0
    preferences = {
        key: snapshot[key] for key in ("mode", "chat_backend", "investment_submode")
        if snapshot.get(key) is not None
    }
    seed = f"aka-no-claw:legacy:{session_id}"
    events = [SessionRunEvent(
        event_version=1, event_id=uuid5(NAMESPACE_URL, f"{seed}:created").hex,
        session_id=session_id, run_id="legacy-import", seq=1, occurred_at=occurred_at,
        type="session.created", visibility="internal",
        payload={"evidence": "legacy_snapshot", "display_preferences": preferences},
    )]
    for index, message in enumerate(snapshot.get("messages") or [], start=2):
        if not isinstance(message, dict) or message.get("role") not in {"user", "assistant"}:
            continue
        text = message.get("text")
        if not isinstance(text, str):
            continue
        events.append(SessionRunEvent(
            event_version=1, event_id=uuid5(NAMESPACE_URL, f"{seed}:{index}").hex,
            session_id=session_id, run_id="legacy-import", seq=len(events) + 1,
            occurred_at=occurred_at, type=f"{message['role']}.message", visibility="user",
            payload={"text": text, "evidence": "legacy_snapshot"},
        ))
    return events
