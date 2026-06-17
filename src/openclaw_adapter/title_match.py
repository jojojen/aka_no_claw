"""Embedding-based product-title matcher for marketplace search results.

The lexical token filter in :mod:`market_monitor.mercari_search` keeps a listing
only when every query token appears verbatim in its title, so the *same* card
under a different spelling — ``ヴァイス`` vs ``ヴァイスシュヴァルツ``, a dropped
word, a reordering — gets discarded. This builds a matcher that scores each
title against the query by bge-m3 cosine similarity and keeps the ones above a
threshold, recovering those listings without a hardcoded synonym list (Rule G).

The matcher is dependency-injected (embedder + lexical fallback) so it stays a
pure function for unit tests, and ``market_monitor`` never has to import the
embedder (it lives here, in the higher repo).
"""
from __future__ import annotations

import logging
from typing import Callable, Optional

from .embedding_match import cosine, l2_normalize

logger = logging.getLogger(__name__)

Embedder = Callable[[str], "Optional[list[float]]"]
Items = "list[dict[str, object]]"
LexicalFallback = Callable[[str, Items], Items]
TitleMatcher = Callable[[str, Items], Items]


def _unit_vector(embedder: Embedder, text: str) -> "Optional[list[float]]":
    try:
        vec = embedder(text)
    except Exception:  # noqa: BLE001 - a flaky embed call must not kill the scrape
        return None
    if not vec:
        return None
    return l2_normalize(vec)


def build_semantic_title_matcher(
    embedder: Embedder,
    *,
    threshold: float,
    lexical_fallback: LexicalFallback,
) -> TitleMatcher:
    """Return a ``(query, items) -> kept_items`` matcher.

    Each item's ``title`` is embedded and kept when its cosine similarity to the
    embedded query is ``>= threshold``. If the *query* itself can't be embedded
    (Ollama down), the whole batch defers to ``lexical_fallback`` so behaviour
    degrades to the old token filter rather than dropping everything.
    """

    def _match(query: str, items: Items) -> Items:
        if not items:
            return []
        query_vec = _unit_vector(embedder, query)
        if query_vec is None:
            logger.warning(
                "title_match: query embed failed; using lexical fallback query=%s", query
            )
            return lexical_fallback(query, items)

        kept: list[dict[str, object]] = []
        for item in items:
            title = str(item.get("title") or "").strip()
            if not title:
                continue
            title_vec = _unit_vector(embedder, title)
            if title_vec is None:
                continue
            score = cosine(query_vec, title_vec)
            if score >= threshold:
                kept.append(item)
            else:
                logger.debug(
                    "title_match: dropped score=%.3f title=%r", score, title[:60]
                )
        logger.info(
            "title_match: query=%s kept=%d/%d threshold=%.2f",
            query, len(kept), len(items), threshold,
        )
        return kept

    return _match
