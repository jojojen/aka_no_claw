from __future__ import annotations

import json
import logging
import re
import ssl
from dataclasses import dataclass, replace
from html import unescape
from html.parser import HTMLParser
from typing import Callable, Sequence
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse

from urllib.request import Request, urlopen

logger = logging.getLogger(__name__)

DEFAULT_WEB_SEARCH_LIMIT = 5

# How many top results to actually download + read (item 1), and how much of
# each page to keep when feeding the summariser. Kept small because the local
# qwen3:14b on the Mac Mini generates at ~11 tok/s — a large grounding prompt
# pushes the summarise call past its timeout under Ollama queue contention.
DEFAULT_FETCH_PAGE_COUNT = 2
DEFAULT_PAGE_CHARS = 8000
# Per-source budget inside the multi-source summary prompt; smaller than a
# single-page fetch so several articles fit in the local model's context.
DEFAULT_SUMMARY_CONTENT_CHARS = 2000
DEFAULT_REFORMULATED_QUERY_COUNT = 3

# Tags whose text content is boilerplate / non-readable and should be dropped
# when extracting article text from a page.
_NON_CONTENT_TAGS = frozenset(
    {"script", "style", "noscript", "template", "svg", "head", "nav", "header", "footer", "aside", "form", "button"}
)
_BLOCK_TAGS = frozenset(
    {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "section", "article", "blockquote"}
)

FetchUrl = Callable[[str, int, str, ssl.SSLContext | None], str]
SearchFn = Callable[[str, int], tuple["WebSearchResult", ...]]
SummarizeFn = Callable[[str, tuple["WebSearchResult", ...]], str]
FetchPageFn = Callable[[str], str]
ReformulateFn = Callable[[str], Sequence[str]]
PageAnswerFn = Callable[[str, str, str], str]


@dataclass(frozen=True, slots=True)
class WebSearchResult:
    title: str
    url: str
    snippet: str = ""
    content: str = ""


@dataclass(frozen=True, slots=True)
class WebResearchAnswer:
    query: str
    summary: str
    sources: tuple[WebSearchResult, ...]


YAHOO_JAPAN_SEARCH_URL = "https://search.yahoo.co.jp/search"
_YAHOO_PROFILE_DEFAULT = "~/.openclaw/browser_profile/yahoo_jp"
_PLAYWRIGHT_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)
_YAHOO_WAIT_MS = 3500  # wait after domcontentloaded for JS to render results

# --- Persistent browser session (singleton per process) ---
# Keeping the Playwright context alive across calls avoids 2-3s browser startup
# overhead on every search query, making multi-query reformulated searches fast
# enough to stay within the 45s Ollama timeout budget.

_pw_instance = None   # Playwright handle
_pw_ctx = None        # BrowserContext (persistent, keeps cookies/session)
_pw_lock = None       # threading.Lock — lazy-init below


def _get_yahoo_context(profile_dir: str | None = None):
    """Return the shared persistent BrowserContext, launching it if needed."""
    import pathlib
    import threading

    global _pw_instance, _pw_ctx, _pw_lock

    if _pw_lock is None:
        _pw_lock = threading.Lock()

    with _pw_lock:
        if _pw_ctx is not None:
            try:
                # Probe that the context is still alive
                _pw_ctx.pages  # noqa: B018 — raises if context is closed
                return _pw_ctx
            except Exception:
                logger.warning("Yahoo Japan browser context died; restarting")
                _pw_ctx = None
                try:
                    _pw_instance.stop()
                except Exception:
                    pass
                _pw_instance = None

        from playwright.sync_api import sync_playwright

        profile = pathlib.Path(
            profile_dir or pathlib.Path(_YAHOO_PROFILE_DEFAULT).expanduser()
        )
        profile.mkdir(parents=True, exist_ok=True)

        pw = sync_playwright().start()
        ctx = pw.chromium.launch_persistent_context(
            str(profile),
            headless=True,
            args=["--no-sandbox", "--disable-blink-features=AutomationControlled"],
            user_agent=_PLAYWRIGHT_UA,
        )
        _pw_instance = pw
        _pw_ctx = ctx
        logger.info("Yahoo Japan browser context started profile=%s", profile)
        return ctx


