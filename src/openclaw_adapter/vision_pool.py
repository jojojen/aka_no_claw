from __future__ import annotations

import base64
import io
import json
import logging
import os
import re
import time
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Protocol
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from urllib.parse import urljoin

from assistant_runtime import build_ssl_context

from .command_bridge_models import ModelAttempt
from .image_translate import encode_image_for_vision

logger = logging.getLogger(__name__)

_MAX_VISION_IMAGES = 3
_VISION_TEMPERATURE = 0.2
_GEMINI_API_BASE = "https://generativelanguage.googleapis.com/v1beta"
_MISTRAL_API_BASE = "https://api.mistral.ai/v1"
_NVIDIA_API_BASE = "https://integrate.api.nvidia.com/v1"

_OBSERVE_PROMPT = (
    "請客觀描述這張圖片中可見的內容與狀態，包括任何可見的瑕疵、損傷、文字。"
    "用繁體中文、條列、不要臆測圖片外的資訊。"
)

_MODEL_STATUS_OK = "ok"
_MODEL_STATUS_ERROR = "error"
_MODEL_STATUS_NOT_CONFIGURED = "not_configured"


class VisionClient(Protocol):
    def generate(self, prompt: str, images_b64: list[str], *, temperature: float = 0.2) -> str: ...


class _VisionRequestError(RuntimeError):
    def __init__(self, message: str, *, status: str = _MODEL_STATUS_ERROR) -> None:
        super().__init__(message)
        self.status = status


class GeminiVisionClient:
    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        timeout_seconds: int = 180,
        ssl_context: object | None = None,
    ) -> None:
        self.api_key = api_key
        self.model = model
        self.timeout_seconds = max(1, timeout_seconds)
        self.ssl_context = ssl_context

    def generate(self, prompt: str, images_b64: list[str], *, temperature: float = _VISION_TEMPERATURE) -> str:
        parts: list[dict[str, object]] = [{"text": prompt}]
        for b64 in images_b64:
            parts.append({
                "inline_data": {"mime_type": "image/jpeg", "data": b64},
            })
        payload = {
            "contents": [{"role": "user", "parts": parts}],
            "generationConfig": {"temperature": temperature},
        }
        url = (
            f"{_GEMINI_API_BASE}/models/{self.model}:generateContent"
            f"?key={self.api_key}"
        )
        from urllib.parse import quote
        url = (
            f"{_GEMINI_API_BASE}/models/{quote(self.model, safe='')}:generateContent"
            f"?key={quote(self.api_key, safe='')}"
        )
        request = Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            method="POST",
        )
        deadline = time.monotonic() + self.timeout_seconds
        try:
            with urlopen(request, timeout=self.timeout_seconds, context=self.ssl_context) as response:
                body = response.read().decode("utf-8", errors="replace")
        except HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", errors="replace")[:800]
            except Exception:
                detail = ""
            status = _MODEL_STATUS_ERROR
            if exc.code == 429:
                status = "rate_limited"
            elif "resource_exhausted" in detail.lower() or "quota" in detail.lower():
                status = "quota_exhausted"
            raise _VisionRequestError(
                f"Gemini vision HTTP {exc.code}: {detail}", status=status,
            ) from exc
        except URLError as exc:
            raise _VisionRequestError(
                f"Gemini vision request failed: {exc.reason}", status=_MODEL_STATUS_ERROR,
            ) from exc
        try:
            data = json.loads(body)
        except ValueError as exc:
            raise _VisionRequestError(
                "Gemini vision returned invalid JSON", status=_MODEL_STATUS_ERROR,
            ) from exc
        text = _extract_gemini_vision_text(data)
        if not text:
            raise _VisionRequestError("Gemini vision returned no text", status=_MODEL_STATUS_ERROR)
        return text

    def _enforce_deadline(self, deadline: float) -> None:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise _VisionRequestError("Gemini vision deadline exceeded", status=_MODEL_STATUS_ERROR)


