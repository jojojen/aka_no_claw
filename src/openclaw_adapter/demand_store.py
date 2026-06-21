"""Demand Signal Feature Store & market-attention metrics (issue #17).

Normalizes the ecosystem's scattered demand evidence — SNS mentions/reposts
(sns_monitor_bot), reputation/marketplace attention (reputation_snapshot),
official announcements, future search/trend providers — into entity-centric
(#12) demand *features*, then exposes a structured demand *snapshot* (current
features + freshness + confidence + trend + burst/momentum) for the Fair Value
(#15) and Opportunity Scoring (#16) engines to consume.

Deterministic V1: no LLM sentiment, no forecasting. Features are numeric, carry
provenance, and feed an explainable ``DemandSignal`` so demand's contribution to
a score stays auditable. Burst/momentum are computed from the stored time series,
not improvised at query time.
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha1
from pathlib import Path
from typing import Iterator, Mapping, Sequence

from .entity_opportunity_score import DemandSignal

# ── Deliverable 3: demand metrics vocabulary ──────────────────────────────────
# Closed vocabulary of known metrics. Unknown names are still stored (provenance
# preserved) but don't contribute to the composite attention score.
DEMAND_METRICS: tuple[str, ...] = (
    "mention_count",
    "unique_authors",
    "repost_count",
    "engagement_score",
    "announcement_score",
    "search_interest",
    "creator_attention",
    "market_attention",
    "burst_score",
    "momentum_score",
)

# Reference scales for count-like metrics → normalized to [0,1] as value/scale.
# Metrics already on a 0–1 scale use 1.0. These are normalization priors (like
# domain trust priors), not entity recognition.
_FEATURE_SCALES: dict[str, float] = {
    "mention_count": 100.0,
    "unique_authors": 50.0,
    "repost_count": 200.0,
    "engagement_score": 1.0,
    "announcement_score": 1.0,
    "search_interest": 100.0,
    "creator_attention": 1.0,
    "market_attention": 1.0,
}

# Metrics that make up the composite market-attention score, with weights.
_ATTENTION_WEIGHTS: dict[str, float] = {
    "market_attention": 0.30,
    "mention_count": 0.20,
    "unique_authors": 0.15,
    "engagement_score": 0.15,
    "search_interest": 0.10,
    "announcement_score": 0.10,
}

# ── Deliverable 5: integration source mappings (documented, entity-keyed) ──────
# Where each feature is expected to originate. Linkage is always by #12 entity_id.
DEMAND_SOURCE_MAPPINGS: dict[str, tuple[str, ...]] = {
    "sns_monitor_bot": ("mention_count", "unique_authors", "repost_count",
                        "engagement_score", "burst_score", "momentum_score"),
    "reputation_snapshot": ("market_attention", "creator_attention"),
    "official_announcement": ("announcement_score",),
    "search_trends": ("search_interest",),
    "collectible_intelligence": ("market_attention",),
}

TREND_RISING = "rising"
TREND_FALLING = "falling"
TREND_FLAT = "flat"
TREND_UNKNOWN = "unknown"

DEFAULT_WINDOW_DAYS = 14
_BURST_RECENT_DAYS = 1.0
_BURST_BASELINE_DAYS = 7.0


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _utc_now_iso() -> str:
    return _utc_now().isoformat()


def _parse_iso(ts: str) -> datetime | None:
    try:
        dt = datetime.fromisoformat(ts)
    except (TypeError, ValueError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _normalize_ts(ts: str | None) -> str:
    dt = _parse_iso(ts) if ts else None
    return (dt or _utc_now()).astimezone(timezone.utc).isoformat()


def _clamp(value: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, float(value)))


def normalize_feature_name(value: object) -> str:
    if not isinstance(value, str):
        return "unknown"
    return value.strip().lower().replace(" ", "_").replace("-", "_") or "unknown"


def normalize_feature_value(name: str, value: float) -> float:
    """Map a raw feature value into [0,1] using its reference scale. Unknown
    metrics get a pass-through clamp so they're safe but uncomposited."""
    scale = _FEATURE_SCALES.get(name)
    if scale is None:
        return _clamp(value)
    return _clamp(float(value) / scale) if scale else _clamp(value)


