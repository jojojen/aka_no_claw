from __future__ import annotations

import pytest
from assistant_runtime.settings import AssistantSettings
from openclaw_adapter.natural_language import (
    _select_router_model,
    build_telegram_natural_language_router_from_settings,
)
from price_monitor_bot.natural_language import fallback_route_telegram_natural_language


# ── Router settings tests ─────────────────────────────────────────────────────

def test_natural_language_router_is_disabled_without_text_backend() -> None:
    settings = AssistantSettings(
        openclaw_local_text_backend=None,
        openclaw_local_text_model="gemma3:1b",
        openclaw_local_vision_model="gemma3:1b",
    )

    assert build_telegram_natural_language_router_from_settings(settings) is None


def test_select_router_model_prefers_strongest_available_local_model() -> None:
    settings = AssistantSettings(
        openclaw_local_text_backend="ollama",
        openclaw_local_text_model="qwen3:4b",
        openclaw_local_vision_model="qwen2.5vl:7b,gemma3:12b",
    )

    assert _select_router_model(settings) == "gemma3:12b"


def test_natural_language_router_loads_tool_spec_from_file() -> None:
    settings = AssistantSettings(
        openclaw_local_text_backend="ollama",
        openclaw_local_text_model="gemma3:4b",
        openclaw_local_vision_model=None,
    )

    router = build_telegram_natural_language_router_from_settings(settings)

    assert router is not None
    assert len(router.tool_spec) > 0
    assert "lookup_card" in router.tool_spec
    assert "trend_board" in router.tool_spec
    assert "reputation_snapshot" in router.tool_spec
    assert "web_research" in router.tool_spec


# ── /help — capability / usage questions ─────────────────────────────────────

def test_fallback_routes_capability_question_to_help() -> None:
    result = fallback_route_telegram_natural_language("你會什麼")

    assert result is not None
    assert result.intent == "help"


def test_fallback_routes_usage_question_to_help() -> None:
    result = fallback_route_telegram_natural_language("怎麼用這個機器人")

    assert result is not None
    assert result.intent == "help"


# ── /status — runtime / model / service-health questions ─────────────────────

def test_fallback_routes_runtime_question_to_status() -> None:
    # Canonical "狀態" phrasing — the fallback intentionally no longer covers
    # synonym sprawl like "目前狀況"; that case falls through to the LLM in prod.
    result = fallback_route_telegram_natural_language("目前狀態如何")

    assert result is not None
    assert result.intent == "status"


def test_fallback_routes_model_question_to_status() -> None:
    # Model-introspection questions are now LLM-only (slimmed keyword list);
    # the fallback safety net still covers explicit "health" / "狀態" phrasings.
    result = fallback_route_telegram_natural_language("service health check 狀態")

    assert result is not None
    assert result.intent == "status"


def test_fallback_routes_health_keyword_to_status() -> None:
    result = fallback_route_telegram_natural_language("bot health check")

    assert result is not None
    assert result.intent == "status"


# ── /tools — full tool catalog ───────────────────────────────────────────────

def test_fallback_routes_all_tools_request_to_tools() -> None:
    result = fallback_route_telegram_natural_language("列出所有工具")

    assert result is not None
    assert result.intent == "tools"


def test_fallback_routes_capabilities_request_to_tools() -> None:
    result = fallback_route_telegram_natural_language("有哪些可用工具")

    assert result is not None
    assert result.intent == "tools"


def test_fallback_routes_catalog_keyword_to_tools() -> None:
    result = fallback_route_telegram_natural_language("功能清單")

    assert result is not None
    assert result.intent == "tools"


# ── /price — single-card price lookup ────────────────────────────────────────

def test_fallback_routes_pokemon_price_lookup_with_card_number_and_rarity() -> None:
    result = fallback_route_telegram_natural_language("幫我查寶可夢 リザードンex 201/165 SAR")

    assert result is not None
    assert result.intent == "lookup_card"
    assert result.game == "pokemon"
    assert result.card_number == "201/165"
    assert result.rarity == "SAR"


def test_fallback_routes_ws_price_lookup_with_rarity() -> None:
    result = fallback_route_telegram_natural_language("ws 初音ミク SSP 價格")

    assert result is not None
    assert result.intent == "lookup_card"
    assert result.game == "ws"
    assert result.rarity == "SSP"


def test_fallback_routes_ptcg_card_valuation() -> None:
    result = fallback_route_telegram_natural_language("ptcg Pikachu ex 估價")

    assert result is not None
    assert result.intent == "lookup_card"
    assert result.game == "pokemon"


def test_fallback_routes_yugioh_price_lookup_with_card_number() -> None:
    result = fallback_route_telegram_natural_language("查遊戯王 青眼の白龍 QCCP-JP001 UR 價格")

    assert result is not None
    assert result.intent == "lookup_card"
    assert result.game == "yugioh"
    assert result.name == "青眼の白龍"
    assert result.card_number == "QCCP-JP001"


