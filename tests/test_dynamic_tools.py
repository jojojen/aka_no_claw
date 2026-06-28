"""Unit tests for DynamicToolRunner — no network, no real model, no real venv.

The Ollama client and the venv python are faked so these run fast and offline.
"""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest

from openclaw_adapter.dynamic_tools import (
    CloudBackendUnavailable,
    DynamicToolRunner,
    OllamaTextClient,
    OpenCodeCliTextClient,
    OpenCodeTextClient,
    _extract_answer,
    _extract_code,
    _check_numeric,
    _check_direction,
    _syntax_error,
    _is_truncation_error,
    _defaults_schema_from_code,
    _ensure_stdlib_imports,
    build_dynamic_tool_runner_from_settings,
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
                 presentation_response: str | None = None, split_response: str = "{}",
                 validate_responses: list | None = None,
                 feasibility_response: str | Exception = "FEASIBLE",
                 rule_select_response: str | Exception = "NONE",
                 ground_response: str | Exception = "NONE",
                 searchq_response: str | Exception = "stub-query",
                 needs_search_response: str | Exception = "NO"):
        self._code = list(code_responses)
        self._pick = pick_response
        self._distill = distill_response
        self._meta = meta_response          # prepended before ===CODE=== on codegen
        self._params = params_response       # returned for param-extraction calls
        self._presentation = presentation_response  # returned for reformat calls
        self._split = split_response         # returned for intent-split calls
        # Answer-validation gate responses; None/exhausted → "PASS" (keeps old tests green).
        self._validate = list(validate_responses) if validate_responses else []
        self._feasibility = feasibility_response
        self._rule_select = rule_select_response
        self._ground = ground_response
        self._searchq = searchq_response
        self._needs_search = needs_search_response
        # Default: declare no external API needed so exploration is a no-op in tests.
        self._explorer = explorer_response if explorer_response is not None else 'print("NO_EXTERNAL_API")'
        self.calls = {"pick": 0, "code": 0, "repair": 0, "distill": 0, "explore": 0,
                      "params": 0, "present": 0, "split": 0, "validate": 0, "feasibility": 0,
                      "select": 0, "ground": 0, "searchq": 0, "needs_search": 0}
        self.repair_prompts: list[str] = []
        self.code_prompts: list[str] = []
        self.timeout_seconds = 420
        self.num_predict = 1000  # mirrors OllamaTextClient attribute
        self.num_ctx = 8192      # mirrors OllamaTextClient attribute
        self.model = "stub:1b"   # the cascade switches this per tier
        # (model, think) recorded for every codegen / repair call, in order.
        self.codegen_models: list[tuple[str, bool]] = []
        self.num_ctx_seen: list[int | None] = []

    def generate(self, prompt: str, *, temperature: float = 0.0, think: bool = False) -> str:
        if "可行性判斷" in prompt:
            # Combined preflight: feasibility line + rule-selection line.
            self.calls["feasibility"] += 1
            if isinstance(self._feasibility, Exception):
                raise self._feasibility
            self.calls["select"] += 1
            if isinstance(self._rule_select, Exception):
                raise self._rule_select
            return self._feasibility + "\n" + self._rule_select
        if "直接相關的公式" in prompt:
            # Reference-page grounding extraction (only fires when a selected
            # rule cites a 參考: URL and the fetch succeeded). Distillation is
            # one call per page; a list response is consumed in order.
            self.calls["ground"] += 1
            if isinstance(self._ground, Exception):
                raise self._ground
            if isinstance(self._ground, list):
                return self._ground.pop(0)
            return self._ground
        if "機構公告型事實數值" in prompt:
            # Gate: do successful rule references still lack a current
            # institution-announced value? Default NO keeps old tests offline.
            self.calls["needs_search"] += 1
            if isinstance(self._needs_search, Exception):
                raise self._needs_search
            return self._needs_search
        if "網頁搜尋查詢" in prompt:
            # Search-grounding query reformulation (fallback when no rule URL).
            self.calls["searchq"] += 1
            if isinstance(self._searchq, Exception):
                raise self._searchq
            return self._searchq
        if "是否合理回應" in prompt:
            self.calls["validate"] += 1
            if not self._validate:
                return "PASS"
            entry = self._validate.pop(0)
            if isinstance(entry, Exception):
                raise entry
            return entry
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
            self.repair_prompts.append(prompt)
        else:
            self.calls["code"] += 1
            self.code_prompts.append(prompt)
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
    # Scripts must differ — identical repair output short-circuits the tier.
    client = FakeClient(code_responses=[BAD_SCRIPT + f"\n# v{i}" for i in range(7)])
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


def test_blocked_package_feeds_repair_instead_of_crashing(tmp_path):
    # A script declaring an unapproved package must NOT crash /new — the pip
    # refusal becomes an exec failure that the repair loop fixes (rewrite with
    # stdlib), so the run still succeeds.
    blocked_script = "# requires: scipy\n" + GOOD_SCRIPT
    client = FakeClient(code_responses=[blocked_script, GOOD_SCRIPT])
    runner = _make_runner(tmp_path, client)

    def strict_pip(packages):
        if "scipy" in packages:
            raise RuntimeError("⛔ /new: 以下套件不在核准清單，已拒絕安裝：scipy")

    runner._pip_install = strict_pip  # type: ignore[assignment]
    res = runner.run_detailed("需要被擋下的套件")
    assert res.ok
    assert res.generations == 2
    assert client.calls["repair"] == 1
    assert "核准清單" in client.repair_prompts[0]
    assert "標準函式庫" in client.repair_prompts[0]