def _extract_gemini_vision_text(data: object) -> str:
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


class MistralVisionClient:
    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        timeout_seconds: int = 180,
    ) -> None:
        self.api_key = api_key
        self.model = model
        self.timeout_seconds = max(1, timeout_seconds)

    def generate(self, prompt: str, images_b64: list[str], *, temperature: float = _VISION_TEMPERATURE) -> str:
        content: list[dict[str, object]] = [{"type": "text", "text": prompt}]
        for b64 in images_b64:
            content.append({
                "type": "image_url",
                "image_url": {"url": f"data:image/jpeg;base64,{b64}"},
            })
        payload = {
            "model": self.model,
            "messages": [{"role": "user", "content": content}],
            "temperature": temperature,
            "stream": False,
        }
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Authorization": f"Bearer {self.api_key}",
        }
        url = f"{_MISTRAL_API_BASE}/chat/completions"
        request = Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                body = response.read().decode("utf-8", errors="replace")
        except HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", errors="replace")[:400]
            except Exception:
                detail = ""
            raise _VisionRequestError(
                f"Mistral vision HTTP {exc.code}: {detail}", status=_MODEL_STATUS_ERROR,
            ) from exc
        except URLError as exc:
            raise _VisionRequestError(
                f"Mistral vision request failed: {exc.reason}", status=_MODEL_STATUS_ERROR,
            ) from exc
        parsed = json.loads(body)
        choices = parsed.get("choices")
        if not isinstance(choices, list) or not choices:
            raise _VisionRequestError("Mistral vision response missing choices", status=_MODEL_STATUS_ERROR)
        text = (choices[0].get("message") or {}).get("content") or ""
        if not isinstance(text, str):
            raise _VisionRequestError("Mistral vision response content is not text", status=_MODEL_STATUS_ERROR)
        return text.strip()


class NvidiaVisionClient:
    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        timeout_seconds: int = 180,
    ) -> None:
        self.api_key = api_key
        self.model = model
        self.timeout_seconds = max(1, timeout_seconds)

    def generate(self, prompt: str, images_b64: list[str], *, temperature: float = _VISION_TEMPERATURE) -> str:
        content: list[dict[str, object]] = [{"type": "text", "text": prompt}]
        for b64 in images_b64:
            content.append({
                "type": "image_url",
                "image_url": {"url": f"data:image/jpeg;base64,{b64}"},
            })
        payload = {
            "model": self.model,
            "messages": [{"role": "user", "content": content}],
            "temperature": temperature,
            "stream": False,
        }
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Authorization": f"Bearer {self.api_key}",
        }
        url = f"{_NVIDIA_API_BASE}/chat/completions"
        request = Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                body = response.read().decode("utf-8", errors="replace")
        except HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", errors="replace")[:400]
            except Exception:
                detail = ""
            raise _VisionRequestError(
                f"NVIDIA vision HTTP {exc.code}: {detail}", status=_MODEL_STATUS_ERROR,
            ) from exc
        except URLError as exc:
            raise _VisionRequestError(
                f"NVIDIA vision request failed: {exc.reason}", status=_MODEL_STATUS_ERROR,
            ) from exc
        parsed = json.loads(body)
        choices = parsed.get("choices")
        if not isinstance(choices, list) or not choices:
            raise _VisionRequestError("NVIDIA vision response missing choices", status=_MODEL_STATUS_ERROR)
        text = (choices[0].get("message") or {}).get("content") or ""
        if not isinstance(text, str):
            raise _VisionRequestError("NVIDIA vision response content is not text", status=_MODEL_STATUS_ERROR)
        return text.strip()


