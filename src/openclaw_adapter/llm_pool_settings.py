from __future__ import annotations

import json
import logging
import os
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from assistant_runtime import AssistantSettings

from .command_bridge_models import (
    CHAT_BACKEND_CLOUD_MISTRAL,
    CHAT_BACKEND_CLOUD_PICKLE,
    CHAT_BACKEND_CLOUD_POOL,
    CHAT_BACKEND_GEMINI,
    CHAT_BACKEND_LOCAL,
)

logger = logging.getLogger(__name__)

LLM_PROVIDER_GEMINI = "gemini"
LLM_PROVIDER_MISTRAL = "mistral"
LLM_PROVIDER_BIG_PICKLE = "big_pickle"
LLM_PROVIDER_LOCAL = "local"

_DEFAULT_CHAT_BACKEND = CHAT_BACKEND_CLOUD_POOL
_DEFAULT_CLOUD_POOL = (
    LLM_PROVIDER_GEMINI,
    LLM_PROVIDER_MISTRAL,
    LLM_PROVIDER_BIG_PICKLE,
)
_ALL_PROVIDERS = (
    LLM_PROVIDER_GEMINI,
    LLM_PROVIDER_MISTRAL,
    LLM_PROVIDER_BIG_PICKLE,
    LLM_PROVIDER_LOCAL,
)
_KNOWN_CHAT_BACKENDS = frozenset(
    {
        CHAT_BACKEND_CLOUD_POOL,
        CHAT_BACKEND_LOCAL,
        CHAT_BACKEND_CLOUD_MISTRAL,
        CHAT_BACKEND_GEMINI,
        CHAT_BACKEND_CLOUD_PICKLE,
    }
)
_BACKEND_TO_PROVIDER = {
    CHAT_BACKEND_LOCAL: LLM_PROVIDER_LOCAL,
    CHAT_BACKEND_CLOUD_MISTRAL: LLM_PROVIDER_MISTRAL,
    CHAT_BACKEND_GEMINI: LLM_PROVIDER_GEMINI,
    CHAT_BACKEND_CLOUD_PICKLE: LLM_PROVIDER_BIG_PICKLE,
}
_PROVIDER_TO_BACKEND = {
    LLM_PROVIDER_LOCAL: CHAT_BACKEND_LOCAL,
    LLM_PROVIDER_MISTRAL: CHAT_BACKEND_CLOUD_MISTRAL,
    LLM_PROVIDER_GEMINI: CHAT_BACKEND_GEMINI,
    LLM_PROVIDER_BIG_PICKLE: CHAT_BACKEND_CLOUD_PICKLE,
}
_PROVIDER_LABELS = {
    LLM_PROVIDER_GEMINI: "Gemini",
    LLM_PROVIDER_MISTRAL: "Mistral",
    LLM_PROVIDER_BIG_PICKLE: "OpenCode",
    LLM_PROVIDER_LOCAL: "本地",
}
_DEFAULT_PROVIDER_OPTIONS = (
    {"value": CHAT_BACKEND_CLOUD_POOL, "label": "雲端池"},
    {"value": CHAT_BACKEND_GEMINI, "label": "Gemini"},
    {"value": CHAT_BACKEND_CLOUD_MISTRAL, "label": "Mistral"},
    {"value": CHAT_BACKEND_CLOUD_PICKLE, "label": "OpenCode"},
    {"value": CHAT_BACKEND_LOCAL, "label": "本地"},
)
_GEMINI_RECOMMENDED_MODELS = (
    "gemini-3.5-flash",
    "gemini-3.1-flash-lite",
    "gemini-2.5-pro",
    "gemini-2.5-flash",
    "gemini-2.5-flash-lite",
)
_MISTRAL_RECOMMENDED_MODELS = (
    "mistral-small-latest",
    "mistral-medium-latest",
    "mistral-large-latest",
)
_OPENCODE_RECOMMENDED_MODELS = (
    "big-pickle",
    "deepseek-v4-flash-free",
    "mimo-v2.5-free",
    "north-mini-code-free",
    "nemotron-3-ultra-free",
)


class ChatLlmPoolWriteError(RuntimeError):
    """Raised when persisting llm_pool.json fails."""


