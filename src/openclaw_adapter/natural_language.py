"""Thin wrapper + settings bridge for price_monitor_bot.natural_language."""

from __future__ import annotations

import logging
import re
from pathlib import Path

from assistant_runtime import AssistantSettings, build_ssl_context
from price_monitor_bot.natural_language import (  # noqa: F401
    TelegramNaturalLanguageIntent,
    TelegramNaturalLanguageRouter,
    build_telegram_natural_language_router,
    fallback_route_telegram_natural_language,
)

logger = logging.getLogger(__name__)
_ROUTER_SPEC_PATH = Path(__file__).resolve().parents[2] / "TELEGRAM_TOOL_SPEC.md"


def build_telegram_natural_language_router_from_settings(
    settings: AssistantSettings,
) -> TelegramNaturalLanguageRouter | None:
    model = _select_router_model(settings)
    if model is None:
        return None

    backend = (settings.openclaw_local_text_backend or "").strip().lower()
    if not backend:
        return None
    if backend != "ollama":
        logger.warning("Unsupported Telegram natural-language router backend=%s", backend)
        return None

    return build_telegram_natural_language_router(
        endpoint=settings.openclaw_local_text_endpoint,
        model=model,
        backend=backend,
        timeout_seconds=max(1, settings.openclaw_local_text_timeout_seconds),
        tool_spec=_load_router_tool_spec(),
        ssl_context=build_ssl_context(settings) if settings.openclaw_local_text_endpoint.startswith("https://") else None,
    )


def _select_router_model(settings: AssistantSettings) -> str | None:
    candidates = _split_models(settings.openclaw_local_text_model) + _split_models(settings.openclaw_local_vision_model)
    if not candidates:
        return None
    return max(candidates, key=_router_model_rank)


def _split_models(raw_models: str | None) -> tuple[str, ...]:
    if not raw_models:
        return ()
    return tuple(part.strip() for part in raw_models.split(",") if part.strip())


def _router_model_rank(model: str) -> tuple[float, int, str]:
    lowered = model.lower()
    return (
        _extract_model_size_billions(lowered),
        1 if "gemma" in lowered else 0,
        lowered,
    )


def _extract_model_size_billions(model: str) -> float:
    match = re.search(r":(\d+(?:\.\d+)?)b\b", model)
    if match is None:
        return 0.0
    try:
        return float(match.group(1))
    except ValueError:
        return 0.0


def _load_router_tool_spec() -> str:
    try:
        return _ROUTER_SPEC_PATH.read_text(encoding="utf-8").strip()
    except OSError:
        logger.warning("Telegram router tool spec is unavailable path=%s", _ROUTER_SPEC_PATH)
        return ""
