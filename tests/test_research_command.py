from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from openclaw_adapter.reputation_snapshot import SnapshotStillPending
from openclaw_adapter.research_command import (
    BudgetExhaustedError,
    ItemData,
    MercariItemAdapter,
    ResearchBudget,
    ResearchReport,
    ResearchSectionResult,
    SellerReputationSnapshot,
    ShopReference,
    build_budgeted_search_fn,
    build_research_handler,
    format_research_compact_report,
    normalize_mercari_item_url,
    normalize_mercari_shops_url,
    parse_research_target,
)
from openclaw_adapter.knowledge_db import KnowledgeDatabase, is_source_id
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


def test_normalize_market_title_folds_katakana_vu_row_variants() -> None:
    from openclaw_adapter.research_command import _normalize_market_title

    # ヴァイスシュヴァルツ (correct) vs ヴァイスシュバルツ (common b-row spelling)
    assert _normalize_market_title("桐谷遥 世界一の笑顔 ヴァイスシュヴァルツ SR") == (
        _normalize_market_title("桐谷遥 世界一の笑顔 ヴァイスシュバルツ SR")
    )


def test_comp_filter_keeps_katakana_vu_row_spelling_variant() -> None:
    from openclaw_adapter.research_command import _filter_market_items_for_price

    reference = "桐谷遥 世界一の笑顔 ヴァイスシュバルツ SR"
    items = [
        {"title": "ヴァイスシュヴァルツ 桐谷遥 世界一の笑顔 SR", "price": 700},
    ]
    kept, dropped = _filter_market_items_for_price(
        reference_title=reference, items=items, min_similarity=0.5
    )
    assert dropped == 0
    assert len(kept) == 1


# Production min_similarity gate; assert keep/drop rather than raw scores so the
# 0.32 threshold boundary is actually exercised.
_PROD_MIN_SIMILARITY = 0.32


def test_comp_filter_drops_subset_candidate_case_a() -> None:
    # candidate ⊂ reference: a bare single card must not pass as the same
    # sellable unit as the sealed BOX. Previously coverage + containment_bonus
    # inflated this above threshold; both are now gone.
    from openclaw_adapter.research_command import _filter_market_items_for_price

    reference = "黒炎の支配者 BOX シュリンク付き 未開封"
    items = [{"title": "黒炎の支配者", "price": 500}]
    kept, dropped = _filter_market_items_for_price(
        reference_title=reference, items=items, min_similarity=_PROD_MIN_SIMILARITY
    )
    assert kept == []
    assert dropped == 1


def test_comp_filter_keeps_same_sellable_unit_case_b() -> None:
    # Same tokens, different word order → still the same sealed BOX → KEEP.
    from openclaw_adapter.research_command import _filter_market_items_for_price

    reference = "黒炎の支配者 BOX シュリンク付き 未開封"
    items = [{"title": "黒炎の支配者 BOX 未開封 シュリンク付き", "price": 6000}]
    kept, dropped = _filter_market_items_for_price(
        reference_title=reference, items=items, min_similarity=_PROD_MIN_SIMILARITY
    )
    assert len(kept) == 1
    assert dropped == 0


def test_comp_filter_case_c_scores_below_case_b() -> None:
    # Shares product name but missing the BOX/spec tokens. PR1 (idf=None = plain
    # Jaccard) only removes subset inflation; it does NOT promise to drop this
    # 2-token overlap — that needs PR2 IDF. We assert the ordering (C < B) and
    # document that C may still pass the 0.32 gate under PR1.
    from openclaw_adapter.research_command import _title_similarity_score

    reference = "ポケモンカード 黒炎の支配者 BOX シュリンク付き"
    case_b_score = _title_similarity_score(
        reference, "ポケモンカード 黒炎の支配者 BOX シュリンク付き"
    )
    case_c_score = _title_similarity_score(reference, "ポケモンカード 黒炎の支配者")
    assert case_c_score < case_b_score


def test_weighted_jaccard_degenerates_to_plain_jaccard_when_idf_none() -> None:
    from openclaw_adapter.research_command import weighted_jaccard

    ref = {"a", "b", "c", "d"}
    cand = {"a", "b"}
    assert weighted_jaccard(ref, cand) == pytest.approx(2 / 4)
    assert weighted_jaccard(set(), set()) == 0.0


def test_weighted_jaccard_idf_interface_downweights_shared_common_token() -> None:
    from openclaw_adapter.research_command import weighted_jaccard

    ref = {"common", "rare"}
    cand = {"common"}
    idf = {"common": 1.0, "rare": 10.0}
    # Shared token is common (low idf); plain Jaccard would be 1/2, but with idf
    # the missing rare token dominates the union weight → much lower.
    assert weighted_jaccard(ref, cand, idf=idf) == pytest.approx(1.0 / 11.0)


# --- PR2: historical DF/IDF weighting ---------------------------------------

# Deterministic synthetic corpus: the product family words (ポケモンカード /
# 黒炎の支配者) recur across many single-card listings → low IDF; the sealed-box
# attributes (box / シュリンク付き / 未開封) appear in only a couple of docs →
# high IDF. This is pure statistics (Rule G): no term is boosted by hand, the
# weights come entirely from document frequency over these titles.
_IDF_FIXTURE_SINGLES = (
    "ピカチュウ", "リザードン ex", "ミュウ", "イーブイ", "ナンジャモ sar", "単品",
    "プロモ", "ピカチュウ ar", "リザードン sar", "ミライドン", "コライドン",
    "パオジアン ex", "セグレイブ", "サーフゴー ex", "まとめ売り", "美品", "傷あり",
    "ペリペリ付き", "未使用", "コレクション", "おまけ付き", "値下げ", "即購入可",
)


def _build_idf_fixture():
    from openclaw_adapter.research_command import build_title_idf_stats_from_titles

    corpus = [f"ポケモンカード 黒炎の支配者 {s}" for s in _IDF_FIXTURE_SINGLES]
    corpus += [
        f"ポケモンカード {s}"
        for s in ("151 リザードン", "クレイバースト", "スノーハザード", "vstar ユニバース")
    ]
    corpus += ["ポケモンカード 黒炎の支配者 box シュリンク付き 未開封"]
    corpus += ["ポケモンカード 黒炎の支配者 box 未開封"]
    return build_title_idf_stats_from_titles(corpus)


def test_weighted_jaccard_uses_idf_to_penalize_missing_high_info_tokens() -> None:
    # Unit Test 1: candidate keeps only the generic family tokens and is missing
    # the high-IDF BOX/シュリンク付き attributes. Plain Jaccard would be 2/4=0.5;
    # IDF weighting collapses it because the missing tokens dominate the union.
    from openclaw_adapter.research_command import weighted_jaccard

    ref = {"ポケモンカード", "黒炎の支配者", "BOX", "シュリンク付き"}
    cand = {"ポケモンカード", "黒炎の支配者"}
    idf = {"ポケモンカード": 1.0, "黒炎の支配者": 1.2, "BOX": 5.0, "シュリンク付き": 6.0}

    plain = weighted_jaccard(ref, cand)
    weighted = weighted_jaccard(ref, cand, idf=idf)
    assert plain == pytest.approx(0.5)
    assert weighted == pytest.approx(2.2 / 13.2)  # ≈ 0.1667
    assert weighted < plain


def test_weighted_jaccard_identical_high_info_unit_scores_one() -> None:
    # Unit Test 2: identical sets → intersection == union → 1.0 regardless of idf.
    from openclaw_adapter.research_command import weighted_jaccard

    tokens = {"黒炎の支配者", "BOX", "シュリンク付き", "未開封"}
    idf = {"黒炎の支配者": 1.2, "BOX": 5.0, "シュリンク付き": 6.0, "未開封": 4.0}
    assert weighted_jaccard(tokens, set(tokens), idf=idf) == pytest.approx(1.0)


