from __future__ import annotations

import threading

import pytest

from openclaw_adapter.research_command import (
    BudgetExhaustedError,
    ResearchBudget,
    build_budgeted_search_fn,
    build_research_handler,
    normalize_mercari_item_url,
    parse_research_target,
)


class FakeNotifier:
    def __init__(self) -> None:
        self.messages: list[str] = []

    def send(self, text: str) -> None:
        self.messages.append(text)


def _parse_stage(ctx) -> str:
    ctx.target = parse_research_target(ctx.raw_input)
    if ctx.target.mode == "mercari_url":
        return f"已正規化 Mercari 商品網址（{ctx.target.item_id}）"
    return "已辨識為商品名稱研究"


def _placeholder(note: str):
    def run(ctx) -> str:
        return note

    return run


def test_normalize_mercari_item_url_strips_tracking_query() -> None:
    assert normalize_mercari_item_url(
        "https://jp.mercari.com/item/m65806654179?afid=123&utm_source=x&source_location=share"
    ) == "https://jp.mercari.com/item/m65806654179"


def test_parse_research_target_treats_non_url_as_text_query() -> None:
    target = parse_research_target("  初音ミク   15th   フィギュア ")

    assert target.mode == "text_query"
    assert target.display_text == "初音ミク 15th フィギュア"
    assert target.canonical_url is None


def test_budgeted_search_fn_stops_after_budget_exhausted() -> None:
    calls: list[tuple[str, int]] = []
    budget = ResearchBudget(max_searches=2)

    def backend(query: str, limit: int) -> tuple[object, ...]:
        calls.append((query, limit))
        return ("ok",)

    wrapped = build_budgeted_search_fn(backend, budget)

    assert wrapped("a", 1) == ("ok",)
    assert wrapped("b", 2) == ("ok",)
    with pytest.raises(BudgetExhaustedError):
        wrapped("c", 3)

    assert calls == [("a", 1), ("b", 2)]


def test_research_handler_reports_progress_heartbeat_and_final_reply() -> None:
    notifier = FakeNotifier()

    def heartbeat_stage(ctx) -> str:
        ctx.heartbeat("還在整理資料源配置")
        return "M1 骨架：已保留商品抓取接點"

    handler = build_research_handler(
        notifier_factory=lambda chat_id: notifier,
        stage_runners=(
            _parse_stage,
            heartbeat_stage,
            _placeholder("M1 骨架：已保留實體辨識與知識庫接點"),
            _placeholder("M1 骨架：已保留增值潛力分析階段"),
            _placeholder("M1 骨架：已保留合理市價分析階段"),
            _placeholder("M1 骨架：已保留流動性分析階段"),
            _placeholder("M1 骨架：已保留賣家風險分析階段"),
        ),
    )

    reply = handler("https://jp.mercari.com/item/m65806654179?afid=foo", "chat-1")

    assert notifier.messages[0] == "⏳ [0/6] 解析輸入中…"
    assert notifier.messages[1] == "✅ [0/6] 完成（已正規化 Mercari 商品網址（m65806654179））"
    assert "⏳ [1/6] 取得商品資料：還在整理資料源配置" in notifier.messages
    assert notifier.messages[-1] == "✅ [6/6] 完成（M1 骨架：已保留賣家風險分析階段）"
    assert "龍蝦 /research 已完成 M1 骨架流程。" in reply
    assert "https://jp.mercari.com/item/m65806654179" in reply


def test_research_handler_rejects_overlapping_jobs_in_same_chat() -> None:
    started = threading.Event()
    release = threading.Event()
    notifiers: dict[str, FakeNotifier] = {}

    def notifier_factory(chat_id: str) -> FakeNotifier:
        return notifiers.setdefault(chat_id, FakeNotifier())

    def blocking_stage(ctx) -> str:
        started.set()
        release.wait(timeout=2)
        return "M1 骨架：已保留商品抓取接點"

    handler = build_research_handler(
        notifier_factory=notifier_factory,
        stage_runners=(
            _parse_stage,
            blocking_stage,
            _placeholder("M1 骨架：已保留實體辨識與知識庫接點"),
            _placeholder("M1 骨架：已保留增值潛力分析階段"),
            _placeholder("M1 骨架：已保留合理市價分析階段"),
            _placeholder("M1 骨架：已保留流動性分析階段"),
            _placeholder("名稱模式首版不做賣家風險"),
        ),
    )

    result_box: dict[str, str] = {}

    def first_run() -> None:
        result_box["reply"] = handler("初音ミク 15th フィギュア", "chat-1")

    thread = threading.Thread(target=first_run)
    thread.start()
    assert started.wait(timeout=1)

    busy_reply = handler("別的商品", "chat-1")

    release.set()
    thread.join(timeout=2)

    assert busy_reply == "同一個聊天室目前已有 /research 在執行中，請等上一個研究完成。"
    assert "龍蝦 /research 已完成 M1 骨架流程。" in result_box["reply"]
