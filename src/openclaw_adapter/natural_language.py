"""Thin wrapper + settings bridge for price_monitor_bot.natural_language."""

from __future__ import annotations

import logging

from assistant_runtime import AssistantSettings, build_ssl_context
from price_monitor_bot.natural_language import (  # noqa: F401
    TelegramNaturalLanguageIntent,
    TelegramNaturalLanguageRouter,
    build_telegram_natural_language_router,
    fallback_route_telegram_natural_language,
)

logger = logging.getLogger(__name__)


def build_telegram_natural_language_router_from_settings(
    settings: AssistantSettings,
) -> TelegramNaturalLanguageRouter | None:
    model = _select_router_model(settings)
    if model is None:
        return None

    backend = (settings.openclaw_local_text_backend or "ollama").strip().lower()
    if backend != "ollama":
        logger.warning("Unsupported Telegram natural-language router backend=%s", backend)
        return None

    return build_telegram_natural_language_router(
        endpoint=settings.openclaw_local_text_endpoint,
        model=model,
        backend=backend,
        timeout_seconds=max(1, settings.openclaw_local_text_timeout_seconds),
        ssl_context=build_ssl_context(settings) if settings.openclaw_local_text_endpoint.startswith("https://") else None,
    )


def _select_router_model(settings: AssistantSettings) -> str | None:
    if settings.openclaw_local_text_model:
        return settings.openclaw_local_text_model

    candidates = [part.strip() for part in (settings.openclaw_local_vision_model or "").split(",") if part.strip()]
    if not candidates:
        return None
    for candidate in candidates:
        if "gemma" in candidate.lower():
            return candidate
    return candidates[0]
