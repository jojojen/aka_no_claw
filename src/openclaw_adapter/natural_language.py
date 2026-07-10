"""Settings bridge: builds a TelegramNaturalLanguageRouter from AssistantSettings."""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

from assistant_runtime import AssistantSettings, build_ssl_context
from telegram_nl.natural_language import (  # noqa: F401
    TelegramNaturalLanguageIntent,
    TelegramNaturalLanguageRouter,
    build_telegram_natural_language_router,
    fallback_route_telegram_natural_language,
    _load_json_fragment,
    _normalize_intent,
)
from .llm_pool_settings import (
    LLM_PROVIDER_GEMINI,
    chat_backend_enabled,
    default_chat_backend,
    enabled_cloud_pool_providers,
    provider_enabled,
    provider_is_configured,
    resolve_provider_model,
)

logger = logging.getLogger(__name__)
_ROUTER_SPEC_PATH = Path(__file__).resolve().parents[2] / "docs" / "TELEGRAM_TOOL_SPEC.md"
_GEMINI_API_BASE = "https://generativelanguage.googleapis.com/v1beta"
_APP_ALLOWED_INTENTS = frozenset({"create_workflow", "play_music", "home_action"})
_WF_CREATE_KEYWORDS = (
    "建立 workflow", "建一個 workflow", "建立一個 workflow",
    "create a workflow", "create workflow",
    "幫我建立", "建立自動化", "自動化流程", "工作流程",
    "建立工作流", "設定 workflow",
)
_MUSIC_PLAY_KEYWORDS = (
    "放音樂", "播放音樂", "放歌", "播歌", "放一首歌", "放首歌",
    "play music", "play a song", "放我最愛", "隨機放", "放最愛",
)
_MUSIC_PLAY_RE = re.compile(
    r"(?:播放?|播|放).*(?:最愛|收藏)(?:清單|歌單|歌曲|音樂|歌)?",
    re.IGNORECASE,
)
_HOME_ON_VERBS = ("開", "打開", "turn on", "on")
_HOME_OFF_VERBS = ("關", "關掉", "關閉", "turn off", "off", "熄")
_HOME_DIM_VERBS = ("調暗", "dim", "暗一點")
_HOME_BRIGHT_VERBS = ("調亮", "bright", "brighten", "亮一點")
_HOME_DEVICE_KEYWORDS = ("電燈", "燈", "light", "lights", "照明", "燈光")
_OPENCLAW_PROMPT_SUFFIX = (
    "Additional app intents:\n"
    "Use create_workflow when the user wants to BUILD / CREATE / 建立 / 設定 a workflow / 工作流 / 自動化流程 / 例行任務.\n"
    "  Set workflow_description to the full natural-language task description the user provided (verbatim or lightly cleaned).\n"
    "  Signals: '建立 workflow', '建立一個...工作流', 'create a workflow', '幫我建立...流程', '自動化...任務'.\n"
    "Use play_music when the user wants to play / 播放 / 放 music, a song, or audio.\n"
    "  Set music_query to the song/artist name, or 'playbest' if the user wants the best/favourite track, or 'random' for random playback, or null if unspecified.\n"
    "  Signals: '放音樂', '播放音樂', '放歌', '播歌', '放一首歌', '放最愛', '隨機放音樂', 'play music', 'play a song'.\n"
    "Use home_action when the user wants to control a smart home device (lights, appliances, curtains, etc.).\n"
    "  Set home_target to the device or area (e.g. '客廳電燈', '臥室燈', '全部燈', 'all lights', '電燈'). Set home_target to null if no specific device is named.\n"
    "  Set home_command to one of: 'on', 'off', 'dim', 'bright'. Use 'on' for 開/打開/turn on; 'off' for 關/關閉/turn off/熄; 'dim' for 調暗; 'bright' for 調亮. Set null if unclear.\n"
    "  Signals: '開燈', '關燈', '打開電燈', '關掉燈', '開客廳燈', '把燈打開', 'turn on the lights', 'lights off'.\n"
    "Examples:\n"
    '- "建立一個 workflow：每天早上查東京天氣，念出來" -> create_workflow, workflow_description="每天早上查東京天氣，念出來"\n'
    '- "幫我建立自動化流程：先說早安問候，再播放最愛音樂" -> create_workflow, workflow_description="先說早安問候，再播放最愛音樂"\n'
    '- "放音樂" -> play_music, music_query=null\n'
    '- "放我最愛的音樂" -> play_music, music_query="playbest"\n'
    '- "隨機放一首" -> play_music, music_query="random"\n'
    '- "放 初音ミク の曲" -> play_music, music_query="初音ミク"\n'
    '- "開客廳燈" -> home_action, home_target="客廳燈", home_command="on"\n'
    '- "關掉臥室電燈" -> home_action, home_target="臥室電燈", home_command="off"\n'
    '- "把燈打開" -> home_action, home_target="燈", home_command="on"\n'
    '- "turn on the lights" -> home_action, home_target="lights", home_command="on"\n'
    '- "打開客廳電燈" -> home_action, home_target="客廳電燈", home_command="on"\n'
)


