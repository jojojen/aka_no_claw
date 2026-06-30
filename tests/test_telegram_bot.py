from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from assistant_runtime import AssistantSettings
from market_monitor.models import FairValueEstimate, MarketOffer, TrackedItem
from tcg_tracker.catalog import TcgCardSpec
from tcg_tracker.hot_cards import HotCardBoard, HotCardEntry, HotCardReference
from tcg_tracker.image_lookup import ParsedCardImage, TcgImageLookupOutcome
from tcg_tracker.service import TcgLookupResult
from tests.image_lookup_case_fixtures import get_image_lookup_live_case
from price_monitor_bot.bot import (
    PendingTelegramTextClarification,
    RegisteredCommand,
    TelegramPhotoIntentAnalysis,
    TelegramPhotoIntentOption,
)

from openclaw_adapter.formatters import format_lookup_result_telegram
from openclaw_adapter.natural_language import TelegramNaturalLanguageIntent
from openclaw_adapter.reputation_snapshot import ReputationSnapshotResult, SnapshotStillPending
from openclaw_adapter.telegram_bot import (
    TelegramCommandProcessor,
    TelegramFileAttachment,
    TelegramLookupQuery,
    TelegramResearchQuery,
    TelegramReputationQuery,
    TelegramReputationDelivery,
    build_processing_ack,
    default_photo_renderer,
    default_reputation_renderer,
    format_liquidity_board,
    format_photo_lookup_result,
    format_reputation_snapshot_result,
    handle_telegram_message,
    parse_lookup_command,
    parse_reputation_snapshot_command,
    _build_research_seller_snapshot_lookup,
    _build_research_callback_handler,
    _build_research_reply_formatter,
    _ResearchReplyCache,
    _build_status_text,
    _build_registries,
    _chromium_launch_options,
)
from openclaw_adapter.research_command import ResearchReport, ResearchSectionResult, SellerReputationSnapshot

# Every call to handle_telegram_message now sends an immediate intake ack
# before kicking off the real processing pipeline.
PHOTO_INTAKE_ACK = "已收到圖片，開始解讀使用者意圖"
TEXT_INTAKE_ACK = "已收到訊息，開始解讀使用者意圖"


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
        self.sent_documents: list[tuple[str, str | None]] = []
        self.sent_photos: list[tuple[str, str | None]] = []

    def send_message(
        self,
        *,
        chat_id: str | int,
        text: str,
        reply_markup: dict[str, object] | None = None,
    ) -> dict[str, object]:
        self.sent_messages.append(text)
        return {"chat_id": str(chat_id), "text": text, "reply_markup": reply_markup}

    def send_document(self, *, chat_id: str | int, document_path: Path, caption: str | None = None) -> dict[str, object]:
        self.sent_documents.append((document_path.name, caption))
        return {"chat_id": str(chat_id), "document": document_path.name, "caption": caption}

    def send_photo(self, *, chat_id: str | int, photo_path: Path, caption: str | None = None) -> dict[str, object]:
        self.sent_photos.append((photo_path.name, caption))
        return {"chat_id": str(chat_id), "photo": photo_path.name, "caption": caption}

    def get_file(self, *, file_id: str) -> dict[str, object]:
        assert self.sample_path is not None
        return {"file_path": self.sample_path.name, "file_id": file_id}

    def download_file(self, *, file_path: str) -> bytes:
        assert self.sample_path is not None
        assert file_path == self.sample_path.name
        return self.sample_path.read_bytes()


class StubNaturalLanguageRouter:
    def __init__(self, intent: TelegramNaturalLanguageIntent | None) -> None:
        self.intent = intent
        self.seen_texts: list[str] = []

    def route(self, text: str) -> TelegramNaturalLanguageIntent | None:
        self.seen_texts.append(text)
        return self.intent


def _ambiguous_photo_analysis() -> TelegramPhotoIntentAnalysis:
    return TelegramPhotoIntentAnalysis(
        options=(
            TelegramPhotoIntentOption(1, "pokemon_card_price", "要我查這張寶可夢卡市價嗎？", "/scan pokemon"),
            TelegramPhotoIntentOption(2, "yugioh_card_price", "要我查這張遊戲王卡市價嗎？", "/scan yugioh"),
            TelegramPhotoIntentOption(3, "pokemon_box_price", "要我查這個寶可夢卡盒市價嗎？", "/scan pokemon"),
        ),
        parsed_game="pokemon",
        parsed_item_kind="card",
        parsed_title="Pikachu ex",
    )


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


def test_parse_lookup_command_supports_yugioh_and_union_arena_aliases() -> None:
    ygo_query = parse_lookup_command("ygo | 青眼の白龍 | QCCP-JP001 | ウルトラ")
    ua_query = parse_lookup_command("ua 綾波レイ")

    assert ygo_query == TelegramLookupQuery(
        game="yugioh",
        name="青眼の白龍",
        card_number="QCCP-JP001",
        rarity="ウルトラ",
        set_code="qccp",
    )
    assert ua_query == TelegramLookupQuery(game="union_arena", name="綾波レイ")


