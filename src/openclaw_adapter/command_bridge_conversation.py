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


class ConversationState:
    """Own in-process continuation state and its synchronization primitives.

    The bridge keeps compatibility aliases during R1.4 so existing callers that
    inspect a paused continuation keep working while ownership is centralized.
    """

    def __init__(self) -> None:
        self.music_continuations: dict[str, dict] = {}
        self.music_lock = threading.Lock()
        self.goal_continuations: dict[str, dict] = {}
        self.goal_lock = threading.Lock()
        self.goal_pending_confirms: dict[str, dict] = {}
        self.goal_pending_lock = threading.Lock()
        self.goal_completed_workflows: dict[str, object] = {}
        self.goal_completed_lock = threading.Lock()

    def clear(self, conversation_key: str) -> None:
        for values, lock in (
            (self.music_continuations, self.music_lock),
            (self.goal_continuations, self.goal_lock),
            (self.goal_pending_confirms, self.goal_pending_lock),
            (self.goal_completed_workflows, self.goal_completed_lock),
        ):
            with lock:
                values.pop(conversation_key, None)
