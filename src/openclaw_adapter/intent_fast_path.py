"""Embedding fast-path in front of the LLM intent router.

Zero-arg commands (list/info) carry no parameters, so a single bge-m3 embedding
+ cosine over canonical phrasings can route them in ~130 ms instead of waiting
~50 s for the qwen3 LLM router. Slot-bearing intents are never short-circuited
here — they always fall through to the LLM so parameter extraction is unchanged.

This is a fast-path, NOT a replacement: `route()` returns None to mean "not
confident / not a zero-arg intent → let the existing LLM router handle it".
The blast radius of a wrong short-circuit is bounded to the six harmless
list/info commands in ZERO_ARG_INTENTS.
"""
from __future__ import annotations

import json
import logging
import math
from pathlib import Path

from price_monitor_bot.natural_language import TelegramNaturalLanguageIntent

logger = logging.getLogger(__name__)

# Commands whose handler needs NO extracted parameters. Only these may be
# short-circuited; every other intent carries slots the LLM must extract.
ZERO_ARG_INTENTS = frozenset(
    {"help", "status", "tools", "scan_help", "sns_list", "list_watches"}
)

# Margin the winning intent must beat the runner-up by. Guards the close
# zero-arg pair sns_list vs list_watches: a genuinely ambiguous "看清單" falls
# through to the LLM (which has the SNS/Mercari disambiguation rules) instead of
# a confident wrong pick.
_DEFAULT_MARGIN = 0.03
_DEFAULT_MIN_SCORE = 0.65

_PHRASINGS_PATH = (
    Path(__file__).resolve().parents[2] / "data" / "intent_routing_phrasings.json"
)


def _norm(vec: list[float]) -> list[float] | None:
    n = math.sqrt(math.fsum(x * x for x in vec))
    if not n or not math.isfinite(n):
        return None
    return [x / n for x in vec]


def _dot(a: list[float], b: list[float]) -> float:
    return math.fsum(x * y for x, y in zip(a, b))


class EmbeddingIntentRouter:
    def __init__(
        self,
        embedder,
        phrasings: dict[str, list[str]],
        *,
        min_score: float = _DEFAULT_MIN_SCORE,
        margin: float = _DEFAULT_MARGIN,
        zero_arg_intents=ZERO_ARG_INTENTS,
    ) -> None:
        self._embedder = embedder
        self._min_score = min_score
        self._margin = margin
        self._zero_arg = frozenset(zero_arg_intents)
        self._index: dict[str, list[list[float]]] = {}
        for intent, examples in phrasings.items():
            rows: list[list[float]] = []
            for ex in examples:
                try:
                    vec = embedder(ex)
                except Exception:  # noqa: BLE001 - best effort, skip bad phrasing
                    vec = None
                if vec:
                    unit = _norm(vec)
                    if unit is not None:
                        rows.append(unit)
            if rows:
                self._index[intent] = rows
        self.ready = bool(self._index)

    def route(self, text: str) -> TelegramNaturalLanguageIntent | None:
        if not self.ready or not text or not text.strip():
            return None
        try:
            qvec = self._embedder(text)
        except Exception:  # noqa: BLE001 - embed outage must not break routing
            logger.exception("intent fast-path embed failed; deferring to LLM router")
            return None
        if not qvec:
            return None
        nq = _norm(qvec)
        if nq is None:
            return None
        scored = [
            (intent, max(_dot(nq, row) for row in rows))
            for intent, rows in self._index.items()
        ]
        scored.sort(key=lambda t: t[1], reverse=True)
        top_intent, top_score = scored[0]
        second = scored[1][1] if len(scored) > 1 else 0.0
        if (
            top_intent in self._zero_arg
            and top_score >= self._min_score
            and (top_score - second) >= self._margin
        ):
            logger.info(
                "intent fast-path hit intent=%s score=%.3f margin=%.3f",
                top_intent,
                top_score,
                top_score - second,
            )
            return TelegramNaturalLanguageIntent(
                intent=top_intent, confidence=float(top_score)
            )
        return None


def build_intent_fast_path(settings, embedder=None) -> EmbeddingIntentRouter | None:
    if embedder is None:
        from .kb_embedder import build_kb_embedder

        embedder = build_kb_embedder(settings)
    if embedder is None:
        logger.info("intent fast-path disabled (no embedder configured)")
        return None
    try:
        phrasings = json.loads(_PHRASINGS_PATH.read_text(encoding="utf-8"))
    except OSError:
        logger.warning("intent fast-path phrasings unavailable path=%s", _PHRASINGS_PATH)
        return None
    min_score = float(
        getattr(settings, "openclaw_intent_fastpath_min_score", _DEFAULT_MIN_SCORE)
    )
    router = EmbeddingIntentRouter(embedder, phrasings, min_score=min_score)
    if not router.ready:
        logger.warning("intent fast-path built but index empty; disabling")
        return None
    logger.info(
        "intent fast-path ready intents=%d min_score=%.2f",
        len(router._index),
        min_score,
    )
    return router