def test_idf_formula_is_monotonic_smoothed_and_capped() -> None:
    from openclaw_adapter.research_command import _idf_from_df

    total = 1000
    common = _idf_from_df(900, total)
    rare = _idf_from_df(1, total)
    unseen = _idf_from_df(0, total)
    assert common < rare < unseen  # monotonically decreasing in df
    assert common >= 1.0  # never negative / never below the +1.0 floor
    # Cap protects against runaway weights from a tiny corpus.
    assert _idf_from_df(0, 10_000_000, max_idf=8.0) == pytest.approx(8.0)


def test_build_title_df_counts_each_title_once_per_unique_term() -> None:
    from openclaw_adapter.research_command import build_title_df_from_titles

    # "box" appears twice in one title but must contribute df 1 (set semantics).
    payload = build_title_df_from_titles(["box box ポケモンカード", "ポケモンカード 単品"])
    assert payload["total_docs"] == 2
    assert payload["token_df"]["box"] == 1
    assert payload["token_df"]["ポケモンカード"] == 2


def test_comp_filter_case_c_drops_with_representative_idf() -> None:
    # Integration Test 1: Case C (shares only the product family, missing the
    # BOX/spec attributes) must DROP through the real production filter path once
    # historical IDF is supplied. Under PR1 (idf=None) this scored 0.5455 (KEEP).
    from openclaw_adapter.research_command import _filter_market_items_for_price

    reference = "ポケモンカード 黒炎の支配者 BOX シュリンク付き"
    items = [{"title": "ポケモンカード 黒炎の支配者", "price": 800}]
    kept, dropped = _filter_market_items_for_price(
        reference_title=reference,
        items=items,
        min_similarity=_PROD_MIN_SIMILARITY,
        idf_stats=_build_idf_fixture(),
    )
    assert kept == []
    assert dropped == 1


def test_comp_filter_same_unit_keeps_with_idf() -> None:
    # Integration Test 2: reordered identical sealed-box unit must still KEEP even
    # with IDF active — the token Jaccard is 1.0 (intersection == union).
    from openclaw_adapter.research_command import _filter_market_items_for_price

    reference = "黒炎の支配者 BOX シュリンク付き 未開封"
    items = [{"title": "黒炎の支配者 BOX 未開封 シュリンク付き", "price": 6000}]
    kept, dropped = _filter_market_items_for_price(
        reference_title=reference,
        items=items,
        min_similarity=_PROD_MIN_SIMILARITY,
        idf_stats=_build_idf_fixture(),
    )
    assert len(kept) == 1
    assert dropped == 0


def test_comp_filter_case_a_stays_dropped_with_idf() -> None:
    # Regression: PR1's subset drop must survive PR2. A bare single card is a
    # strict subset of the sealed BOX and must stay DROPPED with IDF active.
    from openclaw_adapter.research_command import _filter_market_items_for_price

    reference = "黒炎の支配者 BOX シュリンク付き 未開封"
    items = [{"title": "黒炎の支配者", "price": 500}]
    kept, dropped = _filter_market_items_for_price(
        reference_title=reference,
        items=items,
        min_similarity=_PROD_MIN_SIMILARITY,
        idf_stats=_build_idf_fixture(),
    )
    assert kept == []
    assert dropped == 1


def test_load_title_idf_stats_missing_file_returns_none() -> None:
    from openclaw_adapter.research_command import load_title_idf_stats

    assert load_title_idf_stats(Path("/nonexistent/market_title_df.json")) is None


def test_comp_filter_cold_start_falls_back_to_plain_jaccard(monkeypatch) -> None:
    # Cold start: with no DF stats available the filter must behave exactly like
    # PR1 (plain Jaccard). Case C scored 0.5455 under PR1 → still KEEP at 0.32.
    from openclaw_adapter import research_command as rc

    monkeypatch.setattr(rc, "_default_title_idf_stats", lambda: None)
    reference = "ポケモンカード 黒炎の支配者 BOX シュリンク付き"
    items = [{"title": "ポケモンカード 黒炎の支配者", "price": 800}]
    kept, dropped = rc._filter_market_items_for_price(
        reference_title=reference, items=items, min_similarity=_PROD_MIN_SIMILARITY
    )
    assert len(kept) == 1
    assert dropped == 0


def test_explain_title_similarity_reports_missing_high_idf_attributes() -> None:
    from openclaw_adapter.research_command import explain_title_similarity

    report = explain_title_similarity(
        "ポケモンカード 黒炎の支配者 BOX シュリンク付き",
        "ポケモンカード 黒炎の支配者",
        idf_stats=_build_idf_fixture(),
    )
    assert set(report["matched_tokens"]) == {"ポケモンカード", "黒炎の支配者"}
    assert set(report["missing_from_candidate"]) == {"box", "シュリンク付き"}
    # Missing attributes carry strictly higher weight than the shared generics.
    assert min(report["missing_from_candidate"].values()) > max(
        report["matched_tokens"].values()
    )
    assert report["final_score"] < _PROD_MIN_SIMILARITY


# --- PR2: activation gate (thickness threshold + canary) + hot reload --------


def test_gate_rejects_thin_table() -> None:
    from openclaw_adapter.research_command import gate_title_idf_stats

    thin = _build_idf_fixture()  # ~29 docs
    assert thin.total_docs < 3000
    assert gate_title_idf_stats(thin) is None  # default min_docs gate
    # With a low min_docs it clears thickness and the canary still holds.
    assert gate_title_idf_stats(thin, min_docs=10) is thin


def test_gate_rejects_off_domain_table_via_canary() -> None:
    # A table that is "thick enough" but learned from an unrelated domain must be
    # rejected by the canary: here every term is unseen → uniform default weight →
    # the canary's subset-card DROP collapses back to plain Jaccard (0.2778) which
    # is fine, but a deliberately broken table (all equal, no discrimination) on a
    # KEEP case would fail. We assert the canary helper directly on a degenerate map.
    from openclaw_adapter.research_command import (
        TitleIdfStats,
        _passes_activation_canary,
    )

    # Degenerate: every token weighted identically and bigrams too → still must
    # preserve drop<0.32<=keep on the canary (plain-Jaccard-equivalent does).
    sane_like_plain = TitleIdfStats(
        total_docs=9999, token_idf={}, bigram_idf={},
        default_token_idf=1.0, default_bigram_idf=1.0,
    )
    assert _passes_activation_canary(sane_like_plain) is True

    # Now a pathological map that inverts importance (generic words heaviest):
    # it lifts the subset-card canary above threshold → must be rejected.
    inverted = TitleIdfStats(
        total_docs=9999,
        token_idf={"黒炎の支配者": 50.0, "box": 0.01, "シュリンク付き": 0.01, "未開封": 0.01},
        bigram_idf={},
        default_token_idf=0.01,
        default_bigram_idf=0.01,
    )
    assert _passes_activation_canary(inverted) is False