def test_cascade_escalates_fast_model_to_strong(tmp_path):
    # Tier A (fast model) fails 3x, then Tier B (strong model) succeeds on its
    # first attempt. Verifies the model name climbs fast -> strong on failure and
    # that the common case would never have touched the strong model.
    client = FakeClient(
        code_responses=[BAD_SCRIPT + "\n# v1", BAD_SCRIPT + "\n# v2",
                        BAD_SCRIPT + "\n# v3", GOOD_SCRIPT],
    )
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


def test_identical_repair_escalates_tier_early(tmp_path):
    # When repair returns byte-identical code, re-executing it would fail the
    # same way — the runner skips the wasted cycles and climbs a tier directly.
    client = FakeClient(code_responses=[BAD_SCRIPT, BAD_SCRIPT, GOOD_SCRIPT])
    runner = _make_runner(tmp_path, client)
    res = runner.run_detailed("修復卡死的任務")
    assert res.ok
    assert res.generations == 2  # tier-1 attempt + tier-2 success; no replay of dupes
    assert client.calls["repair"] == 1


def test_truncation_marker_covers_cut_try_block():
    # A num_predict-capped script typically dies between `try:` and its handler;
    # that must count as truncation (bump & regenerate), not a plain syntax error
    # (repair at the same cap → truncates again → burns the tier's budget).
    from openclaw_adapter.dynamic_tools import _is_truncation_error
    assert _is_truncation_error("expected 'except' or 'finally' block (line 22)")


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


def test_validator_pass_unchanged(tmp_path):
    # Default validator (PASS) → behavior identical to pre-gate pipeline,
    # but the validation call itself must have happened.
    client = FakeClient(code_responses=[GOOD_SCRIPT])
    runner = _make_runner(tmp_path, client)
    res = runner.run_detailed("計算常數")
    assert res.ok
    assert res.generations == 1
    assert client.calls["validate"] == 1
    assert len(runner._load_manifest()) == 1


def test_validator_fail_triggers_repair(tmp_path):
    # Execution succeeds but the answer flunks validation → counts as a failed
    # generation, validator reason is fed into the repair prompt, retry passes.
    client = FakeClient(
        code_responses=[GOOD_SCRIPT, GOOD_SCRIPT + "\n# retry"],
        validate_responses=["FAIL: 主題不符", "PASS"],
    )
    runner = _make_runner(tmp_path, client)
    res = runner.run_detailed("查天氣")
    assert res.ok
    assert res.generations == 2
    assert client.calls["repair"] == 1
    assert "主題不符" in client.repair_prompts[0]


def test_reuse_validation_fail_regenerates(tmp_path):
    # First run registers a legacy tool (validator passes by default).
    client1 = FakeClient(code_responses=[GOOD_SCRIPT])
    runner = _make_runner(tmp_path, client1)
    runner.run_detailed("查某個東西")

    # Same request → exact-match reuse executes, but its answer FAILs validation
    # → falls through to fresh generation, whose answer PASSes.
    client2 = FakeClient(
        code_responses=[GOOD_SCRIPT],
        validate_responses=["FAIL: 答非所問", "PASS"],
    )
    runner.client = client2
    second = runner.run_detailed("查某個東西")
    assert second.ok
    assert second.reused is False
    assert client2.calls["code"] == 1
    assert client2.calls["validate"] == 2


def test_validator_exception_fails_open(tmp_path):
    # A sick validator (call raises) must never block a good answer.
    client = FakeClient(
        code_responses=[GOOD_SCRIPT],
        validate_responses=[RuntimeError("ollama down")],
    )
    runner = _make_runner(tmp_path, client)
    res = runner.run_detailed("計算常數")
    assert res.ok
    assert res.generations == 1
    assert client.calls["validate"] == 1


def test_validator_always_fail_respects_cap(tmp_path):
    # Validation failures consume the same 3+3+1 budget as exec failures.
    client = FakeClient(
        code_responses=[GOOD_SCRIPT + f"\n# v{i}" for i in range(7)],
        validate_responses=["FAIL: 答非所問"] * 7,
    )
    runner = _make_runner(tmp_path, client)
    res = runner.run_detailed("永遠答非所問")
    assert not res.ok
    assert res.generations == 7
    assert "答案驗證未通過" in res.error
    # A tool that never passed validation must not enter the reuse pool.
    assert runner._load_manifest() == []


def test_feasibility_infeasible_fails_fast(tmp_path):
    # No key-free public source → honest failure BEFORE any codegen is burned.
    client = FakeClient(
        code_responses=[],
        feasibility_response="INFEASIBLE: 即時機票票價需要授權 API",
    )
    runner = _make_runner(tmp_path, client)
    res = runner.run_detailed("查今晚從東京飛台北最便宜的飛機")
    assert not res.ok
    assert res.generations == 0
    assert client.calls["code"] == 0
    assert "機票" in res.error
    assert runner._load_manifest() == []


def test_feasibility_error_fails_open(tmp_path):
    # A sick feasibility checker must never block generation.
    client = FakeClient(
        code_responses=[GOOD_SCRIPT],
        feasibility_response=RuntimeError("ollama down"),
    )
    runner = _make_runner(tmp_path, client)
    res = runner.run_detailed("計算常數")
    assert res.ok
    assert res.generations == 1


def _make_rule_db(tmp_path):
    db = KnowledgeDatabase(tmp_path / "k.sqlite3")
    db.upsert_codegen_knowledge(
        category="output_contract", title="通用守則",
        technique="答案夾在標記之間輸出",
        keywords=("*",), origin="seed", confidence=0.9,
    )
    db.upsert_codegen_knowledge(
        category="finance", title="股價資料源",
        technique="股價用 Yahoo chart API 取收盤序列",
        keywords=("股票", "報酬"), origin="seed", confidence=0.95,
    )
    return db


