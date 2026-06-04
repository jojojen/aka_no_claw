"""Favorite-song ingestion for /quiz like song.

This module does the expensive work exactly once per liked song:
  - fetch YouTube metadata
  - find a full-lyrics page (VocaDB first, then lyric sites)
  - split lyrics into reusable lines/sentences
  - run Sudachi-based tokenization
  - tag tokens with a rule-based JLPT level when possible

Later quiz/flashcard workflows can reuse the stored rows instead of refetching
the web page or re-running morphology on the entire song.
"""

from __future__ import annotations

import json
import logging
import re
import unicodedata
from dataclasses import dataclass
from functools import lru_cache
from typing import Iterable
from urllib.parse import parse_qs, quote, urlparse

import requests
from bs4 import BeautifulSoup, NavigableString, Tag

logger = logging.getLogger(__name__)

_YOUTUBE_OEMBED = "https://www.youtube.com/oembed?format=json&url={url}"
_VOCADB_SONGS_API = "https://vocadb.net/api/songs"
_UTANET_SEARCH = "https://www.uta-net.com/search/?Aselect=2&Bselect=3&Keyword={query}&sort=4"
_UTATEN_SEARCH = "https://utaten.com/search?layout_search_text={query}&layout_search_type=title"
_HTTP_TIMEOUT_SECONDS = 20
_USER_AGENT = "OpenClawQuiz/0.1 (+https://local-dev)"
_MAX_SEARCH_CANDIDATES = 6
_TITLE_NOISE = (
    "official music video",
    "official video",
    "music video",
    "official mv",
    " lyric video",
    "lyrics video",
    "audio video",
    "visualizer",
)
_POS_JOIN_COUNT = 4
_JAPANESE_CHAR = r"一-龥々ぁ-ゖァ-ヺーｦ-ﾟ"


class FavoriteSongError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class YoutubeSongMetadata:
    video_id: str
    youtube_url: str
    youtube_short_url: str
    title: str
    artist: str
    raw_title: str


@dataclass(frozen=True, slots=True)
class LyricsMatch:
    title: str
    artist: str
    lyrics_url: str
    lyrics_text: str
    source_kind: str


@dataclass(frozen=True, slots=True)
class AnalyzedToken:
    sentence_index: int
    surface: str
    dictionary_form: str
    reading: str
    pos: str
    jlpt_level: str | None


@dataclass(frozen=True, slots=True)
class FavoriteSongIngestResult:
    song_id: int
    title: str
    artist: str
    youtube_short_url: str
    lyrics_url: str
    status: str
    sentence_count: int
    token_count: int
    n1_token_count: int
    reused_existing: bool = False


def extract_youtube_video_id(url: str) -> str | None:
    raw = (url or "").strip()
    if not raw:
        return None
    parsed = urlparse(raw)
    host = (parsed.netloc or "").lower()
    path = parsed.path or ""
    if host in {"youtu.be", "www.youtu.be"}:
        return path.lstrip("/").split("/")[0] or None
    if "youtube.com" in host:
        if path == "/watch":
            return (parse_qs(parsed.query).get("v") or [None])[0]
        if path.startswith("/shorts/") or path.startswith("/embed/"):
            return path.rstrip("/").split("/")[-1] or None
    return None


def build_youtube_short_url(video_id: str) -> str:
    return f"https://youtu.be/{video_id}"


def _normalize_title(text: str) -> str:
    s = unicodedata.normalize("NFKC", text or "").lower()
    s = s.replace("♯", "#").replace("＃", "#")
    s = re.sub(rf"[^{_JAPANESE_CHAR}a-z0-9#]+", "", s)
    return s


def _compact_japanese_spaces(text: str) -> str:
    s = re.sub(rf"(?<=[{_JAPANESE_CHAR}])[ \t]+(?=[{_JAPANESE_CHAR}])", "", text or "")
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


def _katakana_to_hiragana(text: str) -> str:
    chars: list[str] = []
    for ch in text or "":
        code = ord(ch)
        if 0x30A1 <= code <= 0x30F6:
            chars.append(chr(code - 0x60))
        else:
            chars.append(ch)
    return "".join(chars)


