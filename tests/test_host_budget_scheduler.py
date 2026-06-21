"""Issue #25 — Request priority, queueing & diagnostics scheduler.

Builds on the #22 store and #24 coordinator. These tests cover the scheduling
*behavior* layered on in #25:

- D1 priority vocabulary + safe fallback
- D2 grant / wait / skip decisions
- D3 reserved manual capacity (background can't starve manual)
- D4 bounded, configurable manual wait (no unbounded queue)
- D5 diagnostics events carry requester/priority/host/decision/wait_seconds
- D6 the explicit acceptance scenarios
"""
from __future__ import annotations

import threading
import time

from market_monitor.host_budget import (
    DECISION_GRANTED,
    DECISION_MANUAL_WAIT_TIMEOUT,
    DECISION_SKIPPED_COOLING_DOWN,
    DECISION_SKIPPED_CONCURRENCY_LIMIT,
    DECISION_WAITED_THEN_GRANTED,
    DEFAULT_PRIORITY,
    PRIORITY_BACKGROUND_ENRICHMENT,
    PRIORITY_MANUAL_RESEARCH,
    REQUESTER_OPPORTUNITY,
    REQUESTER_RESEARCH,
    YUYUTEI_HOST,
    HostBudget,
    HostBudgetStore,
    HostPolicy,
    normalize_priority,
    reserved_manual_slots,
)

YUYU_URL = "https://yuyu-tei.jp/sell/ws/s/search?x=1"
MULTI_HOST = "multi.example"
MULTI_URL = f"https://{MULTI_HOST}/path"


def _budget(tmp_path, **kwargs) -> HostBudget:
    store = HostBudgetStore(tmp_path / "hb.db")
    # A host that allows 2 concurrent requests so a manual slot can be reserved.
    store.upsert_policy(HostPolicy(host=MULTI_HOST, requests_per_minute=60,
                                   min_interval_seconds=1.0, max_concurrency=2,
                                   cooldown_seconds=120, enabled=True))
    return HostBudget(store, **kwargs)


# ── D1: priority vocabulary ───────────────────────────────────────────────────

def test_unknown_priority_falls_back_to_background():
    assert normalize_priority("not-a-real-priority") == DEFAULT_PRIORITY
    assert normalize_priority(None) == DEFAULT_PRIORITY
    assert normalize_priority(PRIORITY_MANUAL_RESEARCH) == PRIORITY_MANUAL_RESEARCH


def test_reserved_manual_slots_helper():
    assert reserved_manual_slots(1) == 0   # single-slot host can't reserve
    assert reserved_manual_slots(2) == 1
    assert reserved_manual_slots(4) == 1


# ── D3: reserved manual capacity ──────────────────────────────────────────────

def test_manual_outranks_background_via_reserved_capacity(tmp_path):
    budget = _budget(tmp_path)
    bg1 = budget.acquire_fetch_slot(url=MULTI_URL, requester=REQUESTER_OPPORTUNITY,
                                    priority=PRIORITY_BACKGROUND_ENRICHMENT)
    assert bg1.granted
    # Second background is refused: the 2nd slot is reserved for manual callers.
    bg2 = budget.acquire_fetch_slot(url=MULTI_URL, priority=PRIORITY_BACKGROUND_ENRICHMENT)
    assert not bg2.granted
    assert bg2.decision == DECISION_SKIPPED_CONCURRENCY_LIMIT
    # A manual caller still gets the reserved slot immediately.
    manual = budget.acquire_fetch_slot(url=MULTI_URL, requester=REQUESTER_RESEARCH,
                                       priority=PRIORITY_MANUAL_RESEARCH)
    assert manual.granted
    bg1.release()
    manual.release()


def test_background_degrades_gracefully_when_capacity_unavailable(tmp_path):
    budget = _budget(tmp_path)
    # Yuyutei is single-slot: no reservation possible, background still runs.
    permit = budget.acquire_fetch_slot(url=YUYU_URL, priority=PRIORITY_BACKGROUND_ENRICHMENT)
    assert permit.granted
    permit.release()
    again = budget.acquire_fetch_slot(url=YUYU_URL, priority=PRIORITY_BACKGROUND_ENRICHMENT)
    assert again.granted
    again.release()


# ── D2: grant / wait / skip + cooldown precedence ─────────────────────────────

