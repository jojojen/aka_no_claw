"""Deterministic, closed-vocabulary risk classification for frozen actions."""

from __future__ import annotations

import ast
from dataclasses import dataclass
from enum import StrEnum
from urllib.parse import urlsplit


class RiskLevel(StrEnum):
    READ_ONLY = "read_only"
    REVERSIBLE = "reversible"
    PERSISTENT_WRITE = "persistent_write"
    SCHEDULED = "scheduled"
    DESTRUCTIVE = "destructive"
    PRIVILEGED = "privileged"


class PolicyOutcome(StrEnum):
    AUTO_ALLOW = "auto_allow"
    ASK = "ask"
    DENY = "deny"


@dataclass(frozen=True, slots=True)
class EffectProfile:
    risk: RiskLevel
    capabilities: tuple[str, ...]
    network_scopes: tuple[str, ...] = ()
    filesystem_scopes: tuple[str, ...] = ()
    device_scopes: tuple[str, ...] = ()


_PRIVILEGED_NAMES = frozenset({"eval", "exec", "compile", "__import__"})
_PRIVILEGED_MODULES = frozenset({"subprocess", "ctypes", "pty", "socket"})
_NETWORK_MODULES = frozenset({"urllib", "requests", "http", "aiohttp", "smtplib", "ftplib", "imaplib"})
_NETWORK_WRITE_METHODS = frozenset({
    "post", "put", "patch", "request", "sendmail", "send_message", "storbinary",
    "storlines", "store",
})
_NETWORK_DELETE_METHODS = frozenset({"delete", "dele"})
_DESTRUCTIVE_METHODS = frozenset({
    "unlink", "rmdir", "remove", "removedirs", "rename", "replace", "rmtree",
})
_WRITE_METHODS = frozenset({
    "write", "write_text", "write_bytes", "mkdir", "makedirs", "dump", "save",
    "touch", "chmod", "chown", "symlink_to", "hardlink_to", "truncate", "execute",
    "executemany", "executescript", "commit", "copy", "copy2", "copyfile", "copytree",
    "move", "extract", "extractall", "make_archive", "unpack_archive",
})


def _root_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return _root_name(node.value)
    return None


def classify_generated_tool(code: str) -> EffectProfile:
    """Classify parsed source without trusting a model-supplied label.

    This is deliberately conservative: a syntax or analysis failure is privileged,
    so an approval can never turn an unreadable artifact into an allowed action.
    """
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return EffectProfile(RiskLevel.PRIVILEGED, ("invalid_artifact",))

    imports: set[str] = set()
    calls: set[str] = set()
    network_scopes: set[str] = set()
    network_write = False
    network_delete = False
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(alias.name.split(".", 1)[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.add(node.module.split(".", 1)[0])
        elif isinstance(node, ast.Call):
            function_name: str | None = None
            if isinstance(node.func, ast.Name):
                function_name = node.func.id
                calls.add(function_name)
            elif isinstance(node.func, ast.Attribute):
                function_name = node.func.attr
                calls.add(function_name)
                root = _root_name(node.func)
                if root:
                    calls.add(f"{root}.{node.func.attr}")
                if node.func.attr in _NETWORK_WRITE_METHODS:
                    network_write = True
                elif node.func.attr in _NETWORK_DELETE_METHODS:
                    network_delete = True
            for value in (*node.args, *(keyword.value for keyword in node.keywords)):
                if isinstance(value, ast.Constant) and isinstance(value.value, str):
                    parsed = urlsplit(value.value)
                    if parsed.scheme in {"http", "https"} and parsed.hostname:
                        network_scopes.add(parsed.hostname.lower())
            if function_name == "urlopen" and any(
                keyword.arg == "data" for keyword in node.keywords
            ):
                network_write = True
            if function_name == "Request":
                method = next((kw.value for kw in node.keywords if kw.arg == "method"), None)
                data = next((kw.value for kw in node.keywords if kw.arg == "data"), None)
                if isinstance(method, ast.Constant) and isinstance(method.value, str):
                    verb = method.value.upper()
                    network_delete = network_delete or verb == "DELETE"
                    network_write = network_write or verb not in {"GET", "HEAD", "OPTIONS", "DELETE"}
                elif data is not None:
                    network_write = True

    if imports & _PRIVILEGED_MODULES or calls & _PRIVILEGED_NAMES or any(
        call in {"os.system", "os.popen", "sys.exit"} for call in calls
    ):
        return EffectProfile(RiskLevel.PRIVILEGED, ("privileged_runtime",))
    capabilities: set[str] = set()
    risks: set[RiskLevel] = set()
    filesystem_scopes: tuple[str, ...] = ()
    concrete_network_scopes = tuple(sorted(network_scopes)) or ("outbound_http",)
    if network_delete:
        capabilities.add("network_delete")
        risks.add(RiskLevel.DESTRUCTIVE)
    if calls & _DESTRUCTIVE_METHODS:
        capabilities.add("filesystem_delete")
        risks.add(RiskLevel.DESTRUCTIVE)
        filesystem_scopes = ("tool_workspace",)
    if calls & _WRITE_METHODS or "open" in calls:
        capabilities.add("filesystem_write")
        risks.add(RiskLevel.PERSISTENT_WRITE)
        filesystem_scopes = ("tool_workspace",)
    if network_write:
        capabilities.add("network_write")
        risks.add(RiskLevel.PERSISTENT_WRITE)
    network_access = bool(
        imports & _NETWORK_MODULES
        or any(call.endswith(".urlopen") or call.endswith(".get") for call in calls)
    )
    if network_access and not (network_write or network_delete):
        capabilities.add("network_read")
        risks.add(RiskLevel.READ_ONLY)
    if not capabilities:
        capabilities.add("local_compute")
        risks.add(RiskLevel.READ_ONLY)
    risk = RiskLevel.DESTRUCTIVE if RiskLevel.DESTRUCTIVE in risks else (
        RiskLevel.PERSISTENT_WRITE if RiskLevel.PERSISTENT_WRITE in risks else RiskLevel.READ_ONLY
    )
    return EffectProfile(
        risk,
        tuple(sorted(capabilities)),
        network_scopes=concrete_network_scopes if network_access else (),
        filesystem_scopes=filesystem_scopes,
    )


def include_dependency_install(
    profile: EffectProfile, dependencies: tuple[str, ...]
) -> EffectProfile:
    """Include the pip side effects required before generated-tool execution."""
    if not dependencies:
        return profile
    risk = profile.risk
    if risk in {RiskLevel.READ_ONLY, RiskLevel.REVERSIBLE}:
        risk = RiskLevel.PERSISTENT_WRITE
    return EffectProfile(
        risk=risk,
        capabilities=tuple(sorted({*profile.capabilities, "dependency_install"})),
        network_scopes=tuple(sorted({*profile.network_scopes, "python_package_index"})),
        filesystem_scopes=tuple(
            sorted({*profile.filesystem_scopes, "generated_tool_virtualenv"})
        ),
        device_scopes=profile.device_scopes,
    )


def decide_policy(profile: EffectProfile, policy: str) -> PolicyOutcome:
    """Return the fixed policy decision; unknown policy configuration denies."""
    if policy != "ask_generated_writes":
        return PolicyOutcome.DENY
    if profile.risk is RiskLevel.PRIVILEGED:
        return PolicyOutcome.DENY
    if profile.risk in {RiskLevel.PERSISTENT_WRITE, RiskLevel.SCHEDULED, RiskLevel.DESTRUCTIVE}:
        return PolicyOutcome.ASK
    return PolicyOutcome.AUTO_ALLOW
