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
    _syntax_error,
    _is_truncation_error,
    _defaults_schema_from_code,
)
from openclaw_adapter.knowledge_db import KnowledgeDatabase


GOOD_SCRIPT = (
    'print("===ANSWER===")\n'
    'print("結果 42（計算依據：常數）")\n'
    'print("===END===")\n'
)
BAD_SCRIPT = 'import sys\nsys.stderr.write("boom\\n")\nsys.exit(1)\n'
# Genuine (non-truncation) syntax error → gate repairs without burning a gen.
SYNTAX_BROKEN = "def f(:\n    pass\n"
# Truncation signature (unterminated string) → gate bumps num_predict + regenerates.
TRUNCATED_SCRIPT = 'print("===ANSWER==='
SECRET_PROBE = (
    "import os\n"
    'print("===ANSWER===")\n'
    'print("TOKEN=" + repr(os.environ.get("OPENCLAW_TELEGRAM_BOT_TOKEN")))\n'
    'print("===END===")\n'
)


class FakeClient:
    """Routes generate() by prompt markers to canned responses."""

    def __init__(self, *, code_responses, pick_response="NONE", distill_response="{}",
                 explorer_response: str | None = None, meta_response="", params_response="{}"):
        self._code = list(code_responses)
        self._pick = pick_response
        self._distill = distill_response
        self._meta = meta_response          # prepended before ===CODE=== on codegen
        self._params = params_response       # returned for param-extraction calls
        # Default: declare no external API needed so exploration is a no-op in tests.
        self._explorer = explorer_response if explorer_response is not None else 'print("NO_EXTERNAL_API")'
        self.calls = {"pick": 0, "code": 0, "repair": 0, "distill": 0, "explore": 0, "params": 0}
        self.timeout_seconds = 420
        self.num_predict = 1000  # mirrors OllamaTextClient attribute

    def generate(self, prompt: str, *, temperature: float = 0.0, think: bool = False) -> str:
        if "工具類型" in prompt:
            self.calls["pick"] += 1
            return self._pick
        if "抽取參數" in prompt:
            self.calls["params"] += 1
            return self._params
        if "抽象成" in prompt:
            self.calls["distill"] += 1
            return self._distill
        if "API 探索腳本" in prompt:
            self.calls["explore"] += 1
            return self._explorer
        if "執行失敗" in prompt:
            self.calls["repair"] += 1
        else:
            self.calls["code"] += 1
        # both codegen and repair pull from the same queue
        return self._meta + "===CODE===\n" + self._code.pop(0)


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
    # Escalation (Phase 2) mutates the client; must be restored afterwards.
    assert client.num_predict == 1000
    assert client.timeout_seconds == 420


def test_syntax_helpers():
    assert _syntax_error("x = 1\n") == ""
    assert _is_truncation_error(_syntax_error('print("abc'))      # unterminated str
    assert _is_truncation_error(_syntax_error("def f():"))        # missing body
    assert not _is_truncation_error(_syntax_error("def f(:\n  pass"))  # genuine bad syntax


def test_syntax_gate_fixes_without_burning_generation(tmp_path):
    # First output has a real syntax error; gate repairs it before any exec, so
    # the successful run is generation #1 (the syntax fix is free, like ModuleNotFound).
    client = FakeClient(code_responses=[SYNTAX_BROKEN, GOOD_SCRIPT])
    runner = _make_runner(tmp_path, client)
    res = runner.run_detailed("語法壞掉的任務")
    assert res.ok
    assert res.generations == 1
    assert client.calls["repair"] == 1  # the syntax fix used a repair call
    assert client.calls["code"] == 1


def test_truncation_bumps_and_regenerates(tmp_path):
    # Truncated output → gate regenerates from scratch (not repair) and the run
    # still lands on generation #1.
    client = FakeClient(code_responses=[TRUNCATED_SCRIPT, GOOD_SCRIPT])
    runner = _make_runner(tmp_path, client)
    res = runner.run_detailed("被截斷的任務")
    assert res.ok
    assert res.generations == 1
    assert client.calls["code"] == 2   # regenerated, not repaired
    assert client.calls["repair"] == 0
    assert client.num_predict == 1000  # bumped during run, restored afterwards


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


