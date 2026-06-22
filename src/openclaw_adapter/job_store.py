"""Persistent job snapshots for web async research reconnect (aka_no_claw #37).

Each web async job is atomically written to .openclaw_tmp/web_jobs/<job_id>.json
at creation, and again when the job finishes (done/error). poll_job() checks
this store when the in-memory job is missing (browser reload or bridge restart)
and returns the correct terminal state — or "interrupted" when the persisted
status is still "running" but the in-memory worker no longer exists.

Retention: purge_expired() deletes snapshots older than MAX_AGE_SECONDS by
updated_at. It is called on every new job creation (GC-on-write), so unbounded
growth is impossible without a background daemon.
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import time
from pathlib import Path

logger = logging.getLogger(__name__)

MAX_AGE_SECONDS = 7200  # 2 hours

# Only accept UUID-hex job ids (32 lowercase hex chars) so an attacker cannot
# inject path components like "../escape" and write/read outside web_jobs/.
_SAFE_JOB_ID_RE = re.compile(r"^[a-f0-9]{32}$")


def _is_safe_job_id(job_id: str) -> bool:
    return bool(_SAFE_JOB_ID_RE.match(job_id))


class JobStore:
    def __init__(self, dir_path: str) -> None:
        self._dir = Path(dir_path)

    def save(self, snapshot: dict) -> None:
        """Atomically write a job snapshot. Silently skips on OSError or
        an unsafe job_id (path traversal guard)."""
        job_id = snapshot.get("job_id", "")
        if not _is_safe_job_id(job_id):
            logger.warning("job_store: rejected unsafe job_id %r", job_id)
            return
        try:
            self._dir.mkdir(parents=True, exist_ok=True)
            body = json.dumps(snapshot, ensure_ascii=False).encode("utf-8")
            path = self._dir / f"{job_id}.json"
            with tempfile.NamedTemporaryFile(
                "wb", dir=self._dir, delete=False, suffix=".tmp"
            ) as tmp:
                tmp.write(body)
                tmp_path = Path(tmp.name)
            os.replace(tmp_path, path)
        except OSError:
            logger.warning("job_store: save failed for job=%s", job_id, exc_info=True)

    def load(self, job_id: str) -> dict | None:
        """Return persisted snapshot for job_id, or None if missing, corrupt,
        or an unsafe job_id (path traversal guard)."""
        if not _is_safe_job_id(job_id):
            logger.warning("job_store: rejected unsafe job_id %r on load", job_id)
            return None
        path = self._dir / f"{job_id}.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else None
        except FileNotFoundError:
            return None
        except (OSError, ValueError):
            logger.warning("job_store: unreadable snapshot for job=%s", job_id, exc_info=True)
            return None

    def purge_expired(self) -> int:
        """Delete snapshots whose updated_at is older than MAX_AGE_SECONDS."""
        cutoff = time.time() - MAX_AGE_SECONDS
        deleted = 0
        try:
            for p in self._dir.glob("*.json"):
                try:
                    data = json.loads(p.read_text(encoding="utf-8"))
                    updated_at = data.get("updated_at", 0)
                    if isinstance(updated_at, (int, float)) and updated_at < cutoff:
                        p.unlink(missing_ok=True)
                        deleted += 1
                except (OSError, ValueError):
                    pass
        except OSError:
            pass
        return deleted