class CloudPoolRotation:
    """Rotates which cloud-pool provider a chain-walk starts from.

    One goal-loop run shares a single instance across every LLM call it makes
    (draft, each replan, the result judge, each llm_transform step). Without
    rotation every call re-tries provider[0] first, so one long multi-step task
    hammers a single provider's rate limit instead of spreading load across the
    pool; the existing per-call fail-and-fall-through behavior is unchanged,
    only the starting point advances between calls.
    """

    def __init__(self) -> None:
        self._cursor = 0

    def rotate(self, items: Sequence) -> list:
        """Return ``items`` reordered to start at the current cursor (wrapping
        around) and advance the cursor for the next call."""
        size = len(items)
        if size == 0:
            return []
        order = [items[(self._cursor + i) % size] for i in range(size)]
        self._cursor = (self._cursor + 1) % size
        return order


@dataclass(frozen=True, slots=True)
class ProviderSettings:
    enabled: bool
    model: str


@dataclass(frozen=True, slots=True)
class ChatLlmPoolSettings:
    default_chat_provider: str
    cloud_pool: tuple[str, ...]
    providers: dict[str, ProviderSettings]

    def to_dict(self) -> dict[str, object]:
        return {
            "default_chat_provider": self.default_chat_provider,
            "cloud_pool": list(self.cloud_pool),
            "providers": {
                provider: {"enabled": cfg.enabled, "model": cfg.model}
                for provider, cfg in self.providers.items()
            },
        }


def load_chat_llm_pool_settings(settings: AssistantSettings) -> ChatLlmPoolSettings:
    defaults = default_chat_llm_pool_settings(settings)
    path = _config_path(settings)
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return defaults
    except OSError as exc:
        logger.warning("llm pool: unreadable config %s: %s", path, exc)
        return defaults
    try:
        payload = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        logger.warning("llm pool: invalid JSON at %s; using defaults", path)
        return defaults
    return normalize_chat_llm_pool_settings(settings, payload)


def save_chat_llm_pool_settings(
    settings: AssistantSettings,
    payload: object,
) -> ChatLlmPoolSettings:
    normalized = normalize_chat_llm_pool_settings(settings, payload)
    path = _config_path(settings)
    body = json.dumps(normalized.to_dict(), ensure_ascii=False, indent=2).encode("utf-8")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            "wb", dir=path.parent, delete=False, suffix=".tmp"
        ) as tmp:
            tmp.write(body)
            tmp_path = Path(tmp.name)
        os.replace(tmp_path, path)
    except OSError as exc:
        logger.exception("llm pool: write failed path=%s", path)
        raise ChatLlmPoolWriteError(str(exc)) from exc
    return normalized


def normalize_chat_llm_pool_settings(
    settings: AssistantSettings,
    payload: object,
) -> ChatLlmPoolSettings:
    defaults = default_chat_llm_pool_settings(settings)
    data = payload if isinstance(payload, dict) else {}
    raw_default = data.get("default_chat_provider")
    default_chat_provider = (
        str(raw_default)
        if isinstance(raw_default, str) and raw_default in _KNOWN_CHAT_BACKENDS
        else defaults.default_chat_provider
    )
    cloud_pool = _normalize_cloud_pool(data.get("cloud_pool"), defaults.cloud_pool)
    raw_providers = data.get("providers") if isinstance(data.get("providers"), dict) else {}
    providers: dict[str, ProviderSettings] = {}
    for provider in _ALL_PROVIDERS:
        base = defaults.providers[provider]
        raw_cfg = raw_providers.get(provider) if isinstance(raw_providers, dict) else None
        enabled = base.enabled
        model = base.model
        if isinstance(raw_cfg, dict):
            raw_enabled = raw_cfg.get("enabled")
            if isinstance(raw_enabled, bool):
                enabled = raw_enabled
            raw_model = raw_cfg.get("model")
            if isinstance(raw_model, str) and raw_model.strip():
                if provider == LLM_PROVIDER_BIG_PICKLE and raw_model.strip().lower() == "auto":
                    model = _default_opencode_model(settings)
                else:
                    model = _normalize_model_choice(provider, raw_model)
        providers[provider] = ProviderSettings(enabled=enabled, model=model)
    default_provider = _BACKEND_TO_PROVIDER.get(default_chat_provider)
    if default_provider is not None and not providers[default_provider].enabled:
        providers[default_provider] = ProviderSettings(
            enabled=True,
            model=providers[default_provider].model,
        )
    return ChatLlmPoolSettings(
        default_chat_provider=default_chat_provider,
        cloud_pool=cloud_pool,
        providers=providers,
    )


