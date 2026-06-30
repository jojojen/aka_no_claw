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
import json
import re
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
CHAT_BACKEND_CLOUD_MISTRAL = "cloud_mistral"
_CHAT_BACKENDS = {CHAT_BACKEND_LOCAL, CHAT_BACKEND_CLOUD_PICKLE, CHAT_BACKEND_CLOUD_MISTRAL}

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
EVENT_REDIRECT = "redirect"

DEFAULT_SOURCE = "aka_no_claw_web"

# --- Chat history (Web Chat continuity, issue #44) ------------------------
# Recent visible chat turns the frontend sends so the bridge can answer
# follow-ups in context. History is best-effort context, not authoritative
# data: it is sanitized and trimmed here, and a malformed entry is skipped
# rather than failing the whole request.
#
# Only the user's own visible turns count: a tampered/buggy frontend must not be
# able to inject a `system` instruction into the prompt, so system turns are
# rejected (a trusted backend summary would be added server-side, not here).
CHAT_ROLES = {"user", "assistant"}
MAX_HISTORY_TURNS = 12
MAX_HISTORY_CONTENT_LEN = 4000
# Cumulative character budget across the kept turns (not just per-turn): the
# window is "recent", so keep the newest turns until either the turn count or
# this total budget is hit — otherwise 12 * 4000 chars could bloat the prompt.
MAX_HISTORY_TOTAL_CHARS = 4000


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
    request. Entries are kept only when role is user/assistant and content is a
    non-empty string; content is per-turn length-capped, then the list is kept to
    the most recent turns within both MAX_HISTORY_TURNS and a cumulative
    MAX_HISTORY_TOTAL_CHARS budget (walked newest->oldest, restored to
    chronological order)."""
    if not isinstance(raw, list):
        return ()
    valid: list[ChatTurn] = []
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
        valid.append(ChatTurn(role=role, content=content[:MAX_HISTORY_CONTENT_LEN]))
    kept: list[ChatTurn] = []
    total = 0
    for turn in reversed(valid):
        if len(kept) >= MAX_HISTORY_TURNS:
            break
        total += len(turn.content)
        # Always keep at least the newest turn even if it alone exceeds budget.
        if total > MAX_HISTORY_TOTAL_CHARS and kept:
            break
        kept.append(turn)
    kept.reverse()
    return tuple(kept)


# --- Web Chat contextual tool routing (issue #45) ------------------------
# Phase 2: before answering a chat message the bridge asks a local router LLM
# whether the question needs a tool. Only one bounded tool is whitelisted for
# now (``/search`` → grounded web search). The router speaks strict JSON; this
# module owns the *trust boundary* around that output: a malformed payload, an
# unknown decision/tool, or an empty tool query is rejected so the caller falls
# back to a plain (direct) chat answer instead of executing anything unsafe.
ROUTER_DECISION_DIRECT = "direct"
ROUTER_DECISION_TOOL = "tool"
CHAT_TOOL_SEARCH = "/search"
# Hardcoding the tool whitelist is deliberate (a closed protocol allowlist, not
# open-ended recognition): only these exact tools may ever be dispatched.
CHAT_TOOLS = {CHAT_TOOL_SEARCH}

# The router ``query`` is untrusted LLM output that flows into logs, the visible
# tool banner, and ``web_search()`` — so it is normalized and budgeted before it
# is trusted: control characters stripped, all whitespace (incl. newlines)
# collapsed to single spaces, and the result capped. Whitelisting the *tool*
# guards against arbitrary command execution; this guards the tool's *argument*.
MAX_ROUTER_QUERY_LEN = 256
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f]")


def _normalize_router_query(query: str) -> str:
    cleaned = _CONTROL_CHARS_RE.sub(" ", query)
    cleaned = " ".join(cleaned.split())
    return cleaned[:MAX_ROUTER_QUERY_LEN].strip()


@dataclass(frozen=True, slots=True)
class RouterDecision:
    decision: str  # ROUTER_DECISION_DIRECT | ROUTER_DECISION_TOOL
    tool: str | None = None
    query: str = ""
    reason_summary: str = ""


def _loads_first_json_object(text: str) -> object:
    """Best-effort decode of the first JSON object in router output.

    A small local model may wrap the JSON in prose, ``<think>`` noise, or a
    ```` ```json ```` fence. Try a clean parse first, then fall back to the
    substring between the first ``{`` and the last ``}``. Returns ``None`` when
    nothing parses (the caller treats that as an untrusted decision)."""
    try:
        return json.loads(text)
    except ValueError:
        pass
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        return json.loads(text[start : end + 1])
    except ValueError:
        return None


def parse_router_decision(raw: object) -> RouterDecision | None:
    """Coerce raw router LLM output into a trusted RouterDecision, or ``None``.

    Returns ``None`` (→ caller falls back to direct chat) on anything that can't
    be trusted: non-string input, unparseable JSON, an unknown ``decision``, a
    tool not on the whitelist, or a tool decision with an empty ``query``. Extra
    fields are ignored. A valid ``direct`` decision returns a RouterDecision so
    the caller can log the router's reasoning even when no tool runs."""
    if not isinstance(raw, str):
        return None
    data = _loads_first_json_object(raw.strip())
    if not isinstance(data, dict):
        return None
    decision = data.get("decision")
    reason = _opt_str(data.get("reason_summary")) or ""
    if decision == ROUTER_DECISION_DIRECT:
        return RouterDecision(decision=ROUTER_DECISION_DIRECT, reason_summary=reason)
    if decision == ROUTER_DECISION_TOOL:
        tool = data.get("tool")
        if tool not in CHAT_TOOLS:
            return None
        query = data.get("query")
        if not isinstance(query, str):
            return None
        query = _normalize_router_query(query)
        if not query:
            return None
        return RouterDecision(
            decision=ROUTER_DECISION_TOOL,
            tool=tool,
            query=query,
            reason_summary=reason,
        )
    return None


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


def stream_redirect(
    intent: str, description: str, *, workflow_id: str = ""
) -> dict[str, object]:
    ev: dict[str, object] = {
        "type": EVENT_REDIRECT,
        "intent": intent,
        "description": description,
    }
    if workflow_id:
        ev["workflow_id"] = workflow_id
    return ev


def _opt_str(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


# --- Music intent fast-path (issue #49) ----------------------------------
# A deterministic keyword layer that fires BEFORE the LLM router for obvious
# natural-language music requests.  It maps only to a closed set of allowlisted
# actions and never executes arbitrary slash commands.
#
# Issue #50 extends this with a bounded multi-step plan action (MUSIC_ACTION_PLAN)
# for requests that contain an external-context qualifier such as 熱門/最新.
MUSIC_ACTION_RANDOM = "play_random"
MUSIC_ACTION_PLAY_QUERY = "play_query"
MUSIC_ACTION_STOP = "stop"
MUSIC_ACTION_NOW = "now"
MUSIC_ACTION_LIST_ALL = "list_all"
MUSIC_ACTION_LIST_FAVORITES = "list_favorites"
MUSIC_ACTION_PLAN = "plan"         # bounded multi-tool plan (#50)
_MUSIC_ACTIONS = frozenset({
    MUSIC_ACTION_RANDOM, MUSIC_ACTION_PLAY_QUERY, MUSIC_ACTION_STOP,
    MUSIC_ACTION_NOW, MUSIC_ACTION_LIST_ALL, MUSIC_ACTION_LIST_FAVORITES,
    MUSIC_ACTION_PLAN,
})

# Qualifiers that signal the user wants external-context information (e.g.
# popularity / latest release) before selecting a local song.  Presence of any
# of these in a play-style request triggers the multi-step plan path (#50).
_MUSIC_PLAN_QUALIFIER_RE = re.compile(
    r"(熱門|最新|代表曲|推薦|人氣|知名|好聽|有名|名曲|popular|最近|新出|新曲)",
    re.IGNORECASE,
)
# Detect "播/播放/放 <artist> <qualifier>" without requiring spaces (Chinese text
# normally omits them).  The non-greedy (.+?) stops at the first qualifier so
# the captured group is just the artist / keyword portion.
_MUSIC_PLAN_RE = re.compile(
    r"^(?:播放?|播|放)\s*(.+?)\s*(?:熱門|最新|代表曲|推薦|人氣|知名|好聽|有名|名曲|popular|最近|新出|新曲)"
    r"(?:單曲|歌曲|歌)?$",
    re.IGNORECASE,
)

# Simple one-intent patterns ordered most-specific first; PLAY_QUERY last.
_MUSIC_FAST_PATH_RE: list[tuple[re.Pattern[str], str, int]] = [
    # play_random — generic "play a song" without a specific title
    (re.compile(
        r"^(?:播放?|放|來)(?:一首|隨機|個)(?:歌|音樂|曲子)?$"
        r"|^隨機(?:播放|選歌|播歌)?$|^播歌$"
    ), MUSIC_ACTION_RANDOM, 0),
    # stop
    (re.compile(
        r"^(?:停止(?:音樂|播放|播歌)?|停播|音樂停|停掉(?:音樂|播放)?|暫停(?:音樂|播放)?)$"
    ), MUSIC_ACTION_STOP, 0),
    # now playing
    (re.compile(
        r"(?:現在|正在|目前)(?:播|放)(?:的|什麼|啥)?|播什麼|在播啥"
    ), MUSIC_ACTION_NOW, 0),
    # list all songs
    (re.compile(
        r"列出?(?:所有|全部)?歌曲|有(?:什麼|哪些)歌|打開音樂清單|音樂清單|歌曲列表|可以播(?:放)?的歌"
    ), MUSIC_ACTION_LIST_ALL, 0),
    # list favorites
    (re.compile(
        r"列出?最愛|我的歌單|最愛歌曲|收藏(?:歌曲|清單)|最愛清單"
    ), MUSIC_ACTION_LIST_FAVORITES, 0),
    # play_query — specific title/artist (last so generic patterns win first)
    (re.compile(r"^(?:播放|播|放)\s*(.+)$"), MUSIC_ACTION_PLAY_QUERY, 1),
]


@dataclass(frozen=True, slots=True)
class MusicIntent:
    """A resolved music action from the fast-path detector.

    ``action`` is always one of the ``MUSIC_ACTION_*`` constants.
    ``query`` is the song/artist search text (PLAY_QUERY, PLAN only).
    ``qualifier`` is the external-context word that triggered the plan path
    (e.g. ``"熱門"``; PLAN only).
    """
    action: str
    query: str = ""
    qualifier: str = ""


def detect_music_intent(text: str) -> MusicIntent | None:
    """Deterministic fast-path: parse natural-language music requests into a
    bounded ``MusicIntent`` before the LLM router runs.

    Checks the multi-tool plan path first (qualifier words like 熱門/最新 override
    plain play_query so the bridge knows to fetch external context before
    selecting a song).  Falls back to the simple one-step patterns.  Returns
    ``None`` when nothing matches so the caller passes through to the LLM router.
    """
    t = (text or "").strip()
    # Plan path first: "播放 <artist> 熱門單曲" style requests
    m = _MUSIC_PLAN_RE.match(t)
    if m:
        artist = m.group(1).strip()
        qm = _MUSIC_PLAN_QUALIFIER_RE.search(t)
        qualifier = qm.group(1) if qm else "熱門"
        return MusicIntent(action=MUSIC_ACTION_PLAN, query=artist, qualifier=qualifier)
    # Simple one-step fast-path
    for pattern, action, group in _MUSIC_FAST_PATH_RE:
        mm = pattern.search(t)
        if mm:
            query = mm.group(group).strip() if group else ""
            return MusicIntent(action=action, query=query)
    return None


# --- Chat tool typed envelope (issue #46) --------------------------------
# A small typed layer between CommandBridge and individual tool executors so
# each tool does not reimplment its own validation, budget enforcement, banner
# formatting, trace logging, or error semantics.

@dataclass(frozen=True, slots=True)
class ChatToolPolicy:
    """Static per-tool limits and display metadata.

    All char limits apply *after* whitespace normalisation.  Enforcement
    happens in ChatToolRequest construction so the executor never receives an
    oversized input.
    """
    display_name: str
    max_query_chars: int = 256
    max_source_field_chars: int = 500
    max_source_pack_chars: int = 4000


@dataclass(frozen=True, slots=True)
class ChatToolRequest:
    """Validated, budget-enforced input for a single chat tool call.

    Built by :func:`make_chat_tool_request` which applies the policy before
    handing control to the executor.
    """
    tool: str
    query: str          # sanitized, length-capped by policy.max_query_chars
    user_question: str  # original user text (for synthesis prompts)
    policy: ChatToolPolicy


@dataclass(frozen=True, slots=True)
class ChatToolResult:
    """Typed return value from a chat tool executor.

    The ``answer`` field is the user-visible text (may include a source block).
    ``source_count`` and ``result_summary`` are used for trace logging only —
    they never contain private chain-of-thought or raw external content.
    """
    answer: str
    source_count: int = 0
    result_summary: str = ""


def make_chat_tool_request(
    tool: str,
    raw_query: str,
    user_question: str,
    policy: ChatToolPolicy,
) -> ChatToolRequest:
    """Validate and budget-enforce a raw router query into a ChatToolRequest.

    Applies the same control-char stripping and whitespace collapsing as
    :func:`_normalize_router_query`, then enforces ``policy.max_query_chars``.
    Raises ``ValueError`` when the cleaned query is empty after normalisation.
    """
    cleaned = _CONTROL_CHARS_RE.sub(" ", raw_query or "")
    cleaned = " ".join(cleaned.split())[: policy.max_query_chars].strip()
    if not cleaned:
        raise ValueError(f"chat tool {tool!r}: query is empty after normalisation")
    return ChatToolRequest(
        tool=tool,
        query=cleaned,
        user_question=(user_question or "").strip(),
        policy=policy,
    )
