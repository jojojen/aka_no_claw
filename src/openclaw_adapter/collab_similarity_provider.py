"""CollabSimilarityProvider (D3).

Given a new collab announcement (ip_canonical + tcg_game + ip_heat), finds the
top-N most similar historical cases in CollabOutcomesStore and returns a
CollabInference with profit statistics that can be injected into the classifier
prompt or notification formatter.

Similarity scoring (higher = more similar):
  +4  same tcg_game
  +3  same ip_canonical
  +2  same broad ip_type (shounen / moe / vtuber / pokemon / …)
  +1  ip_heat_at_announce within ±20 percentile of query heat
"""

from __future__ import annotations

import logging
import statistics
from dataclasses import dataclass, field
from typing import Sequence

from openclaw_adapter.collab_outcomes_store import CollabOutcome, CollabOutcomesStore

logger = logging.getLogger(__name__)

# ── IP type buckets ────────────────────────────────────────────────────────────

_IP_TYPE_MAP: dict[str, str] = {
    # shounen / action
    "demon slayer": "shounen",
    "kimetsu no yaiba": "shounen",
    "jujutsu kaisen": "shounen",
    "chainsaw man": "shounen",
    "my hero academia": "shounen",
    "attack on titan": "shounen",
    "one piece": "shounen",
    "dragon ball": "shounen",
    "blue lock": "shounen",
    "kaiju no 8": "shounen",
    "kaiju no.8": "shounen",
    "fullmetal alchemist": "shounen",
    # isekai
    "frieren": "isekai",
    "re zero": "isekai",
    "re:zero": "isekai",
    "mushoku tensei": "isekai",
    "overlord": "isekai",
    # moe / slice of life
    "bocchi the rock": "moe",
    "spy x family": "moe",
    "kaguya sama": "moe",
    "lycoris recoil": "moe",
    # mecha / sci-fi
    "gundam": "mecha",
    "evangelion": "mecha",
    # vtuber / idol
    "hololive": "vtuber",
    "project sekai": "idol",
    "pjsk": "idol",
    "idolmaster": "idol",
    # fate / gacha
    "fate grand order": "gacha",
    "fgo": "gacha",
    "genshin": "gacha",
    # pokemon
    "pokemon": "pokemon",
    # other
    "oshi no ko": "idol",
    "sword art online": "moe",
    "sao": "moe",
}


def _ip_type(ip_canonical: str) -> str:
    return _IP_TYPE_MAP.get(ip_canonical.strip().lower(), "other")


# ── Dataclasses ────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class SimilarCase:
    case_id: str
    ip_canonical: str
    tcg_game: str
    product_name: str
    announce_date: str
    profit_pct_30d: float | None
    profit_pct_180d: float | None
    similarity_score: float


@dataclass(frozen=True)
class CollabInference:
    """Result of CollabSimilarityProvider for a single query."""

    query_ip: str
    query_tcg: str
    n_samples: int
    similar_cases: tuple[SimilarCase, ...]

    # Stats across cases that have profit_pct_180d data
    mean_profit_pct_180d: float | None = None
    win_rate_180d: float | None = None        # fraction where profit_pct_180d > 0
    best_profit_pct_180d: float | None = None
    worst_profit_pct_180d: float | None = None

    # Stats across cases that have profit_pct_30d data
    mean_profit_pct_30d: float | None = None
    win_rate_30d: float | None = None

    def as_prompt_block(self) -> str:
        """Format for injection into the classifier prompt."""
        if self.n_samples == 0:
            return ""
        lines: list[str] = [
            f"歴史 collab 参考（{self.query_ip} × {self.query_tcg}）:",
            f"  類似 {self.n_samples} 件",
        ]
        if self.mean_profit_pct_180d is not None:
            wp = f"{self.win_rate_180d * 100:.0f}%" if self.win_rate_180d is not None else "N/A"
            lines.append(
                f"  180 日平均利益 {self.mean_profit_pct_180d:+.1f}%、勝率 {wp}"
            )
            if self.best_profit_pct_180d is not None:
                lines.append(
                    f"  最良 {self.best_profit_pct_180d:+.1f}% / "
                    f"最悪 {self.worst_profit_pct_180d:+.1f}%"
                )
        if self.mean_profit_pct_30d is not None:
            wp30 = f"{self.win_rate_30d * 100:.0f}%" if self.win_rate_30d is not None else "N/A"
            lines.append(
                f"  30 日平均利益 {self.mean_profit_pct_30d:+.1f}%、勝率 {wp30}"
            )
        # top examples
        for c in self.similar_cases[:3]:
            p = f"{c.profit_pct_180d:+.0f}%" if c.profit_pct_180d is not None else "—"
            lines.append(f"  ・{c.ip_canonical} × {c.tcg_game} ({c.announce_date[:7]}) → {p}")
        return "\n".join(lines)

    def as_notification_block(self) -> str:
        """Format for the 📊 section in the Telegram notification."""
        if self.n_samples == 0:
            return ""
        parts: list[str] = [f"📊 歴史推理（類似 {self.n_samples} 件）"]
        if self.mean_profit_pct_180d is not None:
            n_win = int(round((self.win_rate_180d or 0) * self.n_samples))
            parts.append(
                f"  平均 180 日利益 {self.mean_profit_pct_180d:+.1f}%、"
                f"勝率 {n_win}/{self.n_samples}"
            )
            if self.best_profit_pct_180d is not None:
                parts.append(
                    f"  最良 {self.best_profit_pct_180d:+.0f}% / "
                    f"最悪 {self.worst_profit_pct_180d:+.0f}%"
                )
        for c in self.similar_cases[:3]:
            p = f"{c.profit_pct_180d:+.0f}%" if c.profit_pct_180d is not None else "—"
            parts.append(f"  ・{c.ip_canonical} × {c.tcg_game} → {p}")
        return "\n".join(parts)


