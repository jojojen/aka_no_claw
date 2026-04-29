from __future__ import annotations

import threading
from pathlib import Path

import openclaw_adapter.reputation_agent as reputation_agent


def test_ensure_agent_thread_starts_new_thread(monkeypatch) -> None:
    started_thread = threading.Thread(target=lambda: None, name="test-reputation-agent")

    monkeypatch.setattr(reputation_agent, "_agent_thread", None)
    monkeypatch.setattr(reputation_agent, "check_prerequisites", lambda api_key: None)
    monkeypatch.setattr(reputation_agent, "check_server_authorization", lambda server_url, api_key: None)
    monkeypatch.setattr(reputation_agent, "start_agent_thread", lambda **kwargs: started_thread)

    thread, started_now = reputation_agent.ensure_agent_thread(
        server_url="http://127.0.0.1:5000",
        api_key="token",
        poll_secs=5,
    )

    assert thread is started_thread
    assert started_now is True


def test_ensure_agent_thread_reuses_existing_thread(monkeypatch) -> None:
    existing_thread = threading.Thread(target=lambda: None, name="existing-reputation-agent")
    monkeypatch.setattr(existing_thread, "is_alive", lambda: True)
    monkeypatch.setattr(reputation_agent, "_agent_thread", existing_thread)
    monkeypatch.setattr(reputation_agent, "check_server_authorization", lambda server_url, api_key: None)

    thread, started_now = reputation_agent.ensure_agent_thread(
        server_url="http://127.0.0.1:5000",
        api_key="token",
        poll_secs=5,
    )

    assert thread is existing_thread
    assert started_now is False


def test_ensure_agent_thread_fails_fast_when_prerequisites_missing(monkeypatch) -> None:
    monkeypatch.setattr(reputation_agent, "_agent_thread", None)
    monkeypatch.setattr(reputation_agent, "check_prerequisites", lambda api_key: "REPUTATION_AGENT_ADMIN_TOKEN is not set")

    try:
        reputation_agent.ensure_agent_thread(
            server_url="http://127.0.0.1:5000",
            api_key="",
            poll_secs=5,
        )
    except RuntimeError as exc:
        assert "REPUTATION_AGENT_ADMIN_TOKEN" in str(exc)
    else:  # pragma: no cover - defensive.
        raise AssertionError("Expected ensure_agent_thread to fail fast when prerequisites are missing.")


def test_ensure_agent_thread_fails_fast_when_server_rejects_token(monkeypatch) -> None:
    monkeypatch.setattr(reputation_agent, "_agent_thread", None)
    monkeypatch.setattr(reputation_agent, "check_prerequisites", lambda api_key: None)
    monkeypatch.setattr(
        reputation_agent,
        "check_server_authorization",
        lambda server_url, api_key: "REPUTATION_AGENT_ADMIN_TOKEN is invalid for REPUTATION_AGENT_SERVER_URL",
    )

    try:
        reputation_agent.ensure_agent_thread(
            server_url="http://127.0.0.1:5000",
            api_key="wrong-token",
            poll_secs=5,
        )
    except RuntimeError as exc:
        assert "invalid" in str(exc)
    else:  # pragma: no cover - defensive.
        raise AssertionError("Expected ensure_agent_thread to fail fast when the server rejects the token.")


def test_ensure_server_ready_auto_starts_local_server(monkeypatch) -> None:
    calls: list[str] = []
    responses = iter(
        [
            "Could not reach reputation_snapshot server: [WinError 10061] connection refused",
            None,
        ]
    )

    monkeypatch.setattr(reputation_agent, "check_server_authorization", lambda server_url, api_key: next(responses))
    monkeypatch.setattr(reputation_agent, "_is_local_server_url", lambda server_url: True)
    monkeypatch.setattr(reputation_agent, "_find_local_reputation_snapshot_dir", lambda: Path(r"C:\fake\reputation_snapshot"))
    monkeypatch.setattr(reputation_agent, "_launch_local_reputation_snapshot", lambda repo_dir: calls.append(str(repo_dir)))
    monkeypatch.setattr(reputation_agent.time, "sleep", lambda seconds: None)

    started_now, err = reputation_agent.ensure_server_ready("http://127.0.0.1:5000", "token", timeout_seconds=2.0)

    assert started_now is True
    assert err is None
    assert calls == [r"C:\fake\reputation_snapshot"]


