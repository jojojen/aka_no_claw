"""Pure comparable-market evidence policy (R3.6)."""

from __future__ import annotations


def derive_active_price_cap(listed_price_jpy: int | None, sold_average_jpy: int | None = None) -> int:
    """Bound active listings without discarding high-value text-query evidence."""
    if listed_price_jpy is not None and listed_price_jpy > 0:
        return max(5_000, int(listed_price_jpy * 2.0))
    if sold_average_jpy is not None and sold_average_jpy > 0:
        return max(5_000, int(sold_average_jpy * 2.0))
    return 50_000
