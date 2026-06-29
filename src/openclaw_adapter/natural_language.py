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
    _load_json_fragment,
    _normalize_intent,
)

logger = logging.getLogger(__name__)
_ROUTER_SPEC_PATH = Path(__file__).resolve().parents[2] / "docs" / "TELEGRAM_TOOL_SPEC.md"


class _CloudFirstRouter:
    """Cloud-big-pickle first NL router; falls back to local ollama on failure."""

    backend = "cloud-first"

    def __init__(
        self,
        local_router: TelegramNaturalLanguageRouter,
        cloud_client: object,
    ) -> None:
        self._local = local_router
        self._cloud = cloud_client

    @property
    def descriptor(self) -> str:
        return f"cloud-first:{getattr(self._cloud, 'model', '?')}+{self._local.descriptor}"

    @property
    def tool_spec(self) -> str:
        return self._local.tool_spec

    def route(self, text: str) -> TelegramNaturalLanguageIntent | None:
        content = text.strip()
        if not content:
            return None
        try:
            prompt = self._local._build_prompt(content)
            raw = self._cloud.generate(prompt, temperature=0.0)
            parsed = _load_json_fragment(raw)
            if not isinstance(parsed, dict):
                raise RuntimeError(f"Cloud router returned non-dict: {type(parsed).__name__}")
            return _normalize_intent(parsed)
        except Exception as exc:
            logger.warning("Cloud NL router failed, falling back to local: %s", exc)
            return self._local.route(text)


def build_telegram_natural_language_router_from_settings(
    settings: AssistantSettings,
) -> TelegramNaturalLanguageRouter | _CloudFirstRouter | None:
    model = _select_router_model(settings)
    if model is None:
        return None

    backend = (settings.openclaw_local_text_backend or "").strip().lower()
    if not backend:
        return None
    if backend != "ollama":
        logger.warning("Unsupported Telegram natural-language router backend=%s", backend)
        return None

    local_router = build_telegram_natural_language_router(
        endpoint=settings.openclaw_local_text_endpoint,
        model=model,
        backend=backend,
        timeout_seconds=max(1, settings.openclaw_local_text_timeout_seconds),
        tool_spec=_load_router_tool_spec(),
        ssl_context=build_ssl_context(settings) if settings.openclaw_local_text_endpoint.startswith("https://") else None,
    )

    cloud_client = _build_cloud_router_client(settings)
    if cloud_client is not None and local_router is not None:
        return _CloudFirstRouter(local_router, cloud_client)
    return local_router


def _build_cloud_router_client(settings: AssistantSettings) -> object | None:
    from .dynamic_tools import OpenCodeTextClient
    base_url = (getattr(settings, "openclaw_opencode_base_url", None) or "").strip()
    if not base_url:
        return None
    raw_model = (getattr(settings, "openclaw_opencode_model", None) or "big-pickle").strip()
    model = raw_model.split("/")[-1] if "/" in raw_model else raw_model
    return OpenCodeTextClient(
        base_url=base_url,
        model=model,
        api_key=getattr(settings, "openclaw_opencode_api_key", None),
        timeout_seconds=60,
        max_tokens=2048,
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
