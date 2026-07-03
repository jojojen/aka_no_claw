"""Benchmark-scoped self-healing repair loop behind the ``/fix`` command.

v1 scope (issue #57): repair targets are the deterministic benchmarks under
``docs/fix_benchmarks/`` only — never production code. The loop is:

    reproduce (run verifier on broken parser)
    -> ask an LLM for a replacement module
    -> run the benchmark's own verify.py on the candidate
    -> iterate on failure output
    -> on PASS, show a unified diff + apply button
    -> apply persists the candidate into the benchmark's attempts/ dir

The LLM is any object exposing ``generate(prompt, *, temperature) -> str``
(the shared duck type of OllamaTextClient / OpenCodeTextClient /
MistralTextClient), so the provider can be swapped by changing only
:func:`resolve_fix_llm_client`.
"""
from __future__ import annotations

import difflib
import logging
import re
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_BENCHMARKS_ROOT = _REPO_ROOT / "docs" / "fix_benchmarks"

_MAX_ATTEMPTS = 4
_FIXTURE_CHAR_CAP = 8000
_DIFF_CHAR_CAP = 3000
_VERIFIER_TIMEOUT_SECONDS = 60
_PASS_RATE_RE = re.compile(r"Pass rate:\s*(\d+)/(\d+)")

# Entry-point contract is keyed on the broken module's filename; this is a
# closed protocol enum (like HTTP methods), not open-world recognition.
_ENTRY_BY_FILENAME = {
    "parser.py": ("--parser", "parse(html: str) -> dict"),
    "classifier.py": ("--classifier", "classify(capture: dict) -> dict"),
}


@dataclass(frozen=True)
class FixBenchmark:
    name: str
    root: Path
    verify_py: Path
    broken: Path
    verifier_flag: str
    entry_signature: str


@dataclass
class VerifierResult:
    exit_code: int
    output: str
    passed: int | None = None
    total: int | None = None

    @property
    def ok(self) -> bool:
        return self.exit_code == 0

    def summary(self) -> str:
        if self.passed is not None and self.total is not None:
            return f"{self.passed}/{self.total} pass"
        return "PASS" if self.ok else "FAIL"


@dataclass
class FixRepairResult:
    benchmark: FixBenchmark
    success: bool
    attempts: int
    candidate_source: str | None
    diff: str | None
    verifier: VerifierResult | None
    failure_reason: str | None = None


def discover_benchmarks(root: Path = DEFAULT_BENCHMARKS_ROOT) -> list[FixBenchmark]:
    """A benchmark is any dir (recursive) holding verify.py + broken/<one .py>."""
    benchmarks: list[FixBenchmark] = []
    if not root.is_dir():
        return benchmarks
    for verify_py in sorted(root.rglob("verify.py")):
        bench_dir = verify_py.parent
        broken_dir = bench_dir / "broken"
        if not broken_dir.is_dir():
            continue
        broken_files = sorted(broken_dir.glob("*.py"))
        if len(broken_files) != 1:
            continue
        entry = _ENTRY_BY_FILENAME.get(broken_files[0].name)
        if entry is None:
            continue
        flag, signature = entry
        benchmarks.append(
            FixBenchmark(
                name=bench_dir.relative_to(root).as_posix(),
                root=bench_dir,
                verify_py=verify_py,
                broken=broken_files[0],
                verifier_flag=flag,
                entry_signature=signature,
            )
        )
    return benchmarks


def find_benchmark(name: str, root: Path = DEFAULT_BENCHMARKS_ROOT) -> FixBenchmark | None:
    wanted = (name or "").strip().strip("/")
    for bench in discover_benchmarks(root):
        if bench.name == wanted:
            return bench
    return None


