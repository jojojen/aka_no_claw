"""Canonical manifests and persisted state for Web action approval."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from typing import Any

from .action_risk import EffectProfile


def canonical_json(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def sha256_json(value: object) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


@dataclass(frozen=True, slots=True)
class FrozenActionManifest:
    schema_version: int
    action_kind: str
    tool_slug: str | None
    artifact_sha256: str
    arguments_sha256: str
    dependency_lock_sha256: str | None
    requested_capabilities: tuple[str, ...]
    network_scopes: tuple[str, ...]
    filesystem_scopes: tuple[str, ...]
    device_scopes: tuple[str, ...]
    policy_version: str
    created_at: float

    @property
    def hash(self) -> str:
        return sha256_json(asdict(self))

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def for_generated_tool(cls, *, slug: str, code: str, arguments: dict, profile: EffectProfile, policy_version: str, created_at: float) -> "FrozenActionManifest":
        return cls(
            schema_version=1, action_kind="generated_tool.execute", tool_slug=slug,
            artifact_sha256=hashlib.sha256(code.encode("utf-8")).hexdigest(),
            arguments_sha256=sha256_json(arguments), dependency_lock_sha256=None,
            requested_capabilities=profile.capabilities, network_scopes=profile.network_scopes,
            filesystem_scopes=profile.filesystem_scopes, device_scopes=profile.device_scopes,
            policy_version=policy_version, created_at=created_at,
        )


@dataclass(frozen=True, slots=True)
class PendingApproval:
    approval_id: str
    session_id: str
    run_id: str
    manifest: dict[str, Any]
    manifest_hash: str
    risk: str
    expires_at: float
    nonce: str
    token: str
    descriptor: dict[str, Any]
    status: str = "pending"
    resolution: str | None = None
    resolved_at: float | None = None
    result_message: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "PendingApproval":
        return cls(**value)

    def public(self) -> dict[str, Any]:
        manifest = self.manifest
        return {
            "approval_id": self.approval_id, "session_id": self.session_id, "run_id": self.run_id,
            "manifest_hash": self.manifest_hash, "approval_token": self.token,
            "expires_at": self.expires_at, "risk": self.risk,
            "action_kind": manifest["action_kind"], "tool_slug": manifest.get("tool_slug"),
            "requested_capabilities": manifest["requested_capabilities"],
            "network_scopes": manifest["network_scopes"], "filesystem_scopes": manifest["filesystem_scopes"],
            "device_scopes": manifest["device_scopes"], "status": self.status,
        }
