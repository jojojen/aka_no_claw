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

from collections.abc import Sequence

from . import policy
from .embedding import cosine_similarity
from .models import (
    RISK_LOW,
    VoiceActionCandidate,
    VoiceActionDescriptor,
    VoiceActionRegistry,
    VoiceClarification,
    VoiceDirectAction,
    VoiceUserContext,
)


def resolve_direct_prototype_action(
    *,
    embedding: Sequence[float],
    prototypes: Sequence[object],
    actions: Sequence[VoiceActionDescriptor],
    min_confirmed: int = policy.DIRECT_MIN_CONFIRMED,
    similarity_threshold: float = policy.DIRECT_SIMILARITY_THRESHOLD,
    required_margin: float = policy.DIRECT_MARGIN,
) -> VoiceDirectAction | None:
    """Prototype direct fast path decision (#82 PR4, design §8.3), pure.

    Returns a dispatchable resolution ONLY when every hard rule holds:
    the best-matching action is available, low-risk AND reversible; the
    matched prototype has enough confirmed samples; similarity clears the
    absolute threshold; and the top-1/top-2 margin across *different*
    actions is wide enough. Any miss returns None — open-set rejection
    (§3.4): unknown speech must never dispatch. With a single learned
    action there is no top-2, so safety rests on the absolute threshold
    (margin is then trivially satisfied).
    """
    if not embedding or not prototypes:
        return None
    eligible = {
        a.action_id: a
        for a in actions
        if a.available and a.risk == RISK_LOW and a.reversible
    }
    if not eligible:
        return None
    # Best similarity + best prototype per action (§7.5: nearest prototype).
    best_by_action: dict[str, tuple[float, object]] = {}
    for proto in prototypes:
        action_id = getattr(proto, "action_id", "")
        if action_id not in eligible:
            continue
        score = cosine_similarity(embedding, getattr(proto, "embedding", ()))
        prev = best_by_action.get(action_id)
        if prev is None or score > prev[0]:
            best_by_action[action_id] = (score, proto)
    if not best_by_action:
        return None
    ranked = sorted(
        best_by_action.items(), key=lambda item: item[1][0], reverse=True
    )
    top_action_id, (top_score, top_proto) = ranked[0]
    runner_up = ranked[1][1][0] if len(ranked) > 1 else 0.0
    margin = top_score - runner_up
    if top_score < similarity_threshold:
        return None
    if margin < required_margin:
        return None
    if int(getattr(top_proto, "confirmed_count", 0)) < min_confirmed:
        return None
    descriptor = eligible[top_action_id]
    return VoiceDirectAction(
        action_id=descriptor.action_id,
        display_label=descriptor.display_label,
        risk=descriptor.risk,
        confidence=top_score,
        margin=margin,
        reason_code=policy.REASON_PROTOTYPE_HIGH_CONFIDENCE,
        prototype_id=str(getattr(top_proto, "prototype_id", "")),
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
