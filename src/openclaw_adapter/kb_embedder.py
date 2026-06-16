"""Ollama-backed multilingual embedder for KB semantic retrieval.

Wraps a local embed model (default ``bge-m3``) served by the Ollama text
endpoint. Conforms to ``knowledge_db.Embedder``: best-effort ``__call__`` that
returns ``None`` on any failure so a KB write never breaks when embedding is
unavailable.

Spike `docs/KB_EMBEDDING_PLAN.md` showed bge-m3 hit@1 9/10 vs lexical 6/10 on
the live 149-entry KB; an English-centric model (nomic) scored 3/10, so model
choice matters — keep this multilingual.
"""
from __future__ import annotations

import json
import logging
import ssl
import urllib.request

logger = logging.getLogger(__name__)

_DEFAULT_DIM = 1024  # bge-m3


class OllamaEmbedder:
    """Calls ``POST {endpoint}/api/embeddings``. ``dim`` is probed once at
    construction and corrected on every successful call."""

    def __init__(
        self,
        *,
        endpoint: str,
        model: str,
        timeout: int = 60,
        ssl_context: ssl.SSLContext | None = None,
    ) -> None:
        self.model = model
        self.dim = _DEFAULT_DIM
        self._url = endpoint.rstrip("/") + "/api/embeddings"
        self._timeout = max(1, int(timeout))
        self._ssl = ssl_context if endpoint.startswith("https://") else None
        probe = self("test")  # also sets self.dim on success
        if probe is None:
            logger.warning("KB embedder probe failed (model=%s url=%s)", model, self._url)

    def __call__(self, text: str) -> list[float] | None:
        body = json.dumps({"model": self.model, "prompt": text}).encode()
        req = urllib.request.Request(
            self._url, data=body, headers={"Content-Type": "application/json"}
        )
        try:
            with urllib.request.urlopen(req, timeout=self._timeout, context=self._ssl) as resp:
                payload = json.loads(resp.read())
        except Exception:
            logger.warning("KB embed call failed (model=%s)", self.model, exc_info=True)
            return None
        vec = payload.get("embedding")
        if not isinstance(vec, list) or not vec:
            logger.warning("KB embed: empty/invalid embedding for model=%s", self.model)
            return None
        self.dim = len(vec)
        return [float(x) for x in vec]


def build_kb_embedder(settings, ssl_context: ssl.SSLContext | None = None):
    """Build a KB embedder from settings, or ``None`` to disable KB embedding.

    Disabled when the text backend isn't ollama, the endpoint is missing, or
    ``openclaw_kb_embed_model`` is blank — in which case the KB stays
    pure-lexical (the pre-embedding behaviour)."""
    backend = (getattr(settings, "openclaw_local_text_backend", "") or "").strip().lower()
    endpoint = getattr(settings, "openclaw_local_text_endpoint", "") or ""
    model = (getattr(settings, "openclaw_kb_embed_model", "") or "").strip()
    timeout = max(1, getattr(settings, "openclaw_local_text_timeout_seconds", 60))
    if backend != "ollama" or not endpoint or not model:
        logger.info(
            "KB embedder disabled (backend=%s endpoint=%s model=%s) — KB stays lexical",
            backend, endpoint, model,
        )
        return None
    return OllamaEmbedder(
        endpoint=endpoint, model=model, timeout=timeout, ssl_context=ssl_context
    )