def test_llm_rule_selector_injects_picked_rule(tmp_path):
    # LLM picks topical rule #1 → its technique reaches the codegen prompt,
    # alongside the always-on rule. No keyword match required ("ETF績效" hits
    # none of the stored keywords).
    client = FakeClient(code_responses=[GOOD_SCRIPT], rule_select_response="1")
    runner = _make_runner(tmp_path, client, db=_make_rule_db(tmp_path))
    res = runner.run_detailed("ETF績效如何")
    assert res.ok
    assert client.calls["select"] == 1
    assert "Yahoo chart API" in client.code_prompts[0]
    assert "答案夾在標記之間" in client.code_prompts[0]
    # Topical recipe must PRECEDE always-on rules: the methodology block is
    # char-budgeted, so a recipe placed after a dozen disciplines gets cut.
    prompt = client.code_prompts[0]
    assert prompt.index("Yahoo chart API") < prompt.index("答案夾在標記之間")
    # A selected recipe already documents the API structure → no live explorer.
    assert client.calls["explore"] == 0


def test_llm_rule_selector_none_excludes_topical(tmp_path):
    # LLM says NONE → topical recipe stays OUT of the prompt even though the
    # request contains a matching keyword; always-on rules still injected.
    client = FakeClient(code_responses=[GOOD_SCRIPT], rule_select_response="NONE")
    runner = _make_runner(tmp_path, client, db=_make_rule_db(tmp_path))
    res = runner.run_detailed("算股票報酬")
    assert res.ok
    assert "Yahoo chart API" not in client.code_prompts[0]
    assert "答案夾在標記之間" in client.code_prompts[0]
    # No recipe selected → unknown domain → live exploration still runs.
    assert client.calls["explore"] == 1


def test_requires_maps_import_name_to_pip_name(tmp_path):
    # `# requires: dateutil` names the import, but the pip distribution is
    # python-dateutil (approved); installing the raw module name got an
    # approved package blocked in live runs.
    client = FakeClient(code_responses=[])
    runner = _make_runner(tmp_path, client)
    assert runner._parse_requires("# requires: dateutil, bs4\nx=1") == (
        "python-dateutil", "beautifulsoup4")


def test_distill_per_text_drops_irrelevant_page(tmp_path):
    # Pages are distilled one call each (a joined prompt can overflow num_ctx,
    # Ollama head-truncates it and the instructions silently vanish → NONE).
    # A wrong-metric page answering NONE is dropped; the relevant one stays.
    client = FakeClient(
        code_responses=[],
        ground_response=["NONE\n（不同指標，不相關）", "現行值 0.75%（自2026-06）"],
    )
    runner = _make_runner(tmp_path, client)
    out = runner._distill_reference_texts(
        "日銀政策金利", ["prime-rate junk page", "boj policy rate page"])
    assert out == "現行值 0.75%（自2026-06）"
    assert client.calls["ground"] == 2
    # The per-call num_ctx bump must not leak past the distiller.
    assert client.num_ctx == 8192


def test_keyword_floor_merges_unpicked_topical(tmp_path):
    # The preflight listing re-orders between runs, so the LLM's pick is
    # unstable. A topical rule whose declared keyword appears verbatim in the
    # request must reach the prompt even when the LLM didn't pick it.
    db = _make_rule_db(tmp_path)
    db.upsert_codegen_knowledge(
        category="numeric_method", title="極值掃描守則",
        technique="極值掃描要在更新當下記錄構成點",
        keywords=("回撤", "極值"), origin="seed", confidence=0.9,
    )
    client = FakeClient(code_responses=[GOOD_SCRIPT], rule_select_response="1")
    runner = _make_runner(tmp_path, client, db=db)
    res = runner.run_detailed("算股票今年的最大回撤")
    assert res.ok
    prompt = client.code_prompts[0]
    assert "Yahoo chart API" in prompt          # LLM-picked rule (#1)
    assert "極值掃描要在更新當下記錄構成點" in prompt  # keyword floor


def test_keyword_floor_respects_llm_none(tmp_path):
    # Floor only augments a non-empty LLM pick; an explicit NONE verdict
    # stays authoritative even when keywords match.
    db = _make_rule_db(tmp_path)
    client = FakeClient(code_responses=[GOOD_SCRIPT], rule_select_response="NONE")
    runner = _make_runner(tmp_path, client, db=db)
    res = runner.run_detailed("算股票報酬")
    assert res.ok
    assert "Yahoo chart API" not in client.code_prompts[0]


def test_llm_rule_selector_error_falls_back_to_keyword(tmp_path):
    # Selector LLM call dies → keyword-scored retrieval takes over, so a
    # keyword-matching request still gets the topical recipe.
    client = FakeClient(
        code_responses=[GOOD_SCRIPT],
        rule_select_response=RuntimeError("ollama down"),
    )
    runner = _make_runner(tmp_path, client, db=_make_rule_db(tmp_path))
    res = runner.run_detailed("算股票報酬")
    assert res.ok
    assert "Yahoo chart API" in client.code_prompts[0]


def _make_ref_rule_db(tmp_path):
    db = KnowledgeDatabase(tmp_path / "k.sqlite3")
    db.upsert_codegen_knowledge(
        category="finance", title="股價資料源",
        technique="股價用 Yahoo chart API。公式以參考頁為準：\n參考: https://example.org/rate",
        keywords=("股票", "報酬"), origin="seed", confidence=0.95,
    )
    return db