def search_yahoo_japan_playwright(
    query: str,
    *,
    max_results: int = DEFAULT_WEB_SEARCH_LIMIT,
    profile_dir: str | None = None,
    reuse_context: bool = True,
) -> tuple[WebSearchResult, ...]:
    """Yahoo Japan web search via a persistent Playwright Chromium session.

    The browser context is started once and reused across calls so multi-query
    reformulated searches don't pay browser-startup cost on every query.
    """
    from playwright.sync_api import TimeoutError as PlaywrightTimeout

    cleaned_query = " ".join(query.split()).strip()
    if not cleaned_query:
        return ()

    url = f"{YAHOO_JAPAN_SEARCH_URL}?{urlencode({'p': cleaned_query})}"
    logger.info("Yahoo Japan search query=%s", cleaned_query)

    if reuse_context:
        ctx = _get_yahoo_context(profile_dir)
        page = ctx.new_page()
        try:
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=30_000)
            except PlaywrightTimeout:
                logger.warning("Yahoo Japan goto timeout; reading current DOM")
            page.wait_for_timeout(_YAHOO_WAIT_MS)
            results = _extract_yahoo_japan_results(page, max_results)
        finally:
            page.close()
    else:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-blink-features=AutomationControlled"],
            )
            context = browser.new_context(
                locale="ja-JP",
                user_agent=_PLAYWRIGHT_UA,
                viewport={"width": 1280, "height": 900},
            )
            page = context.new_page()
            try:
                try:
                    page.goto(url, wait_until="domcontentloaded", timeout=30_000)
                except PlaywrightTimeout:
                    logger.warning("Yahoo Japan goto timeout; reading current DOM")
                page.wait_for_timeout(_YAHOO_WAIT_MS)
                results = _extract_yahoo_japan_results(page, max_results)
            finally:
                context.close()
                browser.close()

    if not results:
        logger.warning("Yahoo Japan returned 0 results query=%s", cleaned_query)
    return results


def _extract_yahoo_japan_results(page: object, max_results: int) -> tuple[WebSearchResult, ...]:
    results: list[WebSearchResult] = []
    for card in page.query_selector_all("div.sw-Card"):  # type: ignore[attr-defined]
        if len(results) >= max_results:
            break
        title_el = card.query_selector(".sw-Card__title a")
        if not title_el:
            continue
        href = (title_el.get_attribute("href") or "").strip()
        if not _is_external_http_url(href):
            continue
        raw_title = title_el.inner_text().strip()
        title = raw_title.splitlines()[0].strip() if raw_title else ""
        if not title:
            continue
        snip_el = card.query_selector("p")
        snippet = (snip_el.inner_text().strip() if snip_el else "").replace("\xa0", " ")
        results.append(WebSearchResult(title=title, url=href, snippet=snippet))
    return tuple(results)




def build_web_research_answer(
    query: str,
    *,
    search_fn: SearchFn,
    summarize_fn: SummarizeFn,
    max_results: int = DEFAULT_WEB_SEARCH_LIMIT,
    reformulate_fn: ReformulateFn | None = None,
    fetch_page_fn: FetchPageFn | None = None,
    fetch_page_count: int = DEFAULT_FETCH_PAGE_COUNT,
) -> WebResearchAnswer:
    """Search the web and summarise the results.

    With ``reformulate_fn`` (item 4) the raw question is first turned into a few
    focused search queries whose results are merged. With ``fetch_page_fn``
    (item 1) the top results are actually downloaded so the summariser reads the
    article body instead of only the search-engine snippet. Both default to
    ``None``, in which case behaviour is identical to the snippet-only pipeline.
    """
    cleaned_query = " ".join(query.split()).strip()
    if not cleaned_query:
        raise ValueError("Research query cannot be empty.")

    limit = max(1, min(10, max_results))
    queries = _plan_search_queries(cleaned_query, reformulate_fn)
    sources = _run_searches(search_fn, queries, limit)
    if not sources:
        return WebResearchAnswer(
            query=cleaned_query,
            summary=(
                f"我找不到足夠有用的網路來源來回答：{cleaned_query}\n"
                "（搜尋來源可能暫時受限）若你已有特定網址，可改用 "
                "/fetch <網址> <問題> 直接讀取該頁面。"
            ),
            sources=(),
        )

    if fetch_page_fn is not None:
        sources = _attach_page_content(sources, fetch_page_fn, fetch_page_count)

    summary = summarize_fn(cleaned_query, sources).strip()
    if not summary:
        summary = "我找到了來源，但本地 LLM 沒有回傳可用的摘要。"
    return WebResearchAnswer(query=cleaned_query, summary=summary, sources=sources)


def _plan_search_queries(cleaned_query: str, reformulate_fn: ReformulateFn | None) -> tuple[str, ...]:
    if reformulate_fn is None:
        return (cleaned_query,)
    try:
        raw = reformulate_fn(cleaned_query)
    except Exception:
        logger.exception("Query reformulation failed; falling back to original query=%s", cleaned_query)
        return (cleaned_query,)

    planned: list[str] = [cleaned_query]
    seen = {cleaned_query.lower()}
    for candidate in raw or ():
        normalized = " ".join(str(candidate).split()).strip()
        if not normalized or normalized.lower() in seen:
            continue
        seen.add(normalized.lower())
        planned.append(normalized)
    return tuple(planned[:DEFAULT_REFORMULATED_QUERY_COUNT])


