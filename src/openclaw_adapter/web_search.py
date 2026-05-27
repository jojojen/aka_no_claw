from __future__ import annotations

import json
import logging
import re
import ssl
from dataclasses import dataclass
from html import unescape
from html.parser import HTMLParser
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urlparse
from urllib.request import Request, urlopen

logger = logging.getLogger(__name__)

DEFAULT_WEB_SEARCH_LIMIT = 5
DUCKDUCKGO_HTML_URL = "https://html.duckduckgo.com/html/"

FetchUrl = Callable[[str, int, str, ssl.SSLContext | None], str]
SearchFn = Callable[[str, int], tuple["WebSearchResult", ...]]
SummarizeFn = Callable[[str, tuple["WebSearchResult", ...]], str]


@dataclass(frozen=True, slots=True)
class WebSearchResult:
    title: str
    url: str
    snippet: str = ""


@dataclass(frozen=True, slots=True)
class WebResearchAnswer:
    query: str
    summary: str
    sources: tuple[WebSearchResult, ...]


def search_duckduckgo(
    query: str,
    *,
    max_results: int = DEFAULT_WEB_SEARCH_LIMIT,
    timeout_seconds: int = 20,
    user_agent: str = "OpenClawWebResearch/0.1 (+https://local-dev)",
    ssl_context: ssl.SSLContext | None = None,
    fetch_url: FetchUrl | None = None,
) -> tuple[WebSearchResult, ...]:
    cleaned_query = " ".join(query.split()).strip()
    if not cleaned_query:
        return ()

    limit = max(1, min(10, max_results))
    fetch = fetch_url or _fetch_url
    url = f"{DUCKDUCKGO_HTML_URL}?{urlencode({'q': cleaned_query})}"
    logger.info("DuckDuckGo search query=%s limit=%s", cleaned_query, limit)
    html = fetch(url, timeout_seconds, user_agent, ssl_context)
    return parse_duckduckgo_html(html, max_results=limit)


def parse_duckduckgo_html(html: str, *, max_results: int = DEFAULT_WEB_SEARCH_LIMIT) -> tuple[WebSearchResult, ...]:
    parser = _DuckDuckGoResultParser()
    parser.feed(html)
    return _dedupe_results(parser.results, max_results=max_results)


def build_web_research_answer(
    query: str,
    *,
    search_fn: SearchFn,
    summarize_fn: SummarizeFn,
    max_results: int = DEFAULT_WEB_SEARCH_LIMIT,
) -> WebResearchAnswer:
    cleaned_query = " ".join(query.split()).strip()
    if not cleaned_query:
        raise ValueError("Research query cannot be empty.")

    sources = search_fn(cleaned_query, max(1, min(10, max_results)))
    if not sources:
        return WebResearchAnswer(
            query=cleaned_query,
            summary=f"我找不到足夠有用的網路來源來回答：{cleaned_query}",
            sources=(),
        )

    summary = summarize_fn(cleaned_query, sources).strip()
    if not summary:
        summary = "我找到了來源，但本地 LLM 沒有回傳可用的摘要。"
    return WebResearchAnswer(query=cleaned_query, summary=summary, sources=sources)


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
        source_lines.extend(
            [
                f"[{index}] {source.title}",
                f"URL: {source.url}",
                f"Snippet: {source.snippet or '(no snippet)'}",
            ]
        )
    return (
        "You answer Telegram questions for OpenClaw using web search results.\n"
        "CRITICAL LANGUAGE RULE: Always answer in Traditional Chinese as used in Taiwan (zh-TW).\n"
        "Do not answer in English, Japanese, Simplified Chinese, or Mainland Chinese phrasing.\n"
        "Use Taiwan wording such as 資訊、品質、熱門、價格、來源, and avoid simplified characters.\n"
        "Use only the provided sources. If the sources are weak, say so plainly.\n"
        "Keep the answer concise and useful.\n"
        "Cite claims with bracketed source numbers like [1] or [2].\n\n"
        f"User question:\n{query}\n\n"
        "Sources:\n"
        + "\n".join(source_lines)
        + "\n\nAnswer:"
    )


