"""Generator-independent benchmark evaluation helpers (R4.7)."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Protocol

_BENCHMARKS_PATH = Path(__file__).resolve().parent.parent / "dynamic_tools_benchmarks.json"
_NUM_RE = re.compile(r"-?\d+(?:,\d{3})*(?:\.\d+)?")
_PCT_NUM_RE = re.compile(r"(-?\d+(?:,\d{3})*(?:\.\d+)?)\s*%")


class DetailedRunner(Protocol):
    def run_detailed(self, request: str): ...


def _numbers(text: str, *, pct_only: bool) -> list[float]:
    raw = (_PCT_NUM_RE if pct_only else _NUM_RE).findall(text or "")
    out: list[float] = []
    for token in raw:
        try:
            out.append(float(token.replace(",", "")))
        except ValueError:
            continue
    return out


def _check_numeric(answer: str, check: dict) -> tuple[bool, str]:
    label = check.get("label", "數值")
    expected = float(check["expected"])
    tol = abs(expected) * float(check.get("tolerance_pct", 5.0)) / 100.0
    tol = max(tol, 1e-9)
    pool = _numbers(answer, pct_only=bool(check.get("is_pct")))
    best = None
    for num in pool:
        diff = abs(num - expected)
        if best is None or diff < best[0]:
            best = (diff, num)
    if best is not None and best[0] <= tol:
        return True, f"{label}: 命中 {best[1]:g}（期望 {expected:g}±{tol:g}）"
    got = f"最接近 {best[1]:g}" if best else "找不到數值"
    return False, f"{label}: 失敗（期望 {expected:g}±{tol:g}，{got}）"


def _check_direction(answer: str, keyword_groups: list) -> tuple[bool, str]:
    lc = (answer or "").lower()
    for group in keyword_groups:
        if not any(str(kw).lower() in lc for kw in group):
            return False, f"方向性失敗：缺少 {group} 任一關鍵字"
    return True, "方向性通過"


def load_benchmarks() -> list[dict]:
    return json.loads(_BENCHMARKS_PATH.read_text(encoding="utf-8"))


def run_benchmarks(runner: DetailedRunner, benchmarks: list[dict] | None = None) -> bool:
    benchmarks = benchmarks if benchmarks is not None else load_benchmarks()
    all_pass = True
    for bench in benchmarks:
        print(f"\n=== benchmark {bench['id']}: {bench['request']} ===")
        result = runner.run_detailed(bench["request"])
        print(f"ok={result.ok} reused={result.reused} gens={result.generations}")
        print("ANSWER:", result.answer or result.error)
        bench_pass = result.ok
        if not result.ok:
            all_pass = False
            print("FAIL: 工具執行失敗")
            continue
        for check in bench.get("numeric_checks", []):
            ok, msg = _check_numeric(result.answer, check)
            bench_pass = bench_pass and ok
            print(("  ✅ " if ok else "  ❌ ") + msg)
        if bench.get("direction_keywords"):
            ok, msg = _check_direction(result.answer, bench["direction_keywords"])
            bench_pass = bench_pass and ok
            print(("  ✅ " if ok else "  ❌ ") + msg)
        print(f"  → {bench['id']}: {'PASS' if bench_pass else 'FAIL'}")
        all_pass = all_pass and bench_pass
    print(f"\n=== overall: {'PASS' if all_pass else 'FAIL'} ===")
    return all_pass
