from __future__ import annotations

import json

from openclaw_adapter import web_search as ws
from openclaw_adapter.web_search import (
    WebResearchAnswer,
    WebSearchResult,
    build_web_fetch_answer,
    build_web_research_answer,
    extract_readable_text,
    format_web_research_answer,
    summarize_web_sources_with_ollama,
)


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
        return "皮卡丘卡受歡迎，主因是皮卡丘是寶可夢代表角色之一 [1]。"

    answer = build_web_research_answer(
        "why are Pikachu cards popular",
        search_fn=search,
        summarize_fn=summarize,
        max_results=5,
    )

    assert calls["search"] == ("why are Pikachu cards popular", 5)
    assert calls["summarize"] == ("why are Pikachu cards popular", sources)
    assert answer.summary.endswith("[1]。")
    assert answer.sources == sources


def test_format_web_research_answer_includes_reference_urls() -> None:
    answer = WebResearchAnswer(
        query="why are Pikachu cards popular",
        summary="皮卡丘是寶可夢代表角色之一 [1]。",
        sources=(
            WebSearchResult(title="Mascot source", url="https://example.com/mascot", snippet="Mascot."),
            WebSearchResult(title="Demand source", url="https://example.com/demand", snippet="Demand."),
        ),
    )

    text = format_web_research_answer(answer)

    assert "皮卡丘是寶可夢代表角色之一 [1]。" in text
    assert "參考來源：" in text
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
            return json.dumps({"response": "皮卡丘是寶可夢代表角色之一 [1]。"}).encode("utf-8")

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

    assert text == "皮卡丘是寶可夢代表角色之一 [1]。"
    assert captured["url"] == "http://127.0.0.1:11434/api/generate"
    assert captured["timeout"] == 9
    payload = captured["payload"]
    assert payload["model"] == "qwen3:4b"
    assert "Always answer in Traditional Chinese as used in Taiwan" in payload["prompt"]
    assert "Do not answer in English, Japanese, Simplified Chinese" in payload["prompt"]
    assert "Match the user's language" not in payload["prompt"]
    assert "Mascot source" in payload["prompt"]
    assert "https://example.com/mascot" in payload["prompt"]


def test_build_web_research_answer_uses_traditional_chinese_no_source_fallback() -> None:
    answer = build_web_research_answer(
        "why are Pikachu cards popular",
        search_fn=lambda query, limit: (),
        summarize_fn=lambda query, sources: "unused",
    )

    assert answer.summary.startswith(
        "我找不到足夠有用的網路來源來回答：why are Pikachu cards popular"
    )
    # When search yields nothing, point the user at the /fetch fallback.
    assert "/fetch" in answer.summary


def test_extract_readable_text_drops_boilerplate_keeps_article() -> None:
    html = """
    <html><head><title>T</title><style>.x{}</style></head>
    <body><nav>HOME ABOUT</nav><script>var a=1;</script>
    <article><h1>標題</h1><p>第一段內容。</p><p>第二段內容。</p></article>
    <footer>copyright</footer></body></html>
    """

    text = extract_readable_text(html)

    assert "標題" in text and "第一段內容。" in text
    assert "var a" not in text and "HOME ABOUT" not in text and "copyright" not in text


def test_build_web_research_answer_fetch_feeds_page_content_to_summarizer() -> None:
    seen: dict[str, object] = {}

    def summarize(query: str, sources: tuple[WebSearchResult, ...]) -> str:
        seen["has_content"] = bool(sources[0].content)
        return "OK"

    answer = build_web_research_answer(
        "q",
        search_fn=lambda q, limit: (WebSearchResult("A", "https://a.com", snippet="s"),),
        summarize_fn=summarize,
        fetch_page_fn=lambda url: "FULL PAGE BODY",
    )

    assert seen["has_content"] is True
    assert answer.summary == "OK"


def test_build_web_research_answer_reformulate_merges_and_dedupes() -> None:
    table = {
        "q": (WebSearchResult("A", "https://a.com/x"),),
        "alt": (WebSearchResult("A2", "https://a.com/x/"), WebSearchResult("B", "https://b.com")),
    }

    def summarize(query: str, sources: tuple[WebSearchResult, ...]) -> str:
        return ",".join(s.url for s in sources)

    answer = build_web_research_answer(
        "q",
        search_fn=lambda q, limit: table.get(q, ()),
        summarize_fn=summarize,
        reformulate_fn=lambda q: ["q", "alt"],
    )

    assert [s.url for s in answer.sources] == ["https://a.com/x", "https://b.com"]


def test_build_web_fetch_answer_success_and_validation() -> None:
    ok = build_web_fetch_answer(
        "https://a.com/article",
        "重點是什麼",
        fetch_page_fn=lambda u: "PAGE TEXT",
        answer_fn=lambda u, p, content: f"ANS<{p}|{content}>",
    )
    assert ok.summary == "ANS<重點是什麼|PAGE TEXT>"
    assert ok.sources[0].url == "https://a.com/article"

    bad = build_web_fetch_answer(
        "notaurl", "x", fetch_page_fn=lambda u: "y", answer_fn=lambda u, p, c: "z",
    )
    assert "有效網址" in bad.summary

    empty = build_web_fetch_answer(
        "https://a.com", "x", fetch_page_fn=lambda u: "", answer_fn=lambda u, p, c: "z",
    )
    assert "抓不到" in empty.summary


