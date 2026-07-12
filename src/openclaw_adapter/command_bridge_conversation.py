"""Conversation-scoped persistence for the command bridge (P1 R1.4a, #74).

HTTP handlers keep response formatting in ``CommandBridge``.  This collaborator
owns only the short-lived console snapshot and the atomic read-modify-write used
when a streaming client disconnects after a worker has produced a result.
"""

from __future__ import annotations

import threading
from uuid import uuid4

from .session_memory import SessionMemoryStore


class ConversationSession:
    """Serialize access to the single persisted Web-console session."""

    def __init__(
        self, memory_dir: str | None, *, store: SessionMemoryStore | None = None
    ) -> None:
        self._memory_dir = memory_dir
        self._store = store
        self._lock = threading.RLock()

    def store(self) -> SessionMemoryStore:
        with self._lock:
            if self._store is None:
                if not self._memory_dir:
                    raise RuntimeError("conversation session memory directory is not configured")
                self._store = SessionMemoryStore(self._memory_dir)
            return self._store

    def adopt_store(self, store: SessionMemoryStore) -> None:
        """Adopt a bridge-injected store before first use.

        The bridge has long exposed this narrow private seam to deterministic
        tests and local callers.  Keeping it avoids changing persistence scope
        merely because the lock/ownership moved here.
        """
        with self._lock:
            if self._store is None:
                self._store = store

    def load(self) -> dict:
        with self._lock:
            return self.store().load()

    def save(self, snapshot: object) -> dict:
        with self._lock:
            return self.store().save(snapshot)

    def clear(self) -> None:
        with self._lock:
            self.store().clear()

    def append_orphaned_result(self, text: str) -> None:
        """Persist a disconnected stream's completed answer without racing a
        concurrent session read/write."""
        with self._lock:
            snapshot = self.store().load()
            messages = list(snapshot.get("messages") or [])
            messages.append({
                "id": uuid4().hex,
                "role": "assistant",
                "text": text,
                "status": "ok",
            })
            snapshot["messages"] = messages
            self.store().save(snapshot)