def test_default_idf_stats_hot_reloads_on_file_change(monkeypatch, tmp_path) -> None:
    # The long-running bot must pick up a rebuilt DF file with no restart: a change
    # in the file's mtime invalidates the in-memory cache on the next call.
    from openclaw_adapter import research_command as rc

    df_path = tmp_path / "market_title_df.json"
    monkeypatch.setattr(rc, "_TITLE_DF_PATH", df_path)
    monkeypatch.setattr(rc, "_MIN_TITLE_CORPUS_DOCS", 5)
    monkeypatch.setattr(rc, "_idf_cache", {"key": "__unset__", "stats": None})

    # Absent file → cold start (None).
    assert rc._default_title_idf_stats() is None

    # Write a thick-enough, canary-passing table built from real titles.
    titles = [f"ポケモンカード 黒炎の支配者 単品{i}" for i in range(20)]
    titles += ["ポケモンカード 黒炎の支配者 box シュリンク付き 未開封"]
    payload = rc.build_title_df_from_titles(titles)
    import json as _json

    df_path.write_text(_json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    reloaded = rc._default_title_idf_stats()
    assert reloaded is not None
    assert reloaded.total_docs == payload["total_docs"]


def test_appreciation_enricher_fetches_pages_and_summarizes() -> None:
    from openclaw_adapter.research_command import build_appreciation_enricher

    fetched: list[str] = []
    captured_sources: list[tuple] = []

    def _fake_fetch(url: str) -> str:
        fetched.append(url)
        return f"page text for {url}"

    def _fake_summarize(query: str, sources) -> str:
        captured_sources.append(sources)
        return f"催化劑：{query} 有再販與作者新作話題"

    enricher = build_appreciation_enricher(
        fetch_page_fn=_fake_fetch, summarize_fn=_fake_summarize, max_pages=2
    )
    results = (
        WebSearchResult(title="t1", url="https://a.example/1", snippet="s1"),
        WebSearchResult(title="t2", url="https://b.example/2", snippet="s2"),
        WebSearchResult(title="t3", url="https://c.example/3", snippet="s3"),
    )

    summary = enricher("桐谷遥 SR", results)

    assert fetched == ["https://a.example/1", "https://b.example/2"]  # capped at max_pages
    assert captured_sources[0][0].content == "page text for https://a.example/1"
    assert summary == "催化劑：桐谷遥 SR 有再販與作者新作話題"


def test_appreciation_enricher_relevance_gate_drops_offtopic_before_fetch() -> None:
    from openclaw_adapter.research_command import build_appreciation_enricher

    fetched: list[str] = []

    def _fake_fetch(url: str) -> str:
        fetched.append(url)
        return f"page text for {url}"

    def _fake_summarize(query: str, sources) -> str:
        return "催化劑"

    def _gate(query: str, sources):
        # Keep only the second source — the off-topic ones must never be fetched.
        return (sources[1],)

    enricher = build_appreciation_enricher(
        fetch_page_fn=_fake_fetch,
        summarize_fn=_fake_summarize,
        relevance_fn=_gate,
        max_pages=3,
    )
    results = (
        WebSearchResult(title="t1", url="https://a.example/1", snippet="s1"),
        WebSearchResult(title="t2", url="https://b.example/2", snippet="s2"),
        WebSearchResult(title="t3", url="https://c.example/3", snippet="s3"),
    )

    enricher("桐谷遥 SR", results)

    assert fetched == ["https://b.example/2"]


def test_appreciation_section_uses_enrichment_and_drops_snippet_warning() -> None:
    from openclaw_adapter.research_command import _build_appreciation_section_result

    results = (WebSearchResult(title="t", url="https://x.example", snippet="s"),)
    result = _build_appreciation_section_result(
        query="桐谷遥 SR",
        entries=(),
        heat_by_canonical={},
        search_results=results,
        enrichment="催化劑：近期有再販消息",
    )
    assert "web 催化劑摘要：催化劑：近期有再販消息" in result.summary
    assert all("snippet" not in w for w in result.warnings)
    assert result.status == "partial"


def test_parse_entity_profile_requires_confident_and_canonical() -> None:
    from openclaw_adapter.research_command import _parse_entity_profile

    assert _parse_entity_profile('{"confident": false, "canonical_query": "x"}') is None
    assert _parse_entity_profile('{"confident": true, "canonical_query": ""}') is None
    profile = _parse_entity_profile(
        '```json\n{"confident": true, "canonical_query": "桐谷遥 世界一の笑顔 '
        'ヴァイスシュヴァルツ SR", "card_name": "桐谷遥 世界一の笑顔", '
        '"series": "ヴァイスシュヴァルツ", "rarity": "SR", '
        '"aliases": ["ヴァイスシュバルツ 桐谷遥"]}\n```'
    )
    assert profile is not None
    assert profile.canonical_query == "桐谷遥 世界一の笑顔 ヴァイスシュヴァルツ SR"
    assert profile.series == "ヴァイスシュヴァルツ"
    assert "ヴァイスシュバルツ 桐谷遥" in profile.aliases


def test_identify_entities_uses_recognizer_canonical_for_price_query(tmp_path: Path) -> None:
    from openclaw_adapter.research_command import (
        EntityProfile,
        ItemData,
        ResearchBudget,
        ResearchCommandService,
        ResearchJobContext,
        _build_price_query,
    )

    db_path = str(tmp_path / "knowledge.sqlite3")
    KnowledgeDatabase(db_path).bootstrap()

    captured: list[str] = []

    def _recognizer(item: ItemData) -> EntityProfile:
        captured.append(item.title)
        return EntityProfile(
            canonical_query="桐谷遥 世界一の笑顔 ヴァイスシュヴァルツ SR",
            card_name="桐谷遥 世界一の笑顔",
            series="ヴァイスシュヴァルツ",
            rarity="SR",
            aliases=("ヴァイスシュバルツ 桐谷遥",),
        )

    service = ResearchCommandService(
        knowledge_db_path=db_path,
        entity_recognizer_fn=_recognizer,
    )
    item = ItemData(
        source_site="mercari",
        item_url="https://jp.mercari.com/item/m12345678901",
        item_id="m12345678901",
        title="桐谷遥 世界一の笑顔 ヴァイスシュバルツ SR",
        listed_price_jpy=1555,
        description="",
        condition_label=None,
        seller_id="ゴロリ",
        seller_url=None,
        image_urls=(),
        fetched_at="2026-06-17T00:00:00+00:00",
        source_confidence=0.7,
    )
    ctx = ResearchJobContext(
        raw_input=item.item_url,
        chat_id="1",
        notifier=FakeNotifier(),
        budget=ResearchBudget(max_searches=5),
        search_fn=lambda query, limit: (),
        item_data=item,
    )

    summary = service._stage_identify_entities(ctx)

    assert captured == [item.title]
    assert ctx.entity_profile is not None
    assert _build_price_query(ctx) == "桐谷遥 世界一の笑顔 ヴァイスシュヴァルツ SR"
    assert "canonical" in summary
    # the corrected canonical spelling resolves back to the item via alias
    assert (
        KnowledgeDatabase(db_path).lookup_canonical(
            "桐谷遥 世界一の笑顔 ヴァイスシュヴァルツ SR"
        )
        == "mercari:m12345678901"
    )


def test_parse_research_target_treats_non_url_as_text_query() -> None:
    target = parse_research_target("  初音ミク   15th   フィギュア ")

    assert target.mode == "text_query"
    assert target.display_text == "初音ミク 15th フィギュア"
    assert target.canonical_url is None


def test_normalize_mercari_shops_url_strips_query_and_returns_token() -> None:
    result = normalize_mercari_shops_url(
        "https://jp.mercari.com/shops/product/2JPEa5BQcDaCwxJamae4fp"
        "?source_location=share&utm_medium=share&utm_source=ios"
    )
    assert result == (
        "https://jp.mercari.com/shops/product/2JPEa5BQcDaCwxJamae4fp",
        "2JPEa5BQcDaCwxJamae4fp",
    )


def test_normalize_mercari_shops_url_rejects_non_shops() -> None:
    assert normalize_mercari_shops_url("https://jp.mercari.com/item/m65806654179") is None
    assert normalize_mercari_shops_url("https://example.com/shops/product/abc") is None


def test_parse_research_target_routes_shops_url_to_mercari_mode() -> None:
    target = parse_research_target(
        "https://jp.mercari.com/shops/product/2JPEa5BQcDaCwxJamae4fp?utm_source=ios"
    )
    assert target.mode == "mercari_url"
    assert target.item_id == "2JPEa5BQcDaCwxJamae4fp"
    assert target.canonical_url == (
        "https://jp.mercari.com/shops/product/2JPEa5BQcDaCwxJamae4fp"
    )


def test_parse_html_strips_shops_title_suffix_and_keeps_internal_hyphen() -> None:
    token = "2JPEa5BQcDaCwxJamae4fp"
    item_url = f"https://jp.mercari.com/shops/product/{token}"
    html = (
        "<html><head>"
        '<meta property="og:title" content="【中古】K-ON! MUSIC BOX 初回盤 - お宝創庫 メルカリ店">'
        '<meta property="og:image" content="'
        "https://assets.mercari-shops-static.com/-/large/plain/2JPEa59pmqLqDvMeerrUWM.jpg@jpg"
        '">'
        "</head><body></body></html>"
    )
    item = MercariItemAdapter().parse_html(html, item_url=item_url, item_id=token)
    assert item.title == "【中古】K-ON! MUSIC BOX 初回盤"
    assert item.image_urls == (
        "https://assets.mercari-shops-static.com/-/large/plain/2JPEa59pmqLqDvMeerrUWM.jpg@jpg",
    )


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
        heartbeat_interval_seconds=0.0,
    )

    reply = handler("https://jp.mercari.com/item/m65806654179?afid=foo", "chat-1")

    assert notifier.messages[0] == "⏳ /research 已開始，先抓商品頁與市場資料…"
    assert "⏳ [1/6] 取得商品資料：還在整理資料源配置" in notifier.messages
    assert "✅ 已抓到商品頁：M1 骨架：已保留商品抓取接點" in notifier.messages
    assert "✅ 已完成市場比價：M1 骨架：已保留合理市價分析階段" in notifier.messages
    assert "龍蝦 /research 已完成目前可用流程。" in reply
    assert "https://jp.mercari.com/item/m65806654179" in reply


