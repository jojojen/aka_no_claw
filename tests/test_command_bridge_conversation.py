"""Concurrency contracts for command-bridge conversation persistence."""

from __future__ import annotations

import threading

from openclaw_adapter.command_bridge_conversation import ConversationSession


def test_concurrent_orphaned_results_are_all_persisted(tmp_path):
    session = ConversationSession(str(tmp_path))
    start = threading.Barrier(5)
    workers = [
        threading.Thread(
            target=lambda value=value: (start.wait(), session.append_orphaned_result(value))
        )
        for value in ("one", "two", "three", "four")
    ]
    for worker in workers:
        worker.start()
    start.wait()
    for worker in workers:
        worker.join(timeout=2)
        assert not worker.is_alive()

    messages = session.load()["messages"]
    assert sorted(message["text"] for message in messages) == ["four", "one", "three", "two"]