def test_default_photo_renderer_tolerates_disabled_local_vision_backend(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_base_default_photo_renderer(**kwargs):
        captured.update(kwargs)
        return lambda query: "unused"

    monkeypatch.setattr(
        "openclaw_adapter.telegram_bot._base_default_photo_renderer",
        fake_base_default_photo_renderer,
    )
    settings = AssistantSettings(
        openclaw_local_vision_backend=None,
        openclaw_local_vision_model="qwen2.5vl:7b",
    )

    renderer = default_photo_renderer(settings)

    assert renderer is not None
    assert captured["vision_settings"].backend == ""
    assert captured["vision_settings"].model == "qwen2.5vl:7b"


def test_reputation_snapshot_artifacts_use_configured_system_chromium(monkeypatch) -> None:
    monkeypatch.setenv("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH", "/usr/bin/chromium")

    assert _chromium_launch_options() == {
        "headless": True,
        "executable_path": "/usr/bin/chromium",
    }


def test_command_processor_restricts_unconfigured_chat() -> None:
    settings = AssistantSettings(openclaw_telegram_chat_id="999")
    processor = TelegramCommandProcessor(
        settings=settings,
        lookup_renderer=lambda query: query.name,
        board_loader=lambda: (_stub_board(),),
        catalog_renderer=lambda: "catalog",
    )

    assert processor.build_reply(chat_id="123", text="/ping") is None


def test_plain_youtube_url_offers_like_song_confirmation(monkeypatch) -> None:
    monkeypatch.setattr(
        "openclaw_adapter.telegram_bot.build_like_song_confirmation",
        lambda settings, url: (
            "🎵 偵測到 YouTube 歌曲連結\n\n歌曲：勇者\n歌手：YOASOBI\n\n要加入最愛曲目清單嗎？",
            {
                "inline_keyboard": [[
                    {"text": "❤️ 加入最愛", "callback_data": "quiz:ls:OIBODIPC_8Y"},
                    {"text": "先不要", "callback_data": "quiz:lx:OIBODIPC_8Y"},
                ]]
            },
        ),
    )
    settings = AssistantSettings(openclaw_telegram_chat_id="123")
    processor = TelegramCommandProcessor(
        settings=settings,
        lookup_renderer=lambda query: query.name,
        board_loader=lambda: (_stub_board(),),
        catalog_renderer=lambda: "catalog",
    )

    plan = processor.build_reply_plan(
        chat_id="123",
        text="https://youtu.be/OIBODIPC_8Y?si=XzdzDFGtCRQoXH7T",
    )

    text, _ = plan._execute_unpacked()
    markup = plan.reply_markup
    assert "要加入最愛曲目清單嗎？" in text
    assert markup is not None
    assert processor.get_pending_text_clarification("123") is None
    flat = [b for row in markup["inline_keyboard"] for b in row]
    assert any(b["callback_data"] == "quiz:ls:OIBODIPC_8Y" for b in flat)
    assert any(b["callback_data"] == "quiz:lx:OIBODIPC_8Y" for b in flat)


def test_plain_youtube_url_clears_existing_text_clarification(monkeypatch) -> None:
    monkeypatch.setattr(
        "openclaw_adapter.telegram_bot.build_like_song_confirmation",
        lambda settings, url: (
            "🎵 偵測到 YouTube 歌曲連結\n\n歌曲：例えば\n歌手：花譜 -KAF-\n\n要加入最愛曲目清單嗎？",
            {
                "inline_keyboard": [[
                    {"text": "❤️ 加入最愛", "callback_data": "quiz:ls:4_fvGiulqk8"},
                    {"text": "先不要", "callback_data": "quiz:lx:4_fvGiulqk8"},
                ]]
            },
        ),
    )
    settings = AssistantSettings(openclaw_telegram_chat_id="123")
    processor = TelegramCommandProcessor(
        settings=settings,
        lookup_renderer=lambda query: query.name,
        board_loader=lambda: (_stub_board(),),
        catalog_renderer=lambda: "catalog",
    )
    processor.set_pending_text_clarification(
        PendingTelegramTextClarification(
            chat_id="123",
            original_text="舊的澄清",
            options=(),
            top_intent=None,
        )
    )

    plan = processor.build_pending_text_reply_plan(
        chat_id="123",
        text="https://youtu.be/4_fvGiulqk8?si=m8R6kh9a3GzpeSWV",
    )

    assert plan is not None
    text, _ = plan._execute_unpacked()
    assert "要加入最愛曲目清單嗎？" in text
    assert processor.get_pending_text_clarification("123") is None


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
    assert "/snapshot https://jp.mercari.com/item/m123456789" in help_reply
    assert "/search" in help_reply
    assert "/research https://jp.mercari.com/item/m123456789" in help_reply
    assert "/scan pokemon" in help_reply
    assert "/hunt status" in help_reply
    assert "/quiz grammar" in help_reply
    assert "/translateja 你好，今天辛苦了" in help_reply
    assert "/translatezh お疲れさま、今日は大変だったね" in help_reply


def test_openclaw_registries_include_translate_commands() -> None:
    settings = AssistantSettings(openclaw_telegram_chat_id="123")
    command_handlers, _, _, _ = _build_registries(settings, dynamic_tool_runner=None)
    for command in ("/translateja", "/ja", "/jp", "/translatezh", "/zh"):
        assert command in command_handlers


def test_openclaw_registries_include_research_commands() -> None:
    settings = AssistantSettings(openclaw_telegram_chat_id="123")
    command_handlers, callback_handlers, _, _ = _build_registries(settings, dynamic_tool_runner=None)
    for command in ("/research", "/resaerch"):
        assert command in command_handlers
    assert "rs" in callback_handlers


def test_openclaw_registries_include_ir_command() -> None:
    settings = AssistantSettings(openclaw_telegram_chat_id="123")
    command_handlers, callback_handlers, _, _ = _build_registries(settings, dynamic_tool_runner=None)
    assert "/ir" in command_handlers
    assert "ir" in callback_handlers


# ── home_action NL executor ───────────────────────────────────────────────────

def test_home_action_plan_dispatches_ir_send() -> None:
    from telegram_nl.natural_language import TelegramNaturalLanguageIntent as NLIntent

    settings = AssistantSettings(openclaw_telegram_chat_id="123")
    processor = TelegramCommandProcessor(
        settings=settings,
        lookup_renderer=lambda query: query.name,
        board_loader=lambda: (_stub_board(),),
        catalog_renderer=lambda: "catalog",
    )

    calls: list[str] = []
    processor._command_registry["/ir"] = RegisteredCommand(
        lambda raw, cid: calls.append(raw) or "ir_ok"
    )

    intent = NLIntent(intent="home_action", home_target="客廳電燈", home_command="on")
    plan = processor._build_app_natural_language_reply_plan(intent, chat_id="123")

    assert plan is not None
    plan._execute_unpacked()
    assert calls == ["send 客廳電燈 on"]


def test_home_action_plan_off_command() -> None:
    from telegram_nl.natural_language import TelegramNaturalLanguageIntent as NLIntent

    settings = AssistantSettings(openclaw_telegram_chat_id="123")
    processor = TelegramCommandProcessor(
        settings=settings,
        lookup_renderer=lambda query: query.name,
        board_loader=lambda: (_stub_board(),),
        catalog_renderer=lambda: "catalog",
    )

    calls: list[str] = []
    processor._command_registry["/ir"] = RegisteredCommand(
        lambda raw, cid: calls.append(raw) or "ir_ok"
    )

    intent = NLIntent(intent="home_action", home_target="臥室燈", home_command="off")
    plan = processor._build_app_natural_language_reply_plan(intent, chat_id="123")

    assert plan is not None
    plan._execute_unpacked()
    assert calls == ["send 臥室燈 off"]


def test_home_action_plan_when_ir_not_registered() -> None:
    from telegram_nl.natural_language import TelegramNaturalLanguageIntent as NLIntent

    settings = AssistantSettings(openclaw_telegram_chat_id="123")
    processor = TelegramCommandProcessor(
        settings=settings,
        lookup_renderer=lambda query: query.name,
        board_loader=lambda: (_stub_board(),),
        catalog_renderer=lambda: "catalog",
    )
    # Do NOT register /ir — simulate disabled command
    processor._command_registry.pop("/ir", None)

    intent = NLIntent(intent="home_action", home_target="燈", home_command="on")
    plan = processor._build_app_natural_language_reply_plan(intent, chat_id="123")

    assert plan is not None
    assert plan.reply == "/ir 指令尚未啟用。"


def test_build_registries_passes_knowledge_db_path_to_research_handler(monkeypatch, tmp_path: Path) -> None:
    settings = AssistantSettings(
        openclaw_telegram_chat_id="123",
        knowledge_db_path=str(tmp_path / "knowledge.sqlite3"),
    )
    seen: dict[str, object] = {}

    def _fake_build_research_handler(**kwargs):
        seen.update(kwargs)
        return lambda remainder, chat_id: "ok"

    monkeypatch.setattr("openclaw_adapter.telegram_bot.build_research_handler", _fake_build_research_handler)

    _build_registries(settings, dynamic_tool_runner=None)

    assert seen["knowledge_db_path"] == str(tmp_path / "knowledge.sqlite3")
    assert callable(seen["search_fn"])
    assert callable(seen["seller_snapshot_lookup_fn"])
    assert callable(seen["ip_heat_lookup_fn"])


def test_command_processor_routes_research_to_registered_handler_before_web_search() -> None:
    settings = AssistantSettings(openclaw_telegram_chat_id="123")
    command_handlers, _, _, _ = _build_registries(settings, dynamic_tool_runner=None)
    processor = TelegramCommandProcessor(
        settings=settings,
        lookup_renderer=lambda query: query.name,
        board_loader=lambda: (_stub_board(),),
        catalog_renderer=lambda: "catalog",
        research_renderer=lambda query: "不應走到這裡",
        command_handlers=command_handlers,
    )

    plan = processor.build_reply_plan(
        chat_id="123",
        text="/research https://jp.mercari.com/item/m65806654179?afid=foo",
    )

    assert plan.ack == "收到，正在進行深度商品研究（會分階段回報進度）…"
    assert plan.run_in_background is True
    assert plan.reply_factory is not None


def test_research_reply_formatter_returns_compact_text_with_buttons() -> None:
    cache = _ResearchReplyCache()
    formatter = _build_research_reply_formatter(cache)
    report = ResearchReport(
        chat_id="123",
        mode_label="Mercari 商品網址",
        target_display_text="https://jp.mercari.com/item/m1",
        budget_used=1,
        budget_max=5,
        item_data=None,
        seller_snapshot=None,
        section_results=(
            ResearchSectionResult(
                section_name="合理市價分析",
                status="partial",
                confidence=0.6,
                sample_count=2,
                evidence_count=2,
                summary="賣家開價 ¥1,800；Mercari sold 樣本 1 筆，均價約 ¥1,500。",
            ),
        ),
        warnings=("sold 樣本少於 3 筆，成交均價可信度有限。",),
    )

    text, markup = formatter(report)

    assert text.startswith("/research 摘要")
    flat = [button for row in markup["inline_keyboard"] for button in row]
    assert any(button["text"] == "看市價" for button in flat)
    assert all(str(button["callback_data"]).startswith("rs:") for button in flat)


def test_research_callback_handler_renders_cached_detail_view() -> None:
    cache = _ResearchReplyCache()
    report = ResearchReport(
        chat_id="123",
        mode_label="Mercari 商品網址",
        target_display_text="https://jp.mercari.com/item/m1",
        budget_used=1,
        budget_max=5,
        item_data=None,
        seller_snapshot=None,
        section_results=(
            ResearchSectionResult(
                section_name="合理市價分析",
                status="partial",
                confidence=0.6,
                sample_count=2,
                evidence_count=2,
                summary="賣家開價 ¥1,800；Mercari sold 樣本 1 筆，均價約 ¥1,500。",
                evidence_urls=("https://jp.mercari.com/item/a",),
                warnings=("sold 樣本少於 3 筆，成交均價可信度有限。",),
            ),
            ResearchSectionResult(
                section_name="流動性分析",
                status="partial",
                confidence=0.2,
                sample_count=2,
                evidence_count=2,
                summary="Mercari active 1 筆 / sold 1 筆；樣本偏少。",
            ),
        ),
        warnings=("sold 樣本少於 3 筆，成交均價可信度有限。",),
    )
    token = cache.put(report)
    handler = _build_research_callback_handler(cache)

    toast, text, markup = handler(f"{token}:price", "orig", "123")

    assert toast == "已切換研究視圖"
    assert text is not None
    assert "/research 市價細節" in text
    assert "合理市價分析 [partial]" in text
    assert markup is not None


def test_research_reply_formatter_prefers_snapshot_seller_when_item_seller_missing() -> None:
    cache = _ResearchReplyCache()
    formatter = _build_research_reply_formatter(cache)
    report = ResearchReport(
        chat_id="123",
        mode_label="Mercari 商品網址",
        target_display_text="https://jp.mercari.com/item/m1",
        budget_used=1,
        budget_max=5,
        item_data=None,
        seller_snapshot=SellerReputationSnapshot(
            seller_url="https://jp.mercari.com/user/profile/123",
            proof_url="http://127.0.0.1:5055/p/proof_x",
            proof_id="proof_x",
            reused=True,
            display_name="bassman",
            captured_at=None,
            total_reviews=9,
            listing_count=13,
            followers_count=None,
            following_count=None,
            seller_positive=6,
            seller_negative=0,
            seller_rate=100.0,
        ),
        section_results=(),
        warnings=(),
    )

    text, _markup = formatter(report)

    assert "賣家：bassman" in text


def test_command_processor_handles_translate_aliases(monkeypatch) -> None:
    settings = AssistantSettings(
        openclaw_telegram_chat_id="123",
        openclaw_local_text_backend="ollama",
        openclaw_local_text_endpoint="http://127.0.0.1:11434",
        openclaw_local_text_model="qwen3:14b",
    )

    def _fake_call_local_text_model(*, endpoint, model, prompt, timeout_seconds, ssl_context):
        if "日文" in prompt:
            return "こんにちは、今日はお疲れさまでした。"
        return "你好，今天辛苦了。"

    monkeypatch.setattr(
        "openclaw_adapter.telegram_bot._call_local_text_model",
        _fake_call_local_text_model,
    )

    command_handlers, _, _, _ = _build_registries(settings, dynamic_tool_runner=None)
    processor = TelegramCommandProcessor(
        settings=settings,
        lookup_renderer=lambda query: query.name,
        board_loader=lambda: (_stub_board(),),
        catalog_renderer=lambda: "catalog",
        command_handlers=command_handlers,
    )

    assert processor.build_reply(chat_id="123", text="/translateja 你好，今天辛苦了") == (
        "こんにちは、今日はお疲れさまでした。"
    )
    assert processor.build_reply(chat_id="123", text="/ja 你好，今天辛苦了") == (
        "こんにちは、今日はお疲れさまでした。"
    )
    assert processor.build_reply(chat_id="123", text="/translatezh お疲れさま") == "你好，今天辛苦了。"
    assert processor.build_reply(chat_id="123", text="/zh お疲れさま") == "你好，今天辛苦了。"


def test_command_processor_handles_hunt_status() -> None:
    settings = AssistantSettings(openclaw_telegram_chat_id="123")

    def _stub_hunt(remainder: str, chat_id: str) -> str:
        if remainder.strip() in {"status", ""}:
            return "targets: Umbreon"
        return "unknown"

    processor = TelegramCommandProcessor(
        settings=settings,
        lookup_renderer=lambda query: query.name,
        board_loader=lambda: (_stub_board(),),
        catalog_renderer=lambda: "catalog",
        command_handlers={"/hunt": RegisteredCommand(_stub_hunt)},
    )

    assert processor.build_reply(chat_id="123", text="/hunt status") == "targets: Umbreon"


def test_command_processor_handles_hunt_remove() -> None:
    import re as _re

    settings = AssistantSettings(openclaw_telegram_chat_id="123")
    seen: list[str] = []

    def _stub_hunt(remainder: str, chat_id: str) -> str:
        m = _re.match(r"^(?:remove|delete|dismiss)\s+(.+)$", remainder.strip(), _re.IGNORECASE)
        if m:
            target = m.group(1).strip()
            seen.append(target)
            return f"removed:{target}"
        return "unknown"

    processor = TelegramCommandProcessor(
        settings=settings,
        lookup_renderer=lambda query: query.name,
        board_loader=lambda: (_stub_board(),),
        catalog_renderer=lambda: "catalog",
        command_handlers={"/hunt": RegisteredCommand(_stub_hunt)},
    )

    assert processor.build_reply(chat_id="123", text="/hunt remove 2") == "removed:2"
    assert processor.build_reply(chat_id="123", text="/hunt delete Umbreon ex SAR") == "removed:Umbreon ex SAR"
    assert seen == ["2", "Umbreon ex SAR"]


def test_build_status_text_includes_feature_models_and_sizes(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    # .env.example sets configured text=qwen3:4b, vision=qwen2.5vl:7b,gemma3:12b
    # No .env override, so configured stays as .env.example values.
    (tmp_path / ".env.example").write_text(
        "\n".join([
            "OPENCLAW_LOCAL_TEXT_BACKEND=ollama",
            "OPENCLAW_LOCAL_TEXT_MODEL=qwen3:4b",
            "OPENCLAW_LOCAL_TEXT_TIMEOUT_SECONDS=75",
            "OPENCLAW_LOCAL_TEXT_ENDPOINT=http://127.0.0.1:11434",
            "OPENCLAW_LOCAL_VISION_BACKEND=ollama",
            "OPENCLAW_LOCAL_VISION_MODEL=qwen2.5vl:7b,gemma3:12b",
            "OPENCLAW_LOCAL_VISION_TIMEOUT_SECONDS=180",
            "OPENCLAW_LOCAL_VISION_ENDPOINT=http://127.0.0.1:11434",
        ]),
        encoding="utf-8",
    )
    settings = AssistantSettings(
        monitor_env="development",
        monitor_db_path="data/monitor.sqlite3",
        openclaw_telegram_chat_ids=("123", "456"),
        openclaw_tesseract_path="/opt/homebrew/bin/tesseract",
        openclaw_tessdata_dir="/opt/homebrew/share/tessdata",
        openclaw_local_text_backend="ollama",
        openclaw_local_text_model="qwen3:4b",
        openclaw_local_text_endpoint="http://127.0.0.1:11434",
        openclaw_local_text_timeout_seconds=75,
        openclaw_local_vision_backend="ollama",
        openclaw_local_vision_model="qwen2.5vl:7b,gemma3:12b",
        openclaw_local_vision_endpoint="http://127.0.0.1:11434",
        openclaw_local_vision_timeout_seconds=180,
        reputation_agent_server_url="http://127.0.0.1:5055",
        reputation_agent_poll_secs=5,
    )

    text = _build_status_text(settings)

    # Active router model = gemma3:12b (strongest across text+vision models).
    # Configured text model = qwen3:4b (from .env.example) → shows active vs configured.
    assert "text routing: active=ollama / gemma3:12b (12B) | configured=ollama / qwen3:4b (4B) | timeout=75s" in text
    assert "image scan vision: ollama / qwen2.5vl:7b (7B), gemma3:12b (12B) | timeout=180s" in text
    assert "image scan OCR: engine=tesseract | binary=/opt/homebrew/bin/tesseract" in text
    assert "price lookup / trend / watch: model=none" in text
    assert "reputation snapshot: model=none | server=http://127.0.0.1:5055 | poll=5s" in text


def test_build_status_text_shows_configured_models_when_runtime_is_disabled(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env.example").write_text(
        "\n".join(
            [
                "OPENCLAW_LOCAL_TEXT_BACKEND=ollama",
                "OPENCLAW_LOCAL_TEXT_MODEL=qwen3:4b",
                "OPENCLAW_LOCAL_TEXT_TIMEOUT_SECONDS=75",
                "OPENCLAW_LOCAL_TEXT_ENDPOINT=http://127.0.0.1:11434",
                "OPENCLAW_LOCAL_VISION_BACKEND=ollama",
                "OPENCLAW_LOCAL_VISION_MODEL=gemma3:4b",
                "OPENCLAW_LOCAL_VISION_TIMEOUT_SECONDS=180",
                "OPENCLAW_LOCAL_VISION_ENDPOINT=http://127.0.0.1:11434",
            ]
        ),
        encoding="utf-8",
    )
    (tmp_path / ".env").write_text(
        "\n".join(
            [
                "OPENCLAW_LOCAL_VISION_BACKEND=ollama",
                "OPENCLAW_LOCAL_VISION_MODEL=qwen2.5vl:7b,gemma3:12b",
            ]
        ),
        encoding="utf-8",
    )
    settings = AssistantSettings(
        openclaw_local_text_backend=None,
        openclaw_local_text_model=None,
        openclaw_local_text_endpoint="http://127.0.0.1:11434",
        openclaw_local_text_timeout_seconds=45,
        openclaw_local_vision_backend=None,
        openclaw_local_vision_model=None,
        openclaw_local_vision_endpoint="http://127.0.0.1:11434",
        openclaw_local_vision_timeout_seconds=180,
    )

    text = _build_status_text(settings)

    assert "text routing: active=disabled / none | configured=ollama / qwen3:4b (4B) | timeout=75s" in text
    assert "image scan vision: active=disabled / none | configured=ollama / qwen2.5vl:7b (7B), gemma3:12b (12B) | timeout=180s" in text


def test_parse_reputation_snapshot_command_requires_url() -> None:
    query = parse_reputation_snapshot_command("https://jp.mercari.com/item/m123456789")

    assert query == TelegramReputationQuery(query_url="https://jp.mercari.com/item/m123456789")


def test_command_processor_handles_snapshot_command() -> None:
    settings = AssistantSettings(openclaw_telegram_chat_id="123")
    processor = TelegramCommandProcessor(
        settings=settings,
        lookup_renderer=lambda query: query.name,
        board_loader=lambda: (_stub_board(),),
        catalog_renderer=lambda: "catalog",
        reputation_renderer=lambda query: f"snapshot:{query.query_url}",
    )

    reply = processor.build_reply(chat_id="123", text="/snapshot https://jp.mercari.com/item/m123456789")

    assert reply == "snapshot:https://jp.mercari.com/item/m123456789"


def test_command_processor_handles_web_search_command() -> None:
    settings = AssistantSettings(openclaw_telegram_chat_id="123")
    seen: list[TelegramResearchQuery] = []
    processor = TelegramCommandProcessor(
        settings=settings,
        lookup_renderer=lambda query: query.name,
        board_loader=lambda: (_stub_board(),),
        catalog_renderer=lambda: "catalog",
        research_renderer=lambda query: seen.append(query) or "已整理搜尋結果。\n參考來源：\nhttps://example.com/source",
    )

    reply = processor.build_reply(chat_id="123", text="/search why Pikachu Pokemon cards are popular")

    assert reply == "已整理搜尋結果。\n參考來源：\nhttps://example.com/source"
    assert seen == [TelegramResearchQuery(query="why Pikachu Pokemon cards are popular")]


def test_format_reputation_snapshot_result_shows_proof_link() -> None:
    text = format_reputation_snapshot_result(
        type(
            "Result",
            (),
            {
                "proof_url": "http://127.0.0.1:5000/p/proof_123",
                "proof_id": "proof_123",
                "reused": True,
            },
        )()
    )

    assert "信譽快照已就緒" in text
    assert "沿用既有快照" in text
    assert "proof_123" in text
    assert "http://127.0.0.1:5000/p/proof_123" in text


def test_command_processor_handles_natural_language_lookup_via_router() -> None:
    settings = AssistantSettings(openclaw_telegram_chat_id="123")
    router = StubNaturalLanguageRouter(
        TelegramNaturalLanguageIntent(
            intent="lookup_card",
            game="pokemon",
            name="Pikachu ex",
            card_number="132/106",
            rarity="SAR",
            set_code="sv08",
            confidence=0.98,
        )
    )
    processor = TelegramCommandProcessor(
        settings=settings,
        lookup_renderer=lambda query: f"{query.game}:{query.name}:{query.card_number}:{query.rarity}:{query.set_code}",
        board_loader=lambda: (_stub_board(),),
        catalog_renderer=lambda: "catalog",
        natural_language_router=router,
    )

    reply = processor.build_reply(chat_id="123", text="幫我查 pokemon Pikachu ex 132/106 SAR sv08")

    assert reply == "pokemon:Pikachu ex:132/106:SAR:sv08"
    assert router.seen_texts == ["幫我查 pokemon Pikachu ex 132/106 SAR sv08"]


def test_command_processor_handles_natural_language_trend_via_router() -> None:
    settings = AssistantSettings(openclaw_telegram_chat_id="123")
    router = StubNaturalLanguageRouter(
        TelegramNaturalLanguageIntent(
            intent="trend_board",
            game="pokemon",
            limit=3,
            confidence=0.91,
        )
    )
    processor = TelegramCommandProcessor(
        settings=settings,
        lookup_renderer=lambda query: query.name,
        board_loader=lambda: (_stub_board(),),
        catalog_renderer=lambda: "catalog",
        natural_language_router=router,
    )

    reply = processor.build_reply(chat_id="123", text="pokemon 熱門前 3")

    assert "Pokemon Liquidity Board" in reply
    assert router.seen_texts == ["pokemon 熱門前 3"]


def test_command_processor_builds_ack_for_natural_language_trend() -> None:
    settings = AssistantSettings(openclaw_telegram_chat_id="123")
    router = StubNaturalLanguageRouter(
        TelegramNaturalLanguageIntent(
            intent="trend_board",
            game="ws",
            limit=5,
            confidence=0.94,
        )
    )
    processor = TelegramCommandProcessor(
        settings=settings,
        lookup_renderer=lambda query: query.name,
        board_loader=lambda: (
            HotCardBoard(
                game="ws",
                label="WS Liquidity Board",
                methodology="stub methodology",
                generated_at=datetime.now(timezone.utc),
                items=_stub_board().items,
            ),
        ),
        catalog_renderer=lambda: "catalog",
        natural_language_router=router,
    )

    plan = processor.build_reply_plan(chat_id="123", text="ws 熱門前 5")

    assert plan.ack == "已理解查詢內容，相當於 /trend ws 5，開始整理資料。"
    reply = plan.execute()
    assert reply is not None
    assert "WS Liquidity Board" in reply
    assert router.seen_texts == ["ws 熱門前 5"]


def test_command_processor_handles_natural_language_status_via_router() -> None:
    settings = AssistantSettings(openclaw_telegram_chat_id="123")
    router = StubNaturalLanguageRouter(
        TelegramNaturalLanguageIntent(
            intent="status",
            confidence=0.96,
        )
    )
    processor = TelegramCommandProcessor(
        settings=settings,
        lookup_renderer=lambda query: query.name,
        board_loader=lambda: (_stub_board(),),
        catalog_renderer=lambda: "catalog",
        natural_language_router=router,
        status_renderer=lambda: "runtime ok",
    )

    reply = processor.build_reply(chat_id="123", text="你現在狀態如何")

    assert reply == "runtime ok"
    assert router.seen_texts == ["你現在狀態如何"]


def test_command_processor_handles_natural_language_tools_via_router() -> None:
    settings = AssistantSettings(openclaw_telegram_chat_id="123")
    router = StubNaturalLanguageRouter(
        TelegramNaturalLanguageIntent(
            intent="tools",
            confidence=0.94,
        )
    )
    processor = TelegramCommandProcessor(
        settings=settings,
        lookup_renderer=lambda query: query.name,
        board_loader=lambda: (_stub_board(),),
        catalog_renderer=lambda: "tool catalog",
        natural_language_router=router,
    )

    reply = processor.build_reply(chat_id="123", text="把所有工具列出來")

    assert reply == "tool catalog"
    assert router.seen_texts == ["把所有工具列出來"]


def test_command_processor_handles_natural_language_scan_help_via_router() -> None:
    settings = AssistantSettings(openclaw_telegram_chat_id="123")
    router = StubNaturalLanguageRouter(
        TelegramNaturalLanguageIntent(
            intent="scan_help",
            confidence=0.93,
        )
    )
    processor = TelegramCommandProcessor(
        settings=settings,
        lookup_renderer=lambda query: query.name,
        board_loader=lambda: (_stub_board(),),
        catalog_renderer=lambda: "catalog",
        natural_language_router=router,
    )

    reply = processor.build_reply(chat_id="123", text="我要怎麼用照片查價")

    assert reply == "Send a card photo with the caption /scan pokemon or /scan ws, and I will parse it and then look up the price."
    assert router.seen_texts == ["我要怎麼用照片查價"]


def test_command_processor_handles_natural_language_web_research_via_router() -> None:
    settings = AssistantSettings(openclaw_telegram_chat_id="123")
    router = StubNaturalLanguageRouter(
        TelegramNaturalLanguageIntent(
            intent="web_research",
            research_query="why Pikachu Pokemon cards are popular",
            confidence=0.91,
        )
    )
    seen: list[TelegramResearchQuery] = []
    processor = TelegramCommandProcessor(
        settings=settings,
        lookup_renderer=lambda query: query.name,
        board_loader=lambda: (_stub_board(),),
        catalog_renderer=lambda: "catalog",
        natural_language_router=router,
        research_renderer=lambda query: seen.append(query) or "皮卡丘是寶可夢代表角色之一 [1]。\n\n參考來源：\n[1] Source\nhttps://example.com/source",
    )

    plan = processor.build_reply_plan(chat_id="123", text="為什麼皮卡丘的寶可夢卡這麼受歡迎？")

    assert plan.ack == "已理解：相當於 /search why Pikachu Pokemon cards are popular，正在搜尋資料來源並整理答案…"
    assert plan.execute() == "皮卡丘是寶可夢代表角色之一 [1]。\n\n參考來源：\n[1] Source\nhttps://example.com/source"
    assert seen == [TelegramResearchQuery(query="why Pikachu Pokemon cards are popular")]
    assert router.seen_texts == ["為什麼皮卡丘的寶可夢卡這麼受歡迎？"]


def test_command_processor_handles_natural_language_opportunity_remove_via_router() -> None:
    import re as _re

    settings = AssistantSettings(openclaw_telegram_chat_id="123")
    router = StubNaturalLanguageRouter(
        TelegramNaturalLanguageIntent(
            intent="opportunity_remove",
            opportunity_target="2",
            confidence=0.92,
        )
    )
    seen: list[str] = []

    def _stub_hunt(remainder: str, chat_id: str) -> str:
        m = _re.match(r"^(?:remove|dismiss)\s+(.+)$", remainder.strip(), _re.IGNORECASE)
        target = m.group(1).strip() if m else remainder.strip()
        seen.append(target)
        return f"removed:{target}"

    processor = TelegramCommandProcessor(
        settings=settings,
        lookup_renderer=lambda query: query.name,
        board_loader=lambda: (_stub_board(),),
        catalog_renderer=lambda: "catalog",
        natural_language_router=router,
        command_handlers={"/hunt": RegisteredCommand(_stub_hunt)},
    )

    plan = processor.build_reply_plan(chat_id="123", text="把機會清單的第 2 筆移除")

    assert plan.ack == "已理解：相當於 /hunt remove 2，正在移除。"
    assert plan.execute() == "removed:2"
    assert seen == ["2"]
    assert router.seen_texts == ["把機會清單的第 2 筆移除"]


def test_auto_translate_detection_matrix() -> None:
    from openclaw_adapter.telegram_bot import _looks_like_foreign_text_for_translation as f

    # Japanese (kana) and bare English (no Han) -> auto-translate.
    assert f("お疲れさま、今日は大変だったね")
    assert f("remove target 2 from the opportunity list")
    assert f("why is this card so popular?")
    # Chinese commands (incl. embedded English product names) -> router, not translate.
    assert not f("幫我查 pokemon Pikachu ex 132/106 SAR sv08")
    assert not f("ws 熱門前 5")
    assert not f("把機會清單的第 2 筆移除")
    # Short control tokens -> never hijacked.
    assert not f("ok")
    assert not f("はい")


def test_build_processing_ack_for_heavy_actions() -> None:
    assert build_processing_ack(text="/price pokemon Pikachu ex") == "收到查價指令，開始處理。"
    assert build_processing_ack(text="/trend pokemon") == "收到趨勢榜查詢，開始整理資料。"
    assert build_processing_ack(text="/snapshot https://jp.mercari.com/item/m123456789") == (
        "收到信譽快照查詢，先檢查既有 proof，必要時建立新快照。"
    )
    assert build_processing_ack(text="/search why Pikachu is popular") == "收到搜尋問題，正在找資料來源並整理答案。"
    assert build_processing_ack(has_photo=True) == "收到圖片，開始解析與查價。"
    assert build_processing_ack(text="/ping") is None


def test_handle_telegram_message_sends_ack_then_photo_result() -> None:
    sample_path = get_image_lookup_live_case("pokemon-pikachu-partial-s40").image_path
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
        PHOTO_INTAKE_ACK,
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
        TEXT_INTAKE_ACK,
        "收到查價指令，開始處理。",
        "pokemon:Pikachu ex",
    )
    assert client.sent_messages == list(replies)


def test_handle_telegram_message_clarifies_image_without_caption() -> None:
    sample_path = get_image_lookup_live_case("pokemon-pikachu-partial-s40").image_path
    client = FakeTelegramClient(sample_path=sample_path)
    settings = AssistantSettings(openclaw_telegram_chat_id="123")
    processor = TelegramCommandProcessor(
        settings=settings,
        lookup_renderer=lambda query: query.name,
        board_loader=lambda: (_stub_board(),),
        catalog_renderer=lambda: "catalog",
        photo_intent_analyzer=lambda query: _ambiguous_photo_analysis(),
    )

    replies = handle_telegram_message(
        client=client,
        processor=processor,
        photo_renderer=lambda query: "unused",
        message={
            "chat": {"id": "123"},
            "photo": [{"file_id": "photo-1", "file_size": 128}],
        },
    )

    assert len(replies) == 2
    assert replies[0] == PHOTO_INTAKE_ACK
    assert "1. 要我查這張寶可夢卡市價嗎？" in replies[1]
    assert "4. 都不是，請回答：否，[您的意圖]" in replies[1]


def test_handle_telegram_message_runs_selected_photo_option_after_clarification() -> None:
    sample_path = get_image_lookup_live_case("pokemon-pikachu-partial-s40").image_path
    client = FakeTelegramClient(sample_path=sample_path)
    settings = AssistantSettings(openclaw_telegram_chat_id="123")
    processor = TelegramCommandProcessor(
        settings=settings,
        lookup_renderer=lambda query: query.name,
        board_loader=lambda: (_stub_board(),),
        catalog_renderer=lambda: "catalog",
        photo_intent_analyzer=lambda query: _ambiguous_photo_analysis(),
    )

    handle_telegram_message(
        client=client,
        processor=processor,
        photo_renderer=lambda query: "unused",
        message={
            "chat": {"id": "123"},
            "photo": [{"file_id": "photo-1", "file_size": 128}],
        },
    )
    replies = handle_telegram_message(
        client=client,
        processor=processor,
        photo_renderer=lambda query: f"resolved:{query.caption}:{query.game_hint}",
        message={
            "chat": {"id": "123"},
            "text": "1",
        },
    )

    assert replies == (
        TEXT_INTAKE_ACK,
        "收到，我就照第 1 個方式處理。",
        "resolved:/scan pokemon:pokemon",
    )


def test_handle_telegram_message_supports_photo_override_text() -> None:
    sample_path = get_image_lookup_live_case("pokemon-pikachu-partial-s40").image_path
    client = FakeTelegramClient(sample_path=sample_path)
    settings = AssistantSettings(openclaw_telegram_chat_id="123")
    processor = TelegramCommandProcessor(
        settings=settings,
        lookup_renderer=lambda query: query.name,
        board_loader=lambda: (_stub_board(),),
        catalog_renderer=lambda: "catalog",
        photo_intent_analyzer=lambda query: _ambiguous_photo_analysis(),
    )

    handle_telegram_message(
        client=client,
        processor=processor,
        photo_renderer=lambda query: "unused",
        message={
            "chat": {"id": "123"},
            "photo": [{"file_id": "photo-1", "file_size": 128}],
        },
    )
    replies = handle_telegram_message(
        client=client,
        processor=processor,
        photo_renderer=lambda query: f"resolved:{query.caption}:{query.game_hint}:{query.item_kind_hint}",
        message={
            "chat": {"id": "123"},
            "text": "否，查這張遊戲王卡市價",
        },
    )

    assert replies == (
        TEXT_INTAKE_ACK,
        "收到，我改照你補充的意思處理：查這張遊戲王卡市價",
        "resolved:/scan yugioh:yugioh:card",
    )


def test_handle_telegram_message_sends_snapshot_ack_then_result(tmp_path: Path) -> None:
    client = FakeTelegramClient()
    settings = AssistantSettings(openclaw_telegram_chat_id="123")
    pdf_path = tmp_path / "proof_123.pdf"
    png_path = tmp_path / "proof_123.png"
    pdf_path.write_bytes(b"%PDF-1.4 stub")
    png_path.write_bytes(b"\x89PNG\r\n\x1a\nstub")
    processor = TelegramCommandProcessor(
        settings=settings,
        lookup_renderer=lambda query: f"{query.game}:{query.name}",
        board_loader=lambda: (_stub_board(),),
        catalog_renderer=lambda: "catalog",
        reputation_renderer=lambda query: TelegramReputationDelivery(
            summary_text=f"snapshot:{query.query_url}",
            attachments=(
                TelegramFileAttachment(kind="document", path=pdf_path, caption="Reputation snapshot PDF"),
                TelegramFileAttachment(kind="photo", path=png_path, caption="Reputation snapshot preview"),
            ),
            cleanup_paths=(pdf_path, png_path),
        ),
    )

    replies = handle_telegram_message(
        client=client,
        processor=processor,
        photo_renderer=lambda query: "unused",
        message={
            "chat": {"id": "123"},
            "text": "/snapshot https://jp.mercari.com/item/m123456789",
        },
    )

    assert replies == (
        TEXT_INTAKE_ACK,
        "收到信譽快照查詢，先檢查既有 proof，必要時建立新快照。",
        "snapshot:https://jp.mercari.com/item/m123456789",
    )
    assert client.sent_messages == list(replies)
    assert client.sent_documents == [("proof_123.pdf", "Reputation snapshot PDF")]
    assert client.sent_photos == [("proof_123.png", "Reputation snapshot preview")]
    assert not pdf_path.exists()
    assert not png_path.exists()


def test_default_reputation_renderer_fails_fast_when_agent_cannot_start(monkeypatch) -> None:
    settings = AssistantSettings(
        openclaw_telegram_chat_id="123",
        reputation_agent_admin_token=None,
    )

    def fail_ensure(**kwargs):
        raise RuntimeError("REPUTATION_AGENT_ADMIN_TOKEN is not set")

    monkeypatch.setattr("openclaw_adapter.telegram_bot.ensure_agent_thread", fail_ensure)
    monkeypatch.setattr(
        "openclaw_adapter.telegram_bot.request_reputation_snapshot",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("request_reputation_snapshot should not run")),
    )

    renderer = default_reputation_renderer(settings)

    try:
        renderer(TelegramReputationQuery(query_url="https://jp.mercari.com/item/m123456789"))
    except RuntimeError as exc:
        assert "REPUTATION_AGENT_ADMIN_TOKEN is not set" in str(exc)
    else:  # pragma: no cover - defensive.
        raise AssertionError("Expected renderer to fail immediately when the agent cannot start.")


