from __future__ import annotations

import json
import logging
import ssl
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from assistant_runtime import AssistantSettings, build_ssl_context
from assistant_runtime.logging_utils import mask_identifier, trim_for_log
from market_monitor.http import HttpClient
from tcg_tracker.hot_cards import HotCardBoard, TcgHotCardService
from tcg_tracker.image_lookup import TcgImageLookupOutcome, TcgImagePriceService

from .commands import lookup_card
from .formatters import format_jpy, format_lookup_result

LookupRenderer = Callable[["TelegramLookupQuery"], str]
PhotoLookupRenderer = Callable[["TelegramPhotoQuery"], str]
BoardLoader = Callable[[], tuple[HotCardBoard, ...]]
CatalogRenderer = Callable[[], str]

PRICE_LOOKUP_COMMANDS = {"/lookup", "/price"}
TREND_BOARD_COMMANDS = {"/trend", "/trending", "/hot", "/heat", "/liquidity"}
PHOTO_SCAN_COMMANDS = {"/scan", "/image", "/photo"}
HEAVY_COMMANDS = PRICE_LOOKUP_COMMANDS | TREND_BOARD_COMMANDS

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class TelegramLookupQuery:
    game: str
    name: str
    card_number: str | None = None
    rarity: str | None = None
    set_code: str | None = None


@dataclass(frozen=True, slots=True)
class TelegramPhotoQuery:
    chat_id: str | int
    image_path: Path
    caption: str | None = None
    game_hint: str | None = None
    title_hint: str | None = None
    file_id: str | None = None


class TelegramBotClient:
    def __init__(
        self,
        token: str,
        *,
        timeout_seconds: float = 35.0,
        ssl_context: ssl.SSLContext | None = None,
    ) -> None:
        self._token = token
        self._base_url = f"https://api.telegram.org/bot{token}/"
        self._file_base_url = f"https://api.telegram.org/file/bot{token}/"
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

    def get_file(self, *, file_id: str) -> dict[str, object]:
        result = self._call("getFile", {"file_id": file_id})
        return result if isinstance(result, dict) else {}

    def download_file(self, *, file_path: str) -> bytes:
        request = Request(self._file_base_url + file_path, method="GET")
        try:
            with urlopen(request, timeout=self._timeout_seconds, context=self._ssl_context) as response:
                return response.read()
        except HTTPError as exc:  # pragma: no cover - network-dependent.
            raise RuntimeError(f"Telegram file download HTTP {exc.code} for {file_path}.") from exc
        except URLError as exc:  # pragma: no cover - network-dependent.
            raise RuntimeError(f"Telegram file download failed for {file_path}: {exc.reason}") from exc

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

    def is_allowed_chat(self, chat_id: str | int) -> bool:
        allowed_chat_id = self._settings.openclaw_telegram_chat_id
        if allowed_chat_id is None:
            return True
        return str(chat_id) == str(allowed_chat_id)

    def build_reply(self, *, chat_id: str | int, text: str | None) -> str | None:
        logger.info(
            "Telegram message received chat_id=%s text=%s",
            mask_identifier(chat_id),
            trim_for_log(text or "", limit=320),
        )
        if text is None:
            return None
        if not self.is_allowed_chat(chat_id):
            logger.warning("Rejected Telegram message from unauthorized chat_id=%s", mask_identifier(chat_id))
            return None

        content = text.strip()
        if not content:
            return "Empty command. Use /help to see supported commands."

        command = _extract_command_name(content)
        remainder = _extract_command_remainder(content)
        logger.debug("Telegram command parsed command=%s remainder=%s", command, trim_for_log(remainder, limit=240))

        if command in {"/start", "/help"}:
            return self._help_text()
        if command == "/ping":
            return "pong"
        if command == "/status":
            return self._status_text()
        if command == "/tools":
            return self._catalog_renderer()
        if command in PRICE_LOOKUP_COMMANDS:
            return self._handle_lookup(remainder)
        if command in TREND_BOARD_COMMANDS:
            return self._handle_liquidity(remainder)
        if command in PHOTO_SCAN_COMMANDS:
            return "Send a card photo with the caption /scan pokemon or /scan ws, and I will parse it and then look up the price."

        logger.info("Telegram unknown command command=%s", command)
        return "Unknown command. Use /help, /price, /trend, or send a photo with /scan."

    def _handle_lookup(self, raw: str) -> str:
        try:
            query = parse_lookup_command(raw)
        except ValueError as exc:
            logger.warning("Telegram lookup parse failed raw=%s error=%s", trim_for_log(raw), exc)
            return f"{exc}\nExample: /price pokemon | Pikachu ex | 132/106 | SAR | sv08"

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
            return "Specify a game, for example: /trend pokemon"

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
            return f"Trend board failed: {exc}"

        logger.info("Telegram liquidity board loaded game=%s limit=%s items=%s", game, limit, len(board.items))
        return format_liquidity_board(board, limit=limit)

    def _status_text(self) -> str:
        allowed_chat = self._settings.openclaw_telegram_chat_id or "not restricted"
        tesseract = self._settings.openclaw_tesseract_path or "PATH lookup"
        tessdata = self._settings.openclaw_tessdata_dir or "auto"
        return "\n".join(
            [
                "OpenClaw Telegram status",
                f"env: {self._settings.monitor_env}",
                f"db: {self._settings.monitor_db_path}",
                f"allowed chat: {allowed_chat}",
                f"tesseract: {tesseract}",
                f"tessdata: {tessdata}",
            ]
        )

    def _help_text(self) -> str:
        return "\n".join(
            [
                "OpenClaw Telegram bot",
                "/ping",
                "/status",
                "/tools",
                "/price pokemon Pikachu ex",
                "/price pokemon | Pikachu ex | 132/106 | SAR | sv08",
                "/price ws | Hatsune Miku | PJS/S91-T51 | TD | pjs",
                "/trend pokemon",
                "/trend ws 5",
                "/hot pokemon",
                "/liquidity ws 5",
                "Send a photo with caption: /scan pokemon",
            ]
        )


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
        momentum_text = "" if item.momentum_boost_score <= 0 else f" | boost {item.momentum_boost_score:.2f}"
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


