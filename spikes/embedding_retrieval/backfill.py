"""Backfill KB embeddings for all existing rows using the production embedder.

Idempotent: INSERT OR REPLACE per row. Safe to re-run; the `embeddings` table is
additive (DROP TABLE embeddings to fully remove).

Run:
    .venv/bin/python spikes/embedding_retrieval/backfill.py
"""
from __future__ import annotations

import sys
import time

sys.path.insert(0, "src")

from openclaw_adapter.kb_embedder import OllamaEmbedder  # noqa: E402
from openclaw_adapter.knowledge_db import KnowledgeDatabase  # noqa: E402

DB = "/Users/jen/ai_work_space/related_to_claw/aka_no_claw/data/knowledge.sqlite3"
ENDPOINT = "http://127.0.0.1:11434"
MODEL = "bge-m3"


def main() -> None:
    emb = OllamaEmbedder(endpoint=ENDPOINT, model=MODEL, timeout=60)
    print(f"embedder: model={emb.model} dim={emb.dim}")
    db = KnowledgeDatabase(DB, embedder=emb)

    entries = db.recent_entries(limit=10_000)
    codegen = db.all_codegen_knowledge()
    print(f"entries={len(entries)} codegen={len(codegen)}")

    t0 = time.time()
    ok_e = 0
    for e in entries:
        db._reindex_entry(e.entity_canonical)
        ok_e += 1
    ok_c = 0
    for c in codegen:
        db._reindex_codegen(c.knowledge_id)
        ok_c += 1
    dt = time.time() - t0

    with db.connect() as conn:
        n_entry = conn.execute("SELECT COUNT(*) FROM embeddings WHERE kind='entry'").fetchone()[0]
        n_codegen = conn.execute("SELECT COUNT(*) FROM embeddings WHERE kind='codegen'").fetchone()[0]
    print(f"reindexed entry={ok_e} codegen={ok_c} in {dt:.1f}s")
    print(f"stored vectors: entry={n_entry} codegen={n_codegen}")


if __name__ == "__main__":
    main()
