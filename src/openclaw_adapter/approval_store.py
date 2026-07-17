"""Small atomic on-disk store for one-shot approval records."""

from __future__ import annotations

import json
import os
from pathlib import Path
import secrets
import tempfile
import threading
from typing import Callable

from .approval_models import PendingApproval


class ApprovalStore:
    def __init__(self, root_dir: str) -> None:
        self.root = Path(root_dir)
        self.path = self.root / "approvals.json"
        self.key_path = self.root / "approval_hmac.key"
        self._lock = threading.Lock()

    def signing_key(self) -> bytes:
        with self._lock:
            self.root.mkdir(parents=True, exist_ok=True)
            if self.key_path.exists():
                key = self.key_path.read_bytes()
                if len(key) >= 32:
                    return key
                raise RuntimeError("approval signing key is invalid")
            key = secrets.token_bytes(32)
            fd = os.open(self.key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as handle:
                handle.write(key)
            return key

    def get(self, approval_id: str) -> PendingApproval | None:
        with self._lock:
            data = self._read()
            value = data.get(approval_id)
            return PendingApproval.from_dict(value) if isinstance(value, dict) else None

    def put(self, record: PendingApproval) -> None:
        with self._lock:
            data = self._read()
            if record.approval_id in data:
                raise RuntimeError("approval id already exists")
            data[record.approval_id] = record.to_dict()
            self._write(data)

    def compare_and_set(self, approval_id: str, predicate: Callable[[PendingApproval], bool], replacement: PendingApproval) -> PendingApproval:
        with self._lock:
            data = self._read()
            value = data.get(approval_id)
            if not isinstance(value, dict):
                raise KeyError(approval_id)
            current = PendingApproval.from_dict(value)
            if not predicate(current):
                return current
            data[approval_id] = replacement.to_dict()
            self._write(data)
            return replacement

    def _read(self) -> dict:
        if not self.path.exists():
            return {}
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise RuntimeError("approval store is unavailable") from exc
        if not isinstance(value, dict):
            raise RuntimeError("approval store is invalid")
        return value

    def _write(self, value: dict) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        fd, temp_path = tempfile.mkstemp(prefix=".approvals-", suffix=".tmp", dir=self.root)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(value, handle, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, self.path)
        finally:
            if os.path.exists(temp_path):
                os.unlink(temp_path)