def test_fallback_routes_union_arena_price_lookup_with_card_number() -> None:
    result = fallback_route_telegram_natural_language("Union Area UAPR/EVA-1-71 綾波レイ 估價")

    assert result is not None
    assert result.intent == "lookup_card"
    assert result.game == "union_arena"
    assert result.name == "綾波レイ"
    assert result.card_number == "UAPR/EVA-1-71"


# ── /trend /hot /liquidity — hot / trending / liquidity boards ───────────────

def test_fallback_routes_pokemon_trend_board_with_limit() -> None:
    result = fallback_route_telegram_natural_language("pokemon 熱門前5")

    assert result is not None
    assert result.intent == "trend_board"
    assert result.game == "pokemon"
    assert result.limit == 5


def test_fallback_routes_ws_trending_with_top_limit() -> None:
    result = fallback_route_telegram_natural_language("ws trending top 3")

    assert result is not None
    assert result.intent == "trend_board"
    assert result.game == "ws"
    assert result.limit == 3


def test_fallback_routes_pokemon_liquidity_board() -> None:
    result = fallback_route_telegram_natural_language("寶可夢流動性排行")

    assert result is not None
    assert result.intent == "trend_board"
    assert result.game == "pokemon"


def test_fallback_returns_none_for_trend_without_game() -> None:
    # Trend keywords present but no game specified → cannot route
    result = fallback_route_telegram_natural_language("最近什麼熱門排行")

    assert result is None


# ── /search — sourced web research questions ─────────────────────────────────

def test_fallback_routes_tcg_why_question_to_web_research() -> None:
    result = fallback_route_telegram_natural_language("why pokemon card pickachu card is so popular?")

    assert result is not None
    assert result.intent == "web_research"
    assert result.research_query == "why pokemon card pickachu card is so popular"


def test_fallback_does_not_route_unrelated_weather_question_to_web_research() -> None:
    result = fallback_route_telegram_natural_language("why is tomorrow weather so hot?")

    assert result is None


# ── /hunt remove — opportunity target controls ───────────────────────────────

def test_fallback_routes_opportunity_target_remove_by_number() -> None:
    result = fallback_route_telegram_natural_language("remove target 2 from the opportunity list")

    assert result is not None
    assert result.intent == "opportunity_remove"
    assert result.opportunity_target == "2"


def test_fallback_routes_opportunity_target_remove_by_name() -> None:
    result = fallback_route_telegram_natural_language("I am not interested in Umbreon ex SAR anymore")

    assert result is not None
    assert result.intent == "opportunity_remove"
    assert result.opportunity_target == "Umbreon ex SAR"


# ── /snapshot — Mercari reputation snapshot ───────────────────────────────────

def test_fallback_routes_reputation_query_with_url() -> None:
    result = fallback_route_telegram_natural_language(
        "查詢信用 https://jp.mercari.com/item/m12345"
    )

    assert result is not None
    assert result.intent == "reputation_snapshot"
    assert result.query_url == "https://jp.mercari.com/item/m12345"


def test_fallback_routes_seller_trust_question_with_url() -> None:
    result = fallback_route_telegram_natural_language(
        "這個賣家信譽如何 https://jp.mercari.com/item/m99999"
    )

    assert result is not None
    assert result.intent == "reputation_snapshot"
    assert result.query_url == "https://jp.mercari.com/item/m99999"


def test_fallback_routes_snapshot_keyword_with_url() -> None:
    result = fallback_route_telegram_natural_language(
        "snapshot https://jp.mercari.com/item/m12345"
    )

    assert result is not None
    assert result.intent == "reputation_snapshot"
    assert result.query_url == "https://jp.mercari.com/item/m12345"


# ── Photo scan — image lookup instructions ───────────────────────────────────

def test_fallback_routes_photo_price_instructions_to_scan_help() -> None:
    result = fallback_route_telegram_natural_language("我要怎麼用照片查價")

    assert result is not None
    assert result.intent == "scan_help"


def test_fallback_routes_scan_image_question_to_scan_help() -> None:
    result = fallback_route_telegram_natural_language("如何掃圖查詢")

    assert result is not None
    assert result.intent == "scan_help"


def test_fallback_routes_ocr_question_to_scan_help() -> None:
    result = fallback_route_telegram_natural_language("OCR 怎麼用")

    assert result is not None
    assert result.intent == "scan_help"


# ── /watch — add a Mercari watch ─────────────────────────────────────────────

def test_fallback_routes_watch_add_with_kanji_price_threshold() -> None:
    result = fallback_route_telegram_natural_language("追蹤 初音ミク SSP 5萬以下")

    assert result is not None
    assert result.intent == "add_watch"
    assert result.watch_price_threshold == 50000


