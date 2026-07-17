from __future__ import annotations

import pytest

from openclaw_adapter.prompt_queue import PromptQueueCapacityError, PromptQueueConflict
from openclaw_adapter.prompt_queue_store import PromptQueueStore


def _request(text: str) -> dict:
    return {"mode": "chat", "input": text, "session_id": "s1", "source": "test"}


def test_fifo_order_is_position_based_not_timestamp(tmp_path):
    store = PromptQueueStore(tmp_path, max_entries=3)
    first, _ = store.create("s1", intent="next_turn", request=_request("first"), now=99)
    second, _ = store.create("s1", intent="next_turn", request=_request("second"), now=1)
    claim, snapshot = store.claim_next("s1", now=100)
    assert claim is not None and claim.prompt_id == first.prompt_id
    assert [entry["text"] for entry in snapshot["entries"]] == ["first", "second"]


def test_edit_cancel_and_reorder_require_current_versions(tmp_path):
    store = PromptQueueStore(tmp_path)
    first, _ = store.create("s1", intent="next_turn", request=_request("first"))
    second, _ = store.create("s1", intent="next_turn", request=_request("second"))
    edited = store.edit("s1", first.prompt_id, text="first edited", expected_version=first.version)
    current = {entry["prompt_id"]: entry for entry in edited["entries"]}
    with pytest.raises(PromptQueueConflict):
        store.cancel("s1", first.prompt_id, expected_version=first.version)
    reordered = store.reorder(
        "s1", prompt_ids=[second.prompt_id, first.prompt_id],
        expected_versions={second.prompt_id: second.version, first.prompt_id: current[first.prompt_id]["version"]},
    )
    assert [entry["text"] for entry in reordered["entries"]] == ["second", "first edited"]


def test_capacity_expiry_and_restart_recovery(tmp_path):
    store = PromptQueueStore(tmp_path, max_entries=1, ttl_seconds=10)
    first, _ = store.create("s1", intent="next_turn", request=_request("first"), now=10)
    with pytest.raises(PromptQueueCapacityError):
        store.create("s1", intent="next_turn", request=_request("second"), now=11)
    claimed, _ = store.claim_next("s1", now=12)
    assert claimed is not None
    restarted = PromptQueueStore(tmp_path, max_entries=1, ttl_seconds=10)
    recovered = restarted.reconcile("s1", now=13)
    assert recovered["running_prompt_id"] is None
    assert recovered["entries"][0]["status"] == "queued"
    expired = restarted.snapshot("s1", now=30)
    assert expired["entries"] == []


def test_interjection_claim_is_bound_to_the_active_run(tmp_path):
    store = PromptQueueStore(tmp_path)
    entry, _ = store.create(
        "s1", intent="interjection", request=_request("only Japan"), target_run_id="run-a",
    )
    missing, _ = store.claim_interjection("s1", "run-b")
    assert missing is None
    claimed, _ = store.claim_interjection("s1", "run-a")
    assert claimed is not None and claimed.prompt_id == entry.prompt_id


def test_missed_interjection_becomes_a_safe_next_turn(tmp_path):
    store = PromptQueueStore(tmp_path)
    entry, _ = store.create(
        "s1", intent="interjection", request=_request("keep the constraint"), target_run_id="run-a",
    )

    fallback = store.fallback_interjections("s1", "run-a")

    assert len(fallback["entries"]) == 1
    result = fallback["entries"][0]
    assert result["prompt_id"] == entry.prompt_id
    assert result["intent"] == "next_turn"
    assert result["target_run_id"] is None
    assert result["version"] == entry.version + 1
    assert result["updated_at"] >= entry.updated_at


def test_release_returns_a_failed_start_to_the_visible_queue(tmp_path):
    store = PromptQueueStore(tmp_path)
    entry, _ = store.create("s1", intent="next_turn", request=_request("retry me"))
    claimed, _ = store.claim_next("s1")
    assert claimed is not None and claimed.prompt_id == entry.prompt_id

    released = store.release("s1", entry.prompt_id)
    assert released["running_prompt_id"] is None
    assert [(item["text"], item["status"]) for item in released["entries"]] == [("retry me", "queued")]