# ── Provider ───────────────────────────────────────────────────────────────────


class CollabSimilarityProvider:
    """Find historical collabs similar to a query and compute profit statistics."""

    def __init__(
        self,
        store: CollabOutcomesStore,
        *,
        top_n: int = 7,
        min_confidence: float = 0.5,
    ) -> None:
        self._store = store
        self._top_n = top_n
        self._min_confidence = min_confidence

    def infer(
        self,
        ip_canonical: str,
        tcg_game: str,
        *,
        ip_heat: float | None = None,
    ) -> CollabInference:
        """Return a CollabInference for the given ip + tcg combination."""
        ip = ip_canonical.strip().lower()
        tcg = tcg_game.strip().lower()
        query_type = _ip_type(ip)

        candidates = self._store.list_all(
            min_confidence=self._min_confidence, limit=500
        )

        scored: list[tuple[float, CollabOutcome]] = []
        for c in candidates:
            s = self._score(c, ip, tcg, query_type, ip_heat)
            scored.append((s, c))

        scored.sort(key=lambda x: -x[0])
        top = scored[: self._top_n]

        similar_cases = tuple(
            SimilarCase(
                case_id=c.case_id,
                ip_canonical=c.ip_canonical,
                tcg_game=c.tcg_game,
                product_name=c.product_name,
                announce_date=c.announce_date,
                profit_pct_30d=c.profit_pct_30d,
                profit_pct_180d=c.profit_pct_180d,
                similarity_score=score,
            )
            for score, c in top
        )

        return _build_inference(ip, tcg, similar_cases)

    # ── internal ──────────────────────────────────────────────────────────────

    @staticmethod
    def _score(
        c: CollabOutcome,
        query_ip: str,
        query_tcg: str,
        query_type: str,
        query_heat: float | None,
    ) -> float:
        score = 0.0
        if c.tcg_game == query_tcg:
            score += 4.0
        if c.ip_canonical == query_ip:
            score += 3.0
        if _ip_type(c.ip_canonical) == query_type and query_type != "other":
            score += 2.0
        if query_heat is not None and c.ip_heat_at_announce is not None:
            if abs(c.ip_heat_at_announce - query_heat) <= 20:
                score += 1.0
        return score


def _build_inference(
    ip: str,
    tcg: str,
    similar_cases: tuple[SimilarCase, ...],
) -> CollabInference:
    vals_180 = [c.profit_pct_180d for c in similar_cases if c.profit_pct_180d is not None]
    vals_30 = [c.profit_pct_30d for c in similar_cases if c.profit_pct_30d is not None]

    def _stats(vals: list[float]) -> tuple[float | None, float | None, float | None, float | None]:
        if not vals:
            return None, None, None, None
        mean = statistics.mean(vals)
        win_rate = sum(1 for v in vals if v > 0) / len(vals)
        return mean, win_rate, max(vals), min(vals)

    mean_180, wr_180, best_180, worst_180 = _stats(vals_180)
    mean_30, wr_30, _, _ = _stats(vals_30)

    return CollabInference(
        query_ip=ip,
        query_tcg=tcg,
        n_samples=len(similar_cases),
        similar_cases=similar_cases,
        mean_profit_pct_180d=mean_180,
        win_rate_180d=wr_180,
        best_profit_pct_180d=best_180,
        worst_profit_pct_180d=worst_180,
        mean_profit_pct_30d=mean_30,
        win_rate_30d=wr_30,
    )
