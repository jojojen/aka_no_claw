"""
Tests for workspace_validator (D1.3).

Uses throwaway git repos under tmp_path; does not touch real siblings.
"""

import json
import os
import subprocess
import tempfile
from pathlib import Path
from textwrap import dedent

import pytest

from openclaw_adapter.workspace_validator import (
    Finding,
    PackageReport,
    validate_package,
    validate_workspace,
    format_report,
    _repo_name_from_url,
    _load_lock_file,
    _git_rev_parse,
    _git_status_porcelain,
    _load_pyproject_name,
)


def _git_cmd(repo_path: Path, *args):
    """Run a git command in a repo, with CI-safe user config."""
    env = os.environ.copy()
    env["GIT_CONFIG_GLOBAL"] = "/dev/null"
    env["GIT_CONFIG_SYSTEM"] = "/dev/null"

    cmd = [
        "git",
        "-c", "user.email=t@t",
        "-c", "user.name=t",
        "-C", str(repo_path),
        *args,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, check=False, env=env)
    return result


def _init_git_repo(repo_path: Path, pyproject_name: str = None) -> str:
    """Initialize a git repo with one commit and optional pyproject.toml.

    Returns: The commit SHA.
    """
    repo_path.mkdir(parents=True, exist_ok=True)

    # Init repo
    _git_cmd(repo_path, "init")

    # Create pyproject.toml if specified
    if pyproject_name:
        pyproject_path = repo_path / "pyproject.toml"
        pyproject_path.write_text(dedent(f"""
            [build-system]
            requires = ["setuptools", "wheel"]

            [project]
            name = "{pyproject_name}"
            version = "0.1.0"
        """).strip())
        _git_cmd(repo_path, "add", "pyproject.toml")

    # Create initial commit
    result = _git_cmd(repo_path, "commit", "-m", "init")
    if result.returncode != 0:
        # Maybe nothing to commit
        if b"nothing to commit" not in result.stderr:
            raise RuntimeError(f"git commit failed: {result.stderr}")
        # Create a dummy file
        (repo_path / "README.md").write_text("# Test repo")
        _git_cmd(repo_path, "add", "README.md")
        result = _git_cmd(repo_path, "commit", "-m", "init")
        if result.returncode != 0:
            raise RuntimeError(f"git commit failed: {result.stderr}")

    # Get the commit SHA
    rev_result = _git_cmd(repo_path, "rev-parse", "HEAD")
    if rev_result.returncode != 0:
        raise RuntimeError(f"git rev-parse failed: {rev_result.stderr}")
    return rev_result.stdout.strip()


class TestRepoNameFromUrl:
    def test_https_with_git_suffix(self):
        assert _repo_name_from_url("https://github.com/jojojen/telegram_core.git") == "telegram_core"

    def test_https_no_suffix(self):
        assert _repo_name_from_url("https://github.com/jojojen/price_monitor_bot") == "price_monitor_bot"

    def test_trailing_slash(self):
        assert _repo_name_from_url("https://github.com/jojojen/sns_monitor_bot/") == "sns_monitor_bot"


