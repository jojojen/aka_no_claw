"""The wrapper registers aka_no_claw's commands as data in four registries that
the base dispatcher consults — adding a new command must never require editing
price_monitor_bot/bot.py. These tests pin the expected registration keys and the
RegisteredCommand contract (handler shape, ack, background flag).

No network, no Ollama, no Telegram — just the registry construction."""
from __future__ import annotations

from assistant_runtime import AssistantSettings
from openclaw_adapter.goal_planner import build_goal_workflow_prompt
from openclaw_adapter.task_workspace import COMMAND_SINK_DENYLIST
from openclaw_adapter.telegram_bot import (
    PHOTO_SCAN_COMMANDS,
    PRICE_LOOKUP_COMMANDS,
    REPUTATION_SNAPSHOT_COMMANDS,
    TREND_BOARD_COMMANDS,
    _build_openclaw_help_text,
    _build_registries,
)
from openclaw_adapter.workflow_command import command_metadata
from price_monitor_bot.bot import RegisteredCommand


def _settings(tmp_path) -> AssistantSettings:
    return AssistantSettings(
        quiz_db_path=str(tmp_path / "quiz.sqlite3"),
        opportunity_db_path=str(tmp_path / "hunt.sqlite3"),
        knowledge_db_path=str(tmp_path / "knowledge.sqlite3"),
    )


def _registries(tmp_path):
    return _build_registries(_settings(tmp_path), None, sns_db=None, buzz_fn=None)


def test_command_registry_has_expected_keys(tmp_path):
    command_handlers, _, _, _ = _registries(tmp_path)
    expected = {
        "/quiz",
        "/quizlikesong",
        "/voice",
        "/generateaudio",
        "/new",
        "/backupclaw",
        "/backup",
        "/clawrecover",
        "/recoverclaw",
        "/stats",
        "/scorecard",
        "/knowledge",
        "/kb",
        "/research",
        "/resaerch",
        "/snsadd",
        "/sns_add",
        "/snslist",
        "/sns_list",
        "/snsdelete",
        "/sns_delete",
        "/snsbuzz",
        "/sns_buzz",
        "/snsclearfilter",
        "/musicmute",
        "/musiclouder",
        "/musiclower",
        "/lookup",
        "/price",
        "/trend",
        "/trending",
        "/hot",
        "/heat",
        "/liquidity",
        "/snapshot",
        "/proof",
        "/repcheck",
        "/reputation",
        "/search",
        "/web",
        "/fetch",
        "/read",
        "/watch",
        "/watchlist",
        "/watches",
        "/unwatch",
        "/stopwatch",
        "/setprice",
        "/updatewatch",
        "/scan",
        "/image",
        "/photo",
    }
    assert expected <= set(command_handlers)
    assert all(isinstance(v, RegisteredCommand) for v in command_handlers.values())


def test_price_monitor_base_command_sets_are_registered(tmp_path):
    command_handlers, _, _, _ = _registries(tmp_path)
    base_commands = (
        PRICE_LOOKUP_COMMANDS
        | TREND_BOARD_COMMANDS
        | PHOTO_SCAN_COMMANDS
        | REPUTATION_SNAPSHOT_COMMANDS
        | {
            "/search",
            "/research",
            "/web",
            "/fetch",
            "/read",
            "/watch",
            "/watchlist",
            "/watches",
            "/unwatch",
            "/stopwatch",
            "/setprice",
            "/updatewatch",
        }
    )
    assert base_commands <= set(command_handlers)


def test_registered_commands_all_share_metadata_usage(tmp_path):
    command_handlers, _, _, _ = _registries(tmp_path)
    assert [c for c in sorted(command_handlers) if not command_metadata(c)] == []
    assert [c for c in sorted(command_handlers) if not command_handlers[c].usage] == []


def test_workflow_prompt_uses_registry_and_filters_text_unsafe_commands(tmp_path):
    command_handlers, _, _, _ = _registries(tmp_path)
    prompt = build_goal_workflow_prompt(
        "播放米津玄師的熱門歌曲",
        catalog=None,
        command_registry=command_handlers,
    )
    assert "/search" in prompt
    assert "/fetch" in prompt
    assert "/research" in prompt
    assert "商品能不能買" in prompt
    assert "Mercari" in prompt
    assert "/musiclistall" in prompt
    assert "/music" in prompt
    assert "/scan" not in prompt
    assert PHOTO_SCAN_COMMANDS <= COMMAND_SINK_DENYLIST


def test_help_text_is_generated_from_registered_command_usage(tmp_path):
    command_handlers, _, _, _ = _registries(tmp_path)
    help_reply = _build_openclaw_help_text(command_handlers)
    assert "/search" in help_reply
    assert "/fetch https://example.com 這篇文章的重點是什麼" in help_reply
    assert "/read — 同 /fetch" in help_reply
    assert "/research" in help_reply
    assert "投資判斷" in help_reply
    assert "Mercari" in help_reply
    assert "/generateaudio こんにちは" in help_reply
    assert "/generateaudio — 產生音訊檔案" in help_reply
    assert "/price pokemon | Pikachu ex | 132/106 | SAR | sv08" in help_reply
    assert "/scan pokemon" in help_reply


def test_callback_registry_has_expected_keys(tmp_path):
    _, callback_handlers, _, _ = _registries(tmp_path)
    assert {"quiz", "voice", "ragkeep", "ragdel", "snsdel", "snsaddok", "snsfb"} <= set(callback_handlers)
    assert all(callable(v) for v in callback_handlers.values())


def test_view_registry_has_expected_keys(tmp_path):
    _, _, view_handlers, item_deleter_handlers = _registries(tmp_path)
    assert {"km", "kc", "sl"} <= set(view_handlers)
    assert {"km", "kc", "sl"} <= set(item_deleter_handlers)
    for fn in view_handlers.values():
        assert callable(fn)
    for entry in item_deleter_handlers.values():
        fn, label = entry
        assert callable(fn)
        assert isinstance(label, str)


def test_background_and_sync_commands_are_flagged_correctly(tmp_path):
    command_handlers, _, _, _ = _registries(tmp_path)
    # /quiz is a slow local-LLM op → background with an ack.
    quiz = command_handlers["/quiz"]
    assert quiz.background is True
    assert quiz.ack
    # /voice is a fast synchronous param view → no ack, foreground.
    voice = command_handlers["/voice"]
    assert voice.background is False
    assert voice.ack is None
    # /knowledge is sync (fast DB read).
    kb = command_handlers["/knowledge"]
    assert kb.background is False


def test_new_handler_reports_disabled_when_no_runner(tmp_path):
    command_handlers, _, _, _ = _registries(tmp_path)
    reply = command_handlers["/new"].handler("price of x", "chat-1")
    assert "尚未啟用" in reply


def test_knowledge_market_view_fn_returns_tuple(tmp_path):
    _, _, view_handlers, _ = _registries(tmp_path)
    text, markup, page = view_handlers["km"](page=0)
    assert isinstance(text, str)
    assert page == 0  # empty DB → page 0