def _guess_song_title(raw_title: str, artist: str) -> str:
    text = unicodedata.normalize("NFKC", raw_title or "").strip()
    for pattern in (r"「([^」]+)」", r"『([^』]+)』", r"\"([^\"]+)\""):
        m = re.search(pattern, text)
        if m:
            picked = m.group(1).strip()
            if picked:
                return picked
    artist_norm = unicodedata.normalize("NFKC", artist or "").strip()
    if artist_norm and text.startswith(artist_norm):
        text = text[len(artist_norm):].strip(" 　-–—:：/／|")
    for noise in _TITLE_NOISE:
        idx = text.lower().find(noise)
        if idx > 0:
            text = text[:idx].strip(" 　-–—:：/／|")
    for sep in ("／", " / ", " - ", "｜", "|", "　-　"):
        if sep in text:
            left = text.split(sep, 1)[0].strip()
            if left:
                text = left
                break
    text = re.sub(r"\s+\([^)]*?(official|mv|music video|lyric).*?\)\s*$", "", text, flags=re.I)
    return text.strip(" 　-–—:：/／|") or raw_title.strip()


def split_lyrics_sentences(text: str) -> list[str]:
    normalized = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    pieces: list[str] = []
    for chunk in normalized.splitlines():
        line = chunk.strip()
        if not line:
            continue
        bits = re.split(r"(?<=[。！？!?])\s*", line)
        for bit in bits:
            cleaned = _compact_japanese_spaces(bit.strip())
            if cleaned:
                pieces.append(cleaned)
    return pieces


def _visible_text_without_rt(node: Tag | NavigableString) -> str:
    if isinstance(node, NavigableString):
        return str(node)
    if getattr(node, "name", None) == "rt":
        return ""
    if getattr(node, "name", None) == "br":
        return "\n"
    return "".join(_visible_text_without_rt(child) for child in node.children)


@lru_cache(maxsize=1)
def _sudachi_tokenizer():
    from sudachipy import dictionary, tokenizer  # type: ignore

    return dictionary.Dictionary(dict="full").create(mode=tokenizer.Tokenizer.SplitMode.C)


class RuleBasedJlptLexicon:
    """Reuse the existing quiz-backed lexical pool as a deterministic JLPT tagger."""

    def __init__(self, db) -> None:
        self._entries: dict[str, str] = {}
        with db.connect() as conn:
            for row in conn.execute(
                "SELECT headword, level FROM quiz_vocab_cards WHERE TRIM(headword) <> ''"
            ):
                self._entries[_normalize_title(row["headword"] or "")] = row["level"] or ""
            for row in conn.execute(
                "SELECT tested_point, level FROM quiz_questions "
                "WHERE tested_point IS NOT NULL AND TRIM(tested_point) <> ''"
            ):
                self._entries.setdefault(
                    _normalize_title(row["tested_point"] or ""),
                    row["level"] or "",
                )

    def classify(self, *values: str) -> str | None:
        for value in values:
            key = _normalize_title(value)
            if not key:
                continue
            level = self._entries.get(key)
            if level:
                return level.replace("JLPT ", "")
        return None


class SudachiLyricsAnalyzer:
    def __init__(self, *, jlpt_lexicon: RuleBasedJlptLexicon) -> None:
        self._tokenizer = _sudachi_tokenizer()
        self._jlpt_lexicon = jlpt_lexicon

    def analyze(self, sentences: list[str]) -> list[AnalyzedToken]:
        rows: list[AnalyzedToken] = []
        for sentence_index, sentence in enumerate(sentences):
            for morpheme in self._tokenizer.tokenize(sentence):
                surface = (morpheme.surface() or "").strip()
                if not surface:
                    continue
                dictionary_form = (morpheme.dictionary_form() or surface).strip()
                if dictionary_form == "*":
                    dictionary_form = surface
                reading = _katakana_to_hiragana((morpheme.reading_form() or "").strip())
                pos = ",".join(
                    part for part in morpheme.part_of_speech()[:_POS_JOIN_COUNT] if part and part != "*"
                )
                jlpt_level = self._jlpt_lexicon.classify(dictionary_form, surface)
                rows.append(
                    AnalyzedToken(
                        sentence_index=sentence_index,
                        surface=surface,
                        dictionary_form=dictionary_form,
                        reading=reading,
                        pos=pos,
                        jlpt_level=jlpt_level,
                    )
                )
        return rows


