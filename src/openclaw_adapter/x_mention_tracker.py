"""X/Twitter mention volume tracker via Nitter hashtag RSS (C2).

Counts how many times an IP keyword was mentioned on X within the last `days`
days by fetching the Nitter hashtag RSS feed.  The count is a *sample* (Nitter
returns ~40 items per feed) but is consistent across time, making the
percentile ranking produced by IpHeatStore meaningful.
"""

from __future__ import annotations

import logging
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

from .ip_heat_store import HeatSignal, IpHeatStore

logger = logging.getLogger(__name__)

# Nitter mirrors to try in order; first working one wins.
NITTER_HOSTS: tuple[str, ...] = (
    "nitter.net",
    "nitter.privacydev.net",
    "nitter.poast.org",
    "xcancel.com",
)

_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/rss+xml,application/xml,text/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
    "Accept-Encoding": "identity",
}


def _fetch_url(url: str, timeout: float = 15.0) -> str:
    req = urllib.request.Request(url, headers=_BROWSER_HEADERS)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")


def _count_items_in_window(rss_text: str, *, days: int) -> int:
    """Parse RSS XML and count items published within the last `days` days."""
    try:
        root = ET.fromstring(rss_text)
    except ET.ParseError as exc:
        logger.warning("XMentionTracker: RSS parse failed: %s", exc)
        return 0

    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    count = 0
    for item in root.iter("item"):
        pub_el = item.find("pubDate")
        if pub_el is not None and pub_el.text:
            try:
                pub_dt = parsedate_to_datetime(pub_el.text)
                if pub_dt.tzinfo is None:
                    pub_dt = pub_dt.replace(tzinfo=timezone.utc)
                if pub_dt >= cutoff:
                    count += 1
            except Exception:
                count += 1  # unparseable date → assume recent
        else:
            count += 1  # no date element → assume recent
    return count


class XMentionTracker:
    """Tracks X/Twitter mention volume for IP keywords via Nitter RSS.

    Usage:
        tracker = XMentionTracker(heat_store)
        signal = tracker.track_ip(
            ip_canonical="chainsaw man",
            hashtags=["チェンソーマン", "chainsawman"],
        )
    """

    def __init__(
        self,
        heat_store: IpHeatStore,
        *,
        nitter_hosts: tuple[str, ...] = NITTER_HOSTS,
        timeout_seconds: int = 15,
    ) -> None:
        self._store = heat_store
        self._hosts = nitter_hosts
        self._timeout = timeout_seconds

    def _fetch_hashtag_rss(self, hashtag: str) -> str | None:
        """Fetch Nitter hashtag RSS, trying hosts in order. Returns XML or None."""
        encoded = urllib.parse.quote(hashtag.lstrip("#"), safe="")
        last_err: Exception | None = None
        for host in self._hosts:
            url = f"https://{host}/hashtag/{encoded}/rss"
            try:
                body = _fetch_url(url, timeout=float(self._timeout))
                if "<rss" in body and "<item>" in body:
                    logger.debug("XMentionTracker: got RSS from %s for #%s", host, hashtag)
                    return body
                logger.debug("XMentionTracker: %s returned empty feed for #%s", host, hashtag)
            except Exception as exc:
                logger.debug("XMentionTracker: %s failed for #%s: %s", host, hashtag, exc)
                last_err = exc
        logger.warning("XMentionTracker: all hosts failed for #%s (last: %s)", hashtag, last_err)
        return None

    def count_hashtag_mentions(self, hashtag: str, *, days: int = 7) -> int:
        """Fetch Nitter RSS and count tweets for `hashtag` within `days` days."""
        rss = self._fetch_hashtag_rss(hashtag)
        if rss is None:
            return 0
        return _count_items_in_window(rss, days=days)

    def track_ip(
        self,
        *,
        ip_canonical: str,
        hashtags: list[str],
        days: int = 7,
    ) -> HeatSignal | None:
        """Count mentions across all hashtags and record to IpHeatStore as x_mention.

        Sums counts from all hashtags (e.g. JP + EN forms of an IP name).
        Returns the stored HeatSignal, or None if every hashtag fetch failed."""
        total = 0
        any_success = False
        for tag in hashtags:
            rss = self._fetch_hashtag_rss(tag)
            if rss is not None:
                any_success = True
                total += _count_items_in_window(rss, days=days)

        if not any_success:
            logger.warning(
                "XMentionTracker.track_ip: no data for %r (tried %s)", ip_canonical, hashtags
            )
            return None

        return self._store.record(
            ip_canonical=ip_canonical,
            source="x_mention",
            value=float(total),
            window_days=days,
        )
