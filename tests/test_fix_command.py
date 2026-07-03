from __future__ import annotations

import shutil
from pathlib import Path

from openclaw_adapter.fix_command import (
    DEFAULT_BENCHMARKS_ROOT,
    FixPendingApplyCache,
    apply_candidate,
    build_fix_callback_handler,
    build_fix_handler,
    build_repair_prompt,
    discover_benchmarks,
    find_benchmark,
    run_fix_repair,
    run_verifier,
)

PRICE_BENCH = "price_reference_sources"
REFERENCE_SOURCE = (
    DEFAULT_BENCHMARKS_ROOT / PRICE_BENCH / "reference" / "parser.py"
).read_text(encoding="utf-8")


class FakeLLM:
    def __init__(self, responses: list[str]) -> None:
        self._responses = list(responses)
        self.prompts: list[str] = []

    def generate(self, prompt: str, *, temperature: float = 0.0, think: bool = False) -> str:
        self.prompts.append(prompt)
        if len(self._responses) > 1:
            return self._responses.pop(0)
        return self._responses[0]


def test_discovery_finds_verifier_backed_benchmarks_only():
    names = {bench.name for bench in discover_benchmarks()}
    assert names == {
        "price_reference_sources",
        "seller_snapshot_sources",
        "seller_snapshot_sources/lifecycle",
    }


def test_discovery_infers_entry_contract():
    by_name = {bench.name: bench for bench in discover_benchmarks()}
    assert by_name["price_reference_sources"].verifier_flag == "--parser"
    lifecycle = by_name["seller_snapshot_sources/lifecycle"]
    assert lifecycle.verifier_flag == "--classifier"
    assert "classify" in lifecycle.entry_signature


def test_verifier_fails_broken_and_passes_reference():
    bench = find_benchmark(PRICE_BENCH)
    assert bench is not None
    broken = run_verifier(bench, bench.broken)
    assert not broken.ok
    assert broken.total and broken.passed is not None and broken.passed < broken.total
    reference = run_verifier(bench, bench.root / "reference" / "parser.py")
    assert reference.ok
    assert reference.passed == reference.total


def test_repair_prompt_carries_contract_and_failures():
    bench = find_benchmark(PRICE_BENCH)
    prompt = build_repair_prompt(
        bench,
        broken_source="def parse(html):\n    return {}\n",
        verifier_output="FAIL knsr_listing_v1",
    )
    assert "parse(html: str) -> dict" in prompt
    assert "FAIL knsr_listing_v1" in prompt
    assert "knsr_listing_v1.html" in prompt
    assert "do not hardcode" in prompt


def test_repair_loop_succeeds_with_reference_solution():
    bench = find_benchmark(PRICE_BENCH)
    llm = FakeLLM([f"```python\n{REFERENCE_SOURCE}\n```"])
    progress: list[str] = []
    result = run_fix_repair(bench, [(llm, "fake")], notify=progress.append)
    assert result.success
    assert result.attempts == 1
    assert result.verifier is not None and result.verifier.ok
    assert result.diff and "repaired/parser.py" in result.diff
    assert any("PASS" in line for line in progress)


def test_repair_loop_exhausts_on_garbage_and_reports_failure():
    bench = find_benchmark(PRICE_BENCH)
    llm = FakeLLM(["```python\ndef parse(html):\n    return {}\n```"])
    result = run_fix_repair(bench, [(llm, "fake")], max_attempts=2)
    assert not result.success
    assert result.attempts == 2
    assert result.failure_reason
    assert len(llm.prompts) == 2
    # second prompt must carry the first attempt's verifier feedback
    assert "missing keys" in llm.prompts[1] or "FAIL" in llm.prompts[1]


class BrokenPipeLLM:
    def __init__(self) -> None:
        self.calls = 0

    def generate(self, prompt: str, *, temperature: float = 0.0, think: bool = False) -> str:
        self.calls += 1
        raise RuntimeError("Remote end closed connection without response")


