from __future__ import annotations

from assistant_runtime.settings import AssistantSettings
from openclaw_adapter.natural_language import (
    _select_router_model,
    build_telegram_natural_language_router_from_settings,
)


def test_natural_language_router_is_disabled_without_text_backend() -> None:
    settings = AssistantSettings(
        openclaw_local_text_backend=None,
        openclaw_local_text_model="gemma3:1b",
        openclaw_local_vision_model="gemma3:1b",
    )

    assert build_telegram_natural_language_router_from_settings(settings) is None


def test_select_router_model_prefers_strongest_available_local_model() -> None:
    settings = AssistantSettings(
        openclaw_local_text_backend="ollama",
        openclaw_local_text_model="qwen3:4b",
        openclaw_local_vision_model="qwen2.5vl:7b,gemma3:12b",
    )

    assert _select_router_model(settings) == "gemma3:12b"
