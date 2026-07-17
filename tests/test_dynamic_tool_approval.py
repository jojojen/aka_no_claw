from __future__ import annotations

from pathlib import Path

from assistant_runtime import AssistantSettings
from openclaw_adapter.action_risk import PolicyOutcome, RiskLevel, classify_generated_tool, decide_policy
from openclaw_adapter.approval_models import FrozenActionManifest
from openclaw_adapter.approval_service import ApprovalService
from openclaw_adapter.approval_store import ApprovalStore
from openclaw_adapter.command_bridge import _WORKFLOW_APPROVAL_CONTEXT, _WorkflowApprovalContext, _WorkflowShimRunner
from openclaw_adapter.run_recorder import RunRecorder
from openclaw_adapter.session_event_journal import SessionEventJournal


def _manifest(*, code: str = "print('ok')\n", arguments: dict | None = None, created_at: float = 10.0):
    profile = classify_generated_tool(code)
    return FrozenActionManifest.for_generated_tool(
        slug="demo", code=code, arguments=arguments or {"q": "x"}, profile=profile,
        policy_version="ask_generated_writes", created_at=created_at,
    )


def test_risk_is_closed_and_generated_writes_require_approval():
    assert classify_generated_tool("print('ok')\n").risk is RiskLevel.READ_ONLY
    write = classify_generated_tool("from pathlib import Path\nPath('x').write_text('x')\n")
    assert write.risk is RiskLevel.PERSISTENT_WRITE
    assert decide_policy(write, "ask_generated_writes") is PolicyOutcome.ASK
    assert decide_policy(classify_generated_tool("import subprocess\n"), "ask_generated_writes") is PolicyOutcome.DENY
    assert decide_policy(write, "unknown") is PolicyOutcome.DENY


def test_manifest_hash_is_canonical_and_binds_code_and_arguments():
    first = _manifest(arguments={"a": 1, "b": 2})
    same = _manifest(arguments={"b": 2, "a": 1})
    changed_code = _manifest(code="print('changed')\n", arguments={"a": 1, "b": 2})
    changed_args = _manifest(arguments={"a": 2, "b": 2})
    assert first.hash == same.hash
    assert first.hash != changed_code.hash
    assert first.hash != changed_args.hash


def test_approval_is_persisted_single_use_and_restart_safe(tmp_path: Path):
    clock = [100.0]
    service = ApprovalService(ApprovalStore(str(tmp_path)), ttl_seconds=30, clock=lambda: clock[0])
    record = service.request(
        session_id="session", run_id="run", manifest=_manifest(), risk="persistent_write",
        descriptor={"tool_slug": "demo", "arguments": {"q": "x"}},
    )
    restarted = ApprovalService(ApprovalStore(str(tmp_path)), ttl_seconds=30, clock=lambda: clock[0])
    calls: list[str] = []
    resolved, duplicate = restarted.resolve(
        approval_id=record.approval_id, session_id="session", run_id="run", token=record.token,
        decision="approve", execute=lambda _: (calls.append("ran") is None, "done"),
    )
    assert resolved.resolution == "approved"
    assert not duplicate
    assert calls == ["ran"]
    duplicate_record, duplicate = restarted.resolve(
        approval_id=record.approval_id, session_id="session", run_id="run", token=record.token,
        decision="approve", execute=lambda _: (calls.append("again") is None, "bad"),
    )
    assert duplicate and duplicate_record.resolution == "approved"
    assert calls == ["ran"]


def test_expiry_and_rejection_do_not_execute(tmp_path: Path):
    clock = [100.0]
    service = ApprovalService(ApprovalStore(str(tmp_path)), ttl_seconds=1, clock=lambda: clock[0])
    expired = service.request(session_id="s", run_id="r", manifest=_manifest(), risk="persistent_write", descriptor={})
    clock[0] = 101.0
    result, _ = service.resolve(
        approval_id=expired.approval_id, session_id="s", run_id="r", token=expired.token,
        decision="approve", execute=lambda _: (_ for _ in ()).throw(AssertionError("must not execute")),
    )
    assert result.resolution == "expired"
    rejected = service.request(session_id="s", run_id="r2", manifest=_manifest(), risk="persistent_write", descriptor={})
    result, _ = service.resolve(
        approval_id=rejected.approval_id, session_id="s", run_id="r2", token=rejected.token,
        decision="reject", execute=lambda _: (_ for _ in ()).throw(AssertionError("must not execute")),
    )
    assert result.resolution == "reject"


def test_web_shim_pauses_write_before_tool_runner_and_emits_safe_event(tmp_path: Path):
    tool = tmp_path / "demo"
    tool.mkdir()
    (tool / "tool.py").write_text("from pathlib import Path\nPath('x').write_text('x')\n", encoding="utf-8")

    class ToolRunner:
        def __init__(self):
            self.calls = []

        def run_tool_step(self, slug, arguments):
            self.calls.append((slug, arguments))
            return True, "must not run before approval"

    runner = ToolRunner()
    shim = object.__new__(_WorkflowShimRunner)
    shim.tools_dir = tmp_path
    shim._tool_runner = runner
    shim._approval_settings = AssistantSettings(
        openclaw_web_approvals_enabled=True,
        openclaw_web_approval_store_dir=str(tmp_path / "approvals"),
    )
    service = ApprovalService(ApprovalStore(str(tmp_path / "approvals")), ttl_seconds=30)
    recorder = RunRecorder(SessionEventJournal(str(tmp_path / "events"), "session"), run_id="run")
    context = _WorkflowApprovalContext(service, recorder, [])
    token = _WORKFLOW_APPROVAL_CONTEXT.set(context)
    try:
        ok, message = shim.run_tool_step("demo", {"path": "x"})
    finally:
        _WORKFLOW_APPROVAL_CONTEXT.reset(token)
    assert not ok and "等待" in message
    assert runner.calls == []
    event = recorder.journal.events()[-1]
    assert event.type == "approval.requested"
    assert "source" not in event.payload and "arguments" not in event.payload
    assert context.pending[0]["risk"] == "persistent_write"
