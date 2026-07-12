"""Provider-routing primitives for the web command bridge (#74 R1.2).

Owns the pieces of provider selection that are independent of CommandBridge
state: model-attempt status vocabulary, the Gemini HTTP client and its error
classification, the shared cloud-pool failover walk, and sticky-provider chain
reordering. ``command_bridge.py`` re-exports these names so existing consumers
keep importing from ``openclaw_adapter.command_bridge``.
"""

from __future__ import annotations

import json
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

from .command_bridge_models import ModelAttempt, _extract_gemini_text

_MODEL_STATUS_OK = "ok"
_MODEL_STATUS_ERROR = "error"
_MODEL_STATUS_NOT_CONFIGURED = "not_configured"
_MODEL_STATUS_QUOTA_EXHAUSTED = "quota_exhausted"
_MODEL_STATUS_RATE_LIMITED = "rate_limited"
_GEMINI_API_BASE = "https://generativelanguage.googleapis.com/v1beta"


class _GeminiRequestError(RuntimeError):
    def __init__(self, message: str, *, status: str = _MODEL_STATUS_ERROR) -> None:
        super().__init__(message)
        self.status = status


class _GeminiTextClient:
    """Minimal Google Gemini generateContent client for web chat."""

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        timeout_seconds: int,
        ssl_context: object | None = None,
    ) -> None:
        self.api_key = api_key
        self.model = model
        self.timeout_seconds = max(1, timeout_seconds)
        self.ssl_context = ssl_context

    def generate(self, prompt: str, *, temperature: float = 0.7) -> str:
        payload = {
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": temperature},
        }
        url = (
            f"{_GEMINI_API_BASE}/models/{quote(self.model, safe='')}:generateContent"
            f"?key={quote(self.api_key, safe='')}"
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
        try:
            with urlopen(request, timeout=self.timeout_seconds, context=self.ssl_context) as response:
                body = response.read().decode("utf-8", errors="replace")
        except HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", errors="replace")[:800]
            except Exception:
                detail = ""
            raise _GeminiRequestError(
                f"Gemini HTTP {exc.code}: {detail}",
                status=_gemini_http_status(exc.code, detail),
            ) from exc
        except URLError as exc:
            raise _GeminiRequestError(
                f"Gemini request failed: {exc.reason}", status=_MODEL_STATUS_ERROR
            ) from exc
        try:
            data = json.loads(body)
        except ValueError as exc:
            raise _GeminiRequestError(
                "Gemini returned invalid JSON", status=_MODEL_STATUS_ERROR
            ) from exc
        text = _extract_gemini_text(data)
        if not text:
            raise _GeminiRequestError("Gemini returned no text", status=_MODEL_STATUS_ERROR)
        return text


def _gemini_http_status(code: int, detail: str) -> str:
    lowered = detail.lower()
    if code == 429 or "resource_exhausted" in lowered or "quota" in lowered:
        return _MODEL_STATUS_QUOTA_EXHAUSTED
    if code == 403 and ("rate" in lowered or "quota" in lowered):
        return _MODEL_STATUS_RATE_LIMITED
    return _MODEL_STATUS_ERROR


def _is_gemini_fallback_status(status: str) -> bool:
    return status in {_MODEL_STATUS_QUOTA_EXHAUSTED, _MODEL_STATUS_RATE_LIMITED}


def _walk_cloud_pool_chain(
    chain: list[tuple[str, str, object, object]],
    prompt: str,
    *,
    temperature: float,
) -> tuple[str | None, str | None, str | None, tuple[ModelAttempt, ...]]:
    """Try each ``(provider, model, build_fn, configured_fn)`` entry in order,
    first success wins. Shared by every cloud-pool call site (chat-tool plan,
    result judge, blocking chat, llm_transform) so they fail over identically;
    ``chain`` may already be rotated by a ``CloudPoolRotation`` before this
    runs — rotation only changes the starting point, not this walk logic.

    Returns ``(text, final_provider, final_model, attempts)``; ``text`` is
    ``None`` if every entry in ``chain`` was skipped or failed.
    """
    attempts: list[ModelAttempt] = []
    for provider, model_name, build_fn, configured_fn in chain:
        if not configured_fn():
            attempts.append(ModelAttempt(
                provider, model_name, _MODEL_STATUS_NOT_CONFIGURED,
                f"{provider} not configured",
            ))
            continue
        client = build_fn(model_name) if provider == "gemini" else build_fn()
        if client is None:
            attempts.append(ModelAttempt(
                provider, model_name, _MODEL_STATUS_NOT_CONFIGURED,
                f"{provider} unavailable",
            ))
            continue
        try:
            text = client.generate(prompt, temperature=temperature)
        except _GeminiRequestError as exc:
            attempts.append(ModelAttempt(provider, model_name, exc.status, str(exc)))
            continue
        except Exception as exc:  # noqa: BLE001
            attempts.append(ModelAttempt(provider, model_name, _MODEL_STATUS_ERROR, str(exc)))
            continue
        attempts.append(ModelAttempt(provider, model_name, _MODEL_STATUS_OK))
        return text, provider, model_name, tuple(attempts)
    return None, None, None, tuple(attempts)


def _pin_provider_chain(
    chain: list[tuple[str, str, object, object]],
    pinned: str | None,
) -> list[tuple[str, str, object, object]]:
    """Reorder ``chain`` so the entry whose provider label matches ``pinned``
    is first, preserving the relative order of everything else. No-op if
    ``pinned`` is None or not present in ``chain`` (e.g. the operator removed
    that provider from the pool since the pin was recorded)."""
    if pinned is None:
        return chain
    for i, entry in enumerate(chain):
        if entry[0] == pinned:
            return [entry, *chain[:i], *chain[i + 1:]]
    return chain
