from __future__ import annotations

import threading
from pathlib import Path

import pytest

from openclaw_adapter.research_command import (
    BudgetExhaustedError,
    ItemData,
    MercariItemAdapter,
    ResearchBudget,
    SellerReputationSnapshot,
    build_budgeted_search_fn,
    build_research_handler,
    normalize_mercari_item_url,
    parse_research_target,
)
from openclaw_adapter.knowledge_db import KnowledgeDatabase
from openclaw_adapter.web_search import WebSearchResult


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


def _load_fixture(name: str) -> str:
    fixture_path = (
        Path(__file__).resolve().parents[2]
        / "price_monitor_bot"
        / "tests"
        / "fixtures"
        / name
    )
    return fixture_path.read_text(encoding="utf-8")


def _fake_active_search(query: str, price_cap: int, max_results: int) -> list[dict[str, object]]:
    return []


def _fake_sold_search(query: str, max_results: int) -> list[dict[str, object]]:
    return []


def _fake_sold_average(query: str) -> float | None:
    return None


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
        active_market_search_fn=_fake_active_search,
        sold_market_search_fn=_fake_sold_search,
        sold_average_lookup_fn=_fake_sold_average,
    )

    reply = handler("https://jp.mercari.com/item/m65806654179?afid=foo", "chat-1")

    assert notifier.messages[0] == "⏳ [0/6] 解析輸入中…"
    assert notifier.messages[1] == "✅ [0/6] 完成（已正規化 Mercari 商品網址（m65806654179））"
    assert "⏳ [1/6] 取得商品資料：還在整理資料源配置" in notifier.messages
    assert notifier.messages[-1] == "✅ [6/6] 完成（M1 骨架：已保留賣家風險分析階段）"
    assert "龍蝦 /research 已完成目前可用流程。" in reply
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
        active_market_search_fn=_fake_active_search,
        sold_market_search_fn=_fake_sold_search,
        sold_average_lookup_fn=_fake_sold_average,
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
    assert "龍蝦 /research 已完成目前可用流程。" in result_box["reply"]


def test_research_handler_fetches_mercari_item_and_persists_knowledge(tmp_path: Path) -> None:
    notifier = FakeNotifier()
    item_fetcher = MercariItemAdapter(
        fetch_html_fn=lambda _url: _load_fixture("mercari_item_m18542743389.html")
    )
    knowledge_db_path = tmp_path / "knowledge.sqlite3"
    handler = build_research_handler(
        notifier_factory=lambda chat_id: notifier,
        item_fetcher=item_fetcher,
        knowledge_db_path=str(knowledge_db_path),
        active_market_search_fn=_fake_active_search,
        sold_market_search_fn=_fake_sold_search,
        sold_average_lookup_fn=_fake_sold_average,
    )

    reply = handler("https://jp.mercari.com/item/m18542743389?utm_source=share", "chat-1")

    assert any("✅ [1/6] 完成（標題" in message for message in notifier.messages)
    assert "研究模式：Mercari 商品網址" in reply
    assert "商品頁資料：エヴァンゲリオン 30周年フェス限定 綾波レイ ユニオンアリーナ プロモカード" in reply
    assert "賣家 146184751" in reply
    assert "狀態 新品、未使用" in reply
    assert "各節結果：" in reply
    db = KnowledgeDatabase(knowledge_db_path)
    entry = db.get_entry("mercari:m18542743389")
    assert entry is not None
    assert entry.origin == "research_command"
    assert entry.entity_type == "product"
    assert "Mercari 商品頁資料" in entry.summary
    assert "賣家 ID：146184751。" in entry.summary
    assert "商品狀態：新品、未使用。" in entry.summary
    assert "https://jp.mercari.com/item/m18542743389" in entry.source_urls
    assert db.lookup_canonical("エヴァンゲリオン 30周年フェス限定 綾波レイ ユニオンアリーナ プロモカード") == "mercari:m18542743389"