class TestValidatePackage:
    def test_all_clean(self, tmp_path):
        """Package exists, revision matches, clean worktree."""
        sibling_path = tmp_path / "telegram_core"
        rev = _init_git_repo(sibling_path, pyproject_name="telegram-core")

        manifest = {
            "repository": "https://github.com/jojojen/telegram_core.git",
            "revision": rev,
            "distribution": "telegram-core",
            "contract_version": 1,
        }

        report = validate_package("telegram-core", manifest, sibling_path)

        assert report.name == "telegram-core"
        assert report.is_ok()
        assert len(report.findings) == 0

    def test_missing_checkout(self, tmp_path):
        """Sibling directory does not exist."""
        sibling_path = tmp_path / "nonexistent"

        manifest = {
            "repository": "https://github.com/jojojen/telegram_core.git",
            "revision": "abc123",
            "distribution": "telegram-core",
            "contract_version": 1,
        }

        report = validate_package("telegram-core", manifest, sibling_path)

        assert not report.is_ok()
        assert len(report.findings) == 1
        assert report.findings[0].kind == "missing_checkout"
        assert report.findings[0].severity == "error"

    def test_sha_mismatch_warning(self, tmp_path):
        """Revision mismatch; non-strict mode."""
        sibling_path = tmp_path / "telegram_core"
        _init_git_repo(sibling_path, pyproject_name="telegram-core")

        manifest = {
            "repository": "https://github.com/jojojen/telegram_core.git",
            "revision": "0000000000000000000000000000000000000000",
            "distribution": "telegram-core",
            "contract_version": 1,
        }

        report = validate_package("telegram-core", manifest, sibling_path, strict=False)

        assert len(report.findings) == 1
        assert report.findings[0].kind == "sha_mismatch"
        assert report.findings[0].severity == "warning"
        assert report.is_ok()  # warnings don't fail

    def test_sha_mismatch_error_strict(self, tmp_path):
        """Revision mismatch; strict mode."""
        sibling_path = tmp_path / "telegram_core"
        _init_git_repo(sibling_path, pyproject_name="telegram-core")

        manifest = {
            "repository": "https://github.com/jojojen/telegram_core.git",
            "revision": "0000000000000000000000000000000000000000",
            "distribution": "telegram-core",
            "contract_version": 1,
        }

        report = validate_package("telegram-core", manifest, sibling_path, strict=True)

        assert not report.is_ok()
        assert report.findings[0].severity == "error"

    def test_dirty_worktree_warning(self, tmp_path):
        """Worktree has modifications; non-strict mode."""
        sibling_path = tmp_path / "telegram_core"
        _init_git_repo(sibling_path, pyproject_name="telegram-core")

        # Make a change
        (sibling_path / "new_file.py").write_text("# hello")

        manifest = {
            "repository": "https://github.com/jojojen/telegram_core.git",
            "revision": _git_cmd(sibling_path, "rev-parse", "HEAD").stdout.strip(),
            "distribution": "telegram-core",
            "contract_version": 1,
        }

        report = validate_package("telegram-core", manifest, sibling_path, strict=False)

        dirty_findings = [f for f in report.findings if f.kind == "dirty_worktree"]
        assert len(dirty_findings) == 1
        assert dirty_findings[0].severity == "warning"
        assert "1 changes" in dirty_findings[0].message or "dirty worktree" in dirty_findings[0].message

    def test_dirty_worktree_error_strict(self, tmp_path):
        """Worktree has modifications; strict mode."""
        sibling_path = tmp_path / "telegram_core"
        _init_git_repo(sibling_path, pyproject_name="telegram-core")

        # Make a change
        (sibling_path / "new_file.py").write_text("# hello")

        manifest = {
            "repository": "https://github.com/jojojen/telegram_core.git",
            "revision": _git_cmd(sibling_path, "rev-parse", "HEAD").stdout.strip(),
            "distribution": "telegram-core",
            "contract_version": 1,
        }

        report = validate_package("telegram-core", manifest, sibling_path, strict=True)

        dirty_findings = [f for f in report.findings if f.kind == "dirty_worktree"]
        assert len(dirty_findings) == 1
        assert dirty_findings[0].severity == "error"
        assert not report.is_ok()

    def test_metadata_mismatch(self, tmp_path):
        """pyproject.toml name doesn't match manifest distribution."""
        sibling_path = tmp_path / "telegram_core"
        _init_git_repo(sibling_path, pyproject_name="wrong-name")

        manifest = {
            "repository": "https://github.com/jojojen/telegram_core.git",
            "revision": _git_cmd(sibling_path, "rev-parse", "HEAD").stdout.strip(),
            "distribution": "telegram-core",
            "contract_version": 1,
        }

        report = validate_package("telegram-core", manifest, sibling_path)

        assert not report.is_ok()
        mismatch_findings = [f for f in report.findings if f.kind == "metadata_mismatch"]
        assert len(mismatch_findings) == 1
        assert mismatch_findings[0].severity == "error"

    def test_metadata_mismatch_dash_underscore_normalized(self, tmp_path):
        """Dash/underscore in package names should be normalized."""
        sibling_path = tmp_path / "telegram_core"
        _init_git_repo(sibling_path, pyproject_name="telegram_core")  # underscore

        manifest = {
            "repository": "https://github.com/jojojen/telegram_core.git",
            "revision": _git_cmd(sibling_path, "rev-parse", "HEAD").stdout.strip(),
            "distribution": "telegram-core",  # dash
            "contract_version": 1,
        }

        report = validate_package("telegram-core", manifest, sibling_path)

        # Should pass: dash/underscore are equivalent
        mismatch_findings = [f for f in report.findings if f.kind == "metadata_mismatch"]
        assert len(mismatch_findings) == 0

    def test_incompatible_contract_version(self, tmp_path):
        """Contract version mismatch."""
        sibling_path = tmp_path / "telegram_core"
        _init_git_repo(sibling_path, pyproject_name="telegram-core")

        manifest = {
            "repository": "https://github.com/jojojen/telegram_core.git",
            "revision": _git_cmd(sibling_path, "rev-parse", "HEAD").stdout.strip(),
            "distribution": "telegram-core",
            "contract_version": 2,  # unsupported
        }

        report = validate_package("telegram-core", manifest, sibling_path)

        assert not report.is_ok()
        contract_findings = [f for f in report.findings if f.kind == "incompatible_contract"]
        assert len(contract_findings) == 1
        assert contract_findings[0].severity == "error"


