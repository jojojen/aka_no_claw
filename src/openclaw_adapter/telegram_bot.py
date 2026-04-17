from __future__ import annotations

import json
import logging
import ssl
from dataclasses import dataclass
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from assistant_runtime import AssistantSettings, build_ssl_context
from assistant_runtime.logging_utils import mask_identifier, trim_for_log
from market_monitor.http import HttpClient
from tcg_tracker.hot_cards import HotCardBoard, TcgHotCardService

from .commands import lookup_card
from .formatters import format_jpy, format_lookup_result

LookupRenderer = Callable[["TelegramLookupQuery"], str]
BoardLoader = Callable[[], tuple[HotCardBoard, ...]]
CatalogRenderer = Callable[[], str]

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class TelegramLookupQuery:
    game: str
    name: str
    card_number: str | None = None
    rarity: str | None = None
    set_code: str | None = None


class TelegramBotClient:
    def __init__(
        self,
        token: str,
        *,
        timeout_seconds: float = 35.0,
        ssl_context: ssl.SSLContext | None = None,
    ) -> None:
        self._base_url = f"https://api.telegram.org/bot{token}/"
        self._timeout_seconds = timeout_seconds
        self._ssl_context = ssl_context

    def get_me(self) -> dict[str, object]:
        return self._call("getMe")

    def get_updates(self, *, offset: int | None = None, timeout: int = 20) -> list[dict[str, object]]:
        payload: dict[str, object] = {
            "timeout": timeout,
            "allowed_updates": ["message"],
        }
        if offset is not None:
            payload["offset"] = offset
        result = self._call("getUpdates", payload)
        return result if isinstance(result, list) else []

    def send_message(self, *, chat_id: str | int, text: str) -> dict[str, object]:
        return self._call(
            "sendMessage",
            {
                "chat_id": str(chat_id),
                "text": text[:4096],
                "disable_web_page_preview": True,
            },
        )

    def _call(self, method: str, payload: dict[str, object] | None = None) -> dict[str, object] | list[dict[str, object]]:
        request_body = None if payload is None else json.dumps(payload).encode("utf-8")
        request = Request(
            self._base_url + method,
            data=request_body,
            headers={"Content-Type": "application/json; charset=utf-8"},
            method="POST",
        )
        try:
            with urlopen(request, timeout=self._timeout_seconds, context=self._ssl_context) as response:
                response_payload = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:  # pragma: no cover - network-dependent.
            raise RuntimeError(f"Telegram API HTTP {exc.code} for {method}.") from exc
        except URLError as exc:  # pragma: no cover - network-dependent.
            raise RuntimeError(f"Telegram API request failed for {method}: {exc.reason}") from exc

        if not response_payload.get("ok"):
            description = response_payload.get("description", "Unknown Telegram API error.")
            raise RuntimeError(f"Telegram API {method} failed: {description}")
        return response_payload.get("result", {})


