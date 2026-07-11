"""Schema version stamping tests for aka_no_claw storage.

Verifies:
- OpportunityStore.bootstrap() stamps PRAGMA user_version=1 on fresh DB
- KnowledgeDatabase.bootstrap() stamps PRAGMA user_version=1 on fresh DB
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from openclaw_adapter.opportunity_store import OpportunityStore
from openclaw_adapter.opportunity_store import SCHEMA_VERSION as OPPORTUNITY_SCHEMA_VERSION
from openclaw_adapter.knowledge_db import KnowledgeDatabase
from openclaw_adapter.knowledge_db import SCHEMA_VERSION as KNOWLEDGE_SCHEMA_VERSION


def test_opportunity_store_bootstrap_stamps_version_on_fresh_db(tmp_path: Path) -> None:
    """Fresh opportunity DB should have PRAGMA user_version=1 after bootstrap."""
    store = OpportunityStore(tmp_path / "opportunity.db")
    store.bootstrap()
    with sqlite3.connect(store.path) as conn:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
    assert version == 1


def test_opportunity_store_bootstrap_idempotent(tmp_path: Path) -> None:
    """Calling bootstrap() twice should not downgrade version."""
    store = OpportunityStore(tmp_path / "opportunity_twice.db")
    store.bootstrap()
    with sqlite3.connect(store.path) as conn:
        version1 = conn.execute("PRAGMA user_version").fetchone()[0]

    store.bootstrap()  # second call
    with sqlite3.connect(store.path) as conn:
        version2 = conn.execute("PRAGMA user_version").fetchone()[0]

    assert version1 == 1
    assert version2 == 1


def test_opportunity_store_preserves_existing_version(tmp_path: Path) -> None:
    """If DB already has user_version set, bootstrap should not change it."""
    db_path = tmp_path / "opportunity_existing.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute("PRAGMA user_version = 1")

    store = OpportunityStore(db_path)
    store.bootstrap()

    with sqlite3.connect(store.path) as conn:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
    assert version == 1


def test_knowledge_database_bootstrap_stamps_version_on_fresh_db(tmp_path: Path) -> None:
    """Fresh knowledge DB should have PRAGMA user_version=1 after init."""
    db = KnowledgeDatabase(tmp_path / "knowledge.db")
    # KnowledgeDatabase calls bootstrap() in __init__, so version should already be set.
    with sqlite3.connect(db.path) as conn:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
    assert version == 1


def test_knowledge_database_bootstrap_idempotent(tmp_path: Path) -> None:
    """Creating KnowledgeDatabase twice should not downgrade version."""
    db_path = tmp_path / "knowledge_twice.db"
    db1 = KnowledgeDatabase(db_path)
    with sqlite3.connect(db1.path) as conn:
        version1 = conn.execute("PRAGMA user_version").fetchone()[0]

    db2 = KnowledgeDatabase(db_path)
    with sqlite3.connect(db2.path) as conn:
        version2 = conn.execute("PRAGMA user_version").fetchone()[0]

    assert version1 == 1
    assert version2 == 1


def test_opportunity_schema_version_constant() -> None:
    """OpportunityStore should export SCHEMA_VERSION=1."""
    assert OPPORTUNITY_SCHEMA_VERSION == 1


def test_knowledge_schema_version_constant() -> None:
    """KnowledgeDatabase should export SCHEMA_VERSION=1."""
    assert KNOWLEDGE_SCHEMA_VERSION == 1


def test_sns_llm_candidate_provider_logs_state_on_schema_mismatch(tmp_path: Path, caplog) -> None:
    """_read_recent_posts probes+logs the SNS DB state on OperationalError
    instead of a bare exception message (aka_no_claw#77 D2.3 follow-up)."""
    import logging

    from openclaw_adapter.opportunity_agent import SnsLlmCandidateProvider

    db_path = tmp_path / "broken_sns.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute("PRAGMA user_version = 1")
        conn.execute("CREATE TABLE dummy (id TEXT)")

    provider = SnsLlmCandidateProvider(
        db_path=db_path,
        endpoint="http://localhost:0",
        model="dummy",
        timeout_seconds=1,
        lookback_hours=24,
    )
    with caplog.at_level(logging.WARNING):
        posts = provider._read_recent_posts(limit=5)

    assert posts == []
    assert any("Opportunity SNS read failed" in rec.message for rec in caplog.records)
    assert any("state=" in rec.message for rec in caplog.records)
