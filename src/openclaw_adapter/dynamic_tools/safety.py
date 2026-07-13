"""Generator-independent static policy for generated tools (R4.4)."""

from __future__ import annotations

import ast
import logging
import re

logger = logging.getLogger(__name__)

_AUTO_IMPORT_STDLIB = frozenset({
    "os", "sys", "json", "re", "math", "datetime", "time", "random",
    "statistics", "decimal", "collections", "itertools", "functools",
    "csv", "html", "base64", "hashlib", "textwrap", "urllib",
})


def syntax_error(code: str) -> str:
    try:
        ast.parse(code)
    except SyntaxError as exc:
        return f"{exc.msg} (line {exc.lineno if exc.lineno is not None else '?'})"
    return ""


def ensure_stdlib_imports(code: str) -> str:
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return code
    bound: set[str] = set()
    used_roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            bound.update((alias.asname or alias.name).split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            bound.update(alias.asname or alias.name for alias in node.names)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            bound.add(node.name)
        elif isinstance(node, ast.arg):
            bound.add(node.arg)
        elif isinstance(node, ast.Name):
            (bound if isinstance(node.ctx, ast.Store) else used_roots).add(node.id)
    missing = sorted(used_roots & _AUTO_IMPORT_STDLIB - bound)
    if not missing:
        return code
    logger.info("dynamic_tools: auto-adding missing stdlib imports=%s", missing)
    submodules = {"urllib": "import urllib.request, urllib.parse, urllib.error"}
    return "".join(submodules.get(name, f"import {name}") + "\n" for name in missing) + code


def is_safe_package(name: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._\-\[\]=<>]*", name))


def is_approved_package(name: str, approved_packages: frozenset[str]) -> bool:
    canon = re.split(r"[><=!\[]", name)[0].lower().strip()
    base = canon.replace("-", "_").replace(".", "_")
    normalized = {item.lower().replace("-", "_").replace(".", "_") for item in approved_packages}
    return canon in approved_packages or base in normalized


def sandbox_wrapper_failed(stderr: str) -> bool:
    low = (stderr or "").lower()
    return "sandbox-exec" in low and any(marker in low for marker in ("sandbox_apply", "operation not permitted", "profile"))