def default_chat_llm_pool_settings(settings: AssistantSettings) -> ChatLlmPoolSettings:
    local_model = _first_model(getattr(settings, "openclaw_local_text_model", None)) or "qwen3:14b"
    gemini_model = (
        getattr(settings, "openclaw_gemini_primary_model", None) or "gemini-2.5-flash"
    ).strip()
    mistral_model = (
        getattr(settings, "openclaw_mistral_model", None) or "mistral-large-latest"
    ).strip()
    return ChatLlmPoolSettings(
        default_chat_provider=_DEFAULT_CHAT_BACKEND,
        cloud_pool=_DEFAULT_CLOUD_POOL,
        providers={
            LLM_PROVIDER_GEMINI: ProviderSettings(enabled=True, model=gemini_model),
            LLM_PROVIDER_MISTRAL: ProviderSettings(enabled=True, model=mistral_model),
            LLM_PROVIDER_BIG_PICKLE: ProviderSettings(enabled=True, model=_default_opencode_model(settings)),
            LLM_PROVIDER_LOCAL: ProviderSettings(enabled=True, model=local_model),
        },
    )


def chat_llm_pool_payload(settings: AssistantSettings) -> dict[str, object]:
    effective = load_chat_llm_pool_settings(settings)
    return {
        "default_chat_provider": effective.default_chat_provider,
        "cloud_pool": list(effective.cloud_pool),
        "default_provider_options": list(_DEFAULT_PROVIDER_OPTIONS),
        "providers": {
            provider: {
                "label": _PROVIDER_LABELS[provider],
                "enabled": cfg.enabled,
                "model": cfg.model,
                "configured": provider_is_configured(settings, provider),
            }
            for provider, cfg in effective.providers.items()
        },
        "model_options": {
            provider: list(model_options_for_provider(settings, provider, effective.providers[provider].model))
            for provider in _ALL_PROVIDERS
        },
    }


def provider_settings(settings: AssistantSettings, provider: str) -> ProviderSettings:
    return load_chat_llm_pool_settings(settings).providers[provider]


def provider_enabled(settings: AssistantSettings, provider: str) -> bool:
    return provider_settings(settings, provider).enabled


def provider_is_configured(settings: AssistantSettings, provider: str) -> bool:
    if provider == LLM_PROVIDER_GEMINI:
        return bool(getattr(settings, "openclaw_gemini_api_key", None))
    if provider == LLM_PROVIDER_MISTRAL:
        return bool(getattr(settings, "openclaw_mistral_api_key", None))
    return True


def resolve_provider_model(settings: AssistantSettings, provider: str) -> str:
    cfg = provider_settings(settings, provider)
    if provider == LLM_PROVIDER_BIG_PICKLE:
        return _normalize_model_choice(provider, cfg.model) or _default_opencode_model(settings)
    if provider == LLM_PROVIDER_LOCAL:
        return _first_model(cfg.model) or "qwen3:14b"
    return cfg.model.strip()


def cloud_pool_order(settings: AssistantSettings) -> tuple[str, ...]:
    return load_chat_llm_pool_settings(settings).cloud_pool


def enabled_cloud_pool_providers(settings: AssistantSettings) -> tuple[str, ...]:
    cfg = load_chat_llm_pool_settings(settings)
    return tuple(provider for provider in cfg.cloud_pool if cfg.providers[provider].enabled)


def default_chat_backend(settings: AssistantSettings) -> str:
    return load_chat_llm_pool_settings(settings).default_chat_provider


