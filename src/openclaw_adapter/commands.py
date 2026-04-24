"""Thin wrapper — re-exports price_monitor_bot.commands for backwards compatibility."""

from price_monitor_bot.commands import (  # noqa: F401
    build_card_spec,
    list_reference_sources,
    lookup_card,
    seed_example_watchlist,
)