def test_ensure_server_ready_reports_local_startup_failure(monkeypatch) -> None:
    monkeypatch.setattr(
        reputation_agent,
        "check_server_authorization",
        lambda server_url, api_key: "Could not reach reputation_snapshot server: [WinError 10061] connection refused",
    )
    monkeypatch.setattr(reputation_agent, "_is_local_server_url", lambda server_url: True)
    monkeypatch.setattr(reputation_agent, "_find_local_reputation_snapshot_dir", lambda: Path(r"C:\fake\reputation_snapshot"))

    def fail_launch(repo_dir):
        raise RuntimeError("start.bat launch failed")

    monkeypatch.setattr(reputation_agent, "_launch_local_reputation_snapshot", fail_launch)

    started_now, err = reputation_agent.ensure_server_ready("http://127.0.0.1:5000", "token", timeout_seconds=2.0)

    assert started_now is False
    assert err is not None
    assert "Local auto-start failed" in err


def test_launch_local_reputation_snapshot_prefers_no_browser_launcher(monkeypatch, tmp_path) -> None:
    commands: list[tuple[list[str], str]] = []
    (tmp_path / "start-no-browser.bat").write_text("@echo off\n", encoding="utf-8")
    (tmp_path / "start.bat").write_text("@echo off\n", encoding="utf-8")

    def fake_popen(command, *, cwd=None, **kwargs):
        commands.append((list(command), str(cwd)))

    monkeypatch.setattr(reputation_agent.subprocess, "Popen", fake_popen)

    reputation_agent._launch_local_reputation_snapshot(tmp_path)

    assert commands == [
        (
            ["cmd", "/c", "start", "", "start-no-browser.bat"],
            str(tmp_path),
        )
    ]


def test_launch_local_reputation_snapshot_passes_no_browser_to_start_bat(monkeypatch, tmp_path) -> None:
    commands: list[tuple[list[str], str]] = []
    (tmp_path / "start.bat").write_text("@echo off\n", encoding="utf-8")

    def fake_popen(command, *, cwd=None, **kwargs):
        commands.append((list(command), str(cwd)))

    monkeypatch.setattr(reputation_agent.subprocess, "Popen", fake_popen)

    reputation_agent._launch_local_reputation_snapshot(tmp_path)

    assert commands == [
        (
            ["cmd", "/c", "start", "", "start.bat", "go", "--no-browser"],
            str(tmp_path),
        )
    ]


def test_find_profile_url_matches_relative_and_absolute_urls() -> None:
    relative_html = '<a href="/user/profile/427403243">seller</a>'
    absolute_html = '<a href="https://jp.mercari.com/user/profile/427403243">seller</a>'

    assert reputation_agent._find_profile_url(relative_html) == "https://jp.mercari.com/user/profile/427403243"
    assert reputation_agent._find_profile_url(absolute_html) == "https://jp.mercari.com/user/profile/427403243"


class _FakeItemPage:
    def __init__(self) -> None:
        self.waited_selectors: list[str] = []
        self.waited_timeouts: list[int] = []

    def eval_on_selector_all(self, selector: str, script: str):
        assert selector == "a"
        return [
            {"href": "https://jp.mercari.com/notifications", "text": "", "location": "", "aria": ""},
            {
                "href": "https://jp.mercari.com/user/profile/427403243",
                "text": "ミヤジ",
                "location": "item_details:seller_info",
                "aria": "ミヤジ, 384件のレビュー",
            },
        ]

    def wait_for_selector(self, selector: str, timeout: int) -> None:
        self.waited_selectors.append(selector)
        raise RuntimeError("selector not available in test")

    def wait_for_timeout(self, ms: int) -> None:
        self.waited_timeouts.append(ms)

    def content(self) -> str:
        return "<html><body>no profile href in serialized html</body></html>"


