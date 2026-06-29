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

# Cautious re-promotion (Phase 3): a tool that has *ever* failed must earn back
# trust with this many consecutive clean reuses before it returns to ``promoted``.
# A clean first-timer (no recorded failure) still promotes after a single reuse,
# so criterion C is preserved for the common case; only previously-broken tools
# pay the recovery tax. This guards against a flaky tool oscillating
# demoted→promoted→demoted on every other call.
PROMOTE_CLEAN_REUSES_AFTER_FAILURE = 2

STATUS_CANDIDATE = "candidate"
STATUS_PROMOTED = "promoted"
STATUS_RECOVERING = "recovering"
STATUS_DEMOTED = "demoted"
STATUS_BLOCKED = "blocked"
STATUS_INELIGIBLE = "ineligible"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _coerce_nonneg_int(value, default: int) -> int:
    """Coerce a sidecar value to a non-negative int, clamping anything invalid
    to ``default``. ``catalog.json`` is untrusted input (#52 §H): a tampered or
    corrupted value like ``"999"`` or ``None`` must never reach an int/str ``>=``
    comparison in ``_classify`` (that would raise TypeError and brick every
    catalog read — a denial of service)."""
    if isinstance(value, bool):  # bool is an int subclass; reject as a count
        return default
    try:
        out = int(value)
    except (TypeError, ValueError):
        return default
    return out if out >= 0 else default


def _coerce_bool(value, default: bool = False) -> bool:
    """Strict boolean coercion: only a real JSON bool is honored. A string like
    ``"true"`` is treated as malformed and falls back to ``default`` rather than
    being silently truthy."""
    return value if isinstance(value, bool) else default


def _coerce_opt_str(value) -> str | None:
    """Coerce to a string or ``None``; never let a non-string type through."""
    if value is None:
        return None
    return value if isinstance(value, str) else str(value)


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
    # Consecutive clean reuses since the last failure; drives cautious
    # re-promotion (Phase 3). Reset to 0 on any failure, incremented on each
    # successful reuse. Only meaningful once failure_count > 0.
    clean_streak: int = 0
    last_success_at: str | None = None
    last_failure_at: str | None = None
    last_failure_reason: str | None = None
    manual_approved: bool = False
    blocked: bool = False
    blocked_reason: str | None = None
    schema_version: int = SCHEMA_VERSION

    @classmethod
    def from_dict(cls, slug: str, data: dict) -> "ToolState":
        """Build a state from an *untrusted* sidecar dict, coercing every field
        to its expected type and clamping malformed values to safe defaults so a
        corrupted/tampered ``catalog.json`` can never crash a catalog read."""
        if not isinstance(data, dict):
            return cls(slug=slug)
        return cls(
            slug=slug,
            generation_success_count=_coerce_nonneg_int(
                data.get("generation_success_count"), 1
            ),
            reuse_success_count=_coerce_nonneg_int(
                data.get("reuse_success_count"), 0
            ),
            failure_count=_coerce_nonneg_int(data.get("failure_count"), 0),
            consecutive_failures=_coerce_nonneg_int(
                data.get("consecutive_failures"), 0
            ),
            clean_streak=_coerce_nonneg_int(data.get("clean_streak"), 0),
            last_success_at=_coerce_opt_str(data.get("last_success_at")),
            last_failure_at=_coerce_opt_str(data.get("last_failure_at")),
            last_failure_reason=_coerce_opt_str(data.get("last_failure_reason")),
            manual_approved=_coerce_bool(data.get("manual_approved")),
            blocked=_coerce_bool(data.get("blocked")),
            blocked_reason=_coerce_opt_str(data.get("blocked_reason")),
            schema_version=_coerce_nonneg_int(
                data.get("schema_version"), SCHEMA_VERSION
            ),
        )

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

        Omits raw metrics. ``source`` is included for provenance only (matching
        the issue's example surface); it is NOT an execution handle. Execution
        always routes through the DynamicToolRunner reuse path keyed by ``slug``,
        so a planner must never select or run an arbitrary ``source`` path taken
        from model output (#52 §H)."""
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
            # Cautious recovery (Phase 3): a tool with a failure in its history
            # must rebuild a clean streak before it returns to promoted, unless
            # an operator has manually vouched for it. A clean first-timer skips
            # this entirely. RECOVERING is still a reusable tier — it just isn't
            # advertised as a trusted known tool yet.
            if (
                st.failure_count > 0
                and not st.manual_approved
                and st.clean_streak < PROMOTE_CLEAN_REUSES_AFTER_FAILURE
            ):
                return STATUS_RECOVERING, (
                    f"rebuilding trust after failure: "
                    f"{st.clean_streak}/{PROMOTE_CLEAN_REUSES_AFTER_FAILURE} "
                    f"clean reuses"
                )
            reasons = []
            reasons.append("validation_pass")
            if st.reuse_success_count >= 1:
                reasons.append(f"successful_reuse×{st.reuse_success_count}")
            if st.failure_count > 0:
                reasons.append("recovered")
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
        promoted + recovering (not demoted/blocked/ineligible). A recovering
        tool is still offered for reuse — that is how it earns its clean streak
        back — it is simply not yet advertised as a trusted known tool."""
        return [
            e
            for e in self.entries()
            if e.status in (STATUS_CANDIDATE, STATUS_PROMOTED, STATUS_RECOVERING)
        ]

    def reuse_suppressed(self) -> set[str]:
        """Slugs the in-``/new`` reuse path must skip: demoted (repeated reuse
        failures) or manually blocked (safety). Read straight from the sidecar
        so a tool with no recorded history is never suppressed.

        Dependency/path classification governs *planner* exposure (Phase 4), not
        in-``/new`` reuse — the existing generated-tool venv already installs
        declared requires — so it is intentionally excluded here."""
        out: set[str] = set()
        for slug, st in self._load_states().items():
            if st.blocked or st.consecutive_failures >= self.demote_threshold:
                out.add(slug)
        return out

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
            st.clean_streak += 1
            st.last_success_at = _utc_now_iso()

        return self._mutate(slug, fn)

    def record_failure(self, slug: str, reason: str | None = None) -> CatalogEntry | None:
        def fn(st: ToolState) -> None:
            st.failure_count += 1
            st.consecutive_failures += 1
            st.clean_streak = 0
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
