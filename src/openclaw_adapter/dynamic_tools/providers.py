"""Provider contracts and deterministic failure doubles (R4.3)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


class TextGenerationProvider(Protocol):
    model: str
    timeout_seconds: int
    num_ctx: int | None
    num_predict: int | None

    def generate(self, prompt: str, *, temperature: float = 0.0, think: bool = False) -> str: ...


class ProviderUnavailable(RuntimeError):
    """A provider could not supply a complete response for this request."""


@dataclass
class DeterministicFailureProvider:
    """Offline test double that makes provider failure explicit and repeatable."""

    reason: str = "provider unavailable"
    model: str = "deterministic-failure"
    timeout_seconds: int = 1
    num_ctx: int | None = None
    num_predict: int | None = None

    def generate(self, prompt: str, *, temperature: float = 0.0, think: bool = False) -> str:
        raise ProviderUnavailable(self.reason)


def is_truncation_error(message: str, markers: tuple[str, ...]) -> bool:
    return any(marker in (message or "").lower() for marker in markers)