def test_fallback_routes_alert_watch_add_with_plain_digits() -> None:
    result = fallback_route_telegram_natural_language("提醒我 Pikachu ex 低於 30000")

    assert result is not None
    assert result.intent == "add_watch"
    assert result.watch_price_threshold == 30000


def test_fallback_routes_watch_add_with_complex_kanji_price() -> None:
    result = fallback_route_telegram_natural_language("alert ws Aqua SSR 三十萬以内")

    assert result is not None
    assert result.intent == "add_watch"
    assert result.watch_price_threshold == 300000


def test_fallback_routes_sns_account_filter_update_with_full_width_brackets() -> None:
    result = fallback_route_telegram_natural_language("幫我把@tenbai_hakase 加上 ［抽選］ 篩選")

    assert result is not None
    assert result.intent == "sns_add_account"
    assert result.sns_handle == "tenbai_hakase"
    assert result.sns_include_keywords == ("抽選",)


# ── /watchlist — show current watch list ─────────────────────────────────────

def test_fallback_routes_watchlist_request() -> None:
    result = fallback_route_telegram_natural_language("看我的追蹤清單")

    assert result is not None
    assert result.intent == "list_watches"


def test_fallback_routes_my_watches_request() -> None:
    result = fallback_route_telegram_natural_language("我的追蹤")

    assert result is not None
    assert result.intent == "list_watches"


def test_fallback_routes_watchlist_keyword() -> None:
    result = fallback_route_telegram_natural_language("watchlist")

    assert result is not None
    assert result.intent == "list_watches"


# ── /unwatch — remove a watch ────────────────────────────────────────────────

def test_fallback_routes_cancel_watch_with_id() -> None:
    result = fallback_route_telegram_natural_language("取消追蹤 abc12345678")

    assert result is not None
    assert result.intent == "remove_watch"
    assert result.watch_id == "abc12345678"


def test_fallback_routes_stop_watch_with_id() -> None:
    result = fallback_route_telegram_natural_language("停止追蹤 abc12345678")

    assert result is not None
    assert result.intent == "remove_watch"
    assert result.watch_id == "abc12345678"


def test_fallback_routes_unwatch_keyword_with_id() -> None:
    result = fallback_route_telegram_natural_language("unwatch abc12345678")

    assert result is not None
    assert result.intent == "remove_watch"
    assert result.watch_id == "abc12345678"


# ── /setprice — update price threshold of a watch ────────────────────────────

def test_fallback_routes_setprice_with_kanji_threshold() -> None:
    result = fallback_route_telegram_natural_language("把 abc12345678 改成 4萬")

    assert result is not None
    assert result.intent == "update_watch_price"
    assert result.watch_id == "abc12345678"
    assert result.watch_price_threshold == 40000


def test_fallback_routes_update_watch_price_with_plain_digits() -> None:
    result = fallback_route_telegram_natural_language("幫我更新 abc12345678 的價格上限 30000")

    assert result is not None
    assert result.intent == "update_watch_price"
    assert result.watch_id == "abc12345678"
    assert result.watch_price_threshold == 30000


# ── sns_bulk_add_filter — batch filter update by domain ──────────────────────

def test_fallback_routes_bulk_filter_update_for_pokemon_domain() -> None:
    result = fallback_route_telegram_natural_language(
        "把每個跟pokemon相關的sns 追蹤帳號 filter都加上「抽選」"
    )

    assert result is not None
    assert result.intent == "sns_bulk_add_filter"
    assert result.bulk_target_domain == "pokemon"
    assert result.bulk_filter_keywords == ("抽選",)


def test_fallback_routes_bulk_filter_update_for_tcg_umbrella() -> None:
    result = fallback_route_telegram_natural_language(
        "把每個跟 tcg 相關的 sns 追蹤帳號 filter 都加上「抽選」"
    )

    assert result is not None
    assert result.intent == "sns_bulk_add_filter"
    assert result.bulk_target_domain == "tcg"
    assert result.bulk_filter_keywords == ("抽選",)


def test_fallback_routes_bulk_filter_update_for_yugioh() -> None:
    result = fallback_route_telegram_natural_language(
        "所有遊戲王帳號的 filter 都加上「新弾」"
    )

    assert result is not None
    assert result.intent == "sns_bulk_add_filter"
    assert result.bulk_target_domain == "yugioh"
    assert result.bulk_filter_keywords == ("新弾",)


def test_fallback_routes_bulk_filter_update_for_ws() -> None:
    result = fallback_route_telegram_natural_language(
        "幫所有 ws 帳號 filter 加上「再販」"
    )

    assert result is not None
    assert result.intent == "sns_bulk_add_filter"
    assert result.bulk_target_domain == "ws"
    assert result.bulk_filter_keywords == ("再販",)