def test_mercari_item_adapter_extracts_expected_fields_from_fixture() -> None:
    adapter = MercariItemAdapter(fetch_html_fn=lambda _url: _load_fixture("mercari_item_m85537287496.html"))
    target = parse_research_target("https://jp.mercari.com/item/m85537287496")

    item = adapter.fetch(target)

    assert item.item_id == "m85537287496"
    assert item.title == "エヴァンゲリオン 30周年フェス限定 綾波レイ ユニオンアリーナ プロモカード"
    assert item.listed_price_jpy == 8300
    assert item.condition_label == "新品、未使用"
    assert item.seller_id == "433414807"
    assert item.seller_url == "https://jp.mercari.com/user/profile/433414807"
    assert item.image_urls == ("https://static.mercdn.net/item/detail/orig/photos/m85537287496_1.jpg?1776239339",)
    assert item.source_confidence >= 0.8


def test_mercari_item_adapter_falls_back_to_adjacent_condition_and_generic_profile_link() -> None:
    html = """
    <html>
      <head>
        <title>通常盤 形藻土 初回プレス限定仕様 未開封 by メルカリ</title>
        <meta name="description" content="新品で保管しています。">
        <meta name="product:price:amount" content="1800">
        <meta property="og:image" content="https://static.mercdn.net/item/detail/orig/photos/m12345678901_1.jpg?123">
      </head>
      <body>
        <main>
          <section>
            <div class="itemInfo">
              <div class="label">商品の状態</div>
              <div class="valueWrap"><span>新品、未使用</span></div>
            </div>
            <div class="sellerBlock">
              <span>出品者</span>
              <a href="/user/profile/99887766">some seller</a>
            </div>
          </section>
        </main>
      </body>
    </html>
    """
    adapter = MercariItemAdapter(fetch_html_fn=lambda _url: html)
    target = parse_research_target("https://jp.mercari.com/item/m12345678901")

    item = adapter.fetch(target)

    assert item.item_id == "m12345678901"
    assert item.title == "通常盤 形藻土 初回プレス限定仕様 未開封"
    assert item.listed_price_jpy == 1800
    assert item.condition_label == "新品、未使用"
    assert item.seller_id == "99887766"
    assert item.seller_url == "https://jp.mercari.com/user/profile/99887766"
    assert item.image_urls == ("https://static.mercdn.net/item/detail/orig/photos/m12345678901_1.jpg?123",)


def test_mercari_item_adapter_infers_new_condition_from_title_when_page_omits_field() -> None:
    html = """
    <html>
      <head>
        <title>通常盤 形藻土 初回プレス限定仕様 未開封 by メルカリ</title>
        <meta name="description" content="generic mercari description">
        <meta name="product:price:amount" content="1800">
      </head>
      <body><main><p>detail omitted</p></main></body>
    </html>
    """
    adapter = MercariItemAdapter(fetch_html_fn=lambda _url: html)

    item = adapter.fetch(parse_research_target("https://jp.mercari.com/item/m12345000000"))

    assert item.condition_label == "新品、未使用"


def test_research_item_knowledge_uses_item_id_key_and_updates_same_row(tmp_path: Path) -> None:
    class SwappingItemFetcher:
        def __init__(self) -> None:
            self.calls = 0

        def fetch(self, _target):
            self.calls += 1
            title = "初版標題" if self.calls == 1 else "更新後標題"
            return ItemData(
                source_site="mercari",
                item_url="https://jp.mercari.com/item/m99911122233",
                item_id="m99911122233",
                title=title,
                listed_price_jpy=4800,
                description="desc",
                condition_label="目立った傷や汚れなし",
                seller_id="12345",
                seller_url="https://jp.mercari.com/user/profile/12345",
                image_urls=(),
                fetched_at="2026-06-12T00:00:00+00:00",
                source_confidence=0.8,
            )

    knowledge_db_path = tmp_path / "knowledge.sqlite3"
    handler = build_research_handler(
        notifier_factory=lambda chat_id: FakeNotifier(),
        item_fetcher=SwappingItemFetcher(),
        knowledge_db_path=str(knowledge_db_path),
        active_market_search_fn=_fake_active_search,
        sold_market_search_fn=_fake_sold_search,
        sold_average_lookup_fn=_fake_sold_average,
    )

    handler("https://jp.mercari.com/item/m99911122233", "chat-1")
    handler("https://jp.mercari.com/item/m99911122233", "chat-1")

    db = KnowledgeDatabase(knowledge_db_path)
    entry = db.get_entry("mercari:m99911122233")
    assert entry is not None
    assert "更新後標題" in entry.summary
    assert db.lookup_canonical("初版標題") == "mercari:m99911122233"
    assert db.lookup_canonical("更新後標題") == "mercari:m99911122233"
    assert len(db.recent_entries(10)) == 1


