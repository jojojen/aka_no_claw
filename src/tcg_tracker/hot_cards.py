from __future__ import annotations

import math
import re
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Iterable
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from market_monitor.http import HttpClient
from market_monitor.normalize import normalize_card_number, normalize_text

from .catalog import TcgCardSpec

CARDRUSH_POKEMON_RANKING_URL = "https://www.cardrush-pokemon.jp/product-group/22?sort=rank&num=100"
MAGI_WS_RANKING_URL = "https://magi.camp/series/7/products"

CARDRUSH_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/135.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,image/apng,*/*;q=0.8"
    ),
    "Accept-Language": "ja-JP,ja;q=0.9,en-US;q=0.8,en;q=0.7",
    "Cache-Control": "max-age=0",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Sec-CH-UA": '"Google Chrome";v="135", "Not-A.Brand";v="8", "Chromium";v="135"',
    "Sec-CH-UA-Mobile": "?0",
    "Sec-CH-UA-Platform": '"Windows"',
}

JPY_PRICE_RE = re.compile(r"(?P<price>\d[\d,]*)円")
MAGI_PRICE_RE = re.compile(r"¥\s*(?P<price>\d[\d,]*)")
COUNT_RE = re.compile(r"(?:在庫数|出品数)\s*(?P<count>\d+)")
GRADING_RE = re.compile(r"^(?:【|〖)(?P<label>(?:PSA|BGS|ARS|CGC)[^】〗]*)(?:】|〗)")
CARDRUSH_STATE_RE = re.compile(r"^〔(?P<label>[^〕]+)〕")
CARDRUSH_RARITY_RE = re.compile(r"【(?P<label>[^】]+)】")
CARDRUSH_SET_CODE_RE = re.compile(r"\[\s*(?:\[[^\]]+\]\s*)?(?P<code>[A-Za-z0-9]+)\s*\]")
WS_CODE_RE = re.compile(r"(?P<code>[A-Z0-9]+/[A-Z0-9-]+-[A-Z0-9]+)$")
POKEMON_CODE_RE = re.compile(r"\{(?P<code>[^}]+)\}")
POKEMON_INLINE_CODE_RE = re.compile(r"(?P<code>\d{1,3}/\d{1,3})$")


@dataclass(frozen=True, slots=True)
class HotCardReference:
    label: str
    url: str


@dataclass(frozen=True, slots=True)
class HotCardEntry:
    game: str
    rank: int
    title: str
    price_jpy: int | None
    card_number: str | None
    rarity: str | None
    set_code: str | None
    listing_count: int | None
    hot_score: float
    notes: tuple[str, ...]
    is_graded: bool
    references: tuple[HotCardReference, ...]


@dataclass(frozen=True, slots=True)
class HotCardBoard:
    game: str
    label: str
    methodology: str
    generated_at: datetime
    items: tuple[HotCardEntry, ...]


@dataclass(frozen=True, slots=True)
class TcgLookupHint:
    game: str
    title: str
    card_number: str | None
    rarity: str | None
    set_code: str | None
    listing_count: int | None
    confidence: float
    references: tuple[HotCardReference, ...]


@dataclass
class _ParsedHotItem:
    title: str
    price_jpy: int | None
    card_number: str | None
    rarity: str | None
    set_code: str | None
    listing_count: int | None
    is_graded: bool
    condition: str | None
    detail_url: str
    board_url: str
    note: str


class TcgHotCardService:
    def __init__(self, http_client: HttpClient | None = None) -> None:
        self.http_client = http_client or HttpClient()

    def load_boards(self) -> tuple[HotCardBoard, ...]:
        return (
            self.load_pokemon_board(),
            self.load_ws_board(),
        )

    def load_pokemon_board(self, *, limit: int = 10) -> HotCardBoard:
        html = self.http_client.get_text(
            CARDRUSH_POKEMON_RANKING_URL,
            headers=CARDRUSH_BROWSER_HEADERS,
        )
        items = self._build_ranked_entries(
            game="pokemon",
            parsed_items=self._parse_cardrush_pokemon_items(html),
            limit=limit,
        )
        return HotCardBoard(
            game="pokemon",
            label="Pokemon Liquidity Top 10",
            methodology=(
                "Ranks Pokemon cards by observed Cardrush stock depth first, then uses the source's "
                "best-seller order as a secondary tie-breaker. Duplicates across condition variants are merged, "
                "and zero-stock entries are treated as lower-liquidity fallbacks."
            ),
            generated_at=datetime.now(timezone.utc),
            items=items,
        )

    def load_ws_board(self, *, limit: int = 10) -> HotCardBoard:
        html = self.http_client.get_text(MAGI_WS_RANKING_URL)
        items = self._build_ranked_entries(
            game="ws",
            parsed_items=self._parse_magi_ws_items(html),
            limit=limit,
        )
        return HotCardBoard(
            game="ws",
            label="WS Liquidity Top 10",
            methodology=(
                "Ranks Weiss Schwarz cards by active Magi listing count first, then uses the source page order "
                "as a secondary tie-breaker. Grade variants are merged under the same card, raw listings are "
                "preferred over graded copies, and zero-listing entries are treated as lower-liquidity fallbacks."
            ),
            generated_at=datetime.now(timezone.utc),
            items=items,
        )

    def resolve_lookup_spec(self, spec: TcgCardSpec) -> TcgCardSpec | None:
        if spec.game not in {"pokemon", "ws"}:
            return None
        if spec.card_number:
            return None
        if not any((spec.rarity, spec.set_code, spec.set_name)):
            return None

        hints = self.search_lookup_hints(spec, limit=2)
        if not hints:
            return None

        best_hint = hints[0]
        if best_hint.confidence < 26.0:
            return None

        if len(hints) > 1 and hints[1].confidence >= best_hint.confidence - 6.0:
            return None

        aliases = list(spec.aliases)
        if best_hint.title != spec.title and best_hint.title not in aliases:
            aliases.append(best_hint.title)

        return replace(
            spec,
            title=best_hint.title,
            card_number=best_hint.card_number or spec.card_number,
            rarity=spec.rarity or best_hint.rarity,
            set_code=spec.set_code or best_hint.set_code,
            aliases=tuple(aliases),
        )

    def search_lookup_hints(self, spec: TcgCardSpec, *, limit: int = 5) -> tuple[TcgLookupHint, ...]:
        parsed_items = self._load_source_items(spec.game)
        ranked: list[tuple[float, _ParsedHotItem]] = []
        for item in parsed_items:
            confidence = self._hint_score(spec, item)
            if confidence < 18.0:
                continue
            ranked.append((confidence, item))

        ranked.sort(
            key=lambda value: (
                value[0],
                value[1].listing_count or 0,
                0 if not value[1].is_graded else -1,
                value[1].title,
            ),
            reverse=True,
        )

        return tuple(
            TcgLookupHint(
                game=spec.game,
                title=item.title,
                card_number=item.card_number,
                rarity=item.rarity,
                set_code=item.set_code,
                listing_count=item.listing_count,
                confidence=confidence,
                references=(
                    HotCardReference(label="Ranking Source", url=item.board_url),
                    HotCardReference(label="Item Page", url=item.detail_url),
                ),
            )
            for confidence, item in ranked[:limit]
        )

    def _build_ranked_entries(
        self,
        *,
        game: str,
        parsed_items: Iterable[_ParsedHotItem],
        limit: int,
    ) -> tuple[HotCardEntry, ...]:
        aggregates: dict[str, dict[str, object]] = {}
        for source_rank, item in enumerate(parsed_items, start=1):
            key = self._hot_item_key(game, item)
            entry = aggregates.get(key)
            if entry is None:
                aggregates[key] = {
                    "best_rank": source_rank,
                    "best_item": item,
                    "total_count": item.listing_count or 0,
                }
                continue

            entry["best_rank"] = min(int(entry["best_rank"]), source_rank)
            entry["total_count"] = int(entry["total_count"]) + (item.listing_count or 0)
            if self._prefer_item(item, entry["best_item"]):  # type: ignore[arg-type]
                entry["best_item"] = item

        ranked = sorted(
            aggregates.values(),
            key=lambda value: self._liquidity_sort_key(
                best_item=value["best_item"],  # type: ignore[arg-type]
                best_rank=int(value["best_rank"]),
                total_count=int(value["total_count"]),
            ),
        )

        items: list[HotCardEntry] = []
        for display_rank, aggregate in enumerate(ranked[:limit], start=1):
            best_item: _ParsedHotItem = aggregate["best_item"]  # type: ignore[assignment]
            best_rank = int(aggregate["best_rank"])
            total_count = int(aggregate["total_count"])
            notes = [
                best_item.note,
            ]
            if total_count > 0:
                notes.append(
                    f"Primary liquidity signal: {total_count} active listing(s) / stock unit(s) observed."
                )
                notes.append(f"Secondary tie-breaker: source visibility rank #{best_rank}.")
            else:
                notes.append(
                    "No active listing count is currently visible on the source; this entry is a lower-confidence fallback."
                )
                notes.append(f"Fallback visibility signal: source rank #{best_rank}.")
            if best_item.is_graded:
                notes.append("Graded copies are treated as less fungible than raw copies for liquidity ranking.")

            items.append(
                HotCardEntry(
                    game=game,
                    rank=display_rank,
                    title=best_item.title,
                    price_jpy=best_item.price_jpy,
                    card_number=best_item.card_number,
                    rarity=best_item.rarity,
                    set_code=best_item.set_code,
                    listing_count=total_count or None,
                    hot_score=self._hot_score(best_rank, total_count, best_item.is_graded),
                    notes=tuple(notes),
                    is_graded=best_item.is_graded,
                    references=(
                        HotCardReference(label="Ranking Source", url=best_item.board_url),
                        HotCardReference(label="Item Page", url=best_item.detail_url),
                    ),
                )
            )
        return tuple(items)

    def _load_source_items(self, game: str) -> list[_ParsedHotItem]:
        if game == "pokemon":
            html = self.http_client.get_text(
                CARDRUSH_POKEMON_RANKING_URL,
                headers=CARDRUSH_BROWSER_HEADERS,
            )
            return self._parse_cardrush_pokemon_items(html)
        if game == "ws":
            html = self.http_client.get_text(MAGI_WS_RANKING_URL)
            return self._parse_magi_ws_items(html)
        return []

    @staticmethod
    def _prefer_item(candidate: _ParsedHotItem, current: _ParsedHotItem) -> bool:
        candidate_priority = _condition_priority(candidate.condition, candidate.is_graded)
        current_priority = _condition_priority(current.condition, current.is_graded)
        if candidate_priority != current_priority:
            return candidate_priority > current_priority
        if candidate.price_jpy is None:
            return False
        if current.price_jpy is None:
            return True
        return candidate.price_jpy < current.price_jpy

    @staticmethod
    def _liquidity_sort_key(
        *,
        best_item: _ParsedHotItem,
        best_rank: int,
        total_count: int,
    ) -> tuple[object, ...]:
        return (
            0 if total_count > 0 else 1,
            -total_count,
            1 if best_item.is_graded else 0,
            best_rank,
            normalize_text(best_item.title),
        )

    @staticmethod
    def _hot_score(best_rank: int, total_count: int, is_graded: bool) -> float:
        depth_component = math.log1p(max(total_count, 0)) * 32.0
        visibility_component = max(0.0, 14.0 - best_rank * 0.35)
        fungibility_component = -8.0 if is_graded else 5.0
        inactivity_penalty = -18.0 if total_count <= 0 else 0.0
        return round(
            max(0.0, depth_component + visibility_component + fungibility_component + inactivity_penalty),
            2,
        )

    @staticmethod
    def _hint_score(spec: TcgCardSpec, item: _ParsedHotItem) -> float:
        query_title = _title_key(spec.game, spec.title)
        item_title = _title_key(spec.game, item.title)
        base_query_title = _title_key(spec.game, spec.title, drop_game_suffixes=True)
        base_item_title = _title_key(spec.game, item.title, drop_game_suffixes=True)

        score = 0.0
        if query_title == item_title:
            score += 34.0
        elif query_title and (query_title in item_title or item_title in query_title):
            score += 24.0

        if base_query_title == base_item_title:
            score += 18.0
        elif base_query_title and (base_query_title in base_item_title or base_item_title in base_query_title):
            score += 10.0

        if spec.card_number and item.card_number:
            if normalize_card_number(spec.card_number) == normalize_card_number(item.card_number):
                score += 40.0
            else:
                score -= 20.0

        if spec.rarity and item.rarity:
            if normalize_text(spec.rarity) == normalize_text(item.rarity):
                score += 16.0
            else:
                score -= 6.0

        if spec.set_code and item.set_code:
            if normalize_text(spec.set_code) == normalize_text(item.set_code):
                score += 10.0
            else:
                score -= 4.0

        score += min(item.listing_count or 0, 250) * 0.03
        if item.is_graded:
            score -= 4.0
        return score

    @staticmethod
    def _hot_item_key(game: str, item: _ParsedHotItem) -> str:
        card_number = normalize_card_number(item.card_number or "")
        rarity = normalize_text(item.rarity or "")
        title = normalize_text(item.title)
        return "|".join([game, title, card_number, rarity])

    def _parse_cardrush_pokemon_items(self, html: str) -> list[_ParsedHotItem]:
        soup = BeautifulSoup(html, "html.parser")
        items: list[_ParsedHotItem] = []
        for anchor in soup.select("ul.item_list li.list_item_cell div.item_data a[href]"):
            text = " ".join(anchor.get_text(" ", strip=True).split())
            if not text:
                continue
            parsed = _parse_cardrush_text(
                text,
                detail_url=anchor["href"],
                board_url=CARDRUSH_POKEMON_RANKING_URL,
            )
            if parsed is not None:
                items.append(parsed)
        return items

    def _parse_magi_ws_items(self, html: str) -> list[_ParsedHotItem]:
        soup = BeautifulSoup(html, "html.parser")
        items: list[_ParsedHotItem] = []
        for anchor in soup.select("div.product-list__box a[href^='/products/']"):
            text = " ".join(anchor.get_text(" ", strip=True).split())
            if not text:
                continue
            parsed = _parse_magi_text(
                text,
                detail_url=urljoin("https://magi.camp", anchor["href"]),
                board_url=MAGI_WS_RANKING_URL,
            )
            if parsed is not None:
                items.append(parsed)
        return items


def _parse_cardrush_text(text: str, *, detail_url: str, board_url: str) -> _ParsedHotItem | None:
    condition = None
    working = text
    condition_match = CARDRUSH_STATE_RE.match(working)
    if condition_match is not None:
        condition = condition_match.group("label")
        working = working[condition_match.end():].strip()

    rarity_match = CARDRUSH_RARITY_RE.search(working)
    rarity = rarity_match.group("label") if rarity_match is not None else None
    title = working[: rarity_match.start()].strip() if rarity_match is not None else working

    card_number_match = POKEMON_CODE_RE.search(working)
    card_number = card_number_match.group("code").strip() if card_number_match is not None else None
    set_code_match = CARDRUSH_SET_CODE_RE.search(working)
    set_code = set_code_match.group("code").lower() if set_code_match is not None else None

    price_match = JPY_PRICE_RE.search(working)
    price_jpy = int(price_match.group("price").replace(",", "")) if price_match is not None else None

    count_match = COUNT_RE.search(working)
    listing_count = int(count_match.group("count")) if count_match is not None else None

    if not title:
        return None

    return _ParsedHotItem(
        title=title,
        price_jpy=price_jpy,
        card_number=card_number,
        rarity=rarity,
        set_code=set_code,
        listing_count=listing_count,
        is_graded=False,
        condition=condition,
        detail_url=detail_url,
        board_url=board_url,
        note="Signal source: Cardrush best-seller order within the current high-rarity singles category.",
    )


def _parse_magi_text(text: str, *, detail_url: str, board_url: str) -> _ParsedHotItem | None:
    grading_match = GRADING_RE.match(text)
    is_graded = grading_match is not None
    working = text[grading_match.end():].strip() if grading_match is not None else text

    price_match = MAGI_PRICE_RE.search(working)
    price_jpy = int(price_match.group("price").replace(",", "")) if price_match is not None else None

    count_match = COUNT_RE.search(working)
    listing_count = int(count_match.group("count")) if count_match is not None else None

    body_end = price_match.start() if price_match is not None else working.find("- 出品数")
    if body_end == -1:
        body_end = len(working)
    body = working[:body_end].strip()

    card_number = None
    rarity = None
    set_code = None
    ws_code_match = WS_CODE_RE.search(body)
    if ws_code_match is not None:
        card_number = ws_code_match.group("code")
        set_code = card_number.split("/", 1)[0].lower()
        prefix = body[: ws_code_match.start()].strip()
        title, rarity = _split_title_and_rarity(prefix)
    else:
        pokemon_code_match = POKEMON_INLINE_CODE_RE.search(body)
        if pokemon_code_match is not None:
            card_number = pokemon_code_match.group("code").strip()
            prefix = body[: pokemon_code_match.start()].strip()
            title, rarity = _split_title_and_rarity(prefix)
        else:
            title = body

    if not title:
        return None

    return _ParsedHotItem(
        title=title,
        price_jpy=price_jpy,
        card_number=card_number,
        rarity=rarity,
        set_code=set_code,
        listing_count=listing_count,
        is_graded=is_graded,
        condition=None,
        detail_url=detail_url,
        board_url=board_url,
        note="Signal source: Magi popular/recommended Weiss Schwarz page order.",
    )


def _split_title_and_rarity(prefix: str) -> tuple[str, str | None]:
    parts = prefix.rsplit(" ", 1)
    if len(parts) == 2 and _looks_like_rarity(parts[1]):
        return parts[0].strip(), parts[1].strip()
    return prefix.strip(), None


def _looks_like_rarity(token: str) -> bool:
    value = token.strip().upper()
    if not value or len(value) > 6:
        return False
    return value.isalnum()


def _condition_priority(condition: str | None, is_graded: bool) -> int:
    if is_graded:
        return 0
    if condition is None:
        return 4
    normalized = normalize_text(condition)
    if "状態a" in normalized:
        return 3
    if "状態b" in normalized:
        return 2
    if "状態c" in normalized:
        return 1
    return 1


def _title_key(game: str, title: str, *, drop_game_suffixes: bool = False) -> str:
    normalized = normalize_text(title)
    if game == "pokemon" and drop_game_suffixes:
        normalized = normalized.removesuffix("ex")
    return normalized
