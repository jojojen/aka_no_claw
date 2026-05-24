"""Three-segment collab notification formatter (D5).

Assembles the final 🔥 dual-signal notification that combines:
  Segment 1 — 事前訊號: IP heat percentile + announcement context
  Segment 2 — 📊 歴史推理: CollabInference stats from CollabSimilarityProvider
  Segment 3 — 🎫 抽選 / 予約管道: official store listings from B6

Example output:
  🔥 熱度 × 公告雙重訊號 — チェンソーマン × UNION ARENA

  📈 長期 95 / ⚡ 立即 88

  【事前訊號】
    - Bushiroad 公告 UA エクストラブースター チェンソーマン
    - IP 熱度 percentile=87 🔥（x_mention, reddit）

  【📊 歴史推理】（類似 7 件）
    平均 180 日利益 +42.0%、勝率 6/7
    最良 +180.0% / 最悪 -12.0%
    ・demon slayer × weiss_schwarz (2021-05) → +180%
    ・jujutsu kaisen × union_arena (2023-07) → +80%
    ・chainsaw man × weiss_schwarz (2023-01) → +120%

  【🎫 抽選 / 予約管道】
    通路 A：申込開始 XXXX / 締切 YYYY
    https://...

  [👍 有用] [👎 不感興趣] [💰 我下手了]
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Sequence

from openclaw_adapter.collab_similarity_provider import CollabInference


@dataclass
class StoreListingInfo:
    """Minimal representation of an official-store lottery / pre-order listing."""
    store_display: str      # "Joshin" / "UA 公式" etc.
    title: str
    url: str
    status_jp: str = ""     # "予約受付中" / "抽選申込受付中"
    price_jpy: int | None = None
    open_date: str | None = None    # ISO date or None
    deadline: str | None = None     # ISO date or None


@dataclass
class CollabNotification:
    """All data needed to render the three-segment collab notification."""

    # Identity
    ip_canonical: str            # "chainsaw man"
    tcg_game: str                # "union_arena"
    product_name: str            # "UNION ARENA EX チェンソーマン"

    # Segment 1 — ex-ante signals
    long_term_score: float | None = None       # 0-100
    arbitrage_score: float | None = None       # 0-100
    ip_heat_percentile: float | None = None    # 0-100
    heat_sources: list[str] = field(default_factory=list)   # ["x_mention", "reddit"]
    announcement_context: str = ""   # free-text from SNS / classifier rationale

    # Segment 2 — historical inference
    inference: CollabInference | None = None

    # Segment 3 — store listings
    store_listings: list[StoreListingInfo] = field(default_factory=list)

    # 💰 case_id for backfill tracking
    collab_case_id: str | None = None


def format_collab_notification(n: CollabNotification) -> str:
    """Render the full three-segment notification as a plain-text Telegram message."""
    lines: list[str] = []

    # ── headline ──────────────────────────────────────────────────────────
    ip_display = n.ip_canonical.title()
    tcg_display = n.tcg_game.replace("_", " ").title()
    has_dual = n.ip_heat_percentile is not None and n.ip_heat_percentile >= 70
    headline_icon = "🔥" if has_dual else "📈"
    headline_label = "熱度 × 公告雙重訊號" if has_dual else "公告通知"
    lines.append(f"{headline_icon} {headline_label} — {ip_display} × {tcg_display}")
    lines.append("")

    # scores
    score_parts: list[str] = []
    if n.long_term_score is not None:
        score_parts.append(f"📈 長期 {n.long_term_score:.0f}")
    if n.arbitrage_score is not None:
        score_parts.append(f"⚡ 立即 {n.arbitrage_score:.0f}")
    if score_parts:
        lines.append(" / ".join(score_parts))
        lines.append("")

    # ── Segment 1: ex-ante signals ─────────────────────────────────────────
    lines.append("【事前訊號】")
    if n.product_name:
        lines.append(f"  - {n.product_name}")
    if n.announcement_context:
        lines.append(f"  - {n.announcement_context}")
    if n.ip_heat_percentile is not None:
        heat_badge = "🔥" if n.ip_heat_percentile >= 80 else "🌡" if n.ip_heat_percentile >= 60 else ""
        sources_str = "、".join(n.heat_sources) if n.heat_sources else ""
        heat_line = f"  - IP 熱度 percentile={n.ip_heat_percentile:.0f} {heat_badge}"
        if sources_str:
            heat_line += f"（{sources_str}）"
        lines.append(heat_line)
    lines.append("")

    # ── Segment 2: historical inference ────────────────────────────────────
    if n.inference is not None and n.inference.n_samples > 0:
        inf = n.inference
        lines.append(f"【📊 歴史推理】（類似 {inf.n_samples} 件）")
        if inf.mean_profit_pct_180d is not None:
            n_win = int(round((inf.win_rate_180d or 0) * inf.n_samples))
            lines.append(
                f"  平均 180 日利益 {inf.mean_profit_pct_180d:+.1f}%、"
                f"勝率 {n_win}/{inf.n_samples}"
            )
            if inf.best_profit_pct_180d is not None:
                lines.append(
                    f"  最良 {inf.best_profit_pct_180d:+.0f}% / "
                    f"最悪 {inf.worst_profit_pct_180d:+.0f}%"
                )
        for sc in inf.similar_cases[:3]:
            p = f"{sc.profit_pct_180d:+.0f}%" if sc.profit_pct_180d is not None else "—"
            lines.append(
                f"  ・{sc.ip_canonical} × {sc.tcg_game} "
                f"({sc.announce_date[:7]}) → {p}"
            )
        lines.append("")
    else:
        lines.append("【📊 歴史推理】")
        lines.append("  （類似事例なし — 今後データが蓄積されます）")
        lines.append("")

    # ── Segment 3: store listings ───────────────────────────────────────────
    lines.append("【🎫 抽選 / 予約管道】")
    if n.store_listings:
        for listing in n.store_listings:
            store_line = f"  {listing.store_display}"
            if listing.status_jp:
                store_line += f"（{listing.status_jp}）"
            if listing.price_jpy:
                store_line += f" ¥{listing.price_jpy:,}"
            lines.append(store_line)
            if listing.open_date:
                lines.append(f"    申込開始：{listing.open_date}")
            if listing.deadline:
                lines.append(f"    申込締切：{listing.deadline}")
            lines.append(f"    {listing.url}")
    else:
        lines.append("  （まだ公式予約 / 抽選情報なし）")
    lines.append("")

    # ── feedback buttons ───────────────────────────────────────────────────
    lines.append("[👍 有用] [👎 不感興趣] [💰 我下手了]")

    return "\n".join(lines)


def collab_notification_from_dual_signal(
    *,
    ip_canonical: str,
    tcg_game: str,
    product_name: str,
    heat_percentile: float,
    heat_sources: Sequence[str],
    inference: CollabInference | None,
    store_listings: Sequence[StoreListingInfo] | None = None,
    long_term_score: float | None = None,
    arbitrage_score: float | None = None,
    announcement_context: str = "",
    collab_case_id: str | None = None,
) -> CollabNotification:
    """Convenience constructor: build CollabNotification from a DualSignal's data."""
    return CollabNotification(
        ip_canonical=ip_canonical,
        tcg_game=tcg_game,
        product_name=product_name,
        long_term_score=long_term_score,
        arbitrage_score=arbitrage_score,
        ip_heat_percentile=heat_percentile,
        heat_sources=list(heat_sources),
        announcement_context=announcement_context,
        inference=inference,
        store_listings=list(store_listings) if store_listings else [],
        collab_case_id=collab_case_id,
    )