class TestValidateWorkspace:
    def test_all_clean(self, tmp_path):
        """All packages clean."""
        # Set up two sibling repos
        sibling1_path = tmp_path / "telegram_core"
        rev1 = _init_git_repo(sibling1_path, pyproject_name="telegram-core")

        sibling2_path = tmp_path / "price_monitor_bot"
        rev2 = _init_git_repo(sibling2_path, pyproject_name="price-monitor-bot")

        # Create lock file
        lock_file = tmp_path / "workspace-lock.toml"
        lock_file.write_text(dedent(f"""
            schema_version = 1
            generated_at = "2026-07-11T00:00:00+00:00"

            [packages.telegram-core]
            repository = "https://github.com/jojojen/telegram_core.git"
            revision = "{rev1}"
            distribution = "telegram-core"
            contract_version = 1

            [packages.price-monitor-bot]
            repository = "https://github.com/jojojen/price_monitor_bot.git"
            revision = "{rev2}"
            distribution = "price-monitor-bot"
            contract_version = 1
        """).strip())

        reports, global_findings = validate_workspace(
            lock_file=lock_file,
            workspace_root=tmp_path,
            strict=False,
        )

        assert len(reports) == 2
        assert all(r.is_ok() for r in reports)
        assert len(global_findings) == 0

    def test_duplicate_distribution(self, tmp_path):
        """Two packages claim the same distribution name."""
        sibling1 = tmp_path / "telegram_core"
        rev1 = _init_git_repo(sibling1, pyproject_name="telegram-core")

        sibling2 = tmp_path / "telegram_core_alt"
        rev2 = _init_git_repo(sibling2, pyproject_name="telegram-core")  # same name!

        lock_file = tmp_path / "workspace-lock.toml"
        lock_file.write_text(dedent(f"""
            schema_version = 1
            [packages.telegram-core]
            repository = "https://github.com/jojojen/telegram_core.git"
            revision = "{rev1}"
            distribution = "telegram-core"
            contract_version = 1

            [packages.telegram-core-alt]
            repository = "https://github.com/jojojen/telegram_core_alt.git"
            revision = "{rev2}"
            distribution = "telegram-core"
            contract_version = 1
        """).strip())

        reports, global_findings = validate_workspace(
            lock_file=lock_file,
            workspace_root=tmp_path,
        )

        dup_findings = [f for f in global_findings if f.kind == "duplicate_ownership"]
        assert len(dup_findings) == 1
        assert dup_findings[0].severity == "error"

    def test_duplicate_repository_url(self, tmp_path):
        """Two packages reference the same repository URL."""
        sibling1 = tmp_path / "telegram_core"
        rev1 = _init_git_repo(sibling1, pyproject_name="telegram-core")

        lock_file = tmp_path / "workspace-lock.toml"
        lock_file.write_text(dedent(f"""
            schema_version = 1
            [packages.telegram-core]
            repository = "https://github.com/jojojen/telegram_core.git"
            revision = "{rev1}"
            distribution = "telegram-core"
            contract_version = 1

            [packages.telegram-core-alias]
            repository = "https://github.com/jojojen/telegram_core.git"
            revision = "{rev1}"
            distribution = "telegram-core-alias"
            contract_version = 1
        """).strip())

        reports, global_findings = validate_workspace(
            lock_file=lock_file,
            workspace_root=tmp_path,
        )

        dup_findings = [f for f in global_findings if f.kind == "duplicate_ownership"]
        assert len(dup_findings) == 1
        assert dup_findings[0].severity == "error"