def _run_searches(search_fn: SearchFn, queries: tuple[str, ...], limit: int) -> tuple[WebSearchResult, ...]:
    if len(queries) == 1:
        return tuple(search_fn(queries[0], limit))

    merged: list[WebSearchResult] = []
    seen: set[str] = set()
    for query in queries:
        try:
            results = search_fn(query, limit)
        except Exception:
            logger.exception("Search failed for reformulated query=%s", query)
            continue
        for result in results:
            key = _canonical_url_key(result.url)
            if key in seen:
                continue
            seen.add(key)
            merged.append(result)
            if len(merged) >= limit:
                return tuple(merged)
    return tuple(merged)


def _attach_page_content(
    sources: tuple[WebSearchResult, ...],
    fetch_page_fn: FetchPageFn,
    fetch_page_count: int,
) -> tuple[WebSearchResult, ...]:
    enriched: list[WebSearchResult] = []
    budget = max(0, fetch_page_count)
    for source in sources:
        if budget <= 0:
            enriched.append(source)
            continue
        budget -= 1
        try:
            content = fetch_page_fn(source.url)
        except Exception:
            logger.exception("Page fetch failed url=%s", source.url)
            content = ""
        if content:
            enriched.append(replace(source, content=content))
        else:
            enriched.append(source)
    return tuple(enriched)


def summarize_web_sources_with_ollama(
    query: str,
    sources: tuple[WebSearchResult, ...],
    *,
    endpoint: str,
    model: str,
    timeout_seconds: int,
    ssl_context: ssl.SSLContext | None = None,
) -> str:
    if not sources:
        return f"我找不到足夠有用的網路來源來回答：{query}"

    payload = {
        "model": model,
        "prompt": _build_summary_prompt(query, sources),
        "stream": False,
        "think": False,
        "options": {"temperature": 0.2},
    }
    request = Request(
        _resolve_ollama_generate_url(endpoint),
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=timeout_seconds, context=ssl_context) as response:
            body = response.read().decode("utf-8", errors="replace")
    except HTTPError as exc:
        raise RuntimeError(f"Web research LLM HTTP {exc.code}.") from exc
    except URLError as exc:
        raise RuntimeError(f"Web research LLM request failed: {exc.reason}") from exc

    parsed = json.loads(body)
    response_text = parsed.get("response", "")
    if not isinstance(response_text, str):
        raise RuntimeError(f"Web research LLM response type was {type(response_text).__name__}.")
    return response_text.strip()


def format_web_research_answer(answer: WebResearchAnswer) -> str:
    lines = [answer.summary.strip()]
    if not answer.sources:
        return lines[0]

    lines.append("")
    lines.append("參考來源：")
    for index, source in enumerate(answer.sources, start=1):
        title = _compact_whitespace(source.title)
        lines.append(f"[{index}] {title}")
        lines.append(source.url)
    return "\n".join(lines)


def _fetch_url(
    url: str,
    timeout_seconds: int,
    user_agent: str,
    ssl_context: ssl.SSLContext | None,
) -> str:
    request = Request(
        url,
        headers={
            "User-Agent": user_agent,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        },
        method="GET",
    )
    try:
        with urlopen(request, timeout=timeout_seconds, context=ssl_context) as response:
            return response.read().decode("utf-8", errors="replace")
    except HTTPError as exc:
        raise RuntimeError(f"DuckDuckGo search HTTP {exc.code}.") from exc
    except URLError as exc:
        raise RuntimeError(f"DuckDuckGo search failed: {exc.reason}") from exc


def _build_summary_prompt(query: str, sources: tuple[WebSearchResult, ...]) -> str:
    source_lines: list[str] = []
    for index, source in enumerate(sources, start=1):
        source_lines.append(f"[{index}] {source.title}")
        source_lines.append(f"URL: {source.url}")
        if source.content:
            body = _truncate(source.content, DEFAULT_SUMMARY_CONTENT_CHARS)
            source_lines.append(f"Page content:\n{body}")
        else:
            source_lines.append(f"Snippet: {source.snippet or '(no snippet)'}")
        source_lines.append("")
    return (
        "You answer Telegram questions for OpenClaw using web search results.\n"
        "CRITICAL LANGUAGE RULE: Always answer in Traditional Chinese as used in Taiwan (zh-TW).\n"
        "Do not answer in English, Japanese, Simplified Chinese, or Mainland Chinese phrasing.\n"
        "Use Taiwan wording such as 資訊、品質、熱門、價格、來源, and avoid simplified characters.\n"
        "When a source includes 'Page content', prefer it over the short snippet and "
        "ground your answer in that text. Use only the provided sources; if they are "
        "weak or do not answer the question, say so plainly instead of guessing.\n"
        "Keep the answer concise and useful.\n"
        "Cite claims with bracketed source numbers like [1] or [2].\n\n"
        f"User question:\n{query}\n\n"
        "Sources:\n"
        + "\n".join(source_lines)
        + "\nAnswer:"
    )


