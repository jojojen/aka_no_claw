from __future__ import annotations

import json
from io import BytesIO
from unittest.mock import MagicMock, patch

from openclaw_adapter.web_search import search_need_gate


_ENDPOINT = "http://localhost:11434"
_MODEL = "qwen3:14b"
_TIMEOUT = 30


def _make_response(search_needed: bool, reason: str = "test"):
    body = json.dumps({"response": json.dumps({"search_needed": search_needed, "reason": reason})}).encode()
    resp = MagicMock()
    resp.read.return_value = body
    resp.__enter__ = lambda s: s
    resp.__exit__ = MagicMock(return_value=False)
    return resp


def _make_unparseable_response():
    body = json.dumps({"response": "not json at all"}).encode()
    resp = MagicMock()
    resp.read.return_value = body
    resp.__enter__ = lambda s: s
    resp.__exit__ = MagicMock(return_value=False)
    return resp


def test_gate_yes_search_runs(monkeypatch):
    search_called = []

    with patch("openclaw_adapter.web_search.urlopen", return_value=_make_response(True)):
        result = search_need_gate(
            "find latest TCG releases",
            "",
            endpoint=_ENDPOINT,
            model=_MODEL,
            timeout_seconds=_TIMEOUT,
        )

    assert result is True


def test_gate_no_search_skipped(monkeypatch):
    with patch("openclaw_adapter.web_search.urlopen", return_value=_make_response(False, "seed is sufficient")):
        result = search_need_gate(
            "find latest TCG releases",
            "already have 50 rows of TCG data from DB",
            endpoint=_ENDPOINT,
            model=_MODEL,
            timeout_seconds=_TIMEOUT,
        )

    assert result is False


def test_gate_unparseable_twice_fail_open():
    with patch("openclaw_adapter.web_search.urlopen", return_value=_make_unparseable_response()):
        result = search_need_gate(
            "some task",
            "",
            endpoint=_ENDPOINT,
            model=_MODEL,
            timeout_seconds=_TIMEOUT,
        )

    assert result is True


def test_gate_llm_down_fail_open():
    with patch("openclaw_adapter.web_search.urlopen", side_effect=OSError("connection refused")):
        result = search_need_gate(
            "some task",
            "",
            endpoint=_ENDPOINT,
            model=_MODEL,
            timeout_seconds=_TIMEOUT,
        )

    assert result is True