class TestFormatReport:
    def test_human_readable_all_ok(self):
        """Human-readable format with all packages OK."""
        reports = [
            PackageReport(name="pkg1", path="/path/to/pkg1", findings=[]),
            PackageReport(name="pkg2", path="/path/to/pkg2", findings=[]),
        ]
        output = format_report(reports, [], json_output=False)

        assert "pkg1: OK" in output
        assert "pkg2: OK" in output

    def test_human_readable_with_findings(self):
        """Human-readable format with findings."""
        reports = [
            PackageReport(
                name="pkg1",
                path="/path/to/pkg1",
                findings=[Finding(kind="sha_mismatch", message="test", severity="warning")],
            ),
        ]
        output = format_report(reports, [], json_output=False)

        assert "pkg1" in output
        assert "sha_mismatch" in output
        assert "warning" in output

    def test_json_output_valid(self):
        """JSON output is valid and contains expected structure."""
        reports = [
            PackageReport(
                name="pkg1",
                path="/path/to/pkg1",
                findings=[Finding(kind="sha_mismatch", message="test", severity="warning")],
            ),
        ]
        global_findings = [
            Finding(kind="duplicate_ownership", message="dup test", severity="error"),
        ]
        output = format_report(reports, global_findings, json_output=True)

        data = json.loads(output)
        assert "packages" in data
        assert "global_findings" in data
        assert len(data["packages"]) == 1
        assert len(data["global_findings"]) == 1
        assert data["packages"][0]["findings"][0]["kind"] == "sha_mismatch"
        assert data["global_findings"][0]["kind"] == "duplicate_ownership"


class TestCLI:
    def test_smoke_test_with_real_lock_file(self):
        """Smoke test: CLI can be invoked and doesn't crash."""
        import subprocess
        import sys

        result = subprocess.run(
            [sys.executable, "-m", "openclaw_adapter.workspace_validator", "--help"],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0
        assert "lock-file" in result.stdout

    def test_cli_exit_code_no_errors(self, tmp_path):
        """CLI exits with 0 when no errors."""
        sibling = tmp_path / "telegram_core"
        rev = _init_git_repo(sibling, pyproject_name="telegram-core")

        lock_file = tmp_path / "workspace-lock.toml"
        lock_file.write_text(dedent(f"""
            schema_version = 1
            [packages.telegram-core]
            repository = "https://github.com/jojojen/telegram_core.git"
            revision = "{rev}"
            distribution = "telegram-core"
            contract_version = 1
        """).strip())

        import subprocess
        import sys

        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "openclaw_adapter.workspace_validator",
                "--lock-file",
                str(lock_file),
                "--workspace-root",
                str(tmp_path),
            ],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0

    def test_cli_exit_code_with_errors(self, tmp_path):
        """CLI exits with 1 when errors present."""
        sibling = tmp_path / "telegram_core"
        rev = _init_git_repo(sibling, pyproject_name="telegram-core")

        lock_file = tmp_path / "workspace-lock.toml"
        lock_file.write_text(dedent(f"""
            schema_version = 1
            [packages.telegram-core]
            repository = "https://github.com/jojojen/telegram_core.git"
            revision = "0000000000000000000000000000000000000000"
            distribution = "telegram-core"
            contract_version = 1
        """).strip())

        import subprocess
        import sys

        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "openclaw_adapter.workspace_validator",
                "--lock-file",
                str(lock_file),
                "--workspace-root",
                str(tmp_path),
                "--strict",
            ],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 1

    def test_cli_malformed_toml(self, tmp_path):
        """CLI exits with 2 on malformed manifest."""
        lock_file = tmp_path / "workspace-lock.toml"
        lock_file.write_text("this is not [valid TOML")

        import subprocess
        import sys

        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "openclaw_adapter.workspace_validator",
                "--lock-file",
                str(lock_file),
                "--workspace-root",
                str(tmp_path),
            ],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 2
        assert "malformed" in result.stderr.lower() or "error" in result.stderr.lower()