def test_reference_grounding_injects_block(tmp_path):
    # A selected rule cites a 參考: URL → page fetched (stubbed) → fast model
    # distills the request-relevant definitions → extract lands in the codegen
    # prompt. Formulas live on the reference page, never hardcoded in the DB.
    client = FakeClient(code_responses=[GOOD_SCRIPT], rule_select_response="1",
                        ground_response="- YTD 基期為去年最後交易日收盤")
    runner = _make_runner(tmp_path, client, db=_make_ref_rule_db(tmp_path))
    fetched: list[str] = []
    runner._fetch_url_text = (  # type: ignore[assignment]
        lambda url: fetched.append(url) or "rate of return page text")
    res = runner.run_detailed("0050 今年以來報酬率")
    assert res.ok
    assert fetched == ["https://example.org/rate"]
    assert client.calls["ground"] == 1
    assert "參考資料" in client.code_prompts[0]
    assert "YTD 基期為去年最後交易日收盤" in client.code_prompts[0]


def test_reference_grounding_fetch_failure_fails_open(tmp_path):
    # Reference page unreachable → grounding silently skipped, codegen proceeds.
    client = FakeClient(code_responses=[GOOD_SCRIPT], rule_select_response="1")
    runner = _make_runner(tmp_path, client, db=_make_ref_rule_db(tmp_path))

    def boom(url):
        raise OSError("offline")

    runner._fetch_url_text = boom  # type: ignore[assignment]
    res = runner.run_detailed("0050 今年以來報酬率")
    assert res.ok
    assert client.calls["ground"] == 0
    assert "參考資料" not in client.code_prompts[0]


def test_reference_grounding_irrelevant_extract_omits_block(tmp_path):
    # Extractor judges the page irrelevant (NONE) → no 參考資料 block.
    client = FakeClient(code_responses=[GOOD_SCRIPT], rule_select_response="1",
                        ground_response="NONE")
    runner = _make_runner(tmp_path, client, db=_make_ref_rule_db(tmp_path))
    runner._fetch_url_text = lambda url: "unrelated page"  # type: ignore[assignment]
    res = runner.run_detailed("0050 今年以來報酬率")
    assert res.ok
    assert client.calls["ground"] == 1
    assert "參考資料" not in client.code_prompts[0]


def test_search_grounding_injects_block_and_burns_budget(tmp_path):
    # No rule cites a 參考 URL → fallback: ONE web search (stubbed) → page
    # fetch → distillation → 參考資料 block with source URLs; budget counted.
    client = FakeClient(code_responses=[GOOD_SCRIPT],
                        ground_response="- 政策金利 0.75%（2026-01 起）",
                        needs_search_response="YES（缺現行政策金利）",
                        searchq_response="日銀 政策金利 現在")
    runner = _make_runner(tmp_path, client)
    searched: list[tuple[str, int]] = []
    runner.search_fn = lambda q, n: searched.append((q, n)) or [
        SimpleNamespace(url="https://example.jp/boj")]
    runner._fetch_url_text = lambda url: "boj rate page"  # type: ignore[assignment]
    res = runner.run_detailed("以日銀現行政策金利算100萬日圓10年複利")
    assert res.ok
    assert searched == [("日銀 政策金利 現在", 4)]
    assert client.calls["searchq"] == 1
    assert client.calls["ground"] == 1
    assert "參考資料" in client.code_prompts[0]
    assert "政策金利 0.75%" in client.code_prompts[0]
    assert "https://example.jp/boj" in client.code_prompts[0]
    state = json.loads((tmp_path / "generated_tools" / "search_state.json").read_text(encoding="utf-8"))
    assert state["count"] == 1
    assert "以日銀現行政策金利算100萬日圓10年複利" in state["cache"]


def test_search_grounding_cache_hit_skips_search(tmp_path):
    # Same request later the same day → cached block reused, zero new queries.
    request = "以日銀現行政策金利算100萬日圓10年複利"
    state_path = tmp_path / "generated_tools" / "search_state.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps({
        "date": date.today().isoformat(), "count": 3,
        "cache": {request: "- 政策金利 0.75%\n來源:\nhttps://example.jp/boj"},
    }), encoding="utf-8")
    client = FakeClient(code_responses=[GOOD_SCRIPT],
                        needs_search_response="YES（缺現行政策金利）")
    runner = _make_runner(tmp_path, client)

    def no_search(q, n):
        raise AssertionError("search must not fire on cache hit")

    runner.search_fn = no_search
    res = runner.run_detailed(request)
    assert res.ok
    assert client.calls["searchq"] == 0
    assert "政策金利 0.75%" in client.code_prompts[0]
    assert json.loads(state_path.read_text(encoding="utf-8"))["count"] == 3


def test_search_grounding_skips_engine_internal_and_pdf_urls(tmp_path):
    # Engine-internal links and PDFs (HTML-only extractor) must not consume the
    # 2-page fetch budget; the real article behind them gets fetched instead.
    client = FakeClient(code_responses=[GOOD_SCRIPT],
                        ground_response="- 現行 0.75%（自2026-01）",
                        needs_search_response="YES（缺現行政策金利）")
    runner = _make_runner(tmp_path, client)
    runner.search_fn = lambda q, n: [
        SimpleNamespace(url="https://www.boj.or.jp/press/speech.pdf"),
        SimpleNamespace(url="https://search.yahoo.co.jp/image/search?p=rate"),
        SimpleNamespace(url="https://news.example.jp/boj-rate"),
    ]
    fetched: list[str] = []
    runner._fetch_url_text = (  # type: ignore[assignment]
        lambda url: fetched.append(url) or "article text")
    res = runner.run_detailed("以日銀現行政策金利算複利")
    assert res.ok
    assert fetched == ["https://news.example.jp/boj-rate"]
    assert "https://news.example.jp/boj-rate" in client.code_prompts[0]


