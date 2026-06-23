"""Broken lifecycle classifier.

It retries or parses immediately even when the capture clearly hit a cooldown
condition. The benchmark repair should return cooldown_wait for rate limits and
bot interstitials.
"""
from __future__ import annotations


SCHEMA_KEYS = (
    "case_id",
    "action",
    "retry_after_seconds",
    "should_parse",
    "should_requeue",
    "reason",
)


def classify(capture: dict[str, object]) -> dict[str, object]:
    status = int(capture.get("http_status") or 0)
    if status >= 500:
        return {
            "case_id": str(capture.get("case_id") or ""),
            "action": "retry_immediately",
            "retry_after_seconds": 0,
            "should_parse": False,
            "should_requeue": True,
            "reason": "server_error",
        }
    return {
        "case_id": str(capture.get("case_id") or ""),
        "action": "parse_now",
        "retry_after_seconds": 0,
        "should_parse": True,
        "should_requeue": False,
        "reason": "ready",
    }