def run_verifier(
    benchmark: FixBenchmark,
    candidate_path: Path,
    *,
    python_executable: str | None = None,
) -> VerifierResult:
    cmd = [
        python_executable or sys.executable,
        str(benchmark.verify_py),
        benchmark.verifier_flag,
        str(candidate_path),
    ]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=_VERIFIER_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return VerifierResult(exit_code=124, output="verifier timed out")
    output = (proc.stdout or "") + (("\n" + proc.stderr) if proc.stderr else "")
    result = VerifierResult(exit_code=proc.returncode, output=output.strip())
    match = _PASS_RATE_RE.search(output)
    if match:
        result.passed = int(match.group(1))
        result.total = int(match.group(2))
    return result


def _read_fixtures(benchmark: FixBenchmark) -> list[tuple[str, str]]:
    fixtures_dir = benchmark.root / "fixtures"
    out: list[tuple[str, str]] = []
    if not fixtures_dir.is_dir():
        return out
    for path in sorted(fixtures_dir.iterdir()):
        if path.suffix not in (".html", ".json") or not path.is_file():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if len(text) > _FIXTURE_CHAR_CAP:
            text = text[:_FIXTURE_CHAR_CAP] + "\n<!-- truncated -->"
        out.append((path.name, text))
    return out


def build_repair_prompt(
    benchmark: FixBenchmark,
    *,
    broken_source: str,
    verifier_output: str,
    previous_candidate: str | None = None,
) -> str:
    parts: list[str] = [
        "You are repairing a broken Python extraction module so it passes a "
        "deterministic verifier.",
        f"Benchmark: {benchmark.name}",
        f"The module must define {benchmark.entry_signature} at top level and use "
        "only the Python standard library.",
        "Return ONE complete replacement module in a single ```python fenced "
        "block. No explanations outside the block.",
        "Generalize across ALL fixtures — do not hardcode per-fixture outputs or "
        "key on fixture file names.",
        "",
        "## Current module (fails the verifier)",
        "```python",
        (previous_candidate or broken_source).rstrip(),
        "```",
        "",
        "## Verifier output",
        "```",
        verifier_output.strip(),
        "```",
    ]
    fixtures = _read_fixtures(benchmark)
    if fixtures:
        parts.append("")
        parts.append("## Fixtures the module must handle")
        for name, text in fixtures:
            parts.append(f"### {name}")
            parts.append("```")
            parts.append(text.rstrip())
            parts.append("```")
    return "\n".join(parts)


def _build_local_client(settings) -> tuple[object, str]:
    from .dynamic_tools import OllamaTextClient
    from .llm_pool_settings import LLM_PROVIDER_LOCAL, resolve_provider_model

    # 本地模型一律跟著 LLM 池設定 UI 走，不在這裡寫死任何型號
    local_model = resolve_provider_model(settings, LLM_PROVIDER_LOCAL)
    client = OllamaTextClient(
        endpoint=getattr(settings, "openclaw_local_text_endpoint", "http://127.0.0.1:11434"),
        model=str(local_model),
        # 修復 prompt ~7k tokens、產出整個模組：qwen3:14b 在 Mac mini 上
        # 300s 會超時（2026-07-03 live run），最後一棒不能死於超時 → 900s 起跳。
        timeout_seconds=max(
            900, int(getattr(settings, "openclaw_local_text_timeout_seconds", 300))
        ),
        num_ctx=16384,
    )
    return client, f"Ollama {local_model}"