def test_research_handler_builds_appreciation_section_from_knowledge_and_heat(tmp_path: Path) -> None:
    class FakeHeatSignal:
        def __init__(self, source: str, percentile: float) -> None:
            self.source = source
            self.percentile = percentile

    knowledge_db_path = tmp_path / "knowledge.sqlite3"
    db = KnowledgeDatabase(knowledge_db_path)
    db.upsert_entry(
        entity_canonical="evangelion",
        entity_type="ip",
        summary="EVA 是長期有收藏需求的動畫 IP，週年與限定活動通常會帶動周邊成交熱度。",
        source_urls=("https://example.com/eva",),
        confidence=0.7,
        origin="manual",
        aliases=("エヴァンゲリオン",),
    )
    db.upsert_entry(
        entity_canonical="rei ayanami",
        entity_type="creator",
        summary="綾波レイ是 EVA 核心角色，角色人氣通常能支撐單角週邊需求。",
        source_urls=("https://example.com/rei",),
        confidence=0.7,
        origin="manual",
        aliases=("綾波レイ",),
    )
    item_fetcher = MercariItemAdapter(
        fetch_html_fn=lambda _url: _load_fixture("mercari_item_m18542743389.html")
    )

    def heat_lookup(canonicals: tuple[str, ...]) -> dict[str, tuple[object, ...]]:
        assert "evangelion" in canonicals
        return {
            "evangelion": (
                FakeHeatSignal("google_trends", 88.0),
                FakeHeatSignal("reddit", 71.0),
            ),
        }

    handler = build_research_handler(
        notifier_factory=lambda chat_id: FakeNotifier(),
        item_fetcher=item_fetcher,
        knowledge_db_path=str(knowledge_db_path),
        ip_heat_lookup_fn=heat_lookup,
        active_market_search_fn=_fake_active_search,
        sold_market_search_fn=_fake_sold_search,
        sold_average_lookup_fn=_fake_sold_average,
    )

    reply = handler("https://jp.mercari.com/item/m18542743389", "chat-1")

    assert "增值潛力分析 [ok]" in reply
    assert "命中知識庫 2 筆：evangelion(ip)、rei ayanami(creator)。" in reply
    assert "evangelion 近期 熱度高（google_trends 88pct / reddit 71pct）。" in reply
    assert "rei ayanami：綾波レイ是 EVA 核心角色" in reply


def test_research_handler_uses_budgeted_web_search_for_appreciation_gap() -> None:
    seen_queries: list[tuple[str, int]] = []

    def search_backend(query: str, limit: int) -> tuple[WebSearchResult, ...]:
        seen_queries.append((query, limit))
        return (
            WebSearchResult(
                title="エヴァンゲリオン30周年 公式イベント",
                url="https://example.com/eva-event",
                snippet="30周年施策と限定商品を告知。",
            ),
            WebSearchResult(
                title="Mercari listing should be filtered",
                url="https://jp.mercari.com/item/m123456789",
                snippet="ignored",
            ),
        )

    handler = build_research_handler(
        notifier_factory=lambda chat_id: FakeNotifier(),
        search_fn=search_backend,
        active_market_search_fn=_fake_active_search,
        sold_market_search_fn=_fake_sold_search,
        sold_average_lookup_fn=_fake_sold_average,
    )

    reply = handler("エヴァンゲリオン 30周年 限定", "chat-1")

    assert seen_queries == [("エヴァンゲリオン 30周年 限定", 3)]
    assert "搜尋預算：1/5" in reply
    assert "增值潛力分析 [partial]" in reply
    assert "外部補證 1 筆：エヴァンゲリオン30周年 公式イベント。" in reply
    assert "https://example.com/eva-event" in reply
    assert "https://jp.mercari.com/item/m123456789" not in reply


