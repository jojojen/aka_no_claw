from __future__ import annotations

import threading

import pytest

from openclaw_adapter.session_event_journal import (
    CursorExpiredError, JournalCorruptionError, JournalRetentionError, SessionEventJournal,
)


def _journal(tmp_path, **kwargs):
    return SessionEventJournal(str(tmp_path), "web-default", **kwargs)


def test_append_and_cursor_pages_are_exact(tmp_path):
    journal = _journal(tmp_path)
    for number in range(3):
        journal.append("tool.progress", run_id="run-1", payload={"stage": "x", "completed": number})
    first = journal.read(after=0, limit=2)
    second = journal.read(after=first.server_cursor, limit=2)
    assert [event.seq for event in first.events] == [1, 2]
    assert first.server_cursor == 2
    assert first.has_more is True
    assert [event.seq for event in second.events] == [3]
    assert second.server_cursor == 3
    assert second.has_more is False


def test_sequence_allocation_is_strict_under_threads(tmp_path):
    journal = _journal(tmp_path)
    threads = [threading.Thread(
        target=journal.append,
        kwargs={"event_type": "tool.progress", "run_id": f"run-{index}", "payload": {"stage": "x"}},
    ) for index in range(20)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert [event.seq for event in journal.events()] == list(range(1, 21))


def test_incomplete_tail_is_quarantined_and_recovered(tmp_path):
    journal = _journal(tmp_path)
    journal.append("run.accepted", run_id="run-1", payload={})
    with journal._active_path().open("ab") as handle:
        handle.write(b'{"half":')
    recovered = _journal(tmp_path)
    assert [event.seq for event in recovered.events()] == [1]
    assert list((recovered.directory / "quarantine").glob("*.tail"))


def test_malformed_committed_line_fails_visibly(tmp_path):
    journal = _journal(tmp_path)
    journal.append("run.accepted", run_id="run-1", payload={})
    with journal._active_path().open("a", encoding="utf-8") as handle:
        handle.write("{broken}\n")
    with pytest.raises(JournalCorruptionError):
        journal.events()


def test_retention_removes_only_completed_runs_and_expires_old_cursor(tmp_path):
    journal = _journal(tmp_path, max_bytes=600)
    journal.append("run.accepted", run_id="old-run", payload={"text": "x" * 80})
    journal.append("run.completed", run_id="old-run", payload={"text": "x" * 80})
    journal.append("run.accepted", run_id="new-run", payload={"text": "x" * 80})
    assert [event.run_id for event in journal.events()] == ["new-run"]
    with pytest.raises(CursorExpiredError):
        journal.read(after=0)


def test_retention_never_splits_an_active_run(tmp_path):
    journal = _journal(tmp_path, max_bytes=300)
    with pytest.raises(JournalRetentionError):
        journal.append("run.accepted", run_id="run-1", payload={"text": "x" * 160})


def test_age_retention_only_expires_a_whole_terminal_run(tmp_path):
    journal = _journal(tmp_path)
    journal.append("run.accepted", run_id="old-run", payload={}, occurred_at=1.0)
    journal.append("run.completed", run_id="old-run", payload={}, occurred_at=2.0)
    journal.append("run.accepted", run_id="active-run", payload={}, occurred_at=1.0)
    assert journal.expire_complete_runs_before(10.0) == 1
    assert [event.run_id for event in journal.events()] == ["active-run"]
