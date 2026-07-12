"""Typed contracts for the voice-intent gate (design doc §4.1 / §6.1)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, Sequence

RISK_LOW = "low"
RISK_MEDIUM = "medium"
RISK_HIGH = "high"

VOICE_RESOLUTION_CLARIFY = "clarify"
VOICE_RESOLUTION_FALLBACK = "fallback"
VOICE_RESOLUTION_DIRECT_ACTION = "direct_action"


@dataclass(frozen=True)
class VoiceActionDescriptor:
    """One executable action exposed to the voice gate (design §6.1).

    ``dispatch_payload`` is server-side only: clients receive candidates
    (action_id/display_label/risk) and submit back only ``action_id``; the
    backend re-resolves the registry before dispatching (design §5.5)."""

    action_id: str
    display_label: str
    surface: str
    risk: str
    reversible: bool
    available: bool
    context_tags: tuple[str, ...] = ()
    dispatch_kind: str = ""
    dispatch_payload: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class VoiceUserContext:
    conversation_id: str | None = None
    surface: str | None = None


class VoiceActionRegistry(Protocol):
    def list_actions(
        self, *, user_context: VoiceUserContext
    ) -> Sequence[VoiceActionDescriptor]: ...


@dataclass(frozen=True)
class VoiceActionCandidate:
    action_id: str
    display_label: str
    risk: str
    score: float = 0.0

    def to_dict(self) -> dict[str, object]:
        return {
            "action_id": self.action_id,
            "display_label": self.display_label,
            "risk": self.risk,
            "score": self.score,
        }


@dataclass(frozen=True)
class VoiceDirectAction:
    """Wire contract for a prototype direct-action resolution (design §5.3).

    ``prototype_id`` lets the client send negative feedback（「不是這個」）
    against exactly the prototype that triggered the dispatch (§7.6)."""

    action_id: str
    display_label: str
    risk: str
    confidence: float
    margin: float
    reason_code: str
    prototype_id: str

    def to_dict(self) -> dict[str, object]:
        return {
            "kind": VOICE_RESOLUTION_DIRECT_ACTION,
            "action": {
                "action_id": self.action_id,
                "display_label": self.display_label,
                "risk": self.risk,
            },
            "confidence": self.confidence,
            "margin": self.margin,
            "reason_code": self.reason_code,
            "prototype_id": self.prototype_id,
        }


@dataclass(frozen=True)
class VoiceClarification:
    """Wire contract for a clarify resolution (design §5.3).

    ``learning_token`` (PR3) is the raw single-use token the client must echo
    back on confirm for a successful action to become a prototype; None when
    the voice store or utterance embedding is unavailable — clarification
    still works, learning is simply off for this turn."""

    transcript: str
    reason_code: str
    candidates: tuple[VoiceActionCandidate, ...]
    fallback_label: str
    learning_token: str | None = None

    def to_dict(self) -> dict[str, object]:
        out: dict[str, object] = {
            "kind": VOICE_RESOLUTION_CLARIFY,
            "transcript": self.transcript,
            "reason_code": self.reason_code,
            "candidates": [c.to_dict() for c in self.candidates],
            "fallback": {"label": self.fallback_label},
        }
        if self.learning_token:
            out["learning_token"] = self.learning_token
        return out
