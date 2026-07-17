"""Journal authority, legacy compatibility, and session-level projection."""

from __future__ import annotations

from pathlib import Path
import tempfile
import time
from typing import Callable

from .run_recorder import RunRecorder
from .session_event_journal import SessionEventJournal
from .session_memory import SessionMemoryStore
from .session_projection import migrate_legacy_snapshot, project_session


DEFAULT_SESSION_ID = "web-default"


class SessionEventService:
    """Keeps the journal authoritative without removing the old snapshot file."""

    def __init__(self, settings: object, legacy_store: Callable[[], SessionMemoryStore]) -> None:
        self._settings = settings
        self._legacy_store = legacy_store
        configured_memory_dir = getattr(settings, "openclaw_web_memory_dir", None)
        configured_event_dir = getattr(settings, "openclaw_web_event_dir", None)
        if configured_event_dir:
            self._root_dir = str(configured_event_dir)
        elif configured_memory_dir:
            self._root_dir = str(Path(configured_memory_dir).parent / "web_sessions")
        else:
            # Command-only test seams historically pass a partial settings
            # namespace. Keep their event history isolated rather than growing
            # a shared runtime journal in the checkout.
            self._root_dir = tempfile.mkdtemp(prefix="aka_no_claw_events_")
        self._max_bytes = int(getattr(settings, "openclaw_web_event_max_bytes", 25 * 1024 * 1024))
        self._max_payload_bytes = int(getattr(settings, "openclaw_web_event_max_payload_bytes", 64 * 1024))
        self._journals: dict[str, SessionEventJournal] = {}

    def journal(self, session_id: str | None = None) -> SessionEventJournal:
        session_id = session_id or DEFAULT_SESSION_ID
        if session_id not in self._journals:
            self._journals[session_id] = SessionEventJournal(
                self._root_dir, session_id, max_bytes=self._max_bytes,
                max_payload_bytes=self._max_payload_bytes,
            )
        return self._journals[session_id]

    def ensure(self, session_id: str | None = None, *, legacy_snapshot: dict | None = None) -> SessionEventJournal:
        journal = self.journal(session_id)
        max_age_days = int(getattr(self._settings, "openclaw_web_event_max_age_days", 30))
        if max_age_days > 0:
            journal.expire_complete_runs_before(time.time() - max_age_days * 24 * 60 * 60)
        if journal.events():
            return journal
        if legacy_snapshot is not None:
            snapshot = legacy_snapshot
        else:
            try:
                snapshot = self._legacy_store().load()
            except RuntimeError:
                # Narrow unit-test and command-only settings do not configure
                # the optional legacy snapshot; an empty migration is valid.
                snapshot = {}
        for event in migrate_legacy_snapshot(snapshot, session_id=journal.session_id):
            journal.append_existing(event)
        return journal

    def recorder(self, session_id: str | None = None) -> RunRecorder:
        return RunRecorder(self.ensure(session_id))

    def projection(self, session_id: str | None = None) -> dict:
        projection = project_session(self.ensure(session_id).events())
        payload = projection.to_dict()
        preferences = payload.pop("display_preferences")
        return {
            "schema_version": 1,
            "messages": [
                {"id": message["event_id"], "role": message["role"], "text": message["text"]}
                for message in payload.pop("messages")
            ],
            "mode": preferences.get("mode"),
            "chat_backend": preferences.get("chat_backend"),
            "investment_submode": preferences.get("investment_submode"),
            "active_job_id": payload["active_run_ids"][0] if payload["active_run_ids"] else None,
            "queue": payload["prompt_queue"],
            "event_projection": payload,
        }

    def save_compat_snapshot(self, snapshot: object, session_id: str | None = None) -> dict:
        if not isinstance(snapshot, dict):
            raise ValueError("session snapshot must be a JSON object")
        journal = self.ensure(session_id, legacy_snapshot=snapshot)
        # A GET may have bootstrapped an otherwise empty session before the old
        # Web client sends its first POST. Import those visible messages once;
        # after any authoritative message exists, client snapshots cannot
        # replace history.
        current = project_session(journal.events())
        if not current.messages:
            for message in snapshot.get("messages") or []:
                if not isinstance(message, dict):
                    continue
                role, text = message.get("role"), message.get("text")
                if role in {"user", "assistant"} and isinstance(text, str):
                    journal.append(
                        f"{role}.message", run_id="legacy-import",
                        payload={"text": text, "evidence": "legacy_snapshot"},
                    )
        preferences = {
            key: snapshot[key] for key in ("mode", "chat_backend", "investment_submode")
            if snapshot.get(key) is not None
        }
        if preferences:
            journal.append("context.checkpoint", run_id="session", visibility="internal", payload={"display_preferences": preferences})
        return self.projection(session_id)

    def clear(self, session_id: str | None = None) -> None:
        self.ensure(session_id).append(
            "context.checkpoint", run_id="session", visibility="internal", payload={"clear": True}
        )
