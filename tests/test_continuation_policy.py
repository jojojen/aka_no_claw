"""Tests for continuation_policy.py (#81 PR1)."""

from __future__ import annotations

from openclaw_adapter.continuation_policy import (
    ContinuationAction,
    OutcomeStatus,
    classify_outcome,
    decide_continuation,
    operation_key,
)


def test_operation_key_is_structural_and_stable():
    a = operation_key("/research", "  Mercari  m123  ")
    b = operation_key("/RESEARCH", "mercari m123")
    assert a == b == "/research::mercari m123"


def test_satisfied_verdict_is_complete_and_answers():
    outcome = classify_outcome(
        {"satisfied": True, "reason": "done"},
        tool="/research",
        query="m123",
        answer="ok",
        source_count=4,
    )
    assert outcome.status is OutcomeStatus.COMPLETE
    assert outcome.is_complete is True
    assert outcome.artifacts == {"answer": "ok", "source_count": 4}
    assert decide_continuation(outcome) is ContinuationAction.ANSWER


def test_environment_blocked_verdict_surfaces_failure():
    outcome = classify_outcome(
        {"satisfied": False, "environment_blocked": True, "reason": "device offline"},
        tool="/light",
        query="on",
    )
    assert outcome.status is OutcomeStatus.BLOCKED
    assert outcome.missing_evidence == "device offline"
    assert decide_continuation(outcome) is ContinuationAction.SURFACE_FAILURE


def test_unsatisfied_verdict_is_partial_and_escalates_carrying_reason():
    outcome = classify_outcome(
        {"satisfied": False, "reason": "只給市價，未算獲利"},
        tool="/research",
        query="m123",
        answer="市價¥16000",
        source_count=0,
    )
    assert outcome.status is OutcomeStatus.PARTIAL
    assert outcome.missing_evidence == "只給市價，未算獲利"
    assert decide_continuation(outcome) is ContinuationAction.ESCALATE_GOAL_LOOP


def test_missing_reason_defaults_empty_not_none():
    outcome = classify_outcome({"satisfied": False}, tool="/x", query="q")
    assert outcome.missing_evidence == ""