def test_default_reputation_renderer_fails_fast_when_agent_token_is_invalid(monkeypatch) -> None:
    settings = AssistantSettings(
        openclaw_telegram_chat_id="123",
        reputation_agent_admin_token="wrong-token",
    )

    def fail_ensure(**kwargs):
        raise RuntimeError("REPUTATION_AGENT_ADMIN_TOKEN is invalid for REPUTATION_AGENT_SERVER_URL")

    monkeypatch.setattr("openclaw_adapter.telegram_bot.ensure_agent_thread", fail_ensure)
    monkeypatch.setattr(
        "openclaw_adapter.telegram_bot.request_reputation_snapshot",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("request_reputation_snapshot should not run")),
    )

    renderer = default_reputation_renderer(settings)

    try:
        renderer(TelegramReputationQuery(query_url="https://jp.mercari.com/item/m123456789"))
    except RuntimeError as exc:
        assert "invalid" in str(exc)
    else:  # pragma: no cover - defensive.
        raise AssertionError("Expected renderer to fail immediately when the token is invalid.")


def test_handle_telegram_message_sends_natural_language_ack_then_result() -> None:
    client = FakeTelegramClient()
    settings = AssistantSettings(openclaw_telegram_chat_id="123")
    router = StubNaturalLanguageRouter(
        TelegramNaturalLanguageIntent(
            intent="trend_board",
            game="ws",
            limit=5,
            confidence=0.94,
        )
    )
    processor = TelegramCommandProcessor(
        settings=settings,
        lookup_renderer=lambda query: f"{query.game}:{query.name}",
        board_loader=lambda: (
            HotCardBoard(
                game="ws",
                label="WS Liquidity Board",
                methodology="stub methodology",
                generated_at=datetime.now(timezone.utc),
                items=_stub_board().items,
            ),
        ),
        catalog_renderer=lambda: "catalog",
        natural_language_router=router,
    )

    replies = handle_telegram_message(
        client=client,
        processor=processor,
        photo_renderer=lambda query: "unused",
        message={
            "chat": {"id": "123"},
            "text": "ws 熱門前 5",
        },
    )

    assert replies[0] == TEXT_INTAKE_ACK
    assert replies[1] == "已理解查詢內容，相當於 /trend ws 5，開始整理資料。"
    assert "WS Liquidity Board" in replies[2]
    assert client.sent_messages == list(replies)


