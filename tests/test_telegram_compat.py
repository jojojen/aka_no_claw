"""R2.1 (#75): legacy import-path compatibility.

telegram_compat owns the names telegram_bot re-exported without using.
Both the legacy path (telegram_bot) and the compat module must resolve
to the owning module's object, so consumers can migrate incrementally
with zero behavior change.
"""

import price_monitor_bot.bot as price_bot
import telegram_core.polling as core_polling
from openclaw_adapter import telegram_bot, telegram_compat

PRICE_OWNED = (
    "BoardLoader",
    "TelegramLookupQuery",
    "TelegramResearchQuery",
    "build_processing_ack",
    "format_liquidity_board",
    "format_photo_lookup_result",
    "parse_lookup_command",
    "parse_reputation_snapshot_command",
)
CORE_OWNED = ("handle_telegram_message",)


def test_compat_names_resolve_to_owning_module():
    for name in PRICE_OWNED:
        assert getattr(telegram_compat, name) is getattr(price_bot, name)
    for name in CORE_OWNED:
        assert getattr(telegram_compat, name) is getattr(core_polling, name)


def test_legacy_telegram_bot_paths_still_work():
    for name in PRICE_OWNED + CORE_OWNED:
        assert getattr(telegram_bot, name) is getattr(telegram_compat, name)


def test_compat_all_is_complete_and_sorted():
    assert set(telegram_compat.__all__) == set(PRICE_OWNED) | set(CORE_OWNED)
    assert list(telegram_compat.__all__) == sorted(telegram_compat.__all__)