def test_cooldown_blocks_every_priority(tmp_path):
    budget = _budget(tmp_path)
    budget.store.trip_host_cooldown(YUYUTEI_HOST, cooldown_seconds=300, reason="429")
    for prio in (PRIORITY_MANUAL_RESEARCH, PRIORITY_BACKGROUND_ENRICHMENT):
        permit = budget.acquire_fetch_slot(url=YUYU_URL, priority=prio)
        assert not permit.granted
        assert permit.decision == DECISION_SKIPPED_COOLING_DOWN


def test_background_fails_fast_when_at_capacity(tmp_path):
    budget = _budget(tmp_path)
    held = budget.acquire_fetch_slot(url=YUYU_URL, priority=PRIORITY_MANUAL_RESEARCH)
    assert held.granted
    start = time.monotonic()
    bg = budget.acquire_fetch_slot(url=YUYU_URL, priority=PRIORITY_BACKGROUND_ENRICHMENT)
    elapsed = time.monotonic() - start
    assert not bg.granted
    assert bg.decision == DECISION_SKIPPED_CONCURRENCY_LIMIT
    assert elapsed < 0.5  # fail fast, no queueing
    held.release()


def test_unknown_priority_is_treated_as_background(tmp_path):
    budget = _budget(tmp_path)
    held = budget.acquire_fetch_slot(url=YUYU_URL, priority=PRIORITY_MANUAL_RESEARCH)
    assert held.granted
    start = time.monotonic()
    other = budget.acquire_fetch_slot(url=YUYU_URL, priority="bogus-priority")
    elapsed = time.monotonic() - start
    assert not other.granted  # falls back to background → fails fast, doesn't wait
    assert elapsed < 0.5
    held.release()


# ── D4: bounded, configurable manual wait ─────────────────────────────────────

def test_manual_short_wait_succeeds_when_capacity_frees(tmp_path):
    budget = _budget(tmp_path, manual_wait_cap_seconds=5.0)
    held = budget.acquire_fetch_slot(url=YUYU_URL, priority=PRIORITY_MANUAL_RESEARCH)
    assert held.granted

    def _release_soon():
        time.sleep(0.2)
        held.release()

    t = threading.Thread(target=_release_soon)
    t.start()
    permit = budget.acquire_fetch_slot(url=YUYU_URL, priority=PRIORITY_MANUAL_RESEARCH)
    t.join()
    assert permit.granted
    assert permit.decision == DECISION_WAITED_THEN_GRANTED
    assert permit.wait_seconds >= 0.1
    permit.release()


def test_manual_wait_timeout_is_surfaced(tmp_path):
    budget = _budget(tmp_path, manual_wait_cap_seconds=0.2)
    assert budget.manual_wait_cap_seconds == 0.2
    held = budget.acquire_fetch_slot(url=YUYU_URL, priority=PRIORITY_MANUAL_RESEARCH)
    assert held.granted
    start = time.monotonic()
    permit = budget.acquire_fetch_slot(url=YUYU_URL, priority=PRIORITY_MANUAL_RESEARCH)
    elapsed = time.monotonic() - start
    assert not permit.granted
    assert permit.decision == DECISION_MANUAL_WAIT_TIMEOUT
    assert 0.15 <= elapsed < 2.0  # bounded by the configured cap
    held.release()


# ── D5: diagnostics & observability ───────────────────────────────────────────

def test_diagnostics_events_carry_core_fields(tmp_path):
    budget = _budget(tmp_path)
    p = budget.acquire_fetch_slot(url=YUYU_URL, requester=REQUESTER_RESEARCH,
                                  priority=PRIORITY_MANUAL_RESEARCH)
    p.release()
    events = budget.recent_decisions(host=YUYUTEI_HOST)
    assert events
    evt = events[0]
    assert evt.host == YUYUTEI_HOST
    assert evt.requester == REQUESTER_RESEARCH
    assert evt.priority == PRIORITY_MANUAL_RESEARCH
    assert evt.decision == DECISION_GRANTED


def test_decision_summary_aggregates_counts(tmp_path):
    budget = _budget(tmp_path)
    budget.store.trip_host_cooldown(YUYUTEI_HOST, cooldown_seconds=300, reason="429")
    for _ in range(3):
        budget.acquire_fetch_slot(url=YUYU_URL, priority=PRIORITY_BACKGROUND_ENRICHMENT)
    summary = budget.decision_summary(host=YUYUTEI_HOST)
    assert summary.get(DECISION_SKIPPED_COOLING_DOWN) == 3
