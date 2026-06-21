"""Supply & Scarcity Intelligence Layer (issue #18).

Collectible opportunities are strongest when demand rises while supply shrinks.
The rest of the roadmap models price (#13), liquidity (#14), fair value (#15),
opportunity (#16) and demand (#17) — but nothing reasons about *scarcity*:
inventory contraction, reprint/restock risk, EOL/retirement, population limits.

This layer normalizes scattered supply evidence — official-store stock,
marketplace listing counts, retailer restock notices, buyback pressure, official
reprint/EOL announcements — into entity-centric (#12) ``ScarcitySignal`` rows,
then exposes a structured ``SupplySnapshot`` (scarcity metrics + availability
trend + reprint/EOL status + confidence) and an explainable ``SupplySignal`` the
Opportunity Scoring Engine (#16) can consume.

Deterministic V1: no ML, no forecasting. Signals are numeric, carry provenance,
and the scarcity composite stays auditable. Availability trend and supply-shock
are computed from the stored time series, not improvised at query time.
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha1
from pathlib import Path
from typing import Iterator, Mapping, Sequence

from .entity_opportunity_score import SupplySignal

# ── Deliverable 2: scarcity metrics vocabulary ────────────────────────────────
# Closed vocabulary of known metrics. Unknown names are still stored (provenance
# preserved) but don't contribute to the composite scarcity score.
SCARCITY_METRICS: tuple[str, ...] = (
    "inventory_depth",
    "market_availability",
    "listing_count",
    "active_sellers",
    "reprint_risk",
    "restock_risk",
    "restock_probability",
    "availability_score",
    "supply_shock_score",
    "population_count",
    "population_score",
    "print_run_confidence",
    "supply_concentration",
    "buyback_pressure",
)

# Reference scales for count-like metrics → normalized to [0,1] as value/scale.
# Metrics already on a 0–1 scale use 1.0. These are normalization priors, not
# entity recognition.
_SCARCITY_SCALES: dict[str, float] = {
    "inventory_depth": 50.0,
    "listing_count": 100.0,
    "active_sellers": 50.0,
    "population_count": 1000.0,
    "market_availability": 1.0,
    "reprint_risk": 1.0,
    "restock_risk": 1.0,
    "restock_probability": 1.0,
    "availability_score": 1.0,
    "supply_shock_score": 1.0,
    "population_score": 1.0,
    "print_run_confidence": 1.0,
    "supply_concentration": 1.0,
    "buyback_pressure": 1.0,
}

# Metrics that make up the composite scarcity score, with weights. Some metrics
# are "inverse": a high value means *more* supply (less scarce), so their
# normalized value is flipped to a scarcity contribution = 1 - norm.
_SCARCITY_WEIGHTS: dict[str, float] = {
    "supply_shock_score": 0.25,   # direct: sudden contraction → scarce
    "availability_score": 0.20,   # inverse: widely available → not scarce
    "inventory_depth": 0.15,      # inverse: deep stock → not scarce
    "listing_count": 0.10,        # inverse: many listings → not scarce
    "active_sellers": 0.08,       # inverse: many sellers → not scarce
    "reprint_risk": 0.08,         # inverse: reprint likely → supply returns
    "restock_risk": 0.05,         # inverse: restock likely → supply returns
    "population_score": 0.05,     # inverse: large population → not scarce
    "supply_concentration": 0.04,  # direct: concentrated supply → fragile/scarce
}
_SCARCITY_INVERSE: frozenset[str] = frozenset({
    "availability_score", "market_availability", "inventory_depth",
    "listing_count", "active_sellers", "reprint_risk", "restock_risk",
    "restock_probability", "population_score", "population_count",
})

# ── Deliverable 4: documented ingestion source mappings (entity-keyed) ─────────
SUPPLY_SOURCE_MAPPINGS: dict[str, tuple[str, ...]] = {
    "official_store": ("inventory_depth", "availability_score", "restock_probability"),
    "marketplace": ("listing_count", "active_sellers", "supply_concentration"),
    "retailer_stock": ("inventory_depth", "availability_score", "supply_shock_score"),
    "buyback_signal": ("buyback_pressure",),
    "official_announcement": ("reprint_risk", "restock_risk"),
    "collectible_intelligence": ("population_count", "population_score", "print_run_confidence"),
}

# ── Deliverable 3: reprint / restock / EOL lifecycle vocabulary ────────────────
LIFECYCLE_REPRINT_ANNOUNCED = "reprint_announced"
LIFECYCLE_RESTOCK = "restock_event"
LIFECYCLE_PRODUCTION_ENDED = "production_ended"
LIFECYCLE_EOL = "eol"
LIFECYCLE_RETIRED = "retired"
LIFECYCLE_EVENTS: tuple[str, ...] = (
    LIFECYCLE_REPRINT_ANNOUNCED, LIFECYCLE_RESTOCK,
    LIFECYCLE_PRODUCTION_ENDED, LIFECYCLE_EOL, LIFECYCLE_RETIRED,
)

EOL_ACTIVE = "active"
EOL_ENDED = "ended"
EOL_RETIRED = "retired"
EOL_UNKNOWN = "unknown"

REPRINT_NONE = "none"
REPRINT_ANNOUNCED = "announced"
REPRINT_RESTOCKED = "restocked"

# availability trend directions (FALLING availability ⇒ scarcity tightening)
TREND_RISING = "rising"
TREND_FALLING = "falling"
TREND_FLAT = "flat"
TREND_UNKNOWN = "unknown"

DEFAULT_WINDOW_DAYS = 30
_SHOCK_RECENT_DAYS = 1.0
_SHOCK_BASELINE_DAYS = 7.0

# Series metrics, newest-first preference, for trend / shock / contraction.
_AVAILABILITY_SERIES = ("listing_count", "inventory_depth", "active_sellers",
                        "availability_score", "market_availability")


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


def normalize_scarcity_type(value: object) -> str:
    if not isinstance(value, str):
        return "unknown"
    return value.strip().lower().replace(" ", "_").replace("-", "_") or "unknown"


def normalize_scarcity_value(name: str, value: float) -> float:
    """Map a raw metric value into [0,1] using its reference scale. Unknown
    metrics get a pass-through clamp so they're safe but uncomposited."""
    scale = _SCARCITY_SCALES.get(name)
    if scale is None:
        return _clamp(value)
    return _clamp(float(value) / scale) if scale else _clamp(value)