class _PoolAwareRouter:
    """Settings-driven NL router shared with the web chat llm pool."""

    backend = "llm-pool"

    def __init__(self, settings: AssistantSettings) -> None:
        self._settings = settings

    @property
    def descriptor(self) -> str:
        default_backend = default_chat_backend(self._settings)
        local = _build_local_router(self._settings)
        local_desc = local.descriptor if local is not None else "local:none"
        cloud_client = _build_router_cloud_client(self._settings)
        cloud_desc = getattr(cloud_client, "model", "none")
        return f"llm-pool:{default_backend}:{cloud_desc}+{local_desc}"

    @property
    def tool_spec(self) -> str:
        return _load_router_tool_spec()

    @property
    def _extra_allowed_intents(self) -> frozenset[str]:
        return _APP_ALLOWED_INTENTS

    def route(self, text: str) -> TelegramNaturalLanguageIntent | None:
        content = text.strip()
        if not content:
            return None
        local_router = _build_local_router(self._settings)
        cloud_client = _build_router_cloud_client(self._settings)
        if cloud_client is not None and local_router is not None:
            try:
                prompt = local_router._build_prompt(content)
                raw = cloud_client.generate(prompt, temperature=0.0)
                parsed = _load_json_fragment(raw)
                if not isinstance(parsed, dict):
                    raise RuntimeError(
                        f"Cloud router returned non-dict: {type(parsed).__name__}"
                    )
                return _normalize_intent(
                    parsed,
                    extra_allowed_intents=local_router._extra_allowed_intents,
                )
            except Exception as exc:
                logger.warning("Cloud NL router failed, falling back to local: %s", exc)
        if local_router is None or not provider_enabled(self._settings, "local"):
            return None
        return local_router.route(text)


def build_telegram_natural_language_router_from_settings(
    settings: AssistantSettings,
) -> TelegramNaturalLanguageRouter | _PoolAwareRouter | None:
    model = _select_router_model(settings)
    if model is None:
        return None

    backend = (settings.openclaw_local_text_backend or "").strip().lower()
    if not backend:
        return None
    if backend != "ollama":
        logger.warning("Unsupported Telegram natural-language router backend=%s", backend)
        return None
    return _PoolAwareRouter(settings)


def _select_router_model(settings: AssistantSettings) -> str | None:
    selected = (resolve_provider_model(settings, "local") or "").strip()
    if selected:
        return selected
    candidates = _split_models(settings.openclaw_local_text_model) + _split_models(
        settings.openclaw_local_vision_model
    )
    if not candidates:
        return None
    return max(candidates, key=_router_model_rank)


def _build_local_router(settings: AssistantSettings) -> TelegramNaturalLanguageRouter | None:
    model = _select_router_model(settings)
    if model is None:
        return None
    return build_telegram_natural_language_router(
        endpoint=settings.openclaw_local_text_endpoint,
        model=model,
        backend="ollama",
        timeout_seconds=max(1, settings.openclaw_local_text_timeout_seconds),
        tool_spec=_load_router_tool_spec(),
        ssl_context=build_ssl_context(settings)
        if settings.openclaw_local_text_endpoint.startswith("https://")
        else None,
        extra_prompt_suffix=_OPENCLAW_PROMPT_SUFFIX,
        extra_allowed_intents=_APP_ALLOWED_INTENTS,
    )


def _build_router_cloud_client(settings: AssistantSettings) -> object | None:
    for provider in _router_cloud_providers(settings):
        if not provider_is_configured(settings, provider):
            continue
        if provider == LLM_PROVIDER_GEMINI:
            return _GeminiRouterClient(settings, resolve_provider_model(settings, provider))
        if provider == "mistral":
            from .dynamic_tools import MistralTextClient

            return MistralTextClient(
                api_key=getattr(settings, "openclaw_mistral_api_key", None),
                model=resolve_provider_model(settings, provider),
                timeout_seconds=60,
            )
        if provider == "big_pickle":
            from .dynamic_tools import OpenCodeTextClient

            return OpenCodeTextClient(
                base_url=(getattr(settings, "openclaw_opencode_base_url", None) or "").strip(),
                model=resolve_provider_model(settings, provider),
                api_key=getattr(settings, "openclaw_opencode_api_key", None),
                timeout_seconds=60,
                max_tokens=2048,
            )
    return None


