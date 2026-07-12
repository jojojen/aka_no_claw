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
import tempfile
import threading
from pathlib import Path
from typing import Protocol

logger = logging.getLogger(__name__)

BACKEND_SYNTHETIC = "synthetic"
BACKEND_WHISPER_ENCODER = "whisper_encoder"


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


class WhisperEncoderEmbeddingBackend:
    """Local acoustic embedding from the already-installed Whisper encoder.

    Mean-pooling the encoder frames makes the vector robust to container bytes
    and small timing/noise changes, unlike the synthetic hash backend. Audio is
    decoded locally and the temporary file is removed before returning.
    """

    def __init__(self, *, model_name: str, device: str, compute_type: str, download_root: str) -> None:
        self._model_name = model_name
        self._device = device
        self._compute_type = compute_type
        self._download_root = download_root
        self._model = None
        self._lock = threading.Lock()

    @property
    def model_version(self) -> str:
        return f"whisper-encoder-v1:{self._model_name}"

    def _get_model(self):
        if self._model is None:
            with self._lock:
                if self._model is None:
                    from faster_whisper import WhisperModel
                    self._model = WhisperModel(
                        self._model_name,
                        device=self._device,
                        compute_type=self._compute_type,
                        download_root=self._download_root,
                    )
        return self._model

    def embed(self, audio: bytes) -> list[float]:
        if not audio:
            raise ValueError("cannot embed empty audio")
        path: str | None = None
        try:
            with tempfile.NamedTemporaryFile(suffix=".audio", delete=False) as handle:
                handle.write(audio)
                path = handle.name
            import numpy as np
            from faster_whisper.audio import decode_audio
            from faster_whisper.feature_extractor import FeatureExtractor

            waveform = decode_audio(path, sampling_rate=16000)
            features = FeatureExtractor()(waveform)
            encoded = np.asarray(self._get_model().encode(features))
            frames = encoded.reshape(-1, encoded.shape[-1])
            vector = frames.mean(axis=0)
            norm = float(np.linalg.norm(vector))
            if norm == 0.0:
                raise ValueError("Whisper encoder returned a zero embedding")
            return (vector / norm).astype(float).tolist()
        finally:
            if path is not None:
                Path(path).unlink(missing_ok=True)


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
    if backend_id == BACKEND_WHISPER_ENCODER:
        try:
            return WhisperEncoderEmbeddingBackend(
                model_name=str(getattr(settings, "openclaw_stt_model", "base")),
                device=str(getattr(settings, "openclaw_stt_device", "auto")),
                compute_type=str(getattr(settings, "openclaw_stt_compute_type", "default")),
                download_root=str(getattr(settings, "openclaw_stt_download_root", ".openclaw_tmp/whisper")),
            )
        except Exception:  # noqa: BLE001
            logger.exception("voice Whisper encoder backend unavailable; embedding disabled")
            return None
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
