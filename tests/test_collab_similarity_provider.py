"""Tests for CollabSimilarityProvider (D3)."""

from __future__ import annotations

import pytest

from openclaw_adapter.collab_outcomes_store import CollabOutcome, CollabOutcomesStore, make_case_id
from openclaw_adapter.collab_similarity_provider import (
    CollabInference,
    CollabSimilarityProvider,
    SimilarCase,
    _ip_type,
)


@pytest.fixture
def store(tmp_path):
    return CollabOutcomesStore(tmp_path / "collab.sqlite3")


def _outcome(ip, tcg, announce, *, r30=1.5, r180=1.8, heat=75.0, conf=0.7, notes="") -> CollabOutcome:
    p30 = round((r30 - 1.0) * 100, 1)
    p180 = round((r180 - 1.0) * 100, 1)
    return CollabOutcome(
        case_id=make_case_id(ip, tcg, announce),
        ip_canonical=ip,
        tcg_game=tcg,
        product_name=f"{ip} × {tcg}",
        announce_date=announce,
        lottery_open_date=None,
        release_date=None,
        lottery_price_jpy=4400.0,
        secondary_30d_ratio=r30,
        secondary_180d_ratio=r180,
        profit_pct_30d=p30,
        profit_pct_180d=p180,
        ip_heat_at_announce=heat,
        confidence=conf,
        source_urls=[],
        notes=notes,
    )


# ── _ip_type ───────────────────────────────────────────────────────────────────


def test_ip_type_known():
    assert _ip_type("chainsaw man") == "shounen"
    assert _ip_type("hololive") == "vtuber"
    assert _ip_type("pokemon") == "pokemon"


def test_ip_type_unknown():
    assert _ip_type("some unknown ip") == "other"


def test_ip_type_case_insensitive():
    assert _ip_type("Chainsaw Man") == "shounen"


# ── infer — basic ──────────────────────────────────────────────────────────────


def test_infer_empty_store_returns_zero_samples(store):
    prov = CollabSimilarityProvider(store)
    result = prov.infer("chainsaw man", "union_arena")
    assert result.n_samples == 0
    assert result.mean_profit_pct_180d is None


def test_infer_returns_top_n(store):
    for i in range(10):
        store.upsert(_outcome("jujutsu kaisen", "weiss_schwarz", f"2023-0{i+1:02d}-01"))
    prov = CollabSimilarityProvider(store, top_n=3)
    result = prov.infer("jujutsu kaisen", "weiss_schwarz")
    assert result.n_samples == 3


def test_infer_prefers_same_tcg(store):
    store.upsert(_outcome("demon slayer", "weiss_schwarz", "2022-01-01", r180=3.0))
    store.upsert(_outcome("demon slayer", "union_arena", "2022-02-01", r180=1.2))
    prov = CollabSimilarityProvider(store, top_n=2)
    result = prov.infer("chainsaw man", "weiss_schwarz")
    # weiss_schwarz entry should have higher similarity score
    ws_case = next(c for c in result.similar_cases if c.tcg_game == "weiss_schwarz")
    ua_case = next(c for c in result.similar_cases if c.tcg_game == "union_arena")
    assert ws_case.similarity_score > ua_case.similarity_score


def test_infer_prefers_same_ip(store):
    store.upsert(_outcome("chainsaw man", "weiss_schwarz", "2023-01-01", r180=2.5))
    store.upsert(_outcome("one piece", "weiss_schwarz", "2023-02-01", r180=1.3))
    prov = CollabSimilarityProvider(store, top_n=2)
    result = prov.infer("chainsaw man", "weiss_schwarz")
    csm = next(c for c in result.similar_cases if c.ip_canonical == "chainsaw man")
    op = next(c for c in result.similar_cases if c.ip_canonical == "one piece")
    assert csm.similarity_score > op.similarity_score


def test_infer_same_ip_type_scores_higher_than_other(store):
    store.upsert(_outcome("demon slayer", "weiss_schwarz", "2022-01-01"))  # shounen
    store.upsert(_outcome("hololive", "weiss_schwarz", "2022-02-01"))       # vtuber
    prov = CollabSimilarityProvider(store, top_n=2)
    result = prov.infer("jujutsu kaisen", "weiss_schwarz")  # shounen
    ds = next(c for c in result.similar_cases if c.ip_canonical == "demon slayer")
    hl = next(c for c in result.similar_cases if c.ip_canonical == "hololive")
    assert ds.similarity_score > hl.similarity_score


