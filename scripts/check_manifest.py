#!/usr/bin/env python3
"""SYSTEM_MANIFEST.yaml checker (issue #7, Deliverable 2).

Validates the machine-readable system truth index:

  - status values come from the declared status_vocabulary
  - every repo entry has role + responsibility
  - every subsystem entry has status + owner_repo

Exit 0 = valid, 1 = violations. Uses scripts/_docs_yaml (PyYAML-free).
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _docs_yaml import load_manifest  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
MANIFEST = REPO_ROOT / "SYSTEM_MANIFEST.yaml"

REQUIRED_REPO_FIELDS = ["role", "responsibility"]
REQUIRED_SUBSYSTEM_FIELDS = ["status", "owner_repo"]


def main() -> int:
    if not MANIFEST.is_file():
        print(f"FATAL: {MANIFEST} not found", file=sys.stderr)
        return 1

    manifest = load_manifest(MANIFEST)
    errors: list[str] = []

    vocab = manifest.get("status_vocabulary")
    if not isinstance(vocab, list) or not vocab:
        errors.append("[vocab] status_vocabulary is missing or empty")
        vocab = []
    allowed = set(vocab)

    repos = manifest.get("repos") or {}
    if not isinstance(repos, dict) or not repos:
        errors.append("[repos] no repos defined")
    for name, entry in (repos.items() if isinstance(repos, dict) else []):
        if not isinstance(entry, dict):
            errors.append(f"[repos] {name}: entry is not a mapping")
            continue
        for field in REQUIRED_REPO_FIELDS:
            if not entry.get(field):
                errors.append(f"[repos] {name}: missing required field '{field}'")

    subsystems = manifest.get("subsystems") or {}
    if not isinstance(subsystems, dict) or not subsystems:
        errors.append("[subsystems] no subsystems defined")
    for name, entry in (subsystems.items() if isinstance(subsystems, dict) else []):
        if not isinstance(entry, dict):
            errors.append(f"[subsystems] {name}: entry is not a mapping")
            continue
        for field in REQUIRED_SUBSYSTEM_FIELDS:
            if not entry.get(field):
                errors.append(f"[subsystems] {name}: missing required field '{field}'")
        status = entry.get("status")
        if status and allowed and status not in allowed:
            errors.append(
                f"[subsystems] {name}: status '{status}' is not in "
                f"status_vocabulary {sorted(allowed)}"
            )
        owner = entry.get("owner_repo")
        if owner and isinstance(repos, dict) and owner not in repos:
            errors.append(
                f"[subsystems] {name}: owner_repo '{owner}' is not a defined repo"
            )

    if errors:
        print("Manifest check FAILED:\n")
        for e in errors:
            print(f"  - {e}")
        print(f"\n{len(errors)} problem(s) found.")
        return 1
    print("Manifest check PASSED.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
