"""Versioned generated-tool catalog lifecycle operations (R4.8)."""

from __future__ import annotations

import logging
from typing import Protocol

logger = logging.getLogger(__name__)


class ToolCatalog(Protocol):
    def get(self, slug: str): ...

    def record_reuse_success(self, slug: str) -> None: ...

    def record_failure(self, slug: str, reason: str | None) -> None: ...


def tool_type(catalog: ToolCatalog, slug: str | None) -> str | None:
    """Return catalog metadata without allowing bookkeeping to break `/new`."""
    if not slug:
        return None
    try:
        entry = catalog.get(slug)
    except Exception:  # noqa: BLE001 - catalog is best-effort output metadata
        return None
    return entry.tool_type if entry else None


def record_outcome(catalog: ToolCatalog, slug: str | None, ok: bool, reason: str | None) -> None:
    """Persist reuse lifecycle metrics without affecting tool execution."""
    if not slug:
        return
    try:
        if ok:
            catalog.record_reuse_success(slug)
        else:
            catalog.record_failure(slug, reason)
    except Exception:  # noqa: BLE001 - catalog metrics must be non-fatal
        logger.debug("dynamic_tools: catalog metric update failed slug=%s", slug, exc_info=True)
