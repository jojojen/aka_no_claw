"""Tests for the SNS auto-discovery feedback learning loop.

Covers:
* `actionable_for_investing` gate — second LLM-supplied threshold that must
  clear before an account auto-adds.
* polarity-aware feedback timeline (`sns_auto_discovery_feedback`).
* per-domain trust (`sns_discovery_domain_trust`) — keep / reject counters
  and a monotonically-increasing actionable threshold.
* learning loop: rejecting accounts in a domain tightens that domain's
  effective threshold; positive feedback never loosens it.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from openclaw_adapter.opportunity_sns_discovery import (
    DEFAULT_ACTIONABLE_FLOOR,
    DEFAULT_MIN_CONFIDENCE,
    discover_tcg_sns_accounts,
)
from openclaw_adapter.web_search import WebSearchResult
from sns_monitor.storage import SnsDatabase


def _make_db(tmp_path: Path) -> SnsDatabase:
    db = SnsDatabase(tmp_path / "sns.sqlite3")
    db.bootstrap()
    return db


def _result(handle: str) -> list[WebSearchResult]:
    return [WebSearchResult(title="t", url=f"https://twitter.com/{handle}", snippet="")]


def _verdict(*, is_tcg=True, confidence=0.95, actionable=0.9, domains=("pokemon",)) -> str:
    return json.dumps({
        "is_tcg": is_tcg,
        "confidence": confidence,
        "actionable_for_investing": actionable,
        "domains": list(domains),
        "reason": "test",
    })


# ── new actionable gate ─────────────────────────────────────────────────────


def test_candidate_below_actionable_floor_is_skipped(tmp_path: Path) -> None:
    """Cold-start floor (0.75) blocks borderline-actionable accounts even
    when is_tcg + confidence both pass."""
    db = _make_db(tmp_path)
    added = discover_tcg_sns_accounts(
        sns_db=db,
        search_fn=lambda q, *, max_results: _result("decklist_blogger"),
        llm_fn=lambda p: _verdict(actionable=0.5),  # below 0.75 floor
        queries=("test",),
        max_new_per_run=1,
    )
    assert added == []


def test_candidate_above_actionable_floor_is_added(tmp_path: Path) -> None:
    """Cold-start: an account that clears both gates lands in the watchlist."""
    db = _make_db(tmp_path)
    added = discover_tcg_sns_accounts(
        sns_db=db,
        search_fn=lambda q, *, max_results: _result("drop_alerts"),
        llm_fn=lambda p: _verdict(actionable=0.85),
        queries=("test",),
        max_new_per_run=1,
    )
    assert len(added) == 1
    assert added[0].screen_name == "drop_alerts"


def test_confidence_floor_default_is_0_88(tmp_path: Path) -> None:
    """Regression: cold-start min_confidence must be 0.88, not the legacy
    0.7. Accounts at confidence=0.80 must be rejected by the default."""
    db = _make_db(tmp_path)
    added = discover_tcg_sns_accounts(
        sns_db=db,
        search_fn=lambda q, *, max_results: _result("low_conf_acct"),
        llm_fn=lambda p: _verdict(confidence=0.80, actionable=0.95),
        queries=("test",),
        max_new_per_run=1,
    )
    assert added == []
    assert DEFAULT_MIN_CONFIDENCE == pytest.approx(0.88)
    assert DEFAULT_ACTIONABLE_FLOOR == pytest.approx(0.75)


# ── feedback timeline ──────────────────────────────────────────────────────


def test_positive_feedback_writes_polarity_row(tmp_path: Path) -> None:
    db = _make_db(tmp_path)
    db.record_auto_discovery_feedback(
        screen_name="@drop_alerts",
        polarity="positive",
        domains=("pokemon", "tcg"),
        llm_confidence=0.95,
        llm_actionable_score=0.9,
        chat_id="12345",
    )
    summary = db.auto_discovery_feedback_summary()
    assert summary["positive_count"] == 1
    assert summary["negative_count"] == 0


def test_delete_writes_negative_feedback_row(tmp_path: Path) -> None:
    """Removing an auto-discovered rule (via delete_watch_rule) must record
    a polarity='negative' feedback row alongside the existing rejection
    entry. This is what feeds the per-domain trust tightening."""
    db = _make_db(tmp_path)
    added = discover_tcg_sns_accounts(
        sns_db=db,
        search_fn=lambda q, *, max_results: _result("noisy_account"),
        llm_fn=lambda p: _verdict(domains=("yugioh", "tcg")),
        queries=("test",),
        max_new_per_run=1,
    )
    assert len(added) == 1
    db.delete_watch_rule(added[0].rule_id)
    summary = db.auto_discovery_feedback_summary()
    assert summary["negative_count"] == 1
    assert summary["positive_count"] == 0


def test_polarity_value_validated(tmp_path: Path) -> None:
    """Unknown polarity values must raise, not silently pollute the table."""
    db = _make_db(tmp_path)
    with pytest.raises(ValueError):
        db.record_auto_discovery_feedback(
            screen_name="x",
            polarity="maybe",
            domains=("pokemon",),
        )


# ── per-domain trust learning loop ─────────────────────────────────────────


def test_rejection_tightens_per_domain_threshold(tmp_path: Path) -> None:
    """Each negative feedback bumps that domain's actionable_threshold by
    DISCOVERY_TIGHTENING_STEP. Threshold caps at DISCOVERY_MAX_THRESHOLD."""
    db = _make_db(tmp_path)
    base = db.effective_actionable_threshold(("yugioh",))
    assert base == pytest.approx(DEFAULT_ACTIONABLE_FLOOR)
    db.record_auto_discovery_feedback(
        screen_name="bad1", polarity="negative", domains=("yugioh",),
    )
    after_one = db.effective_actionable_threshold(("yugioh",))
    assert after_one == pytest.approx(base + 0.05)
    db.record_auto_discovery_feedback(
        screen_name="bad2", polarity="negative", domains=("yugioh",),
    )
    after_two = db.effective_actionable_threshold(("yugioh",))
    assert after_two == pytest.approx(base + 0.10)


def test_positive_feedback_does_not_lower_threshold(tmp_path: Path) -> None:
    """Positive feedback bumps keep_count but must NOT lower the threshold.
    The loop is intentionally monotonically-tightening — users wanted
    'fewer recommendations', so we never relax automatically."""
    db = _make_db(tmp_path)
    db.record_auto_discovery_feedback(
        screen_name="bad", polarity="negative", domains=("pokemon",),
    )
    tightened = db.effective_actionable_threshold(("pokemon",))
    for _ in range(5):
        db.record_auto_discovery_feedback(
            screen_name=f"good{_}", polarity="positive", domains=("pokemon",),
        )
    after_keeps = db.effective_actionable_threshold(("pokemon",))
    assert after_keeps == pytest.approx(tightened)


def test_threshold_caps_at_max(tmp_path: Path) -> None:
    """Threshold must not exceed DISCOVERY_MAX_THRESHOLD even after many
    rejections (otherwise the gate becomes mathematically unreachable and
    the system permanently locks the domain out)."""
    db = _make_db(tmp_path)
    # 20 rejections, each +0.05 → would naively reach 1.75. Should cap.
    for i in range(20):
        db.record_auto_discovery_feedback(
            screen_name=f"bad{i}", polarity="negative", domains=("ws",),
        )
    capped = db.effective_actionable_threshold(("ws",))
    assert capped <= 0.95 + 1e-9


def test_multi_domain_candidate_held_to_strictest_threshold(tmp_path: Path) -> None:
    """A candidate whose domains include one trusted and one tightened
    domain must clear the STRICTER threshold. Otherwise a noisy domain
    could be laundered through a clean co-domain."""
    db = _make_db(tmp_path)
    # Tighten 'crypto' (not a real domain, just illustrative)
    for _ in range(3):
        db.record_auto_discovery_feedback(
            screen_name=f"noisy{_}", polarity="negative", domains=("crypto",),
        )
    # 'pokemon' is untouched → at floor
    threshold = db.effective_actionable_threshold(("pokemon", "crypto"))
    crypto_only = db.effective_actionable_threshold(("crypto",))
    assert threshold == pytest.approx(crypto_only)
    assert threshold > DEFAULT_ACTIONABLE_FLOOR


def test_per_domain_threshold_blocks_account_after_rejections(tmp_path: Path) -> None:
    """End-to-end: reject enough pokemon accounts and the next pokemon
    candidate at the same actionable score is now rejected by the
    per-domain gate."""
    db = _make_db(tmp_path)
    # 4 rejections → threshold = 0.75 + 4×0.05 = 0.95 (capped)
    for i in range(4):
        db.record_auto_discovery_feedback(
            screen_name=f"bad{i}", polarity="negative", domains=("pokemon",),
        )
    # Candidate at 0.80 actionable would have passed at floor 0.75, but
    # the per-domain ratchet has pushed pokemon's bar to 0.95.
    added = discover_tcg_sns_accounts(
        sns_db=db,
        search_fn=lambda q, *, max_results: _result("borderline_acct"),
        llm_fn=lambda p: _verdict(actionable=0.80, domains=("pokemon",)),
        queries=("test",),
        max_new_per_run=1,
    )
    assert added == []
