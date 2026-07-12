"""Provider-routing primitives for the web command bridge (#74 R1.2).

Owns the pieces of provider selection that are independent of CommandBridge
state: model-attempt status vocabulary, the Gemini HTTP client and its error
classification, the shared cloud-pool failover walk, and sticky-provider chain
reordering. ``command_bridge.py`` re-exports these names so existing consumers
keep importing from ``openclaw_adapter.command_bridge``.
"""

from __future__ import annotations

import json
import threading
from typing import Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

from .command_bridge_models import (
    CHAT_BACKEND_CLOUD_MISTRAL,
    CHAT_BACKEND_CLOUD_NVIDIA,
    CHAT_BACKEND_CLOUD_PICKLE,
    CHAT_BACKEND_CLOUD_POOL,
    CHAT_BACKEND_GEMINI,
    ModelAttempt,
    ModelMetadata,
    _extract_gemini_text,
)
from .llm_pool_settings import (
    LLM_PROVIDER_BIG_PICKLE,
    LLM_PROVIDER_GEMINI,
    LLM_PROVIDER_LOCAL,
    LLM_PROVIDER_MISTRAL,
    LLM_PROVIDER_NVIDIA,
    chat_backend_configured,
    enabled_cloud_pool_providers,
    provider_enabled,
    resolve_provider_model,
)

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


class ChatClientDeps(Protocol):
    """What the router needs from its host: settings plus IO client factories.

    ``CommandBridge`` satisfies this directly; tests can substitute a
    deterministic fake with canned clients."""

    settings: object

    def _build_gemini_chat_client(self, model: str) -> object | None: ...

    def _build_mistral_chat_client(self) -> object | None: ...

    def _build_cloud_chat_client(self) -> object | None: ...

    def _build_nvidia_chat_client(self) -> object | None: ...

    def _ollama_generate_blocking(self, prompt: str) -> str: ...


