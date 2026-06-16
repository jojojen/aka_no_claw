"""Embedding write-hooks + semantic fallback for KnowledgeDatabase.

Uses a deterministic in-process fake embedder (a tiny bag-of-chars vector), so
these tests never touch Ollama. The point is the *wiring* — that writes index,
deletes purge, failures are swallowed, and consumers fall back semantically —
not the quality of any real model (that's the env-gated live spike).
"""
from __future__ import annotations

import pytest

from openclaw_adapter.knowledge_db import KnowledgeDatabase


class FakeEmbedder:
    """Maps text → a fixed-dim vector by hashing characters into buckets.
    Deterministic and cheap; similar strings get similar vectors."""

    model = "fake-embed"
    dim = 32

    def __call__(self, text: str) -> list[float] | None:
        vec = [0.0] * self.dim
        for ch in (text or ""):
            vec[ord(ch) % self.dim] += 1.0
        if not any(vec):
            vec[0] = 1.0
        return vec


class BrokenEmbedder:
    model = "broken"
    dim = 8

    def __call__(self, text: str) -> list[float] | None:
        raise RuntimeError("simulated embed outage")


class NoneEmbedder:
    model = "noner"
    dim = 8

    def __call__(self, text: str) -> list[float] | None:
        return None


@pytest.fixture
def db(tmp_path):
    return KnowledgeDatabase(tmp_path / "kb.sqlite3", embedder=FakeEmbedder())


def _emb_rows(db, kind):
    with db.connect() as conn:
        return conn.execute(
            "SELECT ref_id, model, dim FROM embeddings WHERE kind = ?", (kind,)
        ).fetchall()


# ── write hooks ──────────────────────────────────────────────────────────────


def test_upsert_entry_indexes_embedding(db):
    db.upsert_entry(entity_canonical="pjsk", entity_type="ip", summary="プロセカ rhythm game")
    rows = _emb_rows(db, "entry")
    assert len(rows) == 1
    assert rows[0]["ref_id"] == "pjsk"
    assert rows[0]["model"] == "fake-embed"
    assert rows[0]["dim"] == 32


def test_add_alias_reindexes_entry(db):
    db.upsert_entry(entity_canonical="pjsk", entity_type="ip", summary="x")
    with db.connect() as conn:
        before = conn.execute(
            "SELECT updated_at, vec FROM embeddings WHERE kind='entry' AND ref_id='pjsk'"
        ).fetchone()
    assert db.add_alias("プロセカ", "pjsk") is True
    with db.connect() as conn:
        after = conn.execute(
            "SELECT vec FROM embeddings WHERE kind='entry' AND ref_id='pjsk'"
        ).fetchone()
    # Alias is part of the index text, so the vector must change.
    assert before["vec"] != after["vec"]


def test_delete_entry_purges_embedding(db):
    entry = db.upsert_entry(entity_canonical="pjsk", entity_type="ip", summary="x")
    assert _emb_rows(db, "entry")
    assert db.delete_entry(entry.entry_id) is True
    assert _emb_rows(db, "entry") == []


def test_upsert_codegen_indexes_and_delete_purges(db):
    row = db.upsert_codegen_knowledge(
        category="http", title="retry with backoff", technique="sleep 2**n", keywords=("retry",)
    )
    assert len(_emb_rows(db, "codegen")) == 1
    assert db.delete_codegen(row.knowledge_id) is True
    assert _emb_rows(db, "codegen") == []


# ── best-effort failure policy ───────────────────────────────────────────────


def test_embedder_exception_does_not_break_upsert(tmp_path):
    db = KnowledgeDatabase(tmp_path / "kb.sqlite3", embedder=BrokenEmbedder())
    entry = db.upsert_entry(entity_canonical="pjsk", entity_type="ip", summary="x")
    assert entry.entity_canonical == "pjsk"  # write succeeded
    assert _emb_rows(db, "entry") == []  # no vector stored


def test_embedder_returning_none_skips_index(tmp_path):
    db = KnowledgeDatabase(tmp_path / "kb.sqlite3", embedder=NoneEmbedder())
    db.upsert_entry(entity_canonical="pjsk", entity_type="ip", summary="x")
    assert _emb_rows(db, "entry") == []


def test_no_embedder_means_no_table_writes(tmp_path):
    db = KnowledgeDatabase(tmp_path / "kb.sqlite3")  # embedder=None
    db.upsert_entry(entity_canonical="pjsk", entity_type="ip", summary="x")
    assert _emb_rows(db, "entry") == []
    assert db.search_semantic("entry", "anything", 3) == []


# ── search_semantic ──────────────────────────────────────────────────────────


def test_search_semantic_ranks_by_similarity(db):
    db.upsert_entry(entity_canonical="aaaa", entity_type="other", summary="aaaa")
    db.upsert_entry(entity_canonical="zzzz", entity_type="other", summary="zzzz")
    ranked = db.search_semantic("entry", "aaaa", 2)
    assert ranked[0][0] == "aaaa"  # closest by bag-of-chars


class OtherModelEmbedder(FakeEmbedder):
    model = "other-model"  # same dim, different model id


def test_search_semantic_ignores_model_mismatch(tmp_path):
    db = KnowledgeDatabase(tmp_path / "kb.sqlite3", embedder=FakeEmbedder())
    db.upsert_entry(entity_canonical="pjsk", entity_type="ip", summary="x")
    # Swap to a different model id → stored vectors are now "stale" and ignored
    # until backfill re-runs under the new model.
    db._embedder = OtherModelEmbedder()
    assert db.search_semantic("entry", "pjsk", 3) == []