def format_photo_lookup_result(outcome: TcgImageLookupOutcome) -> str:
    parsed = outcome.parsed
    lines = ["Image scan result"]

    if parsed.game:
        lines.append(f"Detected game: {parsed.game}")
    if parsed.title:
        lines.append(f"Detected card: {parsed.title}")
    metadata = " / ".join(
        value
        for value in (
            parsed.card_number or "",
            parsed.rarity or "",
            parsed.set_code or "",
        )
        if value
    )
    if metadata:
        lines.append(f"Detected fields: {metadata}")

    for warning in outcome.warnings:
        lines.append(f"Note: {warning}")

    if outcome.status == "unavailable":
        lines.append("Price lookup was skipped because OCR is not available on this machine yet.")
        return "\n".join(lines)
    if outcome.status == "unresolved" or outcome.lookup_result is None:
        lines.append("I could not extract enough card fields to run a price lookup from the image.")
        return "\n".join(lines)

    lines.append("")
    lines.append(format_lookup_result(outcome.lookup_result))
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


def default_photo_renderer(settings: AssistantSettings) -> PhotoLookupRenderer:
    image_service = TcgImagePriceService(db_path=settings.monitor_db_path, settings=settings)

    def render(query: TelegramPhotoQuery) -> str:
        logger.info(
            "Telegram photo renderer executing chat_id=%s file_id=%s path=%s caption=%s game_hint=%s title_hint=%s",
            mask_identifier(query.chat_id),
            query.file_id,
            query.image_path,
            trim_for_log(query.caption or "", limit=200),
            query.game_hint,
            query.title_hint,
        )
        outcome = image_service.lookup_image(
            query.image_path,
            caption=query.caption,
            game_hint=query.game_hint,
            title_hint=query.title_hint,
            persist=False,
        )
        logger.info(
            "Telegram photo renderer completed status=%s title=%s game=%s card_number=%s rarity=%s offers=%s",
            outcome.status,
            outcome.parsed.title,
            outcome.parsed.game,
            outcome.parsed.card_number,
            outcome.parsed.rarity,
            0 if outcome.lookup_result is None else len(outcome.lookup_result.offers),
        )
        return format_photo_lookup_result(outcome)

    return render


