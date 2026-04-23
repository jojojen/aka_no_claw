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