def test_handle_telegram_message_sends_natural_language_ack_before_running_heavy_work() -> None:
    client = FakeTelegramClient()
    settings = AssistantSettings(openclaw_telegram_chat_id="123")
    router = StubNaturalLanguageRouter(
        TelegramNaturalLanguageIntent(
            intent="trend_board",
            game="ws",
            limit=3,
            confidence=0.94,
        )
    )

    def board_loader() -> tuple[HotCardBoard, ...]:
        assert client.sent_messages == [TEXT_INTAKE_ACK, "已理解查詢內容，相當於 /trend ws 3，開始整理資料。"]
        return (
            HotCardBoard(
                game="ws",
                label="WS Liquidity Board",
                methodology="stub methodology",
                generated_at=datetime.now(timezone.utc),
                items=_stub_board().items,
            ),
        )

    processor = TelegramCommandProcessor(
        settings=settings,
        lookup_renderer=lambda query: f"{query.game}:{query.name}",
        board_loader=board_loader,
        catalog_renderer=lambda: "catalog",
        natural_language_router=router,
    )

    replies = handle_telegram_message(
        client=client,
        processor=processor,
        photo_renderer=lambda query: "unused",
        message={
            "chat": {"id": "123"},
            "text": "查ws熱門前3",
        },
    )

    assert replies[0] == TEXT_INTAKE_ACK
    assert replies[1] == "已理解查詢內容，相當於 /trend ws 3，開始整理資料。"
    assert "WS Liquidity Board" in replies[2]


