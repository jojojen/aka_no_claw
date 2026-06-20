#!/usr/bin/env python3
"""Documentation drift checker (issue #7, Deliverable 3).

Cross-checks the machine-readable subsystem status in SYSTEM_MANIFEST.yaml
against the human-readable subsystem table in docs/CURRENT_STATE.md. When the
two disagree, status has drifted and one of them is stale.

Exit 0 = aligned, 1 = drift detected. PyYAML-free.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _docs_yaml import load_manifest  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
MANIFEST = REPO_ROOT / "SYSTEM_MANIFEST.yaml"
CURRENT_STATE = REPO_ROOT / "docs" / "CURRENT_STATE.md"

# manifest subsystem key -> CURRENT_STATE.md subsystem label (first table column).
# Labels are matched case-insensitively with backticks stripped.
SUBSYSTEM_LABELS = {
    "telegram": "Telegram bot",
    "cli_registry": "CLI tool registry",
    "price_lookup": "Price lookup",
    "liquidity_board": "Liquidity board",
    "research": "/research",
    "dynamic_tools": "Dynamic tools /new",
    "sns_monitor": "SNS monitor",
    "snsbuzz": "/snsbuzz",
    "reputation_snapshot": "Reputation snapshot",
    "opportunity_agent": "Opportunity agent",
    "dashboard": "Dashboard",
    "knowledge_rag": "Knowledge / RAG",
    "quiz_teaching": "Quiz / teaching loop",
}


def _norm(label: str) -> str:
    return label.replace("`", "").strip().lower()


def _current_state_status() -> dict[str, str]:
    """Parse the '## Subsystems' table into {normalized label: status}."""
    text = CURRENT_STATE.read_text(encoding="utf-8")
    statuses: dict[str, str] = {}
    in_subsystems = False
    for line in text.splitlines():
        if line.startswith("## "):
            in_subsystems = line.strip().lower() == "## subsystems"
            continue
        if not in_subsystems or not line.strip().startswith("|"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 2:
            continue
        name, status = cells[0], cells[1]
        if _norm(name) in ("subsystem", "") or set(name) <= {"-", " "}:
            continue  # header / separator row
        statuses[_norm(name)] = _norm(status)
    return statuses


def main() -> int:
    for path in (MANIFEST, CURRENT_STATE):
        if not path.is_file():
            print(f"FATAL: {path} not found", file=sys.stderr)
            return 1

    manifest = load_manifest(MANIFEST)
    subsystems = manifest.get("subsystems") or {}
    cs_status = _current_state_status()

    drift: list[str] = []
    for key, label in SUBSYSTEM_LABELS.items():
        entry = subsystems.get(key)
        if not isinstance(entry, dict):
            drift.append(
                f"[manifest] subsystem '{key}' is missing from SYSTEM_MANIFEST.yaml"
            )
            continue
        manifest_status = _norm(str(entry.get("status") or ""))
        cs = cs_status.get(_norm(label))
        if cs is None:
            drift.append(
                f"[current_state] subsystem '{label}' (manifest '{key}') is missing "
                f"from CURRENT_STATE.md '## Subsystems' table"
            )
            continue
        if manifest_status != cs:
            drift.append(
                f"[drift] {key}: SYSTEM_MANIFEST='{manifest_status}' but "
                f"CURRENT_STATE '{label}'='{cs}'"
            )

    if drift:
        print("Documentation drift check FAILED:\n")
        for d in drift:
            print(f"  - {d}")
        print(
            f"\n{len(drift)} drift problem(s) found. Update SYSTEM_MANIFEST.yaml and "
            "docs/CURRENT_STATE.md together so status stays consistent."
        )
        return 1
    print(
        f"Documentation drift check PASSED "
        f"({len(SUBSYSTEM_LABELS)} subsystems aligned)."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
