"""Durable, bounded Web follow-up prompts (issue #86)."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Literal


PromptIntent = Literal["next_turn", "interjection"]
PromptStatus = Literal["queued", "draining", "started", "completed", "cancelled", "expired"]


class PromptQueueError(RuntimeError):
    """Base queue failure exposed as a typed HTTP response."""


class PromptQueueConflict(PromptQueueError):
    """A client tried to mutate a version that has changed."""


class PromptQueueCapacityError(PromptQueueError):
    """The bounded per-session queue is full."""


@dataclass(frozen=True, slots=True)
class QueuedPrompt:
    prompt_id: str
    session_id: str
    version: int
    position: int
    intent: PromptIntent
    mode: str
    capture_context: str | None
    text: str
    request: dict[str, Any]
    created_at: float
    updated_at: float
    expires_at: float
    status: PromptStatus = "queued"
    target_run_id: str | None = None
    started_run_id: str | None = None

    def public(self) -> dict[str, object]:
        return {
            "prompt_id": self.prompt_id,
            "session_id": self.session_id,
            "version": self.version,
            "position": self.position,
            "intent": self.intent,
            "mode": self.mode,
            "capture_context": self.capture_context,
            "text": self.text,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "expires_at": self.expires_at,
            "status": self.status,
            "target_run_id": self.target_run_id,
            "started_run_id": self.started_run_id,
        }

    def to_dict(self) -> dict[str, object]:
        return {**self.public(), "request": self.request}

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "QueuedPrompt":
        intent = str(value.get("intent") or "")
        status = str(value.get("status") or "queued")
        if intent not in {"next_turn", "interjection"}:
            raise PromptQueueError("invalid queued prompt intent")
        if status not in {"queued", "draining", "started", "completed", "cancelled", "expired"}:
            raise PromptQueueError("invalid queued prompt status")
        request = value.get("request")
        if not isinstance(request, dict):
            raise PromptQueueError("invalid queued prompt request")
        return cls(
            prompt_id=str(value["prompt_id"]), session_id=str(value["session_id"]),
            version=int(value["version"]), position=int(value["position"]),
            intent=intent, mode=str(value["mode"]),
            capture_context=(str(value["capture_context"]) if value.get("capture_context") else None),
            text=str(value["text"]), request=dict(request),
            created_at=float(value["created_at"]), updated_at=float(value["updated_at"]),
            expires_at=float(value["expires_at"]), status=status,
            target_run_id=(str(value["target_run_id"]) if value.get("target_run_id") else None),
            started_run_id=(str(value["started_run_id"]) if value.get("started_run_id") else None),
        )

    def evolve(self, **changes: object) -> "QueuedPrompt":
        return replace(self, **changes)
