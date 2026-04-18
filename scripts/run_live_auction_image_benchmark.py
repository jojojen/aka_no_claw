from __future__ import annotations

import argparse
import hashlib
import json
import random
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable
from urllib.request import Request, urlopen

from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from assistant_runtime.settings import get_settings, load_dotenv
from market_monitor.http import HttpClient
from tcg_tracker.image_lookup import TcgImagePriceService

YAHOO_AUCTIONS_SEARCH_URL = "https://auctions.yahoo.co.jp/search/search"
YAHOO_AUCTIONS_BROWSER_HEADERS = {
    "Accept-Language": "ja-JP,ja;q=0.9",
    "Cache-Control": "no-cache",
    "Referer": "https://auctions.yahoo.co.jp/",
}
DEFAULT_PAGE_OFFSETS = (1, 51, 101, 151, 201, 251, 301, 351, 401, 451)
POKEMON_QUERIES = ("ポケモンカード", "ポケカ")
WS_QUERIES = ("ヴァイスシュヴァルツ",)

POKEMON_POSITIVE_TITLE_KEYWORDS = (
    "ポケモン",
    "ポケカ",
    "pokemon",
    "sar",
    "sr",
    "ar",
    "ur",
    "chr",
    "csr",
    "ssr",
    "ex",
    "gx",
    "vmax",
    "vstar",
    "sv-p",
    "s-p",
    "sm-p",
    "xy-p",
    "bw-p",
)
WS_POSITIVE_TITLE_KEYWORDS = (
    "ヴァイス",
    "シュヴァルツ",
    "weiss",
    "schwarz",
    "ssp",
    "sp",
    "ofr",
    "sec",
    "rrr",
    "ws/",
)
POKEMON_CROSS_GAME_KEYWORDS = ("ヴァイス", "weiss", "schwarz", "ws/")
WS_CROSS_GAME_KEYWORDS = ("ポケモン", "ポケカ", "pokemon", "sv-p", "s-p", "sm-p", "xy-p", "bw-p")

POKEMON_NEGATIVE_TITLE_KEYWORDS = (
    "box",
    "boxセット",
    "box未開封",
    "シュリンク",
    "スターターセット",
    "デッキ",
    "パック",
    "ファイル",
    "プレイマット",
    "フィギュア",
    "まとめ",
    "引退",
    "空箱",
    "空パック",
    "ケース",
    "サプライ",
    "シール",
    "スリーブ",
    "ぬいぐるみ",
    "プロモパック",
    "未開封box",
    "未開封パック",
    "未開封ボックス",
    "未開封パック",
    "福袋",
    "まとめ売り",
    "引退品",
    "大量",
    "デッキパーツ",
    "連番",
    "未開封",
    "観賞用",
    "ファンアート",
)
WS_NEGATIVE_TITLE_KEYWORDS = (
    "box",
    "rr以下4コン",
    "4コン",
    "デッキ",
    "パック",
    "プレイマット",
    "プロテイン",
    "まとめ",
    "引退",
    "未開封box",
    "未開封パック",
    "未開封ボックス",
    "カートン",
    "ケース",
    "サプライ",
    "スリーブ",
    "セット",
    "ブースター",
    "まとめ売り",
    "引退品",
    "大量",
    "連番",
    "未開封",
    "portable",
    "psp",
    "ps vita",
    "vita",
    "switch",
    "ゲームソフト",
    "ソフト",
    "blu-ray",
    "dvd",
    "フィギュア",
)
MULTI_CARD_COUNT_RE = re.compile(r"(?:[0-9０-９]+\s*枚|[×xX＊*]\s*\d+|\d+\s*個)")
POKEMON_SINGLE_CARD_HINT_RE = re.compile(
    r"(?:(?:\d{1,3}\s*/\s*\d{1,3})|(?:\d{1,3}\s*/\s*(?:SV|SM|XY|BW|S|M)-?P)|(?:SAR|SR|AR|UR|CHR|CSR|SSR|PROMO)\b)",
    re.IGNORECASE,
)
WS_SINGLE_CARD_HINT_RE = re.compile(
    r"(?:(?:[A-Z0-9]{1,6}/[A-Z0-9]{1,6}-\d{2,3}[A-Z]{0,4})|(?:SSP|SP|OFR|SEC\+|SEC|RRR|PR)\b)",
    re.IGNORECASE,
)
_THREAD_LOCAL = threading.local()


