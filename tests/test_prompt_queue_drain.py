from __future__ import annotations

from threading import Event, Lock
import time

from openclaw_adapter.command_bridge import CommandBridge
from openclaw_adapter.prompt_queue_store import PromptQueueStore
from openclaw_adapter.run_recorder import RunRecorder
from openclaw_adapter.session_event_journal import SessionEventJournal


def _bridge_for(store: PromptQueueStore, streamed: Event, *, fail: bool = False) -> CommandBridge:
    bridge = CommandBridge.__new__(CommandBridge)
    bridge._queue_drain_lock = Lock()
    bridge._queue_draining_sessions = set()
    bridge._queue_enabled = lambda: True
    bridge._prompt_queue = lambda: store
    bridge._reconcile_prompt_queue_once = lambda session_id: store.snapshot(session_id)
    bridge._queue_changed = lambda _session_id, _snapshot: None
    bridge._queued_prompt_prior_run = lambda _session_id, _prompt_id: (None, None)
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


def test_drain_start_failure_requires_visible_explicit_retry(tmp_path):
    store = PromptQueueStore(tmp_path)
    store.create("s1", intent="next_turn", request={"mode": "chat", "input": "queued", "session_id": "s1", "source": "test"})
    streamed = Event()
    bridge = _bridge_for(store, streamed, fail=True)

    bridge._maybe_drain_prompt_queue("s1")

    assert streamed.wait(2)
    # The queue is kept durable and visible; polling cannot spin forever
    # retrying a permanent start error.
    for _ in range(100):
        entries = store.snapshot("s1")["entries"]
        if entries and entries[0]["status"] == "interrupted":
            break
        time.sleep(0.01)
    assert entries[0]["status"] == "interrupted"


def test_recovered_accepted_prompt_is_not_executed_again(tmp_path):
    store = PromptQueueStore(tmp_path)
    store.create("s1", intent="next_turn", request={"mode": "chat", "input": "queued", "session_id": "s1", "source": "test"})
    streamed = Event()
    bridge = _bridge_for(store, streamed)
    bridge._queued_prompt_prior_run = lambda _session_id, _prompt_id: ("interrupted", "old-run")

    bridge._maybe_drain_prompt_queue("s1")

    for _ in range(100):
        entries = store.snapshot("s1")["entries"]
        if entries and entries[0]["status"] == "interrupted":
            break
        time.sleep(0.01)
    assert entries[0]["status"] == "interrupted"
    assert not streamed.is_set()


def test_recovered_terminal_prompt_is_completed_without_replay(tmp_path):
    store = PromptQueueStore(tmp_path)
    store.create("s1", intent="next_turn", request={"mode": "chat", "input": "queued", "session_id": "s1", "source": "test"})
    streamed = Event()
    bridge = _bridge_for(store, streamed)
    bridge._queued_prompt_prior_run = lambda _session_id, _prompt_id: ("completed", "old-run")

    bridge._maybe_drain_prompt_queue("s1")

    for _ in range(100):
        if not store.snapshot("s1")["entries"]:
            break
        time.sleep(0.01)
    assert store.snapshot("s1")["entries"] == []
    assert not streamed.is_set()


def test_prior_run_lookup_uses_latest_attempt_and_requires_success(tmp_path):
    journal = SessionEventJournal(str(tmp_path / "events"), "s1")
    first = RunRecorder(journal, run_id="first-run")
    first.accepted("queued", source_prompt_id="prompt-1")
    first.terminal("completed")
    latest = RunRecorder(journal, run_id="latest-run")
    latest.accepted("queued", source_prompt_id="prompt-1")

    bridge = CommandBridge.__new__(CommandBridge)
    bridge._event_sessions = lambda: type(
        "Events", (), {"ensure": lambda _self, _session_id: journal}
    )()

    assert bridge._queued_prompt_prior_run("s1", "prompt-1") == (
        "interrupted", "latest-run"
    )
    latest.terminal("failed")
    assert bridge._queued_prompt_prior_run("s1", "prompt-1") == (
        "interrupted", "latest-run"
    )

    final = RunRecorder(journal, run_id="final-run")
    final.accepted("queued", source_prompt_id="prompt-1")
    final.terminal("completed")
    assert bridge._queued_prompt_prior_run("s1", "prompt-1") == (
        "completed", "final-run"
    )