def _scarcity_contribution(name: str, value: float) -> float:
    """A metric's contribution to scarcity in [0,1]: direct metrics pass through
    their normalized value; inverse metrics (where abundance is high) are flipped."""
    norm = normalize_scarcity_value(name, value)
    return 1.0 - norm if name in _SCARCITY_INVERSE else norm


# ── Deliverable 1: scarcity signal model ──────────────────────────────────────
@dataclass(frozen=True, slots=True)
class ScarcitySignal:
    signal_id: str
    entity_id: str
    observed_at: str
    scarcity_type: str
    value: float
    confidence: float = 1.0
    source_id: str | None = None
    created_at: str | None = None


# ── Deliverable 5: supply snapshot ────────────────────────────────────────────
@dataclass(frozen=True, slots=True)
class SupplySnapshot:
    entity_id: str
    window_days: int
    metrics: Mapping[str, float]          # latest raw value per metric
    signal_count: int
    confidence: float
    freshness_seconds: float | None
    scarcity_score: float                 # [0,1] composite headline (higher = scarcer)
    availability_trend: str               # FALLING availability ⇒ tightening supply
    inventory_contraction: float          # [0,1] how much stock has shrunk
    supply_shock_score: float             # [0,1] sudden supply collapse
    reprint_risk: float                   # [0,1] likelihood supply expands again
    reprint_status: str                   # none / announced / restocked
    eol_status: str                       # active / ended / retired / unknown
    source_ids: tuple[str, ...] = ()

    @property
    def has_data(self) -> bool:
        return self.signal_count > 0


def build_signal_id(
    *, entity_id: str, scarcity_type: str, observed_at: str, source_id: str | None
) -> str:
    key = "|".join((
        entity_id.strip().lower(), scarcity_type.strip().lower(),
        observed_at.strip(), (source_id or "").strip().lower(),
    ))
    return "ss_" + sha1(key.encode("utf-8")).hexdigest()[:16]


_SCHEMA = """
CREATE TABLE IF NOT EXISTS scarcity_signals (
    signal_id     TEXT PRIMARY KEY,
    entity_id     TEXT NOT NULL,
    observed_at   TEXT NOT NULL,
    scarcity_type TEXT NOT NULL,
    value         REAL NOT NULL,
    confidence    REAL NOT NULL DEFAULT 1.0,
    source_id     TEXT,
    created_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_scarcity_signals_entity ON scarcity_signals(entity_id);
CREATE INDEX IF NOT EXISTS idx_scarcity_signals_entity_type_time
    ON scarcity_signals(entity_id, scarcity_type, observed_at);
"""


