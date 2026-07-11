"""
Read-only workspace validator for sibling revision compatibility.

Loads config/workspace-lock.toml and validates sibling checkouts against
the manifest (no mutations, inspection only).
"""

import argparse
import json
import subprocess
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Dict, List


SUPPORTED_CONTRACT_VERSION = 1


@dataclass
class Finding:
    """A single validation finding."""
    kind: str  # missing_checkout, sha_mismatch, dirty_worktree, metadata_mismatch, incompatible_contract, duplicate_ownership
    message: str
    severity: str = "error"  # error or warning


@dataclass
class PackageReport:
    """Validation report for a single package."""
    name: str
    path: str
    findings: List[Finding] = field(default_factory=list)

    def is_ok(self) -> bool:
        """True if no error-level findings."""
        return not any(f.severity == "error" for f in self.findings)


def _repo_name_from_url(url: str) -> str:
    """Extract repo name from a GitHub URL.

    Example: https://github.com/jojojen/telegram_core.git -> telegram_core
    """
    path = url.rstrip("/")
    if path.endswith(".git"):
        path = path[:-4]
    return path.split("/")[-1]


def _load_lock_file(lock_path: Path) -> dict:
    """Load and parse workspace-lock.toml.

    Raises: FileNotFoundError, tomllib.TOMLDecodeError
    """
    with open(lock_path, "rb") as f:
        return tomllib.load(f)


