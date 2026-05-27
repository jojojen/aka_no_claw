"""Tests for SNS auto-discovery rejection feedback loop.

When a user deletes an auto-discovered account, the system should:
1. Record the deletion in sns_auto_discovery_rejects
2. Not re-add the handle in the next discovery pass
3. Expose stats on survival rate
"""
from __future__ import annotations

from pathlib import Path

import pytest

from openclaw_adapter.opportunity_sns_discovery import discover_tcg_sns_accounts
from openclaw_adapter.web_search import WebSearchResult
from sns_monitor.storage import SnsDatabase


def _make_db(tmp_path: Path) -> SnsDatabase:
    db = SnsDatabase(tmp_path / "sns.sqlite3")
    db.bootstrap()
    return db


def _one_result(handle: str) -> list[WebSearchResult]:
    return [WebSearchResult(title="t", url=f"https://twitter.com/{handle}", snippet="")]


def _tcg_verdict(domains: list[str] = None, *, actionable: float = 0.9) -> str:
    """Default verdict for tests — passes both the legacy is_tcg/confidence
    gate AND the new actionable_for_investing gate. Override ``actionable``
    in tests that exercise the second-gate behaviour."""
    d = domains or ["yugioh", "tcg"]
    import json
    return json.dumps({
        "is_tcg": True,
        "domains": d,
        "confidence": 0.95,
        "actionable_for_investing": actionable,
        "reason": "ok",
    })


# ── sns_auto_discovery_rejects table ──────────────────────────────────────────


def test_delete_auto_discovery_rule_records_rejection(tmp_path: Path) -> None:
    """Deleting an auto-discovered rule writes to sns_auto_discovery_rejects."""
    db = _make_db(tmp_path)

    added = discover_tcg_sns_accounts(
        sns_db=db,
        search_fn=lambda q, *, max_results: _one_result("lance_tcgshop"),
        llm_fn=lambda p: _tcg_verdict(["yugioh", "tcg"]),
        queries=("test",),
        max_new_per_run=1,
    )
    assert len(added) == 1
    rule_id = added[0].rule_id

    db.delete_watch_rule(rule_id)

    rejected = db.list_rejected_handles()
    assert "lance_tcgshop" in rejected


def test_delete_manual_rule_does_not_record_rejection(tmp_path: Path) -> None:
    """Manually-added rules (source != 'auto_discovery') do not go into rejects."""
    from sns_monitor.models import AccountWatch
    db = _make_db(tmp_path)

    rule_id = SnsDatabase._watch_rule_id("account", "manual_account")
    rule = AccountWatch(
        rule_id=rule_id,
        screen_name="manual_account",
        user_id=None,
        label="@manual_account",
        include_keywords=(),
        domains=("tcg",),
        enabled=True,
        schedule_minutes=15,
        chat_id="",
        last_checked_at=None,
        source="x",  # manually added
    )
    db.save_watch_rule(rule)
    db.delete_watch_rule(rule_id)

    rejected = db.list_rejected_handles()
    assert "manual_account" not in rejected


# ── discovery respects rejection list ─────────────────────────────────────────


def test_discovery_skips_recently_rejected_handle(tmp_path: Path) -> None:
    """Auto-discovery must not re-add a handle deleted within the last 90 days."""
    db = _make_db(tmp_path)

    # First run: add the account
    added = discover_tcg_sns_accounts(
        sns_db=db,
        search_fn=lambda q, *, max_results: _one_result("lance_tcgshop"),
        llm_fn=lambda p: _tcg_verdict(),
        queries=("test",),
        max_new_per_run=1,
    )
    assert len(added) == 1

    # User deletes it
    db.delete_watch_rule(added[0].rule_id)
    assert "lance_tcgshop" in db.list_rejected_handles()

    # Second run: same handle appears in search results — must be skipped
    added2 = discover_tcg_sns_accounts(
        sns_db=db,
        search_fn=lambda q, *, max_results: _one_result("lance_tcgshop"),
        llm_fn=lambda p: _tcg_verdict(),
        queries=("test",),
        max_new_per_run=1,
    )
    assert added2 == []


def test_discovery_still_adds_other_handles_when_one_is_rejected(tmp_path: Path) -> None:
    """Rejection of one handle should not block discovery of others."""
    db = _make_db(tmp_path)

    # Seed a recent rejection directly (today-1 day → definitely within 90d window)
    from datetime import datetime, timedelta, timezone
    recent_date = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO sns_auto_discovery_rejects (screen_name, original_rule_id, deleted_at) "
            "VALUES ('bad_account', 'old_rule', ?)",
            (recent_date,),
        )
        conn.commit()

    results = [
        WebSearchResult(title="t", url="https://twitter.com/bad_account", snippet=""),
        WebSearchResult(title="t", url="https://twitter.com/good_account", snippet=""),
    ]
    verdicts = iter([_tcg_verdict(), _tcg_verdict()])

    added = discover_tcg_sns_accounts(
        sns_db=db,
        search_fn=lambda q, *, max_results: results,
        llm_fn=lambda p: next(verdicts),
        queries=("test",),
        max_new_per_run=2,
    )
    handles = [r.screen_name for r in added]
    assert "bad_account" not in handles
    assert "good_account" in handles


