"""Pluggable source layer for /quiz question material.

The generator is source-agnostic: it asks a ``SourceProvider`` (selected by the
``theme`` argument of ``/quiz <level> <theme>``) for candidate material and gets
back generic ``QuizSource`` records. Adding a new theme (JPOP songs, short essays,
…) means writing one provider and registering it — no schema or generator change.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class QuizSource:
    """One piece of grounding material for a question, deliberately generic.

    ``source_type`` (vocaloid_song / jpop_song / essay / …) records what kind of
    material this is; ``media_url`` is optional (an essay has no audio).
    """
    source_type: str
    name: str
    text_url: str | None = None
    media_url: str | None = None
    excerpt: str | None = None


@runtime_checkable
class SourceProvider(Protocol):
    theme: str

    def fetch_candidates(self, limit: int = 10) -> list[QuizSource]:
        ...


_REGISTRY: dict[str, SourceProvider] = {}


def register_provider(provider: SourceProvider) -> None:
    """Register a provider under its (lower-cased) ``theme`` key. Idempotent."""
    key = (provider.theme or "").strip().lower()
    if not key:
        raise ValueError("provider.theme cannot be empty")
    _REGISTRY[key] = provider
    logger.info("quiz: registered source provider theme=%s", key)


def get_provider(theme: str) -> SourceProvider | None:
    return _REGISTRY.get((theme or "").strip().lower())


def available_themes() -> tuple[str, ...]:
    return tuple(sorted(_REGISTRY))
