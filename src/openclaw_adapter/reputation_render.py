"""Reputation-snapshot Telegram delivery: agent-backed renderer, playwright
PDF/preview artifacts, and summary formatting.

Moved out of telegram_bot.py in R2.2 (#75). telegram_bot re-imports these
names so legacy import paths and `_build_registries` registration sites are
unchanged.
"""

from __future__ import annotations

import logging
import os
import shutil
import uuid
from pathlib import Path

from assistant_runtime import AssistantSettings
from assistant_runtime.logging_utils import trim_for_log
from price_monitor_bot.bot import (
    ReputationRenderer,
    TelegramReputationDelivery,
    TelegramReputationQuery,
)
from telegram_core.transport import TelegramFileAttachment

from .reputation_agent import ensure_agent_thread
from .reputation_snapshot import (
    ReputationSnapshotResult,
    fetch_reputation_proof_document,
    request_reputation_snapshot,
)

logger = logging.getLogger(__name__)


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
