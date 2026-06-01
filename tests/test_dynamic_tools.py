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
    OllamaTextClient,
    _extract_answer,
    _extract_code,
    _check_numeric,
    _check_direction,
    _syntax_error,
    _is_truncation_error,
    _defaults_schema_from_code,
    _ensure_stdlib_imports,
    probe_ollama,
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
                 explorer_response: str | None = None, meta_response="", params_response="{}",
                 presentation_response: str | None = None, split_response: str = "{}"):
        self._code = list(code_responses)
        self._pick = pick_response
        self._distill = distill_response
        self._meta = meta_response          # prepended before ===CODE=== on codegen
        self._params = params_response       # returned for param-extraction calls
        self._presentation = presentation_response  # returned for reformat calls
        self._split = split_response         # returned for intent-split calls
        # Default: declare no external API needed so exploration is a no-op in tests.
        self._explorer = explorer_response if explorer_response is not None else 'print("NO_EXTERNAL_API")'
        self.calls = {"pick": 0, "code": 0, "repair": 0, "distill": 0, "explore": 0,
                      "params": 0, "present": 0, "split": 0}
        self.timeout_seconds = 420
        self.num_predict = 1000  # mirrors OllamaTextClient attribute
        self.num_ctx = 8192      # mirrors OllamaTextClient attribute
        self.model = "stub:1b"   # the cascade switches this per tier
        # (model, think) recorded for every codegen / repair call, in order.
        self.codegen_models: list[tuple[str, bool]] = []
        self.num_ctx_seen: list[int | None] = []

    def generate(self, prompt: str, *, temperature: float = 0.0, think: bool = False) -> str:
        if "拆成兩部分" in prompt:
            self.calls["split"] += 1
            return self._split  # default "{}" → runner falls back to (request, "")
        if "工具類型" in prompt:
            self.calls["pick"] += 1
            return self._pick
        if "抽取參數" in prompt:
            self.calls["params"] += 1
            return self._params
        if "重新排版" in prompt:
            self.calls["present"] += 1
            # Default: behave like the model honoring "no format → return as-is".
            if self._presentation is None:
                return prompt.split("資料：\n", 1)[-1].strip()
            return self._presentation
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
        # Record which model/think/ctx the cascade used for this codegen call.
        self.codegen_models.append((self.model, think))
        self.num_ctx_seen.append(self.num_ctx)
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
    # Cascade tiers: A fast (3) + B strong-fast (3) + C strong-think (1) = 7 total.
    client = FakeClient(code_responses=[BAD_SCRIPT] * 7)
    runner = _make_runner(tmp_path, client)
    res = runner.run_detailed("總是失敗")
    assert not res.ok
    assert res.generations == 7
    assert "boom" in res.error
    # Tier C (think) mutates the client; must be restored afterwards.
    assert client.num_predict == 1000
    assert client.timeout_seconds == 420
    # num_ctx must stay constant across every tier (changing it forces a reload).
    assert set(client.num_ctx_seen) == {8192}


def test_cascade_escalates_fast_model_to_strong(tmp_path):
    # Tier A (fast model) fails 3x, then Tier B (strong model) succeeds on its
    # first attempt. Verifies the model name climbs fast -> strong on failure and
    # that the common case would never have touched the strong model.
    client = FakeClient(code_responses=[BAD_SCRIPT, BAD_SCRIPT, BAD_SCRIPT, GOOD_SCRIPT])
    runner = DynamicToolRunner(
        client=client,
        tools_dir=tmp_path / "generated_tools",
        knowledge_db=None,
        exec_timeout_seconds=30,
        fast_model="coder:7b",
        strong_model="big:14b",
    )
    runner._ensure_venv = lambda: Path(sys.executable)  # type: ignore[assignment]
    runner._pip_install = lambda packages: None  # type: ignore[assignment]

    res = runner.run_detailed("先失敗三次再升級")
    assert res.ok
    assert res.generations == 4
    # First 3 codegen calls on the fast model (think=False), 4th on strong model.
    assert client.codegen_models[:3] == [("coder:7b", False)] * 3
    assert client.codegen_models[3] == ("big:14b", False)
    # Client model is restored to the fast default after the run.
    assert client.model == "coder:7b"


