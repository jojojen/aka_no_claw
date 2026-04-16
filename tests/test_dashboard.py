from __future__ import annotations

from datetime import datetime, timezone

from assistant_runtime import AssistantSettings
from market_monitor.models import FairValueEstimate, MarketOffer, TrackedItem, WatchRule
from market_monitor.storage import MonitorDatabase
from openclaw_adapter.dashboard import build_dashboard_payload
from openclaw_adapter.toolset import build_tool_registry
from tcg_tracker.hot_cards import HotCardBoard, HotCardEntry, HotCardReference


class StubHotCardService:
    def load_boards(self) -> tuple[HotCardBoard, ...]:
        return (
            HotCardBoard(
                game="pokemon",
                label="Pokemon Liquidity Top 10",
                methodology="stub methodology",
                generated_at=datetime.now(timezone.utc),
                items=(
                    HotCardEntry(
                        game="pokemon",
                        rank=1,
                        title="ピカチュウex",
                        price_jpy=99800,
                        card_number="132/106",
                        rarity="SAR",
                        set_code="sv08",
                        listing_count=5,
                        hot_score=99.0,
                        notes=("stub note",),
                        is_graded=False,
                        references=(HotCardReference(label="Stub", url="https://example.com/pikachu"),),
                    ),
                ),
            ),
        )


def test_dashboard_payload_includes_runtime_data(tmp_path) -> None:
    db_path = tmp_path / "monitor.sqlite3"
    database = MonitorDatabase(db_path)
    database.bootstrap()

    item = TrackedItem(
        item_id="pokemon-pikachu-ex-sar",
        item_type="tcg_card",
        category="tcg",
        title="ピカチュウex",
        attributes={"game": "pokemon", "card_number": "132/106", "rarity": "SAR"},
    )
    database.upsert_item(item)
    database.save_watch_rule(
        WatchRule(
            rule_id="watch-pokemon-pikachu-ex-sar",
            item_id=item.item_id,
            discount_threshold_pct=12.5,
            schedule_minutes=15,
        )
    )
    database.save_offers(
        item.item_id,
        [
            MarketOffer(
                source="yuyutei",
                listing_id="ask-1",
                url="https://example.com/pikachu",
                title="ピカチュウex",
                price_jpy=99800,
                price_kind="ask",
                captured_at=datetime.now(timezone.utc),
                source_category="specialty_store",
            )
        ],
    )
    database.save_snapshot(
        FairValueEstimate(
            item_id=item.item_id,
            amount_jpy=85000,
            confidence=0.81,
            sample_count=2,
            reasoning=("test fixture",),
        )
    )

    settings = AssistantSettings(monitor_db_path=str(db_path), monitor_env="test", log_level="DEBUG")
    registry = build_tool_registry(settings)
    payload = build_dashboard_payload(
        settings=settings,
        registry=registry,
        hot_card_service=StubHotCardService(),
    )

    assert payload["assistant"]["environment"] == "test"
    assert payload["stats"]["tracked_items"] == 1
    assert payload["stats"]["watch_rules"] == 1
    assert payload["stats"]["source_offers"] == 1
    assert payload["stats"]["price_snapshots"] == 1
    assert payload["tracked_items"][0]["title"] == "ピカチュウex"
    assert payload["tracked_items"][0]["fair_value_jpy"] == 85000
    assert any(tool["name"] == "assistant.serve-dashboard" for tool in payload["tools"])
    assert any(source["id"] == "yuyutei" for source in payload["reference_sources"])
    assert payload["hot_cards"][0]["game"] == "pokemon"
    assert payload["hot_cards"][0]["items"][0]["title"] == "ピカチュウex"
    assert payload["hot_cards"][0]["items"][0]["liquidity_score"] == 99.0