def test_handle_telegram_message_sends_web_research_ack_then_result() -> None:
    client = FakeTelegramClient()
    settings = AssistantSettings(openclaw_telegram_chat_id="123")
    processor = TelegramCommandProcessor(
        settings=settings,
        lookup_renderer=lambda query: f"{query.game}:{query.name}",
        board_loader=lambda: (_stub_board(),),
        catalog_renderer=lambda: "catalog",
        research_renderer=lambda query: "皮卡丘是寶可夢代表角色之一 [1]。\n\n參考來源：\n[1] Source\nhttps://example.com/source",
    )

    replies = handle_telegram_message(
        client=client,
        processor=processor,
        photo_renderer=lambda query: "unused",
        message={
            "chat": {"id": "123"},
            "text": "/search why Pikachu Pokemon cards are popular",
        },
    )

    assert replies == (
        TEXT_INTAKE_ACK,
        "收到搜尋問題，正在找資料來源並整理答案。",
        "皮卡丘是寶可夢代表角色之一 [1]。\n\n參考來源：\n[1] Source\nhttps://example.com/source",
    )
    assert client.sent_messages == list(replies)


def _mixed_grade_lookup_result() -> TcgLookupResult:
    now = datetime.now(timezone.utc)
    offers = (
        MarketOffer(source="cardrush_pokemon", listing_id="a1", url="https://cardrush.example/a1",
                    title="ピカチュウex 132/106", price_jpy=32800, price_kind="ask",
                    captured_at=now, source_category="marketplace",
                    attributes={"card_number": "132/106", "rarity": "SAR"}),
        MarketOffer(source="cardrush_pokemon", listing_id="a2", url="https://cardrush.example/a2",
                    title="ピカチュウex 132/106", price_jpy=28000, price_kind="bid",
                    captured_at=now, source_category="marketplace",
                    attributes={"card_number": "132/106", "rarity": "SAR"}),
        MarketOffer(source="magi", listing_id="m1", url="https://magi.example/m1",
                    title="ピカチュウex 132/106", price_jpy=30500, price_kind="market",
                    captured_at=now, source_category="marketplace",
                    attributes={"card_number": "132/106", "rarity": "SAR"}),
        MarketOffer(source="cardrush_pokemon", listing_id="p1", url="https://cardrush.example/p1",
                    title="【PSA10】ピカチュウex 132/106", price_jpy=98000, price_kind="ask",
                    captured_at=now, source_category="marketplace", condition="graded",
                    attributes={"card_number": "132/106", "rarity": "SAR", "is_graded": "1", "grade_label": "PSA10"}),
        MarketOffer(source="cardrush_pokemon", listing_id="p2", url="https://cardrush.example/p2",
                    title="【PSA10】ピカチュウex 132/106", price_jpy=88000, price_kind="bid",
                    captured_at=now, source_category="marketplace", condition="graded",
                    attributes={"card_number": "132/106", "rarity": "SAR", "is_graded": "1", "grade_label": "PSA10"}),
        MarketOffer(source="magi", listing_id="p3", url="https://magi.example/p3",
                    title="【PSA10】ピカチュウex 132/106", price_jpy=92000, price_kind="market",
                    captured_at=now, source_category="marketplace", condition="graded",
                    attributes={"card_number": "132/106", "rarity": "SAR", "is_graded": "1", "grade_label": "PSA10"}),
    )
    spec = TcgCardSpec(game="pokemon", title="Pikachu ex", card_number="132/106", rarity="SAR")
    item = TrackedItem(item_id="x", item_type="card", category="tcg", title="Pikachu ex")
    fv = FairValueEstimate(item_id="x", amount_jpy=32500, confidence=0.72, sample_count=3, reasoning=())
    return TcgLookupResult(spec=spec, item=item, offers=offers, fair_value=fv, notes=("sample note",))