def test_single_model_config_degenerates(tmp_path):
    # When fast == strong (no separate fast model configured), the cascade still
    # works and uses that one model for every tier.
    client = FakeClient(code_responses=[GOOD_SCRIPT])
    runner = DynamicToolRunner(
        client=client,
        tools_dir=tmp_path / "generated_tools",
        knowledge_db=None,
        exec_timeout_seconds=30,
        fast_model="solo:14b",
        strong_model="solo:14b",
    )
    runner._ensure_venv = lambda: Path(sys.executable)  # type: ignore[assignment]
    runner._pip_install = lambda packages: None  # type: ignore[assignment]
    res = runner.run_detailed("一個模型搞定")
    assert res.ok and res.generations == 1
    assert client.codegen_models[0] == ("solo:14b", False)


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


def test_presentation_applies_when_intent_split_yields_format(tmp_path):
    # Intent split yields a format spec → after running the tool (reuse here),
    # the presentation pass reshapes the output to that format.
    client1 = FakeClient(code_responses=[PARAM_TOOL], meta_response=PARAM_META)
    runner = _make_runner(tmp_path, client1)
    runner.run_detailed("輸出 x=10")

    client2 = FakeClient(
        code_responses=[], pick_response="x輸出", params_response='{"x": 77}',
        split_response='{"core": "輸出 x=77", "format": "📊 x 的值：<數值>"}',
        presentation_response="📊 x 的值：77",
    )
    runner.client = client2
    second = runner.run_detailed("輸出 x=77，格式如下\n📊 x 的值：<數值>")
    assert second.ok and second.reused
    assert client2.calls["code"] == 0       # no regeneration — matched on clean core
    assert client2.calls["present"] == 1    # presentation pass ran
    assert second.answer == "📊 x 的值：77"  # reshaped to the requested format


def test_no_format_skips_presentation(tmp_path):
    # Intent split finds no format → presentation must NOT run (no model call).
    client1 = FakeClient(code_responses=[PARAM_TOOL], meta_response=PARAM_META)
    runner = _make_runner(tmp_path, client1)
    runner.run_detailed("輸出 x=10")

    # split_response default "{}" → runner falls back to (request, "") → no format.
    client2 = FakeClient(code_responses=[], params_response='{"x": 10}',
                         presentation_response="SHOULD_NOT_APPEAR")
    runner.client = client2
    second = runner.run_detailed("輸出 x=10")
    assert second.ok and second.reused
    assert client2.calls["present"] == 0
    assert "SHOULD_NOT_APPEAR" not in second.answer


def test_intent_split_routes_core_and_format(tmp_path):
    # The split call drives matching on core and formatting on format_spec.
    client = FakeClient(
        code_responses=[], pick_response="x輸出", params_response='{"x": 5}',
        split_response='{"core": "輸出 x=5", "format": "F:<v>"}',
        presentation_response="F:5",
    )
    # Pre-seed a reusable param tool.
    seed = FakeClient(code_responses=[PARAM_TOOL], meta_response=PARAM_META)
    runner = _make_runner(tmp_path, seed)
    runner.run_detailed("輸出 x=10")
    runner.client = client

    res = runner.run_detailed("輸出 x=5 用 F:<v> 格式")
    assert client.calls["split"] == 1
    assert res.ok and res.answer == "F:5"


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


def test_ensure_stdlib_imports_adds_missing():
    # The exact failure mode observed live: os.path.exists used, os not imported.
    code = "import json\nif os.path.exists('p.json'):\n    json.load(open('p.json'))\n"
    fixed = _ensure_stdlib_imports(code)
    assert fixed.startswith("import os\n")
    assert _syntax_error(fixed) == ""
    # Already-imported modules are not duplicated.
    assert _ensure_stdlib_imports(fixed).count("import os") == 1


def test_ensure_stdlib_imports_respects_aliases_and_bindings():
    # `import urllib.request` binds `urllib`; must not re-add it.
    assert "import urllib\n" not in _ensure_stdlib_imports(
        "import urllib.request\nurllib.request.urlopen('x')\n"
    )
    # A local variable named like a module must not trigger an import.
    assert _ensure_stdlib_imports("time = 5\nprint(time)\n") == "time = 5\nprint(time)\n"
    # from-import binds the imported name.
    assert _ensure_stdlib_imports("from os import path\npath.exists('x')\n").startswith("from os")


