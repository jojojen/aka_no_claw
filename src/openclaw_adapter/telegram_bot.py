"""Telegram bot orchestration — bridges AssistantSettings to price_monitor_bot.bot."""

from __future__ import annotations

import logging
import os
import shutil
import threading
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
    TelegramCommandProcessor as _BaseTelegramCommandProcessor,
)
from price_monitor_bot.watch_monitor import ensure_monitor as _ensure_watch_monitor
from tcg_tracker.image_lookup import TcgVisionSettings

from .natural_language import build_telegram_natural_language_router_from_settings
from .opportunity_agent import format_opportunity_status
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


class TelegramCommandProcessor(_BaseTelegramCommandProcessor):
    """OpenClaw compatibility wrapper around the reusable Telegram processor."""

    def __init__(
        self,
        *,
        settings: AssistantSettings | None = None,
        allowed_chat_ids: frozenset[str] | None = None,
        **kwargs,
    ) -> None:
        if allowed_chat_ids is None and settings is not None and settings.openclaw_telegram_chat_id:
            allowed_chat_ids = frozenset({settings.openclaw_telegram_chat_id})
        super().__init__(allowed_chat_ids=allowed_chat_ids, **kwargs)


def default_lookup_renderer(settings: AssistantSettings) -> LookupRenderer:
    return _base_default_lookup_renderer(db_path=settings.monitor_db_path)


