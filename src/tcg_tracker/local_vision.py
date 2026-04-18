from __future__ import annotations

import base64
import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from assistant_runtime import AssistantSettings, build_ssl_context
from market_monitor.normalize import normalize_card_number

logger = logging.getLogger(__name__)

CARD_JSON_SCHEMA = {
    "type": "object",
    "properties": {
        "game": {"type": ["string", "null"]},
        "title": {"type": ["string", "null"]},
        "aliases": {
            "type": ["array", "null"],
            "items": {"type": "string"},
        },
        "card_number": {"type": ["string", "null"]},
        "rarity": {"type": ["string", "null"]},
        "set_code": {"type": ["string", "null"]},
        "confidence": {"type": ["number", "null"]},
    },
    "required": ["game", "title", "aliases", "card_number", "rarity", "set_code", "confidence"],
    "additionalProperties": False,
}


@dataclass(frozen=True, slots=True)
class LocalVisionCardCandidate:
    backend: str
    model: str
    game: str | None
    title: str | None
    aliases: tuple[str, ...]
    card_number: str | None
    rarity: str | None
    set_code: str | None
    confidence: float | None = None
    raw_response: str = ""
    warnings: tuple[str, ...] = ()

    @property
    def descriptor(self) -> str:
        return f"{self.backend}:{self.model}"


class OllamaLocalVisionClient:
    backend = "ollama"

    def __init__(
        self,
        *,
        endpoint: str,
        model: str,
        timeout_seconds: int,
        settings: AssistantSettings | None = None,
    ) -> None:
        self.endpoint = endpoint.rstrip("/")
        self.model = model
        self.timeout_seconds = timeout_seconds
        self._ssl_context = build_ssl_context(settings) if self.endpoint.startswith("https://") else None

    @property
    def descriptor(self) -> str:
        return f"{self.backend}:{self.model}"

    def analyze_card_image(
        self,
        image_path: Path,
        *,
        game_hint: str | None = None,
        title_hint: str | None = None,
    ) -> LocalVisionCardCandidate | None:
        image_payload = base64.b64encode(image_path.read_bytes()).decode("ascii")
        payload = {
            "model": self.model,
            "prompt": self._build_prompt(game_hint=game_hint, title_hint=title_hint),
            "images": [image_payload],
            "format": CARD_JSON_SCHEMA,
            "stream": False,
            "options": {
                "temperature": 0,
            },
        }
        response_text = self._post_generate(payload)
        candidate_payload = _load_json_fragment(response_text)
        if not isinstance(candidate_payload, dict):
            raise RuntimeError(f"Ollama did not return a JSON object for {self.descriptor}.")

        return LocalVisionCardCandidate(
            backend=self.backend,
            model=self.model,
            game=_normalize_game(candidate_payload.get("game"), fallback=game_hint),
            title=_normalize_text_field(candidate_payload.get("title")),
            aliases=_normalize_aliases(candidate_payload.get("aliases")),
            card_number=_normalize_card_number_field(candidate_payload.get("card_number")),
            rarity=_normalize_token(candidate_payload.get("rarity"), uppercase=True),
            set_code=_normalize_token(candidate_payload.get("set_code"), uppercase=False),
            confidence=_normalize_confidence(candidate_payload.get("confidence")),
            raw_response=response_text,
        )

    def _build_prompt(self, *, game_hint: str | None, title_hint: str | None) -> str:
        hints: list[str] = []
        if game_hint in {"pokemon", "ws"}:
            hints.append(f"game_hint={game_hint}")
        if title_hint:
            hints.append(f"title_hint={title_hint}")
        hint_text = "\n".join(hints) if hints else "no external hints"
        return (
            "Identify the trading card in this image and return only JSON.\n"
            "If the image does not show exactly one identifiable trading card, return null for every card field instead of guessing.\n"
            "Focus on the card name, game, card number, rarity, and set code.\n"
            "Do not merge multiple cards from the same photo into one answer.\n"
            "Prefer the printed card face and footer over slab labels whenever they disagree.\n"
            "Ignore slab grades, cert numbers, copyright lines, attack text, and rule text.\n"
            "Never use slab grade text as the card rarity.\n"
            "Pokemon collector numbers should stay in full market-friendly form when visible, such as 201/165, 020/M-P, 085/SV-P, or 764/742.\n"
            "If a Pokemon promo card shows a set code and number separately, combine them into one card_number field like 085/SV-P.\n"
            "For Japanese Start Deck 100 Battle Collection slab labels such as MC JP #764, return 764/742 when the image supports that collector number.\n"
            'Use game values "pokemon", "ws", or null.\n'
            "Preserve the Japanese title when visible.\n"
            "Use null for unknown values instead of guessing.\n"
            "aliases should contain only high-confidence alternate names.\n"
            "Hints:\n"
            f"{hint_text}\n"
        )

    def _post_generate(self, payload: dict[str, object]) -> str:
        target = _resolve_generate_url(self.endpoint)
        body = None
        for attempt in range(2):
            request = Request(
                target,
                data=json.dumps(payload).encode("utf-8"),
                headers={
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                },
                method="POST",
            )
            logger.debug("Local vision request target=%s model=%s attempt=%s", target, self.model, attempt + 1)
            try:
                with urlopen(request, timeout=self.timeout_seconds, context=self._ssl_context) as response:
                    body = response.read().decode("utf-8", errors="replace")
                break
            except HTTPError as exc:
                if exc.code >= 500 and attempt == 0:
                    time.sleep(1)
                    continue
                raise RuntimeError(f"Ollama request failed with status {exc.code}.") from exc
            except URLError as exc:
                if attempt == 0:
                    time.sleep(1)
                    continue
                raise RuntimeError(f"Ollama request failed: {exc.reason}.") from exc

        if body is None:
            raise RuntimeError(f"Ollama request failed without a response body for {self.descriptor}.")

        payload = json.loads(body)
        response_text = payload.get("response", "")
        if isinstance(response_text, dict):
            return json.dumps(response_text, ensure_ascii=False)
        if not isinstance(response_text, str):
            raise RuntimeError(f"Ollama response field had unexpected type: {type(response_text).__name__}.")
        return response_text.strip()