class TelegramCommandProcessor:
    def __init__(
        self,
        *,
        settings: AssistantSettings,
        lookup_renderer: LookupRenderer,
        board_loader: BoardLoader,
        catalog_renderer: CatalogRenderer,
    ) -> None:
        self._settings = settings
        self._lookup_renderer = lookup_renderer
        self._board_loader = board_loader
        self._catalog_renderer = catalog_renderer

    def build_reply(self, *, chat_id: str | int, text: str | None) -> str | None:
        logger.info(
            "Telegram message received chat_id=%s text=%s",
            mask_identifier(chat_id),
            trim_for_log(text or "", limit=320),
        )
        if text is None:
            return None
        if not self._is_allowed_chat(chat_id):
            logger.warning("Rejected Telegram message from unauthorized chat_id=%s", mask_identifier(chat_id))
            return None

        content = text.strip()
        if not content:
            return "Empty command. Use /help to see supported commands."

        command, _, remainder = content.partition(" ")
        command = command.split("@", 1)[0].lower()
        remainder = remainder.strip()
        logger.debug("Telegram command parsed command=%s remainder=%s", command, trim_for_log(remainder, limit=240))

        if command in {"/start", "/help"}:
            return self._help_text()
        if command == "/ping":
            return "pong"
        if command == "/status":
            return self._status_text()
        if command == "/tools":
            return self._catalog_renderer()
        if command == "/lookup":
            return self._handle_lookup(remainder)
        if command == "/liquidity":
            return self._handle_liquidity(remainder)
        logger.info("Telegram unknown command command=%s", command)
        return "Unknown command. Use /start /help /ping /status /tools /lookup /liquidity"

    def _handle_lookup(self, raw: str) -> str:
        try:
            query = parse_lookup_command(raw)
        except ValueError as exc:
            logger.warning("Telegram lookup parse failed raw=%s error=%s", trim_for_log(raw), exc)
            return f"{exc}\nExample: /lookup pokemon | ピカチュウex | 132/106 | SAR | sv08"

        try:
            logger.info(
                "Telegram lookup parsed game=%s name=%s card_number=%s rarity=%s set_code=%s",
                query.game,
                query.name,
                query.card_number,
                query.rarity,
                query.set_code,
            )
            return self._lookup_renderer(query)
        except Exception as exc:  # pragma: no cover - source/network-dependent.
            logger.exception("Telegram lookup failed game=%s name=%s", query.game, query.name)
            return f"Lookup failed: {exc}"

    def _handle_liquidity(self, raw: str) -> str:
        parts = [part for part in raw.split() if part]
        if not parts:
            return "Specify a game, for example: /liquidity pokemon"

        game = parts[0].lower()
        if game not in {"pokemon", "ws"}:
            return "Unsupported game. Use pokemon or ws."

        limit = 5
        if len(parts) >= 2 and parts[1].isdigit():
            limit = max(1, min(10, int(parts[1])))

        try:
            board = next(board for board in self._board_loader() if board.game == game)
        except StopIteration:
            logger.warning("Telegram liquidity board unavailable game=%s", game)
            return f"No liquidity board is available for {game}."
        except Exception as exc:  # pragma: no cover - source/network-dependent.
            logger.exception("Telegram liquidity load failed game=%s", game)
            return f"Liquidity board failed: {exc}"

        logger.info("Telegram liquidity board loaded game=%s limit=%s items=%s", game, limit, len(board.items))
        return format_liquidity_board(board, limit=limit)

    def _status_text(self) -> str:
        allowed_chat = self._settings.openclaw_telegram_chat_id or "not restricted"
        return "\n".join(
            [
                "OpenClaw Telegram status",
                f"env: {self._settings.monitor_env}",
                f"db: {self._settings.monitor_db_path}",
                f"allowed chat: {allowed_chat}",
            ]
        )

    def _help_text(self) -> str:
        return "\n".join(
            [
                "OpenClaw Telegram test bot",
                "/ping",
                "/status",
                "/tools",
                "/lookup pokemon ピカチュウex",
                "/lookup pokemon | ピカチュウex | 132/106 | SAR | sv08",
                "/lookup ws | “夏の思い出”蒼(サイン入り) | SMP/W60-051SP | SP | smp",
                "/liquidity pokemon",
                "/liquidity ws 5",
            ]
        )

    def _is_allowed_chat(self, chat_id: str | int) -> bool:
        allowed_chat_id = self._settings.openclaw_telegram_chat_id
        if allowed_chat_id is None:
            return True
        return str(chat_id) == str(allowed_chat_id)


def parse_lookup_command(raw: str) -> TelegramLookupQuery:
    body = raw.strip()
    if not body:
        raise ValueError("Lookup command requires at least a game and a name.")

    if "|" in body:
        parts = [part.strip() for part in body.split("|")]
        if len(parts) < 2 or not parts[0] or not parts[1]:
            raise ValueError("Pipe format requires at least game and name.")
        game = parts[0].lower()
        name = parts[1]
        if game not in {"pokemon", "ws"}:
            raise ValueError("Unsupported game. Use pokemon or ws.")
        return TelegramLookupQuery(
            game=game,
            name=name,
            card_number=_value_or_none(parts, 2),
            rarity=_value_or_none(parts, 3),
            set_code=_value_or_none(parts, 4),
        )

    tokens = body.split()
    if len(tokens) < 2:
        raise ValueError("Lookup command requires at least game and name.")
    game = tokens[0].lower()
    if game not in {"pokemon", "ws"}:
        raise ValueError("Unsupported game. Use pokemon or ws.")
    name = " ".join(tokens[1:]).strip()
    if not name:
        raise ValueError("Lookup name cannot be empty.")
    return TelegramLookupQuery(game=game, name=name)


def format_liquidity_board(board: HotCardBoard, *, limit: int = 5) -> str:
    lines = [board.label, board.methodology]
    for item in board.items[:limit]:
        price_text = "price n/a" if item.price_jpy is None else format_jpy(item.price_jpy)
        bid_text = "bid n/a" if item.best_bid_jpy is None else f"bid {format_jpy(item.best_bid_jpy)}"
        ask_text = "ask n/a" if item.best_ask_jpy is None else f"ask {format_jpy(item.best_ask_jpy)}"
        ratio_text = "ratio n/a" if item.bid_ask_ratio is None else f"ratio {item.bid_ask_ratio:.0%}"
        score_text = f"liq {item.hot_score:.2f}"
        attention_text = f"attn {item.attention_score:.2f}"
        momentum_text = (
            ""
            if item.momentum_boost_score <= 0
            else f" | boost {item.momentum_boost_score:.2f}"
        )
        meta = " / ".join(
            value
            for value in (
                item.card_number or "",
                item.rarity or "",
                item.set_code or "",
                "buy-up" if item.buy_signal_label == "priceup" else "",
                "graded" if item.is_graded else "",
            )
            if value
        )
        lines.append(f"{item.rank}. {item.title}")
        lines.append(f"   {price_text} | {bid_text} | {ask_text} | {ratio_text}")
        lines.append(f"   support {item.buy_support_score:.2f}{momentum_text} | {score_text} | {attention_text}")
        if item.social_post_count is not None:
            lines.append(f"   sns {item.social_post_count} posts / {item.social_engagement_count or 0} engagement")
        if meta:
            lines.append(f"   {meta}")
        if item.references:
            lines.append(f"   {item.references[0].url}")
    return "\n".join(lines)