def test_research_handler_builds_price_section_from_active_and_sold_samples(tmp_path: Path) -> None:
    item_fetcher = MercariItemAdapter(
        fetch_html_fn=lambda _url: _load_fixture("mercari_item_m18542743389.html")
    )

    def active_search(query: str, price_cap: int, max_results: int) -> list[dict[str, object]]:
        assert "エヴァンゲリオン" in query
        assert price_cap == 13110
        assert max_results == 8
        return [
            {
                "item_id": "m1",
                "title": "エヴァンゲリオン 綾波レイ プロモ 1",
                "price_jpy": 6100,
                "url": "https://jp.mercari.com/item/m1",
                "thumbnail_url": "",
            },
            {
                "item_id": "m2",
                "title": "エヴァンゲリオン 綾波レイ プロモ 2",
                "price_jpy": 6800,
                "url": "https://jp.mercari.com/item/m2",
                "thumbnail_url": "",
            },
            {
                "item_id": "m3",
                "title": "エヴァンゲリオン 綾波レイ プロモ 3",
                "price_jpy": 7200,
                "url": "https://jp.mercari.com/item/m3",
                "thumbnail_url": "",
            },
        ]

    def sold_search(query: str, max_results: int) -> list[dict[str, object]]:
        assert "エヴァンゲリオン" in query
        assert max_results == 8
        return [
            {
                "item_id": "s1",
                "title": "エヴァンゲリオン 綾波レイ 成交 1",
                "price_jpy": 7000,
                "url": "https://jp.mercari.com/item/s1",
                "thumbnail_url": "",
            },
            {
                "item_id": "s2",
                "title": "エヴァンゲリオン 綾波レイ 成交 2",
                "price_jpy": 7200,
                "url": "https://jp.mercari.com/item/s2",
                "thumbnail_url": "",
            },
            {
                "item_id": "s3",
                "title": "エヴァンゲリオン 綾波レイ 成交 3",
                "price_jpy": 6800,
                "url": "https://jp.mercari.com/item/s3",
                "thumbnail_url": "",
            },
        ]

    handler = build_research_handler(
        notifier_factory=lambda chat_id: FakeNotifier(),
        item_fetcher=item_fetcher,
        knowledge_db_path=str(tmp_path / "knowledge.sqlite3"),
        active_market_search_fn=active_search,
        sold_market_search_fn=sold_search,
        sold_average_lookup_fn=lambda query: 9999.0,
    )

    reply = handler("https://jp.mercari.com/item/m18542743389", "chat-1")

    assert "合理市價分析 [ok]" in reply
    assert "Mercari sold 樣本 3 筆，均價約 ¥7,000" in reply
    assert "active 樣本 3 筆，中位數 ¥6,800，區間 ¥6,100–¥7,200" in reply
    assert "目前開價接近 sold 均價" in reply
    assert "流動性分析 [ok]" in reply
    assert "Mercari active 3 筆 / sold 3 筆；sold/active 比 1.00；樣本顯示流動性中等，仍有一定成交速度。" in reply
    assert "https://jp.mercari.com/item/s1" in reply


