from __future__ import annotations

from tcg_tracker.hot_cards import (
    TcgHotCardService,
    _ParsedHotItem,
    _parse_cardrush_text,
    _parse_magi_text,
)
from tcg_tracker.catalog import TcgCardSpec


def test_parse_cardrush_text_extracts_core_fields() -> None:
    parsed = _parse_cardrush_text(
        "〔状態A-〕ピカチュウex【SAR】{234/193} [ [状態A-]M2a ] 59,800円 (税込) 在庫数 5枚",
        detail_url="https://www.cardrush-pokemon.jp/product/123",
        board_url="https://www.cardrush-pokemon.jp/product-group/22?sort=rank&num=100",
    )

    assert parsed is not None
    assert parsed.title == "ピカチュウex"
    assert parsed.card_number == "234/193"
    assert parsed.rarity == "SAR"
    assert parsed.set_code == "m2a"
    assert parsed.price_jpy == 59800
    assert parsed.listing_count == 5
    assert parsed.condition == "状態A-"


def test_parse_magi_text_extracts_ws_card_fields() -> None:
    parsed = _parse_magi_text(
        "“夏の思い出”蒼(サイン入り) SP SMP/W60-051SP ¥ 22,800 ~ 出品数 1",
        detail_url="https://magi.camp/products/123",
        board_url="https://magi.camp/series/7/products",
    )

    assert parsed is not None
    assert parsed.title == "“夏の思い出”蒼(サイン入り)"
    assert parsed.rarity == "SP"
    assert parsed.card_number == "SMP/W60-051SP"
    assert parsed.set_code == "smp"
    assert parsed.price_jpy == 22800
    assert parsed.listing_count == 1
    assert parsed.is_graded is False


def test_parse_magi_text_handles_codes_with_letter_prefix_after_hyphen() -> None:
    parsed = _parse_magi_text(
        "〖PSA10〗舞台の上で 天音かなた(サイン入り) SP HOL/W91-T108SP - 出品数 0",
        detail_url="https://magi.camp/products/456",
        board_url="https://magi.camp/series/7/products",
    )

    assert parsed is not None
    assert parsed.title == "舞台の上で 天音かなた(サイン入り)"
    assert parsed.rarity == "SP"
    assert parsed.card_number == "HOL/W91-T108SP"
    assert parsed.listing_count == 0
    assert parsed.is_graded is True


def test_hot_card_service_merges_duplicate_variants() -> None:
    service = TcgHotCardService()
    entries = service._build_ranked_entries(  # type: ignore[attr-defined]
        game="pokemon",
        parsed_items=[
            _parse_cardrush_text(
                "〔状態A-〕ピカチュウex【SAR】{234/193} [ [状態A-]M2a ] 59,800円 (税込) 在庫数 5枚",
                detail_url="https://www.cardrush-pokemon.jp/product/123",
                board_url="https://www.cardrush-pokemon.jp/product-group/22?sort=rank&num=100",
            ),
            _parse_cardrush_text(
                "ピカチュウex【SAR】{234/193} [ M2a ] 61,800円 (税込) 在庫数 2枚",
                detail_url="https://www.cardrush-pokemon.jp/product/124",
                board_url="https://www.cardrush-pokemon.jp/product-group/22?sort=rank&num=100",
            ),
            _parse_cardrush_text(
                "ロケット団のミュウツーex【SAR】{237/193} [ M2a ] 34,800円 (税込) 在庫数 1枚",
                detail_url="https://www.cardrush-pokemon.jp/product/125",
                board_url="https://www.cardrush-pokemon.jp/product-group/22?sort=rank&num=100",
            ),
        ],
        limit=10,
    )

    assert len(entries) == 2
    assert entries[0].title == "ピカチュウex"
    assert entries[0].listing_count == 7
    assert entries[0].references[1].url == "https://www.cardrush-pokemon.jp/product/124"


def test_hot_card_service_prioritizes_active_depth_over_source_rank() -> None:
    service = TcgHotCardService()
    entries = service._build_ranked_entries(  # type: ignore[attr-defined]
        game="ws",
        parsed_items=[
            _ParsedHotItem(
                title="rank_only_card",
                price_jpy=30000,
                card_number="AAA/W11-001SP",
                rarity="SP",
                set_code="aaa",
                listing_count=0,
                is_graded=False,
                condition=None,
                detail_url="https://example.com/rank-only",
                board_url="https://example.com/board",
                note="rank only",
            ),
            _ParsedHotItem(
                title="active_card",
                price_jpy=28000,
                card_number="AAA/W11-002SP",
                rarity="SP",
                set_code="aaa",
                listing_count=3,
                is_graded=False,
                condition=None,
                detail_url="https://example.com/active",
                board_url="https://example.com/board",
                note="active",
            ),
        ],
        limit=10,
    )

    assert entries[0].title == "active_card"
    assert entries[1].title == "rank_only_card"
    assert entries[0].hot_score > entries[1].hot_score


def test_hot_card_service_prefers_raw_copies_when_depth_is_equal() -> None:
    service = TcgHotCardService()
    entries = service._build_ranked_entries(  # type: ignore[attr-defined]
        game="ws",
        parsed_items=[
            _ParsedHotItem(
                title="graded_card",
                price_jpy=50000,
                card_number="BBB/W22-001SSP",
                rarity="SSP",
                set_code="bbb",
                listing_count=2,
                is_graded=True,
                condition=None,
                detail_url="https://example.com/graded",
                board_url="https://example.com/board",
                note="graded",
            ),
            _ParsedHotItem(
                title="raw_card",
                price_jpy=45000,
                card_number="BBB/W22-002SSP",
                rarity="SSP",
                set_code="bbb",
                listing_count=2,
                is_graded=False,
                condition=None,
                detail_url="https://example.com/raw",
                board_url="https://example.com/board",
                note="raw",
            ),
        ],
        limit=10,
    )

    assert entries[0].title == "raw_card"
    assert entries[1].title == "graded_card"
    assert entries[0].hot_score > entries[1].hot_score


def test_resolve_lookup_spec_uses_hot_card_metadata_for_precise_variant() -> None:
    class StubHotCardService(TcgHotCardService):
        def _load_source_items(self, game: str) -> list[_ParsedHotItem]:  # type: ignore[override]
            return [
                _ParsedHotItem(
                    title="メガシビルドンex",
                    price_jpy=780,
                    card_number="225/193",
                    rarity="MA",
                    set_code="m2a",
                    listing_count=535,
                    is_graded=False,
                    condition=None,
                    detail_url="https://example.com/225",
                    board_url="https://example.com/board",
                    note="stub",
                ),
                _ParsedHotItem(
                    title="メガシビルドンex",
                    price_jpy=1280,
                    card_number="235/193",
                    rarity="SAR",
                    set_code="m2a",
                    listing_count=140,
                    is_graded=False,
                    condition=None,
                    detail_url="https://example.com/235",
                    board_url="https://example.com/board",
                    note="stub",
                ),
            ]

    service = StubHotCardService()
    resolved = service.resolve_lookup_spec(
        TcgCardSpec(game="pokemon", title="メガシビルドン", rarity="SAR"),
    )

    assert resolved is not None
    assert resolved.title == "メガシビルドンex"
    assert resolved.card_number == "235/193"
    assert resolved.rarity == "SAR"
    assert resolved.set_code == "m2a"
