"""First-use unresolved-control gate (design §8/§9), pure decision logic.

The gate decides only「是否先問」; it never picks an action by itself
(design §9.2). Clarification requires ALL structural signals (design §9.5):

    voice source AND short-form AND open-tool plan AND
    available low-risk candidates AND the user has not already declined.

No transcript semantics, no device-word lists — misrecognized control
speech (「關鍵善」) and real short questions are separated by the user's
explicit choice in the clarification card, which is what bootstraps the
learning loop in later PRs.
"""

from __future__ import annotations

from . import policy
from .models import (
    RISK_LOW,
    VoiceActionCandidate,
    VoiceActionRegistry,
    VoiceClarification,
    VoiceUserContext,
)


class VoiceIntentGate:
    def __init__(self, registry: VoiceActionRegistry) -> None:
        self._registry = registry

    def should_clarify_before_open_tool(
        self,
        *,
        transcript: str,
        plan_query: str,
        duration_ms: int | None = None,
        clarification_declined: bool = False,
        user_context: VoiceUserContext | None = None,
    ) -> bool:
        """Caller has already established voice provenance and an
        open-ended tool plan (/search, /research); this adds the
        short-form + candidate-availability signals."""
        if clarification_declined:
            return False
        if not policy.is_short_form(transcript, duration_ms=duration_ms):
            return False
        query = (plan_query or "").strip()
        if query and len(query) > policy.SHORT_QUERY_MAX_CHARS:
            return False
        return bool(self._low_risk_candidates(user_context))

    def build_first_use_clarification(
        self,
        *,
        transcript: str,
        user_context: VoiceUserContext | None = None,
    ) -> VoiceClarification:
        candidates = self._low_risk_candidates(user_context)
        return VoiceClarification(
            transcript=transcript,
            reason_code=policy.REASON_FIRST_USE_CONTROL_SUSPICION,
            candidates=tuple(candidates[: policy.MAX_CLARIFY_CANDIDATES]),
            fallback_label=policy.CLARIFY_FALLBACK_LABEL,
        )

    def _low_risk_candidates(
        self, user_context: VoiceUserContext | None
    ) -> list[VoiceActionCandidate]:
        context = user_context or VoiceUserContext()
        try:
            actions = self._registry.list_actions(user_context=context)
        except Exception:  # noqa: BLE001 — registry failure must not break chat
            return []
        return [
            VoiceActionCandidate(
                action_id=a.action_id,
                display_label=a.display_label,
                risk=a.risk,
            )
            for a in actions
            if a.available and a.risk == RISK_LOW
        ]
