#!/usr/bin/env python3
"""Documentation health checker (issue #7, Deliverables 1 + 4).

Enforces the governance rules from docs/DOCUMENTATION_GOVERNANCE.md:

  A. Every documentation file under docs/ is listed in DOCS_INDEX.md.
  B. Every stateful doc (non-archive) carries the required metadata headers
     (Last reviewed:, Owner area:).
  C. Files under docs/archive/ are not marked as Current.
  D. The canonical truth documents exist.
  (Deliverable 4) Relative markdown / local doc links resolve to real files.

Exit code 0 = healthy, 1 = one or more violations (printed grouped).
Pure stdlib so it runs in CI without extra deps.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DOCS = REPO_ROOT / "docs"
INDEX = DOCS / "DOCS_INDEX.md"

TRUTH_DOCS = [
    "AGENT_ONBOARDING.md",
    "SYSTEM_MAP.md",
    "CURRENT_STATE.md",
    "TASK_ROUTING.md",
    "VERIFICATION_MATRIX.md",
    "DOCS_INDEX.md",
]

REQUIRED_METADATA = ["Last reviewed:", "Owner area:"]

# Generated benchmark reports are covered by their parent README / experiment
# log. Requiring every timestamped result to be indexed would make each local
# benchmark run create doc-governance churn unrelated to system truth.
GENERATED_REPORT_RE = re.compile(
    r"^local_tool_calling_benchmark/"
    r"(latest_results|latest_realistic_probe|results_\d{8}T\d{6}Z|realistic_probe_\d{8}T\d{6}Z)\.md$"
)

# Docs that index themselves / are pure listings — exempt from "must be indexed".
INDEX_SELF_EXEMPT = {"DOCS_INDEX.md"}

# Standalone public-facing HTML docs (e.g. sanitized troubleshooting guides meant
# to be shared outside the repo), named explicitly rather than glob-matched so
# this doesn't also sweep in the many synthetic *.html fixtures under
# fix_benchmarks/ (those are test data, not documentation). They intentionally
# carry no internal governance metadata — forcing "Owner area:"/"Last reviewed:"
# into a public artifact would leak internal process detail it's designed to
# omit. Rule A (must be indexed) still applies so they don't rot invisibly;
# only rule B (metadata) is waived for them.
PUBLIC_HTML_DOCS = {"BROADLINK_LOCAL_NETWORK_TROUBLESHOOTING.html"}
PUBLIC_HTML_METADATA_EXEMPT = PUBLIC_HTML_DOCS

LINK_RE = re.compile(r"\[[^\]]*\]\(([^)]+)\)")
INDEX_TARGET_RE = re.compile(r"\[[^\]]+\]\(([^)]+)\)")


def _index_link_targets() -> set[str]:
    """Relative (to docs/) link targets recorded in DOCS_INDEX.md."""
    targets: set[str] = set()
    for line in INDEX.read_text(encoding="utf-8").splitlines():
        for m in INDEX_TARGET_RE.finditer(line):
            targets.add(m.group(1).strip())
    return targets


def _docs_files() -> list[Path]:
    md_files = (p for p in DOCS.rglob("*.md") if p.is_file())
    html_docs = (DOCS / name for name in PUBLIC_HTML_DOCS if (DOCS / name).is_file())
    return sorted({*md_files, *html_docs})


def check_indexing(errors: list[str]) -> None:
    targets = _index_link_targets()
    for path in _docs_files():
        rel = path.relative_to(DOCS).as_posix()
        if rel in INDEX_SELF_EXEMPT:
            continue
        if GENERATED_REPORT_RE.match(rel):
            continue
        if rel not in targets:
            errors.append(f"[A indexing] {rel} is not listed in DOCS_INDEX.md")


def check_metadata(errors: list[str]) -> None:
    for path in _docs_files():
        rel = path.relative_to(DOCS).as_posix()
        if rel.startswith("archive/"):
            continue  # archive docs are frozen historical snapshots
        if GENERATED_REPORT_RE.match(rel):
            continue
        if rel in PUBLIC_HTML_METADATA_EXEMPT:
            continue
        text = path.read_text(encoding="utf-8")
        for field in REQUIRED_METADATA:
            if field not in text:
                errors.append(f"[B metadata] {rel} is missing required header '{field}'")


def check_archive_status(errors: list[str]) -> None:
    archive_dir = DOCS / "archive"
    if not archive_dir.is_dir():
        return
    for path in sorted(archive_dir.rglob("*.md")):
        text = path.read_text(encoding="utf-8")
        rel = path.relative_to(DOCS).as_posix()
        if re.search(r"(?im)^\s*Status:\s*Current\b", text):
            errors.append(
                f"[C archive] {rel} is under docs/archive/ but marked 'Status: Current'"
            )


def check_truth_docs(errors: list[str]) -> None:
    for name in TRUTH_DOCS:
        if not (DOCS / name).is_file():
            errors.append(f"[D truth] required truth document docs/{name} is missing")


def check_links(errors: list[str]) -> None:
    for path in _docs_files():
        rel = path.relative_to(DOCS).as_posix()
        for line in path.read_text(encoding="utf-8").splitlines():
            for m in LINK_RE.finditer(line):
                target = m.group(1).strip()
                if not target or target.startswith("#"):
                    continue
                low = target.lower()
                if low.startswith(("http://", "https://", "mailto:", "tel:")):
                    continue
                # strip anchor / query
                target_path = target.split("#", 1)[0].split("?", 1)[0]
                if not target_path:
                    continue
                resolved = (path.parent / target_path).resolve()
                if not resolved.exists():
                    errors.append(
                        f"[link] {rel}: broken link -> '{target}' "
                        f"(resolved {resolved})"
                    )


def main() -> int:
    if not INDEX.is_file():
        print(f"FATAL: {INDEX} not found", file=sys.stderr)
        return 1
    errors: list[str] = []
    check_truth_docs(errors)
    check_indexing(errors)
    check_metadata(errors)
    check_archive_status(errors)
    check_links(errors)

    if errors:
        print("Documentation health check FAILED:\n")
        for e in errors:
            print(f"  - {e}")
        print(f"\n{len(errors)} problem(s) found.")
        return 1
    print("Documentation health check PASSED.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
