from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

from assistant_runtime import AssistantSettings
from openclaw_adapter.action_risk import (
    PolicyOutcome,
    RiskLevel,
    classify_generated_tool,
    decide_policy,
    include_dependency_install,
)
from openclaw_adapter.approval_models import FrozenActionManifest
from openclaw_adapter.approval_service import ApprovalService
from openclaw_adapter.approval_store import ApprovalStore
from openclaw_adapter.command_bridge import _WORKFLOW_APPROVAL_CONTEXT, _WorkflowApprovalContext, _WorkflowShimRunner
from openclaw_adapter.command_bridge import CommandBridge
from openclaw_adapter.knowledge_db import CODEGEN_SEED
from openclaw_adapter.run_recorder import RunRecorder
from openclaw_adapter.session_event_journal import SessionEventJournal


def _manifest(*, code: str = "print('ok')\n", arguments: dict | None = None, created_at: float = 10.0):
    profile = classify_generated_tool(code)
    return FrozenActionManifest.for_generated_tool(
        slug="demo", code=code, arguments=arguments or {"q": "x"}, dependencies=(),
        profile=profile,
        policy_version="ask_generated_writes", created_at=created_at,
    )


def test_risk_is_closed_and_generated_writes_require_approval():
    assert classify_generated_tool("print('ok')\n").risk is RiskLevel.READ_ONLY
    write = classify_generated_tool("from pathlib import Path\nPath('x').write_text('x')\n")
    assert write.risk is RiskLevel.PERSISTENT_WRITE
    assert decide_policy(write, "ask_generated_writes") is PolicyOutcome.ASK
    assert decide_policy(classify_generated_tool("import subprocess\n"), "ask_generated_writes") is PolicyOutcome.DENY
    assert decide_policy(write, "unknown") is PolicyOutcome.DENY
    post = classify_generated_tool("import requests\nrequests.post('https://example.com/items')\n")
    assert post.risk is RiskLevel.PERSISTENT_WRITE
    assert post.network_scopes == ("example.com",)
    delete = classify_generated_tool(
        "import requests\nrequests.delete('https://example.com/items/1')\n"
    )
    assert delete.risk is RiskLevel.DESTRUCTIVE
    urllib_post = classify_generated_tool(
        "import urllib.request\n"
        "request = urllib.request.Request('https://example.com/items', data=b'x')\n"
        "urllib.request.urlopen(request)\n"
    )
    assert urllib_post.risk is RiskLevel.PERSISTENT_WRITE
    assert urllib_post.network_scopes == ("example.com",)
    assert classify_generated_tool(
        "import smtplib\nsmtplib.SMTP('mail.example.com').sendmail('a', 'b', 'x')\n"
    ).risk is RiskLevel.PERSISTENT_WRITE
    assert classify_generated_tool(
        "import sqlite3\nsqlite3.connect('x.db').execute('INSERT INTO x VALUES (1)')\n"
    ).risk is RiskLevel.PERSISTENT_WRITE
    mixed = classify_generated_tool(
        "import requests\nfrom pathlib import Path\n"
        "requests.post('https://api.example.com/items')\nPath('x').write_text('x')\n"
    )
    assert mixed.capabilities == ("filesystem_write", "network_write")
    assert mixed.network_scopes == ("api.example.com",)
    assert mixed.filesystem_scopes == ("tool_workspace",)
    dependency = include_dependency_install(
        classify_generated_tool("print('ok')\n"), ("beautifulsoup4",)
    )
    assert dependency.risk is RiskLevel.PERSISTENT_WRITE
    assert "dependency_install" in dependency.capabilities
    assert dependency.network_scopes == ("python_package_index",)
    assert dependency.filesystem_scopes == ("generated_tool_virtualenv",)


