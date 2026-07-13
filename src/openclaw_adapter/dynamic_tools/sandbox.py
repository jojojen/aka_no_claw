"""Terminal-state cleanup primitives for generated-tool execution (R4.5)."""

from __future__ import annotations

from contextlib import ExitStack
from typing import Callable


class TerminalCleanup:
    """Registers cleanup callbacks and runs each exactly once on every outcome."""

    def __init__(self) -> None:
        self._stack = ExitStack()
        self._closed = False

    def add(self, callback: Callable[[], object]) -> None:
        if self._closed:
            callback()
            return
        self._stack.callback(callback)

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self._stack.close()

    def __enter__(self) -> "TerminalCleanup":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
