"""Telegram bot orchestration — bridges AssistantSettings to price_monitor_bot.bot."""

from __future__ import annotations

import logging
import uuid
from pathlib import Path

from assistant_runtime import AssistantSettings, build_ssl_context
from assistant_runtime.logging_utils import trim_for_log

from market_monitor.storage import MonitorDatabase
from price_monitor_bot.bot import (  # noqa: F401
    BoardLoader,
    CatalogRenderer,
    LookupRenderer,
    PhotoLookupRenderer,
    ReputationRenderer,
    TelegramBotClient,
    TelegramCommandProcessor,
    TelegramFileAttachment,
    TelegramLookupQuery,
    TelegramPhotoQuery,
    TelegramReputationDelivery,
    TelegramReputationQuery,
    TelegramTextReplyPlan,
    build_processing_ack,
    default_board_loader as _base_default_board_loader,
    default_lookup_renderer as _base_default_lookup_renderer,
    default_photo_renderer as _base_default_photo_renderer,
    format_liquidity_board,
    format_photo_lookup_result,
    handle_telegram_message,
    parse_lookup_command,
    parse_reputation_snapshot_command,
    run_telegram_polling as _base_run_telegram_polling,
    send_telegram_test_message as _base_send_telegram_test_message,
)
from price_monitor_bot.watch_monitor import ensure_monitor as _ensure_watch_monitor
from tcg_tracker.image_lookup import TcgVisionSettings

from .natural_language import build_telegram_natural_language_router_from_settings
from .reputation_agent import ensure_agent_thread
from .reputation_snapshot import (
    ReputationSnapshotResult,
    fetch_reputation_proof_document,
    request_reputation_snapshot,
)

logger = logging.getLogger(__name__)

PRICE_LOOKUP_COMMANDS = {"/lookup", "/price"}
TREND_BOARD_COMMANDS = {"/trend", "/trending", "/hot", "/heat", "/liquidity"}
PHOTO_SCAN_COMMANDS = {"/scan", "/image", "/photo"}
REPUTATION_SNAPSHOT_COMMANDS = {"/snapshot", "/proof", "/repcheck", "/reputation"}
HEAVY_COMMANDS = PRICE_LOOKUP_COMMANDS | TREND_BOARD_COMMANDS | REPUTATION_SNAPSHOT_COMMANDS


def default_lookup_renderer(settings: AssistantSettings) -> LookupRenderer:
    return _base_default_lookup_renderer(db_path=settings.monitor_db_path)


def default_photo_renderer(settings: AssistantSettings) -> PhotoLookupRenderer:
    return _base_default_photo_renderer(
        db_path=settings.monitor_db_path,
        tesseract_path=settings.openclaw_tesseract_path,
        tessdata_dir=settings.openclaw_tessdata_dir,
        vision_settings=TcgVisionSettings(
            backend=(settings.openclaw_local_vision_backend or "ollama"),
            endpoint=settings.openclaw_local_vision_endpoint,
            model=settings.openclaw_local_vision_model,
            timeout_seconds=settings.openclaw_local_vision_timeout_seconds,
            ssl_context=build_ssl_context(settings),
        ),
    )


def default_board_loader(settings: AssistantSettings | None = None) -> tuple:
    return _base_default_board_loader(ssl_context=build_ssl_context(settings) if settings else None)