# --- Item 1 helper: download a page and extract readable text -----------------


def fetch_page_text(
    url: str,
    *,
    timeout_seconds: int = 15,
    max_chars: int = DEFAULT_PAGE_CHARS,
    user_agent: str = "OpenClawWebResearch/0.1 (+https://local-dev)",
    ssl_context: ssl.SSLContext | None = None,
    fetch_url: FetchUrl | None = None,
) -> str:
    """Fetch ``url`` and return its readable text, truncated to ``max_chars``.

    Returns an empty string on any network/parse failure so callers can fall
    back to the search snippet rather than aborting the whole research turn.
    """
    if not _is_external_http_url(url) and not url.startswith(("http://", "https://")):
        return ""
    fetch = fetch_url or _fetch_url
    try:
        html = fetch(url, timeout_seconds, user_agent, ssl_context)
    except Exception:
        logger.exception("fetch_page_text download failed url=%s", url)
        return ""
    text = extract_readable_text(html)
    return _truncate(text, max_chars)


def extract_readable_text(html: str) -> str:
    parser = _ReadableTextParser()
    try:
        parser.feed(html)
    except Exception:
        logger.exception("extract_readable_text parse failed")
    return _compact_lines(parser.text)


# --- Item 4: reformulate a question into focused search queries ---------------


def reformulate_queries_with_ollama(
    query: str,
    *,
    endpoint: str,
    model: str,
    timeout_seconds: int,
    max_queries: int = DEFAULT_REFORMULATED_QUERY_COUNT,
    ssl_context: ssl.SSLContext | None = None,
) -> tuple[str, ...]:
    """Ask the local model for up to ``max_queries`` web-search queries.

    Always returns at least the original query; never raises.
    """
    cleaned = " ".join(query.split()).strip()
    if not cleaned:
        return ()
    prompt = (
        "You write web search queries. Given a user's question, output up to "
        f"{max_queries} concise search queries that would find authoritative pages.\n"
        "Rules: one query per line, no numbering, no quotes, no commentary. "
        "Keep proper nouns (song titles, names) intact and in their original language "
        "(Japanese/English) rather than translating them.\n\n"
        f"Question: {cleaned}\n\nQueries:"
    )
    payload = {"model": model, "prompt": prompt, "stream": False, "think": False, "options": {"temperature": 0.2}}
    request = Request(
        _resolve_ollama_generate_url(endpoint),
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=timeout_seconds, context=ssl_context) as response:
            body = response.read().decode("utf-8", errors="replace")
        response_text = json.loads(body).get("response", "")
    except Exception:
        logger.exception("Query reformulation request failed query=%s", cleaned)
        return (cleaned,)

    queries: list[str] = [cleaned]
    seen = {cleaned.lower()}
    for line in str(response_text).splitlines():
        candidate = _strip_query_line(line)
        if not candidate or candidate.lower() in seen:
            continue
        seen.add(candidate.lower())
        queries.append(candidate)
        if len(queries) >= max_queries:
            break
    return tuple(queries)


# --- Item 3: fetch a single URL and answer a focused prompt about it ----------


def build_web_fetch_answer(
    url: str,
    prompt: str,
    *,
    fetch_page_fn: FetchPageFn,
    answer_fn: PageAnswerFn,
) -> WebResearchAnswer:
    """WebFetch equivalent: download one URL and answer ``prompt`` from its text."""
    cleaned_url = url.strip()
    cleaned_prompt = " ".join(prompt.split()).strip() or "請摘要這個網頁的重點。"
    if not cleaned_url.startswith(("http://", "https://")):
        return WebResearchAnswer(
            query=cleaned_prompt,
            summary="請提供以 http(s):// 開頭的有效網址。",
            sources=(),
        )

    content = fetch_page_fn(cleaned_url)
    if not content:
        return WebResearchAnswer(
            query=cleaned_prompt,
            summary=f"我抓不到這個網址可讀取的內容：{cleaned_url}",
            sources=(WebSearchResult(title=cleaned_url, url=cleaned_url),),
        )

    summary = answer_fn(cleaned_url, cleaned_prompt, content).strip()
    if not summary:
        summary = "我讀到了網頁內容，但本地 LLM 沒有回傳可用的回答。"
    return WebResearchAnswer(
        query=cleaned_prompt,
        summary=summary,
        sources=(WebSearchResult(title=cleaned_url, url=cleaned_url, content=content),),
    )


