from __future__ import annotations

import json

from openclaw_adapter.web_search import (
    WebResearchAnswer,
    WebSearchResult,
    build_web_research_answer,
    format_web_research_answer,
    parse_duckduckgo_html,
    search_duckduckgo,
    summarize_web_sources_with_ollama,
)


def test_parse_duckduckgo_html_extracts_result_url_title_and_snippet() -> None:
    html = """
    <div class="result">
      <a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fpikachu&amp;rut=abc">
        Why Pikachu cards are popular
      </a>
      <a class="result__snippet">Pikachu is the mascot and collector demand is broad.</a>
    </div>
    """

    results = parse_duckduckgo_html(html)

    assert results == (
        WebSearchResult(
            title="Why Pikachu cards are popular",
            url="https://example.com/pikachu",
            snippet="Pikachu is the mascot and collector demand is broad.",
        ),
    )


def test_parse_duckduckgo_html_deduplicates_and_applies_limit() -> None:
    html = """
    <a class="result__a" href="https://example.com/a/">First</a>
    <a class="result__snippet">First snippet.</a>
    <a class="result__a" href="https://example.com/a">Duplicate</a>
    <a class="result__snippet">Duplicate snippet.</a>
    <a class="result__a" href="https://example.com/b">Second</a>
    <a class="result__snippet">Second snippet.</a>
    """

    results = parse_duckduckgo_html(html, max_results=1)

    assert results == (WebSearchResult(title="First", url="https://example.com/a/", snippet="First snippet."),)


def test_search_duckduckgo_uses_fetcher_and_returns_limited_results() -> None:
    captured: dict[str, object] = {}

    def fetch(url: str, timeout_seconds: int, user_agent: str, ssl_context) -> str:
        captured["url"] = url
        captured["timeout_seconds"] = timeout_seconds
        captured["user_agent"] = user_agent
        captured["ssl_context"] = ssl_context
        return """
        <a class="result__a" href="https://example.com/a">First</a>
        <a class="result__a" href="https://example.com/b">Second</a>
        """

    results = search_duckduckgo(
        "why pikachu cards are popular",
        max_results=1,
        timeout_seconds=12,
        user_agent="test-agent",
        fetch_url=fetch,
    )

    assert "why+pikachu+cards+are+popular" in str(captured["url"])
    assert captured["timeout_seconds"] == 12
    assert captured["user_agent"] == "test-agent"
    assert len(results) == 1
    assert results[0].url == "https://example.com/a"


def test_build_web_research_answer_passes_sources_to_summarizer() -> None:
    calls: dict[str, object] = {}
    sources = (
        WebSearchResult(title="Mascot source", url="https://example.com/mascot", snippet="Mascot."),
        WebSearchResult(title="Demand source", url="https://example.com/demand", snippet="Demand."),
    )

    def search(query: str, limit: int) -> tuple[WebSearchResult, ...]:
        calls["search"] = (query, limit)
        return sources

    def summarize(query: str, found_sources: tuple[WebSearchResult, ...]) -> str:
        calls["summarize"] = (query, found_sources)
        return "Pikachu cards are popular because Pikachu is the franchise mascot [1]."

    answer = build_web_research_answer(
        "why are Pikachu cards popular",
        search_fn=search,
        summarize_fn=summarize,
        max_results=5,
    )

    assert calls["search"] == ("why are Pikachu cards popular", 5)
    assert calls["summarize"] == ("why are Pikachu cards popular", sources)
    assert answer.summary.endswith("[1].")
    assert answer.sources == sources


def test_format_web_research_answer_includes_reference_urls() -> None:
    answer = WebResearchAnswer(
        query="why are Pikachu cards popular",
        summary="Pikachu is the franchise mascot [1].",
        sources=(
            WebSearchResult(title="Mascot source", url="https://example.com/mascot", snippet="Mascot."),
            WebSearchResult(title="Demand source", url="https://example.com/demand", snippet="Demand."),
        ),
    )

    text = format_web_research_answer(answer)

    assert "Pikachu is the franchise mascot [1]." in text
    assert "References:" in text
    assert "[1] Mascot source" in text
    assert "https://example.com/mascot" in text
    assert "[2] Demand source" in text
    assert "https://example.com/demand" in text


def test_summarize_web_sources_with_ollama_posts_sources(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return None

        def read(self) -> bytes:
            return json.dumps({"response": "Summary with citations [1]."}).encode("utf-8")

    def fake_urlopen(request, timeout: int, context):
        captured["url"] = request.full_url
        captured["timeout"] = timeout
        captured["payload"] = json.loads(request.data.decode("utf-8"))
        captured["context"] = context
        return FakeResponse()

    monkeypatch.setattr("openclaw_adapter.web_search.urlopen", fake_urlopen)

    text = summarize_web_sources_with_ollama(
        "why are Pikachu cards popular",
        (WebSearchResult(title="Mascot source", url="https://example.com/mascot", snippet="Mascot."),),
        endpoint="http://127.0.0.1:11434",
        model="qwen3:4b",
        timeout_seconds=9,
    )

    assert text == "Summary with citations [1]."
    assert captured["url"] == "http://127.0.0.1:11434/api/generate"
    assert captured["timeout"] == 9
    payload = captured["payload"]
    assert payload["model"] == "qwen3:4b"
    assert "Mascot source" in payload["prompt"]
    assert "https://example.com/mascot" in payload["prompt"]
