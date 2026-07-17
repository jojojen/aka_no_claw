"""Versioned, safe event envelopes for replayable Web-console sessions."""

from __future__ import annotations

from dataclasses import dataclass
import json
import re
import unicodedata
from typing import Any, Mapping


EVENT_VERSION = 1
DEFAULT_MAX_PAYLOAD_BYTES = 64 * 1024
MAX_STRING_BYTES = 16 * 1024

EVENT_TYPES = frozenset({
    "session.created", "user.message", "run.accepted", "run.started",
    "planner.completed", "tool.started", "tool.progress", "tool.completed",
    "judge.completed", "assistant.delta", "assistant.message", "run.completed",
    "run.failed", "run.cancel_requested", "run.cancelled", "run.interrupted",
    "approval.requested", "approval.resolved", "context.checkpoint", "queue.changed",
    "interjection.accepted",
})
DURABLE_EVENT_TYPES = EVENT_TYPES - {"assistant.delta"}
TERMINAL_EVENT_TYPES = frozenset({
    "run.completed", "run.failed", "run.cancelled", "run.interrupted",
})
VISIBILITIES = frozenset({"user", "internal"})
_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_EVENT_TYPE_RE = re.compile(r"^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+$")
_PRIVATE_KEY_PARTS = frozenset({
    "api_key", "apikey", "authorization", "cookie", "password", "secret",
    "system_prompt", "reasoning", "chain_of_thought",
})


class EventValidationError(ValueError):
    """Raised when an event cannot safely become durable history."""


def validate_identifier(value: object, field: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER_RE.fullmatch(value):
        raise EventValidationError(f"{field} must be a 1-128 character safe identifier")
    return value


def _normalise_value(value: object, *, path: str = "payload") -> object:
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if value != value or value in {float("inf"), float("-inf")}:
            raise EventValidationError(f"{path} cannot contain a non-finite number")
        return value
    if isinstance(value, str):
        normalised = unicodedata.normalize("NFC", value)
        if len(normalised.encode("utf-8")) > MAX_STRING_BYTES:
            raise EventValidationError(f"{path} exceeds the string size limit")
        return normalised
    if isinstance(value, Mapping):
        output: dict[str, object] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise EventValidationError(f"{path} keys must be strings")
            normalised_key = unicodedata.normalize("NFC", key)
            if any(part in normalised_key.lower() for part in _PRIVATE_KEY_PARTS):
                raise EventValidationError(f"{path}.{normalised_key} is not allowed in event history")
            output[normalised_key] = _normalise_value(item, path=f"{path}.{normalised_key}")
        return output
    if isinstance(value, (list, tuple)):
        return [_normalise_value(item, path=f"{path}[]") for item in value]
    raise EventValidationError(f"{path} must contain JSON-compatible values")


def normalise_payload(payload: Mapping[str, object], *, max_bytes: int = DEFAULT_MAX_PAYLOAD_BYTES) -> dict[str, object]:
    if not isinstance(payload, Mapping):
        raise EventValidationError("payload must be an object")
    if max_bytes <= 0:
        raise EventValidationError("max_bytes must be positive")
    normalised = _normalise_value(payload)
    assert isinstance(normalised, dict)
    encoded = json.dumps(
        normalised, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")
    if len(encoded) > max_bytes:
        raise EventValidationError(f"payload exceeds {max_bytes} bytes")
    return normalised


@dataclass(frozen=True, slots=True)
class SessionRunEvent:
    event_version: int
    event_id: str
    session_id: str
    run_id: str
    seq: int
    occurred_at: float
    type: str
    visibility: str
    payload: dict[str, object]

    def __post_init__(self) -> None:
        if self.event_version != EVENT_VERSION:
            raise EventValidationError(f"unsupported event_version: {self.event_version}")
        validate_identifier(self.event_id, "event_id")
        validate_identifier(self.session_id, "session_id")
        validate_identifier(self.run_id, "run_id")
        if not isinstance(self.seq, int) or self.seq < 1:
            raise EventValidationError("seq must be a positive integer")
        if not isinstance(self.occurred_at, (int, float)):
            raise EventValidationError("occurred_at must be numeric")
        if self.type not in EVENT_TYPES:
            raise EventValidationError(f"unknown event type: {self.type}")
        if self.visibility not in VISIBILITIES:
            raise EventValidationError(f"unknown visibility: {self.visibility}")
        object.__setattr__(self, "payload", normalise_payload(self.payload))

    @property
    def is_terminal(self) -> bool:
        return self.type in TERMINAL_EVENT_TYPES

    def to_dict(self) -> dict[str, object]:
        return {
            "event_version": self.event_version,
            "event_id": self.event_id,
            "session_id": self.session_id,
            "run_id": self.run_id,
            "seq": self.seq,
            "occurred_at": self.occurred_at,
            "type": self.type,
            "visibility": self.visibility,
            "payload": self.payload,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "SessionRunEvent":
        try:
            raw_type = value["type"]
            if raw_type in EVENT_TYPES:
                return cls(
                    event_version=value["event_version"], event_id=value["event_id"],
                    session_id=value["session_id"], run_id=value["run_id"], seq=value["seq"],
                    occurred_at=value["occurred_at"], type=raw_type, visibility=value["visibility"],
                    payload=value["payload"],
                )
            if not isinstance(raw_type, str) or not _EVENT_TYPE_RE.fullmatch(raw_type):
                raise EventValidationError(f"unknown event type: {raw_type}")
            # A future writer may add a type unknown to this code. Preserve the
            # validated envelope so its sequence is never lost; the v1
            # projector deliberately ignores it.
            known = cls(
                event_version=value["event_version"], event_id=value["event_id"],
                session_id=value["session_id"], run_id=value["run_id"], seq=value["seq"],
                occurred_at=value["occurred_at"], type="context.checkpoint",
                visibility=value["visibility"], payload=value["payload"],
            )
            object.__setattr__(known, "type", raw_type)
            return known
        except KeyError as exc:
            raise EventValidationError(f"event is missing {exc.args[0]}") from exc