def _git_rev_parse(repo_path: Path) -> Optional[str]:
    """Get HEAD revision of a git repo, or None if not a git repo."""
    try:
        result = subprocess.run(
            ["git", "-C", str(repo_path), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass
    return None


def _git_status_porcelain(repo_path: Path) -> Optional[str]:
    """Get 'git status --porcelain' output, or None if not a git repo."""
    try:
        result = subprocess.run(
            ["git", "-C", str(repo_path), "status", "--porcelain"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            return result.stdout
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass
    return None


def _load_pyproject_name(repo_path: Path) -> Optional[str]:
    """Load [project] name from sibling pyproject.toml, or None if missing/unparseable."""
    pyproject_path = repo_path / "pyproject.toml"
    if not pyproject_path.exists():
        return None
    try:
        with open(pyproject_path, "rb") as f:
            data = tomllib.load(f)
        return data.get("project", {}).get("name")
    except (tomllib.TOMLDecodeError, OSError):
        return None


def validate_package(
    name: str,
    manifest: dict,
    sibling_path: Path,
    strict: bool = False,
) -> PackageReport:
    """Validate a single package against its manifest entry and sibling checkout.

    Args:
        name: Package name (key in [packages.<name>]).
        manifest: Parsed manifest dict for this package.
        sibling_path: Local path to the sibling directory.
        strict: If True, sha_mismatch/dirty_worktree are errors; else warnings.

    Returns: PackageReport with findings.
    """
    findings: List[Finding] = []

    # Check: git repo exists
    rev = _git_rev_parse(sibling_path)
    if rev is None:
        findings.append(Finding(
            kind="missing_checkout",
            message=f"sibling not a git repo or directory absent: {sibling_path}",
            severity="error",
        ))
        return PackageReport(name=name, path=str(sibling_path), findings=findings)

    # Check: revision mismatch
    expected_rev = manifest.get("revision")
    if expected_rev and rev != expected_rev:
        findings.append(Finding(
            kind="sha_mismatch",
            message=f"HEAD {rev[:8]} != manifest {expected_rev[:8]}",
            severity="error" if strict else "warning",
        ))

    # Check: dirty worktree
    status = _git_status_porcelain(sibling_path)
    if status is not None and status.strip():
        dirty_count = len(status.strip().split("\n"))
        findings.append(Finding(
            kind="dirty_worktree",
            message=f"dirty worktree ({dirty_count} changes)",
            severity="error" if strict else "warning",
        ))

    # Check: metadata mismatch
    expected_dist = manifest.get("distribution")
    if expected_dist:
        actual_name = _load_pyproject_name(sibling_path)
        # Normalize: distutils/setuptools accept both - and _ in package names
        expected_normalized = expected_dist.replace("-", "_")
        actual_normalized = (actual_name or "").replace("-", "_")
        if actual_normalized != expected_normalized:
            findings.append(Finding(
                kind="metadata_mismatch",
                message=f"pyproject.toml [project] name {actual_name!r} != manifest distribution {expected_dist!r}",
                severity="error",
            ))

    # Check: contract version
    contract_ver = manifest.get("contract_version")
    if contract_ver != SUPPORTED_CONTRACT_VERSION:
        findings.append(Finding(
            kind="incompatible_contract",
            message=f"contract_version {contract_ver} != supported {SUPPORTED_CONTRACT_VERSION}",
            severity="error",
        ))

    return PackageReport(name=name, path=str(sibling_path), findings=findings)


def validate_workspace(
    lock_file: Path,
    workspace_root: Path,
    strict: bool = False,
) -> tuple[List[PackageReport], List[Finding]]:
    """Validate all packages in the workspace lock file.

    Args:
        lock_file: Path to workspace-lock.toml.
        workspace_root: Parent directory of sibling checkouts.
        strict: If True, treat sha_mismatch/dirty_worktree as errors.

    Returns: (list of PackageReport, list of global findings like duplicates).
    """
    lock_data = _load_lock_file(lock_file)
    packages = lock_data.get("packages", {})

    reports = []
    global_findings: List[Finding] = []

    # Check for duplicates across manifest
    seen_dist: Dict[str, str] = {}  # distribution -> package name
    seen_url: Dict[str, str] = {}   # repository URL -> package name

    for pkg_name, pkg_manifest in packages.items():
        dist = pkg_manifest.get("distribution")
        url = pkg_manifest.get("repository")

        if dist:
            if dist in seen_dist and seen_dist[dist] != pkg_name:
                global_findings.append(Finding(
                    kind="duplicate_ownership",
                    message=f"distribution '{dist}' in both {seen_dist[dist]!r} and {pkg_name!r}",
                    severity="error",
                ))
            seen_dist[dist] = pkg_name

        if url:
            if url in seen_url and seen_url[url] != pkg_name:
                global_findings.append(Finding(
                    kind="duplicate_ownership",
                    message=f"repository URL {url} in both {seen_url[url]!r} and {pkg_name!r}",
                    severity="error",
                ))
            seen_url[url] = pkg_name

    # Validate each package
    for pkg_name, pkg_manifest in packages.items():
        repo_url = pkg_manifest.get("repository", "")
        sibling_name = _repo_name_from_url(repo_url)
        sibling_path = workspace_root / sibling_name

        report = validate_package(pkg_name, pkg_manifest, sibling_path, strict=strict)
        reports.append(report)

    return reports, global_findings


def format_report(
    reports: List[PackageReport],
    global_findings: List[Finding],
    json_output: bool = False,
) -> str:
    """Format validation results for output.

    Args:
        reports: Per-package reports.
        global_findings: Global findings (duplicates, etc.).
        json_output: If True, output JSON; else human-readable.

    Returns: Formatted output string.
    """
    if json_output:
        data = {
            "packages": [
                {
                    "name": r.name,
                    "path": r.path,
                    "findings": [
                        {"kind": f.kind, "message": f.message, "severity": f.severity}
                        for f in r.findings
                    ],
                }
                for r in reports
            ],
            "global_findings": [
                {"kind": f.kind, "message": f.message, "severity": f.severity}
                for f in global_findings
            ],
        }
        return json.dumps(data, indent=2)

    lines = []
    for report in reports:
        if report.findings:
            for finding in report.findings:
                lines.append(f"  {report.name}: [{finding.severity}] {finding.kind}: {finding.message}")
        else:
            lines.append(f"  {report.name}: OK")

    if global_findings:
        lines.append("")
        for finding in global_findings:
            lines.append(f"  [global] [{finding.severity}] {finding.kind}: {finding.message}")

    if lines:
        return "workspace validation:\n" + "\n".join(lines)
    return "workspace validation: all packages OK"


def main():
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        description="Validate sibling worktree revisions against workspace-lock.toml",
    )
    parser.add_argument(
        "--lock-file",
        type=Path,
        help="Path to workspace-lock.toml (default: <repo_root>/config/workspace-lock.toml)",
    )
    parser.add_argument(
        "--workspace-root",
        type=Path,
        help="Parent directory of sibling checkouts (default: <repo_root>/../)",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Treat sha_mismatch and dirty_worktree as errors (CI/deploy mode)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output JSON instead of human-readable format",
    )

    args = parser.parse_args()

    # Resolve paths
    module_path = Path(__file__).resolve()
    repo_root = module_path.parents[2]

    lock_file = args.lock_file or (repo_root / "config" / "workspace-lock.toml")
    workspace_root = args.workspace_root or (repo_root.parent)

    try:
        reports, global_findings = validate_workspace(
            lock_file=lock_file,
            workspace_root=workspace_root,
            strict=args.strict,
        )
    except FileNotFoundError as e:
        print(f"error: manifest not found: {e}", file=sys.stderr)
        return 2
    except tomllib.TOMLDecodeError as e:
        print(f"error: malformed TOML: {e}", file=sys.stderr)
        return 2
    except Exception as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    # Format and output
    output = format_report(reports, global_findings, json_output=args.json)
    print(output)

    # Determine exit code
    has_errors = (
        any(not r.is_ok() for r in reports) or
        any(f.severity == "error" for f in global_findings)
    )

    return 1 if has_errors else 0


if __name__ == "__main__":
    sys.exit(main())