def test_resolve_item_profile_url_falls_back_to_dom_anchor_href() -> None:
    page = _FakeItemPage()

    profile_url = reputation_agent._resolve_item_profile_url(page, "<html><body>initial html without profile</body></html>")

    assert profile_url == "https://jp.mercari.com/user/profile/427403243"
    assert page.waited_selectors == ['a[data-location="item_details:seller_info"]', '[data-testid="seller-link"]']
    assert page.waited_timeouts == [1500]


def test_extract_item_seller_context_reads_display_name_and_total_reviews() -> None:
    page = _FakeItemPage()

    context = reputation_agent._extract_item_seller_context(
        page,
        "<html><body>initial html without profile</body></html>",
        "seller name\n384",
    )

    assert context["profile_url"] == "https://jp.mercari.com/user/profile/427403243"
    assert context["display_name"]
    assert context["seller_total_reviews"] == 384


def test_extract_item_seller_context_prefers_visible_seller_over_badge_label() -> None:
    class BadgePage(_FakeItemPage):
        def eval_on_selector_all(self, selector: str, script: str):
            return [
                {
                    "href": "https://jp.mercari.com/user/profile/954805077",
                    "text": "Seller Level 10",
                    "location": "item_details:seller_info",
                    "aria": "Seller Level 10",
                },
            ]

    context = reputation_agent._extract_item_seller_context(
        BadgePage(),
        "<html><body>initial html without profile</body></html>",
        "\n".join(
            [
                "メルカリ安心への取り組み",
                "出品者",
                "きずま",
                "3962",
                "本人確認済",
                "Quick shipment",
                "Seller Level 10",
            ]
        ),
    )

    assert context["profile_url"] == "https://jp.mercari.com/user/profile/954805077"
    assert context["display_name"] == "きずま"
    assert context["seller_total_reviews"] == 3962


class _FakeReviewElement:
    def __init__(self, page: "_FakeReviewPage", tab: str) -> None:
        self.page = page
        self.tab = tab

    def click(self) -> None:
        if self.tab in {"seller", "buyer"}:
            self.page.role_tab = self.tab
            self.page.rating_tab = "good"
        else:
            self.page.rating_tab = self.tab


class _FakeReviewPage:
    def __init__(self) -> None:
        self.role_tab = "seller"
        self.rating_tab = "good"

    def query_selector(self, selector: str):
        mapping = {
            '[aria-controls="good"]': "good",
            '[aria-controls="bad"]': "bad",
            '[aria-controls="seller"]': "seller",
            '[aria-controls="buyer"]': "buyer",
        }
        tab = mapping.get(selector)
        if not tab:
            return None
        return _FakeReviewElement(self, tab)

    def wait_for_timeout(self, ms: int) -> None:
        pass

    def content(self) -> str:
        return "<html>seller good</html>"

    def evaluate(self, script: str) -> str:
        active_tab = f"{self.role_tab}_{self.rating_tab}"
        return {
            "seller_good": "良かった (2)\n購入者\nseller review\n2026/04",
            "seller_bad": "残念だった (1)\n購入者\nbad seller review\n2026/04",
            "buyer_good": "良かった (1)\n出品者\nbuyer review\n2026/04",
            "buyer_bad": "残念だった (0)",
        }.get(active_tab, "")


def test_capture_review_tab_texts_keeps_seller_and_buyer_review_texts() -> None:
    page = _FakeReviewPage()

    result = reputation_agent._capture_review_tab_texts(
        page,
        {"raw_html": "<html>initial</html>", "visible_text": "良かった (2)\n購入者\ninitial\n2026/04"},
    )

    assert "seller review" in result["reviews_text"]
    assert "bad seller review" in result["reviews_bad_text"]
    assert "buyer review" in result["reviews_buyer_text"]


def test_profile_page_loaded_rejects_company_page_snapshot() -> None:
    assert reputation_agent._profile_page_loaded(
        {
            "body_text": "メルカリについて 会社概要（運営会社）",
            "has_heading": True,
            "has_avatar": False,
            "has_reviews_link": False,
            "has_profile_link": False,
        }
    ) is False


def test_profile_page_loaded_accepts_profile_snapshot() -> None:
    assert reputation_agent._profile_page_loaded(
        {
            "body_text": "seller profile",
            "has_heading": True,
            "has_avatar": True,
            "has_reviews_link": True,
            "has_profile_link": True,
        }
    ) is True