def test_search_grounding_cache_stores_raw_texts_and_redistills(tmp_path):
    # New-format cache keeps the raw page texts; a later run re-distills them
    # (so distill prompt fixes apply) without touching the search backend.
    request = "以日銀現行政策金利算100萬日圓10年複利"
    state_path = tmp_path / "generated_tools" / "search_state.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps({
        "date": date.today().isoformat(), "count": 2,
        "cache": {request: {"texts": ["boj rate page"],
                            "sources": ["https://example.jp/boj"]}},
    }), encoding="utf-8")
    client = FakeClient(code_responses=[GOOD_SCRIPT],
                        ground_response="- 現行 0.75%（自2026-01）；另有檢討中的 1%，尚未生效",
                        needs_search_response="YES（缺現行政策金利）")
    runner = _make_runner(tmp_path, client)

    def no_search(q, n):
        raise AssertionError("search must not fire on cache hit")

    runner.search_fn = no_search
    res = runner.run_detailed(request)
    assert res.ok
    assert client.calls["searchq"] == 0
    assert client.calls["ground"] == 1  # re-distilled from cached raw text
    assert "現行 0.75%" in client.code_prompts[0]
    assert "https://example.jp/boj" in client.code_prompts[0]
    assert json.loads(state_path.read_text(encoding="utf-8"))["count"] == 2


def test_search_grounding_budget_exhausted_skips(tmp_path):
    # Daily cap reached → no reformulation, no search, codegen proceeds bare.
    state_path = tmp_path / "generated_tools" / "search_state.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps({
        "date": date.today().isoformat(), "count": 4, "cache": {}}), encoding="utf-8")
    client = FakeClient(code_responses=[GOOD_SCRIPT],
                        needs_search_response="YES（缺現行政策金利）")
    runner = _make_runner(tmp_path, client)
    searched: list[str] = []
    runner.search_fn = lambda q, n: searched.append(q) or []
    res = runner.run_detailed("需要新知識的需求")
    assert res.ok
    assert searched == []
    assert client.calls["searchq"] == 0
    assert "參考資料" not in client.code_prompts[0]
    assert json.loads(state_path.read_text(encoding="utf-8"))["count"] == 4


def test_search_grounding_failure_fails_open_but_burns_budget(tmp_path):
    # Backend blows up mid-search → /new continues without grounding, and the
    # query still counts against the budget (it may have reached Yahoo).
    client = FakeClient(code_responses=[GOOD_SCRIPT],
                        needs_search_response="YES（缺現行政策金利）")
    runner = _make_runner(tmp_path, client)

    def boom(q, n):
        raise RuntimeError("yahoo down")

    runner.search_fn = boom
    res = runner.run_detailed("需要新知識的需求")
    assert res.ok
    assert "參考資料" not in client.code_prompts[0]
    state = json.loads((tmp_path / "generated_tools" / "search_state.json").read_text(encoding="utf-8"))
    assert state["count"] == 1


def test_rule_grounding_plus_search_when_current_fact_missing(tmp_path):
    # Rule grounding succeeded with a generic formula page, but the request
    # hinges on a current announced value (policy rate) the page doesn't have
    # → the gate says YES → search grounding APPENDS to the rule references.
    client = FakeClient(code_responses=[GOOD_SCRIPT], rule_select_response="1",
                        ground_response="- 複利公式 FV=PV*(1+r)^n",
                        needs_search_response="YES（缺現行政策金利）",
                        searchq_response="日銀 政策金利 現在")
    runner = _make_runner(tmp_path, client, db=_make_ref_rule_db(tmp_path))
    runner.search_fn = lambda q, n: [SimpleNamespace(url="https://example.jp/boj")]
    runner._fetch_url_text = lambda url: "page text"  # type: ignore[assignment]
    res = runner.run_detailed("以日銀現行政策金利算100萬日圓10年複利的股票替代報酬")
    assert res.ok
    assert client.calls["needs_search"] == 1
    assert client.calls["searchq"] == 1
    assert client.calls["ground"] == 2  # rule distill + search distill
    assert "複利公式 FV=PV*(1+r)^n" in client.code_prompts[0]
    assert "https://example.jp/boj" in client.code_prompts[0]


def test_rule_grounding_gate_no_skips_search(tmp_path):
    # Gate says NO (references suffice) → search backend never touched.
    client = FakeClient(code_responses=[GOOD_SCRIPT], rule_select_response="1",
                        ground_response="- YTD 基期為去年最後交易日收盤")
    runner = _make_runner(tmp_path, client, db=_make_ref_rule_db(tmp_path))

    def no_search(q, n):
        raise AssertionError("search must not fire when gate says NO")

    runner.search_fn = no_search
    runner._fetch_url_text = lambda url: "rate page"  # type: ignore[assignment]
    res = runner.run_detailed("0050 今年以來報酬率")
    assert res.ok
    assert client.calls["needs_search"] == 1
    assert client.calls["searchq"] == 0
    assert "YTD 基期為去年最後交易日收盤" in client.code_prompts[0]


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


