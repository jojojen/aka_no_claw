"""SNS / 4chan demand-catalyst evidence gate (issue #8, Deliverable 3).

Turns a raw SNS / 4chan post into a ``CollectibleSignal`` *only when the post
names a concrete collectible product anchor*. Generic hype / chatter produces no
signal (it contributes heat elsewhere, not product intelligence).

Per Rule G the recognition is **LLM-driven** — there is deliberately no keyword
table here. The model decides whether a post is collectible-related, whether it
names a concrete anchor (a specific set / product / code / release, not vague
"new stuff coming"), and the closed-vocabulary classification (domain / entity /
product type). The output enums are then snapped to the protocol vocabularies in
``collectible_signal`` so a hallucinated token degrades to ``other`` rather than
corrupting the store.

A concrete anchor that lacks official-store / marketplace validation is recorded
as ``informational`` evidence — never promoted to a recommendation here. The
promotion decision belongs to the market-valuation gate (Deliverable 5).
"""
from __future__ import annotations

import json
import logging
import re

from .collectible_signal import (
    ANCHOR_SNS_CATALYST,
    CollectibleSignal,
    make_signal,
    normalize_domain,
    normalize_entity_kind,
    normalize_collectible_product_type,
)

logger = logging.getLogger(__name__)

DEFAULT_MIN_CONFIDENCE: float = 0.6

_JSON_FRAGMENT_RE = re.compile(r"\{.*\}", re.DOTALL)


def _safe_json_loads(raw: str):
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        match = _JSON_FRAGMENT_RE.search(raw)
        if not match:
            return None
        try:
            return json.loads(match.group(0))
        except (ValueError, TypeError):
            return None


def _as_float(value: object) -> float:
    try:
        return float(value) if value is not None else 0.0
    except (TypeError, ValueError):
        return 0.0


def classify_sns_post(
    *,
    text: str,
    source_kind: str = "sns",
    source_url: str = "",
    llm_fn,
    min_confidence: float = DEFAULT_MIN_CONFIDENCE,
) -> CollectibleSignal | None:
    """Classify one SNS / 4chan post into a CollectibleSignal, or None.

    Returns ``None`` when the post is not collectible-related, the model is not
    confident, or it names no concrete product anchor (generic chatter → heat
    only). Otherwise returns an ``informational`` signal carrying the concrete
    anchor — evidence, not a recommendation.

    Parameters
    ----------
    text : str
        The raw post text.
    source_kind : str
        Origin of the post; normalized to SIGNAL_SOURCE_KINDS ("sns" / "fourchan").
    llm_fn : callable(prompt) -> str
        Returns the model's JSON response for the single classification probe.
    """
    if not text or not text.strip():
        return None
    try:
        raw = llm_fn(_build_prompt(text, source_kind))
    except Exception:
        logger.exception("classify_sns_post: LLM probe failed")
        return None
    verdict = _safe_json_loads(raw) or {}

    if not verdict.get("is_collectible"):
        return None
    confidence = _as_float(verdict.get("confidence"))
    if confidence < min_confidence:
        return None
    # No concrete anchor → demand chatter only; contributes heat, not evidence.
    if not verdict.get("has_concrete_anchor"):
        return None

    ip_canonical = str(verdict.get("ip_canonical") or "").strip()
    if not ip_canonical:
        return None

    urls = (source_url,) if source_url else ()
    return make_signal(
        source_kind=source_kind,
        collectible_domain=normalize_domain(verdict.get("collectible_domain")),
        ip_canonical=ip_canonical,
        title=str(verdict.get("title") or "").strip(),
        entity_kind=normalize_entity_kind(verdict.get("entity_kind")),
        product_type=normalize_collectible_product_type(verdict.get("product_type")),
        source_urls=urls,
        confidence=confidence,
        evidence_count=1,
        # Concrete anchor but no official-store / market validation yet → stored
        # as intelligence/evidence, never auto-recommended from here.
        actionability="informational",
        heat_score=_as_float(verdict.get("heat_score")),
        anchor_types=(ANCHOR_SNS_CATALYST,),
        metadata={"gate_reason": str(verdict.get("reason") or "")},
    )


def _build_prompt(text: str, source_kind: str) -> str:
    return (
        "你在從一則社群貼文中萃取「具體的收藏品產品線索」，用於收藏品投資情報。\n"
        "目標是分辨：這則貼文有沒有指向一個*具體的產品*（特定彈／套組／編號／發售品項），\n"
        "還是只是泛泛的期待 / 炫耀 / 閒聊 / 對戰心得（這些沒有具體產品線索）。\n\n"
        f"來源：{source_kind}\n"
        f"貼文內容：\n{text}\n\n"
        "請判斷並嚴格回 JSON：\n"
        "- is_collectible (bool)：是否與收藏品（卡牌 / CD / 色紙 / 壓克力 / 公仔 / 周邊等）相關。\n"
        "- confidence (0-1)：你對上面判斷的信心，不確定就給低分。\n"
        "- has_concrete_anchor (bool)：是否指向*具體產品*（特定彈/套組/編號/品項），\n"
        "  純期待、純炫耀、純閒聊一律 false。\n"
        "- collectible_domain：tcg / music / goods / figure / book / event_merch / other。\n"
        "- entity_kind：character / group / event / artist / set / campaign / store / other。\n"
        "- product_type：single_card / booster_pack / sealed_box / starter_deck / promo /\n"
        "  shikishi / cd / blu_ray / acrylic_stand / figure / plush / book / tapestry / badge / other。\n"
        "- ip_canonical：作品 / IP / 系列正式名稱（例：Project SEKAI、鬼滅の刃）。無法判定回空字串。\n"
        "- title：具體品項名稱（若有）。\n"
        "- heat_score (0-1)：這則貼文反映的需求熱度。\n"
        "- reason：一句話說明 has_concrete_anchor 的依據。\n\n"
        '{"is_collectible": true, "confidence": 0.0, "has_concrete_anchor": false, '
        '"collectible_domain": "other", "entity_kind": "other", "product_type": "other", '
        '"ip_canonical": "", "title": "", "heat_score": 0.0, "reason": ""}\n'
        "不確定的欄位寧可保守（低分 / other / false）。"
    )