def test_codegen_seed_records_manifest_bound_approval_lesson():
    assert any(
        entry["title"] == "一次性核准必須在消費邊界重驗完整綁定並以權威事件恢復狀態"
        for entry in CODEGEN_SEED
    )


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
        decision="approve", execute=lambda _: ("approved", "operator_approved", "done")
        if calls.append("ran") is None else ("execution_failed", "execution_failed", "bad"),
    )
    assert resolved.resolution == "approved"
    assert not duplicate
    assert calls == ["ran"]
    duplicate_record, duplicate = restarted.resolve(
        approval_id=record.approval_id, session_id="session", run_id="run", token=record.token,
        decision="approve", execute=lambda _: ("approved", "operator_approved", "bad")
        if calls.append("again") is None else ("execution_failed", "execution_failed", "bad"),
    )
    assert duplicate and duplicate_record.resolution == "approved"
    assert calls == ["ran"]


def test_concurrent_store_instances_allow_only_one_approval_execution(tmp_path: Path):
    def clock():
        return 100.0

    creator = ApprovalService(ApprovalStore(str(tmp_path)), ttl_seconds=30, clock=clock)
    record = creator.request(
        session_id="session", run_id="run", manifest=_manifest(), risk="persistent_write",
        descriptor={},
    )
    calls: list[str] = []

    def resolve_once():
        service = ApprovalService(ApprovalStore(str(tmp_path)), ttl_seconds=30, clock=clock)
        return service.resolve(
            approval_id=record.approval_id, session_id="session", run_id="run",
            token=record.token, decision="approve",
            execute=lambda _: (calls.append("ran") or ("approved", "operator_approved", "done")),
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: resolve_once(), range(2)))

    assert calls == ["ran"]
    assert sum(not duplicate for _, duplicate in results) == 1


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


def test_tampered_persisted_token_fails_hmac_validation(tmp_path: Path):
    service = ApprovalService(ApprovalStore(str(tmp_path)), ttl_seconds=30, clock=lambda: 100.0)
    record = service.request(
        session_id="s", run_id="r", manifest=_manifest(), risk="persistent_write", descriptor={},
    )
    tampered = record.to_dict()
    tampered["token"] = "0" * 64
    store_data = service.store._read()
    store_data[record.approval_id] = tampered
    service.store._write(store_data)
    try:
        service.resolve(
            approval_id=record.approval_id, session_id="s", run_id="r", token="0" * 64,
            decision="approve", execute=lambda _: ("approved", "operator_approved", "bad"),
        )
    except PermissionError:
        pass
    else:
        raise AssertionError("tampered token must fail closed")


