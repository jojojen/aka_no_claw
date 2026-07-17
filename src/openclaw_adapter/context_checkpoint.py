"""Grounded, bounded Web conversation context checkpoints (#87).

The event journal remains the authority.  This module only derives a compact
model-input aid from a closed prefix and keeps it in a separate, atomic store.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import json
import math
import os
from pathlib import Path
import re
import tempfile
import time
from uuid import uuid4

from .session_events import SessionRunEvent


CHECKPOINT_VERSION = 1
_SECRET_RE = re.compile(r"(?:api[_ -]?key|authorization|password|secret)\s*[:=]\s*\S+", re.I)


@dataclass(frozen=True, slots=True)
class GroundedFact:
    text: str
    source_event_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ContextCheckpoint:
    checkpoint_version: int
    checkpoint_id: str
    session_id: str
    source_seq_start: int
    source_seq_end: int
    created_at: float
    model_provider: str
    model_id: str
    prompt_version: str
    summary: str
    pinned_facts: tuple[GroundedFact, ...]
    unresolved_items: tuple[GroundedFact, ...]
    artifact_refs: tuple[str, ...]
    validation_status: str
    previous_checkpoint_id: str | None = None

    def public(self) -> dict[str, object]:
        return {
            "checkpoint_id": self.checkpoint_id,
            "source_seq_start": self.source_seq_start,
            "source_seq_end": self.source_seq_end,
            "created_at": self.created_at,
            "summary_preview": self.summary[:240],
            "summary": self.summary,
            "pinned_facts": [asdict(item) for item in self.pinned_facts],
            "unresolved_items": [asdict(item) for item in self.unresolved_items],
            "validation_status": self.validation_status,
        }


def estimate_tokens(text: str) -> int:
    """A deliberately labelled estimate, stable across local/cloud providers."""
    return math.ceil(len(text.encode("utf-8")) / 4)


def _safe_excerpt(text: str, limit: int = 480) -> str | None:
    cleaned = " ".join(text.split())
    if not cleaned or _SECRET_RE.search(cleaned):
        return None
    return cleaned[:limit]


class ContextCheckpointStore:
    def __init__(self, root_dir: str, *, max_checkpoints: int = 10, max_summary_bytes: int = 12 * 1024) -> None:
        self._root = Path(root_dir)
        self._max_checkpoints = max(1, max_checkpoints)
        self._max_summary_bytes = max(256, max_summary_bytes)

    def _path(self, session_id: str) -> Path:
        return self._root / session_id / "context_checkpoints.json"

    def load(self, session_id: str) -> list[ContextCheckpoint]:
        try:
            raw = json.loads(self._path(session_id).read_text(encoding="utf-8"))
        except FileNotFoundError:
            return []
        except (OSError, ValueError):
            return []
        if not isinstance(raw, list):
            return []
        checkpoints: list[ContextCheckpoint] = []
        for item in raw:
            try:
                facts = tuple(GroundedFact(str(f["text"]), tuple(str(i) for i in f["source_event_ids"])) for f in item.get("pinned_facts", []))
                unresolved = tuple(GroundedFact(str(f["text"]), tuple(str(i) for i in f["source_event_ids"])) for f in item.get("unresolved_items", []))
                checkpoints.append(ContextCheckpoint(
                    checkpoint_version=int(item["checkpoint_version"]), checkpoint_id=str(item["checkpoint_id"]),
                    session_id=str(item["session_id"]), source_seq_start=int(item["source_seq_start"]),
                    source_seq_end=int(item["source_seq_end"]), created_at=float(item["created_at"]),
                    model_provider=str(item["model_provider"]), model_id=str(item["model_id"]),
                    prompt_version=str(item["prompt_version"]), summary=str(item["summary"]), pinned_facts=facts,
                    unresolved_items=unresolved, artifact_refs=tuple(str(v) for v in item.get("artifact_refs", [])),
                    validation_status=str(item["validation_status"]), previous_checkpoint_id=item.get("previous_checkpoint_id"),
                ))
            except (KeyError, TypeError, ValueError):
                continue
        return checkpoints

    def latest(self, session_id: str) -> ContextCheckpoint | None:
        checkpoints = self.load(session_id)
        return checkpoints[-1] if checkpoints else None

    def save(self, checkpoint: ContextCheckpoint) -> None:
        if len(checkpoint.summary.encode("utf-8")) > self._max_summary_bytes:
            raise ValueError("checkpoint summary exceeds the configured bound")
        current = self.load(checkpoint.session_id)
        current = [item for item in current if item.checkpoint_id != checkpoint.checkpoint_id]
        current.append(checkpoint)
        self._write(checkpoint.session_id, current[-self._max_checkpoints:])

    def clear(self, session_id: str) -> ContextCheckpoint | None:
        latest = self.latest(session_id)
        if latest is not None:
            self._write(session_id, [])
        return latest

    def _write(self, session_id: str, checkpoints: list[ContextCheckpoint]) -> None:
        path = self._path(session_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps([asdict(item) for item in checkpoints], ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False, suffix=".tmp") as tmp:
            tmp.write(payload)
            tmp.flush()
            os.fsync(tmp.fileno())
            temporary = Path(tmp.name)
        os.replace(temporary, path)


class ContextCompactor:
    """Deterministic first compactor; it never asserts facts beyond events."""

    def __init__(self, store: ContextCheckpointStore, *, recent_turns: int = 6) -> None:
        self._store = store
        self._recent_turns = max(2, recent_turns)

    def latest(self, session_id: str) -> ContextCheckpoint | None:
        return self._store.latest(session_id)

    def clear(self, session_id: str) -> ContextCheckpoint | None:
        return self._store.clear(session_id)

    def build(self, session_id: str, events: list[SessionRunEvent]) -> ContextCheckpoint | None:
        messages = [event for event in events if event.type in {"user.message", "assistant.message"} and event.visibility == "user"]
        eligible = messages[:-self._recent_turns]
        if not eligible:
            return None
        previous = self._store.latest(session_id)
        start = eligible[0].seq
        if previous is not None:
            eligible = [event for event in eligible if event.seq > previous.source_seq_end]
            if not eligible:
                return previous
            start = previous.source_seq_start
        source_ids = {event.event_id for event in eligible}
        lines = ["較早對話的可追溯摘要（不是原始歷史）："]
        if previous is not None:
            previous_lines = previous.summary.splitlines()
            if previous_lines and previous_lines[0] == lines[0]:
                previous_lines = previous_lines[1:]
            lines.extend(previous_lines)
        for event in eligible:
            text = event.payload.get("text")
            if not isinstance(text, str):
                continue
            excerpt = _safe_excerpt(text)
            if excerpt is not None:
                label = "使用者" if event.type == "user.message" else "助理"
                lines.append(f"- {label}（{event.event_id}）：{excerpt}")
        if len(lines) == 1:
            return None
        checkpoint = ContextCheckpoint(
            checkpoint_version=CHECKPOINT_VERSION, checkpoint_id=uuid4().hex, session_id=session_id,
            source_seq_start=start, source_seq_end=eligible[-1].seq, created_at=time.time(),
            model_provider="deterministic", model_id="event-excerpt-v1", prompt_version="context-checkpoint-v1",
            summary="\n".join(lines), pinned_facts=(), unresolved_items=(), artifact_refs=(),
            validation_status="valid", previous_checkpoint_id=previous.checkpoint_id if previous else None,
        )
        self.validate(checkpoint, source_ids)
        self._store.save(checkpoint)
        return checkpoint

    @staticmethod
    def validate(checkpoint: ContextCheckpoint, source_ids: set[str]) -> None:
        if checkpoint.source_seq_start < 1 or checkpoint.source_seq_end < checkpoint.source_seq_start:
            raise ValueError("checkpoint source range is invalid")
        if _SECRET_RE.search(checkpoint.summary):
            raise ValueError("checkpoint contains secret-like material")
        for fact in (*checkpoint.pinned_facts, *checkpoint.unresolved_items):
            if not fact.source_event_ids or not set(fact.source_event_ids).issubset(source_ids):
                raise ValueError("checkpoint fact references an event outside its source range")
