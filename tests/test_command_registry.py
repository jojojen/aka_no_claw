"""The wrapper registers aka_no_claw's commands as data in four registries that
the base dispatcher consults — adding a new command must never require editing
price_monitor_bot/bot.py. These tests pin the expected registration keys and the
RegisteredCommand contract (handler shape, ack, background flag).

No network, no Ollama, no Telegram — just the registry construction."""
from __future__ import annotations

from assistant_runtime import AssistantSettings
from openclaw_adapter.telegram_bot import _build_registries
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
        "/say",
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
    }
    assert expected <= set(command_handlers)
    assert all(isinstance(v, RegisteredCommand) for v in command_handlers.values())


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
