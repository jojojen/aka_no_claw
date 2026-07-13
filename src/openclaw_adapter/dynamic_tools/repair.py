"""Bounded repair-attempt accounting (R4.6)."""

from __future__ import annotations

from dataclasses import dataclass, field
from hashlib import sha256


@dataclass(slots=True)
class RepairBudget:
    limit: int = 3
    attempts: int = 0
    _seen: set[str] = field(default_factory=set)

    def accept(self, candidate: str) -> bool:
        """Accept a novel candidate within budget; stop repeated ineffective work."""
        digest = sha256(candidate.encode("utf-8")).hexdigest()
        if self.attempts >= self.limit or digest in self._seen:
            return False
        self._seen.add(digest)
        self.attempts += 1
        return True