# ── auto_discovery_stats ───────────────────────────────────────────────────────


def test_auto_discovery_stats_survive_rate(tmp_path: Path) -> None:
    """Stats should track adds vs deletes accurately."""
    db = _make_db(tmp_path)

    # Add two accounts
    for handle in ("acc_a", "acc_b"):
        added = discover_tcg_sns_accounts(
            sns_db=db,
            search_fn=lambda q, *, max_results, h=handle: _one_result(h),
            llm_fn=lambda p: _tcg_verdict(),
            queries=("test",),
            max_new_per_run=1,
        )
        assert len(added) == 1

    # Delete one
    rules = [r for r in db.list_watch_rules() if hasattr(r, "screen_name") and r.screen_name == "acc_a"]
    assert rules
    db.delete_watch_rule(rules[0].rule_id)

    stats = db.auto_discovery_stats()
    assert stats["total_rejected"] == 1
    assert stats["total_added"] == 1          # acc_b still active
    assert stats["survive_count"] == 1
    assert stats["survive_rate"] == pytest.approx(0.5)


def test_auto_discovery_stats_empty_db(tmp_path: Path) -> None:
    db = _make_db(tmp_path)
    stats = db.auto_discovery_stats()
    assert stats["survive_rate"] == pytest.approx(1.0)
    assert stats["total_added"] == 0
    assert stats["total_rejected"] == 0


# ── list_rejected_handles respects days window ────────────────────────────────


def test_list_rejected_handles_respects_days_window(tmp_path: Path) -> None:
    """Handles deleted long ago should not block re-add after the window expires."""
    db = _make_db(tmp_path)
    # Insert a very old deletion
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO sns_auto_discovery_rejects (screen_name, original_rule_id, deleted_at) "
            "VALUES ('old_handle', 'r1', '2020-01-01T00:00:00+00:00')"
        )
        conn.commit()

    # days=90 window — 2020 deletion is outside it, so NOT returned
    recent = db.list_rejected_handles(days=90)
    assert "old_handle" not in recent

    # days=99999 — covers all time
    all_time = db.list_rejected_handles(days=99999)
    assert "old_handle" in all_time


# ── auto-added rules: source reflects platform, is_auto_discovered flags provenance


def test_auto_added_rule_is_attributed_to_x_with_provenance_flag(tmp_path: Path) -> None:
    """Auto-discovery from twitter.com / x.com URLs sets ``source='x'`` so the
    SnsMonitor runtime can dispatch to the X plugin. The ``is_auto_discovered``
    flag carries the provenance information that was previously conflated
    into ``source='auto_discovery'`` — a sentinel value the monitor's source
    registry couldn't dispatch to."""
    db = _make_db(tmp_path)

    added = discover_tcg_sns_accounts(
        sns_db=db,
        search_fn=lambda q, *, max_results: _one_result("poke_shop"),
        llm_fn=lambda p: _tcg_verdict(["pokemon", "tcg"]),
        queries=("test",),
        max_new_per_run=1,
    )
    assert len(added) == 1
    assert added[0].source == "x"
    assert added[0].is_auto_discovered is True

    # Check DB round-trip
    fetched = db.get_watch_rule(added[0].rule_id)
    assert fetched is not None
    assert getattr(fetched, "source", None) == "x"
    assert getattr(fetched, "is_auto_discovered", None) is True


def test_legacy_auto_discovery_rule_is_migrated_on_bootstrap(tmp_path: Path) -> None:
    """A pre-migration row with source='auto_discovery' must be rewritten by
    bootstrap to source='x' / is_auto_discovered=1 so the monitor runtime
    can finally start polling it. Otherwise the rule sits in the table
    forever, skipped on every poll cycle (the live-DB regression that
    motivated the split)."""
    import sqlite3
    db_path = tmp_path / "legacy.sqlite3"
    legacy = sqlite3.connect(db_path)
    legacy.executescript(
        """
        CREATE TABLE watch_rules (
            rule_id      TEXT PRIMARY KEY,
            kind         TEXT NOT NULL,
            label        TEXT NOT NULL,
            query_json   TEXT NOT NULL,
            enabled      INTEGER NOT NULL DEFAULT 1,
            schedule_minutes INTEGER NOT NULL,
            chat_id      TEXT NOT NULL,
            last_checked_at TEXT,
            created_at   TEXT NOT NULL,
            updated_at   TEXT NOT NULL,
            source       TEXT NOT NULL DEFAULT 'x'
        );
        INSERT INTO watch_rules
            (rule_id, kind, label, query_json, enabled, schedule_minutes,
             chat_id, last_checked_at, created_at, updated_at, source)
        VALUES
            ('legacy-1', 'account', '@legacy_handle',
             '{"screen_name": "legacy_handle", "domains": ["pokemon"]}',
             1, 15, '', NULL,
             '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00',
             'auto_discovery');
        """
    )
    legacy.commit()
    legacy.close()

    SnsDatabase(db_path).bootstrap()

    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT source, is_auto_discovered FROM watch_rules WHERE rule_id = 'legacy-1'"
        ).fetchone()
    assert row["source"] == "x"
    assert row["is_auto_discovered"] == 1
