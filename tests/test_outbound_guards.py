"""Unit tests for outbound_guards."""
from __future__ import annotations

import json

import pytest

from openclaw_adapter.outbound_guards import (
    guard_outbound,
    looks_like_meta_scaffolding,
    looks_like_raw_dump,
    proactive_push_failure,
)


CLEAN_PROSE = "限定コラボ！ユニクロ × チェンソーマン T シャツが本日発売されました。"


# ---------------------------------------------------------------------------
# looks_like_raw_dump
# ---------------------------------------------------------------------------

def test_raw_dump_json_object():
    payload = json.dumps({"items": list(range(100)), "page": 1}) * 3
    assert payload and len(payload) > 400
    reason = looks_like_raw_dump(payload)
    assert reason is not None
    assert "JSON" in reason


def test_raw_dump_json_array():
    payload = json.dumps([{"id": i, "name": f"item_{i}"} for i in range(50)])
    assert len(payload) > 400
    reason = looks_like_raw_dump(payload)
    assert reason is not None


def test_raw_dump_xml():
    payload = "<?xml version='1.0'?><root><item>foo</item></root>"
    reason = looks_like_raw_dump(payload)
    assert reason is not None
    assert "XML" in reason or "xml" in reason.lower()


def test_raw_dump_rss():
    payload = "<rss version='2.0'><channel><title>test</title></channel></rss>"
    reason = looks_like_raw_dump(payload)
    assert reason is not None


def test_raw_dump_html():
    payload = "<html><body><p>Hello</p></body></html>"
    reason = looks_like_raw_dump(payload)
    assert reason is not None


def test_raw_dump_short_json_passes():
    payload = json.dumps({"ok": True})
    assert len(payload) < 400
    assert looks_like_raw_dump(payload) is None


def test_raw_dump_clean_prose_passes():
    assert looks_like_raw_dump(CLEAN_PROSE) is None


def test_raw_dump_part_header_hidden():
    inner = json.dumps({"items": list(range(100)), "page": 1}) * 3
    wrapped = f"--- https://example.com/api ---\n{inner}"
    reason = looks_like_raw_dump(wrapped)
    assert reason is not None, "dump hidden behind part-header must be detected"


def test_raw_dump_multiple_part_headers():
    inner = "<rss version='2.0'><channel><title>feed</title></channel></rss>"
    wrapped = f"--- https://a.com ---\n--- https://b.com ---\n{inner}"
    reason = looks_like_raw_dump(wrapped)
    assert reason is not None


# ---------------------------------------------------------------------------
# looks_like_meta_scaffolding
# ---------------------------------------------------------------------------

def test_meta_as_an_ai():
    text = "As an AI language model, I cannot access the internet."
    reason = looks_like_meta_scaffolding(text)
    assert reason is not None


def test_meta_cut_off():
    text = "It seems like the text got cut off. Please provide the full content."
    reason = looks_like_meta_scaffolding(text)
    assert reason is not None


def test_meta_this_page_appears():
    text = "This page appears to contain the product listing you requested."
    reason = looks_like_meta_scaffolding(text)
    assert reason is not None


def test_meta_clean_passes():
    assert looks_like_meta_scaffolding(CLEAN_PROSE) is None


def test_meta_in_tail():
    body = "A" * 1300
    tail = "text got cut off"
    reason = looks_like_meta_scaffolding(body + tail)
    assert reason is not None


# ---------------------------------------------------------------------------
# proactive_push_failure
# ---------------------------------------------------------------------------

def test_proactive_fetch_failed():
    text = "fetch_failed: timeout connecting to mercari"
    reason = proactive_push_failure(text)
    assert reason is not None
    assert "fetch_failed" in reason


def test_proactive_http_404():
    text = "HTTP 404 returned from endpoint"
    reason = proactive_push_failure(text)
    assert reason is not None


def test_proactive_traceback():
    text = "Traceback (most recent call last):\n  File 'x.py', line 1\nValueError"
    reason = proactive_push_failure(text)
    assert reason is not None


def test_proactive_no_items():
    text = "no_items_retrieved for this query"
    reason = proactive_push_failure(text)
    assert reason is not None


def test_proactive_clean_passes():
    assert proactive_push_failure(CLEAN_PROSE) is None


def test_proactive_forbidden_word_not_blocked():
    text = "This product is forbidden for sale in this region."
    assert proactive_push_failure(text) is None


def test_proactive_rate_limited_phrase_not_blocked():
    text = "The item was rate-limited by the platform due to demand."
    assert proactive_push_failure(text) is None


def test_proactive_also_checks_raw_dump():
    payload = json.dumps({"items": list(range(100)), "page": 1}) * 3
    reason = proactive_push_failure(payload)
    assert reason is not None


# ---------------------------------------------------------------------------
# guard_outbound composition
# ---------------------------------------------------------------------------

def test_guard_outbound_proactive_blocks_ops_token():
    text = "no content to evaluate at this time"
    reason = guard_outbound(text, proactive=True)
    assert reason is not None


def test_guard_outbound_non_proactive_ignores_ops_token():
    text = "no content to evaluate at this time"
    reason = guard_outbound(text, proactive=False)
    assert reason is None


def test_guard_outbound_meta_always_blocked():
    text = "As an AI, I cannot access that URL."
    assert guard_outbound(text, proactive=True) is not None
    assert guard_outbound(text, proactive=False) is not None


def test_guard_outbound_clean_passes_both():
    assert guard_outbound(CLEAN_PROSE, proactive=True) is None
    assert guard_outbound(CLEAN_PROSE, proactive=False) is None


def test_guard_outbound_empty_text():
    assert guard_outbound("", proactive=True) is None
    assert guard_outbound("   ", proactive=False) is None
