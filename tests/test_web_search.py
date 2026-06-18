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


def test_run_searches_interleaves_so_later_query_is_not_starved() -> None:
    # First query alone returns `limit` results; the locale-translated second
    # query must still contribute its top hit instead of being starved.
    first = tuple(WebSearchResult(f"A{i}", f"https://a.com/{i}") for i in range(5))
    second = (WebSearchResult("JP", "https://eiga.example/jp"),) + tuple(
        WebSearchResult(f"B{i}", f"https://b.com/{i}") for i in range(4)
    )
    table = {"orig": first, "jp": second}
    merged = ws._run_searches(lambda q, limit: table[q], ("orig", "jp"), 5)
    urls = [s.url for s in merged]
    assert "https://eiga.example/jp" in urls
    assert urls[0] == "https://a.com/0"
    assert urls[1] == "https://eiga.example/jp"
    assert len(merged) == 5


def test_reformulation_prompt_carries_locale_rule() -> None:
    prompt = ws._build_reformulation_prompt("奧德賽電影 日本上映日", 3)
    assert "LOCALE RULE" in prompt
    assert "locale's primary language" in prompt
    assert "奧德賽電影 日本上映日" in prompt


def test_summary_prompt_carries_grounding_strictness() -> None:
    sources = (WebSearchResult(title="台灣站", url="https://tw.example/x", snippet="台灣 7/17 上映"),)
    prompt = ws._build_summary_prompt("奧德賽在日本何時上映", sources)
    assert "GROUNDING STRICTNESS" in prompt
    assert "country, region, date, or" in prompt
    assert "do NOT present the other" in prompt


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


_POOL = ("yahoo", "duckduckgo", "brave", "startpage")


def _stub_pool(monkeypatch, *, calls, returns):
    """Replace every pool backend with a fake that records the call and returns
    a configured value (or raises if the value is an Exception)."""

    def make(name):
        def fn(query, *, max_results, reuse_context=True, reuse_browser=True):
            calls.append(name)
            value = returns.get(name, ())
            if isinstance(value, Exception):
                raise value
            return value

        return fn

    monkeypatch.setattr(ws, "search_yahoo_japan_playwright", make("yahoo"))
    monkeypatch.setattr(ws, "search_duckduckgo_html", make("duckduckgo"))
    monkeypatch.setattr(ws, "search_brave", make("brave"))
    monkeypatch.setattr(ws, "search_startpage", make("startpage"))


def test_next_search_rotation_cycles_whole_pool(monkeypatch, tmp_path) -> None:
    counter = tmp_path / "rr"
    monkeypatch.setattr(ws, "_search_rr_counter_path", lambda: counter)
    monkeypatch.setattr(ws, "_SEARCH_RR_LOCK", None)
    monkeypatch.setattr(ws, "_SEARCH_POOL", _POOL)
    rotations = [ws._next_search_rotation() for _ in range(5)]
    assert rotations[0] == ("yahoo", "duckduckgo", "brave", "startpage")
    assert rotations[1] == ("duckduckgo", "brave", "startpage", "yahoo")
    assert rotations[2] == ("brave", "startpage", "yahoo", "duckduckgo")
    assert rotations[3] == ("startpage", "yahoo", "duckduckgo", "brave")
    assert rotations[4] == ("yahoo", "duckduckgo", "brave", "startpage")  # wraps
    # Persisted across a process restart (re-read from disk).
    assert counter.read_text().strip() == "5"