def test_research_handler_price_stage_works_for_text_query_without_item_page() -> None:
    def active_search(query: str, price_cap: int, max_results: int) -> list[dict[str, object]]:
        assert query == "初音ミク 15th フィギュア"
        assert price_cap == 50000
        assert max_results == 8
        return [
            {
                "item_id": "mx1",
                "title": "初音ミク 15th フィギュア A",
                "price_jpy": 9000,
                "url": "https://jp.mercari.com/item/mx1",
                "thumbnail_url": "",
            },
            {
                "item_id": "mx2",
                "title": "初音ミク 15th フィギュア B",
                "price_jpy": 9800,
                "url": "https://jp.mercari.com/item/mx2",
                "thumbnail_url": "",
            },
        ]

    handler = build_research_handler(
        notifier_factory=lambda chat_id: FakeNotifier(),
        active_market_search_fn=active_search,
        sold_market_search_fn=_fake_sold_search,
        sold_average_lookup_fn=lambda query: None,
    )

    reply = handler("初音ミク 15th フィギュア", "chat-1")

    assert "研究模式：商品名稱" in reply
    assert "合理市價分析 [partial]" in reply
    assert "active 樣本 2 筆，中位數 ¥9,400，區間 ¥9,000–¥9,800" in reply
    assert "流動性分析 [partial]" in reply
    assert "Mercari active 2 筆 / sold 0 筆；sold/active 比 0.00；樣本偏少，流動性暫時只能做弱判讀。" in reply
    assert "Mercari sold 價目前只拿到平均值接口；此查詢未回傳可用 sold avg。" in reply


def test_research_handler_filters_low_relevance_and_graded_price_samples(tmp_path: Path) -> None:
    item_fetcher = MercariItemAdapter(
        fetch_html_fn=lambda _url: _load_fixture("mercari_item_m18542743389.html")
    )

    def active_search(query: str, price_cap: int, max_results: int) -> list[dict[str, object]]:
        return [
            {
                "item_id": "a1",
                "title": "エヴァンゲリオン 30周年フェス限定 綾波レイ ユニオンアリーナ プロモカード",
                "price_jpy": 6400,
                "url": "https://jp.mercari.com/item/a1",
                "thumbnail_url": "",
            },
            {
                "item_id": "a2",
                "title": "エヴァンゲリオン ポスター 30周年",
                "price_jpy": 1200,
                "url": "https://jp.mercari.com/item/a2",
                "thumbnail_url": "",
            },
        ]

    def sold_search(query: str, max_results: int) -> list[dict[str, object]]:
        return [
            {
                "item_id": "s1",
                "title": "PSA10 エヴァンゲリオン 30周年フェス限定 綾波レイ ユニオンアリーナ プロモカード",
                "price_jpy": 18000,
                "url": "https://jp.mercari.com/item/s1",
                "thumbnail_url": "",
            },
            {
                "item_id": "s2",
                "title": "エヴァンゲリオン 30周年フェス限定 綾波レイ ユニオンアリーナ プロモカード",
                "price_jpy": 7000,
                "url": "https://jp.mercari.com/item/s2",
                "thumbnail_url": "",
            },
        ]

    handler = build_research_handler(
        notifier_factory=lambda chat_id: FakeNotifier(),
        item_fetcher=item_fetcher,
        knowledge_db_path=str(tmp_path / "knowledge.sqlite3"),
        active_market_search_fn=active_search,
        sold_market_search_fn=sold_search,
        sold_average_lookup_fn=lambda query: None,
    )

    reply = handler("https://jp.mercari.com/item/m18542743389", "chat-1")

    assert "Mercari sold 樣本 1 筆，均價約 ¥7,000" in reply
    assert "Warnings：" in reply
    assert "sold 候選排除了 1 筆低相關樣本。" in reply
    assert "active 候選排除了 1 筆低相關樣本。" in reply
    assert "https://jp.mercari.com/item/s2" in reply
    assert "https://jp.mercari.com/item/s1" not in reply


def test_research_handler_includes_seller_snapshot_result(tmp_path: Path) -> None:
    notifier = FakeNotifier()
    item_fetcher = MercariItemAdapter(
        fetch_html_fn=lambda _url: _load_fixture("mercari_item_m85537287496.html")
    )

    def seller_lookup(seller_url: str) -> SellerReputationSnapshot:
        assert seller_url == "https://jp.mercari.com/user/profile/433414807"
        return SellerReputationSnapshot(
            seller_url=seller_url,
            proof_url="http://127.0.0.1:5000/p/proof_123",
            proof_id="proof_123",
            reused=True,
            display_name="kiko",
            captured_at="2026-06-12T01:23:45+09:00",
            total_reviews=4864,
            listing_count=12,
            followers_count=345,
            following_count=22,
            seller_positive=120,
            seller_negative=0,
            seller_rate=100.0,
            overall_rate=100.0,
        )

    handler = build_research_handler(
        notifier_factory=lambda chat_id: notifier,
        item_fetcher=item_fetcher,
        knowledge_db_path=str(tmp_path / "knowledge.sqlite3"),
        seller_snapshot_lookup_fn=seller_lookup,
        active_market_search_fn=_fake_active_search,
        sold_market_search_fn=_fake_sold_search,
        sold_average_lookup_fn=_fake_sold_average,
    )

    reply = handler("https://jp.mercari.com/item/m85537287496", "chat-1")

    assert "賣家風險分析 [ok]" in reply
    assert "快照顯示賣家風險偏低。" in reply
    assert notifier.messages[-1].startswith("✅ [6/6] 完成（賣家 kiko / 總評價 4864")