def test_opencode_generate_posts_openai_chat_payload() -> None:
    import unittest.mock as mock

    captured = {}

    class _FakeResp:
        def read(self):
            return b'{"choices": [{"message": {"content": "  hello\\n"}}]}'

        def close(self):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

    def _fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        captured["timeout"] = timeout
        captured["headers"] = dict(req.header_items())
        captured["payload"] = json.loads(req.data.decode("utf-8"))
        return _FakeResp()

    client = OpenCodeTextClient(
        base_url="https://opencode.ai/zen/v1",
        model="big-pickle",
        timeout_seconds=123,
        api_key="secret",
        max_tokens=99,
    )
    with mock.patch("openclaw_adapter.dynamic_tools.urlopen", _fake_urlopen):
        out = client.generate("write code", temperature=0.2)

    assert out == "hello"
    # zen only serves /chat/completions; the client targets it regardless of base_url suffix.
    assert captured["url"] == "https://opencode.ai/zen/v1/chat/completions"
    assert captured["timeout"] == 123
    assert captured["payload"] == {
        "model": "big-pickle",
        "messages": [{"role": "user", "content": "write code"}],
        "temperature": 0.2,
        "stream": False,
        # CoT models need a >=4096 floor or `content` starves; 99 is raised to 4096.
        "max_tokens": 4096,
    }
    assert captured["headers"]["Authorization"] == "Bearer secret"
    # Browser UA is mandatory: zen blocks the default Python-urllib UA with CF 1010.
    # Header keys are capitalized by urllib (User-agent).
    ua = captured["headers"].get("User-agent") or captured["headers"].get("User-Agent")
    assert ua and "Mozilla/5.0" in ua


def test_opencode_cli_generate_strips_banner_and_ansi(tmp_path) -> None:
    import unittest.mock as mock

    class _FakeProc:
        returncode = 0

        def communicate(self, timeout=None):
            return ("\x1b[0m\n> build · big-pickle\n\x1b[0m\nhello\n", "")

        def poll(self):
            return 0

        def kill(self):
            pass

    def _fake_popen(cmd, **kwargs):
        assert cmd[:5] == ["opencode", "run", "--pure", "-m", "opencode/big-pickle"]
        env = kwargs["env"]
        assert env["HOME"] == str(tmp_path / ".opencode-home")
        assert env["CLAUDE_CONFIG_DIR"] == str(tmp_path / ".opencode-home" / ".claude")
        assert env["XDG_DATA_HOME"] == str(tmp_path / ".opencode-home" / ".local" / "share")
        assert env["XDG_CACHE_HOME"] == str(tmp_path / ".opencode-home" / ".cache")
        return _FakeProc()

    client = OpenCodeCliTextClient(
        model="opencode/big-pickle",
        timeout_seconds=123,
        cwd=tmp_path,
    )
    with mock.patch("openclaw_adapter.dynamic_tools.subprocess.Popen", _fake_popen):
        out = client.generate("say hello")

    assert out == "hello"


def test_opencode_cli_abort_kills_running_subprocess(tmp_path) -> None:
    """A disconnect mid-generation must kill the opencode subprocess (the real
    cloud transport when no API key is set), not let it run to timeout (#30)."""
    import threading
    import unittest.mock as mock

    killed = threading.Event()
    started = threading.Event()

    class _FakeProc:
        returncode = -9

        def communicate(self, timeout=None):
            started.set()
            killed.wait(timeout=5.0)  # block until abort() kills us
            return ("", "killed")

        def poll(self):
            return None if not killed.is_set() else -9

        def kill(self):
            killed.set()

    client = OpenCodeCliTextClient(
        model="opencode/big-pickle", timeout_seconds=123, cwd=tmp_path
    )
    with mock.patch("openclaw_adapter.dynamic_tools.subprocess.Popen",
                    lambda *a, **k: _FakeProc()):
        result: dict[str, object] = {}

        def _run():
            try:
                client.generate("say hello")
            except CloudBackendUnavailable as exc:
                result["err"] = str(exc)

        t = threading.Thread(target=_run)
        t.start()
        assert started.wait(1.0)
        client.abort()
        t.join(2.0)

    assert killed.is_set()
    assert "aborted" in result.get("err", "")


def test_builder_prefers_opencode_http_when_codegen_backend_enabled(tmp_path) -> None:
    import unittest.mock as mock

    settings = SimpleNamespace(
        openclaw_codegen_backend="opencode",
        openclaw_opencode_base_url="https://opencode.ai/zen/v1",
        openclaw_opencode_model="big-pickle",
        openclaw_opencode_api_key=None,
        openclaw_opencode_timeout_seconds=321,
        openclaw_local_text_backend=None,
        openclaw_local_text_model=None,
        openclaw_local_text_endpoint="http://127.0.0.1:11434",
        openclaw_local_text_timeout_seconds=45,
        openclaw_codegen_fast_model=None,
        knowledge_db_path=str(tmp_path / "knowledge.sqlite3"),
    )
    # HTTP probe succeeds → direct-HTTP client; CLI must not even be probed.
    with mock.patch("openclaw_adapter.dynamic_tools.probe_opencode", return_value=True), \
         mock.patch("openclaw_adapter.dynamic_tools.probe_opencode_cli") as cli_probe:
        runner = build_dynamic_tool_runner_from_settings(settings)

    assert isinstance(runner.client, OpenCodeTextClient)
    cli_probe.assert_not_called()
    assert runner.fast_model == "big-pickle"
    assert runner.strong_model == "big-pickle"
    assert runner.client.timeout_seconds == 321


