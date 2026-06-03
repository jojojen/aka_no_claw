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


# 音檔 link preference: a clickable PV in this order. YouTube first (most Miku PVs
# carry one), then NicoNico — many Vocaloid songs live ONLY on ニコニコ, which is why
# a Youtube-only extractor used to leave media_url empty for them.
_PV_SERVICE_PREFERENCE = ("youtube", "niconico", "bilibili", "soundcloud", "piapro", "vimeo")


def _service_rank(service: str) -> int:
    s = (service or "").lower()
    for i, pref in enumerate(_PV_SERVICE_PREFERENCE):
        if pref in s:
            return i
    return len(_PV_SERVICE_PREFERENCE)


def _extract_pv_url(pvs: list) -> str | None:
    """Best playable PV URL: prefer YouTube, then NicoNico/others; within a service
    prefer the Original PV over reprints. Returns None only if no PV has a URL."""
    best_url: str | None = None
    best_key: tuple[int, int] | None = None
    for pv in pvs:
        if not isinstance(pv, dict) or not pv.get("url"):
            continue
        is_original = str(pv.get("pvType", "")).lower() == "original"
        key = (_service_rank(str(pv.get("service", ""))), 0 if is_original else 1)
        if best_key is None or key < best_key:
            best_key, best_url = key, str(pv["url"])
    return best_url


def _vocadb_songs_get(
    params: dict,
    *,
    timeout_seconds: int = 15,
    ssl_context: ssl.SSLContext | None = None,
) -> list:
    """GET vocadb.net/api/songs and return the ``items`` list ([] on any failure)."""
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
        return json.loads(body).get("items") or []
    except (ValueError, AttributeError) as exc:
        logger.warning("VocaDB response not parseable: %s", exc)
        return []


def _norm_name(s: str) -> str:
    """Normalize a song title for matching: unify the sharp variants (＃/♯/#) and
    drop spaces, so 『心拍数 #0822』 (our DB) matches 『心拍数♯0822』 (VocaDB). Kept tight
    — this only erases punctuation/spacing noise, never collapses distinct titles."""
    out = (s or "")
    for ch in ("＃", "♯"):
        out = out.replace(ch, "#")
    return out.replace(" ", "").replace("　", "").strip().lower()


def _select_song_pv(items: list, name: str) -> str | None:
    """Pick the PV URL for the entry whose name EXACTLY equals ``name`` (primary or
    alias) after light normalization. Exactness matters: a substring match drifts to
    parodies/remixes — e.g. 『ロキ』 fuzzy-matched 『ロキソプロフェン…』, attaching the wrong
    audio. Items arrive sorted by RatingScore, so the first match is canonical."""
    target = _norm_name(name)
    if not target:
        return None
    for item in items:
        if not isinstance(item, dict):
            continue
        names = {str(item.get("name") or "")}
        names |= {
            str(n.get("value") or "")
            for n in (item.get("names") or [])
            if isinstance(n, dict)
        }
        if target not in {_norm_name(n) for n in names}:
            continue
        url = _extract_pv_url(item.get("pvs") or [])
        if url:
            return url
    return None


def resolve_song_media_url(
    name: str,
    *,
    timeout_seconds: int = 15,
    ssl_context: ssl.SSLContext | None = None,
) -> str | None:
    """Resolve a song's official 音檔/PV URL from VocaDB by EXACT name. Returns None
    when VocaDB has no exactly-named song with a playable PV (we never guess)."""
    if not (name or "").strip():
        return None
    items = _vocadb_songs_get(
        {
            "query": name,
            "sort": "RatingScore",
            "onlyWithPVs": "true",
            "maxResults": 10,
            "getTotalCount": "false",
            "fields": "PVs,Names",
            "nameMatchMode": "Exact",
        },
        timeout_seconds=timeout_seconds,
        ssl_context=ssl_context,
    )
    return _select_song_pv(items, name)


def backfill_song_media(db, *, resolver=resolve_song_media_url) -> tuple[int, int]:
    """Fill missing 音檔 URLs on song-typed questions by resolving each song name
    against VocaDB. Idempotent, best-effort (network failures skip a name), cached
    per name so duplicate songs cost one lookup. Returns (rows_filled, rows_missing).

    This heals gaps from ANY creation path (e.g. reading-comprehension items built
    from a 賞析 article never carried a PV), so 『一律附音檔』 holds pool-wide."""
    missing = db.missing_media_song_questions()
    if not missing:
        return (0, 0)
    cache: dict[str, str | None] = {}
    filled = 0
    for question_id, name in missing:
        key = (name or "").strip()
        if not key:
            continue
        if key not in cache:
            try:
                cache[key] = resolver(key)
            except Exception:
                logger.exception("backfill_song_media: resolver failed for %r", key)
                cache[key] = None
        url = cache[key]
        if url and db.set_media_url(question_id, url):
            filled += 1
    logger.info("backfill_song_media: filled %d/%d missing media URLs", filled, len(missing))
    return (filled, len(missing))


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
    items = _vocadb_songs_get(
        {
            "artistId[]": HATSUNE_MIKU_ARTIST_ID,
            "songTypes": "Original",
            "sort": "RatingScore",
            "onlyWithPVs": "true",
            "maxResults": max(1, min(100, limit)),
            "getTotalCount": "false",
            "fields": "PVs,Lyrics",
        },
        timeout_seconds=timeout_seconds,
        ssl_context=ssl_context,
    )

    sources: list[QuizSource] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        song_id = item.get("id")
        if not name or song_id is None:
            continue
        vocadb_url = f"https://vocadb.net/S/{song_id}"
        media_url = _extract_pv_url(item.get("pvs") or [])
        excerpt, lyrics_url = _extract_original_lyrics(item.get("lyrics") or [])
        sources.append(
            QuizSource(
                source_type=_SOURCE_TYPE,
                name=name,
                text_url=lyrics_url or vocadb_url,
                media_url=media_url,
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
