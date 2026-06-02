"""Hatsune Miku song source provider for /quiz, backed by the VocaDB API.

VocaDB (https://vocadb.net) exposes a public JSON API. We pull the most popular
*original* songs voiced by Hatsune Miku (artistId 1) sorted by rating score and
enrich each one **from the same API response** by requesting the ``PVs`` and
``Lyrics`` fields:

  - ``media_url``  ← the song's YouTube PV (``pvs[].service == 'Youtube'``)
  - ``excerpt``    ← the original Japanese lyrics (``lyrics[].translationType ==
                      'Original'`` with ``cultureCodes`` containing ``ja``)
  - ``text_url``   ← that lyrics entry's source URL (falls back to the VocaDB page)

This deliberately does NOT use web search: DuckDuckGo is unreachable from this
host (SSL EOF), and VocaDB already carries authoritative PV + lyrics data, so
grounding and the post-answer YouTube link are both reliable.

Everything degrades gracefully: a VocaDB outage yields no candidates, and a song
missing lyrics or a YouTube PV still produces a QuizSource (those fields are
optional — a vocab question can be themed to the song without its full lyrics).
"""

from __future__ import annotations

import json
import logging
import ssl
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .quiz_sources import QuizSource, register_provider

logger = logging.getLogger(__name__)

VOCADB_SONGS_API = "https://vocadb.net/api/songs"
HATSUNE_MIKU_ARTIST_ID = 1  # canonical VocaDB id for 初音ミク
_SOURCE_TYPE = "vocaloid_song"
_EXCERPT_MAX_CHARS = 600


def _extract_youtube_url(pvs: list) -> str | None:
    for pv in pvs:
        if not isinstance(pv, dict):
            continue
        if str(pv.get("service", "")).lower() == "youtube" and pv.get("url"):
            return str(pv["url"])
    return None


def _extract_original_lyrics(lyrics: list) -> tuple[str | None, str | None]:
    """Return ``(japanese_excerpt, source_url)`` from the Original-language entry."""
    best = None
    for entry in lyrics:
        if not isinstance(entry, dict):
            continue
        codes = entry.get("cultureCodes") or []
        is_ja = "ja" in codes
        is_original = str(entry.get("translationType", "")).lower() == "original"
        if is_original and (is_ja or not codes):
            best = entry
            break
        if best is None and is_ja:
            best = entry
    if best is None:
        return None, None
    text = (best.get("value") or "").strip()
    excerpt = text[:_EXCERPT_MAX_CHARS] if text else None
    return excerpt, (best.get("url") or None)


def fetch_miku_song_sources(
    *,
    limit: int = 30,
    timeout_seconds: int = 15,
    ssl_context: ssl.SSLContext | None = None,
) -> list[QuizSource]:
    """Return grounded ``QuizSource`` records for the top-rated original Miku songs.

    Returns ``[]`` on any failure (network, non-JSON, schema drift) — callers
    treat an empty list as "no material this round", never as an error."""
    params = {
        "artistId[]": HATSUNE_MIKU_ARTIST_ID,
        "songTypes": "Original",
        "sort": "RatingScore",
        "onlyWithPVs": "true",
        "maxResults": max(1, min(100, limit)),
        "getTotalCount": "false",
        "fields": "PVs,Lyrics",
    }
    url = f"{VOCADB_SONGS_API}?{urlencode(params)}"
    request = Request(
        url,
        headers={"Accept": "application/json", "User-Agent": "OpenClawQuiz/0.1 (+https://local-dev)"},
        method="GET",
    )
    try:
        with urlopen(request, timeout=timeout_seconds, context=ssl_context) as response:
            body = response.read().decode("utf-8", errors="replace")
    except (HTTPError, URLError, TimeoutError) as exc:
        logger.warning("VocaDB fetch failed: %s", exc)
        return []
    try:
        data = json.loads(body)
        items = data.get("items") or []
    except (ValueError, AttributeError) as exc:
        logger.warning("VocaDB response not parseable: %s", exc)
        return []

    sources: list[QuizSource] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        song_id = item.get("id")
        if not name or song_id is None:
            continue
        vocadb_url = f"https://vocadb.net/S/{song_id}"
        youtube_url = _extract_youtube_url(item.get("pvs") or [])
        excerpt, lyrics_url = _extract_original_lyrics(item.get("lyrics") or [])
        sources.append(
            QuizSource(
                source_type=_SOURCE_TYPE,
                name=name,
                text_url=lyrics_url or vocadb_url,
                media_url=youtube_url,
                excerpt=excerpt,
            )
        )
    logger.info("VocaDB: built %d Miku song sources", len(sources))
    return sources


class MikuRankingProvider:
    """SourceProvider for theme 'miku' — popular Hatsune Miku songs."""

    theme = "miku"

    def __init__(self, *, fetch_sources_fn=fetch_miku_song_sources) -> None:
        self._fetch_sources = fetch_sources_fn

    def fetch_candidates(self, limit: int = 10) -> list[QuizSource]:
        sources = self._fetch_sources(limit=max(limit, 30))
        return sources[:limit]


def register() -> None:
    register_provider(MikuRankingProvider())
