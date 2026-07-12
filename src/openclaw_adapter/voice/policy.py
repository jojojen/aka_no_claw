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
# margin across different actions, and enough confirmed samples.
#
# Calibrated 2026-07-13 against whisper-encoder-v2:base on a zh TTS corpus
# (benchmark.py, 15 known / 3 open-set): same-phrase pairs score ≥0.984,
# different-phrase pairs ≤0.966 (worst case 關電扇 vs 開電扇), open-set
# utterances ≈0.89. At 0.85 the open-set false-accept rate was 100%; at 0.98
# it is 0% with top-1 accuracy 1.0. Margin follows the same scale: own-
# prototype ~0.99 vs nearest other action ~0.96 leaves ~0.03, so 0.10 would
# permanently block direct dispatch. Re-calibrate on real recorded voice (PR5).
DIRECT_SIMILARITY_THRESHOLD = 0.98
DIRECT_MARGIN = 0.02
DIRECT_MIN_CONFIRMED = 3

# Commit-time merge (§7.5「定期合併過度相近樣本」): a confirmed utterance whose
# embedding already scores this close to an existing prototype of the SAME
# action reinforces it (confirmed_count += 1) instead of inserting a sibling.
# Without merging, every confirmation lands as a fresh count=1 row and no
# prototype can ever reach DIRECT_MIN_CONFIRMED. Same scale as the direct
# threshold: measured same-phrase renditions score ≥0.984.
PROTOTYPE_MERGE_SIMILARITY = 0.98


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