def test_research_handler_supports_custom_final_formatter() -> None:
    notifier = FakeNotifier()
    seen: list[ResearchReport] = []

    def final_formatter(report: ResearchReport) -> str:
        seen.append(report)
        return format_research_compact_report(report)

    handler = build_research_handler(
        notifier_factory=lambda chat_id: notifier,
        stage_runners=(
            _parse_stage,
            _placeholder("M1 骨架：已保留商品抓取接點"),
            _placeholder("M1 骨架：已保留實體辨識與知識庫接點"),
            _placeholder("M1 骨架：已保留增值潛力分析階段"),
            _placeholder("M1 骨架：已保留合理市價分析階段"),
            _placeholder("M1 骨架：已保留流動性分析階段"),
            _placeholder("M1 骨架：已保留賣家風險分析階段"),
        ),
        active_market_search_fn=_fake_active_search,
        sold_market_search_fn=_fake_sold_search,
        sold_average_lookup_fn=_fake_sold_average,
        final_formatter=final_formatter,
    )

    reply = handler("https://jp.mercari.com/item/m65806654179", "chat-1")

    assert seen
    assert reply.startswith("/research 摘要")


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

    assert any(message.startswith("✅ 已抓到商品頁：標題") for message in notifier.messages)
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
    # source URLs are interned through the registry (issue #9 D4): the entry
    # stores S-ids that resolve back to the Mercari item URL.
    assert entry.source_urls and all(is_source_id(s) for s in entry.source_urls)
    resolved = [db.get_source(s).canonical_url for s in entry.source_urls]
    assert any("jp.mercari.com/item/m18542743389" in u for u in resolved)
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


def test_mercari_item_adapter_reads_condition_from_embedded_next_data_json() -> None:
    # SPA case: static HTML has no 商品の状態 row; the real condition lives in the
    # __NEXT_DATA__ JSON the page hydrates from. Title alone would mislabel it.
    html = """
    <html>
      <head>
        <title>桐谷遥 世界一の笑顔 ヴァイスシュバルツ SR by メルカリ</title>
        <meta name="description" content="generic mercari description">
        <meta name="product:price:amount" content="1555">
      </head>
      <body>
        <main><p>detail rendered client-side</p></main>
        <script id="__NEXT_DATA__" type="application/json">
          {"props": {"pageProps": {"item": {"name": "桐谷遥",
           "itemCondition": {"id": 3, "name": "目立った傷や汚れなし"}}}}}
        </script>
      </body>
    </html>
    """
    adapter = MercariItemAdapter(fetch_html_fn=lambda _url: html)

    item = adapter.fetch(parse_research_target("https://jp.mercari.com/item/m12345111111"))

    assert item.condition_label == "目立った傷や汚れなし"


def test_extract_condition_from_embedded_json_returns_none_without_known_label() -> None:
    from openclaw_adapter.research_command import _extract_condition_from_embedded_json

    html = (
        '<script id="__NEXT_DATA__" type="application/json">'
        '{"props": {"item": {"itemCondition": {"name": "なんか変な値"}}}}'
        "</script>"
    )
    assert _extract_condition_from_embedded_json(html) is None


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
                FakeHeatSignal("4chan", 71.0),
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
    assert "evangelion 近期 熱度高（google_trends 88pct / 4chan 71pct）。" in reply
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
        shop_reference_fn=lambda q, p: None,
    )

    reply = handler("https://jp.mercari.com/item/m18542743389", "chat-1")

    assert "合理市價分析 [ok]" in reply
    assert "Mercari sold 樣本 3 筆，均價約 ¥7,000" in reply
    assert "active 樣本 3 筆（mercari 3筆 中位¥6,800），中位數 ¥6,800，區間 ¥6,100–¥7,200" in reply
    assert "目前開價接近同條件（中古） sold 均價" in reply
    assert "流動性分析 [ok]" in reply
    assert "樣本顯示流動性中等，仍有一定成交速度。；active 3 筆（跨平台）/ Mercari sold 3 筆；sold/active 比 1.00" in reply
    assert "https://jp.mercari.com/item/s1" in reply


def test_research_handler_active_price_includes_non_mercari_platforms(tmp_path: Path) -> None:
    item_fetcher = MercariItemAdapter(
        fetch_html_fn=lambda _url: _load_fixture("mercari_item_m18542743389.html")
    )

    def active_search(query: str, price_cap: int, max_results: int) -> list[dict[str, object]]:
        return [
            {
                "source": "mercari",
                "item_id": "m1",
                "title": "エヴァンゲリオン 綾波レイ プロモ 1",
                "price_jpy": 6100,
                "url": "https://jp.mercari.com/item/m1",
                "thumbnail_url": "",
            },
            {
                "source": "rakuma",
                "item_id": "r1",
                "title": "エヴァンゲリオン 綾波レイ プロモ 2",
                "price_jpy": 6800,
                "url": "https://fril.jp/item/r1",
                "thumbnail_url": "",
            },
            {
                "source": "yuyutei",
                "item_id": "y1",
                "title": "エヴァンゲリオン 綾波レイ プロモ 3",
                "price_jpy": 7200,
                "url": "https://yuyu-tei.jp/sell/ua/card/y1",
                "thumbnail_url": "",
            },
        ]

    handler = build_research_handler(
        notifier_factory=lambda chat_id: FakeNotifier(),
        item_fetcher=item_fetcher,
        knowledge_db_path=str(tmp_path / "knowledge.sqlite3"),
        active_market_search_fn=active_search,
        sold_market_search_fn=lambda query, max_results: [],
        sold_average_lookup_fn=lambda query: None,
    )

    reply = handler("https://jp.mercari.com/item/m18542743389", "chat-multi")

    assert "active 樣本 3 筆（mercari 1筆 ¥6,100 / rakuma 1筆 ¥6,800 / yuyutei 1筆 ¥7,200）" in reply
    assert "https://fril.jp/item/r1" in reply
    assert "https://yuyu-tei.jp/sell/ua/card/y1" in reply


