"""Typed contracts for the voice-intent gate (design doc §4.1 / §6.1)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, Sequence

RISK_LOW = "low"
RISK_MEDIUM = "medium"
RISK_HIGH = "high"

VOICE_RESOLUTION_CLARIFY = "clarify"
VOICE_RESOLUTION_FALLBACK = "fallback"
# direct_action arrives with the prototype fast path (PR4).


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
class VoiceClarification:
    """Wire contract for a clarify resolution (design §5.3, PR1 subset:
    no learning_token until PR3)."""

    transcript: str
    reason_code: str
    candidates: tuple[VoiceActionCandidate, ...]
    fallback_label: str

    def to_dict(self) -> dict[str, object]:
        return {
            "kind": VOICE_RESOLUTION_CLARIFY,
            "transcript": self.transcript,
            "reason_code": self.reason_code,
            "candidates": [c.to_dict() for c in self.candidates],
            "fallback": {"label": self.fallback_label},
        }
