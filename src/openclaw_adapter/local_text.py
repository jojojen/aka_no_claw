"""Local-text-model glue: WebFetch-style answering and translate handlers.

Moved out of telegram_bot.py in R2.2 (#75). These are the Ollama-backed text
helpers (`/fetch`, `/translateja`, `/translatezh`) built on top of the local
text endpoint. telegram_bot re-imports these names so legacy import paths and
`_build_registries` registration sites are unchanged. chat_web imports
`build_translate_handler` from telegram_bot, which still resolves via the
re-export.
"""

from __future__ import annotations

import json
import logging
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from assistant_runtime import AssistantSettings, build_ssl_context

from .llm_pool_settings import (
    _LLM_NOT_CONFIGURED_MESSAGE,
    _TRANSLATE_NOT_CONFIGURED_MESSAGE,
    _select_text_generation_model,
)
from .web_search import (
    answer_page_with_ollama,
    build_web_fetch_answer,
    fetch_page_text,
    format_web_research_answer,
)

logger = logging.getLogger(__name__)


def default_web_fetch_renderer(settings: AssistantSettings) -> "Callable[[str, str], str]":
    """Item 3: WebFetch equivalent — read one URL and answer a focused prompt."""
    backend = (settings.openclaw_local_text_backend or "").strip().lower()
    endpoint = settings.openclaw_local_text_endpoint
    model = _select_text_generation_model(settings)
    timeout = max(1, settings.openclaw_local_text_timeout_seconds)
    ssl_ctx = build_ssl_context(settings) if endpoint.startswith("https://") else None

    def render(url: str, prompt: str) -> str:
        if backend != "ollama" or not endpoint or not model:
            return _LLM_NOT_CONFIGURED_MESSAGE
        answer = build_web_fetch_answer(
            url,
            prompt,
            fetch_page_fn=lambda u: fetch_page_text(
                u,
                ssl_context=ssl_ctx,
                enable_browser_fallback=True,
            ),
            answer_fn=lambda u, p, content: answer_page_with_ollama(
                u,
                p,
                content,
                endpoint=endpoint,
                model=model,
                timeout_seconds=timeout,
                ssl_context=ssl_ctx,
            ),
        )
        return format_web_research_answer(answer)

    return render


def _call_local_text_model(
    *,
    endpoint: str,
    model: str,
    prompt: str,
    timeout_seconds: int,
    ssl_context,
) -> str:
    request_payload = {
        "model": model,
        "prompt": prompt,
        "stream": False,
        "think": False,
        "options": {"temperature": 0.2},
    }
    request = Request(
        f"{endpoint.rstrip('/')}/api/generate",
        data=json.dumps(request_payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=timeout_seconds, context=ssl_context) as response:
            raw = response.read().decode("utf-8", errors="replace")
    except HTTPError as exc:
        raise RuntimeError(f"翻譯 LLM HTTP {exc.code}.") from exc
    except URLError as exc:
        raise RuntimeError(f"翻譯 LLM request failed: {exc.reason}") from exc
    payload = json.loads(raw)
    result = payload.get("response", "")
    if not isinstance(result, str):
        raise RuntimeError(f"翻譯 LLM response type was {type(result).__name__}.")
    return result.strip()


def build_translate_handler(settings: AssistantSettings, *, target: str) -> Callable[[str, str], str]:
    backend = (settings.openclaw_local_text_backend or "").strip().lower()
    endpoint = settings.openclaw_local_text_endpoint
    model = _select_text_generation_model(settings)
    timeout = max(1, settings.openclaw_local_text_timeout_seconds)
    ssl_ctx = build_ssl_context(settings) if endpoint.startswith("https://") else None
    target = target.strip().lower()
    if target == "ja":
        usage = "用法：/translateja <要翻成日文的文字>"
        instruction = (
            "將下列文字翻譯成自然、通順的日文。"
            "只輸出譯文，不要解說，不要加引號，不要加前綴。"
            "保留 URL、專有名詞、產品名；必要時只做最自然的日文化。"
        )
    else:
        usage = "用法：/translatezh <要翻成繁體中文的文字>"
        instruction = (
            "將下列文字翻譯成自然、通順的繁體中文（台灣用語）。"
            "只輸出譯文，不要解說，不要加引號，不要加前綴。"
            "保留 URL、專有名詞、產品名。"
        )

    def _local_translate(prompt: str) -> str:
        if backend != "ollama" or not endpoint or not model:
            return _TRANSLATE_NOT_CONFIGURED_MESSAGE
        translated = _call_local_text_model(
            endpoint=endpoint,
            model=model,
            prompt=prompt,
            timeout_seconds=timeout,
            ssl_context=ssl_ctx,
        ).strip()
        return translated or "本地模型沒有回傳可用譯文。"

    def handler(remainder: str, chat_id: str) -> str:
        text = (remainder or "").strip()
        if not text:
            return usage
        prompt = f"{instruction}\n\n原文：\n{text}\n\n譯文："
        # Default to the cloud pool (Gemini→Mistral→Big Pickle→NVIDIA), matching
        # the web console. generate_via_cloud_pool already falls back to local
        # when every cloud provider is unavailable; the try/except guards the
        # case where it raises (local also disabled) or a cloud call blows up
        # mid-flight, so a translation never silently drops.
        try:
            from .command_bridge_providers import generate_via_cloud_pool

            translated = (generate_via_cloud_pool(settings, prompt) or "").strip()
            if translated:
                return translated
        except Exception:
            logger.warning("translate cloud pool failed; local fallback", exc_info=True)
        return _local_translate(prompt)

    return handler