def resolve_fix_llm_clients(
    settings,
) -> tuple[list[tuple[object, str]], str | None]:
    """Build the repair-LLM chain in priority order. Returns (chain, warning).

    Chain: OpenCode big-pickle → Mistral (when a key is configured) → local
    Ollama as the last resort. The loop advances down the chain when a client
    fails mid-generation (big-pickle is known to pass the short probe yet drop
    long requests), announcing every switch. Adding another vendor (Gemini, a
    rotation pool, …) means changing only this factory — the repair loop
    depends solely on ``client.generate(prompt, *, temperature) -> str``.
    """
    from .dynamic_tools import (
        OpenCodeTextClient,
        _build_mistral_client,
        probe_opencode,
    )

    chain: list[tuple[object, str]] = []

    base_url = (
        getattr(settings, "openclaw_opencode_base_url", None)
        or "https://opencode.ai/zen/v1"
    ).strip()
    raw_model = (getattr(settings, "openclaw_opencode_model", None) or "big-pickle").strip()
    model = raw_model.split("/")[-1] if "/" in raw_model else raw_model
    try:
        if probe_opencode(base_url, model=model, timeout=10.0):
            chain.append(
                (
                    OpenCodeTextClient(
                        base_url=base_url,
                        model=model,
                        api_key=getattr(settings, "openclaw_opencode_api_key", None),
                        timeout_seconds=max(
                            60,
                            int(getattr(settings, "openclaw_opencode_timeout_seconds", 900)),
                        ),
                    ),
                    f"OpenCode {model}",
                )
            )
    except Exception:  # noqa: BLE001 — probe failure just drops this provider
        logger.warning("fix_command: OpenCode probe raised", exc_info=True)

    try:
        mistral_client, mistral_model = _build_mistral_client(settings)
        if mistral_client is not None:
            chain.append((mistral_client, f"Mistral {mistral_model}"))
    except Exception:  # noqa: BLE001 — probe failure just drops this provider
        logger.warning("fix_command: Mistral setup raised", exc_info=True)

    local, local_label = _build_local_client(settings)
    chain.append((local, local_label))

    warning = None
    if len(chain) == 1:
        warning = (
            f"⚠️ 沒有可用的雲端模型，只剩本地模型（{local_label}）修復，"
            "品質可能較低。"
        )
    return chain, warning


def run_fix_repair(
    benchmark: FixBenchmark,
    llm_clients: list[tuple[object, str]],
    *,
    notify=None,
    max_attempts: int = _MAX_ATTEMPTS,
    python_executable: str | None = None,
) -> FixRepairResult:
    from .dynamic_tools import _extract_code

    def _notify(text: str) -> None:
        if notify is not None:
            try:
                notify(text)
            except Exception:  # noqa: BLE001 — progress must never kill the loop
                logger.warning("fix_command: notify failed", exc_info=True)

    broken_source = benchmark.broken.read_text(encoding="utf-8")
    baseline = run_verifier(benchmark, benchmark.broken, python_executable=python_executable)
    if baseline.ok:
        return FixRepairResult(
            benchmark=benchmark,
            success=False,
            attempts=0,
            candidate_source=None,
            diff=None,
            verifier=baseline,
            failure_reason=(
                "broken parser 已通過 verifier，沒有可修復的失敗（benchmark 異常）。"
            ),
        )
    _notify(f"基線重現：broken parser {baseline.summary()}，開始修復…")

    if not llm_clients:
        return FixRepairResult(
            benchmark=benchmark,
            success=False,
            attempts=0,
            candidate_source=None,
            diff=None,
            verifier=baseline,
            failure_reason="沒有可用的修復 LLM。",
        )

    workdir = Path(tempfile.mkdtemp(prefix="fix_candidate_"))
    verifier_output = baseline.output
    candidate_source: str | None = None
    last_result: VerifierResult | None = baseline
    chain = list(llm_clients)
    client_index = 0
    attempt = 0
    while attempt < max_attempts:
        attempt += 1
        prompt = build_repair_prompt(
            benchmark,
            broken_source=broken_source,
            verifier_output=verifier_output,
            previous_candidate=candidate_source,
        )
        client, label = chain[client_index]
        try:
            response = client.generate(prompt, temperature=0.0)
        except Exception as exc:  # noqa: BLE001 — report, don't crash the handler
            if client_index + 1 < len(chain):
                # big-pickle 型故障：probe 過但長請求被斷 → 換下一個供應商續跑，
                # 不燒掉這次 attempt（C4：切換必須明講；C7：不回頭重打斷線的主機）。
                next_label = chain[client_index + 1][1]
                _notify(
                    f"⚠️ {label} 連線中斷（{exc}），改用 {next_label} 繼續修復…"
                )
                client_index += 1
                attempt -= 1
                continue
            return FixRepairResult(
                benchmark=benchmark,
                success=False,
                attempts=attempt,
                candidate_source=candidate_source,
                diff=None,
                verifier=last_result,
                failure_reason=f"LLM 呼叫失敗（{label}，供應商鏈已用盡）：{exc}",
            )
        code = _extract_code(response)
        if not code.strip():
            # 保留真實 verifier 回饋，別讓一個空回覆把下一輪 prompt 的失敗細節洗掉
            verifier_output = (
                "previous reply contained no code block\n\n"
                f"latest verifier output:\n{verifier_output}"
            )
            _notify(f"attempt {attempt}/{max_attempts} — 回覆沒有程式碼區塊，重試…")
            continue
        candidate_source = code
        candidate_path = workdir / f"attempt_{attempt:02d}_{benchmark.broken.name}"
        candidate_path.write_text(code, encoding="utf-8")
        result = run_verifier(benchmark, candidate_path, python_executable=python_executable)
        last_result = result
        if result.ok:
            diff = "".join(
                difflib.unified_diff(
                    broken_source.splitlines(keepends=True),
                    code.splitlines(keepends=True),
                    fromfile=f"broken/{benchmark.broken.name}",
                    tofile=f"repaired/{benchmark.broken.name}",
                )
            )
            _notify(f"attempt {attempt}/{max_attempts} — verifier PASS（{result.summary()}）")
            return FixRepairResult(
                benchmark=benchmark,
                success=True,
                attempts=attempt,
                candidate_source=code,
                diff=diff,
                verifier=result,
            )
        verifier_output = result.output
        _notify(f"attempt {attempt}/{max_attempts} — {result.summary()}，繼續迭代…")

    return FixRepairResult(
        benchmark=benchmark,
        success=False,
        attempts=max_attempts,
        candidate_source=candidate_source,
        diff=None,
        verifier=last_result,
        failure_reason=f"{max_attempts} 次嘗試後 verifier 仍未通過。",
    )


