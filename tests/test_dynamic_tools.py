"""Unit tests for DynamicToolRunner — no network, no real model, no real venv.

The Ollama client and the venv python are faked so these run fast and offline.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from openclaw_adapter.dynamic_tools import (
    DynamicToolRunner,
    _extract_answer,
    _extract_code,
    _check_numeric,
    _check_direction,
)
from openclaw_adapter.knowledge_db import KnowledgeDatabase


GOOD_SCRIPT = (
    'print("===ANSWER===")\n'
    'print("結果 42（計算依據：常數）")\n'
    'print("===END===")\n'
)
BAD_SCRIPT = 'import sys\nsys.stderr.write("boom\\n")\nsys.exit(1)\n'
SECRET_PROBE = (
    "import os\n"
    'print("===ANSWER===")\n'
    'print("TOKEN=" + repr(os.environ.get("OPENCLAW_TELEGRAM_BOT_TOKEN")))\n'
    'print("===END===")\n'
)


class FakeClient:
    """Routes generate() by prompt markers to canned responses."""

    def __init__(self, *, code_responses, pick_response="NONE", distill_response="{}"):
        self._code = list(code_responses)
        self._pick = pick_response
        self._distill = distill_response
        self.calls = {"pick": 0, "code": 0, "repair": 0, "distill": 0}
        self.timeout_seconds = 420

    def generate(self, prompt: str, *, temperature: float = 0.0, think: bool = False) -> str:
        if "既有工具" in prompt:
            self.calls["pick"] += 1
            return self._pick
        if "抽象成" in prompt:
            self.calls["distill"] += 1
            return self._distill
        if "執行失敗" in prompt:
            self.calls["repair"] += 1
        else:
            self.calls["code"] += 1
        # both codegen and repair pull from the same queue
        return "===CODE===\n" + self._code.pop(0)


def _make_runner(tmp_path: Path, client: FakeClient, db=None) -> DynamicToolRunner:
    runner = DynamicToolRunner(
        client=client,
        tools_dir=tmp_path / "generated_tools",
        knowledge_db=db,
        exec_timeout_seconds=30,
    )
    # Use the test interpreter directly — skip building a real venv.
    runner._ensure_venv = lambda: Path(sys.executable)  # type: ignore[assignment]
    runner._pip_install = lambda packages: None  # type: ignore[assignment]
    return runner


def test_extract_helpers():
    assert _extract_answer("x\n===ANSWER===\nhi\n===END===\ny") == "hi"
    code = _extract_code("===PLAN===\np\n===CODE===\nprint(1)\n")
    assert "print(1)" in code


def test_success_first_try_writes_manifest(tmp_path):
    client = FakeClient(code_responses=[GOOD_SCRIPT])
    runner = _make_runner(tmp_path, client)
    res = runner.run_detailed("計算常數")
    assert res.ok
    assert res.generations == 1
    assert "42" in res.answer
    manifest = runner._load_manifest()
    assert len(manifest) == 1
    assert manifest[0]["request"] == "計算常數"


def test_self_repair_succeeds_on_second(tmp_path):
    client = FakeClient(code_responses=[BAD_SCRIPT, GOOD_SCRIPT])
    runner = _make_runner(tmp_path, client)
    res = runner.run_detailed("做一件會先失敗的事")
    assert res.ok
    assert res.generations == 2
    assert client.calls["repair"] == 1


def test_repair_exhausts_and_fails(tmp_path):
    # Phase 1: 3 attempts (think=False) + Phase 2: 1 escalation (think=True) = 4 total.
    client = FakeClient(code_responses=[BAD_SCRIPT, BAD_SCRIPT, BAD_SCRIPT, BAD_SCRIPT])
    runner = _make_runner(tmp_path, client)
    res = runner.run_detailed("總是失敗")
    assert not res.ok
    assert res.generations == 4
    assert "boom" in res.error


def test_clean_env_strips_secrets(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCLAW_TELEGRAM_BOT_TOKEN", "super-secret-123")
    client = FakeClient(code_responses=[SECRET_PROBE])
    runner = _make_runner(tmp_path, client)
    res = runner.run_detailed("印出 token")
    assert res.ok
    assert "super-secret-123" not in res.answer
    assert "None" in res.answer


def test_clean_env_excludes_openclaw_keys(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCLAW_SECRET", "x")
    runner = _make_runner(tmp_path, FakeClient(code_responses=[GOOD_SCRIPT]))
    env = runner._clean_env(tmp_path)
    assert not any(k.startswith("OPENCLAW_") for k in env)
    assert env["HOME"] == str(tmp_path)


def test_reuse_hits_existing_tool(tmp_path):
    # First run creates a tool.
    client1 = FakeClient(code_responses=[GOOD_SCRIPT])
    runner = _make_runner(tmp_path, client1)
    first = runner.run_detailed("查某個東西")
    tool_id = runner._load_manifest()[0]["id"]

    # Second run: pick_reusable returns that id → should reuse, no codegen.
    client2 = FakeClient(code_responses=[], pick_response=tool_id)
    runner.client = client2
    second = runner.run_detailed("查某個東西（換句話說）")
    assert second.ok
    assert second.reused
    assert client2.calls["code"] == 0


def test_failure_distillation_writes_rule(tmp_path):
    db = KnowledgeDatabase(tmp_path / "k.sqlite3")
    before = len(db.all_codegen_knowledge())
    distilled = (
        '{"category":"validation","title":"測試通則","technique":"通用規則內容",'
        '"keywords":["test"]}'
    )
    client = FakeClient(code_responses=[BAD_SCRIPT, GOOD_SCRIPT], distill_response=distilled)
    runner = _make_runner(tmp_path, client, db=db)
    res = runner.run_detailed("需要修復的任務")
    assert res.ok and res.generations == 2
    assert client.calls["distill"] == 1
    after = db.all_codegen_knowledge()
    assert len(after) == before + 1
    assert any(r.title == "測試通則" and r.origin == "distilled" for r in after)


def test_numeric_check():
    ok, _ = _check_numeric("年化報酬 219.5%", {"expected": 219.5, "tolerance_pct": 5, "is_pct": True})
    assert ok
    bad, _ = _check_numeric("年化報酬 50%", {"expected": 219.5, "tolerance_pct": 5, "is_pct": True})
    assert not bad


def test_direction_check():
    ok, _ = _check_direction("營收下滑、淨利衰退", [["營收"], ["衰退", "下滑"]])
    assert ok
    bad, _ = _check_direction("營收成長", [["營收"], ["衰退", "下滑"]])
    assert not bad