def test_research_renders_yuyutei_shop_reference_band() -> None:
    """A shop platform (Yuyu亭) must surface its 買取/販売 band in the market
    detail text — upper bound stock-backed — not just leave a clickable link."""
    def shop_reference(query: str, price_cap: int) -> ShopReference:
        return ShopReference(
            label="遊々亭",
            buy_reference=57000,
            sell_reference=79900,
            stock_total=2,
            buy_count=2,
            sell_count=2,
            sample_urls=("https://yuyu-tei.jp/buy/ua/card/b1", "https://yuyu-tei.jp/sell/ua/card/s1"),
            buy_min=50000,
            buy_max=64000,
            sell_min=75000,
            sell_max=85000,
        )

    handler = build_research_handler(
        notifier_factory=lambda chat_id: FakeNotifier(),
        active_market_search_fn=lambda q, cap, n: [],
        sold_market_search_fn=lambda q, n: [],
        sold_average_lookup_fn=lambda q: None,
        shop_reference_fn=shop_reference,
    )

    reply = handler("大好きを前に 桐谷遥 SSP UA", "chat-shop")

    assert "遊々亭参考帯 買取¥50,000〜¥64,000／販売¥75,000〜¥85,000（在庫2点）" in reply
    assert "https://yuyu-tei.jp/sell/ua/card/s1" in reply


def test_research_shop_reference_sell_only_when_out_of_stock_drops_upper() -> None:
    """When 販売 has no stock, only 買取 (lower) is shown and flagged as a weak
    upper reference — the user's 庫存0 concern."""
    def shop_reference(query: str, price_cap: int) -> ShopReference:
        return ShopReference(
            label="遊々亭",
            buy_reference=57000,
            sell_reference=None,
            stock_total=0,
            buy_count=1,
            sell_count=0,
            sample_urls=("https://yuyu-tei.jp/buy/ua/card/b1",),
        )

    handler = build_research_handler(
        notifier_factory=lambda chat_id: FakeNotifier(),
        active_market_search_fn=lambda q, cap, n: [],
        sold_market_search_fn=lambda q, n: [],
        sold_average_lookup_fn=lambda q: None,
        shop_reference_fn=shop_reference,
    )

    reply = handler("大好きを前に 桐谷遥 SSP UA", "chat-shop-oos")

    assert "遊々亭参考 買取¥57,000（販売在庫なし、上限參考弱）" in reply


def test_research_single_non_mercari_source_shows_platform_and_price_in_text() -> None:
    """When active listings come from a single non-Mercari source (e.g. Yuyutei
    alone), the summary text must name the platform and its price inline — not
    just drop a bare link the user has to click."""
    def active_search(query: str, price_cap: int, max_results: int) -> list[dict[str, object]]:
        return [
            {
                "source": "yuyutei",
                "item_id": "y1",
                "title": "大好きを前に 桐谷遥 SSP",
                "price_jpy": 79900,
                "url": "https://yuyu-tei.jp/sell/ua/card/y1",
                "thumbnail_url": "",
            },
        ]

    handler = build_research_handler(
        notifier_factory=lambda chat_id: FakeNotifier(),
        active_market_search_fn=active_search,
        sold_market_search_fn=lambda query, max_results: [],
        sold_average_lookup_fn=lambda query: None,
    )

    reply = handler("大好きを前に 桐谷遥 SSP", "chat-yuyu")

    assert "yuyutei 1筆 ¥79,900" in reply


def test_research_active_price_cap_uses_sold_average_for_high_value_text_query() -> None:
    """Bare keyword (no listed price) for a high-value item: the active cap must
    be derived from the sold average, not fall back to the low ¥50,000 default,
    otherwise every active listing gets price-filtered out."""
    seen_caps: list[int] = []

    def active_search(query: str, price_cap: int, max_results: int) -> list[dict[str, object]]:
        seen_caps.append(price_cap)
        return [
            {
                "source": "mercari",
                "item_id": "h1",
                "title": "ピカチュウ SAR プロモ A",
                "price_jpy": 110000,
                "url": "https://jp.mercari.com/item/h1",
                "thumbnail_url": "",
            }
        ]

    def sold_search(query: str, max_results: int) -> list[dict[str, object]]:
        return [
            {
                "source": "mercari",
                "item_id": "hs1",
                "title": "ピカチュウ SAR プロモ B",
                "price_jpy": 120000,
                "url": "https://jp.mercari.com/item/hs1",
                "thumbnail_url": "",
            },
            {
                "source": "mercari",
                "item_id": "hs2",
                "title": "ピカチュウ SAR プロモ C",
                "price_jpy": 116000,
                "url": "https://jp.mercari.com/item/hs2",
                "thumbnail_url": "",
            },
        ]

    handler = build_research_handler(
        notifier_factory=lambda chat_id: FakeNotifier(),
        active_market_search_fn=active_search,
        sold_market_search_fn=sold_search,
        sold_average_lookup_fn=lambda query: None,
    )

    reply = handler("ピカチュウ SAR プロモ", "chat-highval")

    assert seen_caps == [236000]
    assert "https://jp.mercari.com/item/h1" in reply


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
        shop_reference_fn=lambda q, p: None,
    )

    reply = handler("初音ミク 15th フィギュア", "chat-1")

    assert "研究模式：商品名稱" in reply
    assert "合理市價分析 [partial]" in reply
    assert "active 樣本 2 筆（mercari 2筆 中位¥9,400），中位數 ¥9,400，區間 ¥9,000–¥9,800" in reply
    assert "流動性分析 [partial]" in reply
    assert "樣本偏少，流動性暫時只能做弱判讀。；active 2 筆（跨平台）/ Mercari sold 0 筆；sold/active 比 0.00" in reply
    assert "Mercari sold 價目前只拿到平均值接口；此查詢未回傳可用 sold avg。" in reply


def test_research_handler_splits_active_band_by_new_vs_used_condition() -> None:
    query = "ずっと真夜中でいいのに 形藻土 通常盤"

    def active_search(q: str, price_cap: int, max_results: int) -> list[dict[str, object]]:
        return [
            # New: explicit 新品/未開封 claim in title → 新品 bucket.
            {"item_id": "n1", "title": f"{query} 新品未開封 シュリンク付", "price_jpy": 4800,
             "url": "https://jp.mercari.com/item/n1", "thumbnail_url": ""},
            {"item_id": "n2", "title": f"{query} 新品", "price_jpy": 5200,
             "url": "https://jp.mercari.com/item/n2", "thumbnail_url": ""},
            # Used: no new-claim (incl. 未使用に近い, a Mercari *used* grade) → 中古.
            {"item_id": "u1", "title": f"{query} 未使用に近い", "price_jpy": 3000,
             "url": "https://jp.mercari.com/item/u1", "thumbnail_url": ""},
            {"item_id": "u2", "title": f"{query} 開封済み", "price_jpy": 2600,
             "url": "https://jp.mercari.com/item/u2", "thumbnail_url": ""},
        ]

    handler = build_research_handler(
        notifier_factory=lambda chat_id: FakeNotifier(),
        active_market_search_fn=active_search,
        sold_market_search_fn=_fake_sold_search,
        sold_average_lookup_fn=lambda q: None,
    )

    reply = handler(query, "chat-1")

    assert "・中古 active 2 筆，中位數 ¥2,800，區間 ¥2,600–¥3,000" in reply
    assert "・新品 active 2 筆，中位數 ¥5,000，區間 ¥4,800–¥5,200" in reply


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
    assert any(message.startswith("✅ 已完成市場比價：") for message in notifier.messages)


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


def test_research_compact_report_backfills_seller_from_snapshot_when_item_seller_missing(tmp_path: Path) -> None:
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

    def seller_lookup(_query_url: str) -> SellerReputationSnapshot:
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
        final_formatter=format_research_compact_report,
    )

    reply = handler("https://jp.mercari.com/item/m99999999999", "chat-1")

    assert "賣家：fallback seller" in reply


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
        shop_reference_fn=lambda q, p: None,
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