def test_research_handler_summarizes_negative_seller_reviews(tmp_path: Path) -> None:
    item_fetcher = MercariItemAdapter(
        fetch_html_fn=lambda _url: _load_fixture("mercari_item_m18542743389.html")
    )

    def seller_lookup(seller_url: str) -> SellerReputationSnapshot:
        assert seller_url == "https://jp.mercari.com/user/profile/146184751"
        return SellerReputationSnapshot(
            seller_url=seller_url,
            proof_url="http://127.0.0.1:5000/p/proof_456",
            proof_id="proof_456",
            reused=False,
            display_name="risk seller",
            captured_at="2026-06-12T02:34:56+09:00",
            total_reviews=220,
            listing_count=21,
            followers_count=88,
            following_count=5,
            seller_positive=47,
            seller_negative=3,
            seller_rate=94.0,
            overall_rate=97.8,
            seller_negative_excerpts=(
                "発送が予定より遅かった。連絡も少なかったです。",
                "商品の状態が説明より悪かったです。少し残念でした。",
                "発送が予定より遅かった。連絡も少なかったです。",
            ),
        )

    handler = build_research_handler(
        notifier_factory=lambda chat_id: FakeNotifier(),
        item_fetcher=item_fetcher,
        knowledge_db_path=str(tmp_path / "knowledge.sqlite3"),
        seller_snapshot_lookup_fn=seller_lookup,
        active_market_search_fn=_fake_active_search,
        sold_market_search_fn=_fake_sold_search,
        sold_average_lookup_fn=_fake_sold_average,
    )

    reply = handler("https://jp.mercari.com/item/m18542743389", "chat-1")

    assert "快照顯示賣家風險中等，建議人工查看差評內容。" in reply
    assert "差評重點：発送遲延 / 商品狀態落差 / 溝通回覆問題。" in reply
    assert "最近差評例：" in reply
    assert "発送が予定より遅かった。連絡も少なかったです。" in reply
    assert "商品の状態が説明より悪かったです。少し残念でした。" in reply


def test_research_handler_degrades_when_seller_snapshot_fails(tmp_path: Path) -> None:
    item_fetcher = MercariItemAdapter(
        fetch_html_fn=lambda _url: _load_fixture("mercari_item_m18542743389.html")
    )

    def failing_lookup(_seller_url: str) -> SellerReputationSnapshot:
        raise RuntimeError("snapshot server unavailable")

    handler = build_research_handler(
        notifier_factory=lambda chat_id: FakeNotifier(),
        item_fetcher=item_fetcher,
        knowledge_db_path=str(tmp_path / "knowledge.sqlite3"),
        seller_snapshot_lookup_fn=failing_lookup,
        active_market_search_fn=_fake_active_search,
        sold_market_search_fn=_fake_sold_search,
        sold_average_lookup_fn=_fake_sold_average,
    )

    reply = handler("https://jp.mercari.com/item/m18542743389", "chat-1")

    assert "賣家風險分析 [partial]" in reply
    assert "賣家 reputation snapshot 失敗：snapshot server unavailable" in reply
    assert "/snapshot https://jp.mercari.com/user/profile/146184751" in reply


