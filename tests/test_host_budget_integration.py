"""Issue #24 — Host Budget integration into HttpClient & Yuyutei paths.

These tests exercise the *integration* (the #22 store/coordinator unit tests
live in test_host_budget.py). They assert the central guarantee: the shared host
budget is consulted BEFORE any network call, the durable cooldown blocks every
priority, a single host's concurrency cap is honoured even under a ThreadPool,
background callers fail fast while manual ones may wait, a live 429 trips the
cross-process cooldown, and every decision is logged.

The legacy per-host circuit breaker (#19) is neutralised via monkeypatch so
these tests isolate the new budget layer; the conftest already isolates the
SQLite store to a per-test temp file.
"""
from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.error import HTTPError

import pytest

from market_monitor.host_budget import (
    DECISION_GRANTED,
    DECISION_SKIPPED_CONCURRENCY_LIMIT,
    DECISION_SKIPPED_COOLING_DOWN,
    PRIORITY_BACKGROUND_ENRICHMENT,
    PRIORITY_MANUAL_RESEARCH,
    REQUESTER_RESEARCH,
    YUYUTEI_HOST,
    get_host_budget,
)
from market_monitor.http import HostRateLimitedError, HttpClient

YUYU_URL = "https://yuyu-tei.jp/sell/ws/s/search?search_word=x"


class _FakeResponse:
    """Minimal stand-in for the urllib response context manager."""

    def __init__(self, body: bytes = b"<html>ok</html>", status: int = 200) -> None:
        self._body = body
        self.status = status

    def read(self) -> bytes:
        return self._body

    class _Headers:
        @staticmethod
        def get_content_charset() -> str:
            return "utf-8"

        @staticmethod
        def get(_key, default=None):
            return default

    @property
    def headers(self):
        return self._Headers()

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> bool:
        return False


@pytest.fixture(autouse=True)
def _neutralize_legacy_circuit(monkeypatch):
    # The #19 circuit breaker would short-circuit before the budget; pin it open
    # (0s remaining) so these tests measure the #24 budget layer alone.
    monkeypatch.setattr("market_monitor.http._circuit_remaining", lambda host: 0.0)


def test_get_text_consults_budget_before_network_when_cooling_down(monkeypatch):
    budget = get_host_budget()
    budget.store.trip_host_cooldown(YUYUTEI_HOST, cooldown_seconds=300, reason="429")

    def boom(*a, **k):
        raise AssertionError("network must not be touched while cooling down")

    monkeypatch.setattr("market_monitor.http.urlopen", boom)
    client = HttpClient()
    with pytest.raises(HostRateLimitedError) as ei:
        client.get_text(YUYU_URL, retries=1, curl_fallback=False,
                        priority=PRIORITY_MANUAL_RESEARCH, requester=REQUESTER_RESEARCH)
    assert ei.value.decision == DECISION_SKIPPED_COOLING_DOWN
    events = budget.store.recent_events(host=YUYUTEI_HOST)
    assert any(e.decision == DECISION_SKIPPED_COOLING_DOWN for e in events)


