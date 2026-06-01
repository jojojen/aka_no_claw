"""Background pre-warmer: proactively enqueues hot entities for RAG research.

Sources (every interval_seconds, default 6 h):
  - snkrdunk hot card products (top 30 per game, card kind only)
  - Marketplace watchlist queries

Both feeds are already used by the bot. This job makes sure the knowledge
DB is populated *before* a tweet or alert mentions the entity, so the
classifier gets grounded context on first sight rather than firing research
reactively and serving empty context until the next tweet.
"""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path

logger = logging.getLogger(__name__)

_PREWARM_TOP_N = 30
_GAME_KEYS = ("pokemon", "ws", "union_arena", "yugioh")


class KnowledgePrewarmer:
    """Daemon thread that feeds hot entities to EntityResearcher proactively."""

    def __init__(
        self,
        *,
        research_fn,
        monitor_db_path: str | Path,
        http_client=None,
        interval_seconds: float = 24 * 3600,
        initial_delay_seconds: float = 300,
    ) -> None:
        self._research_fn = research_fn
        self._monitor_db_path = Path(monitor_db_path)
        self._http_client = http_client
        self._interval = interval_seconds
        self._initial_delay = initial_delay_seconds
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(
            target=self._loop, name="knowledge-prewarmer", daemon=True
        )
        self._thread.start()
        logger.info(
            "KnowledgePrewarmer started — first run in %.0f min, then every %.0f h",
            self._initial_delay / 60,
            self._interval / 3600,
        )

    def _loop(self) -> None:
        time.sleep(self._initial_delay)
        while True:
            try:
                self._prewarm_once()
            except Exception:
                logger.exception("KnowledgePrewarmer: prewarm_once failed")
            time.sleep(self._interval)

    def _prewarm_once(self) -> None:
        entities: list[str] = []
        entities.extend(self._fetch_snkrdunk_entities())
        entities.extend(self._fetch_watchlist_queries())

        queued = 0
        for name in entities:
            try:
                if self._research_fn(name):
                    queued += 1
            except Exception:
                logger.exception("KnowledgePrewarmer: research_fn failed for %r", name)

        logger.info(
            "KnowledgePrewarmer: scanned %d candidates → queued %d for research",
            len(entities),
            queued,
        )

    def _fetch_snkrdunk_entities(self) -> list[str]:
        try:
            from market_monitor.http import HttpClient
            from tcg_tracker.snkrdunk_ranking import iter_ranked_products
        except ImportError:
            logger.warning("KnowledgePrewarmer: snkrdunk modules not importable — skipping")
            return []

        client = self._http_client or HttpClient()
        names: list[str] = []
        for game in _GAME_KEYS:
            try:
                products = iter_ranked_products(
                    game=game, http_client=client, limit=_PREWARM_TOP_N
                )
                for p in products:
                    if p.item_kind == "card":
                        names.append(p.title)
            except Exception:
                logger.exception(
                    "KnowledgePrewarmer: snkrdunk fetch failed game=%s", game
                )
        logger.debug("KnowledgePrewarmer: snkrdunk → %d card names", len(names))
        return names

    def _fetch_watchlist_queries(self) -> list[str]:
        if not self._monitor_db_path.exists():
            return []
        try:
            from market_monitor.storage import MonitorDatabase
            db = MonitorDatabase(str(self._monitor_db_path))
            watches = db.list_marketplace_watchlist()
            queries = [w.query for w in watches if w.query]
            logger.debug(
                "KnowledgePrewarmer: watchlist → %d queries", len(queries)
            )
            return queries
        except Exception:
            logger.exception("KnowledgePrewarmer: watchlist fetch failed")
            return []