def test_format_lookup_result_telegram_shows_raw_and_psa10_sections_without_mixed_total_price() -> None:
    text = format_lookup_result_telegram(_mixed_grade_lookup_result())

    assert "Raw" in text
    assert "PSA 10" in text

    raw_section = text.split("Raw", 1)[1].split("PSA 10", 1)[0]
    psa_section = text.split("PSA 10", 1)[1].split("Sources:", 1)[0]
    header_section = text.split("Raw", 1)[0]

    for section_name, section in (("raw", raw_section), ("psa10", psa_section)):
        assert "Fair Value:" in section, f"{section_name} section missing Fair Value"
        assert "Avg Price:" in section, f"{section_name} section missing Avg Price"
        assert "Best Bid:" in section, f"{section_name} section missing Best Bid"
        assert "Best Ask:" in section, f"{section_name} section missing Best Ask"
        assert "Best Market:" in section, f"{section_name} section missing Best Market"

    assert "Fair Value: ￥32,500" not in header_section
    assert "Fair Value: ￥32,800" in raw_section
    assert "￥31,100" in raw_section or "￥31,200" in raw_section or "￥31,650" in raw_section
    assert "￥28,000" in raw_section
    assert "￥32,800" in raw_section
    assert "￥30,500" in raw_section
    assert "Source URL: https://magi.example/m1" in raw_section
    assert "Fair Value: ￥98,000" in psa_section
    assert "￥95,000" in psa_section
    assert "￥88,000" in psa_section
    assert "￥98,000" in psa_section
    assert "￥92,000" in psa_section
    assert "Source URL: https://magi.example/p3" in psa_section
    assert "Offers:" not in text