def test_disabled_gate_and_execution_exception_resolve_without_retry(tmp_path: Path):
    service = ApprovalService(ApprovalStore(str(tmp_path)), ttl_seconds=30, clock=lambda: 100.0)
    disabled = service.request(
        session_id="s", run_id="disabled", manifest=_manifest(), risk="persistent_write", descriptor={},
    )
    result, _ = service.resolve(
        approval_id=disabled.approval_id, session_id="s", run_id="disabled",
        token=disabled.token, decision="approve", approval_enabled=False,
        execute=lambda _: (_ for _ in ()).throw(AssertionError("must not execute")),
    )
    assert result.resolution == "disabled"
    assert result.reason_code == "approval_disabled"

    failed = service.request(
        session_id="s", run_id="failed", manifest=_manifest(), risk="persistent_write", descriptor={},
    )
    result, _ = service.resolve(
        approval_id=failed.approval_id, session_id="s", run_id="failed",
        token=failed.token, decision="approve",
        execute=lambda _: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    assert result.status == "resolved"
    assert result.resolution == "execution_failed"
    assert result.reason_code == "execution_failed"


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
    shim.catalog = {"demo": {"slug": "demo"}}
    runner.validate_tool_artifact = lambda code: (True, "", ("requests",))
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
    assert "decision_token" in event.payload
    assert context.pending[0]["risk"] == "persistent_write"
    assert "dependency_install" in context.pending[0]["requested_capabilities"]


def test_enabled_web_approval_fails_closed_without_session_id():
    bridge = object.__new__(CommandBridge)
    bridge.settings = AssistantSettings(openclaw_web_approvals_enabled=True)
    bridge._active_run_recorder = None

    result = bridge.run_workflow_command("run demo")

    assert result["status"] == "error"
    assert "session_id" in result["message"]


def test_bridge_approval_revalidates_manifest_and_never_replays(tmp_path: Path):
    clock = [100.0]
    settings = AssistantSettings(
        openclaw_web_approvals_enabled=True,
        openclaw_web_approval_store_dir=str(tmp_path / "approvals"),
        openclaw_web_event_dir=str(tmp_path / "events"),
    )
    service = ApprovalService(
        ApprovalStore(settings.openclaw_web_approval_store_dir),
        ttl_seconds=30,
        clock=lambda: clock[0],
    )
    bridge = CommandBridge(settings)
    bridge._approval_service_inst = service
    bridge._workflow_surface = lambda: (None, None)
    tool_dir = tmp_path / "tools" / "demo"
    tool_dir.mkdir(parents=True)
    code = "from pathlib import Path\nPath('x').write_text('x')\n"
    (tool_dir / "tool.py").write_text(code, encoding="utf-8")
    calls: list[tuple[str, dict]] = []

    class ToolRunner:
        def validate_tool_artifact(self, current_code):
            return True, "", ()

        def run_tool_step(self, slug, arguments):
            calls.append((slug, arguments))
            return True, "done"

    bridge._workflow_runner = SimpleNamespace(
        catalog={"demo": {"slug": "demo"}},
        tools_dir=tmp_path / "tools",
        _tool_runner=ToolRunner(),
    )

    def request(run_id: str):
        arguments = {"path": "x"}
        return service.request(
            session_id="session",
            run_id=run_id,
            manifest=_manifest(code=code, arguments=arguments, created_at=clock[0]),
            risk="persistent_write",
            descriptor={"tool_slug": "demo", "arguments": arguments},
        )

    approved = request("approved")
    payload = {
        "approval_id": approved.approval_id,
        "session_id": approved.session_id,
        "run_id": approved.run_id,
        "decision_token": approved.token,
        "decision": "approve",
    }
    response = bridge.resolve_workflow_approval(payload)
    assert response["approval"]["resolution"] == "approved"
    assert calls == [("demo", {"path": "x"})]
    duplicate = bridge.resolve_workflow_approval(payload)
    assert duplicate["approval"]["idempotent"] is True
    assert calls == [("demo", {"path": "x"})]

    rejected = request("rejected")
    response = bridge.resolve_workflow_approval({
        "approval_id": rejected.approval_id,
        "session_id": rejected.session_id,
        "run_id": rejected.run_id,
        "decision_token": rejected.token,
        "decision": "reject",
    })
    assert response["approval"]["resolution"] == "reject"
    assert calls == [("demo", {"path": "x"})]

    changed = request("changed")
    (tool_dir / "tool.py").write_text("print('changed')\n", encoding="utf-8")
    response = bridge.resolve_workflow_approval({
        "approval_id": changed.approval_id,
        "session_id": changed.session_id,
        "run_id": changed.run_id,
        "decision_token": changed.token,
        "decision": "approve",
    })
    assert response["approval"]["resolution"] == "manifest_mismatch"
    assert calls == [("demo", {"path": "x"})]

    (tool_dir / "tool.py").write_text(code, encoding="utf-8")
    expired = request("expired")
    clock[0] = expired.expires_at
    response = bridge.resolve_workflow_approval({
        "approval_id": expired.approval_id,
        "session_id": expired.session_id,
        "run_id": expired.run_id,
        "decision_token": expired.token,
        "decision": "approve",
    })
    assert response["approval"]["resolution"] == "expired"
    assert calls == [("demo", {"path": "x"})]

    resolved_events = [
        event for event in bridge._event_sessions().journal("session").events()
        if event.type == "approval.resolved"
    ]
    assert len(resolved_events) == 4
    assert all("decision_token" not in event.payload for event in resolved_events)