def answer_page_with_ollama(
    url: str,
    prompt: str,
    content: str,
    *,
    endpoint: str,
    model: str,
    timeout_seconds: int,
    ssl_context: ssl.SSLContext | None = None,
) -> str:
    body = _truncate(content, DEFAULT_PAGE_CHARS)
    full_prompt = (
        "You read a single web page for OpenClaw and answer the user's request about it.\n"
        "CRITICAL LANGUAGE RULE: Always answer in Traditional Chinese as used in Taiwan (zh-TW).\n"
        "Do not answer in English, Japanese, Simplified Chinese, or Mainland Chinese phrasing.\n"
        "Base your answer ONLY on the page content below. If the page does not contain the "
        "requested information, say so plainly instead of guessing.\n\n"
        f"Page URL: {url}\n"
        f"User request: {prompt}\n\n"
        "Page content:\n"
        f"{body}\n\nAnswer:"
    )
    request_payload = {"model": model, "prompt": full_prompt, "stream": False, "think": False, "options": {"temperature": 0.2}}
    request = Request(
        _resolve_ollama_generate_url(endpoint),
        data=json.dumps(request_payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=timeout_seconds, context=ssl_context) as response:
            raw = response.read().decode("utf-8", errors="replace")
    except HTTPError as exc:
        raise RuntimeError(f"Web fetch LLM HTTP {exc.code}.") from exc
    except URLError as exc:
        raise RuntimeError(f"Web fetch LLM request failed: {exc.reason}") from exc

    parsed = json.loads(raw)
    response_text = parsed.get("response", "")
    if not isinstance(response_text, str):
        raise RuntimeError(f"Web fetch LLM response type was {type(response_text).__name__}.")
    return response_text.strip()


def _strip_query_line(line: str) -> str:
    stripped = line.strip()
    if not stripped:
        return ""
    stripped = re.sub(r"^[\s>*\-•]+", "", stripped)
    stripped = re.sub(r"^\d+[.)、]\s*", "", stripped)
    stripped = stripped.strip().strip("\"'`「」『』")
    return " ".join(stripped.split()).strip()


def _truncate(value: str, max_chars: int) -> str:
    if max_chars <= 0 or len(value) <= max_chars:
        return value
    return value[:max_chars].rstrip() + " …"


def _resolve_ollama_generate_url(endpoint: str) -> str:
    stripped = endpoint.rstrip("/")
    if stripped.endswith("/api/generate"):
        return stripped
    if stripped.endswith("/api"):
        return f"{stripped}/generate"
    return f"{stripped}/api/generate"


class _ReadableTextParser(HTMLParser):
    """Collect human-readable text from a page, dropping scripts/nav/etc."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._chunks: list[str] = []
        self._skip_depth = 0

    @property
    def text(self) -> str:
        return "".join(self._chunks)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        name = tag.lower()
        if name in _NON_CONTENT_TAGS:
            self._skip_depth += 1
            return
        if self._skip_depth == 0 and name in _BLOCK_TAGS:
            self._chunks.append("\n")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self._skip_depth == 0 and tag.lower() in _BLOCK_TAGS:
            self._chunks.append("\n")

    def handle_endtag(self, tag: str) -> None:
        name = tag.lower()
        if name in _NON_CONTENT_TAGS:
            if self._skip_depth > 0:
                self._skip_depth -= 1
            return
        if self._skip_depth == 0 and name in _BLOCK_TAGS:
            self._chunks.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth == 0 and data:
            self._chunks.append(data)


def _compact_lines(value: str) -> str:
    lines = [" ".join(unescape(line).split()).strip() for line in value.splitlines()]
    return "\n".join(line for line in lines if line).strip()


def _is_external_http_url(url: str) -> bool:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        return False
    return not parsed.netloc.endswith("duckduckgo.com")


def _canonical_url_key(url: str) -> str:
    parsed = urlparse(url)
    netloc = parsed.netloc.lower()
    path = re.sub(r"/+$", "", parsed.path)
    return f"{parsed.scheme.lower()}://{netloc}{path}"


def _compact_whitespace(value: str) -> str:
    return " ".join(unescape(value).split()).strip()