def model_options_for_provider(
    settings: AssistantSettings,
    provider: str,
    current_model: str,
) -> tuple[str, ...]:
    options: list[str] = []
    if provider == LLM_PROVIDER_GEMINI:
        options.extend(
            [
                current_model,
                getattr(settings, "openclaw_gemini_primary_model", None) or "",
                getattr(settings, "openclaw_gemini_flash_model", None) or "",
                *_GEMINI_RECOMMENDED_MODELS,
            ]
        )
    elif provider == LLM_PROVIDER_MISTRAL:
        options.extend(
            [
                current_model,
                getattr(settings, "openclaw_mistral_model", None) or "",
                *_MISTRAL_RECOMMENDED_MODELS,
            ]
        )
    elif provider == LLM_PROVIDER_BIG_PICKLE:
        options.extend(
            [
                current_model,
                _default_opencode_model(settings),
                *_OPENCODE_RECOMMENDED_MODELS,
            ]
        )
    elif provider == LLM_PROVIDER_LOCAL:
        options.extend(
            [
                current_model,
                _first_model(getattr(settings, "openclaw_local_text_model", None)) or "",
                "qwen3:4b",
                "qwen3:14b",
                "qwen2.5-coder:7b",
                "gemma3:4b",
            ]
        )
    seen: set[str] = set()
    out: list[str] = []
    for raw in options:
        value = _normalize_model_choice(provider, raw)
        if not value or value in seen:
            continue
        seen.add(value)
        out.append(value)
    return tuple(out)


def chat_backend_enabled(settings: AssistantSettings, chat_backend: str) -> bool:
    if chat_backend == CHAT_BACKEND_CLOUD_POOL:
        pool = enabled_cloud_pool_providers(settings)
        return bool(pool) or provider_enabled(settings, LLM_PROVIDER_LOCAL)
    provider = _BACKEND_TO_PROVIDER.get(chat_backend)
    if provider is None:
        return True
    return provider_enabled(settings, provider)


def chat_backend_configured(settings: AssistantSettings, chat_backend: str) -> bool:
    if not chat_backend_enabled(settings, chat_backend):
        return False
    if chat_backend == CHAT_BACKEND_CLOUD_POOL:
        for provider in enabled_cloud_pool_providers(settings):
            if provider_is_configured(settings, provider):
                return True
        return provider_enabled(settings, LLM_PROVIDER_LOCAL)
    provider = _BACKEND_TO_PROVIDER.get(chat_backend)
    if provider is None:
        return True
    return provider_is_configured(settings, provider)


def provider_for_chat_backend(chat_backend: str) -> str | None:
    return _BACKEND_TO_PROVIDER.get(chat_backend)


def backend_for_provider(provider: str) -> str:
    return _PROVIDER_TO_BACKEND[provider]


def _normalize_cloud_pool(raw: object, default_order: tuple[str, ...]) -> tuple[str, ...]:
    if not isinstance(raw, list):
        return default_order
    seen: set[str] = set()
    out: list[str] = []
    for item in raw:
        if not isinstance(item, str):
            continue
        provider = item.strip().lower()
        if provider not in _DEFAULT_CLOUD_POOL or provider in seen:
            continue
        seen.add(provider)
        out.append(provider)
    for provider in default_order:
        if provider not in seen:
            out.append(provider)
    return tuple(out)


def _normalize_model_choice(provider: str, raw: str) -> str:
    text = raw.strip()
    if not text:
        return ""
    if provider == LLM_PROVIDER_BIG_PICKLE:
        if text.lower() == "auto":
            return "big-pickle"
        return text.split("/")[-1]
    if provider == LLM_PROVIDER_LOCAL:
        return _first_model(text) or ""
    return text


def _first_model(raw_models: str | None) -> str | None:
    if not raw_models:
        return None
    return next((part.strip() for part in raw_models.split(",") if part.strip()), None)


def _default_opencode_model(settings: AssistantSettings) -> str:
    raw = (getattr(settings, "openclaw_opencode_model", None) or "big-pickle").strip()
    normalized = _normalize_model_choice(LLM_PROVIDER_BIG_PICKLE, raw)
    return normalized or "big-pickle"


def _config_path(settings: AssistantSettings) -> Path:
    return Path(
        getattr(settings, "openclaw_llm_pool_config_path", "config/llm_pool.json")
    )