def test_reuse_exact_match_shortcircuits(tmp_path):
    # First run creates a (legacy, no-schema) tool.
    client1 = FakeClient(code_responses=[GOOD_SCRIPT])
    runner = _make_runner(tmp_path, client1)
    runner.run_detailed("查某個東西")

    # Second run with the IDENTICAL request → deterministic exact-match reuse,
    # no codegen AND no classification model call.
    client2 = FakeClient(code_responses=[])
    runner.client = client2
    second = runner.run_detailed("查某個東西")
    assert second.ok
    assert second.reused
    assert client2.calls["code"] == 0
    assert client2.calls["pick"] == 0  # short-circuit avoids the model entirely


def test_parameterized_tool_reuse_matches_by_type(tmp_path):
    # Build a parameterized tool, then reuse it for a DIFFERENT-number request of
    # the same tool_type via classify-then-match (pick_response = the tool_type).
    client1 = FakeClient(code_responses=[PARAM_TOOL], meta_response=PARAM_META)
    runner = _make_runner(tmp_path, client1)
    runner.run_detailed("輸出 x=10")

    client2 = FakeClient(code_responses=[], pick_response="x輸出", params_response='{"x": 77}')
    runner.client = client2
    second = runner.run_detailed("輸出 x=77")
    assert second.ok and second.reused
    assert client2.calls["code"] == 0
    assert client2.calls["pick"] == 1    # classification ran
    assert client2.calls["params"] == 1  # params extracted
    assert "77" in second.answer


PARAM_META = (
    '===META===\n'
    '{"tool_type":"x輸出","param_schema":[{"name":"x","type":"number","desc":"x值"}]}\n'
)
PARAM_TOOL = (
    "import json, os\n"
    "DEFAULTS = {'x': 10}\n"
    "params = dict(DEFAULTS)\n"
    "if os.path.exists('params.json'):\n"
    "    params.update(json.load(open('params.json', encoding='utf-8')))\n"
    'print("===ANSWER===")\n'
    'print("結果 x=" + str(params["x"]) + "（計算依據：讀取參數）")\n'
    'print("===END===")\n'
)


def test_parameterized_tool_registers_schema(tmp_path):
    client = FakeClient(code_responses=[PARAM_TOOL], meta_response=PARAM_META)
    runner = _make_runner(tmp_path, client)
    first = runner.run_detailed("輸出 x=10")
    assert first.ok and "10" in first.answer
    entry = runner._load_manifest()[0]
    assert entry.get("param_schema"), "tool should be registered as parameterized"
    assert entry.get("tool_type") == "x輸出"


def test_defaults_schema_from_code():
    schema = _defaults_schema_from_code(
        "DEFAULTS = {'total': 2000, 'rate': 0.04, 'city': 'Taipei'}\nprint(1)\n"
    )
    by_name = {s["name"]: s for s in schema}
    assert by_name["total"]["type"] == "number"
    assert by_name["rate"]["type"] == "number"
    assert by_name["city"]["type"] == "string"
    assert _defaults_schema_from_code("x = 1\n") is None


# META declares only a tool_type (no param_schema) — mirrors the live 14b defect.
PARAM_META_NO_SCHEMA = '===META===\n{"tool_type":"x輸出"}\n'


def test_schema_derived_from_code_when_meta_omits_it(tmp_path):
    # Even though META carries no param_schema, the DEFAULTS dict in the code is
    # read deterministically so the tool is still registered as parameterized
    # (and therefore reusable).
    client = FakeClient(code_responses=[PARAM_TOOL], meta_response=PARAM_META_NO_SCHEMA)
    runner = _make_runner(tmp_path, client)
    first = runner.run_detailed("輸出 x=10")
    assert first.ok
    entry = runner._load_manifest()[0]
    assert entry.get("param_schema"), "schema should be derived from code DEFAULTS"
    assert entry["param_schema"][0]["name"] == "x"
    assert entry.get("tool_type") == "x輸出"


def test_failure_distillation_writes_rule(tmp_path):
    db = KnowledgeDatabase(tmp_path / "k.sqlite3")
    before = len(db.all_codegen_knowledge())
    distilled = (
        '{"category":"validation","title":"測試通則","technique":"通用規則內容",'
        '"keywords":["test"]}'
    )
    client = FakeClient(code_responses=[BAD_SCRIPT, GOOD_SCRIPT], distill_response=distilled)
    runner = _make_runner(tmp_path, client, db=db)
    runner.distill_enabled = True  # off by default now; opt in to exercise distillation
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
