from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = REPO_ROOT / "scripts"

sys.path.insert(0, str(SCRIPTS))
import _docs_yaml  # noqa: E402
from _docs_yaml import loads  # noqa: E402


def _run(script: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPTS / script)],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
    )


def test_check_docs_health_passes_on_repo() -> None:
    result = _run("check_docs_health.py")
    assert result.returncode == 0, result.stdout + result.stderr


def test_check_manifest_passes_on_repo() -> None:
    result = _run("check_manifest.py")
    assert result.returncode == 0, result.stdout + result.stderr


def test_check_doc_drift_passes_on_repo() -> None:
    result = _run("check_doc_drift.py")
    assert result.returncode == 0, result.stdout + result.stderr


def test_minimal_yaml_loader_parses_mappings_lists_and_seq_of_maps(monkeypatch) -> None:
    # Force the dependency-free fallback: installed PyYAML (pulled in via
    # faster-whisper -> huggingface-hub) would otherwise shadow the code under
    # test and coerce `issue: 3` to int.
    monkeypatch.setattr(_docs_yaml, "_pyyaml", None)
    text = """
top:
  status_vocabulary:
    - shipped
    - beta
  repos:
    a:
      role: primary
      responsibility:
        - one
        - two
  summary: >-
    folded line one
    folded line two
  items:
    - issue: 3
      title: "x: y"
      status: done
""".strip()
    data = loads(text)
    top = data["top"]
    assert top["status_vocabulary"] == ["shipped", "beta"]
    assert top["repos"]["a"]["role"] == "primary"
    assert top["repos"]["a"]["responsibility"] == ["one", "two"]
    assert top["summary"] == "folded line one folded line two"
    assert top["items"] == [{"issue": "3", "title": "x: y", "status": "done"}]