def _resolve_ollama_generate_url(endpoint: str) -> str:
    stripped = endpoint.rstrip("/")
    if stripped.endswith("/api/generate"):
        return stripped
    if stripped.endswith("/api"):
        return f"{stripped}/generate"
    return f"{stripped}/api/generate"


class _DuckDuckGoResultParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.results: list[WebSearchResult] = []
        self._title_parts: list[str] | None = None
        self._title_depth = 0
        self._title_url: str | None = None
        self._snippet_parts: list[str] | None = None
        self._snippet_depth = 0
        self._last_result_index: int | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs_dict = {name.lower(): value or "" for name, value in attrs}
        class_name = attrs_dict.get("class", "")

        if self._title_parts is not None:
            self._title_depth += 1
            return
        if self._snippet_parts is not None:
            self._snippet_depth += 1
            return

        if tag.lower() == "a" and _has_class(class_name, "result__a"):
            self._title_parts = []
            self._title_depth = 1
            self._title_url = _normalize_result_url(attrs_dict.get("href", ""))
            return

        if _has_class(class_name, "result__snippet"):
            self._snippet_parts = []
            self._snippet_depth = 1

    def handle_endtag(self, tag: str) -> None:
        if self._title_parts is not None:
            self._title_depth -= 1
            if self._title_depth <= 0:
                title = _compact_whitespace("".join(self._title_parts))
                url = self._title_url or ""
                if title and _is_external_http_url(url):
                    self.results.append(WebSearchResult(title=title, url=url))
                    self._last_result_index = len(self.results) - 1
                self._title_parts = None
                self._title_url = None
                self._title_depth = 0
            return

        if self._snippet_parts is not None:
            self._snippet_depth -= 1
            if self._snippet_depth <= 0:
                snippet = _compact_whitespace("".join(self._snippet_parts))
                if snippet and self._last_result_index is not None:
                    result = self.results[self._last_result_index]
                    self.results[self._last_result_index] = WebSearchResult(
                        title=result.title,
                        url=result.url,
                        snippet=snippet,
                    )
                self._snippet_parts = None
                self._snippet_depth = 0

    def handle_data(self, data: str) -> None:
        if self._title_parts is not None:
            self._title_parts.append(data)
        elif self._snippet_parts is not None:
            self._snippet_parts.append(data)


def _has_class(class_name: str, expected: str) -> bool:
    return expected in class_name.split()


def _normalize_result_url(href: str) -> str:
    href = unescape(href).strip()
    if href.startswith("//"):
        href = f"https:{href}"
    if href.startswith("/l/"):
        href = f"https://duckduckgo.com{href}"

    parsed = urlparse(href)
    if parsed.netloc.endswith("duckduckgo.com") and parsed.path == "/l/":
        target = parse_qs(parsed.query).get("uddg", [""])[0]
        return target.strip()
    return href


def _is_external_http_url(url: str) -> bool:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        return False
    return not parsed.netloc.endswith("duckduckgo.com")


def _dedupe_results(results: list[WebSearchResult], *, max_results: int) -> tuple[WebSearchResult, ...]:
    deduped: list[WebSearchResult] = []
    seen: set[str] = set()
    for result in results:
        key = _canonical_url_key(result.url)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(result)
        if len(deduped) >= max_results:
            break
    return tuple(deduped)


def _canonical_url_key(url: str) -> str:
    parsed = urlparse(url)
    netloc = parsed.netloc.lower()
    path = re.sub(r"/+$", "", parsed.path)
    return f"{parsed.scheme.lower()}://{netloc}{path}"


def _compact_whitespace(value: str) -> str:
    return " ".join(unescape(value).split()).strip()
