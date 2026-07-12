"""R2.0 uniqueness/precedence tests across the three-layer dispatch chain
(issue #75, plan §10):

    telegram_core -> price_monitor_bot.TelegramCommandProcessor
                  -> openclaw_adapter.telegram_bot.TelegramCommandProcessor

Callback prefixes live in ONE flat namespace but arrive from five places with
different precedence (async hooks > registry > core builtins; runtime
dict.update merges are later-wins and silent). A collision is never an error
at runtime — it silently disables a button — so disjointness is pinned here.
See docs/R2_TELEGRAM_OWNERSHIP_INVENTORY.md §1/§5 for the full contract.

No network, no Ollama, no Telegram — registry construction only."""
from __future__ import annotations

import pytest

from assistant_runtime import AssistantSettings
from openclaw_adapter.catalog_planner import CatalogPlanner
from openclaw_adapter.telegram_bot import (
    TelegramCommandProcessor,
    _build_registries,
)
from openclaw_adapter.workflow_editor import WorkflowEditor
from price_monitor_bot.bot import (
    TelegramCommandProcessor as PriceTelegramCommandProcessor,
)
from telegram_core.processor import BUILTIN_COMMANDS

# Dispatched by handle_telegram_callback_query AFTER the registry (a registry
# entry would shadow them and silently break list views / clarifications).
CORE_BUILTIN_CALLBACK_PREFIXES = frozenset(
    {"pg", "del", "close", "popt", "topt", "noop"}
)

# Intercepted by handle_callback_query_async overrides BEFORE the registry
# (a registry entry under these is dead code): aka handles "goal";
# price_monitor_bot handles "wprc"/"fbprc" (ForceReply flows).
ASYNC_HOOK_PREFIXES = frozenset({"goal", "wprc", "fbprc"})

# Defaults injected by price_monitor_bot.TelegramCommandProcessor.__init__;
# external (aka) kwargs win on collision, so aka must not reuse these names
# unless it intends to replace the price-domain behavior.
PRICE_DEFAULT_CALLBACK_PREFIXES = frozenset(
    {"cond", "wedit", "wmkt", "wback", "fbpos"}
)
PRICE_DEFAULT_LIST_KINDS = frozenset({"wl"})


def _settings(tmp_path) -> AssistantSettings:
    return AssistantSettings(
        quiz_db_path=str(tmp_path / "quiz.sqlite3"),
        opportunity_db_path=str(tmp_path / "hunt.sqlite3"),
        knowledge_db_path=str(tmp_path / "knowledge.sqlite3"),
    )


@pytest.fixture()
def registries(tmp_path):
    return _build_registries(
        _settings(tmp_path), None, sns_db=None, buzz_fn=None,
        start_schedulers=False,
    )


def _runtime_merge_prefixes() -> set[str]:
    """The prefixes run_telegram_polling later dict.update()s into the
    registry — built from the real classes so a new prefix is caught here."""
    planner = CatalogPlanner(None)
    editor = WorkflowEditor(store=None)
    return set(planner.callback_handlers()) | set(editor.callback_handlers())


# ---- command namespace ------------------------------------------------------


def test_no_registered_command_collides_with_core_builtins(registries):
    command_handlers, _, _, _ = registries
    assert set(command_handlers) & BUILTIN_COMMANDS == set()
    assert all(name.startswith("/") for name in command_handlers)


def test_command_names_are_unique_after_late_registrations(registries):
    # /schedulehome (and /workflow when a runner exists) are added after the
    # dict literal; both must be additions, not overrides of existing rows.
    command_handlers, _, _, _ = registries
    assert "/schedulehome" in command_handlers
    assert "/workflow" not in command_handlers  # runner=None → not registered


# ---- callback prefix namespace ----------------------------------------------


def test_callback_prefixes_are_wire_format_safe(registries):
    _, callback_handlers, _, _ = registries
    for prefix in callback_handlers:
        assert prefix, "callback prefix must be non-empty"
        assert ":" not in prefix, (
            f"{prefix!r} can never match: dispatch splits on the first ':'"
        )


def test_callback_prefixes_do_not_shadow_core_builtins(registries):
    _, callback_handlers, _, _ = registries
    overlap = set(callback_handlers) & CORE_BUILTIN_CALLBACK_PREFIXES
    assert overlap == set(), (
        f"registry beats builtins — {overlap} would silently break list views"
    )
    assert _runtime_merge_prefixes() & CORE_BUILTIN_CALLBACK_PREFIXES == set()


