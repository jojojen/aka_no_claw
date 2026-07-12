"""Audio embedding abstraction for voice personalization (#82 PR2, design §7.1).

The real local acoustic backend is deliberately NOT chosen here — design §21
lists it as pending benchmark. This module ships the protocol every backend
must satisfy, a deterministic synthetic backend (CI / tests / benchmark
plumbing, per design §15 「CI 加入 deterministic synthetic embedding backend」),
and the settings-driven resolver. Embeddings are versioned; vectors from
different model versions are never compared (design §12.3).
"""

from __future__ import annotations

import hashlib
import logging
import math
from typing import Protocol

logger = logging.getLogger(__name__)

BACKEND_SYNTHETIC = "synthetic"


class VoiceEmbeddingBackend(Protocol):
    @property
    def model_version(self) -> str: ...

    def embed(self, audio: bytes) -> list[float]: ...


class SyntheticEmbeddingBackend:
    """Deterministic bytes→vector backend.

    Identical audio bytes always produce the identical unit-norm vector, and
    different bytes are effectively orthogonal — enough to exercise store
    round-trips, model-version isolation and nearest-prototype retrieval in
    tests and the benchmark harness without any model download. It carries no
    acoustic meaning and must never be promoted to a production default."""

    def __init__(self, dim: int = 32) -> None:
        self._dim = max(4, dim)

    @property
    def model_version(self) -> str:
        return f"synthetic-v1-d{self._dim}"

    def embed(self, audio: bytes) -> list[float]:
        if not audio:
            raise ValueError("cannot embed empty audio")
        values: list[float] = []
        counter = 0
        while len(values) < self._dim:
            digest = hashlib.sha256(audio + counter.to_bytes(4, "big")).digest()
            values.extend(b / 255.0 - 0.5 for b in digest)
            counter += 1
        vec = values[: self._dim]
        norm = math.sqrt(sum(v * v for v in vec)) or 1.0
        return [v / norm for v in vec]


def resolve_embedding_backend(settings: object) -> VoiceEmbeddingBackend | None:
    """Build the configured backend, or None when embedding is disabled.

    Fail-soft by contract (design §13.4): an unknown/broken backend id logs
    and disables embedding rather than breaking STT or chat."""
    backend_id = str(
        getattr(settings, "openclaw_voice_embedding_backend", "") or ""
    ).strip().lower()
    if not backend_id:
        return None
    if backend_id == BACKEND_SYNTHETIC:
        return SyntheticEmbeddingBackend()
    logger.warning(
        "voice embedding backend %r is not available; embedding disabled",
        backend_id,
    )
    return None


def cosine_similarity(a: list[float], b: list[float]) -> float:
    if len(a) != len(b) or not a:
        raise ValueError("cosine_similarity requires equal, non-zero dimensions")
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)
