from __future__ import annotations

from openclaw_adapter.context_checkpoint import ContextCheckpointStore, ContextCompactor
from assistant_runtime.settings import AssistantSettings
from openclaw_adapter.command_bridge import CommandBridge
from openclaw_adapter.session_events import SessionRunEvent


def _event(seq: int, kind: str, text: str, *, run_id: str = "r1") -> SessionRunEvent:
    return SessionRunEvent(
        event_version=1, event_id=f"event-{seq}", session_id="s1", run_id=run_id, seq=seq,
        occurred_at=float(seq), type=kind, visibility="user", payload={"text": text},
    )


def test_checkpoint_has_exact_closed_range_and_keeps_recent_turns(tmp_path) -> None:
    compactor = ContextCompactor(ContextCheckpointStore(str(tmp_path)), recent_turns=2)
    events = [_event(1, "user.message", "我偏好日文資料"), _event(2, "assistant.message", "了解"),
              _event(3, "user.message", "最新問題"), _event(4, "assistant.message", "最新答案")]

    checkpoint = compactor.build("s1", events)

    assert checkpoint is not None
    assert (checkpoint.source_seq_start, checkpoint.source_seq_end) == (1, 2)
    assert "event-1" in checkpoint.summary
    assert "最新問題" not in checkpoint.summary
    assert compactor.latest("s1") == checkpoint


def test_secret_like_event_is_not_put_in_checkpoint(tmp_path) -> None:
    compactor = ContextCompactor(ContextCheckpointStore(str(tmp_path)), recent_turns=2)
    events = [_event(1, "user.message", "api_key=do-not-store"), _event(2, "assistant.message", "已收到"),
              _event(3, "user.message", "later"), _event(4, "assistant.message", "later answer")]

    checkpoint = compactor.build("s1", events)

    assert checkpoint is not None
    assert "do-not-store" not in checkpoint.summary


def test_clear_removes_only_checkpoint_store(tmp_path) -> None:
    compactor = ContextCompactor(ContextCheckpointStore(str(tmp_path)), recent_turns=2)
    events = [_event(1, "user.message", "old"), _event(2, "assistant.message", "answer"),
              _event(3, "user.message", "later"), _event(4, "assistant.message", "later answer")]
    assert compactor.build("s1", events)

    removed = compactor.clear("s1")

    assert removed is not None
    assert compactor.latest("s1") is None
    assert len(events) == 4


def test_compaction_never_reimports_messages_before_session_clear(tmp_path) -> None:
    compactor = ContextCompactor(ContextCheckpointStore(str(tmp_path)), recent_turns=2)
    events = [
        _event(1, "run.accepted", ""),
        _event(2, "user.message", "must be forgotten"),
        _event(3, "assistant.message", "old answer"),
        SessionRunEvent(
            event_version=1, event_id="event-4", session_id="s1", run_id="session",
            seq=4, occurred_at=4.0, type="context.checkpoint", visibility="internal",
            payload={"clear": True},
        ),
        _event(5, "assistant.message", "late old answer"),
        _event(6, "user.message", "new question", run_id="run-new"),
        _event(7, "assistant.message", "new answer", run_id="run-new"),
    ]

    assert compactor.build("s1", events) is None


def test_chained_checkpoint_keeps_previous_summary_and_extends_range(tmp_path) -> None:
    compactor = ContextCompactor(ContextCheckpointStore(str(tmp_path)), recent_turns=2)
    first_events = [_event(1, "user.message", "first constraint"), _event(2, "assistant.message", "first answer"),
                    _event(3, "user.message", "recent one"), _event(4, "assistant.message", "recent answer")]
    first = compactor.build("s1", first_events)
    assert first is not None
    second_events = first_events + [_event(5, "user.message", "second constraint"),
                                    _event(6, "assistant.message", "second answer")]

    second = compactor.build("s1", second_events)

    assert second is not None
    assert second.previous_checkpoint_id == first.checkpoint_id
    assert (second.source_seq_start, second.source_seq_end) == (1, 4)
    assert "first constraint" in second.summary
    assert "recent one" in second.summary


def test_checkpoint_separates_tool_evidence_from_unverified_assistant_claims(tmp_path) -> None:
    compactor = ContextCompactor(ContextCheckpointStore(str(tmp_path)), recent_turns=2)
    events = [
        _event(1, "user.message", "均價是一萬一", run_id="old"),
        SessionRunEvent(
            event_version=1, event_id="event-2", session_id="s1", run_id="old", seq=2,
            occurred_at=2.0, type="tool.result", visibility="internal",
            payload={
                "evidence_id": "e-market", "tool_call_id": "call-market",
                "source_type": "tool_result", "tool": "/research", "query": "item",
                "status": "ok", "content": "成交均價 ¥11,110", "summary": "market",
            },
        ),
        _event(3, "assistant.message", "鑑定後一定賣一萬三", run_id="old"),
        _event(4, "user.message", "現在已經八千了", run_id="new"),
        _event(5, "assistant.message", "收到更正", run_id="new"),
    ]

    checkpoint = compactor.build("s1", events)

    assert checkpoint is not None
    assert "使用者陳述／更正" in checkpoint.summary
    assert "工具證據 e-market" in checkpoint.summary
    assert "成交均價 ¥11,110" in checkpoint.summary
    assert "助理先前說法（未驗證）" in checkpoint.summary
    assert "現在已經八千了" not in checkpoint.summary


def test_checkpoint_drops_interrupted_assistant_partial(tmp_path) -> None:
    compactor = ContextCompactor(ContextCheckpointStore(str(tmp_path)), recent_turns=2)
    events = [
        _event(1, "user.message", "old", run_id="bad"),
        SessionRunEvent(
            event_version=1, event_id="event-2", session_id="s1", run_id="bad", seq=2,
            occurred_at=2.0, type="assistant.message", visibility="user",
            payload={"text": "unfinished false claim", "partial": True},
        ),
        _event(3, "run.interrupted", "", run_id="bad"),
        _event(4, "user.message", "new", run_id="good"),
        _event(5, "assistant.message", "answer", run_id="good"),
    ]

    checkpoint = compactor.build("s1", events)

    assert checkpoint is not None
    assert "unfinished false claim" not in checkpoint.summary


def test_auto_compaction_obeys_threshold_and_writes_an_event(tmp_path) -> None:
    bridge = CommandBridge(AssistantSettings(
        openclaw_web_memory_dir=str(tmp_path / "memory"), openclaw_web_event_dir=str(tmp_path / "events"),
        openclaw_web_context_window_tokens=4, openclaw_web_context_compact_threshold_percent=50,
        openclaw_web_context_recent_turns=2, openclaw_web_context_compact_cooldown_seconds=0,
    ))
    journal = bridge._event_sessions().ensure("s1")
    for kind, text in (("user.message", "old question"), ("assistant.message", "old answer"),
                       ("user.message", "recent question"), ("assistant.message", "recent answer")):
        journal.append(kind, run_id="r1", payload={"text": text})

    bridge._maybe_auto_compact_context("s1")

    assert bridge.context_status("s1")["checkpoint"] is not None
    assert any(event.type == "context.checkpoint" for event in journal.events())