def test_infer_heat_bonus_within_20_pct(store):
    store.upsert(_outcome("demon slayer", "weiss_schwarz", "2022-01-01", heat=80.0))
    store.upsert(_outcome("one piece", "weiss_schwarz", "2022-02-01", heat=20.0))
    prov = CollabSimilarityProvider(store, top_n=2)
    result = prov.infer("chainsaw man", "weiss_schwarz", ip_heat=85.0)
    ds = next(c for c in result.similar_cases if c.ip_canonical == "demon slayer")
    op = next(c for c in result.similar_cases if c.ip_canonical == "one piece")
    assert ds.similarity_score > op.similarity_score


# ── infer — statistics ─────────────────────────────────────────────────────────


def test_infer_mean_profit_correct(store):
    store.upsert(_outcome("demon slayer", "weiss_schwarz", "2022-01-01", r180=2.0))  # +100%
    store.upsert(_outcome("jujutsu kaisen", "weiss_schwarz", "2022-02-01", r180=3.0))  # +200%
    prov = CollabSimilarityProvider(store, top_n=2)
    result = prov.infer("chainsaw man", "weiss_schwarz")
    assert result.mean_profit_pct_180d == pytest.approx(150.0)


def test_infer_win_rate_all_positive(store):
    store.upsert(_outcome("demon slayer", "weiss_schwarz", "2022-01-01", r180=2.0))
    store.upsert(_outcome("jujutsu kaisen", "weiss_schwarz", "2022-02-01", r180=1.5))
    prov = CollabSimilarityProvider(store, top_n=2)
    result = prov.infer("chainsaw man", "weiss_schwarz")
    assert result.win_rate_180d == pytest.approx(1.0)


def test_infer_win_rate_mixed(store):
    store.upsert(_outcome("demon slayer", "weiss_schwarz", "2022-01-01", r180=0.8))  # -20%
    store.upsert(_outcome("jujutsu kaisen", "weiss_schwarz", "2022-02-01", r180=1.5))  # +50%
    prov = CollabSimilarityProvider(store, top_n=2)
    result = prov.infer("chainsaw man", "weiss_schwarz")
    assert result.win_rate_180d == pytest.approx(0.5)


def test_infer_best_and_worst(store):
    store.upsert(_outcome("demon slayer", "weiss_schwarz", "2022-01-01", r180=4.0))   # +300%
    store.upsert(_outcome("jujutsu kaisen", "weiss_schwarz", "2022-02-01", r180=0.9))  # -10%
    prov = CollabSimilarityProvider(store, top_n=2)
    result = prov.infer("chainsaw man", "weiss_schwarz")
    assert result.best_profit_pct_180d == pytest.approx(300.0)
    assert result.worst_profit_pct_180d == pytest.approx(-10.0)


def test_infer_confidence_filter(store):
    store.upsert(_outcome("demon slayer", "weiss_schwarz", "2022-01-01", r180=2.0, conf=0.3))
    store.upsert(_outcome("jujutsu kaisen", "weiss_schwarz", "2022-02-01", r180=3.0, conf=0.8))
    prov = CollabSimilarityProvider(store, min_confidence=0.5)
    result = prov.infer("chainsaw man", "weiss_schwarz")
    assert result.n_samples == 1
    assert result.similar_cases[0].ip_canonical == "jujutsu kaisen"


# ── as_prompt_block / as_notification_block ────────────────────────────────────


def test_as_prompt_block_empty_when_no_samples(store):
    prov = CollabSimilarityProvider(store)
    result = prov.infer("unknown ip", "unknown_tcg")
    assert result.as_prompt_block() == ""


def test_as_prompt_block_contains_stats(store):
    store.upsert(_outcome("demon slayer", "weiss_schwarz", "2022-01-01", r180=2.0))
    prov = CollabSimilarityProvider(store, top_n=1)
    result = prov.infer("chainsaw man", "weiss_schwarz")
    block = result.as_prompt_block()
    assert "180" in block
    assert "%" in block
    assert "chainsaw man" in block


def test_as_notification_block_contains_jp_label(store):
    store.upsert(_outcome("jujutsu kaisen", "union_arena", "2023-01-01", r180=1.8))
    prov = CollabSimilarityProvider(store, top_n=1)
    result = prov.infer("chainsaw man", "union_arena")
    block = result.as_notification_block()
    assert "📊" in block
    assert "歴史推理" in block


def test_as_notification_block_empty_when_no_samples(store):
    prov = CollabSimilarityProvider(store)
    result = prov.infer("x", "y")
    assert result.as_notification_block() == ""