def run_telegram_polling(
    *,
    settings: AssistantSettings,
    lookup_renderer: LookupRenderer,
    board_loader: BoardLoader,
    catalog_renderer: CatalogRenderer,
    photo_renderer: PhotoLookupRenderer | None = None,
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
    resolved_photo_renderer = photo_renderer or default_photo_renderer(settings)

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
                handle_telegram_message(
                    client=client,
                    processor=processor,
                    photo_renderer=resolved_photo_renderer,
                    message=message,
                )
    except KeyboardInterrupt:
        logger.info("Telegram polling stopped by KeyboardInterrupt")
        print("Telegram polling stopped.")
    return 0


def handle_telegram_message(
    *,
    client: TelegramBotClient,
    processor: TelegramCommandProcessor,
    photo_renderer: PhotoLookupRenderer,
    message: dict[str, object],
) -> tuple[str, ...]:
    chat = message.get("chat")
    if not isinstance(chat, dict):
        return ()
    chat_id = chat.get("id")
    if chat_id is None:
        return ()
    if not processor.is_allowed_chat(chat_id):
        logger.warning("Rejected Telegram message from unauthorized chat_id=%s", mask_identifier(chat_id))
        return ()

    replies: list[str] = []
    photo_items = message.get("photo")
    if isinstance(photo_items, list) and photo_items:
        ack = build_processing_ack(has_photo=True)
        if ack:
            client.send_message(chat_id=chat_id, text=ack)
            replies.append(ack)
        final_reply = _handle_photo_message(
            client=client,
            photo_renderer=photo_renderer,
            chat_id=chat_id,
            message=message,
        )
        client.send_message(chat_id=chat_id, text=final_reply)
        replies.append(final_reply)
        return tuple(replies)

    text = message.get("text")
    ack = build_processing_ack(text=text if isinstance(text, str) else None)
    if ack:
        client.send_message(chat_id=chat_id, text=ack)
        replies.append(ack)

    reply = processor.build_reply(chat_id=chat_id, text=text if isinstance(text, str) else None)
    if reply:
        logger.debug(
            "Telegram reply sending chat_id=%s text=%s",
            mask_identifier(chat_id),
            trim_for_log(reply, limit=320),
        )
        client.send_message(chat_id=chat_id, text=reply)
        replies.append(reply)
    return tuple(replies)


def build_processing_ack(*, text: str | None = None, has_photo: bool = False) -> str | None:
    if has_photo:
        return "收到圖片，開始解析與查價。"
    command = _extract_command_name(text)
    if command in PRICE_LOOKUP_COMMANDS:
        return "收到查價指令，開始處理。"
    if command in TREND_BOARD_COMMANDS:
        return "收到趨勢榜查詢，開始整理資料。"
    return None


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


def _handle_photo_message(
    *,
    client: TelegramBotClient,
    photo_renderer: PhotoLookupRenderer,
    chat_id: str | int,
    message: dict[str, object],
) -> str:
    caption = message.get("caption")
    caption_text = caption if isinstance(caption, str) else None
    photo_items = message.get("photo")
    if not isinstance(photo_items, list) or not photo_items:
        return "No image was attached."

    candidates = [item for item in photo_items if isinstance(item, dict) and item.get("file_id")]
    if not candidates:
        return "Could not resolve the Telegram file metadata for this image."

    best_item = max(
        candidates,
        key=lambda item: int(item.get("file_size") or 0),
    )
    file_id = best_item.get("file_id")
    if not isinstance(file_id, str):
        return "Could not resolve the Telegram file id for this image."

    game_hint, title_hint = _parse_photo_caption_for_lookup(caption_text)

    try:
        file_info = client.get_file(file_id=file_id)
        file_path = file_info.get("file_path")
        if not isinstance(file_path, str) or not file_path:
            return "Telegram did not return a downloadable file path for this image."
        payload = client.download_file(file_path=file_path)
        suffix = Path(file_path).suffix or ".jpg"
        temp_root = Path.cwd() / ".openclaw_tmp"
        temp_root.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="wb",
            suffix=suffix,
            prefix="telegram-upload-",
            dir=temp_root,
            delete=False,
        ) as handle:
            handle.write(payload)
            local_path = Path(handle.name)
        try:
            query = TelegramPhotoQuery(
                chat_id=chat_id,
                image_path=local_path,
                caption=caption_text,
                game_hint=game_hint,
                title_hint=title_hint,
                file_id=file_id,
            )
            return photo_renderer(query)
        finally:
            try:
                local_path.unlink(missing_ok=True)
            except PermissionError:
                logger.debug("Could not remove temporary Telegram photo path=%s", local_path)
    except Exception as exc:  # pragma: no cover - network-dependent.
        logger.exception("Telegram photo handling failed chat_id=%s file_id=%s", mask_identifier(chat_id), file_id)
        return f"Image lookup failed: {exc}"


def _parse_photo_caption_for_lookup(caption: str | None) -> tuple[str | None, str | None]:
    if caption is None:
        return None, None
    content = caption.strip()
    if not content:
        return None, None
    for prefix in PHOTO_SCAN_COMMANDS:
        if content.lower().startswith(prefix):
            content = content[len(prefix):].strip()
            break
    if not content:
        return None, None
    tokens = content.split()
    if not tokens:
        return None, None
    first = tokens[0].lower()
    if first in {"pokemon", "ws"}:
        remainder = " ".join(tokens[1:]).strip()
        return first, remainder or None
    return None, content


def _extract_command_name(text: str | None) -> str | None:
    if text is None:
        return None
    content = text.strip()
    if not content or not content.startswith("/"):
        return None
    command, *_ = content.split(maxsplit=1)
    return command.split("@", 1)[0].lower()


def _extract_command_remainder(text: str | None) -> str:
    if text is None:
        return ""
    content = text.strip()
    if not content:
        return ""
    _, _, remainder = content.partition(" ")
    return remainder.strip()


def _value_or_none(parts: list[str], index: int) -> str | None:
    if index >= len(parts):
        return None
    value = parts[index].strip()
    return value or None
