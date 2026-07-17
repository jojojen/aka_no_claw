"""Request and resolve manifest-bound approvals without UI-specific state."""

from __future__ import annotations

from dataclasses import replace
import hmac
import hashlib
import secrets
import time
from typing import Callable
from uuid import uuid4

from .approval_models import FrozenActionManifest, PendingApproval
from .approval_store import ApprovalStore


class ApprovalService:
    def __init__(self, store: ApprovalStore, *, ttl_seconds: int, clock: Callable[[], float] = time.time) -> None:
        self.store = store
        self.ttl_seconds = max(1, int(ttl_seconds))
        self.clock = clock

    def request(self, *, session_id: str, run_id: str, manifest: FrozenActionManifest, risk: str, descriptor: dict) -> PendingApproval:
        now = self.clock()
        approval_id, nonce, expires_at = uuid4().hex, secrets.token_urlsafe(18), now + self.ttl_seconds
        token = self._token(approval_id, session_id, run_id, manifest.hash, expires_at, nonce)
        record = PendingApproval(
            approval_id=approval_id, session_id=session_id, run_id=run_id,
            manifest=manifest.to_dict(), manifest_hash=manifest.hash, risk=risk,
            expires_at=expires_at, nonce=nonce, token=token, descriptor=descriptor,
        )
        self.store.put(record)
        return record

    def resolve(
        self, *, approval_id: str, session_id: str, run_id: str, token: str,
        decision: str, execute: Callable[[PendingApproval], tuple[str, str, str]],
        approval_enabled: bool = True,
    ) -> tuple[PendingApproval, bool]:
        if decision not in {"approve", "reject", "cancel"}:
            raise ValueError("decision must be approve, reject, or cancel")
        current = self.store.get(approval_id)
        if current is None:
            raise KeyError(approval_id)
        expected_token = self._token(
            current.approval_id, current.session_id, current.run_id,
            current.manifest_hash, current.expires_at, current.nonce,
        )
        if (
            current.session_id != session_id
            or current.run_id != run_id
            or not hmac.compare_digest(current.token, expected_token)
            or not hmac.compare_digest(token, expected_token)
        ):
            raise PermissionError("approval binding does not match")
        if current.status != "pending":
            if current.decision == decision:
                return current, True
            raise RuntimeError("approval was already resolved")
        now = self.clock()
        if now >= current.expires_at:
            expired = replace(
                current, status="resolved", decision=decision, resolution="expired", resolved_at=now,
                result_message="核准已逾期", reason_code="expired",
            )
            return self.store.compare_and_set(approval_id, lambda item: item.status == "pending", expired), False
        if decision != "approve":
            reason_code = "operator_rejected" if decision == "reject" else "run_cancelled"
            resolved = replace(
                current, status="resolved", decision=decision, resolution=decision, resolved_at=now,
                result_message="操作未獲核准", reason_code=reason_code,
            )
            return self.store.compare_and_set(approval_id, lambda item: item.status == "pending", resolved), False
        if not approval_enabled:
            disabled = replace(
                current, status="resolved", decision=decision, resolution="disabled", resolved_at=now,
                result_message="Web 核准功能目前已停用，操作未執行", reason_code="approval_disabled",
            )
            return self.store.compare_and_set(approval_id, lambda item: item.status == "pending", disabled), False
        consumed = replace(current, status="executing", decision=decision, resolution="approve", resolved_at=now)
        actual = self.store.compare_and_set(approval_id, lambda item: item.status == "pending", consumed)
        if actual is not consumed:
            if actual.decision == decision:
                return actual, True
            raise RuntimeError("approval was already resolved")
        try:
            resolution, reason_code, message = execute(consumed)
        except Exception:
            resolution, reason_code, message = (
                "execution_failed", "execution_failed", "核准後執行失敗，操作不會重試",
            )
        resolved = replace(
            consumed, status="resolved", resolution=resolution, resolved_at=self.clock(),
            result_message=message, reason_code=reason_code,
        )
        self.store.compare_and_set(approval_id, lambda item: item.status == "executing", resolved)
        return resolved, False

    def _token(self, approval_id: str, session_id: str, run_id: str, manifest_hash: str, expires_at: float, nonce: str) -> str:
        message = "|".join((approval_id, session_id, run_id, manifest_hash, f"{expires_at:.6f}", nonce)).encode("utf-8")
        return hmac.new(self.store.signing_key(), message, hashlib.sha256).hexdigest()
