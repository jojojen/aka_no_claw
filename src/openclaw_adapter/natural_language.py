"""Thin wrapper + settings bridge for price_monitor_bot.natural_language.

Extends the base price_monitor_bot router with aka_no_claw-specific intents:
  create_workflow — draft a workflow from natural language
  play_music      — play music via the local music handler
These intents are NOT in the base price_monitor_bot schema; they are injected
via TelegramNaturalLanguageRouter's extension mechanism.
"""

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

# ── aka_no_claw-specific NL intent extension ──────────────────────────────────

_AKA_EXTRA_INTENTS: frozenset[str] = frozenset({"create_workflow", "play_music"})

_AKA_EXTRA_SCHEMA_PROPERTIES: dict = {
    "workflow_description": {"type": ["string", "null"]},
    "music_query": {"type": ["string", "null"]},
}
_AKA_EXTRA_SCHEMA_REQUIRED: list[str] = ["workflow_description", "music_query"]

_AKA_EXTRA_PROMPT_SUFFIX = (
    "Additional intents for this assistant (extend the allowed list above):\n"
    "  create_workflow, play_music\n"
    "Use create_workflow when the user wants to BUILD / CREATE / 建立 / 設定 a "
    "workflow / 工作流 / 自動化流程 / 例行任務.\n"
    "  Set workflow_description to the full natural-language task description "
    "the user provided (verbatim or lightly cleaned).\n"
    "  Signals: '建立 workflow', '建立一個...工作流', 'create a workflow', "
    "'幫我建立...流程', '自動化...任務'.\n"
    "Use play_music when the user wants to play / 播放 / 放 music, a song, or audio.\n"
    "  Set music_query to the search query (e.g. the song/artist name), or "
    "'playbest' if the user wants the best/favourite track, or 'random' for "
    "random playback, or null if unspecified.\n"
    "  Signals: '放音樂', '播放音樂', '放歌', '播歌', '放一首歌', '放最愛', "
    "'隨機放音樂', 'play music', 'play a song'.\n"
    'Examples:\n'
    '- "建立一個 workflow：每天早上查東京天氣，念出來" -> create_workflow, '
    'workflow_description="每天早上查東京天氣，念出來"\n'
    '- "幫我建立自動化流程：先說早安問候，再播放最愛音樂" -> create_workflow, '
    'workflow_description="先說早安問候，再播放最愛音樂"\n'
    '- "放音樂" -> play_music, music_query=null\n'
    '- "放我最愛的音樂" -> play_music, music_query="playbest"\n'
    '- "隨機放一首" -> play_music, music_query="random"\n'
    '- "放 初音ミク の曲" -> play_music, music_query="初音ミク"\n'
)


def _aka_fallback_route(text: str) -> TelegramNaturalLanguageIntent | None:
    """Keyword-based fallback for aka-specific intents (create_workflow, play_music).
    Called after the base fallback_route_telegram_natural_language returns None."""
    content = text.strip()
    lowered = content.lower()

    _WF_CREATE_KEYWORDS = (
        "建立 workflow", "建一個 workflow", "建立一個 workflow",
        "create a workflow", "create workflow",
        "幫我建立", "建立自動化", "自動化流程", "工作流程",
        "建立工作流", "設定 workflow",
    )
    if any(kw in content for kw in _WF_CREATE_KEYWORDS):
        return TelegramNaturalLanguageIntent(
            intent="create_workflow",
            workflow_description=content,
            confidence=0.80,
        )

    _MUSIC_PLAY_KEYWORDS = (
        "放音樂", "播放音樂", "放歌", "播歌", "放一首歌", "放首歌",
        "play music", "play a song", "放我最愛", "隨機放", "放最愛",
    )
    if any(kw in lowered for kw in _MUSIC_PLAY_KEYWORDS):
        if "最愛" in content or "playbest" in lowered or "best" in lowered:
            music_q: str | None = "playbest"
        elif "隨機" in content or "random" in lowered:
            music_q = "random"
        else:
            music_q = None
        return TelegramNaturalLanguageIntent(
            intent="play_music",
            music_query=music_q,
            confidence=0.85,
        )

    return None

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

    @property
    def _extra_allowed_intents(self) -> frozenset[str]:
        return self._local._extra_allowed_intents

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
            return _normalize_intent(parsed, extra_allowed_intents=_AKA_EXTRA_INTENTS)
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
        extra_schema_properties=_AKA_EXTRA_SCHEMA_PROPERTIES,
        extra_schema_required=_AKA_EXTRA_SCHEMA_REQUIRED,
        extra_prompt_suffix=_AKA_EXTRA_PROMPT_SUFFIX,
        extra_allowed_intents=_AKA_EXTRA_INTENTS,
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