def _sold_evidence(*items: dict[str, object]) -> tuple:
    from openclaw_adapter.research_command import _price_evidence_from_market_item

    return tuple(_price_evidence_from_market_item(it, sold_status="sold") for it in items)


def test_price_comparison_uses_same_condition_not_pooled_average() -> None:
    # A 中古 listing at ¥2,790 against a sold sample that mixes 新品 (¥6,000) and
    # 中古 (¥3,000): pooled avg ¥5,000 would falsely read "低於 ~44%", but the
    # like-for-like 中古 avg ¥3,000 reads "接近". New vs used must not be mixed.
    from openclaw_adapter.research_command import _build_price_section_result

    sold = _sold_evidence(
        {"title": "メルカリ 商品 新品未開封", "price_jpy": 6000, "url": "https://jp.mercari.com/item/n1", "source": "mercari"},
        {"title": "メルカリ 商品 新品", "price_jpy": 6000, "url": "https://jp.mercari.com/item/n2", "source": "mercari"},
        {"title": "メルカリ 商品 中古", "price_jpy": 3000, "url": "https://jp.mercari.com/item/u1", "source": "mercari"},
    )
    pooled_avg = 5000.0

    result = _build_price_section_result(
        query="メルカリ 商品",
        listed_price_jpy=2790,
        active_evidence=(),
        sold_evidence=sold,
        sold_average_jpy=pooled_avg,
        listed_condition_label="中古",
    )

    assert "目前開價接近同條件（中古） sold 均價" in result.summary
    assert "低於" not in result.summary  # the misleading pooled comparison is gone


def test_price_comparison_withheld_when_no_same_condition_comp() -> None:
    # Listed item is 新品 but every sold comp is 中古 — a cross-condition % is the
    # exact mix we must avoid, so the comparison is withheld with a caveat.
    from openclaw_adapter.research_command import _build_price_section_result

    sold = _sold_evidence(
        {"title": "メルカリ 商品 中古 A", "price_jpy": 3000, "url": "https://jp.mercari.com/item/u1", "source": "mercari"},
        {"title": "メルカリ 商品 中古 B", "price_jpy": 3200, "url": "https://jp.mercari.com/item/u2", "source": "mercari"},
    )

    result = _build_price_section_result(
        query="メルカリ 商品",
        listed_price_jpy=6500,
        active_evidence=(),
        sold_evidence=sold,
        sold_average_jpy=3100.0,
        listed_condition_label="新品",
    )

    assert "無同條件（新品）sold 樣本，未做價差比較（避免新品／中古混比）" in result.summary
    # The misleading "open price > used average" reading must NOT appear.
    assert "高於" not in result.summary


def test_drop_price_outliers_removes_extreme_comp() -> None:
    from openclaw_adapter.research_command import _drop_price_outliers

    evidence = _sold_evidence(
        {"title": "商品 A", "price_jpy": 3000, "url": "https://jp.mercari.com/item/a", "source": "mercari"},
        {"title": "商品 B", "price_jpy": 3100, "url": "https://jp.mercari.com/item/b", "source": "mercari"},
        {"title": "商品 C", "price_jpy": 2950, "url": "https://jp.mercari.com/item/c", "source": "mercari"},
        {"title": "商品 D", "price_jpy": 3050, "url": "https://jp.mercari.com/item/d", "source": "mercari"},
        {"title": "誤植 99万", "price_jpy": 999999, "url": "https://jp.mercari.com/item/x", "source": "mercari"},
    )

    kept, dropped = _drop_price_outliers(evidence)

    assert dropped == 1
    assert all(e.price_jpy != 999999 for e in kept)


def test_drop_price_outliers_noop_small_sample() -> None:
    from openclaw_adapter.research_command import _drop_price_outliers

    evidence = _sold_evidence(
        {"title": "商品 A", "price_jpy": 3000, "url": "https://jp.mercari.com/item/a", "source": "mercari"},
        {"title": "誤植", "price_jpy": 999999, "url": "https://jp.mercari.com/item/x", "source": "mercari"},
    )

    kept, dropped = _drop_price_outliers(evidence)

    # Fewer than 4 priced comps → too few to call an outlier, keep everything.
    assert dropped == 0
    assert len(kept) == 2


def test_price_section_surfaces_outlier_drop_and_driving_comps() -> None:
    from openclaw_adapter.research_command import _build_price_section_result

    sold = _sold_evidence(
        {"title": "メルカリ 商品 中古 A", "price_jpy": 3000, "url": "https://jp.mercari.com/item/u1", "source": "mercari"},
        {"title": "メルカリ 商品 中古 B", "price_jpy": 3100, "url": "https://jp.mercari.com/item/u2", "source": "mercari"},
        {"title": "メルカリ 商品 中古 C", "price_jpy": 2900, "url": "https://jp.mercari.com/item/u3", "source": "mercari"},
    )

    result = _build_price_section_result(
        query="メルカリ 商品",
        listed_price_jpy=3000,
        active_evidence=(),
        sold_evidence=sold,
        sold_average_jpy=3000.0,
        listed_condition_label="中古",
        sold_outliers=2,
    )

    # Outlier drop is surfaced as a warning, and the verdict names the comps it rests on.
    assert any("價格離群" in w for w in result.warnings)
    assert "結論依據" in result.summary
    assert "https://jp.mercari.com/item/u1" in result.summary


# ── Yuyu-tei cross-process cooldown + output note ─────────────────────────────

def test_yuyutei_cooldown_helpers_round_trip(tmp_path, monkeypatch) -> None:
    from openclaw_adapter import research_command as rc
    from market_monitor import http as http_module

    # The helpers delegate to the shared cross-process circuit breaker. Isolate
    # its marker file to tmp_path so the test never reads or deletes the real
    # cooldown a live OpenClaw process may have set for yuyu-tei.
    monkeypatch.setattr(
        http_module, "_circuit_file_path", lambda host: tmp_path / f"circuit_{host}"
    )

    assert rc._yuyutei_cooldown_remaining() == 0.0  # no marker → no cooldown

    rc._yuyutei_trip_cross_process_cooldown()
    remaining = rc._yuyutei_cooldown_remaining()
    assert 0.0 < remaining <= 300.0


def test_shop_reference_from_band_skips_subprocess_when_cooldown_active(monkeypatch) -> None:
    """When the cross-process cooldown is fresh, _shop_reference_from_band must
    raise RuntimeError before spawning a subprocess."""
    from openclaw_adapter import research_command as rc

    monkeypatch.setattr(rc, "_yuyutei_cooldown_remaining", lambda: 250.0)

    launched = []
    monkeypatch.setattr(rc, "run_in_subprocess", lambda *a, **kw: launched.append(a) or {})

    import pytest
    with pytest.raises(RuntimeError, match="rate-limited"):
        rc._shop_reference_from_band("query", 5000, None)

    assert launched == []  # subprocess never launched


def test_shop_reference_from_band_raises_after_subprocess_writes_cooldown(monkeypatch) -> None:
    """When the subprocess returns None AND the cooldown file was freshly written
    (by a 429 inside the subprocess), _shop_reference_from_band must raise so
    the caller adds a user-visible warning."""
    from openclaw_adapter import research_command as rc

    # Cooldown not active before subprocess, but written during it.
    calls = iter([0.0, 250.0])
    monkeypatch.setattr(rc, "_yuyutei_cooldown_remaining", lambda: next(calls))
    monkeypatch.setattr(rc, "run_in_subprocess", lambda *a, **kw: None)

    import pytest
    with pytest.raises(RuntimeError, match="rate-limited"):
        rc._shop_reference_from_band("query", 5000, None)