class FavoriteSongIngestor:
    def __init__(self, *, settings, db) -> None:
        self._settings = settings
        self._db = db
        self._session = requests.Session()
        self._session.headers.update({"User-Agent": _USER_AGENT})
        if getattr(settings, "openclaw_tls_insecure_skip_verify", False):
            self._session.verify = False
        elif getattr(settings, "openclaw_ca_bundle_path", None):
            self._session.verify = settings.openclaw_ca_bundle_path
        self._analyzer = SudachiLyricsAnalyzer(jlpt_lexicon=RuleBasedJlptLexicon(db))

    def ingest_youtube_song(self, youtube_url: str) -> FavoriteSongIngestResult:
        metadata = self._fetch_youtube_metadata(youtube_url)
        existing = self._db.get_favorite_song_by_youtube_short_url(metadata.youtube_short_url)
        if existing is not None and (existing["status"] or "") == "ready":
            counts = self._db.favorite_song_analysis_counts(int(existing["id"]))
            return FavoriteSongIngestResult(
                song_id=int(existing["id"]),
                title=(existing["title"] or metadata.title).strip(),
                artist=(existing["artist"] or metadata.artist).strip(),
                youtube_short_url=metadata.youtube_short_url,
                lyrics_url=(existing["lyrics_url"] or "").strip(),
                status="ready",
                sentence_count=counts["sentences"],
                token_count=counts["tokens"],
                n1_token_count=counts["n1_tokens"],
                reused_existing=True,
            )

        song_id = self._db.upsert_favorite_song(
            title=metadata.title,
            artist=metadata.artist,
            youtube_url=metadata.youtube_url,
            youtube_short_url=metadata.youtube_short_url,
            status="fetching",
            youtube_title_raw=metadata.raw_title,
            video_id=metadata.video_id,
        )
        try:
            lyrics = self._find_lyrics(title=metadata.title, artist=metadata.artist, video_id=metadata.video_id)
            sentences = split_lyrics_sentences(lyrics.lyrics_text)
            if not sentences:
                raise FavoriteSongError("歌詞切句後沒有可用句子")
            tokens = self._analyzer.analyze(sentences)
            self._db.replace_favorite_song_analysis(
                song_id=song_id,
                lyrics_url=lyrics.lyrics_url,
                lyrics_text=lyrics.lyrics_text,
                sentences=sentences,
                tokens=tokens,
                status="ready",
            )
            n1_count = sum(1 for token in tokens if token.jlpt_level == "N1")
            return FavoriteSongIngestResult(
                song_id=song_id,
                title=metadata.title,
                artist=metadata.artist,
                youtube_short_url=metadata.youtube_short_url,
                lyrics_url=lyrics.lyrics_url,
                status="ready",
                sentence_count=len(sentences),
                token_count=len(tokens),
                n1_token_count=n1_count,
            )
        except Exception as exc:
            self._db.mark_favorite_song_status(song_id=song_id, status="failed", last_error=str(exc))
            raise

    def _fetch_youtube_metadata(self, youtube_url: str) -> YoutubeSongMetadata:
        video_id = extract_youtube_video_id(youtube_url)
        if not video_id:
            raise FavoriteSongError("無法辨識 YouTube 影片網址")
        canonical_url = f"https://www.youtube.com/watch?v={video_id}"
        short_url = build_youtube_short_url(video_id)
        res = self._session.get(
            _YOUTUBE_OEMBED.format(url=quote(canonical_url, safe="")),
            timeout=_HTTP_TIMEOUT_SECONDS,
        )
        res.raise_for_status()
        payload = res.json()
        raw_title = str(payload.get("title") or "").strip()
        artist = str(payload.get("author_name") or "").strip()
        title = _guess_song_title(raw_title, artist)
        if not title:
            raise FavoriteSongError("YouTube metadata 缺歌曲標題")
        return YoutubeSongMetadata(
            video_id=video_id,
            youtube_url=canonical_url,
            youtube_short_url=short_url,
            title=title,
            artist=artist,
            raw_title=raw_title,
        )

    def _find_lyrics(self, *, title: str, artist: str, video_id: str) -> LyricsMatch:
        for finder in (self._find_vocadb_lyrics, self._find_utanet_lyrics, self._find_utaten_lyrics):
            result = finder(title=title, artist=artist, video_id=video_id)
            if result is not None:
                return result
        raise FavoriteSongError("找不到可用的歌詞全文來源")

    def _find_vocadb_lyrics(
        self, *, title: str, artist: str, video_id: str
    ) -> LyricsMatch | None:
        params = {
            "query": title,
            "maxResults": _MAX_SEARCH_CANDIDATES,
            "fields": "PVs,Lyrics,Artists,Names",
            "nameMatchMode": "Auto",
        }
        res = self._session.get(_VOCADB_SONGS_API, params=params, timeout=_HTTP_TIMEOUT_SECONDS)
        res.raise_for_status()
        items = (res.json().get("items") or []) if res.content else []
        best: tuple[int, dict] | None = None
        wanted_title = _normalize_title(title)
        wanted_artist = _normalize_title(artist)
        for item in items:
            if not isinstance(item, dict):
                continue
            score = 0
            names = {str(item.get("name") or "")}
            names |= {
                str(entry.get("value") or "")
                for entry in (item.get("names") or [])
                if isinstance(entry, dict)
            }
            normalized_names = {_normalize_title(name) for name in names if name}
            if any(video_id == str(pv.get("pvId") or "").strip() for pv in (item.get("pvs") or []) if isinstance(pv, dict)):
                score += 100
            if wanted_title and wanted_title in normalized_names:
                score += 30
            artist_string = str(item.get("artistString") or "")
            if wanted_artist and wanted_artist and wanted_artist in _normalize_title(artist_string):
                score += 10
            lyrics = item.get("lyrics") or []
            lyric_entry = _pick_vocadb_lyric_entry(lyrics)
            if lyric_entry is not None:
                score += 10
            if score <= 0:
                continue
            if best is None or score > best[0]:
                best = (score, item)
        if best is None:
            return None
        item = best[1]
        lyric_entry = _pick_vocadb_lyric_entry(item.get("lyrics") or [])
        if lyric_entry is None:
            return None
        lyrics_text = _compact_japanese_spaces(str(lyric_entry.get("value") or "").replace("／", "\n"))
        if not lyrics_text:
            return None
        return LyricsMatch(
            title=str(item.get("name") or title).strip() or title,
            artist=str(item.get("artistString") or artist).strip() or artist,
            lyrics_url=str(lyric_entry.get("url") or f"https://vocadb.net/S/{item.get('id')}"),
            lyrics_text=lyrics_text,
            source_kind="vocadb",
        )

    def _find_utanet_lyrics(
        self, *, title: str, artist: str, video_id: str
    ) -> LyricsMatch | None:
        del video_id
        search_html = self._session.get(
            _UTANET_SEARCH.format(query=quote(title)),
            timeout=_HTTP_TIMEOUT_SECONDS,
        )
        search_html.raise_for_status()
        soup = BeautifulSoup(search_html.text, "html.parser")
        seen: set[str] = set()
        candidates: list[str] = []
        for anchor in soup.select('a[href*="/song/"]'):
            href = (anchor.get("href") or "").strip()
            if not href or href in seen:
                continue
            seen.add(href)
            candidates.append(href)
            if len(candidates) >= _MAX_SEARCH_CANDIDATES:
                break
        return self._pick_best_utanet_candidate(candidates, title=title, artist=artist)

    def _pick_best_utanet_candidate(
        self, candidates: Iterable[str], *, title: str, artist: str
    ) -> LyricsMatch | None:
        best: tuple[int, LyricsMatch] | None = None
        want_title = _normalize_title(title)
        want_artist = _normalize_title(artist)
        for href in candidates:
            url = href if href.startswith("http") else f"https://www.uta-net.com{href}"
            res = self._session.get(url, timeout=_HTTP_TIMEOUT_SECONDS)
            res.raise_for_status()
            soup = BeautifulSoup(res.text, "html.parser")
            title_node = soup.select_one("h2")
            artist_node = soup.select_one('a[itemprop="byArtist"]') or soup.select_one("h3")
            lyrics_node = soup.select_one("#kashi_area")
            if title_node is None or artist_node is None or lyrics_node is None:
                continue
            found_title = " ".join(title_node.get_text(" ", strip=True).split())
            found_artist = " ".join(artist_node.get_text(" ", strip=True).split())
            lyrics_text = _compact_japanese_spaces(
                lyrics_node.get_text("\n", strip=True).replace("　", " ")
            )
            if not lyrics_text:
                continue
            score = 0
            norm_title = _normalize_title(found_title)
            norm_artist = _normalize_title(found_artist)
            if want_title and norm_title == want_title:
                score += 50
            elif want_title and want_title in norm_title:
                score += 20
            if want_artist and want_artist and want_artist in norm_artist:
                score += 15
            if score <= 0:
                continue
            match = LyricsMatch(
                title=found_title,
                artist=found_artist,
                lyrics_url=url,
                lyrics_text=lyrics_text,
                source_kind="uta-net",
            )
            if best is None or score > best[0]:
                best = (score, match)
        return best[1] if best else None

    def _find_utaten_lyrics(
        self, *, title: str, artist: str, video_id: str
    ) -> LyricsMatch | None:
        del video_id
        search_html = self._session.get(
            _UTATEN_SEARCH.format(query=quote(title)),
            timeout=_HTTP_TIMEOUT_SECONDS,
        )
        search_html.raise_for_status()
        soup = BeautifulSoup(search_html.text, "html.parser")
        seen: set[str] = set()
        candidates: list[str] = []
        for anchor in soup.select('a[href*="/lyric/"]'):
            href = (anchor.get("href") or "").strip()
            if not href or href in seen:
                continue
            seen.add(href)
            candidates.append(href)
            if len(candidates) >= _MAX_SEARCH_CANDIDATES:
                break
        best: tuple[int, LyricsMatch] | None = None
        want_title = _normalize_title(title)
        want_artist = _normalize_title(artist)
        for href in candidates:
            url = href if href.startswith("http") else f"https://utaten.com{href}"
            res = self._session.get(url, timeout=_HTTP_TIMEOUT_SECONDS)
            res.raise_for_status()
            soup = BeautifulSoup(res.text, "html.parser")
            title_node = soup.select_one(".newLyricTitle") or soup.select_one("h1")
            artist_node = soup.select_one('a[href*="/artist/"]')
            lyrics_node = soup.select_one(".hiragana") or soup.select_one(".medium") or soup.select_one(".lyricBody")
            if title_node is None or artist_node is None or lyrics_node is None:
                continue
            raw_title = " ".join(title_node.get_text(" ", strip=True).split())
            found_title = _extract_utaten_song_title(raw_title) or title
            found_artist = " ".join(artist_node.get_text(" ", strip=True).split())
            lyrics_text = _compact_japanese_spaces(_visible_text_without_rt(lyrics_node))
            if not lyrics_text:
                continue
            score = 0
            norm_title = _normalize_title(found_title)
            norm_artist = _normalize_title(found_artist)
            if want_title and norm_title == want_title:
                score += 50
            elif want_title and want_title in norm_title:
                score += 20
            if want_artist and want_artist and want_artist in norm_artist:
                score += 15
            if score <= 0:
                continue
            match = LyricsMatch(
                title=found_title,
                artist=found_artist,
                lyrics_url=url,
                lyrics_text=lyrics_text,
                source_kind="utaten",
            )
            if best is None or score > best[0]:
                best = (score, match)
        return best[1] if best else None


def _extract_utaten_song_title(raw_title: str) -> str:
    text = unicodedata.normalize("NFKC", raw_title or "")
    text = re.sub(r"^よみ：.*?\s+", "", text)
    text = re.sub(r"\s+歌詞.*$", "", text)
    return text.strip()


def _pick_vocadb_lyric_entry(lyrics: Iterable[dict]) -> dict | None:
    best: dict | None = None
    for entry in lyrics:
        if not isinstance(entry, dict):
            continue
        codes = entry.get("cultureCodes") or []
        is_ja = "ja" in codes or not codes
        if not is_ja:
            continue
        kind = str(entry.get("translationType") or "").lower()
        if kind == "original":
            return entry
        if best is None:
            best = entry
    return best
