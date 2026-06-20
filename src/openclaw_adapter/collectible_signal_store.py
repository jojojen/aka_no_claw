"""Persistence for CollectibleSignal records (issue #8, Deliverable 1).

A standalone SQLite store, deliberately separate from ``OpportunityStore`` so
the existing TCG opportunity pipeline is untouched. Signals are the generic
intelligence layer; candidates remain the (TCG-only) recommendation layer.

Mirrors the conventions in ``opportunity_store.py``: WAL journal, ``Row``
factory, a ``bootstrap()`` that creates schema, and a ``_json()`` helper with
``ensure_ascii=False, sort_keys=True`` for stable, human-readable JSON columns.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .collectible_signal import CollectibleSignal
from .opportunity_models import utc_now_iso

logger = logging.getLogger(__name__)


SCHEMA = """
CREATE TABLE IF NOT EXISTS collectible_signals (
    signal_id TEXT PRIMARY KEY,
    source_kind TEXT NOT NULL,
    collectible_domain TEXT NOT NULL,
    ip_canonical TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    entity_kind TEXT NOT NULL DEFAULT 'other',
    product_family TEXT,
    product_type TEXT NOT NULL DEFAULT 'other',
    official_code TEXT,
    release_window TEXT,
    retail_price_jpy INTEGER,
    source_urls_json TEXT NOT NULL DEFAULT '[]',
    confidence REAL NOT NULL DEFAULT 0.0,
    evidence_count INTEGER NOT NULL DEFAULT 0,
    actionability TEXT NOT NULL DEFAULT 'informational',
    block_reason TEXT,
    heat_score REAL NOT NULL DEFAULT 0.0,
    anchor_types_json TEXT NOT NULL DEFAULT '[]',
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_collectible_signals_domain
    ON collectible_signals(collectible_domain);
CREATE INDEX IF NOT EXISTS idx_collectible_signals_actionability
    ON collectible_signals(actionability);
"""


def _json(data: object) -> str:
    return json.dumps(data, ensure_ascii=False, sort_keys=True)


def _decode_json_list(value: object) -> tuple[str, ...]:
    if not value:
        return ()
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError, json.JSONDecodeError):
        return ()
    if not isinstance(parsed, list):
        return ()
    return tuple(str(item) for item in parsed if isinstance(item, str) and item.strip())


def _decode_json_map(value: object) -> dict:
    if not value:
        return {}
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


class CollectibleSignalStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA busy_timeout=5000")
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()

    def bootstrap(self) -> None:
        with self.connect() as connection:
            connection.executescript(SCHEMA)

    def upsert_signal(self, signal: CollectibleSignal) -> None:
        """Insert or update a signal by its derived id.

        ``heat_score``, ``evidence_count`` and ``confidence`` accumulate
        monotonically (MAX) across repeated observations of the same product
        so a later weaker echo never demotes accumulated evidence.
        """
        now = utc_now_iso()
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO collectible_signals (
                    signal_id, source_kind, collectible_domain, ip_canonical,
                    title, entity_kind, product_family, product_type,
                    official_code, release_window, retail_price_jpy,
                    source_urls_json, confidence, evidence_count, actionability,
                    block_reason, heat_score, anchor_types_json, metadata_json,
                    created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(signal_id) DO UPDATE SET
                    source_kind=excluded.source_kind,
                    collectible_domain=excluded.collectible_domain,
                    ip_canonical=excluded.ip_canonical,
                    title=excluded.title,
                    entity_kind=excluded.entity_kind,
                    product_family=excluded.product_family,
                    product_type=excluded.product_type,
                    official_code=COALESCE(excluded.official_code, collectible_signals.official_code),
                    release_window=COALESCE(excluded.release_window, collectible_signals.release_window),
                    retail_price_jpy=COALESCE(excluded.retail_price_jpy, collectible_signals.retail_price_jpy),
                    source_urls_json=excluded.source_urls_json,
                    confidence=MAX(collectible_signals.confidence, excluded.confidence),
                    evidence_count=MAX(collectible_signals.evidence_count, excluded.evidence_count),
                    actionability=excluded.actionability,
                    block_reason=excluded.block_reason,
                    heat_score=MAX(collectible_signals.heat_score, excluded.heat_score),
                    anchor_types_json=excluded.anchor_types_json,
                    metadata_json=excluded.metadata_json,
                    updated_at=excluded.updated_at
                """,
                (
                    signal.signal_id,
                    signal.source_kind,
                    signal.collectible_domain,
                    signal.ip_canonical,
                    signal.title,
                    signal.entity_kind,
                    signal.product_family,
                    signal.product_type,
                    signal.official_code,
                    signal.release_window,
                    signal.retail_price_jpy,
                    _json(list(signal.source_urls)),
                    signal.confidence,
                    signal.evidence_count,
                    signal.actionability,
                    signal.block_reason,
                    signal.heat_score,
                    _json(list(signal.anchor_types)),
                    _json(dict(signal.metadata)),
                    signal.created_at or now,
                    now,
                ),
            )

    def get_signal(self, signal_id: str) -> CollectibleSignal | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM collectible_signals WHERE signal_id = ?",
                (signal_id,),
            ).fetchone()
        return _signal_from_row(row) if row is not None else None

    def list_signals(
        self,
        *,
        collectible_domain: str | None = None,
        actionability: str | None = None,
        limit: int = 50,
    ) -> list[CollectibleSignal]:
        clauses: list[str] = []
        params: list[object] = []
        if collectible_domain is not None:
            clauses.append("collectible_domain = ?")
            params.append(collectible_domain)
        if actionability is not None:
            clauses.append("actionability = ?")
            params.append(actionability)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        params.append(limit)
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM collectible_signals"
                + where
                + " ORDER BY heat_score DESC, updated_at DESC LIMIT ?",
                params,
            ).fetchall()
        return [_signal_from_row(row) for row in rows]


def _signal_from_row(row: sqlite3.Row) -> CollectibleSignal:
    # Rows are already normalized at write time, so reconstruct the frozen
    # dataclass directly — this preserves the stored signal_id and created_at
    # rather than recomputing/overwriting them via make_signal().
    return CollectibleSignal(
        signal_id=row["signal_id"],
        source_kind=row["source_kind"],
        collectible_domain=row["collectible_domain"],
        ip_canonical=row["ip_canonical"],
        title=row["title"] or "",
        entity_kind=row["entity_kind"] or "other",
        product_family=row["product_family"],
        product_type=row["product_type"] or "other",
        official_code=row["official_code"],
        release_window=row["release_window"],
        retail_price_jpy=row["retail_price_jpy"],
        source_urls=_decode_json_list(row["source_urls_json"]),
        confidence=row["confidence"] or 0.0,
        evidence_count=row["evidence_count"] or 0,
        actionability=row["actionability"] or "informational",
        block_reason=row["block_reason"],
        heat_score=row["heat_score"] or 0.0,
        anchor_types=_decode_json_list(row["anchor_types_json"]),
        metadata=_decode_json_map(row["metadata_json"]),
        created_at=row["created_at"],
    )
