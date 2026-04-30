"""Thin wrappers around price_monitor_bot commands for backwards compatibility."""

from __future__ import annotations

from price_monitor_bot.commands import (  # noqa: F401
    _lookup_with_hot_card_fallback,
    build_card_spec,
    list_reference_sources,
    seed_example_watchlist,
)
from tcg_tracker.hot_cards import TcgHotCardService
from tcg_tracker.service import TcgLookupResult, TcgPriceService


def lookup_card(
    *,
    db_path,
    game: str,
    name: str,
    item_kind: str = "card",
    card_number: str | None = None,
    rarity: str | None = None,
    set_code: str | None = None,
    set_name: str | None = None,
    aliases: tuple[str, ...] = (),
    extra_keywords: tuple[str, ...] = (),
    persist: bool = True,
    hot_card_service: TcgHotCardService | None = None,
) -> TcgLookupResult:
    service = TcgPriceService(db_path=db_path)
    spec = build_card_spec(
        game=game,
        name=name,
        item_kind=item_kind,
        card_number=card_number,
        rarity=rarity,
        set_code=set_code,
        set_name=set_name,
        aliases=aliases,
        extra_keywords=extra_keywords,
    )
    return _lookup_with_hot_card_fallback(
        service=service,
        spec=spec,
        persist=persist,
        hot_card_service=hot_card_service or TcgHotCardService(),
    )