def default_reputation_renderer(settings: AssistantSettings) -> ReputationRenderer:
    def render(query: TelegramReputationQuery) -> TelegramReputationDelivery:
        logger.info("Telegram reputation snapshot requested query_url=%s", trim_for_log(query.query_url, limit=240))
        thread, started_now = ensure_agent_thread(
            server_url=settings.reputation_agent_server_url,
            api_key=settings.reputation_agent_admin_token or "",
            poll_secs=settings.reputation_agent_poll_secs,
        )
        logger.info(
            "Telegram reputation agent ready started_now=%s thread_name=%s alive=%s",
            started_now,
            thread.name,
            thread.is_alive(),
        )
        result = request_reputation_snapshot(settings=settings, query_url=query.query_url)
        logger.info(
            "Telegram reputation snapshot completed query_url=%s proof_id=%s reused=%s",
            trim_for_log(query.query_url, limit=240),
            result.proof_id,
            result.reused,
        )
        proof_document = None
        if result.proof_id is not None:
            try:
                proof_document = fetch_reputation_proof_document(settings=settings, proof_id=result.proof_id)
            except Exception:
                logger.exception("Telegram reputation proof fetch failed proof_id=%s", result.proof_id)
        pdf_path, preview_path = render_reputation_snapshot_artifacts(settings=settings, result=result)
        return TelegramReputationDelivery(
            summary_text=format_reputation_snapshot_delivery_text(result, proof_document),
            attachments=(
                TelegramFileAttachment(kind="document", path=pdf_path, caption="Reputation snapshot PDF"),
                TelegramFileAttachment(kind="photo", path=preview_path, caption="Reputation snapshot preview"),
            ),
            cleanup_paths=(pdf_path, preview_path),
        )

    return render


def render_reputation_snapshot_artifacts(
    *,
    settings: AssistantSettings,
    result: ReputationSnapshotResult,
) -> tuple[Path, Path]:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:  # pragma: no cover - environment-dependent.
        raise RuntimeError("playwright is not installed — run: pip install playwright && playwright install chromium") from exc

    proof_id = result.proof_id or f"proof_{uuid.uuid4().hex[:12]}"
    temp_root = Path.cwd() / ".openclaw_tmp" / "reputation_snapshot"
    temp_root.mkdir(parents=True, exist_ok=True)
    pdf_path = temp_root / f"{proof_id}.pdf"
    preview_path = temp_root / f"{proof_id}.png"

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context(
            locale="ja-JP",
            viewport={"width": 1400, "height": 1800},
            ignore_https_errors=settings.openclaw_tls_insecure_skip_verify,
        )
        page = context.new_page()
        page.goto(result.proof_url, wait_until="networkidle", timeout=60000)
        page.emulate_media(media="screen")
        page.pdf(
            path=str(pdf_path),
            format="A4",
            print_background=True,
            margin={"top": "12mm", "right": "10mm", "bottom": "12mm", "left": "10mm"},
        )
        page.screenshot(path=str(preview_path), full_page=False)
        context.close()
        browser.close()

    return pdf_path, preview_path


def format_reputation_snapshot_result(result: ReputationSnapshotResult) -> str:
    action_text = "沿用既有快照" if result.reused else "已建立新快照"
    lines = [
        "信譽快照已就緒",
        action_text,
        result.proof_url,
    ]
    if result.proof_id:
        lines.insert(2, f"proof_id: {result.proof_id}")
    return "\n".join(lines)


def format_reputation_snapshot_delivery_text(
    result: ReputationSnapshotResult,
    proof_document: dict[str, object] | None,
) -> str:
    action_text = "沿用既有快照" if result.reused else "已建立新快照"
    subject = proof_document.get("subject", {}) if isinstance(proof_document, dict) else {}
    metrics = proof_document.get("metrics", {}) if isinstance(proof_document, dict) else {}

    display_name = subject.get("display_name") if isinstance(subject, dict) else None
    captured_at = proof_document.get("captured_at") if isinstance(proof_document, dict) else None
    total_reviews = metrics.get("total_reviews") if isinstance(metrics, dict) else None
    listing_count = metrics.get("listing_count") if isinstance(metrics, dict) else None
    followers_count = metrics.get("followers_count") if isinstance(metrics, dict) else None
    following_count = metrics.get("following_count") if isinstance(metrics, dict) else None

    lines = ["信譽快照已就緒", action_text]
    if display_name:
        lines.append(f"賣家：{display_name}")

    metrics_bits = []
    if total_reviews is not None:
        metrics_bits.append(f"評價 {total_reviews}")
    if listing_count is not None:
        metrics_bits.append(f"刊登 {listing_count}")
    if followers_count is not None:
        metrics_bits.append(f"追蹤者 {followers_count}")
    if following_count is not None:
        metrics_bits.append(f"追蹤中 {following_count}")
    if metrics_bits:
        lines.append(" / ".join(metrics_bits))

    if captured_at:
        lines.append(f"快照時間：{captured_at}")
    if result.proof_id:
        lines.append(f"proof_id: {result.proof_id}")
    lines.append("已附上 PDF 與預覽圖，可直接在手機查看。")
    lines.append(result.proof_url)
    return "\n".join(lines)