def default_lookup_renderer(settings: AssistantSettings) -> LookupRenderer:
    def render(query: TelegramLookupQuery) -> str:
        logger.debug(
            "Telegram lookup renderer executing game=%s name=%s card_number=%s rarity=%s set_code=%s",
            query.game,
            query.name,
            query.card_number,
            query.rarity,
            query.set_code,
        )
        result = lookup_card(
            db_path=settings.monitor_db_path,
            game=query.game,
            name=query.name,
            card_number=query.card_number,
            rarity=query.rarity,
            set_code=query.set_code,
            persist=False,
        )
        logger.info(
            "Telegram lookup renderer completed game=%s name=%s offers=%s fair_value=%s",
            query.game,
            query.name,
            len(result.offers),
            None if result.fair_value is None else result.fair_value.amount_jpy,
        )
        return format_lookup_result(result)

    return render


def run_telegram_polling(
    *,
    settings: AssistantSettings,
    lookup_renderer: LookupRenderer,
    board_loader: BoardLoader,
    catalog_renderer: CatalogRenderer,
    poll_timeout: int = 20,
    notify_startup: bool = False,
    drop_pending_updates: bool = True,
) -> int:
    token = require_telegram_token(settings)
    client = TelegramBotClient(token, ssl_context=build_ssl_context(settings))
    me = client.get_me()
    username = me.get("username", "<unknown>")
    logger.info(
        "Telegram polling starting username=%s notify_startup=%s drop_pending_updates=%s allowed_chat=%s",
        username,
        notify_startup,
        drop_pending_updates,
        mask_identifier(settings.openclaw_telegram_chat_id),
    )

    offset: int | None = None
    if drop_pending_updates:
        pending_updates = client.get_updates(timeout=0)
        if pending_updates:
            offset = int(pending_updates[-1]["update_id"]) + 1

    processor = TelegramCommandProcessor(
        settings=settings,
        lookup_renderer=lookup_renderer,
        board_loader=board_loader,
        catalog_renderer=catalog_renderer,
    )

    print(f"OpenClaw Telegram bot polling as @{username}")
    if notify_startup and settings.openclaw_telegram_chat_id is not None:
        client.send_message(chat_id=settings.openclaw_telegram_chat_id, text="OpenClaw Telegram bot is online.")
        logger.info("Telegram startup notification sent chat_id=%s", mask_identifier(settings.openclaw_telegram_chat_id))

    try:
        while True:
            updates = client.get_updates(offset=offset, timeout=poll_timeout)
            for update in updates:
                offset = int(update["update_id"]) + 1
                message = update.get("message")
                if not isinstance(message, dict):
                    continue
                chat = message.get("chat")
                if not isinstance(chat, dict):
                    continue
                chat_id = chat.get("id")
                if chat_id is None:
                    continue

                reply = processor.build_reply(chat_id=chat_id, text=message.get("text"))
                if reply:
                    logger.debug(
                        "Telegram reply sending chat_id=%s text=%s",
                        mask_identifier(chat_id),
                        trim_for_log(reply, limit=320),
                    )
                    client.send_message(chat_id=chat_id, text=reply)
    except KeyboardInterrupt:
        logger.info("Telegram polling stopped by KeyboardInterrupt")
        print("Telegram polling stopped.")
    return 0


def send_telegram_test_message(*, settings: AssistantSettings, message: str) -> int:
    token = require_telegram_token(settings)
    chat_id = require_telegram_chat_id(settings)
    client = TelegramBotClient(token, ssl_context=build_ssl_context(settings))
    logger.info("Telegram test message sending chat_id=%s text=%s", mask_identifier(chat_id), trim_for_log(message))
    client.send_message(chat_id=chat_id, text=message)
    print(f"Sent Telegram test message to chat {chat_id}.")
    return 0


def require_telegram_token(settings: AssistantSettings) -> str:
    token = settings.openclaw_telegram_bot_token
    if token is None:
        raise RuntimeError("Telegram bot token is missing. Put it in .env as OPENCLAW_TELEGRAM_BOT_TOKEN.")
    return token


def require_telegram_chat_id(settings: AssistantSettings) -> str:
    chat_id = settings.openclaw_telegram_chat_id
    if chat_id is None:
        raise RuntimeError("Telegram chat id is missing. Put it in .env as OPENCLAW_TELEGRAM_CHAT_ID.")
    return chat_id


def default_board_loader(settings: AssistantSettings | None = None) -> tuple[HotCardBoard, ...]:
    client = HttpClient(
        ssl_context=build_ssl_context(settings),
    )
    return TcgHotCardService(http_client=client).load_boards()


def _value_or_none(parts: list[str], index: int) -> str | None:
    if index >= len(parts):
        return None
    value = parts[index].strip()
    return value or None
