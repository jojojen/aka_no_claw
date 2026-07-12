"""Learning-token lifecycle and atomic prototype commit (#82 PR3, §5.4/§7.3).

The raw token travels to the client inside the clarification payload; only
its SHA-256 hash is stored (design §13.2). A prototype is committed ONLY
when every §7.3 condition holds: valid single-use token, the confirmed
action was in the token's candidate set, the action executed successfully,
and the utterance still has a readable, version-tagged embedding. Fallback,
failure, replay and expiry all learn nothing (§7.4).
"""

from __future__ import annotations

import hashlib
import logging
import secrets
from dataclasses import dataclass
from uuid import uuid4

from . import policy
from .embedding import cosine_similarity
from .prototype_store import VoiceStore, VoiceStoreError

logger = logging.getLogger(__name__)

LEARNING_COMMITTED = "committed"
LEARNING_SKIPPED_NO_TOKEN = "skipped_no_token"
LEARNING_SKIPPED_NO_EMBEDDING = "skipped_no_embedding"
LEARNING_INVALID_TOKEN = "invalid_token"
LEARNING_ACTION_NOT_IN_CANDIDATES = "action_not_in_candidates"
LEARNING_ACTION_FAILED = "action_failed"
LEARNING_STORE_ERROR = "store_error"


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class RedeemedToken:
    utterance_id: str
    profile_id: str
    candidate_action_ids: tuple[str, ...]


def issue_learning_token(
    store: VoiceStore,
    *,
    utterance_id: str,
    candidate_action_ids: tuple[str, ...],
    ttl_seconds: float = policy.LEARNING_TOKEN_TTL_SECONDS,
) -> str | None:
    """Create a token bound to this utterance + candidate set; returns the raw
    token for the clarification payload, or None on store failure (fail-soft:
    clarification still works, learning is just unavailable)."""
    token = secrets.token_urlsafe(32)
    try:
        store.create_learning_token(
            token_hash=_hash_token(token),
            utterance_id=utterance_id,
            candidate_action_ids=candidate_action_ids,
            ttl_seconds=ttl_seconds,
        )
    except VoiceStoreError:
        logger.exception("learning token issuance failed (fail-soft)")
        return None
    return token


def redeem_learning_token(store: VoiceStore, token: str) -> RedeemedToken | None:
    """Single-use redemption. None for unknown/expired/already-consumed."""
    try:
        row = store.consume_learning_token(_hash_token(token))
    except VoiceStoreError:
        logger.exception("learning token redemption failed (fail-soft)")
        return None
    if row is None:
        return None
    return RedeemedToken(
        utterance_id=str(row["utterance_id"]),
        profile_id=str(row["profile_id"]),
        candidate_action_ids=tuple(row["candidate_action_ids"]),
    )


def commit_prototype(
    store: VoiceStore,
    *,
    utterance_id: str,
    action_id: str,
) -> str:
    """Create a prototype from the stored utterance embedding (§7.3). Returns
    a LEARNING_* status string; never raises (learning must not break the
    action result the user is looking at)."""
    try:
        utterance = store.get_utterance(utterance_id)
        if (
            utterance is None
            or not utterance.embedding
            or not utterance.embedding_model_version
        ):
            return LEARNING_SKIPPED_NO_EMBEDDING
        embedding = list(utterance.embedding)
        # §7.5 merge: reinforce an existing over-similar prototype of the same
        # action instead of stacking count=1 siblings that can never mature.
        existing = store.list_prototypes(
            action_id=action_id,
            embedding_model_version=utterance.embedding_model_version,
        )
        best = None
        best_score = 0.0
        for proto in existing:
            if len(proto.embedding) != len(embedding):
                continue
            score = cosine_similarity(list(proto.embedding), embedding)
            if score > best_score:
                best, best_score = proto, score
        if best is not None and best_score >= policy.PROTOTYPE_MERGE_SIMILARITY:
            store.record_confirmation(best.prototype_id)
        else:
            store.add_prototype(
                prototype_id=uuid4().hex,
                action_id=action_id,
                embedding=embedding,
                embedding_model_version=utterance.embedding_model_version,
            )
        # Consumed utterances are GC'd promptly (§12.2).
        store.mark_utterance_consumed(utterance_id)
        return LEARNING_COMMITTED
    except VoiceStoreError:
        logger.exception("prototype commit failed (fail-soft)")
        return LEARNING_STORE_ERROR
