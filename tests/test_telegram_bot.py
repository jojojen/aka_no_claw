from __future__ import annotations

from datetime import datetime, timezone

from assistant_runtime import AssistantSettings
from tcg_tracker.hot_cards import HotCardBoard, HotCardEntry, HotCardReference

from openclaw_adapter.telegram_bot import (
    TelegramCommandProcessor,
    TelegramLookupQuery,
    format_liquidity_board,
    parse_lookup_command,
)


def _stub_board() -> HotCardBoard:
    return HotCardBoard(
        game="pokemon",
        label="Pokemon Liquidity Board",
        methodology="stub methodology",
        generated_at=datetime.now(timezone.utc),
        items=(
            HotCardEntry(
                game="pokemon",
                rank=1,
                title="ピカチュウex",
                price_jpy=99800,
                thumbnail_url="https://example.com/pikachu.jpg",
                card_number="132/106",
                rarity="SAR",
                set_code="sv08",
                listing_count=5,
                best_ask_jpy=99800,
                best_bid_jpy=80000,
                previous_bid_jpy=50000,
                bid_ask_ratio=0.8016,
                buy_support_score=90.08,
                momentum_boost_score=6.0,
                buy_signal_label="priceup",
                hot_score=88.2,
                attention_score=41.7,
                social_post_count=3,
                social_engagement_count=120,
                notes=("stub note",),
                is_graded=False,
                references=(HotCardReference(label="Ranking Source", url="https://example.com/rank"),),
            ),
        ),
    )


def test_parse_lookup_command_supports_pipe_format() -> None:
    query = parse_lookup_command("pokemon | ピカチュウex | 132/106 | SAR | sv08")

    assert query == TelegramLookupQuery(
        game="pokemon",
        name="ピカチュウex",
        card_number="132/106",
        rarity="SAR",
        set_code="sv08",
    )


def test_parse_lookup_command_supports_simple_format() -> None:
    query = parse_lookup_command("ws “夏の思い出”蒼(サイン入り)")

    assert query == TelegramLookupQuery(
        game="ws",
        name="“夏の思い出”蒼(サイン入り)",
    )


def test_command_processor_restricts_unconfigured_chat() -> None:
    settings = AssistantSettings(openclaw_telegram_chat_id="999")
    processor = TelegramCommandProcessor(
        settings=settings,
        lookup_renderer=lambda query: query.name,
        board_loader=lambda: (_stub_board(),),
        catalog_renderer=lambda: "catalog",
    )

    assert processor.build_reply(chat_id="123", text="/ping") is None


def test_command_processor_handles_lookup_and_liquidity() -> None:
    settings = AssistantSettings(openclaw_telegram_chat_id="123")
    processor = TelegramCommandProcessor(
        settings=settings,
        lookup_renderer=lambda query: f"{query.game}:{query.name}:{query.card_number}",
        board_loader=lambda: (_stub_board(),),
        catalog_renderer=lambda: "catalog",
    )

    lookup_reply = processor.build_reply(chat_id="123", text="/lookup pokemon | ピカチュウex | 132/106")
    liquidity_reply = processor.build_reply(chat_id="123", text="/liquidity pokemon")

    assert lookup_reply == "pokemon:ピカチュウex:132/106"
    assert "Pokemon Liquidity Board" in liquidity_reply
    assert "bid ¥80,000" in liquidity_reply
    assert "ask ¥99,800" in liquidity_reply
    assert "boost 6.00" in liquidity_reply


def test_format_liquidity_board_includes_reference_url() -> None:
    text = format_liquidity_board(_stub_board(), limit=1)

    assert "https://example.com/rank" in text
    assert "liq 88.20" in text
    assert "attn 41.70" in text
    assert "support 90.08" in text
    assert "buy-up" in text
