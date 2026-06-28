#!/usr/bin/env python3
"""Deterministic verifier for seller snapshot lifecycle classifiers."""
from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType


BENCHMARK_ROOT = Path(__file__).resolve().parent
FIXTURES = BENCHMARK_ROOT / "fixtures"
EXPECTED = BENCHMARK_ROOT / "expected"
SCHEMA_KEYS = (
    "case_id",
    "action",
    "retry_after_seconds",
    "should_parse",
    "should_requeue",
    "reason",
)


def _load_classifier(path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location("candidate_classifier", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load classifier from {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if not hasattr(module, "classify"):
        raise RuntimeError(f"{path} does not define classify(capture: dict)")
    return module


def _load_json(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def _normalize_result(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise AssertionError(f"classify() returned {type(value).__name__}, expected dict")
    extra = sorted(set(value) - set(SCHEMA_KEYS))
    missing = sorted(set(SCHEMA_KEYS) - set(value))
    if extra:
        raise AssertionError(f"unexpected keys: {extra}")
    if missing:
        raise AssertionError(f"missing keys: {missing}")
    return {key: value[key] for key in SCHEMA_KEYS}


def _compare(name: str, actual: dict[str, object], expected: dict[str, object]) -> list[str]:
    errors: list[str] = []
    for key in SCHEMA_KEYS:
        if actual.get(key) != expected.get(key):
            errors.append(
                f"{name}: {key}: expected {expected.get(key)!r}, got {actual.get(key)!r}"
            )
    return errors


def verify(classifier_path: Path, min_pass_rate: float) -> int:
    classifier = _load_classifier(classifier_path)
    fixture_paths = sorted(FIXTURES.glob("*.json"))
    if not fixture_paths:
        print("No fixture files found.", file=sys.stderr)
        return 2

    passed = 0
    failures: list[str] = []

    for fixture_path in fixture_paths:
        case_name = fixture_path.stem
        fixture = _load_json(fixture_path)
        expected = _load_json(EXPECTED / f"{case_name}.json")
        try:
            actual = _normalize_result(classifier.classify(fixture))
            case_errors = _compare(case_name, actual, expected)
        except Exception as exc:  # noqa: BLE001 - verifier must report failures.
            case_errors = [f"{case_name}: classifier raised {exc.__class__.__name__}: {exc}"]

        if case_errors:
            failures.extend(case_errors)
            print(f"FAIL {case_name}")
        else:
            passed += 1
            print(f"PASS {case_name}")

    pass_rate = passed / len(fixture_paths)
    print(f"\nPass rate: {passed}/{len(fixture_paths)} = {pass_rate:.0%}")

    if failures:
        print("\nFailures:")
        for failure in failures:
            print(f"  - {failure}")

    if pass_rate < min_pass_rate:
        print(
            f"\nVerifier FAILED: pass rate {pass_rate:.0%} < required {min_pass_rate:.0%}",
            file=sys.stderr,
        )
        return 1

    print("\nVerifier PASSED.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--classifier", required=True, help="Path to candidate classifier.py")
    parser.add_argument(
        "--min-pass-rate",
        type=float,
        default=1.0,
        help="Minimum required pass rate, default: 1.0",
    )
    args = parser.parse_args()
    if not 0 < args.min_pass_rate <= 1:
        parser.error("--min-pass-rate must be in (0, 1]")
    return verify(Path(args.classifier).resolve(), args.min_pass_rate)


if __name__ == "__main__":
    raise SystemExit(main())