def test_repair_loop_rotates_to_next_provider_without_burning_attempt():
    bench = find_benchmark(PRICE_BENCH)
    dead_cloud = BrokenPipeLLM()
    backup = FakeLLM([f"```python\n{REFERENCE_SOURCE}\n```"])
    progress: list[str] = []
    result = run_fix_repair(
        bench,
        [(dead_cloud, "OpenCode big-pickle"), (backup, "Mistral m")],
        notify=progress.append,
        max_attempts=2,
    )
    assert result.success
    assert result.attempts == 1  # the dead provider's failure did not burn it
    assert dead_cloud.calls == 1  # C7: never re-poke the dropped provider
    assert any("OpenCode big-pickle 連線中斷" in line and "Mistral m" in line
               for line in progress)


def test_repair_loop_reports_when_provider_chain_exhausted():
    bench = find_benchmark(PRICE_BENCH)
    result = run_fix_repair(
        bench,
        [(BrokenPipeLLM(), "cloud-a"), (BrokenPipeLLM(), "cloud-b")],
        max_attempts=2,
    )
    assert not result.success
    assert "供應商鏈已用盡" in result.failure_reason
    assert "cloud-b" in result.failure_reason


def _copy_benchmark(tmp_path: Path) -> Path:
    root = tmp_path / "fix_benchmarks"
    shutil.copytree(DEFAULT_BENCHMARKS_ROOT / PRICE_BENCH, root / PRICE_BENCH)
    return root


def test_apply_candidate_writes_attempts_and_keeps_broken(tmp_path):
    root = _copy_benchmark(tmp_path)
    bench = find_benchmark(PRICE_BENCH, root)
    broken_before = bench.broken.read_text(encoding="utf-8")
    target = apply_candidate(bench, REFERENCE_SOURCE)
    assert target.parent == bench.root / "attempts"
    assert target.read_text(encoding="utf-8") == REFERENCE_SOURCE
    assert bench.broken.read_text(encoding="utf-8") == broken_before
    assert run_verifier(bench, target).ok


def test_handler_lists_benchmarks_without_args(tmp_path):
    root = _copy_benchmark(tmp_path)
    handler = build_fix_handler(
        settings=object(),
        pending_cache=FixPendingApplyCache(),
        benchmarks_root=root,
        client_resolver=lambda s: ([(FakeLLM([""]), "fake")], None),
    )
    reply = handler("", "123")
    assert isinstance(reply, str)
    assert PRICE_BENCH in reply


def test_handler_repair_then_callback_apply(tmp_path):
    root = _copy_benchmark(tmp_path)
    cache = FixPendingApplyCache()
    llm = FakeLLM([f"```python\n{REFERENCE_SOURCE}\n```"])
    handler = build_fix_handler(
        settings=object(),
        pending_cache=cache,
        benchmarks_root=root,
        client_resolver=lambda s: ([(llm, "fake")], None),
    )
    reply = handler(PRICE_BENCH, "123")
    assert isinstance(reply, tuple)
    text, markup = reply
    assert "verifier PASS" in text
    callback_data = markup["inline_keyboard"][0][0]["callback_data"]
    assert callback_data.startswith("fix:")
    payload = callback_data[len("fix:"):]

    callback = build_fix_callback_handler(cache)
    toast, message, _markup = callback(payload, text, "123")
    assert toast == "已套用修復"
    attempts = list((root / PRICE_BENCH / "attempts").glob("attempt_*_opencode.py"))
    assert len(attempts) == 1
    assert "複驗" in message


def test_callback_rejects_wrong_chat_and_expired_token(tmp_path):
    root = _copy_benchmark(tmp_path)
    cache = FixPendingApplyCache()
    llm = FakeLLM([f"```python\n{REFERENCE_SOURCE}\n```"])
    handler = build_fix_handler(
        settings=object(),
        pending_cache=cache,
        benchmarks_root=root,
        client_resolver=lambda s: ([(llm, "fake")], None),
    )
    text, markup = handler(PRICE_BENCH, "123")
    payload = markup["inline_keyboard"][0][0]["callback_data"][len("fix:"):]
    callback = build_fix_callback_handler(cache)
    toast, _msg, _kb = callback(payload, text, "999")
    assert toast == "修復候選已過期"
    # a wrong-chat press must not consume the token; the owner can still apply
    toast2, _msg2, _kb2 = callback(payload, text, "123")
    assert toast2 == "已套用修復"
