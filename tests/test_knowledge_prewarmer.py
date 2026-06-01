"""Unit tests for KnowledgePrewarmer — no network, no Ollama."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from openclaw_adapter.knowledge_prewarmer import KnowledgePrewarmer


def _make_prewarmer(research_fn, monitor_db_path="/nonexistent/monitor.sqlite3"):
    return KnowledgePrewarmer(
        research_fn=research_fn,
        monitor_db_path=monitor_db_path,
        interval_seconds=999999,
        initial_delay_seconds=0,
    )


class TestPrwarmOnce:
    def test_feeds_snkrdunk_names_to_research_fn(self, tmp_path):
        queued = []
        pw = _make_prewarmer(research_fn=lambda name: queued.append(name) or True)

        fake_product = MagicMock()
        fake_product.item_kind = "card"
        fake_product.title = "ピカチュウex SA"

        with patch(
            "openclaw_adapter.knowledge_prewarmer.KnowledgePrewarmer._fetch_snkrdunk_entities",
            return_value=["ピカチュウex SA", "リザードンex"],
        ), patch(
            "openclaw_adapter.knowledge_prewarmer.KnowledgePrewarmer._fetch_watchlist_queries",
            return_value=[],
        ):
            pw._prewarm_once()

        assert "ピカチュウex SA" in queued
        assert "リザードンex" in queued

    def test_feeds_watchlist_queries(self):
        queued = []
        pw = _make_prewarmer(research_fn=lambda name: queued.append(name) or True)

        with patch(
            "openclaw_adapter.knowledge_prewarmer.KnowledgePrewarmer._fetch_snkrdunk_entities",
            return_value=[],
        ), patch(
            "openclaw_adapter.knowledge_prewarmer.KnowledgePrewarmer._fetch_watchlist_queries",
            return_value=["シャイニートレジャー", "ポケモンカード151"],
        ):
            pw._prewarm_once()

        assert "シャイニートレジャー" in queued
        assert "ポケモンカード151" in queued

    def test_skips_when_research_fn_returns_false(self):
        # research_fn returns False (entity already in DB)
        call_count = [0]

        def research_fn(name):
            call_count[0] += 1
            return False

        pw = _make_prewarmer(research_fn=research_fn)
        with patch(
            "openclaw_adapter.knowledge_prewarmer.KnowledgePrewarmer._fetch_snkrdunk_entities",
            return_value=["ダイケンキ", "エンブオー"],
        ), patch(
            "openclaw_adapter.knowledge_prewarmer.KnowledgePrewarmer._fetch_watchlist_queries",
            return_value=[],
        ):
            pw._prewarm_once()

        assert call_count[0] == 2  # still called, but returned False (already in DB)

    def test_snkrdunk_filters_sealed_boxes(self):
        """Only 'card' kind products should be enqueued; sealed boxes skipped."""
        queued = []
        pw = _make_prewarmer(research_fn=lambda name: queued.append(name) or True)

        card = MagicMock(item_kind="card", title="リザードン")
        box = MagicMock(item_kind="sealed_box", title="拡張パック")

        with patch(
            "tcg_tracker.snkrdunk_ranking.iter_ranked_products",
            return_value=[card, box],
        ), patch(
            "openclaw_adapter.knowledge_prewarmer.KnowledgePrewarmer._fetch_watchlist_queries",
            return_value=[],
        ):
            pw._prewarm_once()

        assert "リザードン" in queued
        assert "拡張パック" not in queued

    def test_watchlist_missing_db_returns_empty(self, tmp_path):
        pw = _make_prewarmer(
            research_fn=lambda _: True,
            monitor_db_path=str(tmp_path / "nonexistent.sqlite3"),
        )
        result = pw._fetch_watchlist_queries()
        assert result == []

    def test_snkrdunk_iter_exception_returns_empty(self):
        """If iter_ranked_products raises for every game, method returns []."""
        pw = _make_prewarmer(research_fn=lambda _: True)
        with patch(
            "tcg_tracker.snkrdunk_ranking.iter_ranked_products",
            side_effect=Exception("network error"),
        ):
            result = pw._fetch_snkrdunk_entities()
        assert result == []
