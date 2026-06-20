"""Minimal YAML loader for SYSTEM_MANIFEST.yaml (issue #7 checkers).

Dependency-free so the docs-health checks run in CI and locally without
PyYAML. Supports exactly the subset this repo's manifest uses:

  - nested mappings (2-space indentation)
  - sequences of scalars (``- item``)
  - scalar values (optionally quoted)
  - folded/literal block scalars (``key: >-`` / ``|``) — joined to one string

If PyYAML happens to be installed it is used instead (more robust).
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

_MAP_ITEM_RE = re.compile(r"^[A-Za-z_][\w.-]*:(\s|$)")

try:  # prefer the real parser when available
    import yaml as _pyyaml
except Exception:  # pragma: no cover - exercised in minimal environments
    _pyyaml = None


def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def _strip_lines(text: str) -> list[tuple[int, str]]:
    out: list[tuple[int, str]] = []
    for raw in text.splitlines():
        if not raw.strip():
            continue
        stripped = raw.lstrip(" ")
        if stripped.startswith("#"):
            continue
        indent = len(raw) - len(stripped)
        out.append((indent, stripped.rstrip()))
    return out


def _parse(lines: list[tuple[int, str]], idx: int, indent: int) -> tuple[Any, int]:
    if idx >= len(lines):
        return {}, idx
    first_indent, first_content = lines[idx]
    is_list = first_content.startswith("- ")
    container: Any = [] if is_list else {}
    while idx < len(lines):
        ind, content = lines[idx]
        if ind < indent:
            break
        if content.startswith("- "):
            inner = content[2:]
            if _MAP_ITEM_RE.match(inner):
                # sequence-of-mappings element: rewrite this line as a plain
                # map key at the element indent, then parse it as a map.
                item_indent = ind + 2
                lines[idx] = (item_indent, inner)
                item, idx = _parse(lines, idx, item_indent)
                container.append(item)
            else:
                container.append(_unquote(inner))
                idx += 1
            continue
        key, sep, rest = content.partition(":")
        key = key.strip()
        rest = rest.strip()
        if rest in (">-", ">", "|", "|-", "|+", ">+"):
            idx += 1
            buff: list[str] = []
            while idx < len(lines) and lines[idx][0] > ind:
                buff.append(lines[idx][1])
                idx += 1
            container[key] = " ".join(buff)
        elif rest == "":
            # nested block (map or list) at deeper indent, or empty value
            if idx + 1 < len(lines) and lines[idx + 1][0] > ind:
                child, idx = _parse(lines, idx + 1, lines[idx + 1][0])
                container[key] = child
            else:
                container[key] = None
                idx += 1
        else:
            container[key] = _unquote(rest)
            idx += 1
    return container, idx


def loads(text: str) -> Any:
    if _pyyaml is not None:
        return _pyyaml.safe_load(text)
    lines = _strip_lines(text)
    if not lines:
        return {}
    data, _ = _parse(lines, 0, lines[0][0])
    return data


def load_manifest(path: str | Path) -> Any:
    return loads(Path(path).read_text(encoding="utf-8"))