def test_cooldown_blocks_every_priority(monkeypatch):
    budget = get_host_budget()
    budget.store.trip_host_cooldown(YUYUTEI_HOST, cooldown_seconds=300, reason="429")
    monkeypatch.setattr("market_monitor.http.urlopen",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no net")))
    client = HttpClient()
    for prio in (PRIORITY_MANUAL_RESEARCH, PRIORITY_BACKGROUND_ENRICHMENT):
        with pytest.raises(HostRateLimitedError) as ei:
            client.get_text(YUYU_URL, retries=1, curl_fallback=False, priority=prio)
        assert ei.value.decision == DECISION_SKIPPED_COOLING_DOWN


def test_get_text_granted_returns_text_logs_and_releases(monkeypatch):
    budget = get_host_budget()
    monkeypatch.setattr("market_monitor.http.urlopen", lambda *a, **k: _FakeResponse())
    client = HttpClient()
    text = client.get_text(YUYU_URL, retries=1, curl_fallback=False,
                           priority=PRIORITY_MANUAL_RESEARCH)
    assert "ok" in text
    events = budget.store.recent_events(host=YUYUTEI_HOST)
    assert any(e.decision == DECISION_GRANTED for e in events)
    # Slot was released: a second call still succeeds (not stuck at concurrency 1).
    assert "ok" in client.get_text(YUYU_URL, retries=1, curl_fallback=False,
                                   priority=PRIORITY_MANUAL_RESEARCH)


def test_get_bytes_consults_budget_before_network(monkeypatch):
    budget = get_host_budget()
    budget.store.trip_host_cooldown(YUYUTEI_HOST, cooldown_seconds=300, reason="429")
    monkeypatch.setattr("market_monitor.http.urlopen",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no net")))
    client = HttpClient()
    with pytest.raises(HostRateLimitedError) as ei:
        client.get_bytes(YUYU_URL, priority=PRIORITY_MANUAL_RESEARCH)
    assert ei.value.decision == DECISION_SKIPPED_COOLING_DOWN


def test_background_get_text_fails_fast_when_slot_taken(monkeypatch):
    budget = get_host_budget()
    # Hold the single Yuyutei slot, then a background fetch must fail fast.
    permit = budget.acquire_fetch_slot(url=YUYU_URL, priority=PRIORITY_MANUAL_RESEARCH)
    assert permit.granted
    try:
        monkeypatch.setattr("market_monitor.http.urlopen",
                            lambda *a, **k: (_ for _ in ()).throw(AssertionError("no net")))
        client = HttpClient()
        with pytest.raises(HostRateLimitedError) as ei:
            client.get_text(YUYU_URL, retries=1, curl_fallback=False,
                            priority=PRIORITY_BACKGROUND_ENRICHMENT)
        assert ei.value.decision == DECISION_SKIPPED_CONCURRENCY_LIMIT
    finally:
        permit.release()


def test_threadpool_cannot_exceed_yuyutei_concurrency(monkeypatch):
    inflight = {"cur": 0, "max": 0}
    lock = threading.Lock()

    def fake_urlopen(*a, **k):
        with lock:
            inflight["cur"] += 1
            inflight["max"] = max(inflight["max"], inflight["cur"])
        time.sleep(0.05)
        with lock:
            inflight["cur"] -= 1
        return _FakeResponse()

    monkeypatch.setattr("market_monitor.http.urlopen", fake_urlopen)
    client = HttpClient()

    def fetch(i: int):
        try:
            return client.get_text(f"{YUYU_URL}&n={i}", retries=1, curl_fallback=False,
                                   priority=PRIORITY_MANUAL_RESEARCH, timeout_seconds=5)
        except HostRateLimitedError:
            return None

    with ThreadPoolExecutor(max_workers=5) as ex:
        list(ex.map(fetch, range(5)))
    # max_concurrency=1 for yuyu-tei.jp: never two in flight at once.
    assert inflight["max"] == 1


def test_live_429_trips_durable_cooldown(monkeypatch):
    budget = get_host_budget()

    def fake_urlopen(*a, **k):
        raise HTTPError(YUYU_URL, 429, "Too Many Requests", {}, None)

    monkeypatch.setattr("market_monitor.http.urlopen", fake_urlopen)
    client = HttpClient()
    with pytest.raises(HTTPError):
        client.get_text(YUYU_URL, retries=1, curl_fallback=False,
                        priority=PRIORITY_MANUAL_RESEARCH, requester=REQUESTER_RESEARCH)
    cd = budget.store.get_host_cooldown(YUYUTEI_HOST)
    assert cd is not None and cd.active
    assert cd.last_status == 429


def test_yuyutei_client_skips_network_when_cooling_down(monkeypatch):
    from market_monitor.yuyutei_search import YuyuteiMarketplaceSearchClient

    budget = get_host_budget()
    budget.store.trip_host_cooldown(YUYUTEI_HOST, cooldown_seconds=300, reason="429")
    monkeypatch.setattr("market_monitor.http.urlopen",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no net")))
    client = YuyuteiMarketplaceSearchClient(
        requester=REQUESTER_RESEARCH, priority=PRIORITY_MANUAL_RESEARCH,
    )
    band = client.reference_band("リザードン pokemon", price_max=100_000,
                                 source_options={"game_code": "poc"})
    assert band is None
