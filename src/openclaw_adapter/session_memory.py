"""Server-side short-term session memory for the aka_no_claw_web console (#32).

The mobile console keeps recent chat + deep-research output only in browser
runtime state, so a page reload / browser close / reconnect from another device
loses it. This module owns a tiny JSON snapshot on the Mac mini so the console
can restore its session from the server — the source of truth lives here, not in
phone storage and not in any git-tracked file.

Storage is a single ``default_session.json`` under a gitignored temp dir
(``.openclaw_tmp/web_console_memory/`` by default, overridable via
``OPENCLAW_WEB_MEMORY_DIR``). This is deliberately a flat JSON snapshot, not a
new database: single-user, local-only, debug-friendly. It is never written to
``knowledge.sqlite3`` and carries no long-term/searchable history.

Bounded retention keeps the snapshot from growing without limit: at most
``MAX_MESSAGES`` messages, dropped to fit ``MAX_BYTES``, and a whole snapshot
older than ``MAX_AGE_SECONDS`` is treated as expired (load returns empty).
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from pathlib import Path

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1
_SESSION_FILENAME = "default_session.json"

# Retention bounds (issue #32 asks for at least one; we apply all three).
MAX_MESSAGES = 100
MAX_BYTES = 5 * 1024 * 1024  # 5 MB serialized snapshot
MAX_AGE_SECONDS = 7 * 24 * 60 * 60  # 7 days
MAX_SCALAR_LEN = 512  # per-field cap so scalar bloat can't bypass MAX_BYTES

# Top-level scalar fields we round-trip (besides ``messages``). Anything else in
# an incoming snapshot is ignored so a future frontend field can't bloat the
# file or smuggle in non-session data.
_SCALAR_FIELDS = (
    "mode",
    "chat_backend",
    "investment_submode",
    "active_job_id",
)


class SessionWriteError(RuntimeError):
    """Raised when persisting the session snapshot to disk fails."""


def empty_session() -> dict:
    """A blank session — what every failure path (missing / corrupt / expired)
    returns, so the frontend simply starts from a clean console."""
    return {
        "schema_version": SCHEMA_VERSION,
        "messages": [],
        "mode": None,
        "chat_backend": None,
        "investment_submode": None,
        "active_job_id": None,
        "updated_at": None,
    }


class SessionMemoryStore:
    def __init__(self, dir_path: str) -> None:
        self._dir = Path(dir_path)
        self._path = self._dir / _SESSION_FILENAME

    # --- load ------------------------------------------------------------
    def load(self) -> dict:
        """Return the saved snapshot, or an empty session. Never raises: a
        missing file, unreadable bytes, corrupt JSON, wrong shape, or an
        expired (>7d) snapshot all fall back to ``empty_session`` so a bad file
        can't crash the API."""
        try:
            raw = self._path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return empty_session()
        except OSError as exc:
            logger.warning("session memory: unreadable file %s: %s", self._path, exc)
            return empty_session()
        try:
            data = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            logger.warning("session memory: corrupt JSON at %s — starting blank", self._path)
            return empty_session()
        if not isinstance(data, dict):
            logger.warning("session memory: snapshot is not an object — starting blank")
            return empty_session()

        updated_at = data.get("updated_at")
        if isinstance(updated_at, (int, float)) and (time.time() - updated_at) > MAX_AGE_SECONDS:
            logger.info("session memory: snapshot older than %ss — treating as empty", MAX_AGE_SECONDS)
            return empty_session()

        return self._normalize(data)

    # --- save ------------------------------------------------------------
    def save(self, snapshot: object) -> dict:
        """Normalize + trim + atomically write the snapshot. Returns the stored
        snapshot (with refreshed ``updated_at`` / ``schema_version``). Raises
        :class:`SessionWriteError` if the write fails so the HTTP layer can
        answer with a structured error/warning instead of a silent loss."""
        if not isinstance(snapshot, dict):
            raise SessionWriteError("session snapshot must be a JSON object")
        normalized = self._normalize(snapshot)
        normalized["updated_at"] = time.time()
        normalized["schema_version"] = SCHEMA_VERSION
        normalized = self._trim(normalized)

        body = json.dumps(normalized, ensure_ascii=False).encode("utf-8")
        if len(body) > MAX_BYTES:
            raise SessionWriteError(
                f"snapshot exceeds MAX_BYTES after trimming ({len(body)} > {MAX_BYTES})"
            )
        try:
            self._dir.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                "wb", dir=self._dir, delete=False, suffix=".tmp"
            ) as tmp:
                tmp.write(body)
                tmp_path = Path(tmp.name)
            os.replace(tmp_path, self._path)
        except OSError as exc:
            logger.exception("session memory: write failed at %s", self._path)
            raise SessionWriteError(str(exc)) from exc
        return normalized

    # --- clear -----------------------------------------------------------
    def clear(self) -> None:
        """Delete the saved snapshot. Missing file is a no-op (idempotent).
        Any other OSError is re-raised so callers can report a genuine failure
        rather than silently returning success (DELETE false-success fix)."""
        try:
            self._path.unlink()
        except FileNotFoundError:
            return
        except OSError:
            logger.warning("session memory: clear failed at %s", self._path, exc_info=True)
            raise

    # --- helpers ---------------------------------------------------------
    def _normalize(self, data: dict) -> dict:
        out = empty_session()
        raw_messages = data.get("messages")
        out["messages"] = list(raw_messages) if isinstance(raw_messages, list) else []
        for field in _SCALAR_FIELDS:
            if field in data:
                val = data[field]
                if isinstance(val, str):
                    val = val[:MAX_SCALAR_LEN]
                elif val is not None:
                    val = None  # reject non-string, non-None scalars
                out[field] = val
        if isinstance(data.get("updated_at"), (int, float)):
            out["updated_at"] = data["updated_at"]
        return out

    def _trim(self, snapshot: dict) -> dict:
        messages = snapshot.get("messages") or []
        if len(messages) > MAX_MESSAGES:
            messages = messages[-MAX_MESSAGES:]
        # Drop oldest until the serialized snapshot fits the byte budget.
        # MAX_BYTES is a hard limit: if even a single message exceeds the budget,
        # drop to an empty list rather than persist an over-budget snapshot.
        while messages:
            snapshot["messages"] = messages
            if len(json.dumps(snapshot, ensure_ascii=False).encode("utf-8")) <= MAX_BYTES:
                break
            messages = messages[1:]
        snapshot["messages"] = messages
        return snapshot