def test_builder_falls_back_to_opencode_cli_when_http_blocked(tmp_path) -> None:
    import unittest.mock as mock

    settings = SimpleNamespace(
        openclaw_codegen_backend="opencode",
        openclaw_opencode_base_url="https://opencode.ai/zen/v1",
        openclaw_opencode_model="big-pickle",
        openclaw_opencode_api_key=None,
        openclaw_opencode_timeout_seconds=321,
        openclaw_local_text_backend=None,
        openclaw_local_text_model=None,
        openclaw_local_text_endpoint="http://127.0.0.1:11434",
        openclaw_local_text_timeout_seconds=45,
        openclaw_codegen_fast_model=None,
        knowledge_db_path=str(tmp_path / "knowledge.sqlite3"),
    )
    # HTTP probe fails (e.g. CF block) → CLI fallback.
    with mock.patch("openclaw_adapter.dynamic_tools.probe_opencode", return_value=False), \
         mock.patch("openclaw_adapter.dynamic_tools.probe_opencode_cli", return_value=True):
        runner = build_dynamic_tool_runner_from_settings(settings)

    assert isinstance(runner.client, OpenCodeCliTextClient)
    assert runner.fast_model == "opencode/big-pickle"
    assert runner.strong_model == "opencode/big-pickle"
    assert runner.client.timeout_seconds == 321


def test_builder_falls_back_to_ollama_when_opencode_probe_fails(tmp_path) -> None:
    import unittest.mock as mock

    settings = SimpleNamespace(
        openclaw_codegen_backend="opencode",
        openclaw_opencode_base_url="https://opencode.ai/zen/v1",
        openclaw_opencode_model="big-pickle",
        openclaw_opencode_api_key=None,
        openclaw_opencode_timeout_seconds=321,
        openclaw_local_text_backend="ollama",
        openclaw_local_text_model="qwen3:14b",
        openclaw_local_text_endpoint="http://127.0.0.1:11434",
        openclaw_local_text_timeout_seconds=75,
        openclaw_codegen_fast_model="qwen2.5-coder:7b",
        knowledge_db_path=str(tmp_path / "knowledge.sqlite3"),
    )
    # Both HTTP and CLI unavailable → Ollama.
    with mock.patch("openclaw_adapter.dynamic_tools.probe_opencode", return_value=False), \
         mock.patch("openclaw_adapter.dynamic_tools.probe_opencode_cli", return_value=False), \
         mock.patch("openclaw_adapter.dynamic_tools.probe_ollama", return_value=True):
        runner = build_dynamic_tool_runner_from_settings(settings)

    assert isinstance(runner.client, OllamaTextClient)
    assert runner.fast_model == "qwen2.5-coder:7b"
    assert runner.strong_model == "qwen3:14b"


# ── cross-validation (generator/critic split) ────────────────────────────────


def _make_cross_runner(tmp_path, primary, validator):
    runner = _make_runner(tmp_path, primary)
    runner.validator_client = validator
    runner.validator_model = "qwen3:14b"
    return runner


def test_validate_answer_no_validator_unchanged(tmp_path):
    # validator_client None → single self-validation, reason passed through verbatim.
    primary = FakeClient(code_responses=[], validate_responses=["FAIL: 主題不符"])
    runner = _make_runner(tmp_path, primary)
    valid, reason = runner._validate_answer("x", "y")
    assert valid is False
    assert reason == "主題不符"


def test_validate_answer_both_pass(tmp_path):
    primary = FakeClient(code_responses=[])
    validator = FakeClient(code_responses=[])  # both default to PASS
    runner = _make_cross_runner(tmp_path, primary, validator)
    valid, reason = runner._validate_answer("夏威夷天氣", "夏威夷 25度")
    assert valid is True
    assert primary.calls["validate"] == 1
    assert validator.calls["validate"] == 1


def test_validate_answer_disagreement_marked(tmp_path):
    # Generator self-approves; independent local validator dissents → not valid,
    # tagged [歧見] so the repair loop can react and later fall back.
    primary = FakeClient(code_responses=[])  # PASS
    validator = FakeClient(code_responses=[],
                           validate_responses=["FAIL: 地點是東京不是夏威夷"])
    runner = _make_cross_runner(tmp_path, primary, validator)
    valid, reason = runner._validate_answer("夏威夷天氣", "東京 25度")
    assert valid is False
    assert reason.startswith("[歧見]")
    assert "東京" in reason


def test_validate_answer_both_fail_no_disagreement_tag(tmp_path):
    primary = FakeClient(code_responses=[], validate_responses=["FAIL: 空洞"])
    validator = FakeClient(code_responses=[], validate_responses=["FAIL: 也空洞"])
    runner = _make_cross_runner(tmp_path, primary, validator)
    valid, reason = runner._validate_answer("x", "y")
    assert valid is False
    assert not reason.startswith("[歧見]")


class _SlowValidator:
    """Validator stub whose generate() blocks longer than the timeout cap."""

    def __init__(self, delay, verdict="PASS"):
        self.delay = delay
        self.verdict = verdict
        self.model = "qwen3:14b"
        self.calls = 0

    def generate(self, prompt, *, temperature=0.0, think=False):
        self.calls += 1
        time.sleep(self.delay)
        return self.verdict


def test_validate_answer_slow_validator_times_out_to_primary_pass(tmp_path):
    # Local validator hangs past the cap → fall back to the primary verdict
    # (here PASS) instead of stalling the reply.
    primary = FakeClient(code_responses=[])  # PASS
    validator = _SlowValidator(delay=5.0)
    runner = _make_cross_runner(tmp_path, primary, validator)
    runner.validator_timeout_seconds = 0.2
    t0 = time.time()
    valid, reason = runner._validate_answer("夏威夷天氣", "夏威夷 25度")
    assert valid is True
    assert reason == ""
    assert time.time() - t0 < 3.0  # did not wait for the 5s validator


def test_validate_answer_slow_validator_times_out_keeps_primary_fail(tmp_path):
    # Primary FAILs and the validator is too slow → return the primary FAIL
    # reason verbatim, not a [歧見] tag (no second opinion arrived).
    primary = FakeClient(code_responses=[], validate_responses=["FAIL: 主題不符"])
    validator = _SlowValidator(delay=5.0)
    runner = _make_cross_runner(tmp_path, primary, validator)
    runner.validator_timeout_seconds = 0.2
    valid, reason = runner._validate_answer("x", "y")
    assert valid is False
    assert reason == "主題不符"
    assert not reason.startswith("[歧見]")


