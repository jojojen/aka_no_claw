"""Thread-safe append-only JSONL storage for durable session events."""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import tempfile
import threading
import time
from uuid import uuid4

from .session_events import (
    DEFAULT_MAX_PAYLOAD_BYTES, DURABLE_EVENT_TYPES, EventValidationError,
    SessionRunEvent, validate_identifier,
)


DEFAULT_MAX_BYTES = 25 * 1024 * 1024
_LOCKS: dict[str, threading.RLock] = {}
_LOCKS_GUARD = threading.Lock()


class JournalError(RuntimeError):
    """Base error for durable-history failures."""


class JournalCorruptionError(JournalError):
    """A committed JSONL line was malformed; replay must not pretend otherwise."""


class CursorExpiredError(JournalError):
    """The requested cursor predates retained complete-run history."""


class JournalRetentionError(JournalError):
    """The byte bound cannot be met without splitting a nonterminal run."""


@dataclass(frozen=True, slots=True)
class CursorPage:
    events: list[SessionRunEvent]
    server_cursor: int
    has_more: bool
    latest_seq: int


class SessionEventJournal:
    """A per-session JSONL journal whose append lock owns sequence allocation."""

    def __init__(
        self, root_dir: str, session_id: str, *, max_bytes: int = DEFAULT_MAX_BYTES,
        max_payload_bytes: int = DEFAULT_MAX_PAYLOAD_BYTES,
    ) -> None:
        self.session_id = validate_identifier(session_id, "session_id")
        if max_bytes <= 0 or max_payload_bytes <= 0:
            raise ValueError("journal limits must be positive")
        self._dir = Path(root_dir).expanduser().resolve() / self.session_id
        self._max_bytes = max_bytes
        self._max_payload_bytes = max_payload_bytes
        with _LOCKS_GUARD:
            self._lock = _LOCKS.setdefault(str(self._dir), threading.RLock())

    @property
    def directory(self) -> Path:
        return self._dir

    def append(
        self, event_type: str, *, run_id: str, payload: dict[str, object],
        visibility: str = "user", occurred_at: float | None = None,
    ) -> SessionRunEvent:
        if event_type not in DURABLE_EVENT_TYPES:
            raise EventValidationError(f"{event_type} is not a durable event type")
        with self._lock:
            events, metadata = self._load_locked(recover_tail=True)
            event = SessionRunEvent(
                event_version=1, event_id=uuid4().hex, session_id=self.session_id,
                run_id=run_id, seq=int(metadata["last_seq"]) + 1,
                occurred_at=time.time() if occurred_at is None else occurred_at,
                type=event_type, visibility=visibility,
                payload=self._bounded_payload(payload),
            )
            candidate = events + [event]
            retained = self._retained_events(candidate)
            if retained != candidate:
                self._rewrite_locked(retained)
            else:
                self._append_line_locked(event)
            self._write_metadata_locked(retained)
            return event

    def read(self, *, after: int | None = None, limit: int = 500) -> CursorPage:
        if after is not None and (not isinstance(after, int) or after < 0):
            raise ValueError("after must be a non-negative integer")
        if not isinstance(limit, int) or not 1 <= limit <= 500:
            raise ValueError("limit must be between 1 and 500")
        with self._lock:
            events, metadata = self._load_locked(recover_tail=True)
            earliest = int(metadata["earliest_seq"])
            if after is not None and events and after < earliest - 1:
                raise CursorExpiredError(f"cursor {after} predates retained sequence {earliest}")
            selected = [event for event in events if after is None or event.seq > after]
            page_events = selected[:limit]
            latest_seq = int(metadata["last_seq"])
            server_cursor = page_events[-1].seq if page_events else latest_seq
            return CursorPage(
                events=page_events, server_cursor=server_cursor,
                has_more=len(selected) > len(page_events), latest_seq=latest_seq,
            )

    def events(self) -> list[SessionRunEvent]:
        with self._lock:
            events, _ = self._load_locked(recover_tail=True)
            return events

    def _bounded_payload(self, payload: dict[str, object]) -> dict[str, object]:
        from .session_events import normalise_payload
        return normalise_payload(payload, max_bytes=self._max_payload_bytes)

    def _load_locked(self, *, recover_tail: bool) -> tuple[list[SessionRunEvent], dict[str, int]]:
        self._dir.mkdir(parents=True, exist_ok=True)
        events = self._read_events_locked(recover_tail=recover_tail)
        metadata = self._read_metadata_locked(events)
        return events, metadata

    def _segment_paths(self) -> list[Path]:
        return sorted(self._dir.glob("updates-*.jsonl"))

    def _active_path(self) -> Path:
        paths = self._segment_paths()
        return paths[-1] if paths else self._dir / "updates-000001.jsonl"

    def _read_events_locked(self, *, recover_tail: bool) -> list[SessionRunEvent]:
        events: list[SessionRunEvent] = []
        for path in self._segment_paths():
            raw = path.read_bytes()
            if raw and not raw.endswith(b"\n"):
                if not recover_tail:
                    raise JournalCorruptionError(f"incomplete tail in {path.name}")
                newline = raw.rfind(b"\n")
                complete, tail = (raw[:newline + 1], raw[newline + 1:]) if newline >= 0 else (b"", raw)
                quarantine = self._dir / "quarantine"
                quarantine.mkdir(exist_ok=True)
                (quarantine / f"{path.name}.{int(time.time() * 1000)}.tail").write_bytes(tail)
                with path.open("r+b") as handle:
                    handle.truncate(len(complete))
                raw = complete
            for line_number, line in enumerate(raw.splitlines(), start=1):
                try:
                    value = json.loads(line.decode("utf-8"))
                    event = SessionRunEvent.from_dict(value)
                except (UnicodeDecodeError, ValueError, TypeError, EventValidationError) as exc:
                    raise JournalCorruptionError(f"malformed committed line {path.name}:{line_number}") from exc
                if event.session_id != self.session_id:
                    raise JournalCorruptionError(f"wrong session in {path.name}:{line_number}")
                events.append(event)
        sequences = [event.seq for event in events]
        if sequences != sorted(set(sequences)):
            raise JournalCorruptionError("event sequences are not strictly increasing")
        return events

    def _read_metadata_locked(self, events: list[SessionRunEvent]) -> dict[str, int]:
        path = self._dir / "metadata.json"
        fallback = {
            "earliest_seq": events[0].seq if events else 0,
            "last_seq": events[-1].seq if events else 0,
        }
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                return fallback
            last_seq = raw.get("last_seq")
            earliest_seq = raw.get("earliest_seq")
            if isinstance(last_seq, int) and isinstance(earliest_seq, int) and last_seq >= fallback["last_seq"]:
                return {"last_seq": last_seq, "earliest_seq": earliest_seq}
        except (FileNotFoundError, OSError, ValueError):
            pass
        return fallback

    def _append_line_locked(self, event: SessionRunEvent) -> None:
        path = self._active_path()
        line = json.dumps(event.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
        with path.open("a", encoding="utf-8") as handle:
            handle.write(line)
            handle.flush()
            os.fsync(handle.fileno())

    def _retained_events(self, events: list[SessionRunEvent]) -> list[SessionRunEvent]:
        retained = events
        while self._encoded_size(retained) > self._max_bytes:
            terminal_runs = {
                event.run_id for event in retained if event.is_terminal
            }
            removable = next((event.run_id for event in retained if event.run_id in terminal_runs), None)
            if removable is None:
                raise JournalRetentionError("journal limit would split an active run")
            retained = [event for event in retained if event.run_id != removable]
        return retained

    @staticmethod
    def _encoded_size(events: list[SessionRunEvent]) -> int:
        return sum(len(json.dumps(event.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")) + 1 for event in events)

    def _rewrite_locked(self, events: list[SessionRunEvent]) -> None:
        existing = self._segment_paths()
        next_index = (int(existing[-1].stem.rsplit("-", 1)[1]) + 1) if existing else 1
        target = self._dir / f"updates-{next_index:06d}.jsonl"
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=self._dir, delete=False, suffix=".tmp") as tmp:
            for event in events:
                tmp.write(json.dumps(event.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n")
            tmp.flush()
            os.fsync(tmp.fileno())
            temp_path = Path(tmp.name)
        os.replace(temp_path, target)
        for path in existing:
            path.unlink()

    def _write_metadata_locked(self, events: list[SessionRunEvent]) -> None:
        previous = self._read_metadata_locked(events)
        metadata = {
            "event_version": 1,
            "earliest_seq": events[0].seq if events else previous["last_seq"],
            "last_seq": max(previous["last_seq"], events[-1].seq if events else 0),
        }
        path = self._dir / "metadata.json"
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=self._dir, delete=False, suffix=".tmp") as tmp:
            json.dump(metadata, tmp, sort_keys=True, separators=(",", ":"))
            tmp.flush()
            os.fsync(tmp.fileno())
            temp_path = Path(tmp.name)
        os.replace(temp_path, path)
