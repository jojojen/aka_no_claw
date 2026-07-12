"""Thresholds and vocabulary for the voice gate (design §9.5 / §11).

All values are structural signals, not semantic word lists (Rule G):
short-form is defined by length/duration, never by device vocabulary.
Numbers are pre-benchmark conservative defaults (design §21 lists them as
"尚待 benchmark 決定"); tune here, not at call sites.
"""

from __future__ import annotations

# A control utterance like「關電扇」is 3 chars; an information question is
# usually much longer. Misrecognitions keep the short shape (「關鍵善」).
SHORT_TRANSCRIPT_MAX_CHARS = 12
SHORT_QUERY_MAX_CHARS = 16
SHORT_UTTERANCE_MAX_MS = 4_000

MAX_CLARIFY_CANDIDATES = 6

# Learning tokens (§13.2) are single-use and short-lived; long enough for a
# user to read the clarification card, far shorter than the utterance TTL.
LEARNING_TOKEN_TTL_SECONDS = 600

CLARIFY_FALLBACK_LABEL = "都不是，當一般問題處理"

REASON_FIRST_USE_CONTROL_SUSPICION = "first_use_control_suspicion"
REASON_PROTOTYPE_HIGH_CONFIDENCE = "prototype_high_confidence"

# Direct fast path (#82 PR4, §8.3): a prototype match may skip the Chat
# router only when ALL of these hold — absolute similarity, top-1/top-2
# margin across different actions, and enough confirmed samples. Conservative
# pre-benchmark values; open-set safety leans on the absolute threshold.
DIRECT_SIMILARITY_THRESHOLD = 0.85
DIRECT_MARGIN = 0.10
DIRECT_MIN_CONFIRMED = 3


def is_short_form(
    transcript: str, *, duration_ms: int | None = None
) -> bool:
    """Structural short-form check: short text, no URL, short audio.

    Any URL means the input cannot be a spoken control utterance; a known
    duration above the cap means the utterance was long even if the STT
    output came back short (mumbled speech collapses to few chars)."""
    text = (transcript or "").strip()
    if not text or len(text) > SHORT_TRANSCRIPT_MAX_CHARS:
        return False
    lowered = text.lower()
    if "://" in lowered or "http" in lowered or "www." in lowered:
        return False
    if duration_ms is not None and duration_ms > SHORT_UTTERANCE_MAX_MS:
        return False
    return True
