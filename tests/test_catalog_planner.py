"""Tests for the live Chat/planner glue over the generated-tool catalog (#52)."""
from __future__ import annotations

from openclaw_adapter.catalog_planner import CatalogPlanner
from openclaw_adapter.dynamic_tools import ReusePlan


class FakeRunner:
    def __init__(self, *, plan: ReusePlan | None = None, reuse_reply="REUSED", run_reply="GENERATED"):
        self._plan = plan
        self._reuse_reply = reuse_reply
        self._run_reply = run_reply
        self.reuse_calls: list = []
        self.run_calls: list = []

    def plan_for_text(self, text):
        return self._plan

    def run_reuse_plan(self, plan):
        self.reuse_calls.append(plan)
        return self._reuse_reply

    def run(self, text):
        self.run_calls.append(text)
        return self._run_reply


def test_none_action_defers_to_default():
    planner = CatalogPlanner(FakeRunner(plan=ReusePlan(action="none")))
    assert planner.handle_text("哈哈", "1") is None


def test_runner_absent_defers():
    assert CatalogPlanner(None).handle_text("查天氣", "1") is None


def test_blank_text_defers():
    planner = CatalogPlanner(FakeRunner(plan=ReusePlan(action="run")))
    assert planner.handle_text("   ", "1") is None


def test_promoted_run_answers_inline_without_buttons():
    runner = FakeRunner(plan=ReusePlan(action="run", slug="w"), reuse_reply="大阪 晴")
    planner = CatalogPlanner(runner)
    result = planner.handle_text("查大阪天氣", "1")
    assert result is not None
    reply, markup = result
    assert reply == "大阪 晴"
    assert markup is None
    assert len(runner.reuse_calls) == 1


def test_confirm_reuse_offers_use_button_and_does_not_run_yet():
    runner = FakeRunner(plan=ReusePlan(action="confirm_reuse", slug="w", tool_type="weather"))
    planner = CatalogPlanner(runner)
    reply, markup = planner.handle_text("查大阪天氣", "1")
    assert "weather" in reply
    assert runner.reuse_calls == []  # not run until confirmed
    cb = markup["inline_keyboard"][0][0]["callback_data"]
    assert cb.startswith("cataloguse:")


def test_confirm_generate_offers_new_button_and_does_not_generate_yet():
    runner = FakeRunner(plan=ReusePlan(action="confirm_generate"))
    planner = CatalogPlanner(runner)
    reply, markup = planner.handle_text("查大阪天氣", "1")
    assert "新生成" in reply
    assert runner.run_calls == []
    cb = markup["inline_keyboard"][0][0]["callback_data"]
    assert cb.startswith("catalognew:")


def test_confirm_reuse_then_use_callback_runs_the_matched_tool():
    plan = ReusePlan(action="confirm_reuse", slug="w", tool_type="weather")
    runner = FakeRunner(plan=plan, reuse_reply="大阪 晴")
    planner = CatalogPlanner(runner)
    _, markup = planner.handle_text("查大阪天氣", "1")
    token = markup["inline_keyboard"][0][0]["callback_data"].split(":", 1)[1]
    toast, new_text, new_markup = planner.callback_handlers()["cataloguse"](token, "查大阪天氣", "1")
    assert toast is None
    assert new_text == "大阪 晴"
    assert runner.reuse_calls == [plan]


def test_confirm_generate_then_new_callback_generates():
    runner = FakeRunner(plan=ReusePlan(action="confirm_generate"), run_reply="GEN")
    planner = CatalogPlanner(runner)
    _, markup = planner.handle_text("查大阪天氣", "1")
    token = markup["inline_keyboard"][0][0]["callback_data"].split(":", 1)[1]
    toast, new_text, _ = planner.callback_handlers()["catalognew"](token, "查大阪天氣", "1")
    assert new_text == "GEN"
    assert runner.run_calls == ["查大阪天氣"]


def test_cancel_callback_clears_pending_and_does_nothing():
    runner = FakeRunner(plan=ReusePlan(action="confirm_reuse", slug="w", tool_type="weather"))
    planner = CatalogPlanner(runner)
    _, markup = planner.handle_text("查大阪天氣", "1")
    token = markup["inline_keyboard"][0][0]["callback_data"].split(":", 1)[1]
    toast, new_text, _ = planner.callback_handlers()["catalogno"](token, "查大阪天氣", "1")
    assert new_text == "已取消。"
    assert runner.reuse_calls == []


def test_unknown_token_is_treated_as_expired():
    runner = FakeRunner(plan=ReusePlan(action="confirm_reuse", slug="w", tool_type="weather"))
    planner = CatalogPlanner(runner)
    toast, new_text, _ = planner.callback_handlers()["cataloguse"]("bogus", "查大阪天氣", "1")
    assert "逾時" in toast
    assert new_text is None
    assert runner.reuse_calls == []


def test_token_is_single_use():
    plan = ReusePlan(action="confirm_reuse", slug="w", tool_type="weather")
    runner = FakeRunner(plan=plan)
    planner = CatalogPlanner(runner)
    _, markup = planner.handle_text("查大阪天氣", "1")
    token = markup["inline_keyboard"][0][0]["callback_data"].split(":", 1)[1]
    planner.callback_handlers()["cataloguse"](token, "查大阪天氣", "1")
    # Replaying the same token must not run the tool a second time.
    toast, new_text, _ = planner.callback_handlers()["cataloguse"](token, "查大阪天氣", "1")
    assert "逾時" in toast
    assert len(runner.reuse_calls) == 1


def test_wrong_kind_token_rejected():
    # A reuse token must not satisfy the generate callback (and vice versa).
    runner = FakeRunner(plan=ReusePlan(action="confirm_reuse", slug="w", tool_type="weather"))
    planner = CatalogPlanner(runner)
    _, markup = planner.handle_text("查大阪天氣", "1")
    token = markup["inline_keyboard"][0][0]["callback_data"].split(":", 1)[1]
    toast, new_text, _ = planner.callback_handlers()["catalognew"](token, "查大阪天氣", "1")
    assert "逾時" in toast
    assert runner.run_calls == []


def test_plan_exception_defers_gracefully():
    class Boom:
        def plan_for_text(self, text):
            raise RuntimeError("nope")

    assert CatalogPlanner(Boom()).handle_text("查天氣", "1") is None
