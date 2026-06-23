"""Reference lifecycle classifier for synthetic seller snapshot capture outcomes."""
from __future__ import annotations


SCHEMA_KEYS = (
    "case_id",
    "action",
    "retry_after_seconds",
    "should_parse",
    "should_requeue",
    "reason",
)

_DEFAULT_COOLDOWN_SECONDS = 300


def _retry_after_seconds(headers: object) -> int:
    if not isinstance(headers, dict):
        return _DEFAULT_COOLDOWN_SECONDS
    raw = headers.get("Retry-After") or headers.get("retry-after")
    try:
        return max(_DEFAULT_COOLDOWN_SECONDS, int(str(raw)))
    except (TypeError, ValueError):
        return _DEFAULT_COOLDOWN_SECONDS


def _blocked_text(text: object) -> bool:
    if not isinstance(text, str):
        return False
    lowered = text.lower()
    markers = (
        "request limit",
        "too many requests",
        "verify that you are not automated",
        "not automated",
        "access temporarily",
        "please wait",
    )
    return any(marker in lowered for marker in markers)


def classify(capture: dict[str, object]) -> dict[str, object]:
    case_id = str(capture.get("case_id") or "")
    status = int(capture.get("http_status") or 0)
    body_text = capture.get("body_text")

    if status == 429:
        return {
            "case_id": case_id,
            "action": "cooldown_wait",
            "retry_after_seconds": _retry_after_seconds(capture.get("headers")),
            "should_parse": False,
            "should_requeue": True,
            "reason": "rate_limited",
        }

    if _blocked_text(body_text):
        return {
            "case_id": case_id,
            "action": "cooldown_wait",
            "retry_after_seconds": _DEFAULT_COOLDOWN_SECONDS,
            "should_parse": False,
            "should_requeue": True,
            "reason": "bot_interstitial",
        }

    return {
        "case_id": case_id,
        "action": "parse_now",
        "retry_after_seconds": 0,
        "should_parse": True,
        "should_requeue": False,
        "reason": "ready",
    }
