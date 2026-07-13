"""Pure comparable-market evidence policy (R3.6)."""

from __future__ import annotations

from collections.abc import Callable

from .models import ResearchJobContext, ResearchSectionResult


def derive_active_price_cap(listed_price_jpy: int | None, sold_average_jpy: int | None = None) -> int:
    """Bound active listings without discarding high-value text-query evidence."""
    if listed_price_jpy is not None and listed_price_jpy > 0:
        return max(5_000, int(listed_price_jpy * 2.0))
    if sold_average_jpy is not None and sold_average_jpy > 0:
        return max(5_000, int(sold_average_jpy * 2.0))
    return 50_000


def build_liquidity_stage(
    ctx: ResearchJobContext,
    *,
    query_for: Callable[[ResearchJobContext], str],
    build_result: Callable[..., ResearchSectionResult],
) -> str:
    """Build liquidity evidence from the same comparable set as price (R3.6)."""
    result = build_result(
        query=query_for(ctx),
        active_evidence=ctx.active_price_evidence,
        sold_evidence=ctx.sold_price_evidence,
        sold_average_jpy=ctx.sold_average_jpy,
    )
    ctx.add_section_result(result)
    return result.summary