def test_ddg_decode_href_unwraps_redirect_and_passes_plain() -> None:
    wrapped = "//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fpage%3Fa%3D1&rut=x"
    assert ws._ddg_decode_href(wrapped) == "https://example.com/page?a=1"
    # A plain external URL is returned unchanged.
    assert ws._ddg_decode_href("https://example.com/x") == "https://example.com/x"
    # Empty / None are safe.
    assert ws._ddg_decode_href("") == ""
    assert ws._ddg_decode_href(None) == ""


def test_next_search_order_alternates_across_calls(monkeypatch, tmp_path) -> None:
    counter = tmp_path / "rr"
    monkeypatch.setattr(ws, "_search_rr_counter_path", lambda: counter)
    monkeypatch.setattr(ws, "_SEARCH_RR_LOCK", None)
    # Fresh counter (no file) starts at 0 → yahoo first, then flips each call.
    first = ws._next_search_order()
    second = ws._next_search_order()
    third = ws._next_search_order()
    assert first == ("yahoo", "duckduckgo")
    assert second == ("duckduckgo", "yahoo")
    assert third == ("yahoo", "duckduckgo")
    # Persisted across a process restart (re-read from disk).
    assert counter.read_text().strip() == "3"


def test_web_search_uses_primary_then_returns(monkeypatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(ws, "_next_search_order", lambda: ("yahoo", "duckduckgo"))

    def fake_yahoo(query, *, max_results, reuse_context=True):
        calls.append("yahoo")
        return (WebSearchResult("Y", "https://y.com"),)

    def fake_ddg(query, *, max_results, reuse_context=True):
        calls.append("duckduckgo")
        return (WebSearchResult("D", "https://d.com"),)

    monkeypatch.setattr(ws, "search_yahoo_japan_playwright", fake_yahoo)
    monkeypatch.setattr(ws, "search_duckduckgo_html", fake_ddg)

    results = ws.web_search("q", max_results=3)
    assert [r.url for r in results] == ["https://y.com"]
    # Fallback backend is never touched when the primary yields results.
    assert calls == ["yahoo"]


def test_web_search_falls_back_on_empty_primary(monkeypatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(ws, "_next_search_order", lambda: ("duckduckgo", "yahoo"))

    def fake_yahoo(query, *, max_results, reuse_context=True):
        calls.append("yahoo")
        return (WebSearchResult("Y", "https://y.com"),)

    def fake_ddg(query, *, max_results, reuse_context=True):
        calls.append("duckduckgo")
        return ()

    monkeypatch.setattr(ws, "search_yahoo_japan_playwright", fake_yahoo)
    monkeypatch.setattr(ws, "search_duckduckgo_html", fake_ddg)

    results = ws.web_search("q", max_results=3)
    assert [r.url for r in results] == ["https://y.com"]
    assert calls == ["duckduckgo", "yahoo"]


def test_web_search_falls_back_on_primary_exception(monkeypatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(ws, "_next_search_order", lambda: ("yahoo", "duckduckgo"))

    def fake_yahoo(query, *, max_results, reuse_context=True):
        calls.append("yahoo")
        raise RuntimeError("yahoo boom")

    def fake_ddg(query, *, max_results, reuse_context=True):
        calls.append("duckduckgo")
        return (WebSearchResult("D", "https://d.com"),)

    monkeypatch.setattr(ws, "search_yahoo_japan_playwright", fake_yahoo)
    monkeypatch.setattr(ws, "search_duckduckgo_html", fake_ddg)

    results = ws.web_search("q", max_results=3)
    assert [r.url for r in results] == ["https://d.com"]
    assert calls == ["yahoo", "duckduckgo"]


def test_web_search_returns_empty_when_both_fail(monkeypatch) -> None:
    monkeypatch.setattr(ws, "_next_search_order", lambda: ("yahoo", "duckduckgo"))
    monkeypatch.setattr(
        ws, "search_yahoo_japan_playwright",
        lambda query, *, max_results, reuse_context=True: (),
    )
    monkeypatch.setattr(
        ws, "search_duckduckgo_html",
        lambda query, *, max_results: (),
    )
    assert ws.web_search("q", max_results=3) == ()


def test_web_search_forwards_reuse_flag_to_both_backends(monkeypatch) -> None:
    captured: dict[str, object] = {}
    monkeypatch.setattr(ws, "_next_search_order", lambda: ("yahoo", "duckduckgo"))

    def fake_yahoo(query, *, max_results, reuse_context=True):
        captured["yahoo_reuse"] = reuse_context
        return ()

    def fake_ddg(query, *, max_results, reuse_context=True):
        captured["ddg_reuse"] = reuse_context
        return (WebSearchResult("D", "https://d.com"),)

    monkeypatch.setattr(ws, "search_yahoo_japan_playwright", fake_yahoo)
    monkeypatch.setattr(ws, "search_duckduckgo_html", fake_ddg)
    ws.web_search("q", max_results=3, reuse_browser=False)
    assert captured["yahoo_reuse"] is False
    assert captured["ddg_reuse"] is False
