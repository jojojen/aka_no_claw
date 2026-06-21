"""Issue #22 — Shared Host Budget State & Policy Registry.

Tests map onto the deliverables:
- HostPolicy model + safe default + yuyu-tei default + load from defaults (D1)
- SQLite HostBudgetStore: shared across processes, survives restart, configurable
  path, idempotent bootstrap (D2)
- cooldown API: read/write, longer-never-shortened, records who/why/status,
  expired cleared/ignored (D3)
- request event logging: host/requester/priority/decision/created_at, URL hashed
  only, queryable (D4)
"""
from __future__ import annotations

import sqlite3
import time

from market_monitor.host_budget import (
    DECISION_GRANTED,
    DECISION_SKIPPED_COOLING_DOWN,
    DEFAULT_HOST_POLICY,
    YUYUTEI_HOST,
    HostBudgetStore,
    HostPolicy,
    normalize_host,
    policy_for,
)


# --- D1: policy model + defaults ----------------------------------------------

def test_host_policy_model_fields():
    p = HostPolicy(host="example.com", requests_per_minute=10,
                   min_interval_seconds=5.0, max_concurrency=1,
                   cooldown_seconds=60, enabled=True)
    assert p.host == "example.com"
    assert p.max_concurrency == 1


def test_unknown_host_falls_back_to_safe_default(tmp_path):
    store = HostBudgetStore(tmp_path / "hb.db")
    p = store.get_policy("totally-unknown-host.example")
    assert p.host == "totally-unknown-host.example"
    assert p.max_concurrency == DEFAULT_HOST_POLICY.max_concurrency
    assert p.cooldown_seconds == DEFAULT_HOST_POLICY.cooldown_seconds


def test_yuyutei_default_policy_exists(tmp_path):
    store = HostBudgetStore(tmp_path / "hb.db")
    p = store.get_policy(YUYUTEI_HOST)
    assert p.host == YUYUTEI_HOST
    assert p.max_concurrency == 1
    assert p.min_interval_seconds >= 10.0
    assert p.cooldown_seconds == 300


def test_policy_for_helper_without_store():
    assert policy_for(YUYUTEI_HOST).max_concurrency == 1
    assert policy_for("https://www.elsewhere.example/x").host == "www.elsewhere.example"


def test_policies_can_be_loaded_and_overridden(tmp_path):
    store = HostBudgetStore(tmp_path / "hb.db")
    custom = HostPolicy(host="yuyu-tei.jp", requests_per_minute=3,
                        min_interval_seconds=20.0, max_concurrency=1,
                        cooldown_seconds=600, enabled=False)
    store.upsert_policy(custom)
    loaded = store.get_policy(YUYUTEI_HOST)
    assert loaded.min_interval_seconds == 20.0
    assert loaded.enabled is False


# --- D2: store sharing / durability / idempotency -----------------------------

def test_bootstrap_is_idempotent(tmp_path):
    path = tmp_path / "hb.db"
    HostBudgetStore(path)
    HostBudgetStore(path)  # second open must not error
    store = HostBudgetStore(path)
    assert store.get_policy(YUYUTEI_HOST).host == YUYUTEI_HOST


def test_state_shared_across_store_instances(tmp_path):
    # Two store instances on the same path simulate two OpenClaw processes.
    path = tmp_path / "hb.db"
    writer = HostBudgetStore(path)
    reader = HostBudgetStore(path)
    writer.trip_host_cooldown("yuyu-tei.jp", reason="429", requester="agent_a",
                              cooldown_seconds=300)
    cd = reader.get_host_cooldown("yuyu-tei.jp")
    assert cd is not None and cd.active
    assert cd.requester == "agent_a"


def test_cooldown_survives_new_store_instance(tmp_path):
    path = tmp_path / "hb.db"
    HostBudgetStore(path).trip_host_cooldown("yuyu-tei.jp", cooldown_seconds=300,
                                             reason="429")
    # A fresh instance (process restart) still sees the cooldown.
    cd = HostBudgetStore(path).get_host_cooldown("yuyu-tei.jp")
    assert cd is not None and cd.remaining_seconds > 0


def test_store_path_is_configurable(tmp_path):
    path = tmp_path / "nested" / "custom_budget.sqlite3"
    store = HostBudgetStore(path)
    assert store.path == path
    assert path.exists()


# --- D3: cooldown API ----------------------------------------------------------

def test_cooldown_read_write(tmp_path):
    store = HostBudgetStore(tmp_path / "hb.db")
    assert store.get_host_cooldown("yuyu-tei.jp") is None
    store.trip_host_cooldown("yuyu-tei.jp", reason="429", requester="agent",
                             cooldown_seconds=300, last_status=429)
    cd = store.get_host_cooldown("yuyu-tei.jp")
    assert cd is not None
    assert cd.reason == "429"
    assert cd.requester == "agent"
    assert cd.last_status == 429
    assert cd.tripped_at is not None