def test_fallback_routes_bulk_filter_update_for_union_arena() -> None:
    result = fallback_route_telegram_natural_language(
        "把每個 union arena 帳號的 filter 都加上「抽選」"
    )

    assert result is not None
    assert result.intent == "sns_bulk_add_filter"
    assert result.bulk_target_domain == "union_arena"
    assert result.bulk_filter_keywords == ("抽選",)


def test_single_handle_filter_update_still_routes_to_sns_add_account() -> None:
    """Regression: bulk-filter rescue must not steal single-@handle updates."""
    result = fallback_route_telegram_natural_language(
        "幫我把 @tenbai_hakase 加上 [抽選] 篩選"
    )

    assert result is not None
    assert result.intent == "sns_add_account"
    assert result.sns_handle == "tenbai_hakase"


# ── _normalize_intent accepts sns_bulk_add_filter LLM payload ────────────────

def test_normalize_intent_accepts_bulk_filter_payload() -> None:
    from price_monitor_bot.natural_language import _normalize_intent

    payload = {
        "intent": "sns_bulk_add_filter",
        "bulk_target_domain": "pokemon",
        "bulk_filter_keywords": ["抽選"],
    }
    result = _normalize_intent(payload)

    assert result.intent == "sns_bulk_add_filter"
    assert result.bulk_target_domain == "pokemon"
    assert result.bulk_filter_keywords == ("抽選",)


def test_normalize_intent_aliases_bulk_target_domain_chinese() -> None:
    from price_monitor_bot.natural_language import _normalize_intent

    payload = {
        "intent": "sns_bulk_add_filter",
        "bulk_target_domain": "寶可夢",
        "bulk_filter_keywords": ["抽選"],
    }
    result = _normalize_intent(payload)

    assert result.bulk_target_domain == "pokemon"


# ── sns_clear_filter — clear include_keywords on a single @handle ─────────────

def test_fallback_routes_clear_filter_for_at_handle() -> None:
    result = fallback_route_telegram_natural_language(
        "把 @ARS_Arsales 的 filter 全部拿掉"
    )

    assert result is not None
    assert result.intent == "sns_clear_filter"
    assert result.sns_handle == "ARS_Arsales"


def test_fallback_routes_clear_filter_with_qingkong() -> None:
    result = fallback_route_telegram_natural_language("清空 @elonmusk 的篩選")

    assert result is not None
    assert result.intent == "sns_clear_filter"
    assert result.sns_handle == "elonmusk"


def test_fallback_routes_clear_filter_with_qingchu() -> None:
    result = fallback_route_telegram_natural_language("把 @aka_claw 的關鍵字清除")

    assert result is not None
    assert result.intent == "sns_clear_filter"
    assert result.sns_handle == "aka_claw"


def test_fallback_routes_clear_filter_english() -> None:
    result = fallback_route_telegram_natural_language("clear filter on @aka_claw")

    assert result is not None
    assert result.intent == "sns_clear_filter"
    assert result.sns_handle == "aka_claw"


def test_clear_filter_requires_both_clear_verb_and_filter_hint() -> None:
    """'拿掉 @X' alone (no filter/篩選 keyword) must NOT become sns_clear_filter."""
    result = fallback_route_telegram_natural_language("拿掉 @aka_claw")

    assert result is None or result.intent != "sns_clear_filter"


# ── Regression — sns_delete keeps working for the right phrases ──────────────

def test_delete_zhuizong_still_routes_to_sns_delete() -> None:
    result = fallback_route_telegram_natural_language("刪除追蹤 @elonmusk")

    assert result is not None
    assert result.intent == "sns_delete"
    assert result.sns_handle == "elonmusk"


def test_unfollow_still_routes_to_sns_delete() -> None:
    result = fallback_route_telegram_natural_language("unfollow @elonmusk")

    assert result is not None
    assert result.intent == "sns_delete"


# ── _normalize_intent accepts sns_clear_filter LLM payload ───────────────────

def test_normalize_intent_accepts_clear_filter_payload() -> None:
    from price_monitor_bot.natural_language import _normalize_intent

    payload = {"intent": "sns_clear_filter", "sns_handle": "ARS_Arsales"}
    result = _normalize_intent(payload)

    assert result.intent == "sns_clear_filter"
    assert result.sns_handle == "ARS_Arsales"


# ── unknown — unrelated messages ─────────────────────────────────────────────

def test_fallback_returns_none_for_unrelated_message() -> None:
    result = fallback_route_telegram_natural_language("明天天氣如何")

    assert result is None


def test_fallback_returns_none_for_empty_message() -> None:
    result = fallback_route_telegram_natural_language("")

    assert result is None
