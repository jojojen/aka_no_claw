"""Deterministic, closed-vocabulary risk classification for frozen actions."""

from __future__ import annotations

import ast
from dataclasses import dataclass
from enum import StrEnum


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
_NETWORK_MODULES = frozenset({"urllib", "requests", "http", "aiohttp"})
_DESTRUCTIVE_METHODS = frozenset({"unlink", "rmdir", "remove", "removedirs", "rename", "replace"})
_WRITE_METHODS = frozenset({"write", "write_text", "write_bytes", "mkdir", "makedirs", "dump"})


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
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(alias.name.split(".", 1)[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.add(node.module.split(".", 1)[0])
        elif isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name):
                calls.add(node.func.id)
            elif isinstance(node.func, ast.Attribute):
                calls.add(node.func.attr)
                root = _root_name(node.func)
                if root:
                    calls.add(f"{root}.{node.func.attr}")

    if imports & _PRIVILEGED_MODULES or calls & _PRIVILEGED_NAMES or any(
        call in {"os.system", "os.popen", "sys.exit"} for call in calls
    ):
        return EffectProfile(RiskLevel.PRIVILEGED, ("privileged_runtime",))
    if calls & _DESTRUCTIVE_METHODS:
        return EffectProfile(RiskLevel.DESTRUCTIVE, ("filesystem_delete",), filesystem_scopes=("local_workspace",))
    if calls & _WRITE_METHODS or "open" in calls:
        return EffectProfile(RiskLevel.PERSISTENT_WRITE, ("filesystem_write",), filesystem_scopes=("local_workspace",))
    if imports & _NETWORK_MODULES or any(call.endswith(".urlopen") or call.endswith(".get") for call in calls):
        return EffectProfile(RiskLevel.READ_ONLY, ("network_read",), network_scopes=("outbound_http",))
    return EffectProfile(RiskLevel.READ_ONLY, ("local_compute",))


def decide_policy(profile: EffectProfile, policy: str) -> PolicyOutcome:
    """Return the fixed policy decision; unknown policy configuration denies."""
    if policy != "ask_generated_writes":
        return PolicyOutcome.DENY
    if profile.risk is RiskLevel.PRIVILEGED:
        return PolicyOutcome.DENY
    if profile.risk in {RiskLevel.PERSISTENT_WRITE, RiskLevel.SCHEDULED, RiskLevel.DESTRUCTIVE}:
        return PolicyOutcome.ASK
    return PolicyOutcome.AUTO_ALLOW