def apply_candidate(benchmark: FixBenchmark, candidate_source: str) -> Path:
    """Persist an approved candidate into the benchmark's attempts/ dir.

    broken/ is never modified — it must stay broken for the benchmark to keep
    reproducing the failure.
    """
    attempts_dir = benchmark.root / "attempts"
    attempts_dir.mkdir(exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    target = attempts_dir / f"attempt_{stamp}_opencode.py"
    target.write_text(candidate_source, encoding="utf-8")
    return target


class FixPendingApplyCache:
    """Token-addressed PASS results awaiting user approval (per-chat, TTL)."""

    def __init__(self, *, max_entries: int = 32, ttl_seconds: int = 600) -> None:
        self._max_entries = max(4, max_entries)
        self._ttl_seconds = max(60, ttl_seconds)
        self._lock = threading.Lock()
        self._entries: dict[str, tuple[str, float, FixRepairResult]] = {}

    def put(self, chat_id: str, result: FixRepairResult) -> str:
        token = uuid.uuid4().hex[:8]
        now = time.time()
        with self._lock:
            self._prune_locked(now)
            self._entries[token] = (str(chat_id), now, result)
            while len(self._entries) > self._max_entries:
                self._entries.pop(next(iter(self._entries)), None)
        return token

    def pop(self, *, token: str, chat_id: str) -> FixRepairResult | None:
        with self._lock:
            self._prune_locked(time.time())
            entry = self._entries.get(token)
            if entry is None or entry[0] != str(chat_id):
                return None
            self._entries.pop(token, None)
            return entry[2]

    def _prune_locked(self, now: float) -> None:
        for token in [
            t for t, (_c, created, _r) in self._entries.items()
            if now - created > self._ttl_seconds
        ]:
            self._entries.pop(token, None)


def _fix_reply_markup(token: str) -> dict[str, object]:
    return {
        "inline_keyboard": [
            [
                {"text": "✅ 套用修復", "callback_data": f"fix:{token}:apply"},
                {"text": "❌ 放棄", "callback_data": f"fix:{token}:discard"},
            ]
        ]
    }


def _truncate(text: str, cap: int) -> str:
    if len(text) <= cap:
        return text
    return text[:cap] + "\n…（已截斷）"


def _format_benchmark_list(root: Path) -> str:
    benchmarks = discover_benchmarks(root)
    if not benchmarks:
        return f"找不到任何 benchmark（{root}）。"
    lines = ["可修復的 benchmark（/fix <名稱> 開始修復）："]
    for bench in benchmarks:
        baseline = run_verifier(bench, bench.broken)
        lines.append(f"- {bench.name} — broken {baseline.summary()}")
    return "\n".join(lines)


def build_fix_handler(
    settings,
    pending_cache: FixPendingApplyCache,
    *,
    benchmarks_root: Path = DEFAULT_BENCHMARKS_ROOT,
    notifier_factory=None,
    client_resolver=resolve_fix_llm_clients,
):
    def handler(remainder: str, chat_id: str):
        name = (remainder or "").strip()
        if not name:
            return _format_benchmark_list(benchmarks_root)
        benchmark = find_benchmark(name, benchmarks_root)
        if benchmark is None:
            return (
                f"找不到 benchmark「{name}」。\n\n"
                + _format_benchmark_list(benchmarks_root)
            )
        notifier = notifier_factory(chat_id) if notifier_factory is not None else None
        notify = notifier.send if notifier is not None else None
        chain, warning = client_resolver(settings)
        if notify is not None:
            prefix = (warning + "\n") if warning else ""
            labels = " → ".join(label for _client, label in chain)
            notify(f"{prefix}修復引擎鏈：{labels}")
        result = run_fix_repair(benchmark, chain, notify=notify)
        if not result.success:
            detail = _truncate((result.verifier.output if result.verifier else ""), 1200)
            return (
                f"❌ /fix {benchmark.name} 失敗（{result.attempts} 次嘗試）：\n"
                f"{result.failure_reason}\n\n{detail}"
            )
        token = pending_cache.put(str(chat_id), result)
        text = (
            f"✅ /fix {benchmark.name} — verifier PASS"
            f"（{result.verifier.summary()}，attempt {result.attempts}）\n\n"
            f"diff（broken → repaired）：\n{_truncate(result.diff or '', _DIFF_CHAR_CAP)}\n\n"
            "按「套用修復」把修復版存到 attempts/（10 分鐘內有效；broken/ 保持原樣）。"
        )
        return text, _fix_reply_markup(token)

    return handler


def build_fix_callback_handler(pending_cache: FixPendingApplyCache):
    def handler(payload: str, original_text: str, chat_id: str):
        token, _, action = (payload or "").partition(":")
        if action == "discard":
            discarded = pending_cache.pop(token=token, chat_id=str(chat_id))
            if discarded is None:
                return "修復候選已過期", None, None
            return "已放棄此修復候選", "已放棄修復候選，broken/ 未變動。", None
        if action != "apply":
            logger.warning("fix_command: unknown callback action=%r", action)
            return "未知按鈕", None, None
        result = pending_cache.pop(token=token, chat_id=str(chat_id))
        if result is None or not result.candidate_source:
            return "修復候選已過期", "修復候選已過期，請重新執行 /fix。", None
        target = apply_candidate(result.benchmark, result.candidate_source)
        verify_after = run_verifier(result.benchmark, target)
        try:
            shown = target.relative_to(_REPO_ROOT)
        except ValueError:
            shown = target
        return (
            "已套用修復",
            f"已存檔：{shown}\n存檔後複驗：{verify_after.summary()}",
            None,
        )

    return handler