class LocalVisionClient:
    def __init__(
        self,
        *,
        endpoint: str,
        model: str,
        timeout_seconds: int = 180,
        ssl_context: object | None = None,
    ) -> None:
        self.endpoint = endpoint.rstrip("/")
        self.model = model
        self.timeout_seconds = max(1, timeout_seconds)
        self.ssl_context = ssl_context

    def generate(self, prompt: str, images_b64: list[str], *, temperature: float = _VISION_TEMPERATURE) -> str:
        payload = {
            "model": self.model,
            "prompt": prompt,
            "images": images_b64,
            "stream": False,
            "think": False,
            "options": {"temperature": temperature},
        }
        request = Request(
            f"{self.endpoint}/api/generate",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            method="POST",
        )
        try:
            with urlopen(request, timeout=self.timeout_seconds, context=self.ssl_context) as response:
                raw = response.read().decode("utf-8", errors="replace")
        except HTTPError as exc:
            raise _VisionRequestError(f"Local vision HTTP {exc.code}.") from exc
        except URLError as exc:
            raise _VisionRequestError(f"Local vision request failed: {exc.reason}") from exc
        body = json.loads(raw)
        result = body.get("response", "")
        if not isinstance(result, str):
            raise _VisionRequestError(f"Local vision response type was {type(result).__name__}.")
        return result.strip()


def encode_image_bytes_for_vision(data: bytes, *, max_side: int = 1280) -> str:
    try:
        from PIL import Image
    except ImportError:
        return base64.b64encode(data).decode("ascii")
    buffer = io.BytesIO(data)
    try:
        with Image.open(buffer) as im:
            im = im.convert("RGB")
            width, height = im.size
            longest = max(width, height)
            if longest > max_side:
                scale = max_side / longest
                im = im.resize(
                    (max(1, round(width * scale)), max(1, round(height * scale))),
                    Image.LANCZOS,
                )
            out = io.BytesIO()
            im.save(out, format="JPEG", quality=90)
        return base64.b64encode(out.getvalue()).decode("ascii")
    except Exception:
        return base64.b64encode(data).decode("ascii")


