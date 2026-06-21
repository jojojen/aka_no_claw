"""Tests for async job persistence + reconnect (aka_no_claw #37).

Covers: JobStore round-trip, expiry purge, and poll_job fallback behaviour when
the in-memory job is missing (done / error / interrupted / not_found).
"""
from __future__ import annotations

import time
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from openclaw_adapter.job_store import MAX_AGE_SECONDS, JobStore
from openclaw_adapter.command_bridge import (
    JOB_DONE,
    JOB_ERROR,
    JOB_INTERRUPTED,
    JOB_RUNNING,
    CommandBridge,
)


@pytest.fixture
def store(tmp_path):
    return JobStore(str(tmp_path / "web_jobs"))


# --- JobStore round-trip ---------------------------------------------------

def test_save_and_load_roundtrip(store):
    snap = {
        "job_id": "abc123",
        "status": JOB_DONE,
        "progress": ["step 1", "step 2"],
        "message": "研究完成",
        "actions": [{"label": "看市價", "callback_data": "rs:tok:price"}],
        "error": None,
        "created_at": 1000.0,
        "updated_at": 2000.0,
    }
    store.save(snap)
    loaded = store.load("abc123")
    assert loaded is not None
    assert loaded["status"] == JOB_DONE
    assert loaded["message"] == "研究完成"
    assert len(loaded["actions"]) == 1


def test_load_missing_returns_none(store):
    assert store.load("nonexistent") is None


def test_load_corrupt_returns_none(store, tmp_path):
    (tmp_path / "web_jobs").mkdir(parents=True, exist_ok=True)
    (tmp_path / "web_jobs" / "bad.json").write_text("{ not json", encoding="utf-8")
    assert store.load("bad") is None


def test_purge_expired_deletes_old_files(store):
    old_ts = time.time() - MAX_AGE_SECONDS - 1
    recent_ts = time.time() - 60
    store.save({"job_id": "old", "status": JOB_DONE, "updated_at": old_ts})
    store.save({"job_id": "recent", "status": JOB_DONE, "updated_at": recent_ts})
    deleted = store.purge_expired()
    assert deleted == 1
    assert store.load("old") is None
    assert store.load("recent") is not None


def test_purge_expired_missing_dir_is_ok(tmp_path):
    store = JobStore(str(tmp_path / "nonexistent"))
    assert store.purge_expired() == 0


# --- CommandBridge.poll_job fallback behavior ------------------------------

def _bridge(tmp_path) -> CommandBridge:
    settings = SimpleNamespace(
        openclaw_web_memory_dir=str(tmp_path / "mem"),
        openclaw_web_jobs_dir=str(tmp_path / "jobs"),
    )
    return CommandBridge(settings=settings)


def test_poll_in_memory_running_job(tmp_path):
    b = _bridge(tmp_path)
    job = b._jobs.create()
    snap = b.poll_job(job.id)
    assert snap["job_status"] == JOB_RUNNING
    assert snap["progress"] == []


def test_poll_completed_persisted_job_after_in_memory_gc(tmp_path):
    b = _bridge(tmp_path)
    store = b._get_job_store()
    store.save({
        "job_id": "done-job",
        "status": JOB_DONE,
        "progress": ["進行中", "完成"],
        "message": "最終報告",
        "actions": [{"label": "看市價", "callback_data": "rs:x:price"}],
        "error": None,
        "created_at": time.time(),
        "updated_at": time.time(),
    })
    snap = b.poll_job("done-job")
    assert snap["job_status"] == JOB_DONE
    assert snap["message"] == "最終報告"
    assert len(snap["actions"]) == 1
    assert snap["error"] is None


def test_poll_error_persisted_job(tmp_path):
    b = _bridge(tmp_path)
    store = b._get_job_store()
    store.save({
        "job_id": "err-job",
        "status": JOB_ERROR,
        "progress": [],
        "message": "",
        "actions": [],
        "error": "connection refused",
        "created_at": time.time(),
        "updated_at": time.time(),
    })
    snap = b.poll_job("err-job")
    assert snap["job_status"] == JOB_ERROR
    assert "connection refused" in (snap.get("error") or "")


def test_poll_running_persisted_without_worker_returns_interrupted(tmp_path):
    b = _bridge(tmp_path)
    store = b._get_job_store()
    store.save({
        "job_id": "orphan-job",
        "status": JOB_RUNNING,
        "progress": ["step 1"],
        "message": "",
        "actions": [],
        "error": None,
        "created_at": time.time(),
        "updated_at": time.time(),
    })
    snap = b.poll_job("orphan-job")
    assert snap["job_status"] == JOB_INTERRUPTED
    assert snap["progress"] == ["step 1"]


def test_poll_missing_job_returns_not_found(tmp_path):
    b = _bridge(tmp_path)
    snap = b.poll_job("ghost")
    assert snap.get("not_found") is True
    assert snap["job_status"] == JOB_ERROR


def test_start_async_persists_initial_snapshot(tmp_path):
    b = _bridge(tmp_path)
    # Patch _run_command_raw to return immediately without actually running research.
    with patch.object(b, "_run_command_raw", return_value=("報告完成", None)):
        result = b.start_async(
            SimpleNamespace(
                mode="investment",
                submode="deep_product_research",
                input="https://example.com/item/123",
                chat_backend="local",
                attachments=[],
                source="test",
            )
        )
    assert result["status"] == "accepted"
    job_id = result["job_id"]
    # Allow worker thread to complete.
    import time as _time
    _time.sleep(0.2)
    snap = b._get_job_store().load(job_id)
    assert snap is not None
    assert snap["status"] == JOB_DONE
    assert snap["message"] == "報告完成"


def test_start_async_persists_error_snapshot(tmp_path):
    b = _bridge(tmp_path)
    with patch.object(b, "_run_command_raw", side_effect=RuntimeError("boom")):
        result = b.start_async(
            SimpleNamespace(
                mode="investment",
                submode="deep_product_research",
                input="https://example.com/item/456",
                chat_backend="local",
                attachments=[],
                source="test",
            )
        )
    job_id = result["job_id"]
    import time as _time
    _time.sleep(0.2)
    snap = b._get_job_store().load(job_id)
    assert snap is not None
    assert snap["status"] == JOB_ERROR
    assert "boom" in (snap.get("error") or "")