def test_callback_prefixes_are_not_dead_async_hook_names(registries):
    _, callback_handlers, _, _ = registries
    overlap = set(callback_handlers) & ASYNC_HOOK_PREFIXES
    assert overlap == set(), (
        f"async hooks run before the registry — {overlap} would be dead code"
    )
    assert _runtime_merge_prefixes() & ASYNC_HOOK_PREFIXES == set()


def test_callback_prefixes_do_not_shadow_price_defaults(registries):
    _, callback_handlers, _, _ = registries
    assert set(callback_handlers) & PRICE_DEFAULT_CALLBACK_PREFIXES == set()
    assert _runtime_merge_prefixes() & PRICE_DEFAULT_CALLBACK_PREFIXES == set()


def test_runtime_merged_prefixes_do_not_collide_with_registry(registries):
    # run_telegram_polling merges these with plain dict.update (later wins,
    # silently); disjointness is the only thing keeping that merge safe.
    _, callback_handlers, _, _ = registries
    overlap = _runtime_merge_prefixes() & set(callback_handlers)
    assert overlap == set(), f"silent dict.update override: {overlap}"


# ---- list-kind namespace (pg/del/close payloads) ------------------------------


def test_view_and_deleter_kinds_do_not_shadow_price_watchlist(registries):
    _, _, view_handlers, item_deleter_handlers = registries
    assert set(view_handlers) & PRICE_DEFAULT_LIST_KINDS == set()
    assert set(item_deleter_handlers) & PRICE_DEFAULT_LIST_KINDS == set()


def test_every_deleter_kind_has_a_view_renderer(registries):
    # del:<kind>:<id> re-renders the list after deleting; a deleter without a
    # view renderer would delete the row then toast 未知清單.
    _, _, view_handlers, item_deleter_handlers = registries
    assert set(item_deleter_handlers) <= set(view_handlers)


# ---- merge precedence across layers -------------------------------------------


def test_external_callback_kwargs_beat_price_defaults():
    sentinel = lambda payload, original, chat: ("sentinel", None, None)  # noqa: E731
    processor = PriceTelegramCommandProcessor(
        lookup_renderer=lambda query: query.name,
        board_loader=lambda: (),
        catalog_renderer=lambda: "catalog",
        callback_handlers={"cond": sentinel},
    )
    assert processor._callback_registry["cond"] is sentinel
    # non-colliding defaults survive the merge
    assert "wedit" in processor._callback_registry


def test_price_defaults_present_when_no_external_kwargs():
    processor = PriceTelegramCommandProcessor(
        lookup_renderer=lambda query: query.name,
        board_loader=lambda: (),
        catalog_renderer=lambda: "catalog",
    )
    assert PRICE_DEFAULT_CALLBACK_PREFIXES <= set(processor._callback_registry)
    assert PRICE_DEFAULT_LIST_KINDS <= set(processor._view_registry)
    assert PRICE_DEFAULT_LIST_KINDS <= set(processor._deleter_registry)


def test_aka_processor_reserves_goal_via_setdefault():
    processor = TelegramCommandProcessor(
        settings=None,
        lookup_renderer=lambda query: query.name,
        board_loader=lambda: (),
        catalog_renderer=lambda: "catalog",
    )
    assert "goal" in processor._callback_registry
    # the async hook claims prefix "goal" before the registry is consulted
    handled = processor.handle_callback_query_async(
        client=_RecordingClient(),
        callback_id="cb1",
        chat_id="1",
        message_id=1,
        prefix="notgoal",
        payload="x",
        original_text="",
    )
    assert handled is False  # falls through the whole chain for other prefixes


def test_builtin_command_collision_raises_at_construction():
    with pytest.raises(ValueError):
        PriceTelegramCommandProcessor(
            lookup_renderer=lambda query: query.name,
            board_loader=lambda: (),
            catalog_renderer=lambda: "catalog",
            command_handlers={"/help": object()},
        )


def test_colon_callback_prefix_raises_at_construction():
    with pytest.raises(ValueError):
        PriceTelegramCommandProcessor(
            lookup_renderer=lambda query: query.name,
            board_loader=lambda: (),
            catalog_renderer=lambda: "catalog",
            callback_handlers={"bad:prefix": lambda p, o, c: (None, None, None)},
        )


class _RecordingClient:
    def answer_callback_query(self, **kwargs):
        pass

    def send_message(self, **kwargs):
        pass

    def edit_message_text(self, **kwargs):
        pass
