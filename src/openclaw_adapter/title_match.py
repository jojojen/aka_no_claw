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
import re
from typing import Callable, Optional

from .embedding_match import cosine, l2_normalize

logger = logging.getLogger(__name__)

Embedder = Callable[[str], "Optional[list[float]]"]
Items = "list[dict[str, object]]"
LexicalFallback = Callable[[str, Items], Items]
TitleMatcher = Callable[[str, Items], Items]

# Contiguous alphanumeric run allowing the '/' and '-' separators that appear
# inside card serial numbers (e.g. ``123/456``, ``BSF-01``).
_IDENTITY_RUN_RE = re.compile(r"[0-9A-Za-z/\-]+")
_DIGIT_RUN_RE = re.compile(r"\d+")


def extract_identity_tokens(text: str) -> set[str]:
    """Distinctive serial-like tokens (card numbers) in a query or title.

    Structural, not a keyword list (Rule G): a run qualifies as an identity
    token when it contains a digit and is *distinctive* — either mixed
    alphanumeric / separated (``BSF-01``, ``123/456``, ``SP01``) or a long
    pure-digit run. Bare rarities (``RR``, ``SSP`` — no digit) and short
    quantities (``10``) are deliberately excluded so they can never trigger a
    false identity match between two different cards.
    """
    tokens: set[str] = set()
    for raw in _IDENTITY_RUN_RE.findall(text or ""):
        tok = raw.strip("/-").lower()
        if len(tok) < 3 or not any(ch.isdigit() for ch in tok):
            continue
        has_alpha_or_sep = any(ch.isalpha() or ch in "/-" for ch in tok)
        longest_digit_run = max((len(m) for m in _DIGIT_RUN_RE.findall(tok)), default=0)
        if has_alpha_or_sep or longest_digit_run >= 4:
            tokens.add(tok)
    return tokens


def _title_matches_identity(title: str, query_identity: set[str]) -> bool:
    """True when every distinctive query serial appears verbatim in ``title``.

    An exact card-number match is a stronger identity signal than embedding
    similarity (the embedder is exactly what mis-ranks a same-card near-miss),
    so it overrides the semantic threshold. Requiring *all* query identity
    tokens keeps a different card that merely shares one coincidental number
    from being rescued.
    """
    if not query_identity:
        return False
    hay = title.lower()
    return all(tok in hay for tok in query_identity)


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

        query_identity = extract_identity_tokens(query)
        kept: list[dict[str, object]] = []
        rescued = 0
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
            elif _title_matches_identity(title, query_identity):
                rescued += 1
                logger.info(
                    "title_match: identity bypass score=%.3f title=%r ids=%s",
                    score, title[:60], sorted(query_identity),
                )
                kept.append(item)
            else:
                logger.debug(
                    "title_match: dropped score=%.3f title=%r", score, title[:60]
                )
        logger.info(
            "title_match: query=%s kept=%d/%d (identity_rescued=%d) threshold=%.2f",
            query, len(kept), len(items), rescued, threshold,
        )
        return kept

    return _match