def test_longer_cooldown_is_not_shortened(tmp_path):
    store = HostBudgetStore(tmp_path / "hb.db")
    store.trip_host_cooldown("yuyu-tei.jp", cooldown_seconds=600, requester="long")
    long_cd = store.get_host_cooldown("yuyu-tei.jp")
    # A later, shorter trip must not reduce the standing cooldown.
    store.trip_host_cooldown("yuyu-tei.jp", cooldown_seconds=30, requester="short")
    cd = store.get_host_cooldown("yuyu-tei.jp")
    assert cd is not None
    assert abs(cd.expires_at - long_cd.expires_at) < 1.0
    assert cd.requester == "long"


def test_shorter_cooldown_can_extend_when_longer(tmp_path):
    store = HostBudgetStore(tmp_path / "hb.db")
    store.trip_host_cooldown("yuyu-tei.jp", cooldown_seconds=30)
    store.trip_host_cooldown("yuyu-tei.jp", cooldown_seconds=600, requester="extend")
    cd = store.get_host_cooldown("yuyu-tei.jp")
    assert cd is not None and cd.remaining_seconds > 60
    assert cd.requester == "extend"


def test_expired_cooldown_ignored_and_cleared(tmp_path):
    store = HostBudgetStore(tmp_path / "hb.db")
    store.trip_host_cooldown("yuyu-tei.jp", cooldown_seconds=-1)  # already expired
    assert store.get_host_cooldown("yuyu-tei.jp") is None  # ignored on read
    cleared = store.clear_expired_cooldowns()
    assert cleared >= 1
    # Row is gone; clearing again removes nothing.
    assert store.clear_expired_cooldowns() == 0


def test_cooldown_defaults_to_policy_duration(tmp_path):
    store = HostBudgetStore(tmp_path / "hb.db")
    before = time.time()
    store.trip_host_cooldown("yuyu-tei.jp", reason="429")  # no explicit seconds
    cd = store.get_host_cooldown("yuyu-tei.jp")
    assert cd is not None
    # Yuyutei policy cooldown is 300s.
    assert 250 <= (cd.expires_at - before) <= 320


# --- D4: request event logging ------------------------------------------------

def test_request_event_logging_records_core_fields(tmp_path):
    store = HostBudgetStore(tmp_path / "hb.db")
    evt = store.log_request_event(
        host="yuyu-tei.jp", decision=DECISION_GRANTED, requester="research_command",
        priority="manual_research", url="https://yuyu-tei.jp/sell/ws/card/123",
        wait_seconds=1.5, reason="ok",
    )
    assert evt.host == "yuyu-tei.jp"
    assert evt.requester == "research_command"
    assert evt.priority == "manual_research"
    assert evt.decision == DECISION_GRANTED
    assert evt.created_at is not None


def test_request_event_stores_url_hash_not_raw(tmp_path):
    path = tmp_path / "hb.db"
    store = HostBudgetStore(path)
    raw_url = "https://yuyu-tei.jp/sell/ws/card/secret-12345"
    store.log_request_event(host="yuyu-tei.jp", decision=DECISION_GRANTED, url=raw_url)
    # The raw URL must never be persisted anywhere in the events table.
    with sqlite3.connect(path) as conn:
        rows = conn.execute("SELECT url_hash FROM host_request_events").fetchall()
    assert rows and rows[0][0] is not None
    assert raw_url not in str(rows[0][0])
    assert "secret-12345" not in str(rows[0][0])


def test_request_events_are_queryable(tmp_path):
    store = HostBudgetStore(tmp_path / "hb.db")
    store.log_request_event(host="yuyu-tei.jp", decision=DECISION_GRANTED,
                            requester="a")
    store.log_request_event(host="yuyu-tei.jp", decision=DECISION_SKIPPED_COOLING_DOWN,
                            requester="b")
    store.log_request_event(host="other.example", decision=DECISION_GRANTED)
    yuyu = store.recent_events(host="yuyu-tei.jp")
    assert len(yuyu) == 2
    assert {e.decision for e in yuyu} == {DECISION_GRANTED, DECISION_SKIPPED_COOLING_DOWN}
    assert len(store.recent_events(limit=10)) == 3


# --- host normalization -------------------------------------------------------

def test_normalize_host_handles_urls_and_bare_hosts():
    assert normalize_host("https://www.YUYU-TEI.jp/sell/x") == "www.yuyu-tei.jp"
    assert normalize_host("Yuyu-Tei.jp") == "yuyu-tei.jp"