def run_telegram_polling(
    *,
    settings: AssistantSettings,
    lookup_renderer: LookupRenderer,
    board_loader,
    catalog_renderer: CatalogRenderer,
    photo_renderer: PhotoLookupRenderer | None = None,
    poll_timeout: int = 20,
    notify_startup: bool = False,
    drop_pending_updates: bool = True,
) -> int:
    token = require_telegram_token(settings)
    watch_db = _bootstrap_watch_db(settings)
    _start_watch_monitor(settings=settings, watch_db=watch_db, token=token)
    return _base_run_telegram_polling(
        token=token,
        lookup_renderer=lookup_renderer,
        board_loader=board_loader,
        catalog_renderer=catalog_renderer,
        photo_renderer=photo_renderer or default_photo_renderer(settings),
        reputation_renderer=default_reputation_renderer(settings),
        natural_language_router=build_telegram_natural_language_router_from_settings(settings),
        ssl_context=build_ssl_context(settings),
        allowed_chat_id=settings.openclaw_telegram_chat_id,
        status_renderer=lambda: _build_status_text(settings),
        watch_db=watch_db,
        poll_timeout=poll_timeout,
        notify_startup=notify_startup,
        drop_pending_updates=drop_pending_updates,
    )


def _bootstrap_watch_db(settings: AssistantSettings) -> MonitorDatabase:
    db = MonitorDatabase(settings.monitor_db_path)
    db.bootstrap()
    return db


def _start_watch_monitor(
    *,
    settings: AssistantSettings,
    watch_db: MonitorDatabase,
    token: str,
) -> None:
    chat_id = settings.openclaw_telegram_chat_id
    if not chat_id:
        logger.warning("Mercari watch monitor: no OPENCLAW_TELEGRAM_CHAT_ID set, notifications will be skipped")

    ssl_ctx = build_ssl_context(settings)

    def notify(notification_chat_id: str, text: str) -> None:
        resolved_chat = notification_chat_id if notification_chat_id and notification_chat_id != "dashboard" else chat_id
        if not resolved_chat:
            logger.warning("Mercari watch notify: no chat_id, dropping message")
            return
        client = TelegramBotClient(token, ssl_context=ssl_ctx)
        client.send_message(chat_id=resolved_chat, text=text)

    monitor, started = _ensure_watch_monitor(
        db_path=watch_db.path,
        notify_fn=notify,
        interval_seconds=60,
    )
    logger.info("Mercari watch monitor started=%s running=%s", started, monitor.is_running())
    if started:
        print("[watch-monitor] Mercari watch monitor started (interval=60s)")


def send_telegram_test_message(*, settings: AssistantSettings, message: str) -> int:
    token = require_telegram_token(settings)
    chat_id = require_telegram_chat_id(settings)
    return _base_send_telegram_test_message(
        token=token,
        chat_id=chat_id,
        message=message,
        ssl_context=build_ssl_context(settings),
    )


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


def _build_status_text(settings: AssistantSettings) -> str:
    allowed_chat = settings.openclaw_telegram_chat_id or "not restricted"
    tesseract = settings.openclaw_tesseract_path or "PATH lookup"
    tessdata = settings.openclaw_tessdata_dir or "auto"
    return "\n".join(
        [
            "OpenClaw Telegram status",
            f"env: {settings.monitor_env}",
            f"db: {settings.monitor_db_path}",
            f"allowed chat: {allowed_chat}",
            f"tesseract: {tesseract}",
            f"tessdata: {tessdata}",
        ]
    )
