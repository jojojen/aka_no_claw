"""Google Trends tracker for IP heat signals (C4).

Fetches relative search interest (0-100) for IP keywords from Google Trends
and records the result to IpHeatStore as source="google_trends".

The value stored is the *average daily interest* over the requested timeframe
(default: last 7 days, JP geo). Because Google Trends scores are relative
within each request, percentile comparisons over time are valid — a score of
80 always means "80% of peak interest seen in this 7-day window".
"""

from __future__ import annotations

import logging
from typing import Any

from .ip_heat_store import HeatSignal, IpHeatStore

logger = logging.getLogger(__name__)


def _build_trend_req(*, hl: str = "ja-JP", tz: int = 540, timeout: int | None = 15):
    """Instantiate a pytrends TrendReq. Kept here for easy monkeypatching in tests."""
    from pytrends.request import TrendReq  # lazy import so the module loads without pytrends installed
    return TrendReq(hl=hl, tz=tz, timeout=timeout)


def _fetch_average_interest(
    keywords: list[str],
    *,
    timeframe: str = "now 7-d",
    geo: str = "JP",
    hl: str = "ja-JP",
    tz: int = 540,
    timeout: int = 15,
) -> dict[str, float]:
    """Return {keyword: avg_interest} for each keyword (0-100).

    Returns an empty dict if the request fails or returns no data."""
    try:
        pytrends = _build_trend_req(hl=hl, tz=tz, timeout=timeout)
        pytrends.build_payload(keywords, timeframe=timeframe, geo=geo)
        df = pytrends.interest_over_time()
        if df is None or df.empty:
            logger.warning("google_trends_tracker: empty result for %s", keywords)
            return {}
        result: dict[str, float] = {}
        for kw in keywords:
            if kw in df.columns:
                result[kw] = float(df[kw].mean())
        return result
    except Exception as exc:
        logger.warning("google_trends_tracker: fetch failed for %s: %s", keywords, exc)
        return {}


class GoogleTrendsTracker:
    """Tracks Google Trends relative interest for IP keywords.

    Usage:
        tracker = GoogleTrendsTracker(heat_store)
        signal = tracker.track_ip(
            ip_canonical="chainsaw man",
            keywords=["チェンソーマン", "chainsawman"],
            geo="JP",
        )
    """

    def __init__(
        self,
        heat_store: IpHeatStore,
        *,
        geo: str = "JP",
        hl: str = "ja-JP",
        tz: int = 540,
        timeout_seconds: int = 15,
    ) -> None:
        self._store = heat_store
        self._geo = geo
        self._hl = hl
        self._tz = tz
        self._timeout = timeout_seconds

    def _fetch_interest(self, keywords: list[str]) -> dict[str, float]:
        """Overridable: returns {keyword: avg_interest} for the last 7 days."""
        return _fetch_average_interest(
            keywords,
            timeframe="now 7-d",
            geo=self._geo,
            hl=self._hl,
            tz=self._tz,
            timeout=self._timeout,
        )

    def get_interest(self, keyword: str) -> float | None:
        """Return average 7-day interest (0-100) for a single keyword, or None."""
        result = self._fetch_interest([keyword])
        return result.get(keyword)

    def track_ip(
        self,
        *,
        ip_canonical: str,
        keywords: list[str],
        geo: str | None = None,
    ) -> HeatSignal | None:
        """Fetch interest for all keywords, store the maximum as the heat signal.

        Using the max (not sum) because Google Trends keywords are redundant
        forms of the same IP (JP + EN); the best-matching one is the signal.
        Returns None if all fetches fail."""
        if geo is not None:
            old_geo = self._geo
            self._geo = geo

        try:
            interest_map = self._fetch_interest(keywords)
        finally:
            if geo is not None:
                self._geo = old_geo  # type: ignore[possibly-undefined]

        if not interest_map:
            logger.warning(
                "GoogleTrendsTracker.track_ip: no data for %r (tried %s)",
                ip_canonical, keywords,
            )
            return None

        value = max(interest_map.values())
        return self._store.record(
            ip_canonical=ip_canonical,
            source="google_trends",
            value=value,
            window_days=7,
        )