def test_web_search_uses_primary_then_returns(monkeypatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(ws, "_next_search_rotation", lambda: _POOL)
    _stub_pool(
        monkeypatch, calls=calls,
        returns={"yahoo": (WebSearchResult("Y", "https://y.com"),)},
    )
    results = ws.web_search("q", max_results=3)
    assert [r.url for r in results] == ["https://y.com"]
    # No other pool member is touched once the primary yields results.
    assert calls == ["yahoo"]


def test_web_search_falls_through_pool_on_empty(monkeypatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(
        ws, "_next_search_rotation",
        lambda: ("duckduckgo", "brave", "startpage", "yahoo"),
    )
    _stub_pool(
        monkeypatch, calls=calls,
        returns={"brave": (WebSearchResult("B", "https://b.com"),)},
    )
    results = ws.web_search("q", max_results=3)
    assert [r.url for r in results] == ["https://b.com"]
    # ddg empty → brave returns; startpage/yahoo never reached.
    assert calls == ["duckduckgo", "brave"]


def test_web_search_skips_exception_backend(monkeypatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(ws, "_next_search_rotation", lambda: _POOL)
    _stub_pool(
        monkeypatch, calls=calls,
        returns={
            "yahoo": RuntimeError("yahoo boom"),
            "duckduckgo": (WebSearchResult("D", "https://d.com"),),
        },
    )
    results = ws.web_search("q", max_results=3)
    assert [r.url for r in results] == ["https://d.com"]
    assert calls == ["yahoo", "duckduckgo"]


def test_web_search_returns_empty_when_all_backends_fail(monkeypatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(ws, "_next_search_rotation", lambda: _POOL)
    _stub_pool(monkeypatch, calls=calls, returns={})
    assert ws.web_search("q", max_results=3) == ()
    # Every pool member was attempted before giving up.
    assert calls == ["yahoo", "duckduckgo", "brave", "startpage"]


def test_web_search_forwards_reuse_flag_to_backends(monkeypatch) -> None:
    captured: dict[str, object] = {}
    monkeypatch.setattr(ws, "_next_search_rotation", lambda: _POOL)

    def fake_yahoo(query, *, max_results, reuse_context=True):
        captured["yahoo"] = reuse_context
        return ()

    def fake_ddg(query, *, max_results, reuse_browser=True):
        captured["ddg"] = reuse_browser
        return ()

    def fake_brave(query, *, max_results, reuse_browser=True):
        captured["brave"] = reuse_browser
        return (WebSearchResult("B", "https://b.com"),)

    monkeypatch.setattr(ws, "search_yahoo_japan_playwright", fake_yahoo)
    monkeypatch.setattr(ws, "search_duckduckgo_html", fake_ddg)
    monkeypatch.setattr(ws, "search_brave", fake_brave)
    ws.web_search("q", max_results=3, reuse_browser=False)
    # The Yahoo persistent-context flag and the per-call browser flag both flip.
    assert captured["yahoo"] is False
    assert captured["ddg"] is False
    assert captured["brave"] is False


class _FakeEl:
    def __init__(self, text="", attrs=None, children=None):
        self._text = text
        self._attrs = attrs or {}
        self._children = children or {}

    def inner_text(self):
        return self._text

    def get_attribute(self, key):
        return self._attrs.get(key)

    def query_selector(self, sel):
        return self._children.get(sel)


class _FakePage:
    def __init__(self, nodes):
        self._nodes = nodes

    def query_selector_all(self, sel):
        return self._nodes


def test_extract_css_results_title_snippet_filters_and_cap() -> None:
    anchor = _FakeEl(text="Real Title\ntrailing", attrs={"href": "https://ext.com/a"})
    snippet = _FakeEl(text="the snippet\xa0text")
    good = _FakeEl(children={"a.x": anchor, ".title": anchor, ".empty": _FakeEl(""), ".s": snippet})
    # Self-referential nav link to the engine's own host is dropped.
    self_anchor = _FakeEl(text="Nav", attrs={"href": "https://brave.com/about"})
    selfnode = _FakeEl(children={"a.x": self_anchor, ".title": self_anchor})
    extra = _FakeEl(children={"a.x": _FakeEl("X", {"href": "https://ext.com/b"}), ".title": _FakeEl("X", {"href": "https://ext.com/b"})})

    page = _FakePage([good, selfnode, extra])
    res = ws._extract_css_results(
        page, 5,
        node_sel="div", anchor_sel="a.x", title_sel=".title",
        snippet_sels=(".empty", ".s"), engine_host="brave.com",
    )
    assert [(r.title, r.url, r.snippet) for r in res] == [
        ("Real Title", "https://ext.com/a", "the snippet text"),
        ("X", "https://ext.com/b", ""),
    ]

    # max_results caps the output.
    capped = ws._extract_css_results(
        page, 1, node_sel="div", anchor_sel="a.x", title_sel=".title",
        snippet_sels=(".s",), engine_host="brave.com",
    )
    assert len(capped) == 1


def test_extract_css_results_applies_href_decode() -> None:
    anchor = _FakeEl(
        text="T",
        attrs={"href": "//duckduckgo.com/l/?uddg=https%3A%2F%2Freal.com%2Fp"},
    )
    node = _FakeEl(children={"a.r": anchor})
    page = _FakePage([node])
    res = ws._extract_css_results(
        page, 5, node_sel="div", anchor_sel="a.r", href_decode=ws._ddg_decode_href,
    )
    assert res[0].url == "https://real.com/p"
