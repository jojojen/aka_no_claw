"""Cross-signal aggregator: IP heat × TCG announcement candidate intersection (C6).

Identifies IPs that are simultaneously:
  A) Hot: ip_heat_signals percentile ≥ min_percentile (any source)
  B) Active: have at least one recent opportunity candidate in the pipeline

When both signals fire, the IP is flagged as a 🔥 dual-signal target and the
classifier gets a boosted heat block.

Usage:
    aggregator = CrossSignalAggregator(heat_store, candidate_finder=store.find_by_ip)
    for dual in aggregator.find_dual_signals(min_percentile=70.0):
        print(f"🔥 {dual.ip_canonical}: heat={dual.max_percentile:.0f}%")
        for cand in dual.candidates:
            print(f"  - {cand.title}")

The `build_heat_block_for_entities` utility formats heat data as a text block
suitable for injection into the SNS classifier prompt (C5 integration point).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Callable, Sequence

from .ip_heat_store import IpHeatStore

logger = logging.getLogger(__name__)

DUAL_SIGNAL_MIN_PERCENTILE: float = 70.0


@dataclass(frozen=True)
class DualSignal:
    """An IP with both high heat AND active pipeline candidates."""
    ip_canonical: str
    max_percentile: float               # highest percentile across all sources
    source_percentiles: dict[str, float] = field(default_factory=dict)
    candidates: tuple = field(default_factory=tuple)  # list[OpportunityCandidate]


class CrossSignalAggregator:
    """Intersects IP heat data with opportunity pipeline candidates.

    Args:
        heat_store: IpHeatStore instance.
        candidate_finder: callable(ip_canonical: str) → list of candidates.
            Any object with a `.title` attribute works.
            If not provided, heat-only results are still returned (candidates=[]).
    """

    def __init__(
        self,
        heat_store: IpHeatStore,
        *,
        candidate_finder: Callable[[str], Sequence] | None = None,
        min_candidates: int = 1,
    ) -> None:
        self._store = heat_store
        self._finder = candidate_finder
        self._min_candidates = min_candidates

    def find_dual_signals(
        self,
        *,
        min_percentile: float = DUAL_SIGNAL_MIN_PERCENTILE,
        limit: int = 20,
    ) -> list[DualSignal]:
        """Return DualSignal objects for IPs with heat ≥ min_percentile AND
        at least min_candidates matching pipeline candidates."""
        hot_ips = self._store.top_hot_ips(min_percentile=min_percentile, limit=limit)
        results: list[DualSignal] = []
        for ip_canonical, max_pct in hot_ips:
            source_percentiles = self._get_source_percentiles(ip_canonical)
            candidates: Sequence = []
            if self._finder is not None:
                try:
                    candidates = self._finder(ip_canonical) or []
                except Exception:
                    logger.exception("CrossSignalAggregator: candidate_finder failed for %r", ip_canonical)
                    candidates = []
            if self._finder is None or len(candidates) >= self._min_candidates:
                results.append(DualSignal(
                    ip_canonical=ip_canonical,
                    max_percentile=max_pct,
                    source_percentiles=source_percentiles,
                    candidates=tuple(candidates),
                ))
        return results

    def check_ip(
        self,
        ip_canonical: str,
        *,
        min_percentile: float = DUAL_SIGNAL_MIN_PERCENTILE,
    ) -> DualSignal | None:
        """Check a single IP for dual-signal status. Returns None if not hot."""
        pct = self._store.max_percentile_for_ip(ip_canonical)
        if pct is None or pct < min_percentile:
            return None
        source_percentiles = self._get_source_percentiles(ip_canonical)
        candidates: Sequence = []
        if self._finder is not None:
            try:
                candidates = self._finder(ip_canonical) or []
            except Exception:
                logger.exception("CrossSignalAggregator.check_ip: finder failed for %r", ip_canonical)
        return DualSignal(
            ip_canonical=ip_canonical,
            max_percentile=pct,
            source_percentiles=source_percentiles,
            candidates=tuple(candidates),
        )

    def _get_source_percentiles(self, ip_canonical: str) -> dict[str, float]:
        signals = self._store.latest_for_ip(ip_canonical)
        return {s.source: s.percentile for s in signals if s.percentile is not None}


# ── Heat block formatter for SNS classifier (C5 integration) ───────────────


def build_heat_block_for_entities(
    entities: tuple[str, ...] | list[str],
    heat_store: IpHeatStore,
    *,
    min_percentile: float = 0.0,
) -> str:
    """Format ip_heat_signals data as a prompt block for the SNS classifier.

    Returns empty string if no heat data is available.

    Example output:
        - チェンソーマン: x_mention percentile=87, google_trends percentile=92
        - 鬼滅の刃: reddit percentile=65
    """
    lines: list[str] = []
    for entity in entities:
        pct_map = {}
        signals = heat_store.latest_for_ip(entity)
        for sig in signals:
            if sig.percentile is not None and sig.percentile >= min_percentile:
                pct_map[sig.source] = sig.percentile
        if not pct_map:
            continue
        parts = ", ".join(f"{src} percentile={pct:.0f}" for src, pct in sorted(pct_map.items()))
        max_pct = max(pct_map.values())
        badge = " 🔥" if max_pct >= 80 else (" 🌡" if max_pct >= 60 else "")
        lines.append(f"- {entity}: {parts}{badge}")
    return "\n".join(lines)
