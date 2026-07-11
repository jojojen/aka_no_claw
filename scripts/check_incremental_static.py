#!/usr/bin/env python3
"""Run Ruff only on Python files changed between two Git revisions.

This is deliberately a changed-files gate: the repository's historical Ruff
backlog must not make unrelated pull requests fail, while every touched Python
file is held to the current static-check baseline (aka_no_claw#72 C4).
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent


def changed_python_files(base: str, head: str) -> list[str]:
    """Return existing changed Python paths under source or tests."""
    result = subprocess.run(
        [
            "git",
            "diff",
            "--name-only",
            "--diff-filter=ACMR",
            base,
            head,
            "--",
            "src",
            "tests",
        ],
        cwd=REPO_ROOT,
        text=True,
        stdout=subprocess.PIPE,
        check=True,
    )
    return [
        path
        for path in result.stdout.splitlines()
        if path.endswith(".py") and (REPO_ROOT / path).is_file()
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", required=True, help="Git revision to diff from")
    parser.add_argument("--head", default="HEAD", help="Git revision to diff to (default: HEAD)")
    args = parser.parse_args()

    try:
        paths = changed_python_files(args.base, args.head)
    except subprocess.CalledProcessError as exc:
        print(f"FATAL: cannot diff {args.base}..{args.head}: {exc}", file=sys.stderr)
        return 2

    if not paths:
        print(f"Incremental static check: no changed Python files in {args.base}..{args.head}.")
        return 0

    print("Incremental static check (Ruff):", " ".join(paths))
    return subprocess.run([sys.executable, "-m", "ruff", "check", *paths], cwd=REPO_ROOT).returncode


if __name__ == "__main__":
    raise SystemExit(main())
