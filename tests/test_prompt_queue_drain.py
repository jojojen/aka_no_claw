from __future__ import annotations

from threading import Event, Lock
import time

from openclaw_adapter.command_bridge import CommandBridge
from openclaw_adapter.prompt_queue_store import PromptQueueStore


def _bridge_for(store: PromptQueueStore, streamed: Event, *, fail: bool = False) -> CommandBridge:
    bridge = CommandBridge.__new__(CommandBridge)
    bridge._queue_drain_lock = Lock()
    bridge._queue_draining_sessions = set()
    bridge._queue_enabled = lambda: True
    bridge._prompt_queue = lambda: store
    bridge._reconcile_prompt_queue_once = lambda session_id: store.snapshot(session_id)
    bridge._queue_changed = lambda _session_id, _snapshot: None
    bridge.seen_source_prompt_ids = []

    def stream(request, _run_id):
        bridge.seen_source_prompt_ids.append(request.source_prompt_id)
        streamed.set()
        if fail:
            raise RuntimeError("simulated start failure")
        yield {"type": "done"}

    bridge.stream = stream
    return bridge


def test_drain_claims_and_completes_exactly_once(tmp_path):
    store = PromptQueueStore(tmp_path)
    store.create("s1", intent="next_turn", request={"mode": "chat", "input": "queued", "session_id": "s1", "source": "test"})
    streamed = Event()
    bridge = _bridge_for(store, streamed)

    bridge._maybe_drain_prompt_queue("s1")

    assert streamed.wait(2)
    assert store.snapshot("s1")["entries"] == []
    assert bridge.seen_source_prompt_ids and bridge.seen_source_prompt_ids[0]


def test_drain_start_failure_releases_prompt_for_visible_retry(tmp_path):
    store = PromptQueueStore(tmp_path)
    store.create("s1", intent="next_turn", request={"mode": "chat", "input": "queued", "session_id": "s1", "source": "test"})
    streamed = Event()
    bridge = _bridge_for(store, streamed, fail=True)

    bridge._maybe_drain_prompt_queue("s1")

    assert streamed.wait(2)
    # The queue is kept durable and visible; the worker does not spin forever
    # retrying a permanent start error.
    for _ in range(100):
        entries = store.snapshot("s1")["entries"]
        if entries and entries[0]["status"] == "queued":
            break
        time.sleep(0.01)
    assert entries[0]["status"] == "queued"
