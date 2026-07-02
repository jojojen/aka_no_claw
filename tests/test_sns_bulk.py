"""SNS bulk filter/schedule preview → confirm/cancel e2e tests.

Migrated from price_monitor_bot/tests/test_telegram_bot.py when the snsbulk
flow moved to openclaw_adapter.sns_commands (telegram_core extraction P3).
Live SnsDatabase round-trips for the bulk e2e path.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from telegram_core.polling import handle_telegram_callback_query

from openclaw_adapter.telegram_bot import TelegramCommandProcessor

try:
    from sns_monitor.storage import SnsDatabase as _RealSnsDatabase  # noqa: F401
    _HAVE_SNS_MONITOR = True
except ImportError:
    _HAVE_SNS_MONITOR = False

pytestmark = pytest.mark.skipif(
    not _HAVE_SNS_MONITOR, reason="sns_monitor package not installed in this venv"
)


class FakeTelegramClient:
    def __init__(self) -> None:
        self.sent_messages: list[str] = []
        self.edited_messages: list[dict[str, object]] = []
        self.answered_callbacks: list[dict[str, object]] = []

    def send_message(
        self,
        *,
        chat_id: str | int,
        text: str,
        reply_markup: dict[str, object] | None = None,
    ) -> dict[str, object]:
        self.sent_messages.append(text)
        return {"chat_id": str(chat_id), "text": text, "reply_markup": reply_markup}

    def edit_message_text(
        self,
        *,
        chat_id: str | int,
        message_id: int,
        text: str,
        reply_markup: dict[str, object] | None = None,
    ) -> dict[str, object]:
        record = {
            "chat_id": str(chat_id),
            "message_id": message_id,
            "text": text,
            "reply_markup": reply_markup,
        }
        self.edited_messages.append(record)
        return record

    def answer_callback_query(
        self,
        *,
        callback_query_id: str,
        text: str | None = None,
        show_alert: bool = False,
    ) -> dict[str, object]:
        record = {
            "callback_query_id": callback_query_id,
            "text": text,
            "show_alert": show_alert,
        }
        self.answered_callbacks.append(record)
        return record


def _make_bulk_processor_with_tcg_rules(tmp_path: Path) -> tuple[TelegramCommandProcessor, object]:
    from sns_monitor.models import AccountWatch
    from sns_monitor.storage import SnsDatabase

    db = SnsDatabase(tmp_path / "sns.sqlite3")
    db.bootstrap()
    for name, domains in [
        ("poke_news", ("pokemon", "tcg")),
        ("yugioh_jp", ("yugioh",)),
        ("politics_bot", ("politic",)),  # should NOT be picked up
    ]:
        db.save_watch_rule(
            AccountWatch(
                rule_id=SnsDatabase._watch_rule_id("account", name),
                screen_name=name,
                user_id=None,
                label=f"@{name}",
                include_keywords=(),
                domains=domains,
                enabled=True,
                schedule_minutes=15,
                chat_id="0",
                last_checked_at=None,
            )
        )
    proc = TelegramCommandProcessor(
        allowed_chat_ids=frozenset({"123"}),
        lookup_renderer=lambda query: query.name,
        board_loader=lambda: (),
        catalog_renderer=lambda: "catalog",
        sns_db=db,
    )
    return proc, db


def test_sns_bulk_add_filter_preview_lists_affected_and_sets_pending(tmp_path) -> None:
    processor, _ = _make_bulk_processor_with_tcg_rules(tmp_path)

    plan = processor._build_sns_bulk_add_filter_plan(
        chat_id="123", target_domain="tcg", keywords=("抽選",)
    )

    assert "找到 2 個" in plan.reply
    assert "@poke_news" in plan.reply
    assert "@yugioh_jp" in plan.reply
    assert "@politics_bot" not in plan.reply
    kb = plan.reply_markup
    assert kb is not None
    flat = [b for row in kb["inline_keyboard"] for b in row]
    assert {b["callback_data"] for b in flat} == {"bulk:c", "bulk:x"}
    # Pending state must be installed.
    pending = processor.get_pending_sns_bulk_update("123")
    assert pending is not None
    assert pending.bulk_target_domain == "tcg"
    assert pending.keywords == ("抽選",)
    assert len(pending.affected_rule_ids) == 2


def test_sns_bulk_add_filter_confirm_callback_updates_db(tmp_path) -> None:
    processor, db = _make_bulk_processor_with_tcg_rules(tmp_path)
    # Set up pending via the preview builder.
    processor._build_sns_bulk_add_filter_plan(
        chat_id="123", target_domain="tcg", keywords=("抽選",)
    )
    client = FakeTelegramClient()

    handle_telegram_callback_query(
        client=client,
        processor=processor,
        callback_query={
            "id": "cbq-bulk-c",
            "data": "bulk:c",
            "message": {
                "message_id": 100,
                "chat": {"id": "123"},
                "text": "🎯 找到 2 個 tcg 相關帳號…",
            },
        },
    )

    # Both TCG rules now have the new keyword; politics_bot is untouched.
    for handle in ("poke_news", "yugioh_jp"):
        rule = db.get_watch_rule(_RealSnsDatabase._watch_rule_id("account", handle))
        assert rule.include_keywords == ("抽選",)
    politics = db.get_watch_rule(_RealSnsDatabase._watch_rule_id("account", "politics_bot"))
    assert politics.include_keywords == ()
    assert "✓ 已修改 2 個帳號" in client.edited_messages[0]["text"]
    assert processor.get_pending_sns_bulk_update("123") is None


def test_sns_bulk_add_filter_cancel_callback_leaves_db_untouched(tmp_path) -> None:
    processor, db = _make_bulk_processor_with_tcg_rules(tmp_path)
    processor._build_sns_bulk_add_filter_plan(
        chat_id="123", target_domain="tcg", keywords=("抽選",)
    )
    client = FakeTelegramClient()

    handle_telegram_callback_query(
        client=client,
        processor=processor,
        callback_query={
            "id": "cbq-bulk-x",
            "data": "bulk:x",
            "message": {
                "message_id": 100,
                "chat": {"id": "123"},
                "text": "🎯 找到 2 個 tcg 相關帳號…",
            },
        },
    )

    for handle in ("poke_news", "yugioh_jp", "politics_bot"):
        rule = db.get_watch_rule(_RealSnsDatabase._watch_rule_id("account", handle))
        assert rule.include_keywords == ()
    assert "已取消" in client.edited_messages[0]["text"]
    assert processor.get_pending_sns_bulk_update("123") is None


def test_sns_bulk_add_filter_callback_with_no_pending_shows_expired_toast(tmp_path) -> None:
    processor, _ = _make_bulk_processor_with_tcg_rules(tmp_path)
    # No pending — user is tapping a stale button.
    client = FakeTelegramClient()

    handle_telegram_callback_query(
        client=client,
        processor=processor,
        callback_query={
            "id": "cbq-bulk-stale",
            "data": "bulk:c",
            "message": {
                "message_id": 100,
                "chat": {"id": "123"},
                "text": "old preview text",
            },
        },
    )

    assert client.answered_callbacks[0]["text"] == "操作已過期，請重新輸入"
    # The message gets edited with the expired marker, but no DB write.
    assert "已過期" in client.edited_messages[0]["text"]