def test_shop_reference_from_band_raises_on_pre_network_budget_skip(monkeypatch) -> None:
    """A HostBudget pre-network refusal (cooldown / concurrency) must surface as a
    distinct RuntimeError, never collapse into a generic empty result (#24/#25)."""
    from openclaw_adapter import research_command as rc

    monkeypatch.setattr(rc, "_yuyutei_cooldown_remaining", lambda: 0.0)
    monkeypatch.setattr(
        rc, "run_in_subprocess",
        lambda *a, **kw: {
            "__budget_skip__": True,
            "decision": "skipped_concurrency_limit",
            "reason": "manual slot busy",
            "remaining_seconds": 0.0,
        },
    )

    import pytest
    with pytest.raises(RuntimeError, match="skipped before network"):
        rc._shop_reference_from_band("query", 5000, None)


def test_shop_reference_scrape_impl_returns_budget_skip_sentinel(monkeypatch) -> None:
    """When the Yuyutei client records a pre-network budget skip and yields no
    band, the impl returns the sentinel dict instead of a bare None."""
    from openclaw_adapter import research_command as rc
    from market_monitor.host_budget import DECISION_SKIPPED_CONCURRENCY_LIMIT
    from market_monitor.http import HostRateLimitedError

    class _FakeClient:
        def __init__(self, *a, **kw) -> None:
            self.last_budget_skip = HostRateLimitedError(
                "yuyu-tei.jp", 0.0,
                decision=DECISION_SKIPPED_CONCURRENCY_LIMIT, reason="busy",
            )

        def reference_band(self, *a, **kw):
            return None

    import market_monitor.yuyutei_search as ys
    monkeypatch.setattr(ys, "YuyuteiMarketplaceSearchClient", _FakeClient)

    out = rc._shop_reference_scrape_impl("query", 5000, None)
    assert isinstance(out, dict)
    assert out.get("__budget_skip__") is True
    assert out["decision"] == DECISION_SKIPPED_CONCURRENCY_LIMIT


def test_build_price_section_result_shows_yuyu_note_when_shop_failed() -> None:
    """When shop_reference is None and backend_warnings contains the shop failure
    string, the summary must include the "遊々亭参考：暫無法取得" note."""
    from openclaw_adapter.research_command import _build_price_section_result

    result = _build_price_section_result(
        query="ヴァイス SP",
        listed_price_jpy=None,
        active_evidence=(),
        sold_evidence=(),
        sold_average_jpy=None,
        shop_reference=None,
        backend_warnings=("店舗參考價抓取失敗：yuyu-tei.jp rate-limited (280s cross-process cooldown)",),
    )

    assert "遊々亭参考：暫無法取得" in result.summary


def test_build_price_section_result_no_yuyu_note_when_shop_legitimately_skipped() -> None:
    """When shop_reference is None but no shop failure warning was emitted
    (game code not found → legitimately skipped), no yuyu-tei note should appear."""
    from openclaw_adapter.research_command import _build_price_section_result

    result = _build_price_section_result(
        query="unknown product",
        listed_price_jpy=None,
        active_evidence=(),
        sold_evidence=(),
        sold_average_jpy=None,
        shop_reference=None,
    )

    assert "遊々亭" not in result.summary


def test_compact_price_summary_includes_yuyu_note() -> None:
    """_compact_price_summary should surface the 遊々亭 unavailability note."""
    from openclaw_adapter.research_command import _compact_price_summary, ResearchSectionResult

    result = ResearchSectionResult(
        section_name="合理市價分析",
        status="partial",
        confidence=0.3,
        sample_count=0,
        evidence_count=0,
        summary="遊々亭参考：暫無法取得（rate-limited）",
        evidence_urls=(),
        warnings=(),
    )

    compact = _compact_price_summary(result)
    assert "遊々亭" in compact


def test_compact_warning_label_recognizes_shop_failure() -> None:
    from openclaw_adapter.research_command import _compact_warning_label

    label = _compact_warning_label("店舗參考價抓取失敗：yuyu-tei.jp rate-limited (250s cross-process cooldown)")
    assert label == "遊々亭：無法取得店舗參考"


# ---------------------------------------------------------------------------
# PR3 — semantic rerank gate (spec Test 1-5). Gate is dependency-injected so
# unit tests never touch a real Ollama. ``_keep_all_gate`` / ``_drop_card_gate``
# stand in for the LLM's keep-index decision.
# ---------------------------------------------------------------------------

def _keep_all_gate(reference_title, reference_price, candidates):
    return {c.index for c in candidates}


def test_pr3_semantic_gate_rescues_cross_script_equivalent() -> None:
    # Mode 2: same sellable unit written in a different script. Lexical overlap
    # is weak (cross-script: book vs ブック2 share no anchor token), so it lands in
    # the gray zone rather than the lexical-kept set — and the mocked gate rescues
    # it. Uses the real default semantic_floor (0.18).
    from openclaw_adapter.research_command import _filter_market_items_with_semantic_gate

    reference = "YOASOBI THE BOOK II 完全生産限定盤 バインダー入CD"
    items = [{"title": "YOASOBI ザ・ブック2 完全生産限定盤 バインダー付き", "price": 4300}]
    kept, dropped = _filter_market_items_with_semantic_gate(
        reference_title=reference,
        reference_price=4400,
        items=items,
        min_similarity=_PROD_MIN_SIMILARITY,
        semantic_gate_fn=_keep_all_gate,
    )
    assert dropped == 0
    assert len(kept) == 1
    assert kept[0]["title"].startswith("YOASOBI ザ・ブック2")


def test_pr3_semantic_gate_drops_same_family_different_unit() -> None:
    # Mode 1: same product family, different sellable unit (single card vs BOX).
    # The mocked gate keeps only the BOX; the single card must be removed.
    from openclaw_adapter.research_command import _filter_market_items_with_semantic_gate

    reference = "黒炎の支配者 BOX シュリンク付き 未開封"
    items = [
        {"title": "黒炎の支配者 BOX 未開封 シュリンク付き", "price": 6000},
        {"title": "黒炎の支配者 SAR リザードンex", "price": 3900},
    ]

    def gate(reference_title, reference_price, candidates):
        return {c.index for c in candidates if "BOX" in c.title}

    kept, dropped = _filter_market_items_with_semantic_gate(
        reference_title=reference,
        reference_price=14800,
        items=items,
        min_similarity=_PROD_MIN_SIMILARITY,
        semantic_gate_fn=gate,
    )
    titles = [it["title"] for it in kept]
    assert any("BOX" in t for t in titles)
    assert all("SAR" not in t for t in titles)


def test_pr3_semantic_gate_failure_falls_back_to_lexical() -> None:
    # Gate raising OR returning None must fall back to the lexical-kept result,
    # never wipe out every comp.
    from openclaw_adapter.research_command import _filter_market_items_with_semantic_gate

    reference = "黒炎の支配者 BOX シュリンク付き 未開封"
    items = [{"title": "黒炎の支配者 BOX 未開封 シュリンク付き", "price": 6000}]

    def boom(reference_title, reference_price, candidates):
        raise RuntimeError("ollama down")

    kept, dropped = _filter_market_items_with_semantic_gate(
        reference_title=reference,
        reference_price=14800,
        items=items,
        min_similarity=_PROD_MIN_SIMILARITY,
        semantic_gate_fn=boom,
    )
    assert len(kept) == 1 and dropped == 0

    def undecided(reference_title, reference_price, candidates):
        return None

    kept2, dropped2 = _filter_market_items_with_semantic_gate(
        reference_title=reference,
        reference_price=14800,
        items=items,
        min_similarity=_PROD_MIN_SIMILARITY,
        semantic_gate_fn=undecided,
    )
    assert len(kept2) == 1 and dropped2 == 0


