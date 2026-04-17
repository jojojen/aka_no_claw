from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from assistant_runtime import AssistantSettings
from tcg_tracker.hot_cards import HotCardBoard, HotCardEntry, HotCardReference

from openclaw_adapter.telegram_bot import (
    TelegramCommandProcessor,
    TelegramLookupQuery,
    build_processing_ack,
    format_liquidity_board,
    handle_telegram_message,
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
                title="Pikachu ex",
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


class FakeTelegramClient:
    def __init__(self, sample_path: Path | None = None) -> None:
        self.sample_path = sample_path
        self.sent_messages: list[str] = []

    def send_message(self, *, chat_id: str | int, text: str) -> dict[str, object]:
        self.sent_messages.append(text)
        return {"chat_id": str(chat_id), "text": text}

    def get_file(self, *, file_id: str) -> dict[str, object]:
        assert self.sample_path is not None
        return {"file_path": self.sample_path.name, "file_id": file_id}

    def download_file(self, *, file_path: str) -> bytes:
        assert self.sample_path is not None
        assert file_path == self.sample_path.name
        return self.sample_path.read_bytes()


def test_parse_lookup_command_supports_pipe_format() -> None:
    query = parse_lookup_command("pokemon | Pikachu ex | 132/106 | SAR | sv08")

    assert query == TelegramLookupQuery(
        game="pokemon",
        name="Pikachu ex",
        card_number="132/106",
        rarity="SAR",
        set_code="sv08",
    )


def test_parse_lookup_command_supports_simple_format() -> None:
    query = parse_lookup_command("ws Hatsune Miku")

    assert query == TelegramLookupQuery(
        game="ws",
        name="Hatsune Miku",
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


def test_command_processor_handles_price_and_trend_aliases() -> None:
    settings = AssistantSettings(openclaw_telegram_chat_id="123")
    processor = TelegramCommandProcessor(
        settings=settings,
        lookup_renderer=lambda query: f"{query.game}:{query.name}:{query.card_number}",
        board_loader=lambda: (_stub_board(),),
        catalog_renderer=lambda: "catalog",
    )

    lookup_reply = processor.build_reply(chat_id="123", text="/price pokemon | Pikachu ex | 132/106")
    trend_reply = processor.build_reply(chat_id="123", text="/trend pokemon")
    hot_reply = processor.build_reply(chat_id="123", text="/hot pokemon 1")

    assert lookup_reply == "pokemon:Pikachu ex:132/106"
    assert "Pokemon Liquidity Board" in trend_reply
    assert "bid " in trend_reply and "80,000" in trend_reply
    assert "ask " in trend_reply and "99,800" in trend_reply
    assert "boost 6.00" in trend_reply
    assert "Pokemon Liquidity Board" in hot_reply
    assert "\n1. Pikachu ex\n" in hot_reply


def test_command_processor_help_lists_trend_and_scan_commands() -> None:
    settings = AssistantSettings(openclaw_telegram_chat_id="123")
    processor = TelegramCommandProcessor(
        settings=settings,
        lookup_renderer=lambda query: query.name,
        board_loader=lambda: (_stub_board(),),
        catalog_renderer=lambda: "catalog",
    )

    help_reply = processor.build_reply(chat_id="123", text="/help")

    assert "/trend pokemon" in help_reply
    assert "/price pokemon | Pikachu ex | 132/106 | SAR | sv08" in help_reply
    assert "Send a photo with caption: /scan pokemon" in help_reply


def test_build_processing_ack_for_heavy_actions() -> None:
    assert build_processing_ack(text="/price pokemon Pikachu ex") == "收到查價指令，開始處理。"
    assert build_processing_ack(text="/trend pokemon") == "收到趨勢榜查詢，開始整理資料。"
    assert build_processing_ack(has_photo=True) == "收到圖片，開始解析與查價。"
    assert build_processing_ack(text="/ping") is None


def test_handle_telegram_message_sends_ack_then_photo_result() -> None:
    sample_path = Path(__file__).resolve().parents[1] / "fwdspecptcg" / "pikachu.jpg"
    client = FakeTelegramClient(sample_path=sample_path)
    settings = AssistantSettings(openclaw_telegram_chat_id="123")
    processor = TelegramCommandProcessor(
        settings=settings,
        lookup_renderer=lambda query: query.name,
        board_loader=lambda: (_stub_board(),),
        catalog_renderer=lambda: "catalog",
    )

    replies = handle_telegram_message(
        client=client,
        processor=processor,
        photo_renderer=lambda query: f"photo:{query.game_hint}:{query.title_hint}:{query.image_path.suffix}",
        message={
            "chat": {"id": "123"},
            "photo": [{"file_id": "photo-1", "file_size": 128}],
            "caption": "/scan pokemon Pikachu ex",
        },
    )

    assert replies == (
        "收到圖片，開始解析與查價。",
        "photo:pokemon:Pikachu ex:.jpg",
    )
    assert client.sent_messages == list(replies)


def test_handle_telegram_message_sends_ack_then_text_result() -> None:
    client = FakeTelegramClient()
    settings = AssistantSettings(openclaw_telegram_chat_id="123")
    processor = TelegramCommandProcessor(
        settings=settings,
        lookup_renderer=lambda query: f"{query.game}:{query.name}",
        board_loader=lambda: (_stub_board(),),
        catalog_renderer=lambda: "catalog",
    )

    replies = handle_telegram_message(
        client=client,
        processor=processor,
        photo_renderer=lambda query: "unused",
        message={
            "chat": {"id": "123"},
            "text": "/price pokemon Pikachu ex",
        },
    )

    assert replies == (
        "收到查價指令，開始處理。",
        "pokemon:Pikachu ex",
    )
    assert client.sent_messages == list(replies)


def test_format_liquidity_board_includes_reference_url() -> None:
    text = format_liquidity_board(_stub_board(), limit=1)

    assert "https://example.com/rank" in text
    assert "liq 88.20" in text
    assert "attn 41.70" in text
    assert "support 90.08" in text
    assert "buy-up" in text