def test_prewarm_validator_pokes_local_model(tmp_path):
    # Pre-warm fires one trivial generate at the validator so its model loads
    # during generation; restores the validator's model afterwards.
    primary = FakeClient(code_responses=[])
    validator = _SlowValidator(delay=0.0)
    runner = _make_cross_runner(tmp_path, primary, validator)
    runner._prewarm_validator()
    assert validator.calls == 1
    assert validator.model == "qwen3:14b"  # restored


def test_prewarm_validator_noop_without_validator(tmp_path):
    runner = _make_runner(tmp_path, FakeClient(code_responses=[]))
    runner._prewarm_validator()  # must not raise when validator_client is None


def test_cross_validation_disputed_fallback_on_exhaustion(tmp_path):
    # Generator approves every attempt but the local validator always dissents.
    # Repairs exhaust → ship the generator-approved answer WITH a caveat rather
    # than hard-failing, and do NOT register it for reuse.
    primary = FakeClient(code_responses=[GOOD_SCRIPT + f"\n# v{i}" for i in range(7)])
    validator = FakeClient(code_responses=[],
                           validate_responses=["FAIL: 地點是東京不是夏威夷"] * 7)
    runner = _make_cross_runner(tmp_path, primary, validator)
    res = runner.run_detailed("夏威夷天氣")
    assert res.ok
    assert res.generations == 7
    assert "本地交叉驗證有疑慮" in res.answer
    assert "東京" in res.answer
    assert len(runner._load_manifest()) == 0


def test_builder_enables_cross_validation_for_opencode(tmp_path):
    import unittest.mock as mock

    settings = SimpleNamespace(
        openclaw_codegen_backend="opencode",
        openclaw_opencode_base_url="https://opencode.ai/zen/v1",
        openclaw_opencode_model="big-pickle",
        openclaw_opencode_api_key=None,
        openclaw_opencode_timeout_seconds=321,
        openclaw_local_text_backend="ollama",
        openclaw_local_text_model="qwen3:14b",
        openclaw_local_text_endpoint="http://127.0.0.1:11434",
        openclaw_local_text_timeout_seconds=75,
        openclaw_codegen_fast_model=None,
        knowledge_db_path=str(tmp_path / "knowledge.sqlite3"),
    )
    with mock.patch("openclaw_adapter.dynamic_tools.probe_opencode", return_value=True), \
         mock.patch("openclaw_adapter.dynamic_tools.probe_ollama", return_value=True):
        runner = build_dynamic_tool_runner_from_settings(settings)

    assert isinstance(runner.client, OpenCodeTextClient)
    assert isinstance(runner.validator_client, OllamaTextClient)
    assert runner.validator_model == "qwen3:14b"
    assert "驗證" in runner.backend_label
    # Cloud generator → enable restart-on-outage failover to the local runner.
    assert runner.cloud_failover_restart is True


class _DownCloudClient:
    """Stands in for the cloud client when big-pickle is unreachable: every
    generate() raises CloudBackendUnavailable, like the CLI timing out/failing."""

    def __init__(self):
        self.model = "opencode/big-pickle"
        self.num_predict = None
        self.num_ctx = None
        self.timeout_seconds = 900

    def generate(self, prompt, *, temperature=0.0, think=False):
        raise CloudBackendUnavailable("OpenCode CLI failed: boom")


def test_cloud_unavailable_propagates_from_request_path(tmp_path):
    # The very first cloud call (intent split) raises; it must surface as
    # CloudBackendUnavailable, not be swallowed into a request-as-is fallback.
    runner = _make_runner(tmp_path, _DownCloudClient())
    with pytest.raises(CloudBackendUnavailable):
        runner.run_detailed("夏威夷天氣")


def test_run_issues_failover_restart_when_cloud_down(tmp_path):
    import unittest.mock as mock

    runner = _make_runner(tmp_path, _DownCloudClient())
    runner.cloud_failover_restart = True
    with mock.patch("openclaw_adapter.dynamic_tools.subprocess.Popen") as popen:
        msg = runner.run("夏威夷天氣")
    assert "正在重啟" in msg
    popen.assert_called_once()
    # Cooldown marker recorded so a follow-up failure won't loop the restart.
    assert (runner.tools_dir / "_cloud_failover_restart.ts").exists()


def test_run_failover_restart_suppressed_within_cooldown(tmp_path):
    import unittest.mock as mock

    runner = _make_runner(tmp_path, _DownCloudClient())
    runner.cloud_failover_restart = True
    runner.tools_dir.mkdir(parents=True, exist_ok=True)
    (runner.tools_dir / "_cloud_failover_restart.ts").write_text(
        str(time.time()), encoding="utf-8")
    with mock.patch("openclaw_adapter.dynamic_tools.subprocess.Popen") as popen:
        msg = runner.run("夏威夷天氣")
    popen.assert_not_called()
    assert "正在重啟" not in msg
    assert "連不上" in msg


def test_run_no_failover_restart_when_disabled(tmp_path):
    # Local-generation runner (cloud_failover_restart False) must never restart.
    import unittest.mock as mock

    runner = _make_runner(tmp_path, _DownCloudClient())
    assert runner.cloud_failover_restart is False
    with mock.patch("openclaw_adapter.dynamic_tools.subprocess.Popen") as popen:
        msg = runner.run("夏威夷天氣")
    popen.assert_not_called()
    assert "正在重啟" not in msg
    assert "連不上" in msg
