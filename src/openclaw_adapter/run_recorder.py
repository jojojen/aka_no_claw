"""Narrow lifecycle facade used by command-bridge request paths."""

from __future__ import annotations

import time
from uuid import uuid4

from .session_event_journal import SessionEventJournal


class RunRecorder:
    """Own one run's durable transitions and terminal monotonicity."""

    def __init__(self, journal: SessionEventJournal, *, run_id: str | None = None) -> None:
        self.journal = journal
        self.run_id = run_id or uuid4().hex
        self._terminal = False
        self._planner_recorded = False
        self._last_progress: dict[str, float] = {}

    def accepted(self, text: str, *, source_prompt_id: str | None = None) -> None:
        if text:
            self.emit("user.message", {"text": text})
        self.emit("run.accepted", {"source_prompt_id": source_prompt_id} if source_prompt_id else {})

    def started(self) -> None:
        self.emit("run.started", {})

    def planner_completed(self, route: str) -> None:
        if self._planner_recorded:
            return
        self.emit("planner.completed", {"route": route})
        self._planner_recorded = True

    def tool_started(self, tool: str) -> None:
        self.emit("tool.started", {"tool": tool})

    def tool_completed(self, tool: str, *, ok: bool) -> None:
        self.emit("tool.completed", {"tool": tool, "ok": ok})

    def progress(self, stage: str, label: str) -> None:
        now = time.monotonic()
        if now - self._last_progress.get(stage, 0.0) < 0.5:
            return
        self._last_progress[stage] = now
        self.emit("tool.progress", {"stage": stage, "label": label})

    def judge_completed(self, *, satisfied: bool, reason_code: str) -> None:
        self.emit("judge.completed", {"satisfied": satisfied, "reason_code": reason_code})

    def assistant_message(self, text: str, *, partial: bool = False) -> None:
        if text:
            self.emit("assistant.message", {"text": text, "partial": partial})

    def terminal(self, status: str, *, message: str = "") -> None:
        if self._terminal:
            return
        if any(
            event.run_id == self.run_id and event.is_terminal
            for event in self.journal.events()
        ):
            self._terminal = True
            return
        event_type = {
            "completed": "run.completed", "failed": "run.failed",
            "cancelled": "run.cancelled", "interrupted": "run.interrupted",
        }[status]
        self.emit(event_type, {"message": message} if message else {})
        self._terminal = True

    def emit(self, event_type: str, payload: dict[str, object], *, visibility: str = "user") -> None:
        self.journal.append(event_type, run_id=self.run_id, payload=payload, visibility=visibility)
