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
import re
from pathlib import Path

from telegram_nl.natural_language import TelegramNaturalLanguageIntent

from .embedding_match import cosine, embed_unit_vectors, l2_normalize

logger = logging.getLogger(__name__)

# Commands whose handler needs NO extracted parameters. Only these may be
# short-circuited; every other intent carries slots the LLM must extract.
ZERO_ARG_INTENTS = frozenset(
    {"help", "status", "tools", "scan_help", "sns_list", "list_watches"}
)

# Intents whose only slot — a Mercari product URL — is mechanically extractable
# by regex, so they can be fast-pathed too: embed the URL-stripped residual
# (the verb: 研究 vs 信譽) to pick which one, and lift the URL out by pattern.
# A bare URL (no verb residual) stays None → the LLM router asks the user which.
URL_SLOT_INTENTS = frozenset({"product_research", "reputation_snapshot"})

# Intents where the full user utterance IS the slot value. The fast-path
# fires on a confident match and sets workflow_description=text so the caller
# (telegram_bot.py, the only caller of this module — see _build_intent_fast_path)
# can route straight to create_workflow/create_schedule and skip the slow LLM
# router entirely. Web Chat does NOT use this module at all: it intentionally
# lets the selected chat-tool-plan model choose __create_workflow__ vs __goal__
# itself (command_bridge.py's CHAT_TOOL_CREATE_WORKFLOW), so cloud-model
# capability stays visible/testable instead of being masked by an embedding
# shortcut.
FULL_TEXT_INTENTS = frozenset({"create_workflow", "create_schedule"})

_MERCARI_URL_RE = re.compile(
    r"https?://(?:jp\.|www\.)?mercari\.com/"
    r"(?:item/m\d+|shops/product/[A-Za-z0-9]+)(?:[/?#]\S*)?",
    re.IGNORECASE,
)
_ANY_URL_RE = re.compile(r"https?://\S+")


def _strip_urls(text: str) -> str:
    return _ANY_URL_RE.sub(" ", text).strip()

# Margin the winning intent must beat the runner-up by. Guards the close
# zero-arg pair sns_list vs list_watches: a genuinely ambiguous "看清單" falls
# through to the LLM (which has the SNS/Mercari disambiguation rules) instead of
# a confident wrong pick.
_DEFAULT_MARGIN = 0.03
_DEFAULT_MIN_SCORE = 0.65

_PHRASINGS_PATH = (
    Path(__file__).resolve().parents[2] / "data" / "intent_routing_phrasings.json"
)


class EmbeddingIntentRouter:
    def __init__(
        self,
        embedder,
        phrasings: dict[str, list[str]],
        *,
        min_score: float = _DEFAULT_MIN_SCORE,
        margin: float = _DEFAULT_MARGIN,
        zero_arg_intents=ZERO_ARG_INTENTS,
        url_slot_intents=URL_SLOT_INTENTS,
        full_text_intents=FULL_TEXT_INTENTS,
    ) -> None:
        self._embedder = embedder
        self._min_score = min_score
        self._margin = margin
        self._zero_arg = frozenset(zero_arg_intents)
        self._url_slot = frozenset(url_slot_intents)
        self._full_text = frozenset(full_text_intents)
        self._index: dict[str, list[list[float]]] = {}
        # URLs carry no intent signal (and the URL-slot path matches a
        # URL-stripped residual), so strip them before embedding the phrasings.
        for intent, examples in phrasings.items():
            rows = embed_unit_vectors(embedder, [_strip_urls(ex) for ex in examples])
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
        nq = l2_normalize(qvec)
        if nq is None:
            return None
        scored = [
            (intent, max(cosine(nq, row) for row in rows))
            for intent, rows in self._index.items()
        ]
        scored.sort(key=lambda t: t[1], reverse=True)
        top_intent, top_score = scored[0]
        second = scored[1][1] if len(scored) > 1 else 0.0
        if top_score >= self._min_score and (top_score - second) >= self._margin:
            if top_intent in self._zero_arg:
                logger.info(
                    "intent fast-path hit intent=%s score=%.3f margin=%.3f",
                    top_intent,
                    top_score,
                    top_score - second,
                )
                return TelegramNaturalLanguageIntent(
                    intent=top_intent, confidence=float(top_score)
                )
            if top_intent in self._full_text:
                logger.info(
                    "intent fast-path full-text hit intent=%s score=%.3f margin=%.3f",
                    top_intent,
                    top_score,
                    top_score - second,
                )
                return TelegramNaturalLanguageIntent(
                    intent=top_intent,
                    workflow_description=text,
                    confidence=float(top_score),
                )

        url_intent = self._route_url_slot(text)
        if url_intent is not None:
            return url_intent
        return None

    def _route_url_slot(
        self, text: str
    ) -> TelegramNaturalLanguageIntent | None:
        """Fast-path verb + Mercari-URL messages (研究/信譽 <url>).

        The URL slot is lifted by regex; the verb residual picks the intent via
        embedding. A bare URL leaves no residual → None, so the LLM router keeps
        asking the user whether they meant research vs reputation."""
        url_match = _MERCARI_URL_RE.search(text)
        if url_match is None:
            return None
        residual = _strip_urls(text)
        if not residual:
            return None
        try:
            rvec = self._embedder(residual)
        except Exception:  # noqa: BLE001 - embed outage must not break routing
            return None
        rn = l2_normalize(rvec) if rvec else None
        if rn is None:
            return None
        slot = [
            (intent, max(cosine(rn, row) for row in self._index[intent]))
            for intent in self._url_slot
            if intent in self._index
        ]
        if not slot:
            return None
        slot.sort(key=lambda t: t[1], reverse=True)
        top_intent, top_score = slot[0]
        second = slot[1][1] if len(slot) > 1 else 0.0
        if top_score >= self._min_score and (top_score - second) >= self._margin:
            logger.info(
                "intent fast-path URL-slot hit intent=%s score=%.3f margin=%.3f",
                top_intent,
                top_score,
                top_score - second,
            )
            return TelegramNaturalLanguageIntent(
                intent=top_intent,
                query_url=url_match.group(0),
                confidence=float(top_score),
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