def test_format_lookup_result_telegram_has_no_scan_or_note_noise() -> None:
    text = format_lookup_result_telegram(_mixed_grade_lookup_result())

    for forbidden in ("Image scan result", "Detected game", "Detected card", "Detected fields", "Note:", "sample note"):
        assert forbidden not in text, f"Telegram output should not contain {forbidden!r}: {text!r}"


def test_format_photo_lookup_result_is_identical_to_telegram_lookup_result() -> None:
    lookup_result = _mixed_grade_lookup_result()
    parsed = ParsedCardImage(
        status="success",
        game="pokemon",
        title="Pikachu ex",
        aliases=(),
        card_number="132/106",
        rarity="SAR",
        set_code=None,
        raw_text="",
        extracted_lines=(),
    )
    outcome = TcgImageLookupOutcome(
        status="success",
        parsed=parsed,
        lookup_result=lookup_result,
        warnings=("sample warning",),
    )

    text = format_photo_lookup_result(outcome)

    assert text == format_lookup_result_telegram(lookup_result)
    for forbidden in ("Image scan result", "Detected game", "Detected card", "Detected fields", "sample warning", "sample note"):
        assert forbidden not in text


def test_format_lookup_result_telegram_detects_psa10_from_title_with_spacing() -> None:
    now = datetime.now(timezone.utc)
    lookup_result = TcgLookupResult(
        spec=TcgCardSpec(game="pokemon", title="Pikachu ex", card_number="132/106", rarity="SAR"),
        item=TrackedItem(item_id="x", item_type="card", category="tcg", title="Pikachu ex"),
        offers=(
            MarketOffer(
                source="magi",
                listing_id="p1",
                url="https://magi.example/p1",
                title="PSA 10 ピカチュウex 132/106",
                price_jpy=92000,
                price_kind="market",
                captured_at=now,
                source_category="marketplace",
                condition="graded",
            ),
        ),
        fair_value=None,
    )

    text = format_lookup_result_telegram(lookup_result)

    assert "PSA 10" in text
    assert "其他鑑定卡" not in text


