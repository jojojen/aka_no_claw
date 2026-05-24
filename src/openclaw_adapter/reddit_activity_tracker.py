"""Reddit activity tracker for IP heat signals (C3).

Measures subreddit/search activity for a given IP by querying the Reddit
public JSON API (no auth required).  The activity score is the sum of post
scores (upvotes) from the top-50 results — this captures both *breadth*
(many posts) and *depth* (popular posts) more faithfully than a raw count.

Reddit blocks urllib by TLS fingerprint so we fall back to curl, matching
the approach already used in sns_monitor_bot/reddit_buzz.py.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
import urllib.parse
from typing import TypedDict

from .ip_heat_store import HeatSignal, IpHeatStore

logger = logging.getLogger(__name__)

_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

_WINDOW_DAYS: dict[str, int] = {
    "hour": 1, "day": 1, "week": 7, "month": 30, "year": 365,
}


class _Post(TypedDict):
    score: int
    num_comments: int
    title: str


def _curl_get(url: str, *, timeout: float = 15.0) -> str | None:
    """Run curl to bypass Reddit's TLS fingerprint check. Returns body or None."""
    curl_bin = shutil.which("curl") or "/usr/bin/curl"
    try:
        result = subprocess.run(
            [
                curl_bin, "-sSL",
                "--max-time", str(int(timeout)),
                "-H", f"User-Agent: {_USER_AGENT}",
                "-H", "Accept: application/json",
                url,
            ],
            capture_output=True,
            timeout=timeout + 5,
            check=False,
        )
        if result.returncode != 0:
            logger.warning("reddit_activity_tracker: curl %d for %s", result.returncode, url)
            return None
        return result.stdout.decode("utf-8", errors="replace")
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as exc:
        logger.warning("reddit_activity_tracker: curl exec failed: %s", exc)
        return None


def _parse_posts(body: str) -> list[_Post]:
    """Extract post dicts from Reddit listing JSON."""
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        return []
    children = (data.get("data") or {}).get("children") or []
    posts: list[_Post] = []
    for c in children:
        pd = c.get("data") or {}
        if pd.get("stickied"):
            continue  # skip pinned mod posts
        title = (pd.get("title") or "").strip()
        if not title:
            continue
        posts.append(
            _Post(
                score=int(pd.get("score") or 0),
                num_comments=int(pd.get("num_comments") or 0),
                title=title,
            )
        )
    return posts


def _search_posts(query: str, *, window: str = "week", limit: int = 50) -> list[_Post] | None:
    """Fetch Reddit search results (sorted by top). Returns None on failure."""
    encoded = urllib.parse.quote(query)
    url = (
        f"https://www.reddit.com/search.json"
        f"?q={encoded}&sort=top&t={window}&limit={min(50, max(5, limit))}"
        f"&include_over_18=on"
    )
    body = _curl_get(url)
    if body is None:
        return None
    posts = _parse_posts(body)
    if not posts and '"error"' in body:
        logger.warning("reddit_activity_tracker: API error for query=%r", query)
        return None
    return posts


def _subreddit_hot_posts(subreddit: str, *, limit: int = 50) -> list[_Post] | None:
    """Fetch hot posts from a subreddit. Returns None on failure."""
    name = subreddit.strip().lstrip("/").removeprefix("r/").strip("/")
    if not name:
        return None
    url = (
        f"https://www.reddit.com/r/{urllib.parse.quote(name, safe='')}"
        f"/hot.json?limit={min(50, max(5, limit))}&raw_json=1"
    )
    body = _curl_get(url)
    if body is None:
        return None
    return _parse_posts(body)


def _activity_score(posts: list[_Post]) -> float:
    """Return sum of post scores as the activity metric."""
    return float(sum(p["score"] for p in posts))


class RedditActivityTracker:
    """Measures Reddit activity for IP keywords and records heat signals.

    Usage:
        tracker = RedditActivityTracker(heat_store)
        signal = tracker.track_ip(
            ip_canonical="chainsaw man",
            queries=["チェンソーマン anime", "chainsawman"],
            subreddits=["chainsawman"],
        )
    """

    def __init__(
        self,
        heat_store: IpHeatStore,
        *,
        timeout_seconds: int = 15,
    ) -> None:
        self._store = heat_store
        self._timeout = timeout_seconds

    def _fetch_search(self, query: str, *, window: str = "week") -> list[_Post] | None:
        return _search_posts(query, window=window)

    def _fetch_subreddit(self, subreddit: str) -> list[_Post] | None:
        return _subreddit_hot_posts(subreddit)

    def measure_search_activity(self, query: str, *, window: str = "week") -> float:
        """Return activity score for a search query (sum of top-50 post scores)."""
        posts = self._fetch_search(query, window=window)
        return _activity_score(posts) if posts is not None else 0.0

    def measure_subreddit_activity(self, subreddit: str) -> float:
        """Return activity score for a subreddit hot page."""
        posts = self._fetch_subreddit(subreddit)
        return _activity_score(posts) if posts is not None else 0.0

    def track_ip(
        self,
        *,
        ip_canonical: str,
        queries: list[str] | None = None,
        subreddits: list[str] | None = None,
        window: str = "week",
    ) -> HeatSignal | None:
        """Sum activity from all queries + subreddits and record to IpHeatStore.

        At least one of `queries` or `subreddits` must be provided.
        Returns None if every source failed."""
        total = 0.0
        any_success = False

        for q in (queries or []):
            posts = self._fetch_search(q, window=window)
            if posts is not None:
                any_success = True
                total += _activity_score(posts)

        for sub in (subreddits or []):
            posts = self._fetch_subreddit(sub)
            if posts is not None:
                any_success = True
                total += _activity_score(posts)

        if not any_success:
            logger.warning(
                "RedditActivityTracker.track_ip: no data for %r "
                "(queries=%s subreddits=%s)", ip_canonical, queries, subreddits,
            )
            return None

        window_days = _WINDOW_DAYS.get(window, 7)
        return self._store.record(
            ip_canonical=ip_canonical,
            source="reddit",
            value=total,
            window_days=window_days,
        )
