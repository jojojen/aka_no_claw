"""Growing reusable tool catalog over validated ``/new`` outputs (issue #52).

``/new`` already records every successful generated tool in
``generated_tools/manifest.json``. That manifest is the *raw memory*: what was
generated, its ``tool_type``, ``param_schema``, dependencies, and source path.

This module is the **catalog layer** on top of that raw memory (Phase 1 of #52).
It does not change ``/new`` generation or reuse at all. It reads the manifest,
classifies each tool into a lifecycle status (candidate / promoted / demoted /
blocked / ineligible), and tracks success/failure metrics in a *separate*
sidecar file ``generated_tools/catalog.json`` so the manifest stays untouched.

Design constraints carried from the issue (safety, §H):
  - the manifest remains the only source of generated-tool metadata,
  - a tool is only ever a *candidate* if it is parameterized (has a usable
    ``tool_type`` + ``param_schema``) and its source path stays under
    ``generated_tools/`` — never an absolute or ``..`` path,
  - dependencies must be stdlib-only (empty ``requires``) or explicitly approved,
  - status is *derived* from metrics + manifest every read (never trusted from a
    stored field) so a tampered ``status`` cannot promote a tool.

Later phases (fast-path reuse from the planner, top-k retrieval, self-healing)
build on the views exposed here.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

MANIFEST_NAME = "manifest.json"
CATALOG_NAME = "catalog.json"
SCHEMA_VERSION = 1

# A promoted tool that fails this many times in a row is demoted (#52 §6).
DEMOTE_CONSECUTIVE_FAILURES = 3

STATUS_CANDIDATE = "candidate"
STATUS_PROMOTED = "promoted"
STATUS_DEMOTED = "demoted"
STATUS_BLOCKED = "blocked"
STATUS_INELIGIBLE = "ineligible"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


@dataclass
class ToolState:
    """Mutable lifecycle metrics for one generated tool, keyed by slug.

    Persisted in ``catalog.json``. Holds only inputs to the status decision —
    the status itself is recomputed on every read so it can never drift from
    the metrics or be forged by editing the sidecar file."""

    slug: str
    generation_success_count: int = 1
    reuse_success_count: int = 0
    failure_count: int = 0
    consecutive_failures: int = 0
    last_success_at: str | None = None
    last_failure_at: str | None = None
    last_failure_reason: str | None = None
    manual_approved: bool = False
    blocked: bool = False
    blocked_reason: str | None = None
    schema_version: int = SCHEMA_VERSION

    @classmethod
    def from_dict(cls, slug: str, data: dict) -> "ToolState":
        known = {f for f in cls.__dataclass_fields__ if f != "slug"}
        kwargs = {k: v for k, v in data.items() if k in known}
        return cls(slug=slug, **kwargs)

    def to_dict(self) -> dict:
        d = self.__dict__.copy()
        d.pop("slug", None)
        return d


@dataclass
class CatalogEntry:
    """A manifest tool merged with its lifecycle state and derived status.

    This is the read-only view the rest of the system consumes; it is rebuilt
    from manifest + state on every catalog read."""

    slug: str
    tool_type: str | None
    description: str
    param_schema: list
    source: str
    requires: list
    created_at: str | None
    status: str
    promotion_reason: str | None
    metrics: dict
    safety_profile: dict
    schema_version: int = SCHEMA_VERSION

    @property
    def name(self) -> str:
        return f"generated.{self.slug}"

    def planner_view(self) -> dict:
        """Compact schema a planner/Chat layer can retrieve (#52 §3 surface).

        Deliberately omits raw metrics and never exposes an executable path the
        model could choose directly — execution always goes back through the
        DynamicToolRunner reuse path keyed by slug."""
        return {
            "name": self.name,
            "tool_type": self.tool_type,
            "description": self.description,
            "param_schema": self.param_schema,
            "source": self.source,
            "status": self.status,
            "execution": "dynamic_tool_runner_reuse",
        }


def _is_parameterized(entry: dict) -> bool:
    """A reusable tool must carry a tool_type and a non-empty param_schema;
    otherwise it is a static one-off answer that must not be promoted (#52 §D)."""
    schema = entry.get("param_schema")
    return bool(entry.get("tool_type")) and isinstance(schema, list) and len(schema) > 0


def _path_under_tools(path: str | None) -> bool:
    """Source path must stay relative and inside generated_tools/ (#52 §H)."""
    if not path or not isinstance(path, str):
        return False
    p = Path(path)
    return not p.is_absolute() and ".." not in p.parts


class GeneratedToolCatalog:
    """Catalog view + lifecycle bookkeeping over ``generated_tools/manifest.json``.

    Read-only with respect to the manifest. All mutable state lives in
    ``catalog.json`` next to it.
    """

    def __init__(
        self,
        tools_dir: str | Path,
        *,
        approved_requires: Iterable[str] = (),
        demote_threshold: int = DEMOTE_CONSECUTIVE_FAILURES,
        require_manual_approval: bool = False,
    ) -> None:
        self.tools_dir = Path(tools_dir)
        self.approved_requires = {r.strip().lower() for r in approved_requires if r}
        self.demote_threshold = demote_threshold
        # When True a candidate only promotes via an explicit approve(); when
        # False a single successful reuse auto-promotes (#52 §C, config-driven).
        self.require_manual_approval = require_manual_approval

    # ---- raw I/O -----------------------------------------------------------

    def _manifest_path(self) -> Path:
        return self.tools_dir / MANIFEST_NAME

    def _catalog_path(self) -> Path:
        return self.tools_dir / CATALOG_NAME

    def _load_manifest(self) -> list[dict]:
        path = self._manifest_path()
        if not path.exists():
            return []
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, list) else []
        except (OSError, ValueError):
            return []

    def _load_states(self) -> dict[str, ToolState]:
        path = self._catalog_path()
        if not path.exists():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        if not isinstance(data, dict):
            return {}
        out: dict[str, ToolState] = {}
        for slug, sd in data.items():
            if isinstance(sd, dict):
                out[slug] = ToolState.from_dict(slug, sd)
        return out

    def _save_states(self, states: dict[str, ToolState]) -> None:
        payload = {slug: st.to_dict() for slug, st in states.items()}
        self.tools_dir.mkdir(parents=True, exist_ok=True)
        self._catalog_path().write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    # ---- classification ----------------------------------------------------

    def _deps_ok(self, entry: dict) -> bool:
        requires = entry.get("requires") or []
        if not isinstance(requires, list):
            return False
        return all(str(r).strip().lower() in self.approved_requires for r in requires)

    def _safety_profile(self, entry: dict) -> dict:
        return {
            "requires": list(entry.get("requires") or []),
            "network": "allowed_via_generated_tool_runner",
            "writes": "generated_tools_only",
        }

    def _classify(self, entry: dict, state: ToolState | None) -> tuple[str, str | None]:
        """Derive lifecycle status from manifest entry + metrics. Pure function:
        same inputs → same status, so it can never drift from stored metrics."""
        st = state or ToolState(slug=entry.get("slug", ""))

        if st.blocked:
            return STATUS_BLOCKED, st.blocked_reason or "manually blocked"
        if not _path_under_tools(entry.get("path")):
            return STATUS_BLOCKED, "source path escapes generated_tools/"
        if not self._deps_ok(entry):
            return STATUS_BLOCKED, "unapproved dependency"
        if not _is_parameterized(entry):
            # No usable schema/tool_type → static one-off; exact /new reuse may
            # still work, but it is never a general known tool (#52 §D).
            return STATUS_INELIGIBLE, "not parameterized (static one-off)"

        # Manifest presence == at least one validation-pass generation (#52 §3).
        if st.consecutive_failures >= self.demote_threshold:
            return STATUS_DEMOTED, (
                f"{st.consecutive_failures} consecutive failures"
            )

        eligible_to_promote = (
            st.manual_approved
            if self.require_manual_approval
            else (st.reuse_success_count >= 1 or st.manual_approved)
        )
        if eligible_to_promote and st.consecutive_failures == 0:
            reasons = []
            reasons.append("validation_pass")
            if st.reuse_success_count >= 1:
                reasons.append(f"successful_reuse×{st.reuse_success_count}")
            if st.manual_approved:
                reasons.append("manual_approval")
            return STATUS_PROMOTED, " + ".join(reasons)

        return STATUS_CANDIDATE, "validation_pass"

    def _build_entry(self, entry: dict, state: ToolState | None) -> CatalogEntry:
        status, reason = self._classify(entry, state)
        st = state or ToolState(slug=entry.get("slug", ""))
        path = entry.get("path") or ""
        source = f"generated_tools/{path}" if path else ""
        return CatalogEntry(
            slug=entry.get("slug", ""),
            tool_type=entry.get("tool_type"),
            description=entry.get("description") or entry.get("request") or "",
            param_schema=entry.get("param_schema") or [],
            source=source,
            requires=list(entry.get("requires") or []),
            created_at=entry.get("created_at"),
            status=status,
            promotion_reason=reason,
            metrics=st.to_dict(),
            safety_profile=self._safety_profile(entry),
        )

    # ---- views -------------------------------------------------------------

    def entries(self) -> list[CatalogEntry]:
        states = self._load_states()
        return [
            self._build_entry(e, states.get(e.get("slug", "")))
            for e in self._load_manifest()
            if e.get("slug")
        ]

    def get(self, slug: str) -> CatalogEntry | None:
        for e in self.entries():
            if e.slug == slug:
                return e
        return None

    def candidates(self) -> list[CatalogEntry]:
        return [e for e in self.entries() if e.status == STATUS_CANDIDATE]

    def promoted(self) -> list[CatalogEntry]:
        return [e for e in self.entries() if e.status == STATUS_PROMOTED]

    def reusable(self) -> list[CatalogEntry]:
        """Tools the system may consider for reuse evaluation: candidates +
        promoted (not demoted/blocked/ineligible)."""
        return [
            e
            for e in self.entries()
            if e.status in (STATUS_CANDIDATE, STATUS_PROMOTED)
        ]

    # ---- metric mutations --------------------------------------------------

    def _mutate(self, slug: str, fn) -> CatalogEntry | None:
        manifest = {e.get("slug"): e for e in self._load_manifest()}
        if slug not in manifest:
            return None
        states = self._load_states()
        st = states.get(slug) or ToolState(slug=slug)
        fn(st)
        states[slug] = st
        self._save_states(states)
        return self._build_entry(manifest[slug], st)

    def register(self, slug: str) -> CatalogEntry | None:
        """Ensure a state row exists for a freshly generated tool (idempotent)."""
        return self._mutate(slug, lambda st: None)

    def record_generation_success(self, slug: str) -> CatalogEntry | None:
        def fn(st: ToolState) -> None:
            st.generation_success_count += 1
            st.consecutive_failures = 0
            st.last_success_at = _utc_now_iso()

        return self._mutate(slug, fn)

    def record_reuse_success(self, slug: str) -> CatalogEntry | None:
        def fn(st: ToolState) -> None:
            st.reuse_success_count += 1
            st.consecutive_failures = 0
            st.last_success_at = _utc_now_iso()

        return self._mutate(slug, fn)

    def record_failure(self, slug: str, reason: str | None = None) -> CatalogEntry | None:
        def fn(st: ToolState) -> None:
            st.failure_count += 1
            st.consecutive_failures += 1
            st.last_failure_at = _utc_now_iso()
            st.last_failure_reason = reason

        return self._mutate(slug, fn)

    def approve(self, slug: str) -> CatalogEntry | None:
        def fn(st: ToolState) -> None:
            st.manual_approved = True

        return self._mutate(slug, fn)

    def block(self, slug: str, reason: str | None = None) -> CatalogEntry | None:
        def fn(st: ToolState) -> None:
            st.blocked = True
            st.blocked_reason = reason

        return self._mutate(slug, fn)

    def unblock(self, slug: str) -> CatalogEntry | None:
        def fn(st: ToolState) -> None:
            st.blocked = False
            st.blocked_reason = None
            st.consecutive_failures = 0

        return self._mutate(slug, fn)