def default_photo_renderer(settings: AssistantSettings) -> PhotoLookupRenderer:
    return _base_default_photo_renderer(
        db_path=settings.monitor_db_path,
        tesseract_path=settings.openclaw_tesseract_path,
        tessdata_dir=settings.openclaw_tessdata_dir,
        vision_settings=TcgVisionSettings(
            backend=settings.openclaw_local_vision_backend or "",
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
        browser = playwright.chromium.launch(**_chromium_launch_options())
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


def _chromium_launch_options() -> dict[str, object]:
    options: dict[str, object] = {"headless": True}
    executable_path = _resolve_chromium_executable()
    if executable_path:
        options["executable_path"] = executable_path
    return options


def _resolve_chromium_executable() -> str | None:
    configured = os.getenv("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH")
    if configured:
        return configured
    for candidate in ("chromium", "chromium-browser", "google-chrome-stable"):
        resolved = shutil.which(candidate)
        if resolved:
            return resolved
    return None


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
    quality = proof_document.get("quality", {}) if isinstance(proof_document, dict) else {}

    display_name = subject.get("display_name") if isinstance(subject, dict) else None
    captured_at = proof_document.get("captured_at") if isinstance(proof_document, dict) else None
    total_reviews = metrics.get("total_reviews") if isinstance(metrics, dict) else None
    listing_count = metrics.get("listing_count") if isinstance(metrics, dict) else None
    followers_count = metrics.get("followers_count") if isinstance(metrics, dict) else None
    following_count = metrics.get("following_count") if isinstance(metrics, dict) else None
    as_seller = quality.get("as_seller") if isinstance(quality, dict) else None
    as_buyer = quality.get("as_buyer") if isinstance(quality, dict) else None
    overall = quality.get("overall") if isinstance(quality, dict) else None

    lines = ["信譽快照已就緒", action_text]
    if display_name:
        lines.append(f"賣家：{display_name}")

    # Overall review count (from metrics, covers full history)
    meta_bits = []
    if total_reviews is not None:
        meta_bits.append(f"評價 {total_reviews}")
    if listing_count is not None:
        meta_bits.append(f"刊登 {listing_count}")
    if followers_count is not None:
        meta_bits.append(f"追蹤者 {followers_count}")
    if following_count is not None:
        meta_bits.append(f"追蹤中 {following_count}")
    if meta_bits:
        lines.append(" / ".join(meta_bits))

    # Buyer / seller breakdown (from quality, based on captured review entries)
    if isinstance(as_seller, dict) and (as_seller.get("positive") or as_seller.get("negative")):
        pos = as_seller.get("positive") or 0
        neg = as_seller.get("negative") or 0
        rate = as_seller.get("rate")
        rate_str = f"，好評率 {rate}%" if rate is not None else ""
        lines.append(f"身為賣家：好評 {pos} / 差評 {neg}{rate_str}")
    if isinstance(as_buyer, dict) and (as_buyer.get("positive") or as_buyer.get("negative")):
        pos = as_buyer.get("positive") or 0
        neg = as_buyer.get("negative") or 0
        rate = as_buyer.get("rate")
        rate_str = f"，好評率 {rate}%" if rate is not None else ""
        lines.append(f"身為買家：好評 {pos} / 差評 {neg}{rate_str}")
    elif isinstance(overall, dict) and as_seller is None and as_buyer is None:
        # Fallback: only overall quality, no role breakdown available
        rate = overall.get("rate")
        if rate is not None:
            lines.append(f"整體好評率：{rate}%")

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
    from .sns_tools import _start_sns_monitor

    token = require_telegram_token(settings)
    watch_db = _bootstrap_watch_db(settings)
    _start_watch_monitor(settings=settings, watch_db=watch_db, token=token)
    sns_db, sns_buzz_fn = _start_sns_monitor(settings=settings, token=token, ssl_context=build_ssl_context(settings))
    return _base_run_telegram_polling(
        token=token,
        lookup_renderer=lookup_renderer,
        board_loader=board_loader,
        catalog_renderer=catalog_renderer,
        photo_renderer=photo_renderer or default_photo_renderer(settings),
        reputation_renderer=default_reputation_renderer(settings),
        natural_language_router=build_telegram_natural_language_router_from_settings(settings),
        ssl_context=build_ssl_context(settings),
        allowed_chat_ids=frozenset(settings.openclaw_telegram_chat_ids),
        status_renderer=lambda: _build_status_text(settings),
        opportunity_status_renderer=lambda: format_opportunity_status(settings),
        watch_db=watch_db,
        sns_db=sns_db,
        sns_buzz_fn=sns_buzz_fn,
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

    def do_snapshot(notification_chat_id: str, urls: list[str]) -> None:
        resolved_chat = notification_chat_id if notification_chat_id and notification_chat_id != "dashboard" else chat_id
        if not resolved_chat:
            logger.warning("Auto-snapshot: no chat_id, skipping")
            return
        bot_client = TelegramBotClient(token, ssl_context=ssl_ctx)
        try:
            bot_client.send_message(
                chat_id=resolved_chat,
                text=f"正在為 {len(urls)} 筆新商品建立賣家信譽快照，請稍候…",
            )
        except Exception:
            logger.warning("Auto-snapshot: failed to send ack message")

        def _run() -> None:
            for url in urls:
                try:
                    result = request_reputation_snapshot(settings=settings, query_url=url)
                    proof_document = None
                    if result.proof_id:
                        try:
                            proof_document = fetch_reputation_proof_document(
                                settings=settings, proof_id=result.proof_id
                            )
                        except Exception:
                            logger.exception("Auto-snapshot: proof fetch failed proof_id=%s", result.proof_id)
                    pdf_path, preview_path = render_reputation_snapshot_artifacts(
                        settings=settings, result=result
                    )
                    summary = format_reputation_snapshot_delivery_text(result, proof_document)
                    c = TelegramBotClient(token, ssl_context=ssl_ctx)
                    c.send_message(chat_id=resolved_chat, text=summary)
                    c.send_document(
                        chat_id=resolved_chat,
                        document_path=pdf_path,
                        caption="信譽快照 PDF",
                    )
                    c.send_photo(
                        chat_id=resolved_chat,
                        photo_path=preview_path,
                        caption="信譽快照預覽",
                    )
                    for p in (pdf_path, preview_path):
                        try:
                            p.unlink()
                        except Exception:
                            pass
                    logger.info("Auto-snapshot: completed url=%s proof_id=%s", url, result.proof_id)
                except Exception:
                    logger.exception("Auto-snapshot: failed url=%s", url)

        threading.Thread(target=_run, name="auto-snapshot", daemon=True).start()

    monitor, started = _ensure_watch_monitor(
        db_path=watch_db.path,
        notify_fn=notify,
        snapshot_fn=do_snapshot,
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
    allowed_chats = ", ".join(settings.openclaw_telegram_chat_ids) if settings.openclaw_telegram_chat_ids else "not restricted"
    configured = _load_status_configuration_snapshot()
    tesseract = settings.openclaw_tesseract_path or configured.get("OPENCLAW_TESSERACT_PATH") or "PATH lookup"
    tessdata = settings.openclaw_tessdata_dir or configured.get("OPENCLAW_TESSDATA_DIR") or "auto"
    text_backend = (settings.openclaw_local_text_backend or "").strip().lower() or "none"
    text_model = _select_router_model_for_status(settings)
    configured_text_backend = configured.get("OPENCLAW_LOCAL_TEXT_BACKEND") or "none"
    configured_text_model = configured.get("OPENCLAW_LOCAL_TEXT_MODEL")
    configured_text_timeout = configured.get("OPENCLAW_LOCAL_TEXT_TIMEOUT_SECONDS") or str(settings.openclaw_local_text_timeout_seconds)
    configured_text_endpoint = configured.get("OPENCLAW_LOCAL_TEXT_ENDPOINT") or settings.openclaw_local_text_endpoint
    vision_backend = (settings.openclaw_local_vision_backend or "").strip().lower() or "none"
    vision_models = _split_model_list(settings.openclaw_local_vision_model)
    configured_vision_backend = configured.get("OPENCLAW_LOCAL_VISION_BACKEND") or "none"
    configured_vision_models = _split_model_list(configured.get("OPENCLAW_LOCAL_VISION_MODEL"))
    configured_vision_timeout = configured.get("OPENCLAW_LOCAL_VISION_TIMEOUT_SECONDS") or str(settings.openclaw_local_vision_timeout_seconds)
    configured_vision_endpoint = configured.get("OPENCLAW_LOCAL_VISION_ENDPOINT") or settings.openclaw_local_vision_endpoint
    reputation_host = settings.reputation_agent_server_url or "not configured"
    return "\n".join(
        [
            "OpenClaw Telegram status",
            f"env: {settings.monitor_env}",
            f"db: {settings.monitor_db_path}",
            f"allowed chats: {allowed_chats}",
            "",
            "Features",
            _format_status_feature_line(
                "text routing",
                active_backend=text_backend,
                active_model_display=_format_model_display(text_model),
                configured_backend=configured_text_backend,
                configured_model_display=_format_model_display(configured_text_model),
                timeout_seconds=configured_text_timeout,
                endpoint=configured_text_endpoint,
            ),
            _format_status_feature_line(
                "image scan vision",
                active_backend=vision_backend,
                active_model_display=_format_model_list_display(vision_models),
                configured_backend=configured_vision_backend,
                configured_model_display=_format_model_list_display(configured_vision_models),
                timeout_seconds=configured_vision_timeout,
                endpoint=configured_vision_endpoint,
            ),
            f"image scan OCR: engine=tesseract | binary={tesseract} | tessdata={tessdata}",
            "price lookup / trend / watch: model=none | source-driven matching and pricing rules",
            f"reputation snapshot: model=none | server={reputation_host} | poll={settings.reputation_agent_poll_secs}s | renderer=playwright chromium",
            (
                "opportunity agent: "
                f"{'enabled' if settings.opportunity_agent_enabled else 'disabled'}"
                f" | db={settings.opportunity_db_path}"
                f" | interval={settings.opportunity_interval_seconds}s"
                f" | llm_timeout={settings.opportunity_llm_timeout_seconds}s"
                f" | sns_lookback={settings.opportunity_sns_lookback_hours}h"
            ),
        ]
    )


def _select_router_model_for_status(settings: AssistantSettings) -> str | None:
    from .natural_language import _select_router_model

    return _select_router_model(settings)


def _split_model_list(raw_models: str | None) -> tuple[str, ...]:
    if not raw_models:
        return ()
    return tuple(part.strip() for part in raw_models.split(",") if part.strip())


def _format_model_list_display(models: tuple[str, ...]) -> str:
    if not models:
        return "none"
    return ", ".join(_format_model_display(model) for model in models)


def _format_model_display(model: str | None) -> str:
    if not model:
        return "none"
    size = _extract_model_size(model)
    if size is None:
        return model
    return f"{model} ({size})"


def _extract_model_size(model: str) -> str | None:
    for segment in reversed(model.split(":")):
        candidate = segment.strip()
        if not candidate:
            continue
        lowered = candidate.lower()
        if lowered.endswith("b") and any(ch.isdigit() for ch in lowered):
            return lowered.upper()
    return None


def _format_feature_runtime(backend: str, model_display: str) -> str:
    if backend == "none":
        return f"disabled / {model_display}"
    return f"{backend} / {model_display}"


def _format_status_feature_line(
    label: str,
    *,
    active_backend: str,
    active_model_display: str,
    configured_backend: str,
    configured_model_display: str,
    timeout_seconds: str,
    endpoint: str,
) -> str:
    active_runtime = _format_feature_runtime(active_backend, active_model_display)
    configured_runtime = _format_feature_runtime(configured_backend, configured_model_display)
    if active_runtime == configured_runtime:
        runtime_text = active_runtime
    else:
        runtime_text = f"active={active_runtime} | configured={configured_runtime}"
    return f"{label}: {runtime_text} | timeout={timeout_seconds}s | endpoint={endpoint}"


def _load_status_configuration_snapshot() -> dict[str, str]:
    merged: dict[str, str] = {}
    for file_name in (".env.example", ".env"):
        merged.update(_read_env_values(Path.cwd() / file_name))
    return merged


def _read_env_values(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip("'\"")
    return values
