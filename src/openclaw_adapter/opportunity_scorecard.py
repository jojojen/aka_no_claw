"""Opportunity hit-rate scorecard (B1 + B3).

Aggregates sent notifications × user feedback from OpportunityStore to
answer: "which kinds of signal are actually making money?"

hit_rate = (👍 up + 💰 bought) / total_with_feedback
"""
from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

logger = logging.getLogger(__name__)


@dataclass
class ScorecardBucket:
    key: str
    total_sent: int = 0
    up: int = 0
    down: int = 0
    bought: int = 0
    no_feedback: int = 0

    @property
    def total_feedback(self) -> int:
        return self.up + self.down + self.bought

    @property
    def hit_rate_pct(self) -> float | None:
        if self.total_feedback == 0:
            return None
        return (self.up + self.bought) / self.total_feedback * 100.0


@dataclass
class Scorecard:
    overall: ScorecardBucket
    by_source_kind: list[ScorecardBucket] = field(default_factory=list)
    by_game: list[ScorecardBucket] = field(default_factory=list)
    since_days: int = 90
    generated_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    )


def _bucket_from_rows(key: str, rows: list[sqlite3.Row]) -> ScorecardBucket:
    b = ScorecardBucket(key=key)
    for row in rows:
        kind = row["feedback_kind"]
        b.total_sent += 1
        if kind == "up":
            b.up += 1
        elif kind == "down":
            b.down += 1
        elif kind == "bought":
            b.bought += 1
        else:
            b.no_feedback += 1
    return b


@contextmanager
def _connect_ro(path: Path) -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def compute_scorecard(opportunity_db_path: str | Path, *, since_days: int = 90) -> Scorecard:
    """Read opportunity_store and compute hit-rate scorecard.

    Only counts recommendations where notified_at IS NOT NULL (actually sent).
    Returns an empty Scorecard if the DB doesn't exist yet.
    """
    path = Path(opportunity_db_path)
    if not path.exists():
        return Scorecard(overall=ScorecardBucket(key="overall"), since_days=since_days)

    cutoff = datetime.now(timezone.utc).replace(microsecond=0)
    from datetime import timedelta
    cutoff -= timedelta(days=since_days)
    cutoff_iso = cutoff.isoformat()

    try:
        with _connect_ro(path) as conn:
            rows = conn.execute(
                """
                SELECT r.feedback_kind, c.source_kind, c.game
                FROM opportunity_recommendations r
                JOIN opportunity_candidates c ON r.candidate_id = c.candidate_id
                WHERE r.notified_at IS NOT NULL
                  AND r.created_at >= ?
                """,
                (cutoff_iso,),
            ).fetchall()
    except Exception:
        logger.exception("compute_scorecard: DB query failed path=%s", path)
        return Scorecard(overall=ScorecardBucket(key="overall"), since_days=since_days)

    overall = _bucket_from_rows("overall", rows)

    # Group by source_kind
    by_sk: dict[str, list[sqlite3.Row]] = {}
    by_game: dict[str, list[sqlite3.Row]] = {}
    for row in rows:
        sk = row["source_kind"] or "unknown"
        by_sk.setdefault(sk, []).append(row)
        g = row["game"] or "unknown"
        by_game.setdefault(g, []).append(row)

    return Scorecard(
        overall=overall,
        by_source_kind=sorted(
            [_bucket_from_rows(k, v) for k, v in by_sk.items()],
            key=lambda b: b.total_sent, reverse=True,
        ),
        by_game=sorted(
            [_bucket_from_rows(k, v) for k, v in by_game.items()],
            key=lambda b: b.total_sent, reverse=True,
        ),
        since_days=since_days,
    )


def format_scorecard(sc: Scorecard) -> str:
    """Format a Scorecard as a Telegram-friendly text block."""
    o = sc.overall
    lines = [f"📊 機會命中率（最近 {sc.since_days} 天）", ""]

    if o.total_sent == 0:
        lines.append("尚無通知記錄。")
        return "\n".join(lines)

    feedback_pct = f"{o.total_feedback / o.total_sent * 100:.0f}%" if o.total_sent else "—"
    lines.append(
        f"總通知 {o.total_sent} 則　有回饋 {o.total_feedback} 則（{feedback_pct}）"
    )
    lines.append(
        f"👍 {o.up}　💰 {o.bought}　👎 {o.down}　無回饋 {o.no_feedback}"
    )

    if o.hit_rate_pct is not None:
        lines.append(
            f"\n整體命中率：{o.hit_rate_pct:.0f}%"
            f"（{o.up + o.bought}/{o.total_feedback}）"
        )

    def _fmt_bucket(b: ScorecardBucket) -> str:
        hr = f"{b.hit_rate_pct:.0f}%" if b.hit_rate_pct is not None else "—"
        return f"  {b.key}　{b.up + b.bought}/{b.total_feedback} = {hr}（共 {b.total_sent} 則）"

    if sc.by_source_kind:
        lines.append("\n依類型：")
        lines.extend(_fmt_bucket(b) for b in sc.by_source_kind)

    if sc.by_game:
        lines.append("\n依遊戲：")
        lines.extend(_fmt_bucket(b) for b in sc.by_game[:5])  # top 5

    lines.append(f"\n更新：{sc.generated_at[:16].replace('T', ' ')} UTC")
    return "\n".join(lines)


def scorecard_as_prior_block(sc: Scorecard) -> str:
    """Format scorecard stats as a classifier prior block (for LLM injection).

    Returns empty string when there's insufficient data (< 5 with feedback).
    """
    o = sc.overall
    if o.total_feedback < 5:
        return ""
    lines = [
        f"[Bot self-assessed hit rate over last {sc.since_days}d "
        f"({o.total_feedback} feedback samples)]",
        f"  overall hit rate: {o.hit_rate_pct:.0f}%",
    ]
    for b in sc.by_source_kind[:4]:
        if b.total_feedback >= 2 and b.hit_rate_pct is not None:
            lines.append(f"  {b.key}: {b.hit_rate_pct:.0f}% ({b.total_feedback} samples)")
    return "\n".join(lines)


def build_scorecard_handler(settings) -> Callable[[str], str]:
    """Return a /stats handler bound to the project's opportunity DB path."""
    db_path = Path(settings.opportunity_db_path)

    def handler(remainder: str) -> str:
        try:
            since_days = int(remainder.strip()) if remainder.strip().isdigit() else 90
            sc = compute_scorecard(db_path, since_days=since_days)
            return format_scorecard(sc)
        except Exception as exc:
            logger.exception("scorecard_handler: failed")
            return f"❌ 統計失敗：{exc}"

    return handler