def test_syntax_gate_injects_missing_import_without_burning_generation(tmp_path):
    # Code parses fine but references `os` without importing it (runtime NameError).
    # The gate must inject the import so the run succeeds on generation #1.
    bad_import = (
        "import json\n"
        "params = {'city': 'London'}\n"
        "if os.path.exists('params.json'):\n"
        "    params.update(json.load(open('params.json', encoding='utf-8')))\n"
        'print("===ANSWER===")\n'
        'print(f"{params[\'city\']} ok")\n'
        'print("===END===")\n'
    )
    client = FakeClient(code_responses=[bad_import])
    runner = _make_runner(tmp_path, client)
    res = runner.run_detailed("缺 import os 的任務")
    assert res.ok
    assert res.generations == 1
    assert client.calls["repair"] == 0  # fixed statically, no repair call


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


# ── Ollama resilience (A4) ─────────────────────────────────────────────────


def test_probe_ollama_returns_true_when_server_responds() -> None:
    from unittest.mock import patch, MagicMock
    mock_response = MagicMock()
    mock_response.__enter__ = lambda s: s
    mock_response.__exit__ = MagicMock(return_value=False)
    with patch("openclaw_adapter.dynamic_tools.urlopen", return_value=mock_response):
        assert probe_ollama("http://127.0.0.1:11434/api") is True


def test_probe_ollama_returns_false_when_server_unreachable() -> None:
    assert probe_ollama("http://127.0.0.1:19999") is False


def test_probe_ollama_strips_generate_suffix() -> None:
    assert probe_ollama("http://127.0.0.1:19999/api/generate") is False


def test_ollama_generate_retries_on_5xx_then_succeeds(tmp_path) -> None:
    import time as _time
    calls = [0]
    slept: list[float] = []

    class _FakeClient:
        model = "q"
        timeout_seconds = 30
        num_predict = None
        num_ctx = None

        def generate(self, prompt, *, temperature=0.0, think=False):
            calls[0] += 1
            if calls[0] < 3:
                raise RuntimeError("Ollama HTTP 503")
            return f'===ANSWER===\nok\n===END==='

    runner = DynamicToolRunner(
        client=_FakeClient(),
        tools_dir=tmp_path,
        fast_model="q",
        strong_model="q",
    )
    # The retry is inside OllamaTextClient.generate; test it directly.
    import urllib.error
    attempt = [0]
    slept_vals: list[float] = []

    client = OllamaTextClient(endpoint="http://localhost:11434", model="q", timeout_seconds=5)

    original_sleep = __import__("time").sleep

    def _fake_sleep(s):
        slept_vals.append(s)

    import unittest.mock as mock
    responses = [
        urllib.error.HTTPError("url", 503, "Service Unavailable", {}, None),
        urllib.error.HTTPError("url", 503, "Service Unavailable", {}, None),
        None,  # success on 3rd
    ]
    call_idx = [0]

    def _fake_urlopen(req, timeout=None):
        idx = call_idx[0]
        call_idx[0] += 1
        exc = responses[idx]
        if exc is not None:
            raise exc

        class _FakeResp:
            def read(self): return b'{"response": "hello"}'
            def __enter__(self): return self
            def __exit__(self, *_): pass

        return _FakeResp()

    with mock.patch("openclaw_adapter.dynamic_tools.urlopen", _fake_urlopen), \
         mock.patch("openclaw_adapter.dynamic_tools.time.sleep", _fake_sleep):
        result = client.generate("test prompt")

    assert result == "hello"
    assert call_idx[0] == 3
    assert len(slept_vals) == 2


def test_ollama_generate_raises_after_all_retries_exhausted() -> None:
    import urllib.error
    import unittest.mock as mock

    client = OllamaTextClient(endpoint="http://localhost:11434", model="q", timeout_seconds=5)

    def _always_fail(req, timeout=None):
        raise urllib.error.URLError("Connection refused")

    with mock.patch("openclaw_adapter.dynamic_tools.urlopen", _always_fail), \
         mock.patch("openclaw_adapter.dynamic_tools.time.sleep", lambda _: None):
        with pytest.raises(RuntimeError, match="Ollama 不在線"):
            client.generate("test prompt")


def test_ollama_generate_does_not_retry_on_4xx() -> None:
    import urllib.error
    import unittest.mock as mock

    client = OllamaTextClient(endpoint="http://localhost:11434", model="q", timeout_seconds=5)
    call_count = [0]

    def _bad_request(req, timeout=None):
        call_count[0] += 1
        raise urllib.error.HTTPError("url", 400, "Bad Request", {}, None)

    with mock.patch("openclaw_adapter.dynamic_tools.urlopen", _bad_request):
        with pytest.raises(RuntimeError, match="400"):
            client.generate("test prompt")

    assert call_count[0] == 1  # no retry on 4xx
