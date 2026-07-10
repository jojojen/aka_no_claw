"""Item condition assessment via vision LLM.

Analyzes product images to assess physical condition and compare against
seller-claimed condition labels.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Protocol

from .vision_pool import acquire_url_images, walk_vision_pool_chain

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ConditionAssessment:
    """Result of vision-LLM physical condition assessment."""

    summary: str  # 1-3 sentence 繁體中文 condition summary
    flaws: tuple[str, ...]  # visible flaws, () if none
    consistency: str  # "consistent" | "mismatch" | "unknown"
    image_count: int
    provider: str | None = None
    model: str | None = None


class ConditionAssessor(Protocol):
    """Callable that assesses item condition from title, claimed label, and image URLs."""

    def __call__(
        self,
        title: str,
        condition_label: str | None,
        image_urls: Sequence[str],
    ) -> ConditionAssessment | None: ...


# Vision prompt for condition assessment. Strict JSON output only;
# models may wrap it in prose/code fences — we extract the first {...} block.
_CONDITION_ASSESSMENT_PROMPT = (
    "請根據圖片分析這件商品的物理狀況。\n\n"
    "商品名稱：{title}\n"
    "賣家標示狀況：{claimed_condition}\n\n"
    "請客觀描述圖片中可見的所有物理狀況（例如刮痕、破損、磨損、髒汙、包裝與封膜狀態等），"
    "並判斷與賣家標示狀況的相符程度。只描述圖片中確實可見的，不要臆測。\n\n"
    "回應必須為 STRICT JSON 格式（不含任何其他文字），只含以下欄位：\n"
    '{{"summary": "1-3 句繁體中文對狀況的總結", "flaws": ["可見瑕疵1", "可見瑕疵2"], "consistency": "consistent"|"mismatch"|"unknown"}}\n\n'
    "consistency 的定義：\n"
    '- "consistent"：圖片狀況與賣家標示相符\n'
    '- "mismatch"：圖片狀況與賣家標示不相符（狀況較差）\n'
    '- "unknown"：無法判斷或圖片中不清楚'
)


def build_item_condition_assessor(
    settings,
    *,
    acquire_images_fn: Callable[[list[str]], list[tuple[str, str]]] | None = None,
    chain_fn: Callable[[], list[tuple[str, str, Callable, Callable]]] | None = None,
) -> ConditionAssessor:
    """Build a callable that assesses item condition from images.

    Args:
        settings: AssistantSettings for vision provider configuration.
        acquire_images_fn: Callable to download images. Defaults to acquire_url_images.
        chain_fn: Callable to build vision provider chain. Defaults to build_vision_pool_chain.

    Returns:
        A callable (title, condition_label, image_urls) -> ConditionAssessment | None
    """
    if acquire_images_fn is None:
        acquire_images_fn = acquire_url_images
    if chain_fn is None:
        from .vision_pool import build_vision_pool_chain

        chain_fn = lambda: build_vision_pool_chain(settings)

    def assess(
        title: str,
        condition_label: str | None,
        image_urls: Sequence[str],
    ) -> ConditionAssessment | None:
        """Assess item condition from images."""
        # No images: skip
        if not image_urls:
            return None

        # Download images (up to 3)
        try:
            images_b64 = acquire_images_fn(list(image_urls), max_images=3)
        except Exception as exc:
            logger.warning("item_condition: acquire_images failed: %s", exc)
            images_b64 = []

        # Zero downloads: no assessment possible
        if not images_b64:
            return None

        # Build prompt with actual title and condition
        claimed = condition_label or "未提供"
        prompt = _CONDITION_ASSESSMENT_PROMPT.format(title=title, claimed_condition=claimed)

        # Build chain and call vision pool
        try:
            chain = chain_fn()
        except Exception as exc:
            logger.warning("item_condition: build_vision_pool_chain failed: %s", exc)
            return None

        images_for_vision = [b64 for _, b64 in images_b64]
        text, provider, model, _attempts = walk_vision_pool_chain(
            chain, prompt, images_for_vision, temperature=0.2
        )

        # All providers failed
        if text is None:
            logger.debug("item_condition: no vision provider answered")
            return None

        # Parse JSON from response
        assessment = _parse_condition_assessment_json(text)
        if assessment is None:
            # Fallback: raw text trimmed
            summary = text[:400].strip() if text else "無法解析視覺分析結果"
            assessment = ConditionAssessment(
                summary=summary,
                flaws=(),
                consistency="unknown",
                image_count=len(images_b64),
                provider=provider,
                model=model,
            )
        else:
            # Augment with provider/model info
            assessment = ConditionAssessment(
                summary=assessment.summary,
                flaws=assessment.flaws,
                consistency=assessment.consistency,
                image_count=len(images_b64),
                provider=provider,
                model=model,
            )

        return assessment

    return assess


def _parse_condition_assessment_json(text: str) -> ConditionAssessment | None:
    """Extract and parse JSON block from vision response.

    Vision models may wrap JSON in markdown code fences or prose.
    Extracts the first {...} block via non-greedy regex.
    """
    if not text:
        return None

    # Non-greedy match of {...}
    match = re.search(r"\{.*?\}", text, re.DOTALL)
    if not match:
        return None

    json_str = match.group(0)
    try:
        data = json.loads(json_str)
    except json.JSONDecodeError:
        logger.debug("item_condition: failed to parse JSON: %s", json_str[:200])
        return None

    if not isinstance(data, dict):
        return None

    summary = data.get("summary", "")
    if not isinstance(summary, str):
        summary = str(summary)

    flaws_raw = data.get("flaws", [])
    if isinstance(flaws_raw, list):
        flaws = tuple(str(f) for f in flaws_raw if f)
    else:
        flaws = ()

    consistency = data.get("consistency", "unknown")
    if not isinstance(consistency, str):
        consistency = "unknown"
    if consistency not in ("consistent", "mismatch", "unknown"):
        consistency = "unknown"

    return ConditionAssessment(
        summary=summary,
        flaws=flaws,
        consistency=consistency,
        image_count=0,  # Set by caller
        provider=None,
        model=None,
    )
