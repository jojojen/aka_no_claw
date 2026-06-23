"""Request/response models for the local Web command bridge (issue #30).

The aka_no_claw_web mobile console (issue jojojen/aka_no_claw_web#1) speaks a
small JSON contract to this repo's local command bridge. This module owns that
contract: parse/validate the incoming WebCommandRequest, build the
WebCommandResponse the frontend renders, and construct the streaming event
dicts for the chat path. Keeping the contract here (not in the web repo) means
the frontend never imports OpenClaw internals — it only sees JSON.

Contract source: aka_no_claw_web/docs/LOCAL_MOBILE_CONSOLE_MVP.md.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass, field

# --- Modes ----------------------------------------------------------------
MODE_CHAT = "chat"
MODE_TRANSLATION = "translation"
MODE_INVESTMENT = "investment"
# 生活 (lifestyle) mode — music control surface (aka_no_claw_web#3). Its actions
# are driven through the dedicated /api/command/music route (callback buttons),
# not the blocking /api/command router, so it is intentionally not in _MODES.
MODE_LIFE = "life"
SUBMODE_MUSIC = "music"
_MODES = {MODE_CHAT, MODE_TRANSLATION, MODE_INVESTMENT}

# --- Submodes -------------------------------------------------------------
SUBMODE_TEXT_TRANSLATION = "text_translation"
SUBMODE_IMAGE_TRANSLATION = "image_translation"
SUBMODE_DEEP_PRODUCT_RESEARCH = "deep_product_research"
SUBMODE_SELLER_REPUTATION_SNAPSHOT = "seller_reputation_snapshot"

# --- Chat backends --------------------------------------------------------
CHAT_BACKEND_LOCAL = "local"
CHAT_BACKEND_CLOUD_PICKLE = "cloud_pickle"
_CHAT_BACKENDS = {CHAT_BACKEND_LOCAL, CHAT_BACKEND_CLOUD_PICKLE}

# --- Response statuses ----------------------------------------------------
STATUS_OK = "ok"
STATUS_PARTIAL = "partial"
STATUS_ERROR = "error"
STATUS_UNSUPPORTED = "unsupported"

# --- Streaming event types ------------------------------------------------
EVENT_START = "start"
EVENT_DELTA = "delta"
EVENT_HEARTBEAT = "heartbeat"
EVENT_DONE = "done"
EVENT_ERROR = "error"

DEFAULT_SOURCE = "aka_no_claw_web"

# --- Chat history (Web Chat continuity, issue #44) ------------------------
# Recent visible chat turns the frontend sends so the bridge can answer
# follow-ups in context. History is best-effort context, not authoritative
# data: it is sanitized and trimmed here, and a malformed entry is skipped
# rather than failing the whole request.
CHAT_ROLES = {"user", "assistant", "system"}
MAX_HISTORY_TURNS = 12
MAX_HISTORY_CONTENT_LEN = 4000


class RequestValidationError(ValueError):
    """Raised when an incoming request body violates the contract."""


@dataclass(frozen=True, slots=True)
class Attachment:
    type: str
    filename: str | None = None
    content_type: str | None = None
    # Raw attachment bytes (e.g. an uploaded image). Carried in-process only —
    # never serialized back out. The frontend sends them base64-encoded in the
    # ``data_base64`` JSON field (see ``from_dict``).
    data: bytes | None = None

    @classmethod
    def from_dict(cls, data: object) -> "Attachment":
        if not isinstance(data, dict):
            raise RequestValidationError("attachment must be an object")
        atype = str(data.get("type") or "").strip()
        if not atype:
            raise RequestValidationError("attachment.type is required")
        raw_b64 = data.get("data_base64")
        decoded: bytes | None = None
        if raw_b64 is not None:
            if not isinstance(raw_b64, str):
                raise RequestValidationError("attachment.data_base64 must be a base64 string")
            try:
                decoded = base64.b64decode(raw_b64, validate=True)
            except ValueError as exc:  # binascii.Error subclasses ValueError
                raise RequestValidationError(
                    f"attachment.data_base64 is not valid base64: {exc}"
                )
        return cls(
            type=atype,
            filename=_opt_str(data.get("filename")),
            content_type=_opt_str(data.get("content_type")),
            data=decoded,
        )


@dataclass(frozen=True, slots=True)
class ChatTurn:
    role: str
    content: str


@dataclass(frozen=True, slots=True)
class WebCommandRequest:
    mode: str
    input: str = ""
    submode: str | None = None
    chat_backend: str = CHAT_BACKEND_LOCAL
    attachments: tuple[Attachment, ...] = ()
    source: str = DEFAULT_SOURCE
    # Web Chat continuity (#44): recent visible turns + stable ids. Only used
    # when mode == chat; other modes ignore history even if it is present.
    history: tuple[ChatTurn, ...] = ()
    session_id: str | None = None
    conversation_id: str | None = None

    @property
    def has_image_attachment(self) -> bool:
        return any(a.type == "image" for a in self.attachments)


@dataclass(frozen=True, slots=True)
class Action:
    label: str
    command: str
    input: str | None = None

    def to_dict(self) -> dict[str, object]:
        out: dict[str, object] = {"label": self.label, "command": self.command}
        if self.input is not None:
            out["input"] = self.input
        return out


@dataclass(frozen=True, slots=True)
class Source:
    source_id: str | None = None
    title: str | None = None
    url: str | None = None
    domain: str | None = None

    def to_dict(self) -> dict[str, object]:
        out: dict[str, object] = {}
        if self.source_id is not None:
            out["source_id"] = self.source_id
        if self.title is not None:
            out["title"] = self.title
        if self.url is not None:
            out["url"] = self.url
        if self.domain is not None:
            out["domain"] = self.domain
        return out


@dataclass(frozen=True, slots=True)
class WebCommandResponse:
    status: str
    message: str
    mode: str | None = None
    submode: str | None = None
    actions: tuple[Action, ...] = ()
    warnings: tuple[str, ...] = ()
    sources: tuple[Source, ...] = ()

    def to_dict(self) -> dict[str, object]:
        out: dict[str, object] = {"status": self.status, "message": self.message}
        if self.mode is not None:
            out["mode"] = self.mode
        if self.submode is not None:
            out["submode"] = self.submode
        if self.actions:
            out["actions"] = [a.to_dict() for a in self.actions]
        if self.warnings:
            out["warnings"] = list(self.warnings)
        if self.sources:
            out["sources"] = [s.to_dict() for s in self.sources]
        return out


def parse_request(data: object) -> WebCommandRequest:
    """Parse + validate a raw decoded JSON body into a WebCommandRequest.

    Raises RequestValidationError for any contract violation so the HTTP layer
    can answer 400 instead of crashing the worker."""
    if not isinstance(data, dict):
        raise RequestValidationError("request body must be a JSON object")

    mode = str(data.get("mode") or "").strip().lower()
    if mode not in _MODES:
        raise RequestValidationError(
            f"mode must be one of {sorted(_MODES)}, got {mode!r}"
        )

    submode = _opt_str(data.get("submode"))

    chat_backend = str(data.get("chat_backend") or CHAT_BACKEND_LOCAL).strip().lower()
    if chat_backend not in _CHAT_BACKENDS:
        raise RequestValidationError(
            f"chat_backend must be one of {sorted(_CHAT_BACKENDS)}, got {chat_backend!r}"
        )

    raw_input = data.get("input")
    text = "" if raw_input is None else str(raw_input)

    raw_attachments = data.get("attachments") or []
    if not isinstance(raw_attachments, list):
        raise RequestValidationError("attachments must be a list")
    attachments = tuple(Attachment.from_dict(a) for a in raw_attachments)

    source = str(data.get("source") or DEFAULT_SOURCE).strip() or DEFAULT_SOURCE

    history = _sanitize_history(data.get("history"))

    return WebCommandRequest(
        mode=mode,
        input=text,
        submode=submode,
        chat_backend=chat_backend,
        attachments=attachments,
        source=source,
        history=history,
        session_id=_opt_str(data.get("session_id")),
        conversation_id=_opt_str(data.get("conversation_id")),
    )


def _sanitize_history(raw: object) -> tuple[ChatTurn, ...]:
    """Coerce frontend-provided chat history into clean ChatTurns.

    History is best-effort context, so this never raises: a non-list, or any
    malformed entry, is silently skipped rather than failing the whole chat
    request. Entries are kept only when role is known and content is a non-empty
    string; content is length-capped and the list is trimmed to the most recent
    MAX_HISTORY_TURNS turns."""
    if not isinstance(raw, list):
        return ()
    turns: list[ChatTurn] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        role = item.get("role")
        content = item.get("content")
        if role not in CHAT_ROLES or not isinstance(content, str):
            continue
        content = content.strip()
        if not content:
            continue
        turns.append(ChatTurn(role=role, content=content[:MAX_HISTORY_CONTENT_LEN]))
    return tuple(turns[-MAX_HISTORY_TURNS:])


# --- Streaming event constructors ----------------------------------------
def stream_start(request_id: str) -> dict[str, object]:
    return {"type": EVENT_START, "request_id": request_id}


def stream_delta(text: str) -> dict[str, object]:
    return {"type": EVENT_DELTA, "text": text}


def stream_heartbeat() -> dict[str, object]:
    return {"type": EVENT_HEARTBEAT}


def stream_done(message: str) -> dict[str, object]:
    return {"type": EVENT_DONE, "message": message}


def stream_error(message: str) -> dict[str, object]:
    return {"type": EVENT_ERROR, "message": message}


def _opt_str(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