def build_vision_pool_chain(
    settings,
) -> list[tuple[str, str, Callable[[], VisionClient | None], Callable[[], bool]]]:
    """Build vision provider chain from settings.

    Returns ordered list of (provider_label, model_name, build_fn, is_configured_fn)
    tuples. Each build_fn returns a VisionClient or None; is_configured_fn checks
    if the provider is available (credentials + settings).

    Providers are filtered to enabled_vision_pool_providers(settings) and skipped
    if they have no vision client (e.g. big_pickle is text-only).
    """
    from assistant_runtime import build_ssl_context

    from .llm_pool_settings import (
        LLM_PROVIDER_GEMINI,
        LLM_PROVIDER_LOCAL,
        LLM_PROVIDER_MISTRAL,
        LLM_PROVIDER_NVIDIA,
        enabled_vision_pool_providers,
        resolve_vision_provider_model,
    )

    def _build_gemini_vision_client(model: str) -> GeminiVisionClient | None:
        key = getattr(settings, "openclaw_gemini_api_key", None)
        if not key:
            return None
        ssl_ctx = build_ssl_context(settings)
        return GeminiVisionClient(api_key=key, model=model, timeout_seconds=180, ssl_context=ssl_ctx)

    def _build_mistral_vision_client(model: str) -> MistralVisionClient | None:
        key = getattr(settings, "openclaw_mistral_api_key", None)
        if not key:
            return None
        return MistralVisionClient(api_key=key, model=model, timeout_seconds=180)

    def _build_nvidia_vision_client(model: str) -> NvidiaVisionClient | None:
        key = getattr(settings, "openclaw_nvidia_api_key", None)
        if not key:
            return None
        return NvidiaVisionClient(api_key=key, model=model, timeout_seconds=180)

    def _build_local_vision_client(model: str) -> LocalVisionClient | None:
        backend = (getattr(settings, "openclaw_local_vision_backend", None) or "").strip().lower()
        if backend != "ollama":
            return None
        endpoint = getattr(settings, "openclaw_local_vision_endpoint", None)
        if not endpoint:
            return None
        ssl_ctx = build_ssl_context(settings) if endpoint.startswith("https://") else None
        timeout = max(1, getattr(settings, "openclaw_local_vision_timeout_seconds", 180))
        return LocalVisionClient(endpoint=endpoint, model=model, timeout_seconds=timeout, ssl_context=ssl_ctx)

    raw_entries: dict[str, tuple[str, str, Callable[[], VisionClient | None], Callable[[], bool]]] = {
        LLM_PROVIDER_GEMINI: (
            "gemini",
            resolve_vision_provider_model(settings, LLM_PROVIDER_GEMINI),
            lambda: _build_gemini_vision_client(
                resolve_vision_provider_model(settings, LLM_PROVIDER_GEMINI)
            ),
            lambda: bool(getattr(settings, "openclaw_gemini_api_key", None)),
        ),
        LLM_PROVIDER_MISTRAL: (
            "mistral",
            resolve_vision_provider_model(settings, LLM_PROVIDER_MISTRAL),
            lambda: _build_mistral_vision_client(
                resolve_vision_provider_model(settings, LLM_PROVIDER_MISTRAL)
            ),
            lambda: bool(getattr(settings, "openclaw_mistral_api_key", None)),
        ),
        LLM_PROVIDER_NVIDIA: (
            "nvidia",
            resolve_vision_provider_model(settings, LLM_PROVIDER_NVIDIA),
            lambda: _build_nvidia_vision_client(
                resolve_vision_provider_model(settings, LLM_PROVIDER_NVIDIA)
            ),
            lambda: bool(getattr(settings, "openclaw_nvidia_api_key", None)),
        ),
        LLM_PROVIDER_LOCAL: (
            "local",
            resolve_vision_provider_model(settings, LLM_PROVIDER_LOCAL),
            lambda: _build_local_vision_client(
                resolve_vision_provider_model(settings, LLM_PROVIDER_LOCAL)
            ),
            lambda: (getattr(settings, "openclaw_local_vision_backend", None) or "").strip().lower()
            == "ollama",
        ),
    }
    # Settings may enable providers with no vision client (e.g. big_pickle);
    # skip them instead of crashing the whole chain with a KeyError.
    return [
        raw_entries[provider]
        for provider in enabled_vision_pool_providers(settings)
        if provider in raw_entries
    ]


def walk_vision_pool_chain(
    chain: list[tuple[str, str, Callable[[], VisionClient | None], Callable[[], bool]]],
    prompt: str,
    images_b64: list[str],
    *,
    temperature: float = _VISION_TEMPERATURE,
) -> tuple[str | None, str | None, str | None, tuple[ModelAttempt, ...]]:
    attempts: list[ModelAttempt] = []
    for provider, model_name, build_fn, configured_fn in chain:
        if not configured_fn():
            attempts.append(ModelAttempt(
                provider, model_name, _MODEL_STATUS_NOT_CONFIGURED,
                f"{provider} not configured",
            ))
            continue
        client = build_fn()
        if client is None:
            attempts.append(ModelAttempt(
                provider, model_name, _MODEL_STATUS_NOT_CONFIGURED,
                f"{provider} unavailable",
            ))
            continue
        try:
            text = client.generate(prompt, images_b64, temperature=temperature)
        except _VisionRequestError as exc:
            attempts.append(ModelAttempt(provider, model_name, exc.status, str(exc)))
            continue
        except Exception as exc:
            attempts.append(ModelAttempt(provider, model_name, _MODEL_STATUS_ERROR, str(exc)))
            continue
        attempts.append(ModelAttempt(provider, model_name, _MODEL_STATUS_OK))
        return text, provider, model_name, tuple(attempts)
    return None, None, None, tuple(attempts)


# --- WP-6: URL image acquisition ---

