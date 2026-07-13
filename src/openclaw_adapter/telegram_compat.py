"""Compatibility re-exports for legacy ``openclaw_adapter.telegram_bot`` paths.

R2.1 (#75): these names are owned by price_monitor_bot / telegram_core and
were historically re-exported by telegram_bot.py without being used there.
They now live here so the legacy import surface is explicit and separately
testable. ``telegram_bot`` re-imports them, so both
``from openclaw_adapter.telegram_bot import X`` and
``from openclaw_adapter.telegram_compat import X`` resolve to the owning
module's object. New code should import from the owning module directly.
"""

from price_monitor_bot.bot import (
    BoardLoader,
    TelegramLookupQuery,
    TelegramReputationDelivery,
    TelegramReputationQuery,
    TelegramResearchQuery,
    build_processing_ack,
    format_liquidity_board,
    format_photo_lookup_result,
    parse_lookup_command,
    parse_reputation_snapshot_command,
)
from telegram_core.polling import handle_telegram_message
from telegram_core.transport import TelegramFileAttachment

__all__ = [
    "BoardLoader",
    "TelegramFileAttachment",
    "TelegramLookupQuery",
    "TelegramReputationDelivery",
    "TelegramReputationQuery",
    "TelegramResearchQuery",
    "build_processing_ack",
    "format_liquidity_board",
    "format_photo_lookup_result",
    "handle_telegram_message",
    "parse_lookup_command",
    "parse_reputation_snapshot_command",
]
