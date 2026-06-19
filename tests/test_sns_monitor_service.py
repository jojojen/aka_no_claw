from __future__ import annotations

import threading

from openclaw_adapter.sns_monitor_service import (
    _run_knowledge_inbox_poller,
    _run_sns_inbox_poller,
)


class _DummyKnowledgeDb:
    pass


class _DummySnsDb:
    pass


def test_sns_inbox_poller_bootstraps_missing_db_file(tmp_path) -> None:
    db_path = tmp_path / "nested" / "sns_inbox.sqlite3"
    stop_event = threading.Event()
    stop_event.set()

    _run_sns_inbox_poller(_DummySnsDb(), str(db_path), stop_event)

    assert db_path.exists()


def test_knowledge_inbox_poller_bootstraps_missing_db_file(tmp_path) -> None:
    db_path = tmp_path / "nested" / "knowledge_inbox.sqlite3"
    stop_event = threading.Event()
    stop_event.set()

    _run_knowledge_inbox_poller(_DummyKnowledgeDb(), str(db_path), stop_event)

    assert db_path.exists()