_STRUCTURAL_IMAGE_EXTRACTORS: list[tuple[re.Pattern, str]] = [
    (re.compile(r'<meta\s+[^>]*property\s*=\s*["\']og:image["\'][^>]*content\s*=\s*["\']([^"\']+)["\']', re.IGNORECASE), "og:image"),
    (re.compile(r'<meta\s+[^>]*name\s*=\s*["\']twitter:image["\'][^>]*content\s*=\s*["\']([^"\']+)["\']', re.IGNORECASE), "twitter:image"),
    (re.compile(r'<meta\s+[^>]*property\s*=\s*["\']og:image:url["\'][^>]*content\s*=\s*["\']([^"\']+)["\']', re.IGNORECASE), "og:image:url"),
]
_IMG_TAG_RE = re.compile(r'<img[^>]+src\s*=\s*["\']([^"\']+)["\']', re.IGNORECASE)

_MAX_FETCH_IMAGES = 3
_MAX_IMAGE_BYTES = 5 * 1024 * 1024
_MAX_FETCH_WALL_SECONDS = 20


def fetch_page_image_urls(url: str, *, timeout_seconds: int = 15) -> list[str]:
    deadline = time.monotonic() + _MAX_FETCH_WALL_SECONDS
    request = Request(url, headers={
        "User-Agent": "Mozilla/5.0 (compatible; aka_no_claw/1.0; +https://github.com/jojojen/aka_no_claw)",
        "Accept": "text/html,application/xhtml+xml",
    })
    try:
        with urlopen(request, timeout=timeout_seconds) as response:
            html = response.read().decode("utf-8", errors="replace")
    except (HTTPError, URLError, OSError) as exc:
        logger.warning("vision: failed to fetch page %s: %s", url, exc)
        return []

    candidates: list[str] = []
    seen_urls: set[str] = set()

    for pattern, _name in _STRUCTURAL_IMAGE_EXTRACTORS:
        for match in pattern.finditer(html):
            raw = match.group(1).strip()
            if raw and raw not in seen_urls:
                seen_urls.add(raw)
                absolute = urljoin(url, raw)
                candidates.append(absolute)

    for match in _IMG_TAG_RE.finditer(html):
        raw = match.group(1).strip()
        if raw and raw not in seen_urls:
            seen_urls.add(raw)
            absolute = urljoin(url, raw)
            candidates.append(absolute)

    if time.monotonic() > deadline:
        logger.warning("vision: image extraction deadline exceeded for %s", url)
        return candidates[:_MAX_FETCH_IMAGES]

    return candidates[:_MAX_FETCH_IMAGES]


def acquire_url_images(urls: list[str], *, max_images: int = _MAX_FETCH_IMAGES) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    deadline = time.monotonic() + _MAX_FETCH_WALL_SECONDS
    for url in urls:
        if len(out) >= max_images:
            break
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            request = Request(url, headers={
                "User-Agent": "Mozilla/5.0 (compatible; aka_no_claw/1.0; +https://github.com/jojojen/aka_no_claw)",
            })
            with urlopen(request, timeout=max(1, remaining)) as response:
                content_type = response.headers.get("Content-Type", "")
                if not content_type.startswith("image/"):
                    logger.debug("vision: skip non-image %s (content-type=%s)", url, content_type)
                    continue
                content_length = response.headers.get("Content-Length")
                if content_length and int(content_length) > _MAX_IMAGE_BYTES:
                    logger.debug("vision: skip oversized image %s (%s bytes)", url, content_length)
                    continue
                data = response.read(_MAX_IMAGE_BYTES + 1)
                if len(data) > _MAX_IMAGE_BYTES:
                    logger.debug("vision: skip oversized image %s (>%d bytes)", url, _MAX_IMAGE_BYTES)
                    continue
                b64 = encode_image_bytes_for_vision(data)
                out.append((url, b64))
        except (HTTPError, URLError, OSError) as exc:
            logger.debug("vision: failed to fetch image %s: %s", url, exc)
            continue
    return out