def test_pr3_semantic_gate_bounds_candidate_pool() -> None:
    # More candidates than max_semantic_candidates → gate sees only the bounded
    # top-N, so we never fire a giant local-LLM call.
    from openclaw_adapter.research_command import _filter_market_items_with_semantic_gate

    reference = "黒炎の支配者 BOX シュリンク付き 未開封"
    items = [
        {"title": f"黒炎の支配者 BOX 未開封 シュリンク付き ロット{i:02d}", "price": 6000 + i}
        for i in range(30)
    ]
    seen: dict[str, int] = {}

    def gate(reference_title, reference_price, candidates):
        seen["count"] = len(candidates)
        return {c.index for c in candidates}

    kept, _dropped = _filter_market_items_with_semantic_gate(
        reference_title=reference,
        reference_price=14800,
        items=items,
        min_similarity=_PROD_MIN_SIMILARITY,
        semantic_gate_fn=gate,
        max_semantic_candidates=20,
    )
    assert seen["count"] == 20
    assert len(kept) <= 20


def test_pr3_regression_subset_card_default_path_drops() -> None:
    # PR1/PR2 must still hold: a bare single card is dropped by the lexical
    # coarse filter on the default path (no gate) — no subset boosting.
    from openclaw_adapter.research_command import _filter_market_items_with_semantic_gate

    reference = "黒炎の支配者 BOX シュリンク付き 未開封"
    items = [{"title": "黒炎の支配者", "price": 500}]

    kept, dropped = _filter_market_items_with_semantic_gate(
        reference_title=reference,
        reference_price=14800,
        items=items,
        min_similarity=_PROD_MIN_SIMILARITY,
        semantic_gate_fn=None,
    )
    assert kept == [] and dropped == 1

    # ...but an explicit gate rescue can still keep it: the bare card scores in
    # the gray zone (>= semantic_floor) so it reaches the pool and the keep-all
    # gate rescues it. The default lexical path above still drops it.
    kept2, dropped2 = _filter_market_items_with_semantic_gate(
        reference_title=reference,
        reference_price=14800,
        items=items,
        min_similarity=_PROD_MIN_SIMILARITY,
        semantic_gate_fn=_keep_all_gate,
    )
    assert len(kept2) == 1 and dropped2 == 0


# ── Phase 2: parallel independent stages (3/4/6) ────────────────────────────────


def _section_stage(section_name: str, *, delay: float = 0.0, order_box: list | None = None):
    def run(ctx) -> str:
        if delay:
            time.sleep(delay)
        if order_box is not None:
            order_box.append(section_name)
        ctx.add_section_result(
            ResearchSectionResult(
                section_name=section_name,
                status="ok",
                confidence=1.0,
                sample_count=0,
                evidence_count=0,
                summary=f"{section_name} stub",
            )
        )
        return f"{section_name} note"

    return run


def test_research_parallel_stages_preserve_report_order() -> None:
    # Stage 6 (賣家風險) finishes first while 3/4 sleep, so section_results is
    # appended out of canonical order — the report must still be reordered.
    finish_order: list[str] = []
    handler = build_research_handler(
        notifier_factory=lambda chat_id: FakeNotifier(),
        stage_runners=(
            _parse_stage,
            _placeholder("item"),
            _placeholder("entity"),
            _section_stage("增值潛力分析", delay=0.25, order_box=finish_order),
            _section_stage("合理市價分析", delay=0.25, order_box=finish_order),
            _placeholder("liquidity"),
            _section_stage("賣家風險分析", delay=0.0, order_box=finish_order),
        ),
        active_market_search_fn=_fake_active_search,
        sold_market_search_fn=_fake_sold_search,
        sold_average_lookup_fn=_fake_sold_average,
        final_formatter=lambda report: report,
    )

    report = handler("https://jp.mercari.com/item/m65806654179", "chat-1")

    assert finish_order[0] == "賣家風險分析"  # out-of-order completion confirmed
    names = [section.section_name for section in report.section_results]
    assert names == ["增值潛力分析", "合理市價分析", "賣家風險分析"]


def test_research_parallel_stages_run_concurrently() -> None:
    # Three independent stages each sleep 0.3s. Serial would be ~0.9s; running
    # them on the thread pool overlaps the sleeps to ~0.3s wall-clock.
    handler = build_research_handler(
        notifier_factory=lambda chat_id: FakeNotifier(),
        stage_runners=(
            _parse_stage,
            _placeholder("item"),
            _placeholder("entity"),
            _section_stage("增值潛力分析", delay=0.3),
            _section_stage("合理市價分析", delay=0.3),
            _placeholder("liquidity"),
            _section_stage("賣家風險分析", delay=0.3),
        ),
        active_market_search_fn=_fake_active_search,
        sold_market_search_fn=_fake_sold_search,
        sold_average_lookup_fn=_fake_sold_average,
        final_formatter=lambda report: report,
    )

    start = time.monotonic()
    handler("https://jp.mercari.com/item/m65806654179", "chat-1")
    elapsed = time.monotonic() - start

    assert elapsed < 0.7


def test_research_budget_consume_is_thread_safe() -> None:
    budget = ResearchBudget(max_searches=1000)

    def worker() -> None:
        for _ in range(100):
            try:
                budget.consume()
            except BudgetExhaustedError:
                pass

    threads = [threading.Thread(target=worker) for _ in range(10)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert budget.searches_used == 1000


# ── SnapshotStillPending: background followup ────────────────────────────────


def test_research_handler_dispatches_followup_on_snapshot_still_pending(tmp_path: Path) -> None:
    item_fetcher = MercariItemAdapter(
        fetch_html_fn=lambda _url: _load_fixture("mercari_item_m18542743389.html")
    )
    followup_calls: list[tuple] = []

    def pending_lookup(_seller_url: str) -> SellerReputationSnapshot:
        def fake_poll():
            return None
        raise SnapshotStillPending("job_slow", poll_fn=fake_poll)

    def followup_fn(seller_url: str, poll_fn, notifier) -> None:
        followup_calls.append((seller_url, poll_fn, notifier))

    handler = build_research_handler(
        notifier_factory=lambda chat_id: FakeNotifier(),
        item_fetcher=item_fetcher,
        knowledge_db_path=str(tmp_path / "knowledge.sqlite3"),
        seller_snapshot_lookup_fn=pending_lookup,
        seller_snapshot_followup_fn=followup_fn,
        active_market_search_fn=_fake_active_search,
        sold_market_search_fn=_fake_sold_search,
        sold_average_lookup_fn=_fake_sold_average,
    )

    reply = handler("https://jp.mercari.com/item/m18542743389", "chat-1")

    # Section shows processing, not failure.
    assert "賣家風險分析 [partial]" in reply
    assert "處理中" in reply
    assert "snapshot server unavailable" not in reply
    # Followup fn was called exactly once with the right seller URL.
    assert len(followup_calls) == 1
    assert followup_calls[0][0] == "https://jp.mercari.com/user/profile/146184751"
    assert callable(followup_calls[0][1])


def test_research_handler_no_followup_fn_degrades_gracefully_on_still_pending(tmp_path: Path) -> None:
    item_fetcher = MercariItemAdapter(
        fetch_html_fn=lambda _url: _load_fixture("mercari_item_m18542743389.html")
    )

    def pending_lookup(_seller_url: str) -> SellerReputationSnapshot:
        raise SnapshotStillPending("job_slow", poll_fn=lambda: None)

    handler = build_research_handler(
        notifier_factory=lambda chat_id: FakeNotifier(),
        item_fetcher=item_fetcher,
        knowledge_db_path=str(tmp_path / "knowledge.sqlite3"),
        seller_snapshot_lookup_fn=pending_lookup,
        seller_snapshot_followup_fn=None,
        active_market_search_fn=_fake_active_search,
        sold_market_search_fn=_fake_sold_search,
        sold_average_lookup_fn=_fake_sold_average,
    )

    reply = handler("https://jp.mercari.com/item/m18542743389", "chat-1")
    assert "賣家風險分析 [partial]" in reply
    assert "處理中" in reply