class ProviderRouter:
    """Owns provider selection for web chat (#74 R1.2): per-provider model
    resolution, cloud/vision pool chain construction, sticky per-conversation
    provider pins, model metadata, and the blocking cloud-pool / Gemini
    failover generation paths. Client construction stays behind
    :class:`ChatClientDeps` so instance-level monkeypatching (and deterministic
    fakes) keep working."""

    def __init__(self, deps: ChatClientDeps) -> None:
        self._deps = deps
        self._pins: dict[str, str] = {}
        self._pins_lock = threading.Lock()

    @property
    def settings(self):
        return self._deps.settings

    # --- model resolution -------------------------------------------------
    def local_model(self) -> str:
        return resolve_provider_model(self.settings, LLM_PROVIDER_LOCAL)

    def big_pickle_model(self) -> str:
        return resolve_provider_model(self.settings, LLM_PROVIDER_BIG_PICKLE)

    def mistral_model(self) -> str:
        return resolve_provider_model(self.settings, LLM_PROVIDER_MISTRAL)

    def nvidia_model(self) -> str:
        return resolve_provider_model(self.settings, LLM_PROVIDER_NVIDIA)

    def gemini_primary_model(self) -> str:
        return resolve_provider_model(self.settings, LLM_PROVIDER_GEMINI)

    def gemini_flash_model(self) -> str:
        return (
            getattr(self.settings, "openclaw_gemini_flash_model", None)
            or "gemini-2.5-flash"
        ).strip()

    def gemini_route_models(self) -> tuple[str, ...]:
        seen: set[str] = set()
        ordered: list[str] = []
        for model in (self.gemini_primary_model(), self.gemini_flash_model()):
            if model and model not in seen:
                seen.add(model)
                ordered.append(model)
        return tuple(ordered)

    # --- sticky provider pins ----------------------------------------------
    def pinned_provider(self, conversation_key: str | None) -> str | None:
        if not conversation_key:
            return None
        with self._pins_lock:
            return self._pins.get(conversation_key)

    def record_pin(self, conversation_key: str | None, provider: str) -> None:
        if not conversation_key:
            return
        with self._pins_lock:
            self._pins[conversation_key] = provider

    # --- pool chains --------------------------------------------------------
    def cloud_pool_chain(self) -> list[tuple[str, str, object, object]]:
        """Return ordered list of (provider_label, model_name, build_fn, is_configured_fn)."""
        raw_entries = {
            LLM_PROVIDER_GEMINI: (
                "gemini",
                self.gemini_primary_model(),
                self._deps._build_gemini_chat_client,
                lambda: chat_backend_configured(self.settings, CHAT_BACKEND_GEMINI),
            ),
            LLM_PROVIDER_MISTRAL: (
                "mistral",
                self.mistral_model(),
                self._deps._build_mistral_chat_client,
                lambda: chat_backend_configured(self.settings, CHAT_BACKEND_CLOUD_MISTRAL),
            ),
            LLM_PROVIDER_BIG_PICKLE: (
                "opencode",
                self.big_pickle_model(),
                self._deps._build_cloud_chat_client,
                lambda: chat_backend_configured(self.settings, CHAT_BACKEND_CLOUD_PICKLE),
            ),
            LLM_PROVIDER_NVIDIA: (
                "nvidia",
                self.nvidia_model(),
                self._deps._build_nvidia_chat_client,
                lambda: chat_backend_configured(self.settings, CHAT_BACKEND_CLOUD_NVIDIA),
            ),
        }
        return [raw_entries[provider] for provider in enabled_cloud_pool_providers(self.settings)]

    def vision_pool_chain(self):
        from .vision_pool import build_vision_pool_chain
        return build_vision_pool_chain(self.settings)

    def cloud_pool_preview(self) -> tuple[str, str]:
        """First actually usable (provider, model) for the cloud_pool tab preview.
        Checks settings only — no probing. Falls through to Big Pickle which is
        always considered configured."""
        for provider, model_name, _build_fn, configured_fn in self.cloud_pool_chain():
            if configured_fn():
                return provider, model_name
        if provider_enabled(self.settings, LLM_PROVIDER_LOCAL):
            return "local", self.local_model()
        chain = self.cloud_pool_chain()
        if chain:
            return chain[0][0], chain[0][1]
        return "local", self.local_model()

    # --- model metadata ------------------------------------------------------
    def requested_model_for_backend(self, chat_backend: str) -> tuple[str, str]:
        if chat_backend == CHAT_BACKEND_CLOUD_PICKLE:
            return "opencode", self.big_pickle_model()
        if chat_backend == CHAT_BACKEND_CLOUD_MISTRAL:
            return "mistral", self.mistral_model()
        if chat_backend == CHAT_BACKEND_CLOUD_NVIDIA:
            return "nvidia", self.nvidia_model()
        if chat_backend == CHAT_BACKEND_GEMINI:
            return "gemini", self.gemini_primary_model()
        if chat_backend == CHAT_BACKEND_CLOUD_POOL:
            return self.cloud_pool_preview()
        return "local", self.local_model()

    def model_metadata_for_backend(
        self,
        chat_backend: str,
        attempted: tuple[ModelAttempt, ...],
        final_provider: str,
        final_model: str,
        *,
        fallback_reason: str | None = None,
    ) -> ModelMetadata:
        requested_provider, requested_model = self.requested_model_for_backend(chat_backend)
        return ModelMetadata(
            requested_provider=requested_provider,
            requested_model=requested_model,
            attempted_models=attempted,
            final_provider=final_provider,
            final_model=final_model,
            fallback_reason=fallback_reason,
        )

    # --- blocking generation paths -------------------------------------------
    def generate_cloud_pool_blocking(
        self,
        prompt: str,
        *,
        pool_rotation=None,
        conversation_key: str | None = None,
    ) -> tuple[str, ModelMetadata]:
        """Try Gemini → Mistral → Big Pickle → local; return (text, metadata)."""
        chain = self.cloud_pool_chain()
        pinned = self.pinned_provider(conversation_key)
        if pinned is not None and any(entry[0] == pinned for entry in chain):
            rotated = _pin_provider_chain(chain, pinned)
        elif pool_rotation is not None:
            rotated = pool_rotation.rotate(chain)
        else:
            rotated = chain
        text, provider, model_name, attempts = _walk_cloud_pool_chain(
            rotated, prompt, temperature=0.7
        )
        if text is not None:
            self.record_pin(conversation_key, provider)
            fb = len(attempts) > 1
            first_provider, first_model = (
                (rotated[0][0], rotated[0][1]) if rotated else self.cloud_pool_preview()
            )
            return text, ModelMetadata(
                requested_provider=first_provider,
                requested_model=first_model,
                attempted_models=attempts,
                final_provider=provider,
                final_model=model_name,
                fallback_reason=None if not fb else f"Fell back from {attempts[0].provider}",
                fallback_occurred=fb,
                requested_tab=CHAT_BACKEND_CLOUD_POOL,
            )

        if provider_enabled(self.settings, LLM_PROVIDER_LOCAL):
            local_model = self.local_model()
            local_text = self._deps._ollama_generate_blocking(prompt)
            attempts = attempts + (ModelAttempt("local", local_model, _MODEL_STATUS_OK),)
            first_provider, first_model = self.cloud_pool_preview()
            return local_text, ModelMetadata(
                requested_provider=first_provider,
                requested_model=first_model,
                attempted_models=attempts,
                final_provider="local",
                final_model=local_model,
                fallback_reason="All cloud providers unavailable",
                fallback_occurred=True,
                requested_tab=CHAT_BACKEND_CLOUD_POOL,
            )
        raise RuntimeError("雲端池目前沒有可用模型。")

    def generate_gemini_with_fallback(
        self, prompt: str, *, temperature: float
    ) -> tuple[str, ModelMetadata]:
        attempts: list[ModelAttempt] = []
        gemini_models = self.gemini_route_models()
        primary_model = gemini_models[0]

        if not getattr(self.settings, "openclaw_gemini_api_key", None):
            attempts.append(
                ModelAttempt(
                    "gemini",
                    primary_model,
                    _MODEL_STATUS_NOT_CONFIGURED,
                    "Gemini API key missing",
                )
            )
            text = self._deps._ollama_generate_blocking(prompt)
            attempts.append(ModelAttempt("local", self.local_model(), _MODEL_STATUS_OK))
            return text, self.model_metadata_for_backend(
                CHAT_BACKEND_GEMINI,
                tuple(attempts),
                "local",
                self.local_model(),
                fallback_reason="Gemini API key missing",
            )

        last_reason = ""
        for model in gemini_models:
            client = self._deps._build_gemini_chat_client(model)
            if client is None:
                attempts.append(
                    ModelAttempt(
                        "gemini",
                        model,
                        _MODEL_STATUS_NOT_CONFIGURED,
                        "Gemini API key missing",
                    )
                )
                last_reason = "Gemini API key missing"
                break
            try:
                text = client.generate(prompt, temperature=temperature)
            except _GeminiRequestError as exc:
                attempts.append(ModelAttempt("gemini", model, exc.status, str(exc)))
                last_reason = str(exc)
                if _is_gemini_fallback_status(exc.status):
                    continue
                raise
            attempts.append(ModelAttempt("gemini", model, _MODEL_STATUS_OK))
            fallback_reason = attempts[0].reason if len(attempts) > 1 else None
            return text, self.model_metadata_for_backend(
                CHAT_BACKEND_GEMINI,
                tuple(attempts),
                "gemini",
                model,
                fallback_reason=fallback_reason,
            )

        text = self._deps._ollama_generate_blocking(prompt)
        attempts.append(ModelAttempt("local", self.local_model(), _MODEL_STATUS_OK))
        return text, self.model_metadata_for_backend(
            CHAT_BACKEND_GEMINI,
            tuple(attempts),
            "local",
            self.local_model(),
            fallback_reason=last_reason or "Gemini quota or rate limit",
        )