def test_format_liquidity_board_includes_reference_url() -> None:
    text = format_liquidity_board(_stub_board(), limit=1)

    assert "https://example.com/rank" in text
    assert "liq 88.20" in text
    assert "attn 41.70" in text
    assert "support 90.08" in text
    assert "buy-up" in text
    assert "stub methodology" not in text


def test_research_seller_snapshot_lookup_extracts_negative_review_excerpts(monkeypatch) -> None:
    monkeypatch.setattr(
        "openclaw_adapter.telegram_bot.request_reputation_snapshot",
        lambda *, settings, query_url: ReputationSnapshotResult(
            proof_url="http://127.0.0.1:5000/p/proof_123",
            proof_id="proof_123",
            reused=False,
        ),
    )
    monkeypatch.setattr(
        "openclaw_adapter.telegram_bot.fetch_reputation_proof_document",
        lambda *, settings, proof_id: {
            "subject": {"display_name": "risk seller"},
            "metrics": {"total_reviews": 55, "listing_count": 8},
            "quality": {"as_seller": {"positive": 20, "negative": 2, "rate": 90.9}},
            "review_entries": [
                {"role": "seller", "rating": "negative", "body_excerpt": "発送が遅かったです。"},
                {"role": "buyer", "rating": "negative", "body_excerpt": "buyer side"},
                {"role": "seller", "rating": "positive", "body_excerpt": "positive"},
                {"role": "seller", "rating": "negative", "body_excerpt": "梱包が雑でした。"},
                {"role": "seller", "rating": "negative", "body_excerpt": "発送が遅かったです。"},
            ],
        },
    )
    settings = AssistantSettings(reputation_agent_server_url="http://127.0.0.1:5000")

    lookup = _build_research_seller_snapshot_lookup(settings)
    snapshot = lookup("https://jp.mercari.com/user/profile/123")

    assert snapshot.display_name == "risk seller"
    assert snapshot.seller_negative == 2
    assert snapshot.seller_negative_excerpts == ("発送が遅かったです。", "梱包が雑でした。")


def test_research_seller_snapshot_lookup_proof_fetch_timeout_pends(monkeypatch) -> None:
    # Regression (issue #6): the snapshot job completed, but the proof-document
    # fetch timed out transiently. This must not surface as a permanent failure;
    # it should raise SnapshotStillPending so /research schedules the background
    # follow-up that re-fetches the proof.
    monkeypatch.setattr(
        "openclaw_adapter.telegram_bot.request_reputation_snapshot",
        lambda *, settings, query_url: ReputationSnapshotResult(
            proof_url="http://127.0.0.1:5000/p/proof_done",
            proof_id="proof_done",
            reused=False,
            job_id="job_done",
        ),
    )

    def _timing_out_fetch(*, settings, proof_id):
        raise RuntimeError("reputation_snapshot request failed: timed out")

    monkeypatch.setattr(
        "openclaw_adapter.telegram_bot.fetch_reputation_proof_document",
        _timing_out_fetch,
    )
    settings = AssistantSettings(reputation_agent_server_url="http://127.0.0.1:5000")

    lookup = _build_research_seller_snapshot_lookup(settings)

    import pytest

    with pytest.raises(SnapshotStillPending) as exc_info:
        lookup("https://jp.mercari.com/user/profile/123")
    assert exc_info.value.job_id == "job_done"
    assert callable(exc_info.value.poll_fn)


# --- Phase 1: /research appreciation cloud offload ---------------------------


def _research_enricher_settings(**overrides) -> AssistantSettings:
    base = dict(
        openclaw_telegram_chat_id="123",
        openclaw_local_text_backend="ollama",
        openclaw_local_text_endpoint="http://127.0.0.1:11434",
        openclaw_local_text_model="qwen3:14b",
    )
    base.update(overrides)
    return AssistantSettings(**base)


class _FakeCloudTextClient:
    def __init__(self, *, reply: str = "", raises: Exception | None = None) -> None:
        self._reply = reply
        self._raises = raises
        self.calls = 0

    def generate(self, prompt: str, *, temperature: float = 0.0, think: bool = False) -> str:
        self.calls += 1
        if self._raises is not None:
            raise self._raises
        return self._reply


def _one_appreciation_result():
    from openclaw_adapter.web_search import WebSearchResult

    return (WebSearchResult(title="t1", url="https://a.example/1", snippet="s1"),)


def test_research_enricher_uses_cloud_summary(monkeypatch) -> None:
    from openclaw_adapter.telegram_bot import _build_research_appreciation_enricher

    fake = _FakeCloudTextClient(reply="雲端增值結論 [1]")
    monkeypatch.setattr(
        "openclaw_adapter.telegram_bot.build_research_cloud_text_client",
        lambda settings: fake,
    )
    monkeypatch.setattr(
        "openclaw_adapter.telegram_bot.fetch_page_text", lambda url, **kw: "page text"
    )

    def _local_must_not_run(*args, **kwargs):
        raise AssertionError("local summarize must not run on cloud success")

    monkeypatch.setattr(
        "openclaw_adapter.telegram_bot.summarize_web_sources_with_ollama",
        _local_must_not_run,
    )

    settings = _research_enricher_settings(openclaw_research_cloud_enricher="opencode")
    enricher = _build_research_appreciation_enricher(settings)
    assert enricher is not None

    out = enricher("某卡 行情", _one_appreciation_result())
    assert out == "雲端增值結論 [1]"
    assert fake.calls == 1


def test_research_enricher_falls_back_to_local_on_cloud_outage(monkeypatch) -> None:
    from openclaw_adapter.dynamic_tools import CloudBackendUnavailable
    from openclaw_adapter.telegram_bot import _build_research_appreciation_enricher

    fake = _FakeCloudTextClient(raises=CloudBackendUnavailable("cloud down"))
    monkeypatch.setattr(
        "openclaw_adapter.telegram_bot.build_research_cloud_text_client",
        lambda settings: fake,
    )
    monkeypatch.setattr(
        "openclaw_adapter.telegram_bot.fetch_page_text", lambda url, **kw: "page text"
    )
    local_calls = {"n": 0}

    def _local_summarize(q, sources, **kwargs):
        local_calls["n"] += 1
        return "地端 fallback 結論"

    monkeypatch.setattr(
        "openclaw_adapter.telegram_bot.summarize_web_sources_with_ollama",
        _local_summarize,
    )

    settings = _research_enricher_settings(openclaw_research_cloud_enricher="opencode")
    enricher = _build_research_appreciation_enricher(settings)

    out = enricher("某卡 行情", _one_appreciation_result())
    assert out == "地端 fallback 結論"
    assert fake.calls == 1
    assert local_calls["n"] == 1


def test_research_enricher_local_path_keeps_relevance_gate_when_cloud_disabled(monkeypatch) -> None:
    from openclaw_adapter.telegram_bot import _build_research_appreciation_enricher

    monkeypatch.setattr(
        "openclaw_adapter.telegram_bot.build_research_cloud_text_client",
        lambda settings: None,
    )
    monkeypatch.setattr(
        "openclaw_adapter.telegram_bot.fetch_page_text", lambda url, **kw: "page text"
    )
    monkeypatch.setattr(
        "openclaw_adapter.telegram_bot.summarize_web_sources_with_ollama",
        lambda q, sources, **kwargs: "地端結論",
    )
    relevance = {"called": False}

    def _filter(q, sources, **kwargs):
        relevance["called"] = True
        return sources

    monkeypatch.setattr(
        "openclaw_adapter.telegram_bot.filter_relevant_sources_with_ollama", _filter
    )

    settings = _research_enricher_settings()
    enricher = _build_research_appreciation_enricher(settings)
    assert enricher is not None

    out = enricher("某卡 行情", _one_appreciation_result())
    assert out == "地端結論"
    assert relevance["called"] is True


def test_research_enricher_none_when_no_backend(monkeypatch) -> None:
    from openclaw_adapter.telegram_bot import _build_research_appreciation_enricher

    monkeypatch.setattr(
        "openclaw_adapter.telegram_bot.build_research_cloud_text_client",
        lambda settings: None,
    )
    settings = AssistantSettings(openclaw_telegram_chat_id="123")
    assert _build_research_appreciation_enricher(settings) is None