# ── Deliverable 1: demand feature model ───────────────────────────────────────
@dataclass(frozen=True, slots=True)
class DemandFeature:
    feature_id: str
    entity_id: str
    observed_at: str
    feature_name: str
    feature_value: float
    confidence: float = 1.0
    source_id: str | None = None
    created_at: str | None = None


# ── Deliverable 2/4: demand snapshot ──────────────────────────────────────────
@dataclass(frozen=True, slots=True)
class DemandSnapshot:
    entity_id: str
    window_days: int
    features: Mapping[str, float]          # latest raw value per metric
    feature_count: int
    confidence: float
    freshness_seconds: float | None
    trend_direction: str
    burst_score: float                     # [0,1] sudden recent spike
    momentum_score: float                  # [-1,1] sustained acceleration
    market_attention: float                # [0,1] composite headline metric
    source_ids: tuple[str, ...] = ()

    @property
    def has_data(self) -> bool:
        return self.feature_count > 0


def build_feature_id(
    *, entity_id: str, feature_name: str, observed_at: str, source_id: str | None
) -> str:
    key = "|".join((
        entity_id.strip().lower(), feature_name.strip().lower(),
        observed_at.strip(), (source_id or "").strip().lower(),
    ))
    return "df_" + sha1(key.encode("utf-8")).hexdigest()[:16]


_SCHEMA = """
CREATE TABLE IF NOT EXISTS demand_features (
    feature_id    TEXT PRIMARY KEY,
    entity_id     TEXT NOT NULL,
    observed_at   TEXT NOT NULL,
    feature_name  TEXT NOT NULL,
    feature_value REAL NOT NULL,
    confidence    REAL NOT NULL DEFAULT 1.0,
    source_id     TEXT,
    created_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_demand_features_entity ON demand_features(entity_id);
CREATE INDEX IF NOT EXISTS idx_demand_features_entity_name_time
    ON demand_features(entity_id, feature_name, observed_at);
"""


class DemandFeatureStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.bootstrap()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def bootstrap(self) -> None:
        with self.connect() as conn:
            conn.executescript(_SCHEMA)

    def record_feature(
        self,
        *,
        entity_id: str,
        feature_name: str,
        feature_value: float,
        observed_at: str | None = None,
        confidence: float = 1.0,
        source_id: str | None = None,
    ) -> DemandFeature:
        """Append an immutable, entity-keyed demand feature (Deliverable 1).
        Re-recording the same (entity, metric, instant, source) is a no-op via
        the deterministic id, so re-harvesting stays idempotent."""
        eid = (entity_id or "").strip()
        if not eid:
            raise ValueError("record_feature requires entity_id")
        name = normalize_feature_name(feature_name)
        when = _normalize_ts(observed_at)
        fid = build_feature_id(entity_id=eid, feature_name=name, observed_at=when,
                               source_id=source_id)
        now = _utc_now_iso()
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO demand_features (
                    feature_id, entity_id, observed_at, feature_name,
                    feature_value, confidence, source_id, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(feature_id) DO NOTHING
                """,
                (fid, eid, when, name, float(feature_value),
                 _clamp(confidence), source_id, now),
            )
            row = conn.execute(
                "SELECT * FROM demand_features WHERE feature_id = ?", (fid,)
            ).fetchone()
        return _row_to_feature(row)

    def features_for(
        self,
        entity_id: str,
        *,
        feature_name: str | None = None,
        since: str | None = None,
        limit: int | None = None,
    ) -> list[DemandFeature]:
        clauses = ["entity_id = ?"]
        params: list = [(entity_id or "").strip()]
        if feature_name:
            clauses.append("feature_name = ?")
            params.append(normalize_feature_name(feature_name))
        if since:
            clauses.append("observed_at >= ?")
            params.append(_normalize_ts(since))
        sql = ("SELECT * FROM demand_features WHERE " + " AND ".join(clauses)
               + " ORDER BY observed_at DESC, feature_id")
        if limit is not None:
            sql += " LIMIT ?"
            params.append(int(limit))
        with self.connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [_row_to_feature(r) for r in rows]

    def get_demand_snapshot(
        self, entity_id: str, *, window_days: int = DEFAULT_WINDOW_DAYS
    ) -> DemandSnapshot:
        """Structured demand snapshot for an entity (Deliverable 2). Sparse-safe:
        an entity with no features returns an empty, zeroed snapshot rather than
        raising."""
        eid = (entity_id or "").strip()
        window_days = max(1, int(window_days))
        rows = self.features_for(eid)
        if not rows:
            return DemandSnapshot(
                entity_id=eid, window_days=window_days, features={}, feature_count=0,
                confidence=0.0, freshness_seconds=None, trend_direction=TREND_UNKNOWN,
                burst_score=0.0, momentum_score=0.0, market_attention=0.0, source_ids=(),
            )
        return compute_demand_snapshot(eid, rows, window_days=window_days)


# ── pure computation (unit-testable without a DB) ─────────────────────────────
def compute_demand_snapshot(
    entity_id: str, features: Sequence[DemandFeature], *, window_days: int = DEFAULT_WINDOW_DAYS
) -> DemandSnapshot:
    eid = (entity_id or "").strip()
    if not features:
        return DemandSnapshot(
            entity_id=eid, window_days=window_days, features={}, feature_count=0,
            confidence=0.0, freshness_seconds=None, trend_direction=TREND_UNKNOWN,
            burst_score=0.0, momentum_score=0.0, market_attention=0.0, source_ids=(),
        )

    # latest raw value per metric (features arrive newest-first from the store)
    latest: dict[str, float] = {}
    latest_ts: dict[str, str] = {}
    for f in sorted(features, key=lambda x: x.observed_at):
        latest[f.feature_name] = f.feature_value
        latest_ts[f.feature_name] = f.observed_at

    newest_ts = max(latest_ts.values())
    newest_dt = _parse_iso(newest_ts)
    freshness = (_utc_now() - newest_dt).total_seconds() if newest_dt else None

    # market-attention composite from known, normalized metrics present
    attention, used_weight = 0.0, 0.0
    for name, w in _ATTENTION_WEIGHTS.items():
        if name in latest:
            attention += w * normalize_feature_value(name, latest[name])
            used_weight += w
    market_attention = round(attention / used_weight, 4) if used_weight else 0.0

    # burst (recent spike) computed from the densest mention-like series available
    series_name = next(
        (m for m in ("mention_count", "engagement_score", "search_interest", "repost_count")
         if any(f.feature_name == m for f in features)),
        None,
    )
    series = [f for f in features if series_name and f.feature_name == series_name]
    burst = compute_burst_score(series)
    momentum = compute_momentum_score(series, window_days=window_days)

    # if a metric explicitly carries burst/momentum, prefer the larger evidence
    if "burst_score" in latest:
        burst = max(burst, _clamp(latest["burst_score"]))
    if "momentum_score" in latest:
        momentum = max(momentum, _clamp(latest["momentum_score"], -1.0, 1.0))

    trend = (
        TREND_RISING if momentum > 0.1 else
        TREND_FALLING if momentum < -0.1 else
        TREND_FLAT
    )

    confidence = round(
        _clamp(
            statistics_mean([f.confidence for f in features]) * _clamp(0.4 + 0.1 * len(latest))
        ),
        4,
    )
    source_ids = tuple(dict.fromkeys(f.source_id for f in features if f.source_id))

    return DemandSnapshot(
        entity_id=eid, window_days=window_days, features=dict(latest),
        feature_count=len(features), confidence=confidence, freshness_seconds=freshness,
        trend_direction=trend, burst_score=round(burst, 4),
        momentum_score=round(momentum, 4), market_attention=market_attention,
        source_ids=source_ids,
    )


def statistics_mean(values: Sequence[float]) -> float:
    vals = [float(v) for v in values]
    return sum(vals) / len(vals) if vals else 0.0


def compute_burst_score(series: Sequence[DemandFeature]) -> float:
    """Sudden-spike score ∈ [0,1] (Deliverable 4): recent (24h) activity vs the
    prior 7-day baseline, squashed. Zero baseline with recent activity → strong
    burst; sparse/no data → 0."""
    if not series:
        return 0.0
    now = _utc_now()
    recent, baseline = [], []
    for f in series:
        dt = _parse_iso(f.observed_at)
        if dt is None:
            continue
        age_days = (now - dt).total_seconds() / 86400.0
        if age_days <= _BURST_RECENT_DAYS:
            recent.append(f.feature_value)
        elif age_days <= _BURST_RECENT_DAYS + _BURST_BASELINE_DAYS:
            baseline.append(f.feature_value)
    if not recent:
        return 0.0
    recent_level = statistics_mean(recent)
    base_level = statistics_mean(baseline) if baseline else 0.0
    if base_level <= 0:
        return 1.0 if recent_level > 0 else 0.0
    ratio = recent_level / base_level
    # ratio 1 → 0, ratio 2 → 0.5, ratio →∞ → 1
    return _clamp(1.0 - 1.0 / max(1.0, ratio)) if ratio > 1 else 0.0


def compute_momentum_score(
    series: Sequence[DemandFeature], *, window_days: int = DEFAULT_WINDOW_DAYS
) -> float:
    """Sustained acceleration ∈ [-1,1] (Deliverable 4): relative change of the
    window's later half vs its earlier half. Positive ⇒ accelerating demand."""
    if len(series) < 2:
        return 0.0
    now = _utc_now()
    in_window = [
        f for f in series
        if (dt := _parse_iso(f.observed_at)) is not None
        and (now - dt).total_seconds() / 86400.0 <= window_days
    ]
    if len(in_window) < 2:
        return 0.0
    ordered = sorted(in_window, key=lambda x: x.observed_at)
    mid = len(ordered) // 2
    first = statistics_mean([f.feature_value for f in ordered[:mid]] or [0.0])
    second = statistics_mean([f.feature_value for f in ordered[mid:]] or [0.0])
    if first <= 0:
        return 1.0 if second > 0 else 0.0
    change = (second - first) / first
    return _clamp(change, -1.0, 1.0)


# ── Deliverable 6: opportunity integration ────────────────────────────────────
def snapshot_to_demand_signal(snapshot: DemandSnapshot) -> DemandSignal:
    """Adapt a demand snapshot into the ``DemandSignal`` the Opportunity Scoring
    Engine (#16) consumes. ``demand_score`` blends composite attention, burst,
    and positive momentum into [0,1]; reasons keep the contribution explainable."""
    attention = snapshot.market_attention
    burst = snapshot.burst_score
    momentum_pos = max(0.0, snapshot.momentum_score)
    demand_score = _clamp(0.5 * attention + 0.3 * burst + 0.2 * momentum_pos)

    reasons: list[str] = []
    if burst >= 0.5:
        reasons.append(f"demand burst (score {burst:.2f})")
    if snapshot.trend_direction == TREND_RISING:
        reasons.append("demand trending up")
    if attention >= 0.5:
        reasons.append(f"high market attention ({attention:.2f})")
    announcement = bool(snapshot.features.get("announcement_score", 0.0) >= 0.5)
    if announcement:
        reasons.append("official announcement attention")

    return DemandSignal(
        entity_id=snapshot.entity_id,
        demand_score=round(demand_score, 4),
        mention_growth=snapshot.momentum_score if "mention_count" in snapshot.features else None,
        burst_score=burst,
        official_announcement=announcement,
        search_growth=snapshot.features.get("search_interest"),
        reasons=tuple(reasons),
    )


def _row_to_feature(row: sqlite3.Row) -> DemandFeature:
    return DemandFeature(
        feature_id=row["feature_id"],
        entity_id=row["entity_id"],
        observed_at=row["observed_at"],
        feature_name=row["feature_name"],
        feature_value=row["feature_value"],
        confidence=row["confidence"] if row["confidence"] is not None else 1.0,
        source_id=row["source_id"],
        created_at=row["created_at"],
    )