def test_research_handler_falls_back_to_item_url_for_snapshot_when_seller_url_missing(tmp_path: Path) -> None:
    class MissingSellerUrlFetcher:
        def fetch(self, _target):
            return ItemData(
                source_site="mercari",
                item_url="https://jp.mercari.com/item/m99999999999",
                item_id="m99999999999",
                title="測試商品",
                listed_price_jpy=4200,
                description="desc",
                condition_label="新品、未使用",
                seller_id=None,
                seller_url=None,
                image_urls=(),
                fetched_at="2026-06-12T00:00:00+00:00",
                source_confidence=0.7,
            )

    seen: list[str] = []

    def seller_lookup(query_url: str) -> SellerReputationSnapshot:
        seen.append(query_url)
        return SellerReputationSnapshot(
            seller_url="https://jp.mercari.com/user/profile/fallback",
            proof_url="http://127.0.0.1:5000/p/proof_fallback",
            proof_id="proof_fallback",
            reused=False,
            display_name="fallback seller",
            captured_at="2026-06-12T01:23:45+09:00",
            total_reviews=12,
            listing_count=3,
            followers_count=None,
            following_count=None,
            seller_positive=12,
            seller_negative=0,
            seller_rate=100.0,
        )

    handler = build_research_handler(
        notifier_factory=lambda chat_id: FakeNotifier(),
        item_fetcher=MissingSellerUrlFetcher(),
        knowledge_db_path=str(tmp_path / "knowledge.sqlite3"),
        seller_snapshot_lookup_fn=seller_lookup,
        active_market_search_fn=_fake_active_search,
        sold_market_search_fn=_fake_sold_search,
        sold_average_lookup_fn=_fake_sold_average,
    )

    reply = handler("https://jp.mercari.com/item/m99999999999", "chat-1")

    assert seen == ["https://jp.mercari.com/item/m99999999999"]
    assert "賣家風險分析 [ok]" in reply


def test_research_handler_degrades_when_market_search_backends_fail(tmp_path: Path) -> None:
    item_fetcher = MercariItemAdapter(
        fetch_html_fn=lambda _url: _load_fixture("mercari_item_m18542743389.html")
    )

    def fail_active(_query: str, _price_cap: int, _max_results: int) -> list[dict[str, object]]:
        raise RuntimeError("active backend boom")

    def fail_sold(_query: str, _max_results: int) -> list[dict[str, object]]:
        raise RuntimeError("sold backend boom")

    def fail_avg(_query: str) -> float | None:
        raise RuntimeError("avg backend boom")

    handler = build_research_handler(
        notifier_factory=lambda chat_id: FakeNotifier(),
        item_fetcher=item_fetcher,
        knowledge_db_path=str(tmp_path / "knowledge.sqlite3"),
        active_market_search_fn=fail_active,
        sold_market_search_fn=fail_sold,
        sold_average_lookup_fn=fail_avg,
    )

    reply = handler("https://jp.mercari.com/item/m18542743389", "chat-1")

    assert "合理市價分析 [unavailable]" in reply
    assert "Mercari active 比價抓取失敗：active backend boom" in reply
    assert "Mercari sold 比價抓取失敗：sold backend boom" in reply
    assert "Mercari sold 均價查詢失敗：avg backend boom" in reply


def test_research_handler_degrades_when_appreciation_search_backend_fails(tmp_path: Path) -> None:
    item_fetcher = MercariItemAdapter(
        fetch_html_fn=lambda _url: _load_fixture("mercari_item_m18542743389.html")
    )

    def fail_search(_query: str, _limit: int) -> tuple[object, ...]:
        raise RuntimeError("search backend boom")

    handler = build_research_handler(
        notifier_factory=lambda chat_id: FakeNotifier(),
        search_fn=fail_search,
        item_fetcher=item_fetcher,
        knowledge_db_path=str(tmp_path / "knowledge.sqlite3"),
        active_market_search_fn=_fake_active_search,
        sold_market_search_fn=_fake_sold_search,
        sold_average_lookup_fn=_fake_sold_average,
    )

    reply = handler("https://jp.mercari.com/item/m18542743389", "chat-1")

    assert "增值潛力分析" in reply
    assert "search backend boom" not in reply
