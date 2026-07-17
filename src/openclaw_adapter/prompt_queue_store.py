"""Atomic JSON persistence for the Web prompt queue (issue #86)."""

from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import threading
import time
from typing import Any
from uuid import uuid4

from .prompt_queue import (
    PromptQueueCapacityError,
    PromptQueueConflict,
    PromptQueueError,
    QueuedPrompt,
)


class PromptQueueStore:
    """One bounded, versioned queue per session.

    The file is a compact recovery snapshot. ``draining`` is persisted before a
    runner starts so two terminal observers cannot claim the same next turn.
    """

    _locks: dict[str, threading.RLock] = {}
    _locks_guard = threading.Lock()

    def __init__(self, root_dir: str | Path, *, max_entries: int = 20, ttl_seconds: int = 86400) -> None:
        self.root_dir = Path(root_dir)
        self.max_entries = max(1, int(max_entries))
        self.ttl_seconds = max(1, int(ttl_seconds))
        with self._locks_guard:
            self._lock = self._locks.setdefault(str(self.root_dir.resolve()), threading.RLock())

    def snapshot(self, session_id: str, *, now: float | None = None) -> dict[str, object]:
        with self._lock:
            state, changed = self._load(session_id, now=now)
            if changed:
                self._save(session_id, state)
            return self._public(state)

    def reconcile(self, session_id: str, *, now: float | None = None) -> dict[str, object]:
        """Recover abandoned draining entries after a bridge restart."""
        with self._lock:
            state, _changed = self._load(session_id, now=now, recover_draining=True)
            self._save(session_id, state)
            return self._public(state)

    def clear(self, session_id: str) -> None:
        with self._lock:
            path = self._path(session_id)
            if path.exists():
                path.unlink()

    def create(
        self,
        session_id: str,
        *,
        intent: str,
        request: dict[str, Any],
        capture_context: str | None = None,
        target_run_id: str | None = None,
        now: float | None = None,
    ) -> tuple[QueuedPrompt, dict[str, object]]:
        if intent not in {"next_turn", "interjection"}:
            raise PromptQueueError("intent must be next_turn or interjection")
        text = str(request.get("input") or "").strip()
        mode = str(request.get("mode") or "").strip()
        if not text or not mode:
            raise PromptQueueError("queued request requires mode and input")
        if capture_context:
            raise PromptQueueError("capture context cannot be queued; finish or cancel that editor first")
        now = time.time() if now is None else float(now)
        with self._lock:
            state, changed = self._load(session_id, now=now)
            live = [
                item for item in state["entries"]
                if item.status in {"queued", "draining", "interrupted"}
            ]
            if len(live) >= self.max_entries:
                raise PromptQueueCapacityError(f"queue limit is {self.max_entries}")
            position = max((item.position for item in live), default=-1) + 1
            prompt = QueuedPrompt(
                prompt_id=uuid4().hex, session_id=session_id, version=1, position=position,
                intent=intent, mode=mode, capture_context=None, text=text,
                request=dict(request), created_at=now, updated_at=now,
                expires_at=now + self.ttl_seconds, target_run_id=target_run_id,
            )
            state["entries"].append(prompt)
            self._save(session_id, state)
            return prompt, self._public(state)

    def edit(self, session_id: str, prompt_id: str, *, text: str, expected_version: int, now: float | None = None) -> dict[str, object]:
        text = str(text or "").strip()
        if not text:
            raise PromptQueueError("text is required")
        with self._lock:
            state, _changed = self._load(session_id, now=now)
            index, prompt = self._editable(state, prompt_id, expected_version)
            request = {**prompt.request, "input": text}
            state["entries"][index] = prompt.evolve(
                text=text, request=request, version=prompt.version + 1,
                updated_at=time.time() if now is None else float(now),
            )
            self._save(session_id, state)
            return self._public(state)

    def cancel(self, session_id: str, prompt_id: str, *, expected_version: int, now: float | None = None) -> dict[str, object]:
        with self._lock:
            state, _changed = self._load(session_id, now=now)
            index, prompt = self._mutable(
                state, prompt_id, expected_version, statuses={"queued", "interrupted"}
            )
            state["entries"][index] = prompt.evolve(
                status="cancelled", version=prompt.version + 1,
                updated_at=time.time() if now is None else float(now),
            )
            self._save(session_id, state)
            return self._public(state)

    def reorder(self, session_id: str, *, prompt_ids: list[str], expected_versions: dict[str, int], now: float | None = None) -> dict[str, object]:
        with self._lock:
            state, _changed = self._load(session_id, now=now)
            queued = sorted((item for item in state["entries"] if item.status == "queued" and item.intent == "next_turn"), key=lambda item: item.position)
            if len(prompt_ids) != len(set(prompt_ids)) or set(prompt_ids) != {item.prompt_id for item in queued}:
                raise PromptQueueConflict("reorder must include every queued next-turn prompt exactly once")
            by_id = {item.prompt_id: item for item in queued}
            for prompt_id in prompt_ids:
                if expected_versions.get(prompt_id) != by_id[prompt_id].version:
                    raise PromptQueueConflict("queue changed; reload before reordering")
            at = time.time() if now is None else float(now)
            replacements = {
                prompt_id: by_id[prompt_id].evolve(position=position, version=by_id[prompt_id].version + 1, updated_at=at)
                for position, prompt_id in enumerate(prompt_ids)
            }
            state["entries"] = [replacements.get(item.prompt_id, item) for item in state["entries"]]
            self._save(session_id, state)
            return self._public(state)

    def claim_next(self, session_id: str, *, now: float | None = None) -> tuple[QueuedPrompt | None, dict[str, object]]:
        with self._lock:
            state, _changed = self._load(session_id, now=now)
            if state.get("running_prompt_id"):
                return None, self._public(state)
            candidates = sorted(
                (item for item in state["entries"] if item.status == "queued" and item.intent == "next_turn"),
                key=lambda item: item.position,
            )
            if not candidates:
                self._save(session_id, state)
                return None, self._public(state)
            prompt = candidates[0]
            at = time.time() if now is None else float(now)
            claimed = prompt.evolve(status="draining", version=prompt.version + 1, updated_at=at)
            state["entries"] = [claimed if item.prompt_id == prompt.prompt_id else item for item in state["entries"]]
            state["running_prompt_id"] = claimed.prompt_id
            self._save(session_id, state)
            return claimed, self._public(state)

    def claim_interjection(self, session_id: str, run_id: str, *, now: float | None = None) -> tuple[QueuedPrompt | None, dict[str, object]]:
        with self._lock:
            state, _changed = self._load(session_id, now=now)
            candidates = sorted(
                (item for item in state["entries"] if item.status == "queued" and item.intent == "interjection" and item.target_run_id == run_id),
                key=lambda item: item.position,
            )
            if not candidates:
                self._save(session_id, state)
                return None, self._public(state)
            prompt = candidates[0]
            at = time.time() if now is None else float(now)
            claimed = prompt.evolve(status="draining", version=prompt.version + 1, updated_at=at)
            state["entries"] = [claimed if item.prompt_id == prompt.prompt_id else item for item in state["entries"]]
            self._save(session_id, state)
            return claimed, self._public(state)

    def fallback_interjections(self, session_id: str, run_id: str, *, now: float | None = None) -> dict[str, object]:
        """Keep missed goal-loop interjections as safe next-turn prompts."""
        with self._lock:
            state, _changed = self._load(session_id, now=now)
            at = time.time() if now is None else float(now)
            changed = False
            entries: list[QueuedPrompt] = []
            for item in state["entries"]:
                if item.status == "queued" and item.intent == "interjection" and item.target_run_id == run_id:
                    entries.append(item.evolve(
                        intent="next_turn", target_run_id=None,
                        version=item.version + 1, updated_at=at,
                    ))
                    changed = True
                else:
                    entries.append(item)
            state["entries"] = entries
            if changed:
                self._save(session_id, state)
            return self._public(state)

    def complete(self, session_id: str, prompt_id: str, *, run_id: str | None = None, now: float | None = None) -> dict[str, object]:
        with self._lock:
            state, _changed = self._load(session_id, now=now)
            at = time.time() if now is None else float(now)
            found = False
            entries: list[QueuedPrompt] = []
            for item in state["entries"]:
                if item.prompt_id != prompt_id:
                    entries.append(item)
                    continue
                found = True
                if item.status not in {"draining", "started"}:
                    raise PromptQueueConflict("prompt is not claimed")
                entries.append(item.evolve(
                    status="completed", version=item.version + 1, updated_at=at,
                    started_run_id=run_id or item.started_run_id,
                ))
            if not found:
                raise PromptQueueError("queued prompt not found")
            state["entries"] = entries
            if state.get("running_prompt_id") == prompt_id:
                state["running_prompt_id"] = None
            self._save(session_id, state)
            return self._public(state)

    def release(self, session_id: str, prompt_id: str, *, now: float | None = None) -> dict[str, object]:
        """Return a claimed next-turn prompt to the visible queue after a start failure."""
        with self._lock:
            state, _changed = self._load(session_id, now=now)
            at = time.time() if now is None else float(now)
            found = False
            entries: list[QueuedPrompt] = []
            for item in state["entries"]:
                if item.prompt_id != prompt_id:
                    entries.append(item)
                    continue
                found = True
                if item.status != "draining":
                    raise PromptQueueConflict("prompt is not claimed")
                entries.append(item.evolve(status="queued", version=item.version + 1, updated_at=at))
            if not found:
                raise PromptQueueError("queued prompt not found")
            state["entries"] = entries
            if state.get("running_prompt_id") == prompt_id:
                state["running_prompt_id"] = None
            self._save(session_id, state)
            return self._public(state)

    def interrupt(
        self, session_id: str, prompt_id: str, *, run_id: str | None = None,
        now: float | None = None,
    ) -> dict[str, object]:
        """Make an abandoned claim visible without automatically replaying it."""
        with self._lock:
            state, _changed = self._load(session_id, now=now)
            at = time.time() if now is None else float(now)
            found = False
            entries: list[QueuedPrompt] = []
            for item in state["entries"]:
                if item.prompt_id != prompt_id:
                    entries.append(item)
                    continue
                found = True
                if item.status != "draining":
                    raise PromptQueueConflict("prompt is not claimed")
                entries.append(item.evolve(
                    status="interrupted", version=item.version + 1, updated_at=at,
                    started_run_id=run_id or item.started_run_id,
                ))
            if not found:
                raise PromptQueueError("queued prompt not found")
            state["entries"] = entries
            if state.get("running_prompt_id") == prompt_id:
                state["running_prompt_id"] = None
            self._save(session_id, state)
            return self._public(state)

    def retry(
        self, session_id: str, prompt_id: str, *, expected_version: int,
        now: float | None = None,
    ) -> tuple[QueuedPrompt, dict[str, object]]:
        """Create a fresh identity for an explicitly retried interrupted prompt."""
        with self._lock:
            state, _changed = self._load(session_id, now=now)
            index, prompt = self._mutable(
                state, prompt_id, expected_version, statuses={"interrupted"}
            )
            at = time.time() if now is None else float(now)
            retried = prompt.evolve(
                prompt_id=uuid4().hex,
                status="queued",
                version=1,
                created_at=at,
                updated_at=at,
                expires_at=at + self.ttl_seconds,
                started_run_id=None,
            )
            state["entries"][index] = retried
            self._save(session_id, state)
            return retried, self._public(state)

    def _editable(self, state: dict[str, Any], prompt_id: str, expected_version: int) -> tuple[int, QueuedPrompt]:
        return self._mutable(state, prompt_id, expected_version, statuses={"queued"})

    @staticmethod
    def _mutable(
        state: dict[str, Any], prompt_id: str, expected_version: int, *, statuses: set[str]
    ) -> tuple[int, QueuedPrompt]:
        for index, prompt in enumerate(state["entries"]):
            if prompt.prompt_id != prompt_id:
                continue
            if prompt.status not in statuses:
                allowed = " or ".join(sorted(statuses))
                raise PromptQueueConflict(f"only {allowed} prompts may be changed")
            if prompt.version != int(expected_version):
                raise PromptQueueConflict("queue changed; reload before editing")
            return index, prompt
        raise PromptQueueError("queued prompt not found")

    def _path(self, session_id: str) -> Path:
        safe = "".join(char if char.isalnum() or char in {"-", "_"} else "_" for char in session_id)
        return self.root_dir / f"{safe or 'web-default'}.json"

    def _load(self, session_id: str, *, now: float | None = None, recover_draining: bool = False) -> tuple[dict[str, Any], bool]:
        path = self._path(session_id)
        if not path.exists():
            return {"session_id": session_id, "running_prompt_id": None, "entries": []}, False
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            entries = [QueuedPrompt.from_dict(dict(item)) for item in raw.get("entries", [])]
        except (OSError, ValueError, TypeError, KeyError) as exc:
            raise PromptQueueError(f"unable to load prompt queue: {exc}") from exc
        state: dict[str, Any] = {
            "session_id": session_id,
            "running_prompt_id": raw.get("running_prompt_id"),
            "entries": entries,
        }
        changed = self._expire(state, now=time.time() if now is None else float(now))
        if recover_draining:
            at = time.time() if now is None else float(now)
            recovered = []
            for item in state["entries"]:
                if item.status == "draining":
                    recovered.append(item.evolve(status="queued", version=item.version + 1, updated_at=at))
                    changed = True
                else:
                    recovered.append(item)
            state["entries"] = recovered
            if state.get("running_prompt_id"):
                state["running_prompt_id"] = None
                changed = True
        return state, changed

    @staticmethod
    def _expire(state: dict[str, Any], *, now: float) -> bool:
        changed = False
        entries = []
        for item in state["entries"]:
            if item.status in {"queued", "interrupted"} and item.expires_at <= now:
                entries.append(item.evolve(status="expired", version=item.version + 1, updated_at=now))
                changed = True
            else:
                entries.append(item)
        state["entries"] = entries
        return changed

    def _save(self, session_id: str, state: dict[str, Any]) -> None:
        self.root_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": 1,
            "session_id": session_id,
            "running_prompt_id": state.get("running_prompt_id"),
            "entries": [item.to_dict() for item in state["entries"]],
        }
        fd, temporary = tempfile.mkstemp(prefix=".prompt-queue-", dir=self.root_dir)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self._path(session_id))
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    @staticmethod
    def _public(state: dict[str, Any]) -> dict[str, object]:
        entries = sorted(
            (
                item for item in state["entries"]
                if item.status in {"queued", "draining", "interrupted"}
            ),
            key=lambda item: (item.position, item.prompt_id),
        )
        return {
            "session_id": state["session_id"],
            "running_prompt_id": state.get("running_prompt_id"),
            "entries": [item.public() for item in entries],
        }
