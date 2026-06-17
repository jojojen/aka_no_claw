"""Shared cosine-similarity helpers for local-embedding matchers.

Both the intent fast-path router and the image-translate caption recognizer
embed phrasings with bge-m3 and score by cosine similarity. The vector math is
identical, so it lives here once; the routing semantics on top stay separate.
"""
from __future__ import annotations

import math
from typing import Callable, Iterable


def l2_normalize(vec: "list[float]") -> "list[float] | None":
    """Return the unit vector, or None when the norm is zero / non-finite."""
    norm = math.sqrt(math.fsum(x * x for x in vec))
    if not norm or not math.isfinite(norm):
        return None
    return [x / norm for x in vec]


def cosine(a: "list[float]", b: "list[float]") -> float:
    """Cosine similarity of two already-unit vectors (a plain dot product)."""
    return math.fsum(x * y for x, y in zip(a, b))


def embed_unit_vectors(
    embedder: Callable[[str], "list[float] | None"], phrasings: Iterable[str]
) -> "list[list[float]]":
    """Embed each phrasing and L2-normalize it, skipping any the embedder chokes on."""
    rows: list[list[float]] = []
    for phrasing in phrasings:
        try:
            vec = embedder(phrasing)
        except Exception:  # noqa: BLE001 - skip a phrasing the embedder chokes on.
            vec = None
        if vec:
            unit = l2_normalize(vec)
            if unit is not None:
                rows.append(unit)
    return rows