class SupplyScarcityStore:
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

    def record_signal(
        self,
        *,
        entity_id: str,
        scarcity_type: str,
        value: float,
        observed_at: str | None = None,
        confidence: float = 1.0,
        source_id: str | None = None,
    ) -> ScarcitySignal:
        """Append an immutable, entity-keyed scarcity signal (Deliverable 1).
        Re-recording the same (entity, type, instant, source) is a no-op via the
        deterministic id, so re-harvesting stays idempotent."""
        eid = (entity_id or "").strip()
        if not eid:
            raise ValueError("record_signal requires entity_id")
        stype = normalize_scarcity_type(scarcity_type)
        when = _normalize_ts(observed_at)
        sid = build_signal_id(entity_id=eid, scarcity_type=stype, observed_at=when,
                              source_id=source_id)
        now = _utc_now_iso()
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO scarcity_signals (
                    signal_id, entity_id, observed_at, scarcity_type,
                    value, confidence, source_id, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(signal_id) DO NOTHING
                """,
                (sid, eid, when, stype, float(value),
                 _clamp(confidence), source_id, now),
            )
            row = conn.execute(
                "SELECT * FROM scarcity_signals WHERE signal_id = ?", (sid,)
            ).fetchone()
        return _row_to_signal(row)

    def record_lifecycle_event(
        self,
        *,
        entity_id: str,
        event: str,
        observed_at: str | None = None,
        confidence: float = 1.0,
        source_id: str | None = None,
    ) -> ScarcitySignal:
        """Record a structured reprint/restock/EOL event (Deliverable 3) as a
        scarcity signal whose ``scarcity_type`` is a reserved lifecycle name."""
        evt = normalize_scarcity_type(event)
        if evt not in LIFECYCLE_EVENTS:
            raise ValueError(f"unknown lifecycle event: {event!r}")
        return self.record_signal(
            entity_id=entity_id, scarcity_type=evt, value=1.0,
            observed_at=observed_at, confidence=confidence, source_id=source_id,
        )

    def signals_for(
        self,
        entity_id: str,
        *,
        scarcity_type: str | None = None,
        since: str | None = None,
        limit: int | None = None,
    ) -> list[ScarcitySignal]:
        clauses = ["entity_id = ?"]
        params: list = [(entity_id or "").strip()]
        if scarcity_type:
            clauses.append("scarcity_type = ?")
            params.append(normalize_scarcity_type(scarcity_type))
        if since:
            clauses.append("observed_at >= ?")
            params.append(_normalize_ts(since))
        sql = ("SELECT * FROM scarcity_signals WHERE " + " AND ".join(clauses)
               + " ORDER BY observed_at DESC, signal_id")
        if limit is not None:
            sql += " LIMIT ?"
            params.append(int(limit))
        with self.connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [_row_to_signal(r) for r in rows]

    def get_supply_snapshot(
        self, entity_id: str, *, window_days: int = DEFAULT_WINDOW_DAYS
    ) -> SupplySnapshot:
        """Structured supply snapshot for an entity (Deliverable 5). Sparse-safe:
        an entity with no signals returns an empty, zeroed snapshot rather than
        raising."""
        eid = (entity_id or "").strip()
        window_days = max(1, int(window_days))
        rows = self.signals_for(eid)
        if not rows:
            return _empty_snapshot(eid, window_days)
        return compute_supply_snapshot(eid, rows, window_days=window_days)


# ── pure computation (unit-testable without a DB) ─────────────────────────────
def _empty_snapshot(entity_id: str, window_days: int) -> SupplySnapshot:
    return SupplySnapshot(
        entity_id=entity_id, window_days=window_days, metrics={}, signal_count=0,
        confidence=0.0, freshness_seconds=None, scarcity_score=0.0,
        availability_trend=TREND_UNKNOWN, inventory_contraction=0.0,
        supply_shock_score=0.0, reprint_risk=0.0, reprint_status=REPRINT_NONE,
        eol_status=EOL_UNKNOWN, source_ids=(),
    )


def compute_supply_snapshot(
    entity_id: str, signals: Sequence[ScarcitySignal], *,
    window_days: int = DEFAULT_WINDOW_DAYS,
) -> SupplySnapshot:
    eid = (entity_id or "").strip()
    if not signals:
        return _empty_snapshot(eid, window_days)

    # latest raw value per metric (signals arrive newest-first from the store)
    latest: dict[str, float] = {}
    latest_ts: dict[str, str] = {}
    for s in sorted(signals, key=lambda x: x.observed_at):
        latest[s.scarcity_type] = s.value
        latest_ts[s.scarcity_type] = s.observed_at

    newest_ts = max(latest_ts.values())
    newest_dt = _parse_iso(newest_ts)
    freshness = (_utc_now() - newest_dt).total_seconds() if newest_dt else None

    reprint_status, eol_status = _lifecycle_status(latest)

    # availability series → trend, contraction, supply shock
    series_name = next((m for m in _AVAILABILITY_SERIES if m in latest), None)
    series = [s for s in signals if series_name and s.scarcity_type == series_name]
    inventory_contraction = compute_inventory_contraction(series, window_days=window_days)
    availability_trend = _availability_trend(series, window_days=window_days)
    supply_shock = compute_supply_shock_score(series)
    if "supply_shock_score" in latest:
        supply_shock = max(supply_shock, _clamp(latest["supply_shock_score"]))

    # composite scarcity from known, direction-aware metrics present
    scarcity, used_weight = 0.0, 0.0
    for name, w in _SCARCITY_WEIGHTS.items():
        if name in latest:
            scarcity += w * _scarcity_contribution(name, latest[name])
            used_weight += w
    scarcity = scarcity / used_weight if used_weight else 0.0
    # a sudden contraction is itself scarcity evidence even without other metrics
    scarcity = max(scarcity, 0.5 * supply_shock, 0.5 * inventory_contraction)

    # lifecycle impact (Deliverable 3): EOL tightens, reprint/restock loosens
    if eol_status == EOL_RETIRED:
        scarcity = max(scarcity, 0.6)
    elif eol_status == EOL_ENDED:
        scarcity = max(scarcity, 0.45)
    if reprint_status == REPRINT_RESTOCKED:
        scarcity = min(scarcity, 0.3)
    elif reprint_status == REPRINT_ANNOUNCED:
        scarcity *= 0.7
    scarcity = _clamp(scarcity)

    reprint_risk = _reprint_risk(latest, reprint_status)

    confidence = round(
        _clamp(
            statistics_mean([s.confidence for s in signals])
            * _clamp(0.4 + 0.1 * len(latest))
        ),
        4,
    )
    source_ids = tuple(dict.fromkeys(s.source_id for s in signals if s.source_id))

    return SupplySnapshot(
        entity_id=eid, window_days=window_days, metrics=dict(latest),
        signal_count=len(signals), confidence=confidence, freshness_seconds=freshness,
        scarcity_score=round(scarcity, 4), availability_trend=availability_trend,
        inventory_contraction=round(inventory_contraction, 4),
        supply_shock_score=round(supply_shock, 4), reprint_risk=round(reprint_risk, 4),
        reprint_status=reprint_status, eol_status=eol_status, source_ids=source_ids,
    )


def _lifecycle_status(latest: Mapping[str, float]) -> tuple[str, str]:
    if LIFECYCLE_RETIRED in latest:
        eol = EOL_RETIRED
    elif LIFECYCLE_PRODUCTION_ENDED in latest or LIFECYCLE_EOL in latest:
        eol = EOL_ENDED
    else:
        eol = EOL_UNKNOWN
    if LIFECYCLE_RESTOCK in latest:
        reprint = REPRINT_RESTOCKED
    elif LIFECYCLE_REPRINT_ANNOUNCED in latest:
        reprint = REPRINT_ANNOUNCED
    else:
        reprint = REPRINT_NONE
    return reprint, eol


def _reprint_risk(latest: Mapping[str, float], reprint_status: str) -> float:
    if "reprint_risk" in latest:
        return _clamp(latest["reprint_risk"])
    if reprint_status == REPRINT_RESTOCKED:
        return 0.8
    if reprint_status == REPRINT_ANNOUNCED:
        return 0.6
    return 0.0


def statistics_mean(values: Sequence[float]) -> float:
    vals = [float(v) for v in values]
    return sum(vals) / len(vals) if vals else 0.0


def compute_supply_shock_score(series: Sequence[ScarcitySignal]) -> float:
    """Sudden supply-collapse score ∈ [0,1] (Deliverable 4): recent (24h) supply
    level vs the prior 7-day baseline. A sharp drop ⇒ high shock; sparse/no data
    or rising supply ⇒ 0."""
    if not series:
        return 0.0
    now = _utc_now()
    recent, baseline = [], []
    for s in series:
        dt = _parse_iso(s.observed_at)
        if dt is None:
            continue
        age_days = (now - dt).total_seconds() / 86400.0
        if age_days <= _SHOCK_RECENT_DAYS:
            recent.append(s.value)
        elif age_days <= _SHOCK_RECENT_DAYS + _SHOCK_BASELINE_DAYS:
            baseline.append(s.value)
    if not recent or not baseline:
        return 0.0
    recent_level = statistics_mean(recent)
    base_level = statistics_mean(baseline)
    if base_level <= 0:
        return 0.0
    # supply dropped: drop fraction maps directly to shock
    return _clamp(1.0 - recent_level / base_level) if recent_level < base_level else 0.0


def compute_inventory_contraction(
    series: Sequence[ScarcitySignal], *, window_days: int = DEFAULT_WINDOW_DAYS
) -> float:
    """Inventory contraction ∈ [0,1] (Deliverable 4): relative shrinkage of the
    window's later half vs its earlier half. Positive ⇒ stock is disappearing."""
    if len(series) < 2:
        return 0.0
    now = _utc_now()
    in_window = [
        s for s in series
        if (dt := _parse_iso(s.observed_at)) is not None
        and (now - dt).total_seconds() / 86400.0 <= window_days
    ]
    if len(in_window) < 2:
        return 0.0
    ordered = sorted(in_window, key=lambda x: x.observed_at)
    mid = len(ordered) // 2
    first = statistics_mean([s.value for s in ordered[:mid]] or [0.0])
    second = statistics_mean([s.value for s in ordered[mid:]] or [0.0])
    if first <= 0:
        return 0.0
    return _clamp((first - second) / first) if second < first else 0.0


def _availability_trend(
    series: Sequence[ScarcitySignal], *, window_days: int = DEFAULT_WINDOW_DAYS
) -> str:
    if len(series) < 2:
        return TREND_UNKNOWN
    now = _utc_now()
    in_window = [
        s for s in series
        if (dt := _parse_iso(s.observed_at)) is not None
        and (now - dt).total_seconds() / 86400.0 <= window_days
    ]
    if len(in_window) < 2:
        return TREND_UNKNOWN
    ordered = sorted(in_window, key=lambda x: x.observed_at)
    mid = len(ordered) // 2
    first = statistics_mean([s.value for s in ordered[:mid]] or [0.0])
    second = statistics_mean([s.value for s in ordered[mid:]] or [0.0])
    if first <= 0:
        return TREND_RISING if second > 0 else TREND_FLAT
    change = (second - first) / first
    if change > 0.1:
        return TREND_RISING
    if change < -0.1:
        return TREND_FALLING
    return TREND_FLAT


# ── Deliverable 6: opportunity / forecast integration ─────────────────────────
def snapshot_to_supply_signal(snapshot: SupplySnapshot) -> SupplySignal:
    """Adapt a supply snapshot into the ``SupplySignal`` the Opportunity Scoring
    Engine (#16) consumes. ``scarcity_score`` ∈ [0,1] is the single value the
    scorer weights; reasons keep the contribution explainable."""
    reasons: list[str] = []
    if snapshot.eol_status == EOL_RETIRED:
        reasons.append("entity retired (no further supply)")
    elif snapshot.eol_status == EOL_ENDED:
        reasons.append("production ended")
    if snapshot.supply_shock_score >= 0.5:
        reasons.append(f"supply shock (score {snapshot.supply_shock_score:.2f})")
    if snapshot.availability_trend == TREND_FALLING:
        reasons.append("availability tightening")
    if snapshot.inventory_contraction >= 0.4:
        reasons.append(f"inventory contracting ({snapshot.inventory_contraction:.0%})")
    if snapshot.reprint_status == REPRINT_ANNOUNCED:
        reasons.append("reprint announced — supply may expand")
    elif snapshot.reprint_status == REPRINT_RESTOCKED:
        reasons.append("restocked — supply expanding")

    return SupplySignal(
        entity_id=snapshot.entity_id,
        scarcity_score=snapshot.scarcity_score,
        reprint_risk=snapshot.reprint_risk,
        eol=snapshot.eol_status in (EOL_ENDED, EOL_RETIRED),
        availability_trend=snapshot.availability_trend,
        reasons=tuple(reasons),
    )


def _row_to_signal(row: sqlite3.Row) -> ScarcitySignal:
    return ScarcitySignal(
        signal_id=row["signal_id"],
        entity_id=row["entity_id"],
        observed_at=row["observed_at"],
        scarcity_type=row["scarcity_type"],
        value=row["value"],
        confidence=row["confidence"] if row["confidence"] is not None else 1.0,
        source_id=row["source_id"],
        created_at=row["created_at"],
    )