def _router_cloud_providers(settings: AssistantSettings) -> tuple[str, ...]:
    default_backend = default_chat_backend(settings)
    if default_backend == "cloud_pool":
        return enabled_cloud_pool_providers(settings)
    if default_backend == "gemini" and chat_backend_enabled(settings, default_backend):
        return ("gemini",)
    if default_backend == "cloud_mistral" and chat_backend_enabled(settings, default_backend):
        return ("mistral",)
    if default_backend == "cloud_pickle" and chat_backend_enabled(settings, default_backend):
        return ("big_pickle",)
    return ()


class _GeminiRouterClient:
    def __init__(self, settings: AssistantSettings, model: str) -> None:
        self._settings = settings
        self.model = model

    def generate(self, prompt: str, *, temperature: float = 0.0) -> str:
        payload = {
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": temperature},
        }
        key = getattr(self._settings, "openclaw_gemini_api_key", None) or ""
        url = (
            f"{_GEMINI_API_BASE}/models/{quote(self.model, safe='')}:generateContent"
            f"?key={quote(key, safe='')}"
        )
        request = Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json, application/rss+xml, application/xml, text/html, text/plain, */*",
                "User-Agent": "aka_no_claw/1.0 (+https://github.com/jojojen/aka_no_claw; personal-use bot)",
            },
            method="POST",
        )
        ssl_context = (
            build_ssl_context(self._settings)
            if getattr(self._settings, "openclaw_ca_bundle_path", None)
            or getattr(self._settings, "openclaw_tls_insecure_skip_verify", False)
            else None
        )
        try:
            with urlopen(request, timeout=60, context=ssl_context) as response:
                raw = response.read().decode("utf-8", errors="replace")
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"Gemini HTTP {exc.code}: {detail}") from exc
        except URLError as exc:
            raise RuntimeError(f"Gemini request failed: {exc.reason}") from exc
        data = _load_json_fragment(raw)
        text = _extract_gemini_text(data)
        if not text:
            raise RuntimeError("Gemini returned no text")
        return text


def _extract_gemini_text(data: object) -> str:
    if not isinstance(data, dict):
        return ""
    parts: list[str] = []
    for candidate in data.get("candidates") or []:
        if not isinstance(candidate, dict):
            continue
        content = candidate.get("content")
        if not isinstance(content, dict):
            continue
        for part in content.get("parts") or []:
            if isinstance(part, dict) and isinstance(part.get("text"), str):
                parts.append(part["text"])
    return "".join(parts).strip()


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


def _extract_home_target(text: str) -> str | None:
    stripped = text
    all_verbs = sorted(
        (*_HOME_ON_VERBS, *_HOME_OFF_VERBS, *_HOME_DIM_VERBS, *_HOME_BRIGHT_VERBS,
         "把", "幫我", "請", "全部", "the", "all"),
        key=len, reverse=True,
    )
    for kw in all_verbs:
        stripped = re.sub(re.escape(kw), " ", stripped, flags=re.IGNORECASE)
    result = " ".join(stripped.split()).strip()
    return result if result else None


def fallback_route_openclaw_natural_language(text: str) -> TelegramNaturalLanguageIntent | None:
    content = text.strip()
    if not content:
        return None
    lowered = content.lower()

    if any(kw in content for kw in _WF_CREATE_KEYWORDS):
        return TelegramNaturalLanguageIntent(
            intent="create_workflow",
            workflow_description=content,
            confidence=0.80,
        )

    if any(kw in lowered for kw in _MUSIC_PLAY_KEYWORDS) or _MUSIC_PLAY_RE.search(content):
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

    has_device = any(kw in lowered for kw in _HOME_DEVICE_KEYWORDS)
    if has_device:
        if any(kw in lowered for kw in _HOME_DIM_VERBS):
            cmd = "dim"
        elif any(kw in lowered for kw in _HOME_BRIGHT_VERBS):
            cmd = "bright"
        elif any(kw in lowered for kw in _HOME_OFF_VERBS):
            cmd = "off"
        else:
            cmd = "on"
        return TelegramNaturalLanguageIntent(
            intent="home_action",
            home_target=_extract_home_target(content),
            home_command=cmd,
            confidence=0.80,
        )
    return None