def build_local_vision_client(settings: AssistantSettings) -> OllamaLocalVisionClient | None:
    clients = build_local_vision_clients(settings)
    if not clients:
        return None
    return clients[0]


def build_local_vision_clients(settings: AssistantSettings) -> tuple[OllamaLocalVisionClient, ...]:
    models = _parse_model_list(settings.openclaw_local_vision_model)
    if not models:
        return ()

    backend = (settings.openclaw_local_vision_backend or "ollama").strip().lower()
    if backend != "ollama":
        logger.warning("Unsupported local vision backend=%s", backend)
        return ()

    return tuple(
        OllamaLocalVisionClient(
            endpoint=settings.openclaw_local_vision_endpoint,
            model=model,
            timeout_seconds=max(1, settings.openclaw_local_vision_timeout_seconds),
            settings=settings,
        )
        for model in models
    )


def _resolve_generate_url(endpoint: str) -> str:
    parsed = urlparse(endpoint)
    path = parsed.path.rstrip("/")
    if path.endswith("/api/generate"):
        return endpoint
    if path.endswith("/api"):
        return f"{endpoint.rstrip('/')}/generate"
    return f"{endpoint.rstrip('/')}/api/generate"


def _load_json_fragment(value: str) -> object:
    stripped = value.strip()
    if not stripped:
        return None
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        pass

    start = stripped.find("{")
    end = stripped.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    return json.loads(stripped[start : end + 1])


def _normalize_game(value: object, *, fallback: str | None) -> str | None:
    normalized = _normalize_text_field(value)
    if normalized in {"pokemon", "ws"}:
        return normalized
    if fallback in {"pokemon", "ws"}:
        return fallback
    return None


def _normalize_aliases(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    aliases: list[str] = []
    for raw in value:
        normalized = _normalize_text_field(raw)
        if normalized and normalized not in aliases:
            aliases.append(normalized)
    return tuple(aliases)


def _normalize_card_number_field(value: object) -> str | None:
    normalized = _normalize_text_field(value)
    if normalized is None:
        return None
    return normalize_card_number(normalized)


def _normalize_token(value: object, *, uppercase: bool) -> str | None:
    normalized = _normalize_text_field(value)
    if normalized is None:
        return None
    collapsed = "".join(character for character in normalized if character.isalnum() or character in {"/", "+", "-"})
    if not collapsed:
        return None
    return collapsed.upper() if uppercase else collapsed.lower()


def _normalize_text_field(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.lower() in {"null", "none", "unknown", "n/a"}:
        return None
    return text


def _normalize_confidence(value: object) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _parse_model_list(value: object) -> tuple[str, ...]:
    normalized = _normalize_text_field(value)
    if normalized is None:
        return ()
    models: list[str] = []
    for raw in normalized.split(","):
        model = raw.strip()
        if model and model not in models:
            models.append(model)
    return tuple(models)