@dataclass(frozen=True, slots=True)
class ListingCandidate:
    benchmark_game: str
    source: str
    query: str
    page_offset: int
    listing_url: str
    image_url: str
    thumbnail_url: str
    title: str


@dataclass(frozen=True, slots=True)
class BenchmarkResult:
    benchmark_game: str
    source: str
    query: str
    page_offset: int
    listing_url: str
    title: str
    image_url: str
    local_path: str
    elapsed_seconds: float
    status: str
    parsed_game: str | None
    parsed_title: str | None
    parsed_card_number: str | None
    parsed_rarity: str | None
    parsed_set_code: str | None
    offer_count: int
    fair_value_jpy: int | None
    warnings: tuple[str, ...]


def main() -> int:
    parser = argparse.ArgumentParser(description="Run a live auction image benchmark against Yahoo! Auctions card listings.")
    parser.add_argument("--sample-size-per-game", type=int, default=100)
    parser.add_argument("--seed", type=int, default=20260418)
    parser.add_argument("--output-dir", type=Path, default=ROOT / ".openclaw_tmp" / "live_auction_benchmark")
    parser.add_argument("--report-path", type=Path, default=ROOT / "reports" / "live_auction_benchmark_2026-04-18.md")
    parser.add_argument("--pokemon-queries", nargs="+", default=list(POKEMON_QUERIES))
    parser.add_argument("--ws-queries", nargs="+", default=list(WS_QUERIES))
    parser.add_argument("--page-offsets", nargs="+", type=int, default=list(DEFAULT_PAGE_OFFSETS))
    parser.add_argument("--max-workers", type=int, default=2)
    args = parser.parse_args()

    _configure_stdout()
    load_dotenv()
    settings = get_settings()
    http_client = HttpClient(user_agent=settings.yuyutei_user_agent, timeout_seconds=30)
    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "images").mkdir(parents=True, exist_ok=True)
    (output_dir / "pages").mkdir(parents=True, exist_ok=True)
    args.report_path.parent.mkdir(parents=True, exist_ok=True)

    rng = random.Random(args.seed)
    benchmark_plan = {
        "pokemon": tuple(args.pokemon_queries),
        "ws": tuple(args.ws_queries),
    }

    print("Collecting Yahoo! Auctions candidates...", flush=True)
    candidates_by_game: dict[str, list[ListingCandidate]] = {}
    for benchmark_game, queries in benchmark_plan.items():
        candidates = collect_yahoo_auction_candidates(
            http_client=http_client,
            benchmark_game=benchmark_game,
            queries=queries,
            page_offsets=tuple(args.page_offsets),
            cache_dir=output_dir / "pages",
        )
        filtered_candidates = [candidate for candidate in candidates if _candidate_is_relevant(candidate)]
        filtered_candidates = filter_candidates_by_image_shape(
            candidates=filtered_candidates,
            http_client=http_client,
            image_dir=output_dir / "images",
        )
        if len(filtered_candidates) < args.sample_size_per_game:
            raise RuntimeError(
                f"Only collected {len(filtered_candidates)} relevant {benchmark_game} candidates, need {args.sample_size_per_game}."
            )
        selected = rng.sample(filtered_candidates, args.sample_size_per_game)
        selected.sort(key=lambda candidate: (candidate.query, candidate.page_offset, candidate.listing_url))
        candidates_by_game[benchmark_game] = selected
        print(
            f"- {benchmark_game}: collected {len(candidates)} raw / {len(filtered_candidates)} relevant / {len(selected)} selected",
            flush=True,
        )

    print("\nDownloading images and running lookup benchmark...", flush=True)
    benchmark_results: list[BenchmarkResult] = []
    evaluation_jobs: list[tuple[str, int, int, ListingCandidate]] = []
    for benchmark_game in ("pokemon", "ws"):
        selected_candidates = candidates_by_game[benchmark_game]
        print(f"\n[{benchmark_game}] {len(selected_candidates)} listings", flush=True)
        evaluation_jobs.extend(
            (benchmark_game, index, len(selected_candidates), candidate)
            for index, candidate in enumerate(selected_candidates, start=1)
        )

    if max(1, args.max_workers) == 1:
        image_service = TcgImagePriceService(db_path=settings.monitor_db_path, settings=settings)
        for benchmark_game, index, total, candidate in evaluation_jobs:
            result = evaluate_candidate(
                candidate=candidate,
                image_service=image_service,
                http_client=http_client,
                image_dir=output_dir / "images",
            )
            benchmark_results.append(result)
            print(_render_progress_line(benchmark_game, index, total, result), flush=True)
    else:
        with ThreadPoolExecutor(max_workers=max(1, args.max_workers)) as executor:
            future_map = {
                executor.submit(
                    evaluate_candidate_threadsafe,
                    candidate=candidate,
                    settings=settings,
                    image_dir=output_dir / "images",
                ): (benchmark_game, index, total)
                for benchmark_game, index, total, candidate in evaluation_jobs
            }
            for future in as_completed(future_map):
                benchmark_game, index, total = future_map[future]
                result = future.result()
                benchmark_results.append(result)
                print(_render_progress_line(benchmark_game, index, total, result), flush=True)
        benchmark_results.sort(
            key=lambda result: (
                0 if result.benchmark_game == "pokemon" else 1,
                result.query,
                result.page_offset,
                result.listing_url,
            )
        )

    results_path = output_dir / "results.json"
    results_path.write_text(
        json.dumps([asdict(result) for result in benchmark_results], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    summary = build_summary(benchmark_results)
    summary_path = output_dir / "summary.json"
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    markdown = render_markdown_report(
        summary=summary,
        results=benchmark_results,
        sample_size_per_game=args.sample_size_per_game,
        seed=args.seed,
        page_offsets=tuple(args.page_offsets),
        queries=benchmark_plan,
        generated_at=time.strftime("%Y-%m-%d %H:%M:%S"),
    )
    args.report_path.write_text(markdown, encoding="utf-8")

    print("\nBenchmark completed.", flush=True)
    print(f"- Raw results: {results_path}", flush=True)
    print(f"- Summary JSON: {summary_path}", flush=True)
    print(f"- Markdown report: {args.report_path}", flush=True)
    return 0


def collect_yahoo_auction_candidates(
    *,
    http_client: HttpClient,
    benchmark_game: str,
    queries: Iterable[str],
    page_offsets: tuple[int, ...],
    cache_dir: Path,
) -> list[ListingCandidate]:
    candidates: list[ListingCandidate] = []
    seen_listing_urls: set[str] = set()
    for query in queries:
        for page_offset in page_offsets:
            cache_key = hashlib.sha1(f"{benchmark_game}|{query}|{page_offset}".encode("utf-8")).hexdigest()[:16]
            cache_path = cache_dir / f"{benchmark_game}-{cache_key}.html"
            if cache_path.exists():
                html = cache_path.read_text(encoding="utf-8", errors="replace")
            else:
                html = http_client.get_text(
                    YAHOO_AUCTIONS_SEARCH_URL,
                    params={"p": query, "b": str(page_offset)},
                    headers=YAHOO_AUCTIONS_BROWSER_HEADERS,
                )
                cache_path.write_text(html, encoding="utf-8")

            for candidate in _parse_yahoo_search_page(
                html=html,
                benchmark_game=benchmark_game,
                query=query,
                page_offset=page_offset,
            ):
                if candidate.listing_url in seen_listing_urls:
                    continue
                seen_listing_urls.add(candidate.listing_url)
                candidates.append(candidate)
    return candidates


def _parse_yahoo_search_page(
    *,
    html: str,
    benchmark_game: str,
    query: str,
    page_offset: int,
) -> list[ListingCandidate]:
    soup = BeautifulSoup(html, "html.parser")
    parsed: list[ListingCandidate] = []
    for product in soup.select("li.Product"):
        image_link = product.select_one("a.Product__imageLink[href]")
        image = product.select_one("img.Product__imageData[src]")
        title_link = product.select_one("a.Product__titleLink[href], .Product__title a[href]")
        if image_link is None or image is None:
            continue
        listing_url = image_link.get("href", "").strip()
        if "/jp/auction/" not in listing_url:
            continue
        thumbnail_url = image.get("src", "").strip()
        image_url = thumbnail_url.split("?", 1)[0]
        if not image_url:
            continue
        title = ""
        if title_link is not None:
            title = title_link.get_text(" ", strip=True)
        if not title:
            title = image.get("alt", "").strip()
        if not title:
            continue
        parsed.append(
            ListingCandidate(
                benchmark_game=benchmark_game,
                source="yahoo_auctions",
                query=query,
                page_offset=page_offset,
                listing_url=listing_url,
                image_url=image_url,
                thumbnail_url=thumbnail_url,
                title=title,
            )
        )
    return parsed


def evaluate_candidate(
    *,
    candidate: ListingCandidate,
    image_service: TcgImagePriceService,
    http_client: HttpClient,
    image_dir: Path,
) -> BenchmarkResult:
    image_path = image_dir / _candidate_filename(candidate)
    if not image_path.exists():
        _download_binary(http_client=http_client, url=candidate.image_url, destination=image_path)

    started_at = time.perf_counter()
    outcome = image_service.lookup_image(image_path, persist=False)
    elapsed_seconds = time.perf_counter() - started_at
    offer_count = len(outcome.lookup_result.offers) if outcome.lookup_result is not None else 0
    fair_value_jpy = (
        outcome.lookup_result.fair_value.amount_jpy
        if outcome.lookup_result is not None and outcome.lookup_result.fair_value is not None
        else None
    )
    return BenchmarkResult(
        benchmark_game=candidate.benchmark_game,
        source=candidate.source,
        query=candidate.query,
        page_offset=candidate.page_offset,
        listing_url=candidate.listing_url,
        title=candidate.title,
        image_url=candidate.image_url,
        local_path=str(image_path),
        elapsed_seconds=round(elapsed_seconds, 2),
        status=outcome.status,
        parsed_game=outcome.parsed.game,
        parsed_title=outcome.parsed.title,
        parsed_card_number=outcome.parsed.card_number,
        parsed_rarity=outcome.parsed.rarity,
        parsed_set_code=outcome.parsed.set_code,
        offer_count=offer_count,
        fair_value_jpy=fair_value_jpy,
        warnings=tuple(outcome.warnings[:5]),
    )


def evaluate_candidate_threadsafe(
    *,
    candidate: ListingCandidate,
    settings,
    image_dir: Path,
) -> BenchmarkResult:
    runtime = getattr(_THREAD_LOCAL, "runtime", None)
    if runtime is None:
        runtime = {
            "http_client": HttpClient(user_agent=settings.yuyutei_user_agent, timeout_seconds=30),
            "image_service": TcgImagePriceService(db_path=settings.monitor_db_path, settings=settings),
        }
        _THREAD_LOCAL.runtime = runtime
    return evaluate_candidate(
        candidate=candidate,
        image_service=runtime["image_service"],
        http_client=runtime["http_client"],
        image_dir=image_dir,
    )


def filter_candidates_by_image_shape(
    *,
    candidates: Iterable[ListingCandidate],
    http_client: HttpClient,
    image_dir: Path,
) -> list[ListingCandidate]:
    try:
        from PIL import Image, ImageOps
    except ImportError:
        return list(candidates)

    filtered: list[ListingCandidate] = []
    for candidate in candidates:
        image_path = image_dir / _candidate_filename(candidate)
        if not image_path.exists():
            _download_binary(http_client=http_client, url=candidate.image_url, destination=image_path)
        try:
            with Image.open(image_path) as opened:
                width, height = ImageOps.exif_transpose(opened).size
        except Exception:
            continue
        portrait_ratio = height / max(width, 1)
        if width < 360 or height < 500:
            continue
        if portrait_ratio < 1.12:
            continue
        filtered.append(candidate)
    return filtered


def build_summary(results: list[BenchmarkResult]) -> dict[str, object]:
    summary: dict[str, object] = {
        "totals": _summarize_bucket(results),
        "by_game": {},
        "failure_categories": {},
    }
    for benchmark_game in ("pokemon", "ws"):
        game_results = [result for result in results if result.benchmark_game == benchmark_game]
        summary["by_game"][benchmark_game] = _summarize_bucket(game_results)

    failure_categories: dict[str, int] = {}
    for result in results:
        for category in _failure_categories(result):
            failure_categories[category] = failure_categories.get(category, 0) + 1
    summary["failure_categories"] = dict(sorted(failure_categories.items(), key=lambda item: (-item[1], item[0])))
    return summary


def _summarize_bucket(results: list[BenchmarkResult]) -> dict[str, object]:
    total = len(results)
    status_counts: dict[str, int] = {}
    game_match_count = 0
    card_number_count = 0
    offer_count = 0
    fair_value_count = 0
    elapsed_total = 0.0
    for result in results:
        status_counts[result.status] = status_counts.get(result.status, 0) + 1
        if result.parsed_game == result.benchmark_game:
            game_match_count += 1
        if result.parsed_card_number:
            card_number_count += 1
        if result.offer_count > 0:
            offer_count += 1
        if result.fair_value_jpy is not None:
            fair_value_count += 1
        elapsed_total += result.elapsed_seconds
    return {
        "total": total,
        "status_counts": dict(sorted(status_counts.items())),
        "game_match_rate": _ratio(game_match_count, total),
        "card_number_rate": _ratio(card_number_count, total),
        "offer_rate": _ratio(offer_count, total),
        "fair_value_rate": _ratio(fair_value_count, total),
        "average_elapsed_seconds": round(elapsed_total / total, 2) if total else 0.0,
    }


def render_markdown_report(
    *,
    summary: dict[str, object],
    results: list[BenchmarkResult],
    sample_size_per_game: int,
    seed: int,
    page_offsets: tuple[int, ...],
    queries: dict[str, tuple[str, ...]],
    generated_at: str,
) -> str:
    failed_results = [result for result in results if result.status != "success"]
    low_offer_results = [result for result in results if result.status == "success" and result.offer_count == 0]
    lines: list[str] = [
        "# Live Auction Image Benchmark",
        "",
        f"- Generated at: {generated_at}",
        f"- Sample size: {sample_size_per_game} Pokemon + {sample_size_per_game} WS",
        f"- Random seed: `{seed}`",
        "- Source: Yahoo! Auctions search result listing images",
        f"- Page offsets: `{', '.join(str(value) for value in page_offsets)}`",
        f"- Pokemon queries: `{', '.join(queries['pokemon'])}`",
        f"- WS queries: `{', '.join(queries['ws'])}`",
        "",
        "## Summary",
        "",
    ]

    totals = summary["totals"]
    lines.extend(_render_summary_lines("Overall", totals))
    lines.append("")
    for benchmark_game in ("pokemon", "ws"):
        lines.extend(_render_summary_lines(benchmark_game.upper(), summary["by_game"][benchmark_game]))
        lines.append("")

    lines.append("## Failure Categories")
    lines.append("")
    failure_categories: dict[str, int] = summary["failure_categories"]
    if failure_categories:
        for category, count in failure_categories.items():
            lines.append(f"- `{category}`: {count}")
    else:
        lines.append("- No failure categories were recorded.")
    lines.append("")

    lines.append("## Representative Failures")
    lines.append("")
    if failed_results:
        for result in failed_results[:20]:
            lines.append(
                f"- `{result.benchmark_game}` `{result.status}` `{result.title}` -> "
                f"parsed_game=`{result.parsed_game}` card_number=`{result.parsed_card_number}` "
                f"offers=`{result.offer_count}` listing={result.listing_url}"
            )
    else:
        lines.append("- No failed listings in this run.")
    lines.append("")

    if low_offer_results:
        lines.append("## Success Without Market Depth")
        lines.append("")
        for result in low_offer_results[:10]:
            lines.append(
                f"- `{result.benchmark_game}` `{result.title}` -> title=`{result.parsed_title}` "
                f"card_number=`{result.parsed_card_number}` fair=`{result.fair_value_jpy}` listing={result.listing_url}"
            )
        lines.append("")

    lines.append("## Next-Round Plan")
    lines.append("")
    lines.extend(_render_plan_recommendations(results))
    lines.append("")
    return "\n".join(lines)


def _render_summary_lines(label: str, bucket: dict[str, object]) -> list[str]:
    return [
        f"### {label}",
        "",
        f"- Total: {bucket['total']}",
        f"- Status counts: `{json.dumps(bucket['status_counts'], ensure_ascii=False)}`",
        f"- Game match rate: {bucket['game_match_rate']}",
        f"- Card number rate: {bucket['card_number_rate']}",
        f"- Offer rate: {bucket['offer_rate']}",
        f"- Fair value rate: {bucket['fair_value_rate']}",
        f"- Average elapsed seconds: {bucket['average_elapsed_seconds']}",
    ]


def _render_plan_recommendations(results: list[BenchmarkResult]) -> list[str]:
    categories: dict[str, int] = {}
    for result in results:
        for category in _failure_categories(result):
            categories[category] = categories.get(category, 0) + 1

    recommendations: list[str] = []
    if categories.get("non_single_or_bundle_listing", 0) >= 10:
        recommendations.append(
            "- Add a lightweight pre-classifier for listing screenshots and search-result thumbnails so the pipeline can quickly identify bundles, sealed products, or non-card items before full OCR + local vision."
        )
    if categories.get("missing_card_number", 0) >= 10:
        recommendations.append(
            "- Improve card-number extraction for low-resolution auction photos by adding an explicit footer-upscale pass before OCR and a second footer-focused local-vision prompt."
        )
    if categories.get("graded_or_slab_listing", 0) >= 10:
        recommendations.append(
            "- Tighten slab-aware parsing even further for Yahoo! auction photos, especially around grades, cert text, and title reconciliation when the card face is partially occluded by holders."
        )
    if categories.get("market_miss_after_parse", 0) >= 10:
        recommendations.append(
            "- Expand market-resolution fallbacks when OCR gets a likely card number but no offers, including more aggressive title normalization and alternate rarity stripping for auction-derived names."
        )
    if not recommendations:
        recommendations.append(
            "- Focus the next round on the largest unresolved cluster in the raw benchmark results and re-run the same 200-image benchmark after each change."
        )
    return recommendations


def _failure_categories(result: BenchmarkResult) -> tuple[str, ...]:
    categories: list[str] = []
    if not _candidate_title_looks_like_single_card(result.benchmark_game, result.title):
        categories.append("non_single_or_bundle_listing")
    if any(token in result.title.upper() for token in ("PSA", "BGS", "CGC")):
        categories.append("graded_or_slab_listing")
    if result.parsed_game != result.benchmark_game:
        categories.append("wrong_game_or_missing_game")
    if result.parsed_card_number is None:
        categories.append("missing_card_number")
    if result.status != "success" and result.parsed_card_number is not None:
        categories.append("parsed_but_not_success")
    if result.status == "partial" or (result.status == "success" and result.offer_count == 0):
        categories.append("market_miss_after_parse")
    return tuple(_dedupe_preserve_order(categories))


def _candidate_is_relevant(candidate: ListingCandidate) -> bool:
    return _candidate_title_looks_like_single_card(candidate.benchmark_game, candidate.title)


def _candidate_title_looks_like_single_card(benchmark_game: str, title: str) -> bool:
    normalized = title.strip().lower()
    if not normalized:
        return False
    blocked_keywords = POKEMON_NEGATIVE_TITLE_KEYWORDS if benchmark_game == "pokemon" else WS_NEGATIVE_TITLE_KEYWORDS
    if any(keyword in normalized for keyword in blocked_keywords):
        return False
    if MULTI_CARD_COUNT_RE.search(title):
        return False

    if benchmark_game == "pokemon":
        cross_game_keywords = POKEMON_CROSS_GAME_KEYWORDS
        positive_keywords = POKEMON_POSITIVE_TITLE_KEYWORDS
        single_card_hint_re = POKEMON_SINGLE_CARD_HINT_RE
    else:
        cross_game_keywords = WS_CROSS_GAME_KEYWORDS
        positive_keywords = WS_POSITIVE_TITLE_KEYWORDS
        single_card_hint_re = WS_SINGLE_CARD_HINT_RE

    if any(keyword in normalized for keyword in cross_game_keywords):
        return False

    has_positive_keyword = any(keyword in normalized for keyword in positive_keywords)
    has_single_card_hint = single_card_hint_re.search(title) is not None
    return has_positive_keyword or has_single_card_hint


def _candidate_filename(candidate: ListingCandidate) -> str:
    digest = hashlib.sha1(candidate.listing_url.encode("utf-8")).hexdigest()[:16]
    return f"{candidate.benchmark_game}-{digest}.jpg"


def _render_progress_line(
    benchmark_game: str,
    index: int,
    total: int,
    result: BenchmarkResult,
) -> str:
    return (
        f"{benchmark_game} {index:03d}/{total} "
        f"status={result.status} offers={result.offer_count} "
        f"game={result.parsed_game} card_number={result.parsed_card_number} title={result.parsed_title!r}"
    )


def _download_binary(*, http_client: HttpClient, url: str, destination: Path) -> None:
    headers = {
        "User-Agent": http_client.user_agent,
        "Accept-Language": "ja-JP,ja;q=0.9",
        "Referer": "https://auctions.yahoo.co.jp/",
    }
    request = Request(url, headers=headers)
    with urlopen(request, timeout=http_client.timeout_seconds, context=http_client.ssl_context) as response:
        destination.write_bytes(response.read())


def _ratio(numerator: int, denominator: int) -> str:
    if denominator == 0:
        return "0.0%"
    return f"{(numerator / denominator) * 100:.1f}%"


def _dedupe_preserve_order(values: list[str]) -> list[str]:
    deduped: list[str] = []
    for value in values:
        if value and value not in deduped:
            deduped.append(value)
    return deduped


def _configure_stdout() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
    except AttributeError:
        return


if __name__ == "__main__":
    raise SystemExit(main())
