"""Deterministic continuation policy for chat-tool results (#81 PR1).

After a chat tool runs, the bridge must decide one of three things: answer the
user as-is, surface the tool's own failure, or escalate into the goal loop. That
decision used to be a chain of ad-hoc ``if`` branches reading a raw verdict dict.

This module makes the decision an explicit, testable, transport-agnostic
function. It is deliberately **generic**: the "is this answer complete?" judgment
is NOT made here with domain rules — it is supplied by the upstream LLM
satisfaction judge (its ``satisfied`` / ``environment_blocked`` / ``reason``
verdict). This module only maps that verdict onto a typed outcome and a typed
action, and normalises an ``operation_key`` so callers can tell whether two tool
runs did the same expensive work. No keyword lists, no per-goal special cases.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum


class OutcomeStatus(str, Enum):
    """How complete a single tool run was, per the upstream LLM verdict."""

    COMPLETE = "complete"
    PARTIAL = "partial"
    BLOCKED = "blocked"
    FAILED = "failed"


class ContinuationAction(str, Enum):
    """What the bridge should do next with a tool result."""

    ANSWER = "answer"
    SURFACE_FAILURE = "surface_failure"
    ESCALATE_GOAL_LOOP = "escalate_goal_loop"


@dataclass(frozen=True)
class ToolOutcome:
    """Typed verdict about one tool run.

    ``missing_evidence`` is the LLM judge's own free-text reason for why the
    answer fell short — carried forward so a downstream synthesiser can state
    plainly what is still unknown, instead of re-deriving it from keywords.
    ``artifacts`` holds structural signals (the produced answer, source count)
    for callers that want them; it is never inspected for domain content here.
    """

    status: OutcomeStatus
    operation_key: str
    missing_evidence: str = ""
    artifacts: dict[str, object] = field(default_factory=dict)

    @property
    def is_complete(self) -> bool:
        return self.status is OutcomeStatus.COMPLETE


def operation_key(tool: str, query: str) -> str:
    """Stable identity for "this tool with this query".

    Purely structural: lowercase the tool name, collapse the query's
    whitespace and case. Two runs sharing a key did the same expensive work, so
    a planner/policy can refuse to repeat it. No semantic understanding.
    """

    tool_slug = (tool or "").strip().lower()
    normalized_query = re.sub(r"\s+", " ", (query or "").strip().lower())
    return f"{tool_slug}::{normalized_query}"


def classify_outcome(
    verdict: dict[str, object],
    *,
    tool: str,
    query: str,
    answer: str = "",
    source_count: int = 0,
) -> ToolOutcome:
    """Turn an LLM satisfaction ``verdict`` into a typed :class:`ToolOutcome`.

    The verdict is produced upstream by the satisfaction judge and carries the
    only completeness signal we trust:

    - ``satisfied`` truthy            -> COMPLETE
    - ``environment_blocked`` truthy  -> BLOCKED (a device/network wall retrying
      can't route around)
    - otherwise                       -> PARTIAL (an answer exists but the judge
      says it's short of the intent)

    ``FAILED`` is reserved for callers that have no verdict at all (e.g. the
    tool raised) and want to signal a hard failure explicitly.
    """

    key = operation_key(tool, query)
    reason = str(verdict.get("reason") or "")
    artifacts: dict[str, object] = {"answer": answer, "source_count": int(source_count)}
    if bool(verdict.get("satisfied")):
        return ToolOutcome(OutcomeStatus.COMPLETE, key, artifacts=artifacts)
    if bool(verdict.get("environment_blocked")):
        return ToolOutcome(
            OutcomeStatus.BLOCKED, key, missing_evidence=reason, artifacts=artifacts
        )
    return ToolOutcome(
        OutcomeStatus.PARTIAL, key, missing_evidence=reason, artifacts=artifacts
    )


def decide_continuation(outcome: ToolOutcome) -> ContinuationAction:
    """Map a typed outcome onto the next action. Total and deterministic."""

    if outcome.status is OutcomeStatus.COMPLETE:
        return ContinuationAction.ANSWER
    if outcome.status is OutcomeStatus.BLOCKED:
        return ContinuationAction.SURFACE_FAILURE
    # PARTIAL / FAILED: an answer may exist but it's short of intent — hand what
    # we have to the goal loop, which builds on it rather than re-running.
    return ContinuationAction.ESCALATE_GOAL_LOOP
