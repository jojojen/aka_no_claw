"""Bounded RAG context accounting (R4.2b)."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class ContextBudget:
    """Mutable per-request budget that never grants more evidence than allowed."""

    limit: int
    used: int = 0

    def grant(self, requested: int) -> int:
        grant = max(0, min(int(requested), max(0, self.limit - self.used)))
        self.used += grant
        return grant

    @property
    def exhausted(self) -> bool:
        return self.used >= self.limit


def bounded_block(parts: list[str], *, max_chars: int) -> str:
    """Join prompt evidence deterministically without exceeding a character cap."""
    accepted: list[str] = []
    used = 0
    for part in parts:
        text = (part or "").strip()
        if not text:
            continue
        separator = 2 if accepted else 0
        remaining = max_chars - used - separator
        if remaining <= 0:
            break
        accepted.append(text[:remaining])
        used += separator + len(accepted[-1])
    return "\n\n".join(accepted)
